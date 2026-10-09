from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError

from novel_agent.storage.keys import (
    ImmutableObjectKey,
    ObjectKeyFactory,
    QuarantineObjectKey,
    ScopedObjectKey,
    StorageScope,
)
from novel_agent.storage.ports import (
    ConditionalObjectClient,
    ContentHashMismatch,
    CopyIdentity,
    InvalidMediaSignature,
    MissingStorageDependency,
    ObjectAlreadyExists,
    ObjectChanged,
    ObjectMetadata,
    ObjectNotFound,
    ObjectValidationRequest,
    ObjectValidationSpec,
    PromotedObject,
    ReceiptAuthority,
    RetentionAuthority,
    RetentionDeletePermit,
    RetentionViolation,
    ScopeViolation,
    StorageBackendError,
    StorageConflict,
    UploadIntent,
    ValidationReceipt,
    validate_stream,
)

_MISSING_CODES = {"NoSuchKey", "NoSuchObject", "NotFound", "404"}
_DESTINATION_EXISTS_CODES = {"PreconditionFailed", "412"}
_CONFLICT_CODES = {"ConditionalRequestConflict", "409"}


def _error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if code is not None:
                return str(code)
    code = getattr(exc, "code", None)
    return str(code) if code is not None else None


class MinioObjectStore:
    """Boto3-backed S3-compatible boundary using only public conditional APIs.

    Calls are synchronous and must run in a thread or synchronous workflow activity. Whether a
    MinIO server honors destination ``IfNoneMatch`` on ``CopyObject`` is an external release gate.
    """

    def __init__(
        self,
        *,
        client: ConditionalObjectClient,
        bucket: str,
        receipt_signing_key: bytes,
        retention_verification_key: bytes,
        allow_insecure_local: bool = False,
        key_factory: ObjectKeyFactory | None = None,
    ) -> None:
        if not bucket or any(character in bucket for character in "/\\"):
            raise ValueError("bucket must be a non-empty bucket name")
        self._client = client
        self.bucket = bucket
        self._keys = key_factory or ObjectKeyFactory()
        self._receipts = ReceiptAuthority(receipt_signing_key)
        self._retention = RetentionAuthority(retention_verification_key)
        self._allow_insecure_local = allow_insecure_local

    @classmethod
    def from_settings(
        cls,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        receipt_signing_key: bytes,
        retention_verification_key: bytes,
        allow_insecure_local: bool = False,
        region_name: str = "us-east-1",
    ) -> MinioObjectStore:
        parsed = urlsplit(endpoint_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.hostname is None:
            raise ValueError("object-store endpoint must include a hostname")
        if parsed.scheme != "https" and not (
            allow_insecure_local and parsed.scheme == "http" and loopback
        ):
            raise ValueError("object-store endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("object-store endpoint must not contain userinfo")
        try:
            boto3 = import_module("boto3")
        except (ImportError, ModuleNotFoundError) as exc:
            raise MissingStorageDependency(
                "S3-compatible storage requires the optional 'boto3' package"
            ) from exc
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
        )
        return cls(
            client=client,
            bucket=bucket,
            receipt_signing_key=receipt_signing_key,
            retention_verification_key=retention_verification_key,
            allow_insecure_local=allow_insecure_local,
        )

    def create_upload_intent(
        self,
        *,
        scope: StorageScope,
        source_id: UUID,
        upload_id: UUID,
        expires_in: timedelta,
    ) -> UploadIntent:
        if expires_in <= timedelta(0) or expires_in > timedelta(hours=1):
            raise ValueError("upload intent expiry must be at most one hour")
        key = self._keys.staging(scope=scope, source_id=source_id, upload_id=upload_id)
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={"Bucket": self.bucket, "Key": key.value},
                ExpiresIn=int(expires_in.total_seconds()),
            )
        except Exception as exc:
            raise StorageBackendError("failed to create upload intent") from exc
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (self._allow_insecure_local and loopback):
            raise StorageBackendError("storage returned an insecure upload URL")
        try:
            return UploadIntent(
                upload_id=upload_id,
                staging_key=key,
                upload_url=url,
                expires_at=datetime.now(UTC) + expires_in,
                allow_insecure_local=self._allow_insecure_local,
            )
        except ValidationError as exc:
            raise StorageBackendError("storage returned an invalid upload URL") from exc

    def _check_scope(self, scope: StorageScope, key: ScopedObjectKey) -> None:
        if not key.belongs_to(scope):
            raise ScopeViolation("object key does not belong to the trusted storage scope")

    def stat(self, scope: StorageScope, key: ScopedObjectKey) -> ObjectMetadata:
        self._check_scope(scope, key)
        try:
            result = self._client.head_object(Bucket=self.bucket, Key=key.value)
            metadata = result.get("Metadata") or {}
            if not isinstance(metadata, Mapping):
                raise StorageBackendError("object metadata has an invalid shape")
            etag = str(result["ETag"]).strip('"')
            version = result.get("VersionId")
            raw_sha = metadata.get("sha256")
            return ObjectMetadata(
                key=key,
                byte_size=int(result["ContentLength"]),
                etag=etag,
                version_id=str(version) if version else None,
                stored_sha256=str(raw_sha) if raw_sha else None,
                stored_media_type=(
                    str(result["ContentType"]) if result.get("ContentType") else None
                ),
            )
        except ObjectStorageBoundaryExceptions:
            raise
        except Exception as exc:
            if _error_code(exc) in _MISSING_CODES:
                raise ObjectNotFound(key.value) from exc
            if isinstance(exc, (KeyError, TypeError, ValueError, ValidationError)):
                raise StorageBackendError("object metadata is malformed") from exc
            raise StorageBackendError("object stat failed") from exc

    def read_bounded(
        self,
        scope: StorageScope,
        key: ScopedObjectKey,
        *,
        max_bytes: int,
        chunk_size: int = 64 * 1024,
    ) -> Iterator[bytes]:
        self._check_scope(scope, key)
        if max_bytes <= 0 or chunk_size <= 0 or chunk_size > 64 * 1024:
            raise ValueError("bounded read limits are invalid")
        try:
            response = self._client.get_object(
                Bucket=self.bucket,
                Key=key.value,
                Range=f"bytes=0-{max_bytes}",
            )
            body = response["Body"]
        except Exception as exc:
            if _error_code(exc) in _MISSING_CODES:
                raise ObjectNotFound(key.value) from exc
            raise StorageBackendError("bounded object read failed") from exc
        remaining = max_bytes + 1
        try:
            while remaining:
                requested = min(chunk_size, remaining)
                try:
                    chunk = body.read(requested)
                except Exception as exc:
                    raise StorageBackendError("bounded object body read failed") from exc
                if not chunk:
                    break
                if not isinstance(chunk, bytes) or len(chunk) > requested:
                    raise StorageBackendError("object body violated the bounded read contract")
                remaining -= len(chunk)
                yield chunk
                if len(chunk) < requested:
                    break
        finally:
            try:
                body.close()
            except Exception as exc:
                raise StorageBackendError("failed to close object body") from exc
            finally:
                try:
                    release = getattr(body, "release_conn", None)
                    if callable(release):
                        release()
                except Exception as exc:
                    raise StorageBackendError("failed to release object connection") from exc

    def validate(self, scope: StorageScope, request: ObjectValidationRequest) -> ValidationReceipt:
        self._check_scope(scope, request.key)
        metadata = self.stat(scope, request.key)
        if metadata.stored_media_type not in {
            None,
            "application/octet-stream",
            request.declared_media_type,
        }:
            raise InvalidMediaSignature("stored content type conflicts with the declaration")
        validate_stream(
            self.read_bounded(scope, request.key, max_bytes=request.max_bytes),
            metadata,
            request,
        )
        return self._receipts.issue(
            source=request.key,
            tenant_token=scope.tenant_token,
            project_token=scope.project_token,
            etag=metadata.etag,
            version_id=metadata.version_id,
            byte_size=metadata.byte_size,
            sha256=request.expected_sha256,
            media_type=request.declared_media_type,
            file_type=request.file_type,
            max_archive_entries=request.max_archive_entries,
            max_extracted_bytes=request.max_extracted_bytes,
            max_archive_entry_bytes=request.max_archive_entry_bytes,
            max_compression_ratio=request.max_compression_ratio,
            validated_at=datetime.now(UTC),
        )

    def _verify_receipt(self, scope: StorageScope, receipt: ValidationReceipt) -> None:
        self._receipts.verify(receipt)
        self._check_scope(scope, receipt.source)
        if (
            receipt.tenant_token != scope.tenant_token
            or receipt.project_token != scope.project_token
        ):
            raise ScopeViolation("validation receipt has the wrong scope")
        current = self.stat(scope, receipt.source)
        if (
            current.etag != receipt.etag
            or current.version_id != receipt.version_id
            or current.byte_size != receipt.byte_size
        ):
            raise ObjectChanged("source object changed after validation")

    def _copy_args(
        self, receipt: ValidationReceipt, destination: ScopedObjectKey
    ) -> dict[str, Any]:
        source: dict[str, str] = {"Bucket": self.bucket, "Key": receipt.source.value}
        if receipt.version_id:
            source["VersionId"] = receipt.version_id
        return {
            "Bucket": self.bucket,
            "Key": destination.value,
            "CopySource": source,
            "CopySourceIfMatch": receipt.etag,
            "IfNoneMatch": "*",
            "MetadataDirective": "COPY",
        }

    def _request_from_receipt(self, receipt: ValidationReceipt) -> ObjectValidationRequest:
        return ObjectValidationRequest(
            key=receipt.source,
            file_type=receipt.file_type,
            declared_media_type=receipt.media_type,
            expected_byte_size=receipt.byte_size,
            expected_sha256=receipt.sha256,
            max_bytes=receipt.byte_size,
            max_archive_entries=receipt.max_archive_entries,
            max_extracted_bytes=receipt.max_extracted_bytes,
            max_archive_entry_bytes=receipt.max_archive_entry_bytes,
            max_compression_ratio=receipt.max_compression_ratio,
        )

    def _revalidate_destination(
        self,
        scope: StorageScope,
        receipt: ValidationReceipt,
        destination: ScopedObjectKey,
        *,
        copy_identity: CopyIdentity | None,
    ) -> PromotedObject:
        metadata = self.stat(scope, destination)
        if copy_identity is not None and (
            metadata.etag != copy_identity.etag or metadata.version_id != copy_identity.version_id
        ):
            raise StorageConflict("destination changed after conditional copy")
        try:
            validate_stream(
                self.read_bounded(scope, destination, max_bytes=receipt.byte_size),
                metadata,
                self._request_from_receipt(receipt),
            )
        except Exception:
            if copy_identity is not None:
                self._delete_conditionally(
                    scope,
                    destination,
                    etag=copy_identity.etag,
                    version_id=copy_identity.version_id,
                )
            raise
        return PromotedObject(
            key=cast(ImmutableObjectKey | QuarantineObjectKey, destination),
            byte_size=metadata.byte_size,
            sha256=receipt.sha256,
            media_type=receipt.media_type,
            etag=metadata.etag,
            version_id=metadata.version_id,
            created=copy_identity is not None,
        )

    def _copy(
        self, scope: StorageScope, receipt: ValidationReceipt, destination: ScopedObjectKey
    ) -> CopyIdentity:
        try:
            response = self._client.copy_object(**self._copy_args(receipt, destination))
            result = response.get("CopyObjectResult")
            if not isinstance(result, Mapping) or not result.get("ETag"):
                raise StorageBackendError("copy response omitted CopyObjectResult.ETag")
            version = response.get("VersionId")
            return CopyIdentity(
                etag=str(result["ETag"]).strip('"'),
                version_id=str(version) if version else None,
                created=True,
            )
        except StorageBackendError:
            raise
        except Exception as exc:
            if _error_code(exc) in _DESTINATION_EXISTS_CODES:
                raise ObjectAlreadyExists(destination.value) from exc
            if _error_code(exc) in _CONFLICT_CODES:
                raise StorageConflict("conditional copy conflict; retry safely") from exc
            raise StorageBackendError("conditional object copy failed") from exc

    def _cleanup_source_if_unchanged(self, scope: StorageScope, receipt: ValidationReceipt) -> None:
        try:
            current = self.stat(scope, receipt.source)
        except ObjectNotFound:
            return
        if (
            current.etag != receipt.etag
            or current.version_id != receipt.version_id
            or current.byte_size != receipt.byte_size
        ):
            return
        self._delete_conditionally(scope, receipt.source, receipt.etag)

    def _recover_precondition_destination(
        self, scope: StorageScope, receipt: ValidationReceipt, destination: ScopedObjectKey
    ) -> PromotedObject:
        try:
            return self._revalidate_destination(scope, receipt, destination, copy_identity=None)
        except ObjectNotFound:
            self._verify_receipt(scope, receipt)
            raise StorageConflict(
                "copy precondition failed without an existing destination"
            ) from None

    def promote(
        self,
        scope: StorageScope,
        receipt: ValidationReceipt,
        destination: ImmutableObjectKey,
    ) -> PromotedObject:
        self._receipts.verify(receipt)
        self._check_scope(scope, destination)
        self._check_scope(scope, receipt.source)
        if destination.source_token != receipt.source.source_token:
            raise ScopeViolation("promotion source identity differs from destination")
        if destination.parts[-1] != receipt.sha256:
            raise ContentHashMismatch("immutable key hash differs from receipt")
        try:
            existing = self._revalidate_destination(scope, receipt, destination, copy_identity=None)
        except ObjectNotFound:
            pass
        else:
            self._cleanup_source_if_unchanged(scope, receipt)
            return existing
        self._verify_receipt(scope, receipt)
        try:
            copy_identity = self._copy(scope, receipt, destination)
            result = self._revalidate_destination(
                scope, receipt, destination, copy_identity=copy_identity
            )
        except ObjectAlreadyExists:
            result = self._recover_precondition_destination(scope, receipt, destination)
        self._cleanup_source_if_unchanged(scope, receipt)
        return result

    def recover_promoted(
        self,
        scope: StorageScope,
        destination: ImmutableObjectKey,
        spec: ObjectValidationSpec,
    ) -> PromotedObject:
        """Strictly revalidate an already-promoted deterministic object after DB failure."""
        self._check_scope(scope, destination)
        if destination.parts[-1] != spec.expected_sha256:
            raise ContentHashMismatch("immutable key hash differs from expected content")
        metadata = self.stat(scope, destination)
        validate_stream(
            self.read_bounded(scope, destination, max_bytes=spec.max_bytes),
            metadata,
            spec,
        )
        return PromotedObject(
            key=destination,
            byte_size=metadata.byte_size,
            sha256=spec.expected_sha256,
            media_type=spec.declared_media_type,
            etag=metadata.etag,
            version_id=metadata.version_id,
            created=False,
        )

    def quarantine(
        self,
        scope: StorageScope,
        receipt: ValidationReceipt,
        destination: QuarantineObjectKey,
    ) -> PromotedObject:
        self._receipts.verify(receipt)
        self._check_scope(scope, destination)
        self._check_scope(scope, receipt.source)
        if destination.source_token != receipt.source.source_token:
            raise ScopeViolation("quarantine source identity differs from destination")
        try:
            existing = self._revalidate_destination(scope, receipt, destination, copy_identity=None)
        except ObjectNotFound:
            pass
        else:
            self._cleanup_source_if_unchanged(scope, receipt)
            return existing
        self._verify_receipt(scope, receipt)
        try:
            copy_identity = self._copy(scope, receipt, destination)
            result = self._revalidate_destination(
                scope, receipt, destination, copy_identity=copy_identity
            )
        except ObjectAlreadyExists:
            result = self._recover_precondition_destination(scope, receipt, destination)
        self._cleanup_source_if_unchanged(scope, receipt)
        return result

    def _delete_conditionally(
        self,
        scope: StorageScope,
        key: ScopedObjectKey,
        etag: str | None = None,
        version_id: str | None = None,
    ) -> None:
        self._check_scope(scope, key)
        arguments: dict[str, Any] = {"Bucket": self.bucket, "Key": key.value}
        if version_id:
            arguments["VersionId"] = version_id
        elif etag:
            arguments["IfMatch"] = etag
        try:
            self._client.delete_object(**arguments)
        except Exception as exc:
            if _error_code(exc) in _DESTINATION_EXISTS_CODES:
                raise ObjectChanged("object changed before conditional delete") from exc
            if _error_code(exc) in _CONFLICT_CODES:
                raise StorageConflict("conditional delete conflict; retry safely") from exc
            if _error_code(exc) not in _MISSING_CODES:
                raise StorageBackendError("object delete failed") from exc

    def delete(
        self,
        scope: StorageScope,
        key: ScopedObjectKey,
        *,
        permit: RetentionDeletePermit | None = None,
    ) -> None:
        self._check_scope(scope, key)
        if isinstance(key, ImmutableObjectKey):
            if permit is None:
                raise RetentionViolation("immutable evidence requires an audited deletion permit")
            self._retention.verify(permit)
            if (
                permit.tenant_token != scope.tenant_token
                or permit.project_token != scope.project_token
                or permit.immutable_key != key
            ):
                raise RetentionViolation("retention permit does not match immutable evidence")
        self._delete_conditionally(scope, key)


ObjectStorageBoundaryExceptions = (
    ObjectNotFound,
    ScopeViolation,
    StorageBackendError,
)

__all__ = ["MinioObjectStore"]
