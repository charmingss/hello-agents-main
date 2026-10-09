import uuid

import pytest
from pydantic import ValidationError

from novel_agent.outbox.events import ProjectCreatedV1


def test_project_created_event_json_round_trip() -> None:
    event = ProjectCreatedV1(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        actor_id="oidc|user-1",
        request_id="req-1",
        title="长夜",
        language="zh-CN",
        version=1,
    )

    restored = ProjectCreatedV1.model_validate_json(event.model_dump_json())

    assert restored == event
    assert restored.event_type == "project.created.v1"


def test_project_created_event_rejects_uuid_strings_in_python_input() -> None:
    uuid_string = str(uuid.uuid4())

    with pytest.raises(ValidationError):
        ProjectCreatedV1(
            event_id=uuid_string,  # type: ignore[arg-type]
            tenant_id=uuid_string,  # type: ignore[arg-type]
            project_id=uuid_string,  # type: ignore[arg-type]
            branch_id=uuid_string,  # type: ignore[arg-type]
            actor_id="oidc|user-1",
            request_id="req-1",
            title="长夜",
            language="zh-CN",
            version=1,
        )


def test_project_created_event_rejects_empty_actor_subject() -> None:
    with pytest.raises(ValidationError):
        ProjectCreatedV1(
            event_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            actor_id="",
            request_id="req-1",
            title="长夜",
            language="zh-CN",
        )
