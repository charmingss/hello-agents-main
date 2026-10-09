from dataclasses import replace

from novel_agent.analysis.contracts import MAX_CHARS, MAX_SECTIONS, AnalysisError, Section, Window


def _scan(
    text: str, budget: int, section_ends: frozenset[int] = frozenset()
) -> tuple[int, bool]:
    """Conservative punctuation boundary, never detach a following dialogue attribution."""
    stack: list[str] = []
    pairs = {
        "“": "”",
        "‘": "’",
        '"': '"',
        "'": "'",
        "「": "」",
        "『": "』",
        "(": ")",
        "（": "）",
        "[": "]",
        "【": "】",
        "{": "}",
    }
    closing = frozenset(pairs.values())
    boundary = 0
    for i, char in enumerate(text[:budget]):
        if char in {"'", "’"} and i and i + 1 < len(text):
            if text[i - 1].isascii() and text[i - 1].isalpha() and text[i + 1].isalpha():
                continue
        if stack and char == stack[-1]:
            stack.pop()
            # A closed dialogue followed by newline is safe; punctuation inside it alone isn't.
            if not stack and i + 1 < len(text) and text[i + 1] in "\r\n":
                boundary = i + 1
        elif char in pairs:
            stack.append(pairs[char])
        elif char in closing:
            # Unknown or mismatched opening delimiter: this material is not safe to split.
            return boundary, False
        elif not stack and char in "。！？!?":
            boundary = i + 1
        elif not stack and char == "." and i + 1 < len(text) and text[i + 1].isspace():
            boundary = i + 1
        if not stack and i + 1 in section_ends:
            boundary = i + 1
    return boundary, not stack


def safe_boundary(text: str, budget: int) -> int:
    return _scan(text, budget)[0]


def build_window(
    rows: list[Section], *, char_limit: int = MAX_CHARS, section_limit: int = MAX_SECTIONS
) -> Window:
    selected: list[Section] = []
    used = 0
    partial = len(rows) > section_limit
    for row in rows[:section_limit]:
        remaining = char_limit - used
        if row.original_length <= remaining and len(row.text) == row.original_length:
            selected.append(row)
            used += len(row.text)
            continue
        partial = True
        # Scan all preceding sections too: quotations can span paragraph/page sections.
        prefix = "\n".join(s.text for s in selected)
        combined = prefix + ("\n" if selected else "") + row.text
        end, _ = _scan(
            combined, len(prefix) + bool(selected) + remaining, _complete_section_ends(selected)
        )
        selected = _prefix_sections([*selected, row], end)
        used = sum(len(s.text) for s in selected)
        break
    combined = "\n".join(s.text for s in selected)
    boundary, balanced = _scan(combined, len(combined), _complete_section_ends(selected))
    if not balanced:
        selected = _prefix_sections(selected, boundary)
        used = sum(len(s.text) for s in selected)
        partial = True
    if not selected or not any(row.text.strip() for row in selected):
        raise AnalysisError("analysis_input_too_large", 422)
    return Window(tuple(selected), used, partial)


def _complete_section_ends(rows: list[Section]) -> frozenset[int]:
    ends = set()
    offset = 0
    for row in rows:
        offset += len(row.text)
        if len(row.text) == row.original_length:
            ends.add(offset)
        offset += 1
    return frozenset(ends)


def _prefix_sections(rows: list[Section], end: int) -> list[Section]:
    result = []
    for row in rows:
        length = min(len(row.text), end)
        if length <= 0:
            break
        result.append(replace(row, text=row.text[:length]))
        end -= length + 1  # synthetic section boundary is not included in evidence/coverage
    return result
