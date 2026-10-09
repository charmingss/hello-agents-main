from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any, BinaryIO, Protocol, cast

from novel_agent.parsing.contracts import (
    CancellationProbe,
    EncryptedDocument,
    ExtractedContentTooLarge,
    MalformedDocument,
    MissingParserDependency,
    OcrNotSupported,
    ParsedDocument,
    ParserCancelled,
    ParseRequest,
    ParserError,
    ParserIdentity,
    SectionKind,
    SectionSourceRef,
    SourceContentTooLarge,
    build_document,
)
from novel_agent.sources.contracts import SourceFileType


class PdfDocument(Protocol):
    encrypted: bool
    page_count: int

    def get_page_text(self, index: int) -> str | None: ...
    def close(self) -> None: ...


class PdfBackend(Protocol):
    @property
    def identity_name(self) -> str: ...

    @property
    def identity_version(self) -> str: ...

    def open(self, stream: BinaryIO) -> PdfDocument: ...


class _PypdfDocument:
    def __init__(self, reader: Any) -> None:
        self._reader = reader
        self.encrypted = bool(reader.is_encrypted)
        self.page_count = 0 if self.encrypted else len(reader.pages)

    def get_page_text(self, index: int) -> str | None:
        return cast(str | None, self._reader.pages[index].extract_text())

    def close(self) -> None:
        # PdfReader does not own the caller-provided stream and has no close operation.
        self._reader = None


class LazyPypdfBackend:
    identity_name = "pypdf"

    def __init__(self) -> None:
        self._module: Any | None = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        try:
            self._module = importlib.import_module("pypdf")
        except ImportError as exc:
            raise MissingParserDependency(
                "PDF text parsing requires the optional pypdf dependency"
            ) from exc
        return self._module

    @property
    def identity_version(self) -> str:
        version = getattr(self._load(), "__version__", None)
        if not isinstance(version, str) or not version:
            raise MissingParserDependency("pypdf does not expose a usable runtime version")
        return version

    def open(self, stream: BinaryIO) -> PdfDocument:
        pypdf = self._load()
        return _PypdfDocument(pypdf.PdfReader(stream))


def _identity(backend: PdfBackend) -> ParserIdentity:
    config = json.dumps(
        {
            "backend": backend.identity_name,
            "backend_contract": backend.identity_version,
            "line_endings": "lf",
            "mode": "text-layer-only",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ParserIdentity(
        name=f"pdf-{backend.identity_name}",
        version=backend.identity_version,
        configuration_sha256=hashlib.sha256(config).hexdigest(),
    )


def _normalize_page(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


class PdfTextParser:
    file_type = SourceFileType.PDF
    media_type = "application/pdf"

    def __init__(self, backend: PdfBackend) -> None:
        self._backend = backend

    @property
    def identity(self) -> ParserIdentity:
        """Resolve identity on first use so optional backends bind their runtime version."""
        return _identity(self._backend)

    @classmethod
    def from_default(cls) -> PdfTextParser:
        # Constructing the router must remain possible without optional PDF dependencies.
        return cls(LazyPypdfBackend())

    def parse(
        self,
        request: ParseRequest,
        stream: BinaryIO,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> ParsedDocument:
        document: PdfDocument | None = None
        try:
            try:
                position = stream.tell()
                stream.seek(0, 2)
                input_size = stream.tell()
                stream.seek(position)
            except (OSError, AttributeError) as exc:
                raise MalformedDocument("PDF input must be a seekable binary stream") from exc
            if input_size > request.budgets.max_input_bytes or input_size > request.byte_size:
                raise SourceContentTooLarge("PDF stream exceeds its trusted input budget")
            if input_size != request.byte_size:
                raise MalformedDocument("PDF size does not match trusted metadata")
            document = self._backend.open(stream)
            if document.encrypted:
                raise EncryptedDocument("encrypted PDFs are unsupported")
            if document.page_count > request.budgets.max_pages:
                raise ExtractedContentTooLarge("PDF exceeds its page budget")
            values: list[tuple[SectionKind, str, SectionSourceRef]] = []
            running_chars = 0
            running_bytes = 0
            for page_index in range(document.page_count):
                if cancellation is not None and cancellation():
                    raise ParserCancelled("PDF parsing was cancelled")
                raw_text = document.get_page_text(page_index)
                if not raw_text:
                    continue
                remaining_chars = request.budgets.max_extracted_chars - running_chars
                if (
                    len(raw_text) > request.budgets.max_section_chars
                    or len(raw_text) > remaining_chars
                ):
                    raise ExtractedContentTooLarge("PDF page text exceeds coarse character budgets")
                text = _normalize_page(raw_text)
                if not text:
                    continue
                separator_size = 2 if values else 0
                running_chars += separator_size + len(text)
                running_bytes += separator_size + len(text.encode("utf-8"))
                if (
                    running_chars > request.budgets.max_extracted_chars
                    or running_bytes > request.budgets.max_extracted_bytes
                    or len(text) > request.budgets.max_section_chars
                ):
                    raise ExtractedContentTooLarge("PDF text exceeds extraction budgets")
                values.append(("page", text, SectionSourceRef(kind="page", index=page_index)))
            if not values:
                raise OcrNotSupported("PDF has no text layer; OCR is not supported by this parser")
            return build_document(self.identity, values, request.budgets)
        except ParserError:
            raise
        except Exception as exc:
            raise MalformedDocument("PDF could not be parsed") from exc
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    # Cleanup cannot replace the stable primary parser outcome.
                    pass


__all__ = ["LazyPypdfBackend", "PdfBackend", "PdfDocument", "PdfTextParser"]
