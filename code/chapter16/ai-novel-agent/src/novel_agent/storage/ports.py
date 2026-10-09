from __future__ import annotations

import hashlib
import hmac
import json
import re
import tempfile
import zipfile
from codecs import getincrementaldecoder
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, BinaryIO, Protocol, TypedDict, cast
from urllib.parse import urlsplit
from uuid import UUID
from xml.etree import ElementTree

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from novel_agent.sources.contracts import Sha256Hex, SourceFileType, SourceMediaType
from novel_agent.storage.keys import (
    ImmutableObjectKey,
    QuarantineObjectKey,
    ScopedObjectKey,
    StagingObjectKey,
    StorageScope,
)

DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MEDIA_BY_TYPE: dict[SourceFileType, str] = {
    SourceFileType.TXT: "text/plain",
    SourceFileType.DOCX: DOCX_MEDIA,
    SourceFileType.PDF: "application/pdf",
}
_HEX64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_TOKEN = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]


class ObjectStorageError(Exception):
    """Stable storage boundary failure; raw SDK errors must not cross this boundary."""


class MissingStorageDependency(ObjectStorageError):
    pass


class StorageBackendError(ObjectStorageError):
    pass


class ObjectNotFound(ObjectStorageError):
    pass


class ObjectAlreadyExists(ObjectStorageError):
    pass


class ObjectChanged(ObjectStorageError):
    pass


class StorageConflict(ObjectStorageError):
    """Retryable conditional-operation conflict, distinct from an existing destination."""

    pass


class ContentSizeMismatch(ObjectStorageError):
    pass


class ContentHashMismatch(ObjectStorageError):
    pass


class InvalidMediaSignature(ObjectStorageError):
    pass


class ScopeViolation(ObjectStorageError):
    pass


class RetentionViolation(ObjectStorageError):
    pass


class UploadIntent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    upload_id: UUID
    staging_key: StagingObjectKey
    upload_url: str
    required_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime
    allow_insecure_local: bool = False

    @model_validator(mode="after")
    def validate_intent(self) -> UploadIntent:
        parsed = urlsplit(self.upload_url)
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.hostname is None or parsed.username is not None or parsed.password is not None:
            raise ValueError("upload URL must have a hostname and no userinfo")
        if parsed.scheme != "https" and not (
            self.allow_insecure_local and parsed.scheme == "http" and loopback
        ):
            raise ValueError("upload URL must use HTTPS except explicitly allowed loopback")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        if self.expires_at <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return self


