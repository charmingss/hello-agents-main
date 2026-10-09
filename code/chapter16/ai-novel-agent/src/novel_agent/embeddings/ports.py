from __future__ import annotations

from math import isfinite
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from novel_agent.sources.contracts import BoundedToken, Sha256Hex

EmbeddingDistance = Literal["cosine", "dot", "euclid", "manhattan"]
EmbeddingModel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]


class EmbeddingIdentity(BaseModel):
    """Immutable identity for vectors that may safely share one collection."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    provider: BoundedToken
    model: EmbeddingModel
    version: BoundedToken
    dimension: Annotated[int, Field(gt=0, le=1_000_000)]
    distance: EmbeddingDistance
    configuration_sha256: Sha256Hex


class EmbeddingLimits(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    max_batch_size: Annotated[int, Field(gt=0, le=10_000)]
    max_text_chars: Annotated[int, Field(gt=0, le=10_000_000)]
    max_total_chars: Annotated[int, Field(gt=0, le=100_000_000)]


class EmbeddingBatch(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    vectors: tuple[tuple[float, ...], ...]


class EmbeddingProvider(Protocol):
    """No vendor client or response type crosses this application port."""

    identity: EmbeddingIdentity
    limits: EmbeddingLimits

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch: ...


class EmbeddingProviderError(RuntimeError):
    """Stable provider-boundary error whose message contains no vendor details."""


async def embed_bounded(
    provider: EmbeddingProvider, texts: tuple[str, ...]
) -> tuple[tuple[float, ...], ...]:
    if not texts or len(texts) > provider.limits.max_batch_size:
        raise EmbeddingProviderError("embedding batch limit exceeded")
    if any(not text or len(text) > provider.limits.max_text_chars for text in texts):
        raise EmbeddingProviderError("embedding text limit exceeded")
    if sum(map(len, texts)) > provider.limits.max_total_chars:
        raise EmbeddingProviderError("embedding total text limit exceeded")
    try:
        response = await provider.embed(texts)
    except Exception:
        provider_failed = True
    else:
        provider_failed = False
    if provider_failed:
        raise EmbeddingProviderError("embedding provider unavailable") from None
    vectors = response.vectors
    if len(vectors) != len(texts) or any(
        len(vector) != provider.identity.dimension
        or any(not isfinite(component) for component in vector)
        for vector in vectors
    ):
        raise EmbeddingProviderError("invalid embedding response")
    return vectors


__all__ = [
    "EmbeddingBatch",
    "EmbeddingDistance",
    "EmbeddingIdentity",
    "EmbeddingLimits",
    "EmbeddingProvider",
    "EmbeddingProviderError",
    "embed_bounded",
]
