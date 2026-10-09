from __future__ import annotations

from collections.abc import Iterable

from novel_agent.parsing.contracts import (
    DocumentParser,
    ParseRequest,
    ParserIdentity,
    ParserSelectionError,
)
from novel_agent.sources.contracts import SourceFileType

_MEDIA_BY_TYPE: dict[SourceFileType, str] = {
    SourceFileType.TXT: "text/plain",
    SourceFileType.DOCX: (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    SourceFileType.PDF: "application/pdf",
}


class ParserRouter:
    def __init__(self, parsers: Iterable[DocumentParser]) -> None:
        by_type: dict[SourceFileType, DocumentParser] = {}
        for parser in parsers:
            if parser.file_type in by_type:
                raise ValueError(f"duplicate parser registration for {parser.file_type.value}")
            if parser.media_type != _MEDIA_BY_TYPE[parser.file_type]:
                raise ValueError("parser media type does not match its registered file type")
            by_type[parser.file_type] = parser
        self._by_type = by_type

    def select(self, request: ParseRequest) -> DocumentParser:
        expected_media = _MEDIA_BY_TYPE[request.file_type]
        if request.media_type != expected_media:
            raise ParserSelectionError("trusted source type and media type mismatch")
        parser = self._by_type.get(request.file_type)
        if parser is None:
            raise ParserSelectionError(f"unsupported parser type: {request.file_type.value}")
        return parser

    def identity_for(self, request: ParseRequest) -> ParserIdentity:
        return self.select(request).identity


__all__ = ["ParserRouter", "ParserSelectionError"]
