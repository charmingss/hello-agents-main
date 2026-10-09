"""Story Bible service: canonical settings with append-only revisions and analysis promotion."""

from __future__ import annotations

import unicodedata
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.contracts import AnalysisError, Name, ShortText, StrictModel
from novel_agent.analysis.history import SourceAnalysisHistoryService
from novel_agent.db.models.project import Project
from novel_agent.db.models.story_bible import (
    Character,
    CharacterRevision,
    Foreshadowing,
    ForeshadowingRevision,
    StoryBible,
    WorldEntry,
)
from novel_agent.db.models.style_profile import (
    StyleProfile,
    StyleProfileRevision,
)
from novel_agent.vector.schema import TenantProjectScope

MAX_ALIASES = 10
MAX_DESC_TRAITS = 10
MAX_BACKGROUND = 2000
MAX_ENTRY_DESC = 2000
MAX_FORESHADOW_DESC = 2000
MAX_FORESHADOW_RESOLUTION = 2000


def _normalize_key(value: str) -> str:
    """NFKC + strip + casefold for stable character keys."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


class CharacterCreate(StrictModel):
    name: Name
    aliases: Annotated[list[Name], Field(max_length=MAX_ALIASES)] = []
    description: Annotated[list[ShortText], Field(max_length=MAX_DESC_TRAITS)] = []
    traits: Annotated[list[ShortText], Field(max_length=MAX_DESC_TRAITS)] = []
    background: Annotated[str, Field(max_length=MAX_BACKGROUND)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> CharacterCreate:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class CharacterPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    name: Name | None = None
    aliases: Annotated[list[Name], Field(max_length=MAX_ALIASES)] | None = None
    description: Annotated[list[ShortText], Field(max_length=MAX_DESC_TRAITS)] | None = None
    traits: Annotated[list[ShortText], Field(max_length=MAX_DESC_TRAITS)] | None = None
    background: Annotated[str, Field(max_length=MAX_BACKGROUND)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> CharacterPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class WorldEntryCreate(StrictModel):
    entry_type: Literal["location", "organization", "rule", "item", "other"]
    name: Name
    aliases: Annotated[list[Name], Field(max_length=MAX_ALIASES)] = []
    description: Annotated[str, Field(min_length=1, max_length=MAX_ENTRY_DESC)]

    @model_validator(mode="after")
    def reject_explicit_null(self) -> WorldEntryCreate:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class WorldEntryPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    name: Name | None = None
    aliases: Annotated[list[Name], Field(max_length=MAX_ALIASES)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=MAX_ENTRY_DESC)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> WorldEntryPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class ForeshadowingCreate(StrictModel):
    title: Name
    description: Annotated[str, Field(min_length=1, max_length=MAX_FORESHADOW_DESC)]


class ForeshadowingPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    title: Name | None = None
    description: Annotated[str, Field(min_length=1, max_length=MAX_FORESHADOW_DESC)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> ForeshadowingPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class ForeshadowingResolve(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    resolution: Annotated[str, Field(min_length=1, max_length=MAX_FORESHADOW_RESOLUTION)]


# -- Style profile models --


class StyleProfileData(StrictModel):
    """Data for creating or replacing a style profile."""

    narrative_voice: Annotated[str, Field(max_length=200)] | None = None
    tense: Annotated[str, Field(max_length=50)] | None = None
    pacing: Annotated[str, Field(max_length=200)] | None = None
    tone: Annotated[str, Field(max_length=200)] | None = None
    vocabulary_level: Annotated[str, Field(max_length=200)] | None = None
    sentence_structure: Annotated[str, Field(max_length=200)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None


class StyleProfilePatch(StrictModel):
    """Partial patch for a style profile with optimistic lock."""

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    narrative_voice: Annotated[str, Field(max_length=200)] | None = None
    tense: Annotated[str, Field(max_length=50)] | None = None
    pacing: Annotated[str, Field(max_length=200)] | None = None
    tone: Annotated[str, Field(max_length=200)] | None = None
    vocabulary_level: Annotated[str, Field(max_length=200)] | None = None
    sentence_structure: Annotated[str, Field(max_length=200)] | None = None
    notes: Annotated[str, Field(max_length=2000)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> StyleProfilePatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class StoryBibleService:
    """Service for managing the canonical story bible of a project."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        history: SourceAnalysisHistoryService,
    ) -> None:
        self._sessions = session_factory
        self._history = history

    # -- internal helpers --

    async def _project(
        self, session: AsyncSession, scope: TenantProjectScope, *, write: bool
    ) -> Project:
        result = await session.execute(
            select(Project).where(
                Project.tenant_id == scope.tenant_id,
                Project.id == scope.project_id,
                Project.status == "active",
            ).with_for_update(read=not write, of=Project)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise AnalysisError("project_not_found", 404)
        return project

    async def _get_or_create_bible(
        self, session: AsyncSession, scope: TenantProjectScope, subject: str,
    ) -> StoryBible:
        result = await session.execute(
            select(StoryBible).where(
                StoryBible.tenant_id == scope.tenant_id,
                StoryBible.project_id == scope.project_id,
            ).with_for_update()
        )
        bible = result.scalar_one_or_none()
        if bible is not None:
            return bible
        now = datetime.now(UTC)
        bible = StoryBible(
            id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
            created_by_subject=subject, created_at=now, updated_at=now,
        )
        session.add(bible)
        await session.flush()
        return bible

    async def _bible(
        self, session: AsyncSession, scope: TenantProjectScope,
    ) -> StoryBible:
        result = await session.execute(
            select(StoryBible).where(
                StoryBible.tenant_id == scope.tenant_id,
                StoryBible.project_id == scope.project_id,
            )
        )
        bible = result.scalar_one_or_none()
        if bible is None:
            raise AnalysisError("story_bible_not_found", 404)
        return bible

    async def _character(
        self, session: AsyncSession, scope: TenantProjectScope,
        character_id: UUID, *, write: bool,
    ) -> Character:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(Character).where(
                Character.tenant_id == scope.tenant_id,
                Character.project_id == scope.project_id,
                Character.story_bible_id == bible.id,
                Character.id == character_id,
            ).with_for_update(read=not write)
        )
        character = result.scalar_one_or_none()
        if character is None:
            raise AnalysisError("character_not_found", 404)
        return character

    async def _world_entry(
        self, session: AsyncSession, scope: TenantProjectScope,
        entry_id: UUID, *, write: bool,
    ) -> WorldEntry:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(WorldEntry).where(
                WorldEntry.tenant_id == scope.tenant_id,
                WorldEntry.project_id == scope.project_id,
                WorldEntry.story_bible_id == bible.id,
                WorldEntry.id == entry_id,
            ).with_for_update(read=not write)
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise AnalysisError("world_entry_not_found", 404)
        return entry

    async def _foreshadowing(
        self, session: AsyncSession, scope: TenantProjectScope,
        foreshadowing_id: UUID, *, write: bool,
    ) -> Foreshadowing:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(Foreshadowing).where(
                Foreshadowing.tenant_id == scope.tenant_id,
                Foreshadowing.project_id == scope.project_id,
                Foreshadowing.story_bible_id == bible.id,
                Foreshadowing.id == foreshadowing_id,
            ).with_for_update(read=not write)
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise AnalysisError("foreshadowing_not_found", 404)
        return item

    @staticmethod
    def _character_snapshot(character: Character) -> dict[str, Any]:
        return {
            "name": character.name,
            "aliases": list(character.aliases),
            "description": list(character.description),
            "traits": list(character.traits),
            "background": character.background,
        }

    @staticmethod
    def _world_entry_snapshot(entry: WorldEntry) -> dict[str, Any]:
        return {
            "entry_type": entry.entry_type,
            "name": entry.name,
            "aliases": list(entry.aliases),
            "description": entry.description,
        }

    @staticmethod
    def _foreshadowing_snapshot(item: Foreshadowing) -> dict[str, Any]:
        return {
            "title": item.title,
            "description": item.description,
            "status": item.status,
            "resolution": item.resolution,
        }

    @staticmethod
    def _character_dict(character: Character) -> dict[str, Any]:
        return {
            "id": str(character.id),
            "story_bible_id": str(character.story_bible_id),
            "character_key": character.character_key,
            "name": character.name,
            "aliases": list(character.aliases),
            "description": list(character.description),
            "traits": list(character.traits),
            "background": character.background,
            "current_revision": character.current_revision,
            "source_document_id": (
                str(character.source_document_id) if character.source_document_id else None
            ),
            "analysis_id": str(character.analysis_id) if character.analysis_id else None,
            "created_by_subject": character.created_by_subject,
            "created_at": character.created_at.isoformat(),
            "updated_at": character.updated_at.isoformat(),
        }

    @staticmethod
    def _world_entry_dict(entry: WorldEntry) -> dict[str, Any]:
        return {
            "id": str(entry.id),
            "story_bible_id": str(entry.story_bible_id),
            "entry_type": entry.entry_type,
            "name": entry.name,
            "aliases": list(entry.aliases),
            "description": entry.description,
            "current_revision": entry.current_revision,
            "created_by_subject": entry.created_by_subject,
            "created_at": entry.created_at.isoformat(),
            "updated_at": entry.updated_at.isoformat(),
        }

    @staticmethod
    def _foreshadowing_dict(item: Foreshadowing) -> dict[str, Any]:
        return {
            "id": str(item.id),
            "story_bible_id": str(item.story_bible_id),
            "title": item.title,
            "description": item.description,
            "status": item.status,
            "resolution": item.resolution,
            "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
            "current_revision": item.current_revision,
            "created_by_subject": item.created_by_subject,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    # -- public API: get story bible --

    async def get_bible(self, scope: TenantProjectScope) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            bible = await self._bible(session, scope)
            result = await session.execute(
                select(Character).where(
                    Character.tenant_id == scope.tenant_id,
                    Character.project_id == scope.project_id,
                    Character.story_bible_id == bible.id,
                ).order_by(Character.created_at, Character.id)
            )
            characters = list(result.scalars().all())
            return {"items": [self._character_dict(c) for c in characters]}

    async def create_character(
        self, scope: TenantProjectScope, data: CharacterCreate, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            bible = await self._get_or_create_bible(session, scope, subject)
            key = _normalize_key(data.name)
            existing = await session.execute(
                select(Character).where(
                    Character.tenant_id == scope.tenant_id,
                    Character.project_id == scope.project_id,
                    Character.story_bible_id == bible.id,
                    Character.character_key == key,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise AnalysisError("character_already_exists", 409)
            now = datetime.now(UTC)
            character = Character(
                id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
                story_bible_id=bible.id, character_key=key,
                name=data.name, aliases=list(data.aliases),
                description=list(data.description), traits=list(data.traits),
                background=data.background, current_revision=0,
                source_document_id=None, analysis_id=None,
                created_by_subject=subject, created_at=now, updated_at=now,
            )
            session.add(character)
            await session.flush()
            snapshot = self._character_snapshot(character)
            revision = CharacterRevision(
                id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
                story_bible_id=bible.id, character_id=character.id,
                revision=0, snapshot=deepcopy(snapshot), subject=subject, created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._character_dict(character)

    # -- public API: style profile --

    @staticmethod
    def _style_profile_snapshot(profile: StyleProfile) -> dict[str, Any]:
        return {
            "narrative_voice": profile.narrative_voice,
            "tense": profile.tense,
            "pacing": profile.pacing,
            "tone": profile.tone,
            "vocabulary_level": profile.vocabulary_level,
            "sentence_structure": profile.sentence_structure,
            "notes": profile.notes,
        }

    @staticmethod
    def _style_profile_dict(profile: StyleProfile) -> dict[str, Any]:
        return {
            "id": str(profile.id),
            "story_bible_id": str(profile.story_bible_id),
            "narrative_voice": profile.narrative_voice,
            "tense": profile.tense,
            "pacing": profile.pacing,
            "tone": profile.tone,
            "vocabulary_level": profile.vocabulary_level,
            "sentence_structure": profile.sentence_structure,
            "notes": profile.notes,
            "current_revision": profile.current_revision,
            "created_by_subject": profile.created_by_subject,
            "created_at": profile.created_at.isoformat(),
            "updated_at": profile.updated_at.isoformat(),
        }

    async def get_style_profile(
        self, scope: TenantProjectScope,
    ) -> dict[str, Any] | None:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            await self._bible(session, scope)
            result = await session.execute(
                select(StyleProfile).where(
                    StyleProfile.tenant_id == scope.tenant_id,
                    StyleProfile.project_id == scope.project_id,
                )
            )
            profile = result.scalar_one_or_none()
            if profile is None:
                return None
            return self._style_profile_dict(profile)

    async def upsert_style_profile(
        self, scope: TenantProjectScope,
        data: StyleProfileData, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            bible = await self._get_or_create_bible(session, scope, subject)
            result = await session.execute(
                select(StyleProfile).where(
                    StyleProfile.tenant_id == scope.tenant_id,
                    StyleProfile.project_id == scope.project_id,
                ).with_for_update()
            )
            profile = result.scalar_one_or_none()
            now = datetime.now(UTC)
            if profile is None:
                profile = StyleProfile(
                    id=uuid4(), tenant_id=scope.tenant_id,
                    project_id=scope.project_id, story_bible_id=bible.id,
                    narrative_voice=data.narrative_voice, tense=data.tense,
                    pacing=data.pacing, tone=data.tone,
                    vocabulary_level=data.vocabulary_level,
                    sentence_structure=data.sentence_structure,
                    notes=data.notes, current_revision=0,
                    created_by_subject=subject, created_at=now, updated_at=now,
                )
                session.add(profile)
                await session.flush()
            else:
                profile.narrative_voice = data.narrative_voice
                profile.tense = data.tense
                profile.pacing = data.pacing
                profile.tone = data.tone
                profile.vocabulary_level = data.vocabulary_level
                profile.sentence_structure = data.sentence_structure
                profile.notes = data.notes
                profile.current_revision += 1
                profile.updated_at = now
                await session.flush()
            snapshot = self._style_profile_snapshot(profile)
            revision = StyleProfileRevision(
                id=uuid4(), tenant_id=scope.tenant_id,
                project_id=scope.project_id, style_profile_id=profile.id,
                revision=profile.current_revision,
                snapshot=deepcopy(snapshot), subject=subject, created_at=now,
            )
            session.add(revision)
            await session.flush()
            bible.updated_at = now
            return self._style_profile_dict(profile)

    async def patch_style_profile(
        self, scope: TenantProjectScope,
        patch: StyleProfilePatch, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            bible = await self._bible(session, scope)
            result = await session.execute(
                select(StyleProfile).where(
                    StyleProfile.tenant_id == scope.tenant_id,
                    StyleProfile.project_id == scope.project_id,
                ).with_for_update()
            )
            profile = result.scalar_one_or_none()
            if profile is None:
                raise AnalysisError("style_profile_not_found", 404)
            if profile.current_revision != patch.expected_revision:
                raise AnalysisError("style_profile_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(profile, field_name, value)
            profile.current_revision += 1
            profile.updated_at = now
            await session.flush()
            snapshot = self._style_profile_snapshot(profile)
            revision = StyleProfileRevision(
                id=uuid4(), tenant_id=scope.tenant_id,
                project_id=scope.project_id, style_profile_id=profile.id,
                revision=profile.current_revision,
                snapshot=deepcopy(snapshot), subject=subject, created_at=now,
            )
            session.add(revision)
            await session.flush()
            bible.updated_at = now
            return self._style_profile_dict(profile)

    async def get_foreshadowing(
        self, scope: TenantProjectScope, foreshadowing_id: UUID,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            item = await self._foreshadowing(session, scope, foreshadowing_id, write=False)
            return self._foreshadowing_dict(item)

    async def patch_foreshadowing(
        self, scope: TenantProjectScope, foreshadowing_id: UUID,
        patch: ForeshadowingPatch, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            item = await self._foreshadowing(session, scope, foreshadowing_id, write=True)
            if item.current_revision != patch.expected_revision:
                raise AnalysisError("foreshadowing_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(item, field_name, value)
            item.current_revision += 1
            item.updated_at = now
            await session.flush()
            snapshot = self._foreshadowing_snapshot(item)
            revision = ForeshadowingRevision(
                id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
                story_bible_id=item.story_bible_id, foreshadowing_id=item.id,
                revision=item.current_revision,
                snapshot=deepcopy(snapshot), subject=subject, created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._foreshadowing_dict(item)

    async def resolve_foreshadowing(
        self, scope: TenantProjectScope, foreshadowing_id: UUID,
        data: ForeshadowingResolve, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            item = await self._foreshadowing(session, scope, foreshadowing_id, write=True)
            if item.current_revision != data.expected_revision:
                raise AnalysisError("foreshadowing_revision_conflict", 409)
            if item.status == "resolved":
                raise AnalysisError("foreshadowing_already_resolved", 409)
            now = datetime.now(UTC)
            item.status = "resolved"
            item.resolution = data.resolution
            item.resolved_at = now
            item.current_revision += 1
            item.updated_at = now
            await session.flush()
            snapshot = self._foreshadowing_snapshot(item)
            revision = ForeshadowingRevision(
                id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
                story_bible_id=item.story_bible_id, foreshadowing_id=item.id,
                revision=item.current_revision,
                snapshot=deepcopy(snapshot), subject=subject, created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._foreshadowing_dict(item)
