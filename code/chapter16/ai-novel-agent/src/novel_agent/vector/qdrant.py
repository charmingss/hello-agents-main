from __future__ import annotations

import importlib
import inspect
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import ValidationError

from novel_agent.embeddings.ports import EmbeddingDistance, EmbeddingIdentity
from novel_agent.vector.schema import (
    COLLECTION_ALIAS,
    VECTOR_SCHEMA_VERSION,
    ChunkVectorPayload,
    ProjectionFilter,
    SourceCitation,
    TenantProjectScope,
    VectorPoint,
    VectorSearchHit,
    _validate_vector,
    collection_alias_name,
    deterministic_point_id,
    physical_collection_name,
)

MAX_UPSERT_BATCH = 256
MAX_SEARCH_LIMIT = 100


@dataclass(frozen=True)
class CollectionDescription:
    name: str
    dimension: int
    distance: EmbeddingDistance


@dataclass(frozen=True)
class MatchCondition:
    key: str
    value: str


@dataclass(frozen=True)
class ScopedFilter:
    must: tuple[MatchCondition, ...]


@dataclass(frozen=True)
class ScoredPoint:
    id: uuid.UUID | str
    score: float
    payload: Mapping[str, Any]


class QdrantTransport(Protocol):
    """Small semantic port implemented by the optional official-client adapter."""

    async def describe_collection(self, name: str) -> CollectionDescription | None: ...

    async def create_collection(self, value: CollectionDescription) -> None: ...

    async def alias_target(self, alias: str) -> str | None: ...

    async def switch_alias(self, alias: str, collection: str) -> None: ...

    async def upsert(self, collection: str, points: tuple[VectorPoint, ...]) -> None: ...

    async def search(
        self,
        collection: str,
        vector: tuple[float, ...],
        query_filter: ScopedFilter,
        limit: int,
    ) -> tuple[ScoredPoint, ...]: ...

    async def delete(self, collection: str, query_filter: ScopedFilter) -> None: ...

    async def count(self, collection: str, query_filter: ScopedFilter) -> int: ...

    async def aclose(self) -> None: ...


ProjectionOperation = Literal["ensure", "upsert", "search", "delete", "count"]
QdrantErrorCode = Literal[
    "qdrant_unavailable",
    "collection_schema_conflict",
    "invalid_configuration",
    "qdrant_client_missing",
    "qdrant_installation_broken",
]


@dataclass(frozen=True)
class ProjectionFailure:
    operation: ProjectionOperation
    tenant_id: uuid.UUID | None
    project_id: uuid.UUID | None
    collection: str
    embedding_identity_sha256: str
    error_code: QdrantErrorCode


class ProjectionLagRecorder(Protocol):
    """Task 10 may persist this after the authoritative PostgreSQL transaction."""

    async def record(self, failure: ProjectionFailure) -> None: ...


class NoopProjectionLagRecorder:
    """Explicit opt-out for tests/tools that intentionally do not persist projection lag."""

    async def record(self, failure: ProjectionFailure) -> None:
        del failure


class QdrantProjectionError(RuntimeError):
    """Stable, redacted vector projection error."""

    def __init__(self, code: QdrantErrorCode) -> None:
        self.code = code
        super().__init__(code)


class ProjectionConfigurationError(RuntimeError):
    """Stable lifecycle/configuration rejection with no transport interaction."""

    def __init__(self, code: Literal["projection_closed"]) -> None:
        self.code = code
        super().__init__(code)


