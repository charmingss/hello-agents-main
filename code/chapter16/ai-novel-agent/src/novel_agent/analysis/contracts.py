from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

PROMPT_VERSION = "source-analysis-preview-v1"
MAX_CHARS = 24_000
MAX_SECTIONS = 200


class AnalysisError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Section:
    id: UUID
    index: int
    heading: str | None
    text: str
    char_start: int
    original_length: int
    source_ref_kind: str | None
    source_ref_index: int | None
    source_ref_subindex: int | None


@dataclass(frozen=True)
class Window:
    sections: tuple[Section, ...]
    char_count: int
    partial: bool


@dataclass(frozen=True)
class SourceIdentity:
    source_id: UUID
    parse_run_id: UUID
    product_version: int
    product_sha256: str
    display_name: str


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500, pattern=r"\S")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=120, pattern=r"\S")]


class Evidence(StrictModel):
    section_id: UUID
    quote: ShortText


class Claim(StrictModel):
    text: ShortText
    kind: Literal["explicit", "reported", "inferred"]
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
    attributed_speaker: Name | None = None
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def require_reported_speaker(self) -> "Claim":
        if self.kind == "reported" and self.attributed_speaker is None:
            raise ValueError("reported claims require a speaker or 'unknown'")
        if self.kind != "reported" and self.attributed_speaker is not None:
            raise ValueError("speaker applies only to reported claims")
        return self


class Character(StrictModel):
    name: Name
    aliases: Annotated[list[Name], Field(max_length=10)]
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=8)]
    description: Annotated[list[Claim], Field(max_length=10)]
    traits: Annotated[list[Claim], Field(max_length=10)]
    abilities: Annotated[list[Claim], Field(max_length=10)] = []
    deeds: Annotated[list[Claim], Field(max_length=10)] = []


class Candidate(StrictModel):
    summary: Annotated[list[Claim], Field(max_length=20)]
    characters: Annotated[list[Character], Field(max_length=30)]


class AnalysisProvider(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def generate(self, window: Window) -> str: ...
