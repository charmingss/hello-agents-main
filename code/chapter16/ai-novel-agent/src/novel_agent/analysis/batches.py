from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from novel_agent.analysis.contracts import (
    MAX_CHARS,
    MAX_SECTIONS,
    AnalysisError,
    Section,
)
from novel_agent.analysis.window import safe_boundary

DEFAULT_OVERLAP_CHARS = 2_000


@dataclass(frozen=True)
class BatchSlice:
    section_id: UUID
    section_index: int
    text: str
    char_start: int
    core_start: int
    core_end: int
    context: bool


@dataclass(frozen=True)
class BatchPlan:
    index: int
    slices: tuple[BatchSlice, ...]
    core_start: int
    core_end: int
    char_count: int
    context_char_count: int

    @property
    def core_text(self) -> str:
        return "".join(item.text for item in self.slices if not item.context)


def plan_batches(
    sections: Sequence[Section],
    *,
    char_limit: int = MAX_CHARS,
    section_limit: int = MAX_SECTIONS,
    overlap_limit: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[BatchPlan, ...]:
    if char_limit <= 0 or section_limit <= 0 or overlap_limit < 0:
        raise ValueError("batch limits must be positive")

    core_batches: list[list[BatchSlice]] = []
    current: list[BatchSlice] = []
    current_chars = 0

    for section in sorted(sections, key=lambda item: item.index):
        for item in _core_slices(section, char_limit):
            if current and (
                current_chars + len(item.text) > char_limit
                or len(current) >= section_limit
            ):
                core_batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += len(item.text)
    if current:
        core_batches.append(current)

    plans: list[BatchPlan] = []
    preceding: Sequence[BatchSlice] = ()
    for index, core in enumerate(core_batches):
        context = _context_slices(preceding, overlap_limit)
        slices = (*context, *core)
        context_chars = sum(len(item.text) for item in context)
        core_chars = sum(len(item.text) for item in core)
        plans.append(
            BatchPlan(
                index=index,
                slices=slices,
                core_start=core[0].core_start,
                core_end=core[-1].core_end,
                char_count=context_chars + core_chars,
                context_char_count=context_chars,
            )
        )
        preceding = core
    return tuple(plans)


def _core_slices(section: Section, char_limit: int) -> tuple[BatchSlice, ...]:
    if not section.text:
        return ()

    result: list[BatchSlice] = []
    offset = 0
    while len(section.text) - offset > char_limit:
        piece = section.text[offset : offset + char_limit + 1]
        end = safe_boundary(piece, char_limit)
        if end <= 0:
            raise AnalysisError("analysis_input_too_large", 422)
        result.append(_core_slice(section, offset, end))
        offset += end
    if offset < len(section.text):
        result.append(_core_slice(section, offset, len(section.text) - offset))
    return tuple(result)


def _core_slice(section: Section, offset: int, length: int) -> BatchSlice:
    start = section.char_start + offset
    return BatchSlice(
        section_id=section.id,
        section_index=section.index,
        text=section.text[offset : offset + length],
        char_start=start,
        core_start=start,
        core_end=start + length,
        context=False,
    )


def _context_slices(
    preceding: Sequence[BatchSlice], overlap_limit: int
) -> tuple[BatchSlice, ...]:
    remaining = overlap_limit
    reversed_result: list[BatchSlice] = []
    for item in reversed(preceding):
        if remaining <= 0:
            break
        length = min(len(item.text), remaining)
        text = item.text[-length:]
        start = item.core_end - length
        reversed_result.append(
            BatchSlice(
                section_id=item.section_id,
                section_index=item.section_index,
                text=text,
                char_start=start,
                core_start=item.core_end,
                core_end=item.core_end,
                context=True,
            )
        )
        remaining -= length
    return tuple(reversed(reversed_result))


__all__ = ["BatchPlan", "BatchSlice", "plan_batches"]
