from __future__ import annotations

import codecs
import hashlib
import json
from typing import BinaryIO

from novel_agent.parsing.contracts import (
    CancellationProbe,
    InvalidTextContent,
    ParsedDocument,
    ParserCancelled,
    ParseRequest,
    ParserIdentity,
    SectionKind,
    SectionSourceRef,
    SourceContentTooLarge,
    build_document,
)
from novel_agent.sources.contracts import SourceFileType


def _config_hash(values: dict[str, object]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


class StdlibTxtParser:
    file_type = SourceFileType.TXT
    media_type = "text/plain"

    def __init__(self, *, read_size: int = 64 * 1024) -> None:
        if read_size <= 0:
            raise ValueError("read_size must be positive")
        self._read_size = read_size
        self.identity = ParserIdentity(
            name="txt-stdlib",
            version="1",
            configuration_sha256=_config_hash(
                {"encoding": "utf-8-sig", "line_endings": "lf", "paragraphs": "blank-line"}
            ),
        )

    def parse(
        self,
        request: ParseRequest,
        stream: BinaryIO,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> ParsedDocument:
        observed = 0
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        pieces: list[str] = []
        decoded_chars = 0
        decoded_bytes = 0
        try:
            while chunk := stream.read(self._read_size):
                if cancellation is not None and cancellation():
                    raise ParserCancelled("TXT parsing was cancelled")
                if not isinstance(chunk, bytes):
                    raise InvalidTextContent("TXT stream returned non-byte content")
                observed += len(chunk)
                if observed > request.budgets.max_input_bytes or observed > request.byte_size:
                    raise SourceContentTooLarge("TXT stream exceeds its declared input budget")
                decoded = decoder.decode(chunk, final=False)
                decoded_chars += len(decoded)
                decoded_bytes += len(decoded.encode("utf-8"))
                if (
                    decoded_chars > request.budgets.max_extracted_chars
                    or decoded_bytes > request.budgets.max_extracted_bytes
                ):
                    from novel_agent.parsing.contracts import ExtractedContentTooLarge

                    raise ExtractedContentTooLarge("TXT exceeds its character budget")
                pieces.append(decoded)
            final = decoder.decode(b"", final=True)
            if (
                decoded_chars + len(final) > request.budgets.max_extracted_chars
                or decoded_bytes + len(final.encode("utf-8")) > request.budgets.max_extracted_bytes
            ):
                from novel_agent.parsing.contracts import ExtractedContentTooLarge

                raise ExtractedContentTooLarge("TXT exceeds extraction budgets")
            pieces.append(final)
        except UnicodeDecodeError as exc:
            raise InvalidTextContent("TXT must use UTF-8 or UTF-8 with BOM") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidTextContent("TXT stream could not be read") from exc
        if observed != request.byte_size:
            raise InvalidTextContent("TXT stream size does not match trusted metadata")
        text = "".join(pieces).replace("\r\n", "\n").replace("\r", "\n")
        if any((ord(char) < 32 and char not in "\t\n") or 127 <= ord(char) <= 159 for char in text):
            raise InvalidTextContent("TXT contains forbidden control characters")

        paragraphs: list[str] = []
        current: list[str] = []
        current_chars = 0
        for line in text.split("\n"):
            if line.strip():
                current_chars += len(line) + (1 if current else 0)
                if current_chars > request.budgets.max_section_chars:
                    from novel_agent.parsing.contracts import ExtractedContentTooLarge

                    raise ExtractedContentTooLarge("TXT section exceeds its character budget")
                current.append(line)
            elif current:
                paragraphs.append("\n".join(current))
                current = []
                current_chars = 0
                if len(paragraphs) > request.budgets.max_paragraphs:
                    from novel_agent.parsing.contracts import ExtractedContentTooLarge

                    raise ExtractedContentTooLarge("TXT exceeds its paragraph budget")
        if current:
            paragraphs.append("\n".join(current))
        if len(paragraphs) > request.budgets.max_paragraphs:
            from novel_agent.parsing.contracts import ExtractedContentTooLarge

            raise ExtractedContentTooLarge("TXT exceeds its paragraph budget")
        values: list[tuple[SectionKind, str, SectionSourceRef]] = [
            ("paragraph", paragraph, SectionSourceRef(kind="paragraph", index=index))
            for index, paragraph in enumerate(paragraphs)
        ]
        return build_document(self.identity, values, request.budgets)


__all__ = ["StdlibTxtParser"]