class QdrantProjection:
    """Projection adapter with mandatory tenant/project isolation on every data API."""

    def __init__(
        self,
        *,
        transport: QdrantTransport,
        embedding: EmbeddingIdentity,
        lag_recorder: ProjectionLagRecorder,
        owns_transport: bool,
        alias: str = COLLECTION_ALIAS,
    ) -> None:
        if lag_recorder is None:
            raise ValueError("projection lag recorder is required")
        self._transport = transport
        self.embedding = embedding
        self.alias = collection_alias_name(embedding, logical_alias=alias)
        self.collection = physical_collection_name(embedding)
        self._lag_recorder = lag_recorder
        self._owns_transport = owns_transport
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise ProjectionConfigurationError("projection_closed")

    async def _ensure_collection(self) -> str:
        expected = CollectionDescription(
            name=self.collection,
            dimension=self.embedding.dimension,
            distance=self.embedding.distance,
        )
        current = await self._transport.describe_collection(self.collection)
        if current is None:
            try:
                await self._transport.create_collection(expected)
            except Exception:
                # A concurrent creator may have won; inspect instead of deleting/recreating.
                current = await self._transport.describe_collection(self.collection)
                if current is None:
                    raise
            else:
                current = await self._transport.describe_collection(self.collection)
        if current != expected:
            raise QdrantProjectionError("collection_schema_conflict")
        target = await self._transport.alias_target(self.alias)
        if target != self.collection:
            switch_failed = False
            try:
                await self._transport.switch_alias(self.alias, self.collection)
            except Exception:
                switch_failed = True
            verified_target = await self._transport.alias_target(self.alias)
            if verified_target != self.collection:
                if switch_failed:
                    raise QdrantProjectionError("qdrant_unavailable")
                raise QdrantProjectionError("collection_schema_conflict")
        return self.collection

    async def ensure_collection(self) -> str:
        self._require_open()
        try:
            result = await self._ensure_collection()
        except QdrantProjectionError as error:
            code: QdrantErrorCode = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            return result
        await self._record_failure("ensure", None, code)
        raise QdrantProjectionError(code) from None

    async def upsert(self, scope: TenantProjectScope, points: tuple[VectorPoint, ...]) -> None:
        self._require_open()
        if not points or len(points) > MAX_UPSERT_BATCH:
            raise ValueError("upsert batch limit exceeded")
        for point in points:
            if (
                point.payload.tenant_id != scope.tenant_id
                or point.payload.project_id != scope.project_id
            ):
                raise ValueError("point payload scope mismatch")
            if not point.payload.matches_embedding(self.embedding):
                raise ValueError("point embedding identity mismatch")
            _validate_vector(point.vector, self.embedding.dimension)
            if point.id != deterministic_point_id(
                point.payload, self.embedding, schema_version=VECTOR_SCHEMA_VERSION
            ):
                raise ValueError("point id does not match deterministic projection identity")
        try:
            collection = await self._ensure_collection()
            await self._transport.upsert(collection, points)
        except QdrantProjectionError as error:
            code: QdrantErrorCode = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            return
        await self._record_failure("upsert", scope, code)
        raise QdrantProjectionError(code) from None

    async def search(
        self,
        scope: TenantProjectScope,
        vector: tuple[float, ...],
        *,
        limit: int,
        where: ProjectionFilter | None = None,
    ) -> tuple[VectorSearchHit, ...]:
        self._require_open()
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError("search limit must be between 1 and 100")
        _validate_vector(vector, self.embedding.dimension)
        query_filter = _scoped_filter(scope, where, active_only=True)
        try:
            collection = await self._ensure_collection()
            raw_hits = await self._transport.search(collection, vector, query_filter, limit)
            result = tuple(_search_hit(hit, scope, self.embedding) for hit in raw_hits)
        except (ValidationError, ValueError):
            code: QdrantErrorCode = "collection_schema_conflict"
        except QdrantProjectionError as error:
            code = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            return result
        await self._record_failure("search", scope, code)
        raise QdrantProjectionError(code) from None

    async def delete(
        self,
        scope: TenantProjectScope,
        *,
        where: ProjectionFilter | None = None,
    ) -> None:
        self._require_open()
        query_filter = _scoped_filter(scope, where)
        try:
            collection = await self._ensure_collection()
            await self._transport.delete(collection, query_filter)
        except QdrantProjectionError as error:
            code: QdrantErrorCode = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            return
        await self._record_failure("delete", scope, code)
        raise QdrantProjectionError(code) from None

    async def count(
        self,
        scope: TenantProjectScope,
        *,
        where: ProjectionFilter | None = None,
    ) -> int:
        self._require_open()
        query_filter = _scoped_filter(scope, where)
        try:
            collection = await self._ensure_collection()
            value = await self._transport.count(collection, query_filter)
            if value < 0:
                raise ValueError("negative vector count")
        except ValueError:
            code: QdrantErrorCode = "collection_schema_conflict"
        except QdrantProjectionError as error:
            code = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            return value
        await self._record_failure("count", scope, code)
        raise QdrantProjectionError(code) from None

    async def aclose(self) -> None:
        if self._closed:
            return
        if not self._owns_transport:
            self._closed = True
            return
        try:
            await self._transport.aclose()
        except QdrantProjectionError as error:
            code: QdrantErrorCode = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            self._closed = True
            return
        raise QdrantProjectionError(code) from None

    async def __aenter__(self) -> QdrantProjection:
        self._require_open()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback_value: Any) -> None:
        del exc_type, traceback_value
        if exc is None:
            await self.aclose()
            return
        try:
            await self.aclose()
        except QdrantProjectionError:
            return

    async def _record_failure(
        self,
        operation: ProjectionOperation,
        scope: TenantProjectScope | None,
        code: QdrantErrorCode,
    ) -> None:
        identity_hash = self.collection.rsplit("_", maxsplit=1)[-1]
        failure = ProjectionFailure(
            operation=operation,
            tenant_id=None if scope is None else scope.tenant_id,
            project_id=None if scope is None else scope.project_id,
            collection=self.collection,
            embedding_identity_sha256=identity_hash,
            error_code=code,
        )
        try:
            await self._lag_recorder.record(failure)
        except Exception:
            # Projection failure remains primary; recorder is never coupled to PG rollback.
            return


