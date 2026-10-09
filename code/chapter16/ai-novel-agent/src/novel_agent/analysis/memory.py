"""MemoryResult schema: LLM-extracted writing memory items from chapter content.

Phase 6 — 写作记忆提取输出 Schema。
"""

from typing import Annotated, Literal

from pydantic import StringConstraints

from novel_agent.analysis.contracts import StrictModel

MemoryCategory = Literal["character", "world", "plot", "foreshadowing", "relation"]
MemoryContent = Annotated[str, StringConstraints(min_length=1, max_length=2000)]

MAX_MEMORY_ITEMS = 100


class MemoryItem(StrictModel):
    """A single extracted fact from a chapter."""

    category: MemoryCategory
    content: MemoryContent


class MemoryResult(StrictModel):
    """Extraction result: a list of memory items."""

    items: list[MemoryItem]

    def model_post_init(self, __context: object) -> None:
        if len(self.items) > MAX_MEMORY_ITEMS:
            raise ValueError(f"items must have at most {MAX_MEMORY_ITEMS} entries")
