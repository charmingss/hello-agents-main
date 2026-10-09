from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Iterator
from typing import BinaryIO, cast
from xml.etree import ElementTree

from novel_agent.parsing.contracts import (
    CancellationProbe,
    ExtractedContentTooLarge,
    InvalidDocumentContainer,
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

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_MEDIA = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_DOCX_MAIN = f"{_DOCX_MEDIA}.main+xml"


def _hash_config(values: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _safe_name(name: str) -> bool:
    trimmed = name.rstrip("/")
    return bool(trimmed) and not (
        "\\" in name
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or any(part in {"", ".", ".."} for part in trimmed.split("/"))
    )


def _local(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _attribute(element: ElementTree.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key).casefold() == name.casefold():
            return value
    return None


def _safe_xml(payload: bytes, *, max_elements: int, max_depth: int) -> ElementTree.Element:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        raise InvalidDocumentContainer("DOCX XML encoding must be UTF-8")
    if b"\x00" in payload:
        raise InvalidDocumentContainer("DOCX XML contains NUL bytes")
    try:
        text = payload.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidDocumentContainer("DOCX XML encoding must be UTF-8") from exc
    declaration = re.match(r"\s*<\?xml\s+[^>]*encoding\s*=\s*(['\"])([^'\"]+)\1", text, re.I)
    if declaration and declaration.group(2).casefold().replace("_", "-") != "utf-8":
        raise InvalidDocumentContainer("DOCX XML encoding must be UTF-8")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise InvalidDocumentContainer("DOCX XML declaration or entity is forbidden")
    parser = ElementTree.XMLPullParser(events=("start", "end"))
    root: ElementTree.Element | None = None
    element_count = 0
    depth = 0

    def enforce_events() -> None:
        nonlocal root, element_count, depth
        events = cast(Iterator[tuple[str, ElementTree.Element]], parser.read_events())
        for event, element in events:
            if event == "start":
                if root is None:
                    root = element
                element_count += 1
                depth += 1
                if element_count > max_elements:
                    raise InvalidDocumentContainer("DOCX XML has too many elements")
                if depth > max_depth:
                    raise InvalidDocumentContainer("DOCX XML exceeds its depth budget")
            else:
                depth -= 1

    try:
        for offset in range(0, len(text), 4096):
            parser.feed(text[offset : offset + 4096])
            enforce_events()
        parser.close()
        enforce_events()
    except InvalidDocumentContainer:
        raise
    except (ElementTree.ParseError, ValueError) as exc:
        raise InvalidDocumentContainer("DOCX contains malformed XML") from exc
    if root is None:
        raise InvalidDocumentContainer("DOCX contains malformed XML")
    return root


def _paragraph_text(paragraph: ElementTree.Element, max_chars: int, max_bytes: int) -> str:
    if max_chars <= 0 or max_bytes <= 0:
        raise ExtractedContentTooLarge("DOCX paragraph has no remaining extraction budget")
    pieces: list[str] = []
    chars = 0
    byte_count = 0
    for element in paragraph.iter():
        local = _local(element.tag)
        if local == "t" and element.text:
            chars += len(element.text)
            byte_count += len(element.text.encode("utf-8"))
            if chars > max_chars or byte_count > max_bytes:
                raise ExtractedContentTooLarge("DOCX paragraph exceeds extraction budgets")
            pieces.append(element.text)
        elif local == "tab":
            chars += 1
            byte_count += 1
            if chars > max_chars or byte_count > max_bytes:
                raise ExtractedContentTooLarge("DOCX paragraph exceeds extraction budgets")
            pieces.append("\t")
        elif local in {"br", "cr"}:
            chars += 1
            byte_count += 1
            if chars > max_chars or byte_count > max_bytes:
                raise ExtractedContentTooLarge("DOCX paragraph exceeds extraction budgets")
            pieces.append("\n")
    return "".join(pieces).replace("\r\n", "\n").replace("\r", "\n").strip()


def _paragraph_has_text(paragraph: ElementTree.Element) -> bool:
    """Detect output-producing paragraphs without allocating their combined text."""
    return any(
        _local(element.tag) == "t" and bool(element.text and element.text.strip())
        for element in paragraph.iter()
    )


class StdlibDocxParser:
    file_type = SourceFileType.DOCX
    media_type = _DOCX_MEDIA
    identity = ParserIdentity(
        name="docx-stdlib",
        version="2",
        configuration_sha256=_hash_config(
            {
                "body": ["paragraph", "table_row"],
                "cell_separator": "tab",
                "headers_footers": "ignore",
                "nested_tables": "reject",
                "text_boxes": "reject",
                "alt_chunk": "reject",
                "xml_encoding": "utf-8-only",
            }
        ),
    )

    def parse(
        self,
        request: ParseRequest,
        stream: BinaryIO,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> ParsedDocument:
        self._check_input_size(stream, request)
        try:
            with zipfile.ZipFile(stream) as archive:
                roots, names = self._validate_and_read_xml(archive, request, cancellation)
                document = roots[names["word/document.xml"]]
                values = self._extract(document, request, cancellation)
        except (InvalidDocumentContainer, ExtractedContentTooLarge, ParserCancelled):
            raise
        except (OSError, ValueError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
            raise InvalidDocumentContainer("DOCX container is corrupt") from exc
        try:
            return build_document(self.identity, values, request.budgets)
        except InvalidTextContent as exc:
            raise InvalidTextContent("DOCX contains no extractable text") from exc

    @staticmethod
    def _check_input_size(stream: BinaryIO, request: ParseRequest) -> None:
        try:
            current = stream.tell()
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(current)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise InvalidDocumentContainer("DOCX input must be a seekable binary stream") from exc
        if size > request.budgets.max_input_bytes:
            raise SourceContentTooLarge("DOCX exceeds its input budget")
        if size != request.byte_size:
            raise InvalidDocumentContainer("DOCX size does not match trusted metadata")

    @staticmethod
    def _validate_and_read_xml(
        archive: zipfile.ZipFile,
        request: ParseRequest,
        cancellation: CancellationProbe | None,
    ) -> tuple[dict[str, ElementTree.Element], dict[str, str]]:
        infos = archive.infolist()
        if len(infos) > request.budgets.max_xml_entries:
            raise InvalidDocumentContainer("DOCX has too many archive entries")
        folded = [info.filename.casefold() for info in infos]
        if len(folded) != len(set(folded)):
            raise InvalidDocumentContainer("DOCX has duplicate case-insensitive entry names")
        if not all(_safe_name(info.filename) for info in infos):
            raise InvalidDocumentContainer("DOCX has an unsafe archive path")
        if any(info.flag_bits & 1 for info in infos):
            raise InvalidDocumentContainer("encrypted DOCX entries are unsupported")
        active_directories = ("/activex/", "/oleobject/", "/embeddings/", "/customui/")
        active_suffixes = (".exe", ".dll", ".js", ".jse", ".vbs")
        if any(
            name.endswith(("vbaproject.bin", *active_suffixes))
            or any(marker in f"/{name}" for marker in active_directories)
            for name in folded
        ):
            raise InvalidDocumentContainer("DOCX contains active content")

        total = 0
        for info in infos:
            if cancellation is not None and cancellation():
                raise ParserCancelled("DOCX parsing was cancelled")
            if info.file_size > request.budgets.max_xml_entry_bytes:
                raise InvalidDocumentContainer("DOCX entry exceeds its uncompressed budget")
            total += info.file_size
            if info.file_size and (
                info.compress_size == 0
                or info.file_size / info.compress_size > request.budgets.max_compression_ratio
            ):
                raise InvalidDocumentContainer("DOCX entry exceeds its compression ratio")
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise InvalidDocumentContainer("DOCX container uses unsupported compression")
        if total > request.budgets.max_xml_uncompressed_bytes:
            raise InvalidDocumentContainer("DOCX exceeds its total uncompressed budget")

        by_folded = {info.filename.casefold(): info.filename for info in infos}
        required = {"[content_types].xml", "_rels/.rels", "word/document.xml"}
        if not required.issubset(by_folded):
            raise InvalidDocumentContainer("DOCX required OOXML markers are missing")

        def read_xml(name: str) -> ElementTree.Element:
            info = archive.getinfo(name)
            payload = archive.read(info)
            if len(payload) != info.file_size:
                raise InvalidDocumentContainer("DOCX entry size changed during extraction")
            return _safe_xml(
                payload,
                max_elements=request.budgets.max_xml_elements,
                max_depth=request.budgets.max_xml_depth,
            )

        content_types = read_xml(by_folded["[content_types].xml"])
        main_ok = any(
            _local(item.tag).casefold() == "override"
            and _attribute(item, "PartName") == "/word/document.xml"
            and _attribute(item, "ContentType") == _DOCX_MAIN
            for item in content_types.iter()
        )
        if not main_ok:
            raise InvalidDocumentContainer("DOCX main OOXML content type is missing")
        dangerous_content_types = {
            "application/vnd.ms-office.vbaproject",
            "application/vnd.ms-office.activex+xml",
            "application/vnd.ms-office.activex",
            "application/vnd.openxmlformats-officedocument.oleobject",
            "application/vnd.openxmlformats-officedocument.package",
            "application/vnd.ms-office.customui+xml",
        }
        if any(
            (_attribute(item, "ContentType") or "").split(";", 1)[0].strip().casefold()
            in dangerous_content_types
            or "macroenabled" in (_attribute(item, "ContentType") or "").casefold()
            for item in content_types.iter()
        ):
            raise InvalidDocumentContainer("DOCX content types declare active content")
        active_relationships = {
            "oleobject",
            "package",
            "afchunk",
            "attachedtemplate",
            "control",
        }
        for info in infos:
            name = info.filename
            if not name.casefold().endswith(".rels"):
                continue
            root = read_xml(name)
            if any(
                _local(item.tag).casefold() == "relationship"
                and (
                    (_attribute(item, "TargetMode") or "").strip().casefold() == "external"
                    or (_attribute(item, "Type") or "").rstrip("/").rsplit("/", 1)[-1].casefold()
                    in active_relationships
                    or any(
                        marker in (_attribute(item, "Target") or "").casefold()
                        for marker in ("embeddings/", "activex/", "customui/")
                    )
                )
                for item in root.iter()
            ):
                raise InvalidDocumentContainer(
                    "DOCX external or active relationships are forbidden"
                )
            root.clear()
        document = read_xml(by_folded["word/document.xml"])
        return {by_folded["word/document.xml"]: document}, by_folded

    @staticmethod
    def _extract(
        document: ElementTree.Element,
        request: ParseRequest,
        cancellation: CancellationProbe | None,
    ) -> list[tuple[SectionKind, str, SectionSourceRef]]:
        body = document.find(f"{_W}body")
        if body is None:
            raise InvalidDocumentContainer("DOCX document body is missing")
        values: list[tuple[SectionKind, str, SectionSourceRef]] = []
        if (
            document.find(f".//{_W}txbxContent") is not None
            or document.find(f".//{_W}altChunk") is not None
        ):
            raise InvalidDocumentContainer("DOCX contains unsupported complex body content")
        paragraph_index = 0
        table_index = 0
        extracted_chars = 0
        extracted_bytes = 0

        def append_value(kind: SectionKind, text: str, ref: SectionSourceRef) -> None:
            nonlocal extracted_chars, extracted_bytes
            separator = 2 if values else 0
            text_bytes = len(text.encode("utf-8"))
            if (
                len(text) > request.budgets.max_section_chars
                or extracted_chars + separator + len(text) > request.budgets.max_extracted_chars
                or extracted_bytes + separator + text_bytes > request.budgets.max_extracted_bytes
            ):
                raise ExtractedContentTooLarge("DOCX text exceeds extraction budgets")
            values.append((kind, text, ref))
            extracted_chars += separator + len(text)
            extracted_bytes += separator + text_bytes

        for child in body:
            if cancellation is not None and cancellation():
                raise ParserCancelled("DOCX parsing was cancelled")
            if child.tag == f"{_W}p":
                paragraph_index += 1
                if paragraph_index > request.budgets.max_paragraphs:
                    raise ExtractedContentTooLarge("DOCX exceeds its paragraph budget")
                if not _paragraph_has_text(child):
                    continue
                separator = 2 if values else 0
                text = _paragraph_text(
                    child,
                    min(
                        request.budgets.max_section_chars,
                        request.budgets.max_extracted_chars - extracted_chars - separator,
                    ),
                    request.budgets.max_extracted_bytes - extracted_bytes - separator,
                )
                if text:
                    append_value(
                        "paragraph",
                        text,
                        SectionSourceRef(kind="paragraph", index=paragraph_index - 1),
                    )
            elif child.tag == f"{_W}tbl":
                if any(table is not child for table in child.iter(f"{_W}tbl")):
                    raise InvalidDocumentContainer(
                        "DOCX contains unsupported complex nested tables"
                    )
                for row_index, row in enumerate(child.findall(f"{_W}tr")):
                    row_parts: list[str] = []
                    row_chars = 0
                    row_bytes = 0
                    pending_empty_cells = 0
                    document_separator = 2 if values else 0
                    row_char_limit = min(
                        request.budgets.max_section_chars,
                        request.budgets.max_extracted_chars - extracted_chars - document_separator,
                    )
                    row_byte_limit = (
                        request.budgets.max_extracted_bytes - extracted_bytes - document_separator
                    )
                    for cell in row.findall(f"{_W}tc"):
                        cell_paragraphs: list[str] = []
                        cell_chars = 0
                        cell_bytes = 0
                        row_separator = 1 + pending_empty_cells if row_parts else 0
                        for paragraph in cell.iter(f"{_W}p"):
                            paragraph_index += 1
                            if paragraph_index > request.budgets.max_paragraphs:
                                raise ExtractedContentTooLarge("DOCX exceeds its paragraph budget")
                            if not _paragraph_has_text(paragraph):
                                continue
                            paragraph_separator = 1 if cell_paragraphs else 0
                            text = _paragraph_text(
                                paragraph,
                                row_char_limit
                                - row_chars
                                - row_separator
                                - cell_chars
                                - paragraph_separator,
                                row_byte_limit
                                - row_bytes
                                - row_separator
                                - cell_bytes
                                - paragraph_separator,
                            )
                            if text:
                                cell_paragraphs.append(text)
                                cell_chars += paragraph_separator + len(text)
                                cell_bytes += paragraph_separator + len(text.encode("utf-8"))
                        if cell_paragraphs:
                            cell_text = "\n".join(cell_paragraphs)
                            if row_separator:
                                row_parts.append("\t" * row_separator)
                            row_parts.append(cell_text)
                            row_chars += row_separator + cell_chars
                            row_bytes += row_separator + cell_bytes
                            pending_empty_cells = 0
                        elif row_parts:
                            pending_empty_cells += 1
                    row_text = "".join(row_parts)
                    if row_text:
                        append_value(
                            "table_row",
                            row_text,
                            SectionSourceRef(
                                kind="table_row", index=table_index, subindex=row_index
                            ),
                        )
                table_index += 1
        return values


__all__ = ["StdlibDocxParser"]
