from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, BinaryIO, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from novel_agent.sources.contracts import Sha256Hex, SourceFileType, SourceMediaType

ParserToken = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._+-]+$"),
]
SectionKind = Literal["paragraph", "table_row", "page"]
CancellationProbe = Callable[[], bool]

_MEDIA_BY_TYPE: dict[SourceFileType, str] = {
    SourceFileType.TXT: "text/plain",
    SourceFileType.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    SourceFileType.PDF: "application/pdf",
}


class ParserError(Exception):
    """Stable public parser error; backend exceptions never cross this boundary."""


class ParserSelectionError(ParserError):
    pass


class ParserCancelled(ParserError):
    pass


class MissingParserDependency(ParserError):
    pass


class SourceContentTooLarge(ParserError):
    pass


class ExtractedContentTooLarge(ParserError):
    pass


class InvalidTextContent(ParserError):
    pass


class InvalidDocumentContainer(ParserError):
    pass


class MalformedDocument(ParserError):
    pass


class EncryptedDocument(ParserError):
    pass


class OcrNotSupported(ParserError):
    pass


class ParserBudgets(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    max_input_bytes: Annotated[int, Field(gt=0)] = 100 * 1024**2
    max_pages: Annotated[int, Field(gt=0)] = 2_000
    max_paragraphs: Annotated[int, Field(gt=0)] = 200_000
    max_xml_entries: Annotated[int, Field(gt=0)] = 5_000
    max_xml_entry_bytes: Annotated[int, Field(gt=0)] = 32 * 1024**2
    max_xml_uncompressed_bytes: Annotated[int, Field(gt=0)] = 256 * 1024**2
    max_compression_ratio: Annotated[int, Field(gt=0, le=10_000)] = 100
    max_xml_elements: Annotated[int, Field(gt=0)] = 1_000_000
    max_xml_depth: Annotated[int, Field(gt=0)] = 256
    max_extracted_chars: Annotated[int, Field(gt=0)] = 20_000_000
    max_extracted_bytes: Annotated[int, Field(gt=0)] = 80_000_000
    max_section_chars: Annotated[int, Field(gt=0)] = 2_000_000


class ParserIdentity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: ParserToken
    version: ParserToken
    configuration_sha256: Sha256Hex


class ParseRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    source_id: UUID
    file_type: SourceFileType
    media_type: SourceMediaType
    byte_size: Annotated[int, Field(gt=0)]
    content_sha256: Sha256Hex
    budgets: ParserBudgets

    @model_validator(mode="after")
    def validate_trusted_metadata(self) -> ParseRequest:
        if self.media_type != _MEDIA_BY_TYPE[self.file_type]:
            raise ValueError("trusted media type does not match source file type")
        if self.byte_size > self.budgets.max_input_bytes:
            raise ValueError("source size exceeds parser input budget")
        return self


class SectionSourceRef(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    kind: SectionKind
    index: Annotated[int, Field(ge=0)]
    subindex: Annotated[int, Field(ge=0)] | None = None


class ParserSection(BaseModel):
    """A half-open code-point span into ``ParsedDocument.text``."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    ordinal: Annotated[int, Field(ge=0)]
    kind: SectionKind
    text: Annotated[str, StringConstraints(min_length=1)]
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(gt=0)]
    source_ref: SectionSourceRef

    @model_validator(mode="after")
    def validate_span(self) -> ParserSection:
        if self.kind != self.source_ref.kind:
            raise ValueError("section kind must match its source reference")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("section offset span must equal its text length")
        return self


class ParsedDocument(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    parser: ParserIdentity
    text: Annotated[str, StringConstraints(min_length=1)]
    sections: Annotated[tuple[ParserSection, ...], Field(min_length=1)]
    extracted_chars: Annotated[int, Field(gt=0)]
    extracted_bytes: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_sections(self) -> ParsedDocument:
        if self.extracted_chars != len(self.text):
            raise ValueError("extracted character count does not match text")
        if self.extracted_bytes != len(self.text.encode("utf-8")):
            raise ValueError("extracted byte count does not match text")
        cursor = 0
        for ordinal, section in enumerate(self.sections):
            if section.ordinal != ordinal or section.char_start != cursor:
                raise ValueError("sections must be ordered, contiguous, and zero based")
            if self.text[section.char_start : section.char_end] != section.text:
                raise ValueError("section text does not match document text")
            cursor = section.char_end + 2
        if cursor - 2 != len(self.text):
            raise ValueError("sections must be separated by exactly two LF characters")
        return self


@runtime_checkable
class DocumentParser(Protocol):
    """Synchronous, side-effect-free boundary; Task 8 must offload it from async loops."""

    file_type: SourceFileType
    media_type: SourceMediaType
    identity: ParserIdentity

    def parse(
        self,
        request: ParseRequest,
        stream: BinaryIO,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> ParsedDocument: ...


def build_document(
    identity: ParserIdentity,
    values: list[tuple[SectionKind, str, SectionSourceRef]],
    budgets: ParserBudgets,
) -> ParsedDocument:
    sections: list[ParserSection] = []
    cursor = 0
    for ordinal, (kind, text, source_ref) in enumerate(values):
        if len(text) > budgets.max_section_chars:
            raise ExtractedContentTooLarge("a parsed section exceeds its character budget")
        section = ParserSection(
            ordinal=ordinal,
            kind=kind,
            text=text,
            char_start=cursor,
            char_end=cursor + len(text),
            source_ref=source_ref,
        )
        sections.append(section)
        cursor = section.char_end + 2
    if not sections:
        raise InvalidTextContent("document contains no extractable text")
    text = "\n\n".join(section.text for section in sections)
    extracted_bytes = len(text.encode("utf-8"))
    if len(text) > budgets.max_extracted_chars or extracted_bytes > budgets.max_extracted_bytes:
        raise ExtractedContentTooLarge("parsed text exceeds extraction budgets")
    return ParsedDocument(
        parser=identity,
        text=text,
        sections=tuple(sections),
        extracted_chars=len(text),
        extracted_bytes=extracted_bytes,
    )


__all__ = [
    "DocumentParser",
    "CancellationProbe",
    "EncryptedDocument",
    "ExtractedContentTooLarge",
    "InvalidDocumentContainer",
    "InvalidTextContent",
    "MalformedDocument",
    "MissingParserDependency",
    "OcrNotSupported",
    "ParsedDocument",
    "ParseRequest",
    "ParserBudgets",
    "ParserCancelled",
    "ParserError",
    "ParserIdentity",
    "ParserSection",
    "ParserSelectionError",
    "SectionSourceRef",
    "SectionKind",
    "SourceContentTooLarge",
    "build_document",
]
