from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from novel_agent.db.models.project import Project, StoryBranch

RequestId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
ProjectTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class CreateProject(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: RequestId
    title: ProjectTitle
    language: Literal["zh-CN"] = "zh-CN"


@dataclass(frozen=True)
class CreatedProject:
    project: Project
    default_branch: StoryBranch
