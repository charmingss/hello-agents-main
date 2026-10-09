"""Extraction output schema: ExtractionResult and related models.

Phase 3 — 事件/关系/风格提取的 Pydantic schema。
所有模型继承 StrictModel（extra="forbid", frozen=True），
与 contracts.py 中的 Candidate/Claim/Character 保持一致风格。
"""

from typing import Annotated

from pydantic import Field, StringConstraints

from novel_agent.analysis.contracts import StrictModel


class ExtractedParticipant(StrictModel):
    """事件参与者：角色名 + 可选角色身份。"""

    character_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    role: Annotated[str, StringConstraints(max_length=50)] | None = None


class ExtractedEvent(StrictModel):
    """从文本中提取的叙事事件。"""

    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    occurred_at: Annotated[str, StringConstraints(max_length=50)] | None = None
    location: Annotated[str, StringConstraints(max_length=200)] | None = None
    participants: Annotated[list[ExtractedParticipant], Field(min_length=1, max_length=100)]
    event_type: Annotated[str, StringConstraints(max_length=50)] | None = None
    outcome: Annotated[str, StringConstraints(max_length=500)] | None = None


class ExtractedRelation(StrictModel):
    """两个角色之间的关系。"""

    relation_type: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    from_character_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    to_character_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ExtractedStyle(StrictModel):
    """叙事风格画像。所有字段可选，允许部分提取。"""

    narrative_voice: Annotated[str, StringConstraints(max_length=200)] | None = None
    tense: Annotated[str, StringConstraints(max_length=50)] | None = None
    pacing: Annotated[str, StringConstraints(max_length=200)] | None = None
    tone: Annotated[str, StringConstraints(max_length=200)] | None = None
    vocabulary_level: Annotated[str, StringConstraints(max_length=200)] | None = None
    sentence_structure: Annotated[str, StringConstraints(max_length=200)] | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None


class ExtractionResult(StrictModel):
    """提取管线完整输出：事件 + 关系 + 可选风格。"""

    events: Annotated[list[ExtractedEvent], Field(max_length=50)]
    relations: Annotated[list[ExtractedRelation], Field(max_length=50)]
    style: ExtractedStyle | None = None
