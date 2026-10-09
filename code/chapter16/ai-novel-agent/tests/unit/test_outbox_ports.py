from __future__ import annotations

import uuid
from collections import deque
from datetime import UTC, datetime

import pytest

from novel_agent.outbox.ports import OutboxEnvelope


def _envelope(payload: dict[str, object]) -> OutboxEnvelope:
    return OutboxEnvelope(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        aggregate_type="project",
        aggregate_id=uuid.uuid4(),
        aggregate_version=1,
        event_type="project.created.v1",
        payload=payload,
        occurred_at=datetime.now(UTC),
    )


def test_outbox_payload_is_a_defensive_deeply_immutable_snapshot() -> None:
    traits = ["calm"]
    source: dict[str, object] = {
        "character": {"traits": traits},
    }
    envelope = _envelope(source)
    character = envelope.payload["character"]
    assert isinstance(character, dict) is False
    source_character = source["character"]
    assert isinstance(source_character, dict)
    traits.append("changed")
    source["new"] = "later"
    assert envelope.model_dump(mode="json")["payload"] == {
        "character": {"traits": ["calm"]}
    }
    with pytest.raises(TypeError):
        envelope.payload["new"] = "blocked"  # type: ignore[index]
    with pytest.raises(AttributeError):
        envelope.payload["character"]["traits"].append("blocked")  # type: ignore[index,union-attr]


def test_outbox_payload_round_trips_as_json() -> None:
    envelope = _envelope({"nested": {"items": (1, True, None, 1.25, "text")}})
    restored = OutboxEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope


@pytest.mark.parametrize(
    "invalid",
    [
        {"set"},
        frozenset({"set"}),
        bytearray(b"bytes"),
        deque(["item"]),
        object(),
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_outbox_payload_rejects_non_json_values(invalid: object) -> None:
    with pytest.raises(ValueError):
        _envelope({"invalid": invalid})


def test_outbox_payload_rejects_non_string_keys_at_any_depth() -> None:
    with pytest.raises(ValueError):
        _envelope({"nested": {1: "invalid"}})
