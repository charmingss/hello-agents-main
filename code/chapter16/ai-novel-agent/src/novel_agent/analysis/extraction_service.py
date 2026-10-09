"""ExtractionService: load analysis → LLM extract → character match → write graph/bible.

Phase 3 — 独立提取管线，不改动现有分析 prompt。
流程: history.get() → provider.generate() → parse ExtractionResult →
      角色匹配 (NFKC+casefold) → story_graph.create_event/create_relation →
      story_bible.upsert_style_profile
"""

from __future__ import annotations

import unicodedata
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.extraction import ExtractionResult
from novel_agent.analysis.extraction_provider import ExtractionProvider
from novel_agent.analysis.history import SourceAnalysisHistoryService
from novel_agent.analysis.story_bible import StoryBibleService, StyleProfileData
from novel_agent.analysis.story_graph import (
    EventCreate,
    EventParticipant,
    RelationCreate,
    StoryGraphService,
)
from novel_agent.vector.schema import TenantProjectScope


def _normalize_name(value: str) -> str:
    """NFKC + strip + casefold for character name matching."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


class ExtractionSummary(BaseModel):
    """Summary of extraction results."""

    analysis_id: UUID
    events_extracted: int
    relations_extracted: int
    style_updated: bool
    unmatched_characters: list[str] = Field(default_factory=list)
    skipped_events: int = 0
    skipped_relations: int = 0
    deeds_extracted: int = 0


class ExtractionService:
    """Orchestrates the extraction pipeline: analysis → LLM → match → write."""

    def __init__(
        self,
        history: SourceAnalysisHistoryService,
        story_bible: StoryBibleService,
        story_graph: StoryGraphService,
        provider: ExtractionProvider,
    ) -> None:
        self._history = history
        self._story_bible = story_bible
        self._story_graph = story_graph
        self._provider = provider

    async def extract(
        self,
        scope: TenantProjectScope,
        source_id: UUID,
        analysis_id: UUID,
        subject: str,
    ) -> ExtractionSummary:
        # 1. Load analysis detail via history service
        detail = await self._history.get(scope, source_id, analysis_id)

        # 2. Build character name → ID mapping from analysis characters
        original = detail.get("original", {})
        characters = original.get("characters", [])
        character_ids = detail.get("character_ids", [])
        name_to_id: dict[str, UUID] = {}
        for index, char in enumerate(characters):
            name = char.get("name", "")
            if name and index < len(character_ids):
                name_to_id[_normalize_name(name)] = UUID(character_ids[index])
            # Also map aliases
            for alias in char.get("aliases", []):
                if index < len(character_ids):
                    name_to_id[_normalize_name(alias)] = UUID(character_ids[index])

        # 3. Call provider to extract events/relations/style
        try:
            raw = await self._provider.generate(
                analysis_json=original,
                sections=[],  # Sections loaded on demand; for now pass empty
            )
        except AnalysisError:
            raise

        # 4. Parse and validate output
        try:
            result = ExtractionResult.model_validate_json(raw)
        except Exception:
            raise AnalysisError("extraction_invalid_output", 502) from None

        # 5. Process events
        unmatched: set[str] = set()
        events_extracted = 0
        skipped_events = 0

        for event in result.events:
            # Match all participants
            matched_participants: list[EventParticipant] = []
            all_matched = True
            for participant in event.participants:
                normalized = _normalize_name(participant.character_name)
                char_id = name_to_id.get(normalized)
                if char_id is not None:
                    matched_participants.append(
                        EventParticipant(
                            character_id=char_id,
                            role=participant.role or "参与者",
                            order_index=0,
                        )
                    )
                else:
                    unmatched.add(participant.character_name)
                    all_matched = False

            if all_matched and matched_participants:
                try:
                    occurred_at = None
                    if event.occurred_at:
                        from datetime import datetime

                        occurred_at = datetime.fromisoformat(event.occurred_at)

                    event_kwargs: dict[str, Any] = {
                        "title": event.title,
                        "description": event.description,
                        "participants": matched_participants,
                    }
                    if occurred_at is not None:
                        event_kwargs["occurred_at"] = occurred_at
                    if event.location is not None:
                        event_kwargs["location"] = event.location
                    if event.event_type is not None:
                        event_kwargs["event_type"] = event.event_type
                    if event.outcome is not None:
                        event_kwargs["outcome"] = event.outcome

                    event_data = EventCreate(**event_kwargs)
                    await self._story_graph.create_event(scope, event_data, subject)
                    events_extracted += 1
                except AnalysisError:
                    skipped_events += 1
            else:
                skipped_events += 1

        # 5b. Map character deeds to events (fallback for LLM-missed deeds)
        deeds_extracted = 0
        for char_dict in original.get("characters", []):
            char_name = char_dict.get("name", "")
            normalized = _normalize_name(char_name)
            char_id = name_to_id.get(normalized)
            if char_id is None:
                continue
            for deed in char_dict.get("deeds", []):
                deed_text = deed.get("text", "")
                if not deed_text.strip():
                    continue
                try:
                    title = deed_text[:200] if len(deed_text) > 200 else deed_text
                    event_data = EventCreate(
                        title=title,
                        description=deed_text[:2000],
                        participants=[
                            EventParticipant(
                                character_id=char_id,
                                role="主角",
                                order_index=0,
                            )
                        ],
                        event_type="achievement",
                    )
                    await self._story_graph.create_event(scope, event_data, subject)
                    deeds_extracted += 1
                except AnalysisError:
                    skipped_events += 1

        # 6. Process relations
        relations_extracted = 0
        skipped_relations = 0

        for relation in result.relations:
            from_id = name_to_id.get(_normalize_name(relation.from_character_name))
            to_id = name_to_id.get(_normalize_name(relation.to_character_name))

            if from_id is not None and to_id is not None:
                try:
                    relation_data = RelationCreate(
                        relation_type=relation.relation_type,
                        from_character_id=from_id,
                        to_character_id=to_id,
                        description=relation.description,
                    )
                    await self._story_graph.create_relation(scope, relation_data, subject)
                    relations_extracted += 1
                except AnalysisError:
                    skipped_relations += 1
            else:
                skipped_relations += 1
                if from_id is None:
                    unmatched.add(relation.from_character_name)
                if to_id is None:
                    unmatched.add(relation.to_character_name)

        # 7. Process style
        style_updated = False
        if result.style is not None:
            # Check if style has at least one non-None field
            style_dict = result.style.model_dump()
            if any(v is not None for v in style_dict.values()):
                style_data = StyleProfileData(**style_dict)
                await self._story_bible.upsert_style_profile(scope, style_data, subject)
                style_updated = True

        return ExtractionSummary(
            analysis_id=analysis_id,
            events_extracted=events_extracted,
            relations_extracted=relations_extracted,
            style_updated=style_updated,
            unmatched_characters=sorted(unmatched),
            skipped_events=skipped_events,
            skipped_relations=skipped_relations,
            deeds_extracted=deeds_extracted,
        )

    async def aclose(self) -> None:
        await self._provider.aclose()
