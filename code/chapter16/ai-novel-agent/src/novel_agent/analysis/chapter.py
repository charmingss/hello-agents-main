"""ChapterResult schema: LLM-generated single chapter (title, content, summary).

Phase 5 — 单章生成输出 Schema，与 OutlineResult 模式一致。
"""

from typing import Annotated

from pydantic import StringConstraints

from novel_agent.analysis.contracts import StrictModel

ShortTitle = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"\S")]
ChapterContent = Annotated[str, StringConstraints(min_length=50, max_length=200_000)]
ChapterSummary = Annotated[str, StringConstraints(min_length=1, max_length=2000, pattern=r"\S")]


class ChapterResult(StrictModel):
    """A single generated chapter: title, full content, and one-line summary."""

    title: ShortTitle
    content: ChapterContent
    summary: ChapterSummary