def _scoped_filter(
    scope: TenantProjectScope,
    where: ProjectionFilter | None,
    *,
    active_only: bool = False,
) -> ScopedFilter:
    conditions = [
        MatchCondition("tenant_id", str(scope.tenant_id)),
        MatchCondition("project_id", str(scope.project_id)),
    ]
    if where is not None:
        if active_only and where.status not in {None, "active"}:
            raise ValueError("search status must be active")
        for key in ("source_document_id", "parse_run_id", "memory_kind", "status"):
            value = getattr(where, key)
            if value is not None and not (active_only and key == "status"):
                conditions.append(MatchCondition(key, str(value)))
    if active_only:
        conditions.append(MatchCondition("status", "active"))
    return ScopedFilter(must=tuple(conditions))


def _search_hit(
    hit: ScoredPoint, scope: TenantProjectScope, identity: EmbeddingIdentity
) -> VectorSearchHit:
    payload = ChunkVectorPayload.model_validate(hit.payload, strict=False)
    if payload.tenant_id != scope.tenant_id or payload.project_id != scope.project_id:
        raise ValueError("Qdrant returned a point outside the requested scope")
    if not payload.matches_embedding(identity) or payload.status != "active":
        raise ValueError("Qdrant returned an incompatible projection point")
    try:
        point_id = hit.id if isinstance(hit.id, uuid.UUID) else uuid.UUID(hit.id)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("Qdrant returned an invalid point id") from error
    expected_point_id = deterministic_point_id(
        payload, identity, schema_version=VECTOR_SCHEMA_VERSION
    )
    if point_id != expected_point_id:
        raise ValueError("Qdrant returned a point whose id does not match its payload")
    citation = SourceCitation(
        source_document_id=payload.source_document_id,
        parse_run_id=payload.parse_run_id,
        product_version=payload.product_version,
        product_sha256=payload.product_sha256,
        section_id=payload.section_id,
        chunk_id=payload.chunk_id,
        citation_label=payload.citation_label,
        citation_heading=payload.citation_heading,
        source_ref_kind=payload.source_ref_kind,
        source_ref_index=payload.source_ref_index,
        source_ref_subindex=payload.source_ref_subindex,
        char_start=payload.char_start,
        char_end=payload.char_end,
        parser_name=payload.parser_name,
        parser_version=payload.parser_version,
        parser_config_sha256=payload.parser_config_sha256,
        chunker_name=payload.chunker_name,
        chunker_version=payload.chunker_version,
        chunker_config_sha256=payload.chunker_config_sha256,
        containment_policy=payload.containment_policy,
    )
    return VectorSearchHit(
        point_id=point_id,
        score=hit.score,
        text=payload.text,
        citation=citation,
    )


def _distance_to_official(distance: EmbeddingDistance, models: Any) -> Any:
    return {
        "cosine": models.Distance.COSINE,
        "dot": models.Distance.DOT,
        "euclid": models.Distance.EUCLID,
        "manhattan": models.Distance.MANHATTAN,
    }[distance]


_NOT_FOUND = object()


async def _await_strict(value: Any, *, allow_not_found: bool = False) -> Any:
    if not inspect.isawaitable(value):
        raise QdrantProjectionError("invalid_configuration")
    try:
        result = await value
    except QdrantProjectionError as error:
        code: QdrantErrorCode = error.code
        not_found = False
    except Exception as error:
        not_found = allow_not_found and getattr(error, "status_code", None) == 404
        code = "qdrant_unavailable"
    else:
        return result
    if not_found:
        return _NOT_FOUND
    raise QdrantProjectionError(code) from None


def _import_qdrant_module(name: str) -> Any:
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        code: QdrantErrorCode = (
            "qdrant_client_missing"
            if error.name == "qdrant_client"
            else "qdrant_installation_broken"
        )
    except ImportError:
        code = "qdrant_installation_broken"
    else:
        return module
    raise QdrantProjectionError(code) from None


