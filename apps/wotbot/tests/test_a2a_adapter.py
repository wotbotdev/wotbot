"""A2A conversion preserves opaque JSON in results and protocol metadata."""

import pytest
from google.protobuf.json_format import MessageToDict

from wotbot.a2a.adapter import to_wire
from wotbot.agent_api.types import (
    Artifact,
    Message,
    Part,
    Status,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)


@pytest.mark.parametrize("metadata", [{}, [], None, False, 0, ""])
def test_to_wire_preserves_json_data_and_metadata(metadata):
    payload = {
        "metadata": metadata,
        "nested": {"metadata": metadata},
        "items": [{"metadata": metadata}],
    }
    message = Message(message_id="message", parts=[Part(data=payload)], metadata=payload)
    artifact = Artifact(artifact_id="artifact", parts=[Part(data=payload)], metadata=payload)
    task = Task(
        id="task",
        context_id="context",
        history=[message],
        status=Status(message=message),
        artifacts=[artifact],
        metadata=payload,
    )
    original = task.json()

    wire = MessageToDict(to_wire(task))

    assert wire["metadata"] == payload
    for item in (wire["history"][0], wire["status"]["message"], wire["artifacts"][0]):
        assert item["parts"][0]["data"] == payload
        assert item["metadata"] == payload
    assert task.json() == original


@pytest.mark.parametrize("event_type", [Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent])
def test_to_wire_omits_empty_protocol_metadata(event_type):
    artifact = Artifact(artifact_id="artifact", parts=[Part(data={"metadata": {}})])
    if event_type is Task:
        event = Task(id="task", context_id="context", artifacts=[artifact])
    elif event_type is TaskArtifactUpdateEvent:
        event = TaskArtifactUpdateEvent(task_id="task", context_id="context", artifact=artifact)
    else:
        event = TaskStatusUpdateEvent(task_id="task", context_id="context", status=Status())

    wire = MessageToDict(to_wire(event))

    assert "metadata" not in wire
    artifacts = wire.get("artifacts", [])
    if "artifact" in wire:
        artifacts.append(wire["artifact"])
    for item in artifacts:
        assert "metadata" not in item
        assert item["parts"][0]["data"] == {"metadata": {}}