class ObjectMetadata(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    key: ScopedObjectKey
    byte_size: Annotated[int, Field(ge=0)]
    etag: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    version_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None = None
    stored_sha256: Sha256Hex | None = None
    stored_media_type: str | None = None


class ObjectValidationSpec(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    file_type: SourceFileType
    declared_media_type: SourceMediaType
    expected_byte_size: Annotated[int, Field(gt=0)]
    expected_sha256: Sha256Hex
    max_bytes: Annotated[int, Field(gt=0)]
    max_archive_entries: Annotated[int, Field(gt=0)]
    max_extracted_bytes: Annotated[int, Field(gt=0)]
    max_archive_entry_bytes: Annotated[int, Field(gt=0)]
    max_compression_ratio: Annotated[int, Field(gt=0, le=10_000)]

    @model_validator(mode="after")
    def validate_request(self) -> ObjectValidationSpec:
        if self.expected_byte_size > self.max_bytes:
            raise ValueError("expected object size exceeds validation budget")
        if self.declared_media_type != _MEDIA_BY_TYPE[self.file_type]:
            raise ValueError("declared media type does not match source type")
        return self


class ObjectValidationRequest(ObjectValidationSpec):
    key: StagingObjectKey


class ValidationReceipt(BaseModel):
    """HMAC-sealed proof binding validation to one exact stored object version."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source: StagingObjectKey
    tenant_token: _TOKEN
    project_token: _TOKEN
    etag: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    version_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None
    byte_size: Annotated[int, Field(gt=0)]
    sha256: Sha256Hex
    media_type: SourceMediaType
    file_type: SourceFileType
    max_archive_entries: Annotated[int, Field(gt=0)]
    max_extracted_bytes: Annotated[int, Field(gt=0)]
    max_archive_entry_bytes: Annotated[int, Field(gt=0)]
    max_compression_ratio: Annotated[int, Field(gt=0)]
    validated_at: datetime
    seal: _HEX64

    @field_validator("validated_at")
    @classmethod
    def require_aware_validation_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_source_scope_tokens(self) -> ValidationReceipt:
        if (
            self.source.tenant_token != self.tenant_token
            or self.source.project_token != self.project_token
        ):
            raise ValueError("receipt tokens must match the typed source key")
        return self


class PromotedObject(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    key: ImmutableObjectKey | QuarantineObjectKey
    byte_size: Annotated[int, Field(gt=0)]
    sha256: Sha256Hex
    media_type: SourceMediaType
    etag: str
    version_id: str | None
    created: bool


class CopyIdentity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    etag: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    version_id: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None
    created: bool = True


class RetentionDeletePermit(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_token: _TOKEN
    project_token: _TOKEN
    immutable_key: ImmutableObjectKey
    audit_id: UUID
    approved_by: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    issued_at: datetime
    expires_at: datetime
    seal: _HEX64

    @field_validator("issued_at", "expires_at")
    @classmethod
    def require_aware_issue_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retention permit times must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_permit_binding(self) -> RetentionDeletePermit:
        now = datetime.now(UTC)
        if self.expires_at <= self.issued_at:
            raise ValueError("retention permit must expire after it is issued")
        if self.issued_at > now:
            raise ValueError("retention permit cannot be issued in the future")
        if self.expires_at - self.issued_at > timedelta(minutes=15):
            raise ValueError("retention permit lifetime exceeds fifteen minutes")
        if (
            self.immutable_key.tenant_token != self.tenant_token
            or self.immutable_key.project_token != self.project_token
        ):
            raise ValueError("retention permit scope must match its immutable key")
        return self


def _canonical(values: Mapping[str, Any]) -> bytes:
    return json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _receipt_values(receipt: ValidationReceipt, *, exclude_seal: bool = True) -> dict[str, Any]:
    values = receipt.model_dump(mode="json", exclude={"seal"} if exclude_seal else set())
    return values


class ReceiptAuthority:
    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("receipt signing key must contain at least 32 bytes")
        self._key = bytes(key)

    def issue(self, **values: Any) -> ValidationReceipt:
        unsigned = ValidationReceipt(**values, seal="0" * 64)
        seal = hmac.new(
            self._key, _canonical(_receipt_values(unsigned)), hashlib.sha256
        ).hexdigest()
        return unsigned.model_copy(update={"seal": seal})

    def verify(self, receipt: ValidationReceipt) -> None:
        expected = hmac.new(
            self._key, _canonical(_receipt_values(receipt)), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(receipt.seal, expected):
            raise ObjectChanged("validation receipt seal is invalid")


def _permit_values(permit: RetentionDeletePermit) -> dict[str, Any]:
    return permit.model_dump(mode="json", exclude={"seal"})


class RetentionAuthority:
    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("retention signing key must contain at least 32 bytes")
        self._key = bytes(key)

    def issue(
        self,
        *,
        scope: StorageScope,
        immutable_key: ImmutableObjectKey,
        audit_id: UUID,
        approved_by: str,
        reason: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> RetentionDeletePermit:
        unsigned = RetentionDeletePermit(
            tenant_token=scope.tenant_token,
            project_token=scope.project_token,
            immutable_key=immutable_key,
            audit_id=audit_id,
            approved_by=approved_by,
            reason=reason,
            issued_at=issued_at,
            expires_at=expires_at,
            seal="0" * 64,
        )
        seal = hmac.new(self._key, _canonical(_permit_values(unsigned)), hashlib.sha256).hexdigest()
        return unsigned.model_copy(update={"seal": seal})

    def verify(self, permit: RetentionDeletePermit) -> None:
        expected = hmac.new(
            self._key, _canonical(_permit_values(permit)), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(permit.seal, expected):
            raise RetentionViolation("retention permit seal is invalid")
        if permit.expires_at <= datetime.now(UTC):
            raise RetentionViolation("retention permit has expired")


class StreamingBody(Protocol):
    def read(self, amount: int) -> bytes: ...
    def close(self) -> None: ...
    def release_conn(self) -> None: ...


class BodyResponse(TypedDict):
    Body: StreamingBody


class CopyObjectResult(TypedDict, total=False):
    ETag: str


class CopyResponse(TypedDict, total=False):
    CopyObjectResult: CopyObjectResult
    VersionId: str


class ConditionalObjectClient(Protocol):
    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_object(self, **kwargs: Any) -> BodyResponse: ...
    def copy_object(self, **kwargs: Any) -> CopyResponse: ...
    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def generate_presigned_url(self, *args: Any, **kwargs: Any) -> str: ...


class ObjectReader(Protocol):
    def stat(self, scope: StorageScope, key: ScopedObjectKey) -> ObjectMetadata: ...
    def read_bounded(
        self,
        scope: StorageScope,
        key: ScopedObjectKey,
        *,
        max_bytes: int,
        chunk_size: int = 64 * 1024,
    ) -> Iterable[bytes]: ...


class ObjectStore(ObjectReader, Protocol):
    def create_upload_intent(
        self,
        *,
        scope: StorageScope,
        source_id: UUID,
        upload_id: UUID,
        expires_in: timedelta,
    ) -> UploadIntent: ...

    def validate(
        self, scope: StorageScope, request: ObjectValidationRequest
    ) -> ValidationReceipt: ...
    def promote(
        self,
        scope: StorageScope,
        receipt: ValidationReceipt,
        destination: ImmutableObjectKey,
    ) -> PromotedObject: ...
    def recover_promoted(
        self,
        scope: StorageScope,
        destination: ImmutableObjectKey,
        spec: ObjectValidationSpec,
    ) -> PromotedObject: ...

    def quarantine(
        self,
        scope: StorageScope,
        receipt: ValidationReceipt,
        destination: QuarantineObjectKey,
    ) -> PromotedObject: ...

    def delete(
        self,
        scope: StorageScope,
        key: ScopedObjectKey,
        *,
        permit: RetentionDeletePermit | None = None,
    ) -> None: ...


def _validate_txt(stream: BinaryIO) -> None:
    stream.seek(0)
    prefix = stream.read(512)
    if prefix.startswith(b"\xef\xbb\xbf"):
        prefix = prefix[3:]
    lowered = prefix.lstrip().lower()
    signatures = (
        b"%pdf-",
        b"pk\x03\x04",
        b"mz",
        b"\x7felf",
        b"<html",
        b"<!doctype html",
        b"<script",
        b"#!",
    )
    if lowered.startswith(signatures):
        raise InvalidMediaSignature("TXT payload has an active or foreign file signature")
    decoder = getincrementaldecoder("utf-8-sig")("strict")
    stream.seek(0)
    try:
        while raw := stream.read(64 * 1024):
            text = decoder.decode(raw, final=False)
            if "\x00" in text or any(
                (ord(char) < 32 and char not in "\t\n\r\f") or 127 <= ord(char) <= 159
                for char in text
            ):
                raise InvalidMediaSignature("TXT contains binary control characters")
        decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise InvalidMediaSignature("TXT must use UTF-8 or UTF-8 with BOM") from exc


def _safe_archive_name(name: str) -> bool:
    return not (
        not name
        or "\\" in name
        or name.startswith("/")
        or re.match(r"^[a-zA-Z]:", name)
        or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
    )


def _parse_bounded_xml(payload: bytes) -> ElementTree.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise InvalidMediaSignature("OOXML declarations and entities are forbidden")
    return ElementTree.fromstring(payload)


def _attribute_by_local_name(element: ElementTree.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].casefold() == name.casefold():
            return value
    return None


def _validate_docx(stream: BinaryIO, request: ObjectValidationSpec) -> None:
    try:
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            infos = archive.infolist()
            if len(infos) > request.max_archive_entries:
                raise InvalidMediaSignature("DOCX has too many archive entries")
            names = [info.filename for info in infos]
            folded = [name.casefold() for name in names]
            if len(folded) != len(set(folded)) or not all(map(_safe_archive_name, names)):
                raise InvalidMediaSignature("DOCX has duplicate or unsafe archive paths")
            if any(info.flag_bits & 1 for info in infos):
                raise InvalidMediaSignature("encrypted DOCX entries are unsupported")
            active = ("vbaproject.bin", "activex", "oleobject", ".exe", ".dll", ".js")
            if any(any(marker in name.casefold() for marker in active) for name in names):
                raise InvalidMediaSignature("DOCX contains macro or active content")
            total_size = 0
            total_compressed = 0
            for info in infos:
                if info.file_size > request.max_archive_entry_bytes:
                    raise InvalidMediaSignature("DOCX archive entry exceeds its budget")
                total_size += info.file_size
                total_compressed += info.compress_size
                if info.file_size and (
                    not info.compress_size
                    or info.file_size / info.compress_size > request.max_compression_ratio
                ):
                    raise InvalidMediaSignature("DOCX archive entry exceeds compression ratio")
            if total_size > request.max_extracted_bytes or (
                total_size
                and (
                    not total_compressed
                    or total_size / total_compressed > request.max_compression_ratio
                )
            ):
                raise InvalidMediaSignature("DOCX archive exceeds extraction budgets")
            required = {"[content_types].xml", "_rels/.rels", "word/document.xml"}
            if not required.issubset(set(folded)):
                raise InvalidMediaSignature("DOCX container markers are missing")
            content_types = archive.read(names[folded.index("[content_types].xml")])
            root = _parse_bounded_xml(content_types)
            overrides = {
                (
                    _attribute_by_local_name(item, "PartName"),
                    _attribute_by_local_name(item, "ContentType"),
                )
                for item in root.iter()
                if item.tag.rsplit("}", 1)[-1].casefold() == "override"
            }
            if ("/word/document.xml", DOCX_MAIN) not in overrides:
                raise InvalidMediaSignature("DOCX main part is absent or macro-enabled")
            for info in infos:
                if info.filename.casefold().endswith(".rels"):
                    relationships = archive.read(info)
                    relationship_root = _parse_bounded_xml(relationships)
                    for item in relationship_root.iter():
                        if item.tag.rsplit("}", 1)[-1].casefold() != "relationship":
                            continue
                        target_mode = _attribute_by_local_name(item, "TargetMode")
                        if target_mode is not None and target_mode.strip().casefold() == "external":
                            raise InvalidMediaSignature("DOCX external relationships are forbidden")
    except InvalidMediaSignature:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise InvalidMediaSignature("DOCX container is corrupt") from exc


def _validate_pdf(stream: BinaryIO, byte_size: int) -> None:
    stream.seek(0)
    header = stream.read(8)
    stream.seek(max(0, byte_size - 2048))
    tail = stream.read(2048)
    eof_at = tail.rfind(b"%%EOF")
    if (
        not header.startswith(b"%PDF-")
        or eof_at < 0
        or tail[eof_at + len(b"%%EOF") :].strip(b"\x00\t\n\r\f ")
    ):
        raise InvalidMediaSignature("PDF header or EOF boundary is invalid")


def validate_spooled(stream: BinaryIO, request: ObjectValidationSpec) -> None:
    if request.file_type is SourceFileType.TXT:
        _validate_txt(stream)
    elif request.file_type is SourceFileType.DOCX:
        _validate_docx(stream, request)
    else:
        _validate_pdf(stream, request.expected_byte_size)


def validate_stream(
    chunks: Iterable[bytes], metadata: ObjectMetadata, request: ObjectValidationSpec
) -> None:
    if metadata.byte_size != request.expected_byte_size or metadata.byte_size > request.max_bytes:
        raise ContentSizeMismatch("stored size differs from the declared bounded size")
    if metadata.stored_sha256 and metadata.stored_sha256 != request.expected_sha256:
        raise ContentHashMismatch("stored SHA metadata differs from the declaration")
    if metadata.stored_media_type not in {
        None,
        "application/octet-stream",
        request.declared_media_type,
    }:
        raise InvalidMediaSignature("stored content type differs from the declaration")
    digest = hashlib.sha256()
    observed = 0
    with tempfile.SpooledTemporaryFile(max_size=min(request.max_bytes, 8 * 1024**2)) as spool:
        for chunk in chunks:
            if not isinstance(chunk, bytes) or not chunk:
                raise StorageBackendError("storage returned an invalid body chunk")
            observed += len(chunk)
            if observed > request.max_bytes:
                raise ContentSizeMismatch("object exceeded the bounded read")
            digest.update(chunk)
            spool.write(chunk)
        if observed != metadata.byte_size:
            raise ContentSizeMismatch("streamed size differs from object metadata")
        if digest.hexdigest() != request.expected_sha256:
            raise ContentHashMismatch("streamed SHA-256 differs from the declaration")
        spool.seek(0)
        validate_spooled(cast(BinaryIO, spool), request)


__all__ = [
    "ConditionalObjectClient",
    "CopyIdentity",
    "CopyResponse",
    "ContentHashMismatch",
    "ContentSizeMismatch",
    "InvalidMediaSignature",
    "MissingStorageDependency",
    "ObjectAlreadyExists",
    "ObjectChanged",
    "ObjectMetadata",
    "ObjectNotFound",
    "ObjectStorageError",
    "ObjectStore",
    "ObjectValidationRequest",
    "ObjectValidationSpec",
    "PromotedObject",
    "ReceiptAuthority",
    "RetentionAuthority",
    "RetentionDeletePermit",
    "RetentionViolation",
    "ScopeViolation",
    "StorageBackendError",
    "StorageConflict",
    "UploadIntent",
    "ValidationReceipt",
    "validate_stream",
]