class OfficialQdrantTransport:
    """Lazy adapter: importing this module does not require or configure qdrant-client."""

    def __init__(self, client: Any) -> None:
        self._client = client
        required_async_methods = (
            "get_collection",
            "create_collection",
            "get_aliases",
            "update_collection_aliases",
            "upsert",
            "query_points",
            "delete",
            "count",
            "close",
        )
        if any(
            not inspect.iscoroutinefunction(getattr(client, method, None))
            for method in required_async_methods
        ):
            raise QdrantProjectionError("invalid_configuration")
        self._models = _import_qdrant_module("qdrant_client.models")
        self._closed = False

    async def describe_collection(self, name: str) -> CollectionDescription | None:
        info = await _await_strict(
            self._client.get_collection(collection_name=name), allow_not_found=True
        )
        if info is _NOT_FOUND:
            return None
        params = info.config.params.vectors
        if isinstance(params, Mapping):
            raise ValueError("named vectors are not supported by this projection")
        distance = str(getattr(params.distance, "value", params.distance)).lower()
        return CollectionDescription(name, int(params.size), cast(EmbeddingDistance, distance))

    async def create_collection(self, value: CollectionDescription) -> None:
        await _await_strict(
            self._client.create_collection(
                collection_name=value.name,
                vectors_config=self._models.VectorParams(
                    size=value.dimension,
                    distance=_distance_to_official(value.distance, self._models),
                ),
            )
        )

    async def alias_target(self, alias: str) -> str | None:
        response = await _await_strict(self._client.get_aliases())
        targets = [item.collection_name for item in response.aliases if item.alias_name == alias]
        if len(targets) > 1:
            raise ValueError("alias has multiple targets")
        return targets[0] if targets else None

    async def switch_alias(self, alias: str, collection: str) -> None:
        old = await self.alias_target(alias)
        actions: list[Any] = []
        if old is not None:
            actions.append(
                self._models.DeleteAliasOperation(
                    delete_alias=self._models.DeleteAlias(alias_name=alias)
                )
            )
        actions.append(
            self._models.CreateAliasOperation(
                create_alias=self._models.CreateAlias(collection_name=collection, alias_name=alias)
            )
        )
        await _await_strict(
            self._client.update_collection_aliases(change_aliases_operations=actions)
        )

    def _filter(self, value: ScopedFilter) -> Any:
        return self._models.Filter(
            must=[
                self._models.FieldCondition(
                    key=condition.key,
                    match=self._models.MatchValue(value=condition.value),
                )
                for condition in value.must
            ]
        )

    async def upsert(self, collection: str, points: tuple[VectorPoint, ...]) -> None:
        records = [
            self._models.PointStruct(
                id=str(point.id),
                vector=list(point.vector),
                payload=point.payload.model_dump(mode="json"),
            )
            for point in points
        ]
        await _await_strict(
            self._client.upsert(collection_name=collection, points=records, wait=True)
        )

    async def search(
        self,
        collection: str,
        vector: tuple[float, ...],
        query_filter: ScopedFilter,
        limit: int,
    ) -> tuple[ScoredPoint, ...]:
        response = await _await_strict(
            self._client.query_points(
                collection_name=collection,
                query=list(vector),
                query_filter=self._filter(query_filter),
                limit=limit,
                with_payload=True,
            )
        )
        return tuple(
            ScoredPoint(id=item.id, score=float(item.score), payload=item.payload or {})
            for item in response.points
        )

    async def delete(self, collection: str, query_filter: ScopedFilter) -> None:
        selector = self._models.FilterSelector(filter=self._filter(query_filter))
        await _await_strict(
            self._client.delete(collection_name=collection, points_selector=selector, wait=True)
        )

    async def count(self, collection: str, query_filter: ScopedFilter) -> int:
        result = await _await_strict(
            self._client.count(
                collection_name=collection,
                count_filter=self._filter(query_filter),
                exact=True,
            )
        )
        return int(result.count)

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            result = await _await_strict(self._client.close())
        except QdrantProjectionError as error:
            code: QdrantErrorCode = error.code
        except Exception:
            code = "qdrant_unavailable"
        else:
            del result
            self._closed = True
            return
        raise QdrantProjectionError(code) from None


def create_official_qdrant_projection(
    *,
    url: str,
    embedding: EmbeddingIdentity,
    api_key: str | None = None,
    alias: str = COLLECTION_ALIAS,
    lag_recorder: ProjectionLagRecorder,
    timeout: float = 10.0,
) -> QdrantProjection:
    """Composition helper that imports qdrant-client only when explicitly selected."""

    parsed = urlsplit(url)
    if (
        not url.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise QdrantProjectionError("invalid_configuration")
    package = _import_qdrant_module("qdrant_client")
    try:
        client = package.AsyncQdrantClient(url=url, api_key=api_key, timeout=timeout)
    except Exception:
        failed = True
    else:
        failed = False
    if failed:
        raise QdrantProjectionError("invalid_configuration") from None
    return QdrantProjection(
        transport=OfficialQdrantTransport(client),
        embedding=embedding,
        alias=alias,
        lag_recorder=lag_recorder,
        owns_transport=True,
    )


__all__ = [
    "CollectionDescription",
    "MatchCondition",
    "NoopProjectionLagRecorder",
    "OfficialQdrantTransport",
    "ProjectionConfigurationError",
    "ProjectionFailure",
    "ProjectionLagRecorder",
    "QdrantProjection",
    "QdrantProjectionError",
    "QdrantTransport",
    "ScoredPoint",
    "ScopedFilter",
    "create_official_qdrant_projection",
]
