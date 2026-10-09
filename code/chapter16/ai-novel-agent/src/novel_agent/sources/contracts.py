from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal
from unicodedata import category, normalize
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SourceMediaType = Literal[
    "text/plain",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/pdf",
]
BoundedToken = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$"
    ),
]
RequestId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"),
]
SubjectId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=255)]
EvidenceReference = Annotated[str, StringConstraints(min_length=1, max_length=500)]
RightsHolder = Annotated[str, StringConstraints(min_length=1, max_length=255)]

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class SourceFileType(StrEnum):
    TXT = "txt"
    DOCX = "docx"
    PDF = "pdf"


_MEDIA_TYPE_BY_FILE_TYPE: dict[SourceFileType, str] = {
    SourceFileType.TXT: "text/plain",
    SourceFileType.DOCX: _DOCX_MEDIA_TYPE,
    SourceFileType.PDF: "application/pdf",
}

# Display-name defense only: reject well-known active, executable, archive, and legacy
# document suffixes when they are hidden before the final supported extension. Task 4's
# magic/container validation and server-generated object key remain the authoritative boundary.
_DANGEROUS_INTERMEDIATE_SUFFIXES = frozenset(
    {
        "7z",
        "apk",
        "bash",
        "bat",
        "bz2",
        "chm",
        "class",
        "cmd",
        "com",
        "cpl",
        "deb",
        "dll",
        "doc",
        "docm",
        "docx",
        "exe",
        "fish",
        "gz",
        "hta",
        "htm",
        "html",
        "img",
        "inf",
        "iso",
        "jar",
        "js",
        "jse",
        "lnk",
        "msc",
        "msi",
        "msp",
        "pdf",
        "php",
        "pif",
        "pl",
        "ppt",
        "pptm",
        "pptx",
        "ps1",
        "psm1",
        "py",
        "pyw",
        "rar",
        "rb",
        "reg",
        "rpm",
        "scf",
        "scr",
        "sh",
        "shtml",
        "svg",
        "svgz",
        "sys",
        "tar",
        "txt",
        "url",
        "vbe",
        "vbs",
        "war",
        "wsh",
        "wsf",
        "xhtml",
        "xls",
        "xlsm",
        "xlsx",
        "xz",
        "zip",
        "zsh",
    }
)

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
    *(f"COM{number}" for number in "¹²³"),
    *(f"LPT{number}" for number in "¹²³"),
}


class AuthorizationBasis(StrEnum):
    """Legal basis asserted by the uploading user; it is retained for audit."""

    USER_OWNED = "user_owned"
    PUBLIC_DOMAIN = "public_domain"
    LICENSED = "licensed"
    EXPLICIT_PERMISSION = "explicit_permission"


class PermittedUse(StrEnum):
    """Supported uses deliberately exclude copying and named-author style imitation."""

    ANALYSIS = "analysis"
    RETRIEVAL = "retrieval"
    ABSTRACT_STYLE = "abstract_style"


