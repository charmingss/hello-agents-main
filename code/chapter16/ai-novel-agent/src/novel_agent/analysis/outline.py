"""OutlineResult schema: LLM-generated novel outline (title, premise, chapters).

Phase 4 — 大纲生成输出 Schema，与 ExtractionResult 模式一致。
"""

from typing import Annotated

from pydantic import StringConstraints, field_validator

from novel_agent.analysis.contracts import StrictModel

ShortTitle = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"\S")]
LongText = Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r"\S")]
EventText = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class OutlineChapterResult(StrictModel):
    """A single chapter in the generated outline."""

    order_index: int
    title: ShortTitle
    summary: LongText
    key_events: list[EventText] = []  # noqa: B006
    notes: str | None = None

    @field_validator("order_index")
    @classmethod
    def validate_order_index(cls, v: int) -> int:
        if v < 1 or v > 500:
            raise ValueError("order_index must be between 1 and 500")
        return v

    @field_validator("key_events")
    @classmethod
    def validate_key_events(cls, v: list[str]) -> list[str]:
        if len(v) > 20:
            raise ValueError("key_events must have at most 20 entries")
        return v

    @field_validator("notes")
    @classmethod
    def validate_notes_length(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 2000:
            raise ValueError("notes must be at most 2000 characters")
        return v


class OutlineResult(StrictModel):
    """Complete outline result from LLM: title, premise, and chapters."""

    title: ShortTitle
    premise: LongText
    chapters: list[OutlineChapterResult]

    @field_validator("chapters")
    @classmethod
    def validate_chapters(cls, v: list[OutlineChapterResult]) -> list[OutlineChapterResult]:
        if len(v) < 1:
            raise ValueError("outline must have at least 1 chapter")
        if len(v) > 500:
            raise ValueError("outline must have at most 500 chapters")
        return v
