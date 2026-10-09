from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from novel_agent.parsing.contracts import ParsedDocument, SectionSourceRef
from novel_agent.sources.contracts import BoundedToken, Sha256Hex

_IDENTITY_NAMESPACE = uuid.UUID("40d8dcb2-e19d-45b7-9b35-9cfa75e48f4e")
_CONTAINMENT_POLICY: Literal["single-section-v1"] = "single-section-v1"
_HEADING_RULES_VERSION = "conservative-zh-en-v1"
_NATURAL_BOUNDARIES = frozenset("。！？!?；;，,、：:\n")
_ZH_HEADING = re.compile(
    r"^第[〇零一二三四五六七八九十百千万两0-9]{1,12}[章节卷回部篇幕集]"
    r"(?:[ \t\u3000]+[^。！？!?；;]{1,80})?$"
)
_EN_HEADING = re.compile(
    r"^(?:chapter|book|part)[ \t]+(?:[0-9]{1,6}|[ivxlcdm]{1,12})"
    r"(?:[ \t]+[^.!?;]{1,80})?$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^#{1,3}[ \t]+\S.{0,79}$")


class NovelChunkerConfig(BaseModel):
    """Every field is canonicalized into the chunker configuration identity."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    max_chars: Annotated[int, Field(ge=32, le=100_000)] = 1_200
    overlap_chars: Annotated[int, Field(ge=0)] = 120
    boundary_search_chars: Annotated[int, Field(gt=0)] = 160
    max_chunks: Annotated[int, Field(gt=0, le=1_000_000)] = 100_000

    @model_validator(mode="after")
    def validate_related_limits(self) -> NovelChunkerConfig:
        if self.overlap_chars >= self.max_chars:
            raise ValueError("overlap_chars must be smaller than max_chars")
        if self.boundary_search_chars > self.max_chars:
            raise ValueError("boundary_search_chars must not exceed max_chars")
        return self


class ChunkerIdentity(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: BoundedToken
    version: BoundedToken
    configuration_sha256: Sha256Hex
    containment_policy: Literal["single-section-v1"] = _CONTAINMENT_POLICY


class NovelSection(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: uuid.UUID
    index: Annotated[int, Field(ge=0)]
    heading: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None
    text: Annotated[str, StringConstraints(min_length=1)]
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(gt=0)]
    source_ref: SectionSourceRef


class NovelChunk(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: uuid.UUID
    index: Annotated[int, Field(ge=0)]
    section_id: uuid.UUID
    section_index: Annotated[int, Field(ge=0)]
    heading: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None
    text: Annotated[str, StringConstraints(min_length=1)]
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(gt=0)]
    source_ref: SectionSourceRef


class ChunkedDocument(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    source_document_id: uuid.UUID
    source_content_sha256: Sha256Hex
    parser_name: BoundedToken
    parser_version: BoundedToken
    parser_config_sha256: Sha256Hex
    identity: ChunkerIdentity
    config: NovelChunkerConfig
    sections: tuple[NovelSection, ...]
    chunks: tuple[NovelChunk, ...]


def _canonical_hash(config: NovelChunkerConfig) -> str:
    material = {
        "boundary_search_chars": config.boundary_search_chars,
        "containment_policy": _CONTAINMENT_POLICY,
        "heading_rules_version": _HEADING_RULES_VERSION,
        "max_chars": config.max_chars,
        "max_chunks": config.max_chunks,
        "natural_boundaries": "".join(sorted(_NATURAL_BOUNDARIES)),
        "overlap_chars": config.overlap_chars,
        "offset_unit": "python-unicode-code-point-half-open-v1",
        "whitespace_policy": "preserve-sections-trim-chunk-edges-v1",
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_id(kind: str, values: list[object]) -> uuid.UUID:
    encoded = json.dumps([kind, *values], ensure_ascii=True, separators=(",", ":"))
    return uuid.uuid5(_IDENTITY_NAMESPACE, encoded)


def _heading(text: str) -> str | None:
    candidate = text.strip()
    if len(candidate) > 100 or "\n" in candidate:
        return None
    if any(
        pattern.fullmatch(candidate) for pattern in (_ZH_HEADING, _EN_HEADING, _MARKDOWN_HEADING)
    ):
        return candidate
    return None


class NovelChunker:
    """Deterministic code-point chunker whose chunks never cross parser sections."""

    name = "builtin-novel"

    def __init__(self, *, version: str, config: NovelChunkerConfig) -> None:
        self.version = version
        self.config = config
        self.identity = ChunkerIdentity(
            name=self.name,
            version=version,
            configuration_sha256=_canonical_hash(config),
        )

    def chunk(
        self,
        document: ParsedDocument,
        *,
        source_document_id: uuid.UUID,
        source_content_sha256: Sha256Hex,
    ) -> ChunkedDocument:
        lineage: list[object] = [
            str(source_document_id),
            source_content_sha256,
            document.parser.name,
            document.parser.version,
            document.parser.configuration_sha256,
            self.identity.name,
            self.identity.version,
            self.identity.configuration_sha256,
            self.identity.containment_policy,
        ]
        sections: list[NovelSection] = []
        chunks: list[NovelChunk] = []
        for parser_section in document.sections:
            if not parser_section.text.strip():
                raise ValueError("parser section must not contain only whitespace")
            section_id = _stable_id(
                "section",
                [
                    *lineage,
                    parser_section.ordinal,
                    parser_section.char_start,
                    parser_section.char_end,
                ],
            )
            heading = _heading(parser_section.text)
            section = NovelSection(
                id=section_id,
                index=parser_section.ordinal,
                heading=heading,
                text=parser_section.text,
                char_start=parser_section.char_start,
                char_end=parser_section.char_end,
                source_ref=parser_section.source_ref,
            )
            sections.append(section)
            for local_start, local_end in self._spans(parser_section.text):
                if len(chunks) >= self.config.max_chunks:
                    raise ValueError("chunk count exceeds configured maximum")
                char_start = parser_section.char_start + local_start
                char_end = parser_section.char_start + local_end
                chunk_id = _stable_id("chunk", [*lineage, char_start, char_end])
                chunks.append(
                    NovelChunk(
                        id=chunk_id,
                        index=len(chunks),
                        section_id=section_id,
                        section_index=parser_section.ordinal,
                        heading=heading,
                        text=document.text[char_start:char_end],
                        char_start=char_start,
                        char_end=char_end,
                        source_ref=parser_section.source_ref,
                    )
                )
        return ChunkedDocument(
            source_document_id=source_document_id,
            source_content_sha256=source_content_sha256,
            parser_name=document.parser.name,
            parser_version=document.parser.version,
            parser_config_sha256=document.parser.configuration_sha256,
            identity=self.identity,
            config=self.config,
            sections=tuple(sections),
            chunks=tuple(chunks),
        )

    def _spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        position = 0
        text_length = len(text)
        while position < text_length:
            while position < text_length and text[position].isspace():
                position += 1
            if position == text_length:
                break
            end = min(position + self.config.max_chars, text_length)
            if end < text_length:
                lower = max(
                    position + self.config.overlap_chars + 1,
                    end - self.config.boundary_search_chars,
                )
                natural = next(
                    (
                        index + 1
                        for index in range(end - 1, lower - 1, -1)
                        if text[index] in _NATURAL_BOUNDARIES
                    ),
                    None,
                )
                if natural is not None:
                    end = natural
            while end > position and text[end - 1].isspace():
                end -= 1
            if end <= position:
                position += 1
                continue
            spans.append((position, end))
            if end == text_length:
                break
            position = max(position + 1, end - self.config.overlap_chars)
        return spans


def validate_chunked_document(product: ChunkedDocument) -> None:
    if product.identity.configuration_sha256 != _canonical_hash(product.config):
        raise ValueError("chunker configuration identity does not match canonical config")
    lineage: list[object] = [
        str(product.source_document_id),
        product.source_content_sha256,
        product.parser_name,
        product.parser_version,
        product.parser_config_sha256,
        product.identity.name,
        product.identity.version,
        product.identity.configuration_sha256,
        product.identity.containment_policy,
    ]
    if [section.index for section in product.sections] != list(range(len(product.sections))):
        raise ValueError("section indexes must be unique, ordered, and contiguous")
    section_by_id: dict[uuid.UUID, NovelSection] = {}
    expected_start = 0
    for section in product.sections:
        if section.id in section_by_id:
            raise ValueError("section ids must be unique")
        if section.char_start != expected_start or section.char_end - section.char_start != len(
            section.text
        ):
            raise ValueError("section spans must reconstruct the canonical document")
        if section.heading != _heading(section.text):
            raise ValueError("section heading is not canonical")
        expected_id = _stable_id(
            "section", [*lineage, section.index, section.char_start, section.char_end]
        )
        if section.id != expected_id:
            raise ValueError("section id does not match canonical lineage")
        section_by_id[section.id] = section
        expected_start = section.char_end + 2

    chunker = NovelChunker(version=product.identity.version, config=product.config)
    expected_chunks: list[tuple[NovelSection, int, int]] = []
    for section in product.sections:
        expected_chunks.extend(
            (section, section.char_start + start, section.char_start + end)
            for start, end in chunker._spans(section.text)
        )
    if len(expected_chunks) != len(product.chunks):
        raise ValueError("chunk count does not match canonical chunking")
    if len(product.chunks) > product.config.max_chunks:
        raise ValueError("chunk count exceeds configured maximum")
    for index, (chunk, expected) in enumerate(zip(product.chunks, expected_chunks, strict=True)):
        section, start, end = expected
        if (
            chunk.index != index
            or chunk.section_id != section.id
            or chunk.section_index != section.index
            or chunk.heading != section.heading
            or chunk.source_ref != section.source_ref
            or chunk.char_start != start
            or chunk.char_end != end
            or chunk.text != section.text[start - section.char_start : end - section.char_start]
        ):
            raise ValueError("chunk does not match its canonical parent section")
        if chunk.id != _stable_id("chunk", [*lineage, start, end]):
            raise ValueError("chunk id does not match canonical lineage")


def canonical_product_sha256(product: ChunkedDocument) -> str:
    validate_chunked_document(product)
    encoded = json.dumps(
        product.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ChunkedDocument",
    "ChunkerIdentity",
    "NovelChunk",
    "NovelChunker",
    "NovelChunkerConfig",
    "NovelSection",
    "canonical_product_sha256",
    "validate_chunked_document",
]