class SourceStatus(StrEnum):
    UPLOAD_PENDING = "upload_pending"
    UPLOADED = "uploaded"
    ACCEPTED = "accepted"
    PARSING = "parsing"
    PARSED = "parsed"
    CHUNKS_READY = "chunks_ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class AuthorizationDeclaration(BaseModel):
    """The user's bounded rights declaration; it cannot assert server identity or time."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    basis: AuthorizationBasis
    permitted_uses: Annotated[frozenset[PermittedUse], Field(min_length=1, max_length=3)]
    rights_holder: RightsHolder | None = None
    license_identifier: EvidenceReference | None = None
    evidence_reference: EvidenceReference | None = None

    @field_validator("rights_holder", "license_identifier", "evidence_reference", mode="before")
    @classmethod
    def normalize_authorization_text(cls, value: object) -> object:
        if isinstance(value, str):
            return normalize("NFC", value)
        return value

    @field_validator("rights_holder", "license_identifier", "evidence_reference")
    @classmethod
    def reject_unsafe_authorization_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.strip():
            raise ValueError("authorization text must not have surrounding whitespace")
        if any(category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value):
            raise ValueError("authorization text contains unsafe Unicode characters")
        return value

    @model_validator(mode="after")
    def require_evidence_for_claimed_basis(self) -> "AuthorizationDeclaration":
        if self.basis is AuthorizationBasis.LICENSED:
            if self.rights_holder is None or self.license_identifier is None:
                raise ValueError("licensed sources require rights_holder and license_identifier")
            if self.evidence_reference is not None:
                raise ValueError("licensed sources must not include evidence_reference")
        elif self.basis is AuthorizationBasis.EXPLICIT_PERMISSION:
            if self.rights_holder is None or self.evidence_reference is None:
                raise ValueError(
                    "explicit permission requires rights_holder and evidence_reference"
                )
            if self.license_identifier is not None:
                raise ValueError("explicit permission must not include license_identifier")
        elif self.basis is AuthorizationBasis.PUBLIC_DOMAIN:
            if self.evidence_reference is None:
                raise ValueError("public-domain sources require evidence_reference")
            if self.rights_holder is not None or self.license_identifier is not None:
                raise ValueError(
                    "public-domain sources must not include rights_holder or license_identifier"
                )
        elif any(
            value is not None
            for value in (self.rights_holder, self.license_identifier, self.evidence_reference)
        ):
            raise ValueError(
                "user-owned sources must not include rights_holder, license_identifier, "
                "or evidence_reference"
            )
        return self


class AuthorizationProvenance(BaseModel):
    """Auditable provenance completed only with trusted authentication context."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    declaration: AuthorizationDeclaration
    attested_by_subject: SubjectId
    attested_at: datetime

    @field_validator("attested_at")
    @classmethod
    def require_timezone_aware_attestation(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("attested_at must include a timezone")
        return value


class SourceScope(BaseModel):
    """Trusted server-side scope. It must never be populated from an upload body."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: UUID
    project_id: UUID


class ParserProfile(BaseModel):
    """Every field participates in the immutable parse identity."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    parser_name: BoundedToken
    parser_version: BoundedToken
    chunker_version: BoundedToken
    configuration_sha256: Sha256Hex
    chunker_name: BoundedToken = "builtin-novel"
    chunker_configuration_sha256: Sha256Hex = "0" * 64
    containment_policy: Literal["single-section-v1"] = "single-section-v1"


class InitiateSourceUpload(BaseModel):
    """Untrusted upload declaration. Storage locations are intentionally absent."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: RequestId
    filename: DisplayName
    media_type: SourceMediaType
    byte_size: Annotated[int, Field(gt=0, le=10 * 1024**3)]
    content_sha256: Sha256Hex
    authorization: AuthorizationDeclaration

    @field_validator("filename", mode="before")
    @classmethod
    def normalize_display_filename(cls, filename: object) -> object:
        if isinstance(filename, str):
            return normalize("NFC", filename)
        return filename

    @field_validator("filename")
    @classmethod
    def validate_display_filename(cls, filename: str) -> str:
        if filename != filename.strip():
            raise ValueError("filename must not have surrounding whitespace")
        if any(category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in filename):
            raise ValueError("filename contains control or formatting characters")
        if (
            "/" in filename
            or "\\" in filename
            or any(character in filename for character in '<>:"|?*')
            or filename in {".", ".."}
        ):
            raise ValueError("filename must not contain path components")

        suffixes = PurePosixPath(filename).suffixes
        if not suffixes:
            raise ValueError("filename must have a supported extension")
        suffix = suffixes[-1].lower()
        if suffix not in {".txt", ".docx", ".pdf"}:
            raise ValueError("unsupported source extension")
        intermediate_segments = filename.split(".")[1:-1]
        for segment in intermediate_segments:
            normalized_segment = segment.rstrip(" .").casefold()
            if not normalized_segment or normalized_segment in _DANGEROUS_INTERMEDIATE_SUFFIXES:
                raise ValueError("filename contains a dangerous compound extension")

        windows_stem = filename.split(".", maxsplit=1)[0].rstrip(" .").upper()
        if windows_stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError("filename uses a reserved device name")
        return filename

    @model_validator(mode="after")
    def require_matching_media_type(self) -> "InitiateSourceUpload":
        if self.media_type != _MEDIA_TYPE_BY_FILE_TYPE[self.file_type]:
            raise ValueError("media type does not match filename extension")
        return self

    @property
    def file_type(self) -> SourceFileType:
        return SourceFileType(PurePosixPath(self.filename).suffix.lower().removeprefix("."))


class CompleteSourceUpload(BaseModel):
    """Client confirmation; the server verifies these values against object metadata."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    upload_id: UUID
    byte_size: Annotated[int, Field(gt=0, le=10 * 1024**3)]
    content_sha256: Sha256Hex


__all__ = [
    "AuthorizationBasis",
    "AuthorizationDeclaration",
    "AuthorizationProvenance",
    "CompleteSourceUpload",
    "InitiateSourceUpload",
    "ParserProfile",
    "PermittedUse",
    "Sha256Hex",
    "SourceFileType",
    "SourceMediaType",
    "SourceScope",
    "SourceStatus",
]
