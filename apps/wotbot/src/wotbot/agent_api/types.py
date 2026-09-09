"""Durable application tasks and outputs. No protocol SDK objects cross this boundary."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from pydantic.alias_generators import to_camel

from wotbot.core.time import utc_now


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel, extra="allow")

    def json(self):
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class TaskState(StrEnum):
    TASK_STATE_SUBMITTED = "TASK_STATE_SUBMITTED"
    TASK_STATE_WORKING = "TASK_STATE_WORKING"
    TASK_STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    TASK_STATE_AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"
    TASK_STATE_COMPLETED = "TASK_STATE_COMPLETED"
    TASK_STATE_FAILED = "TASK_STATE_FAILED"
    TASK_STATE_CANCELED = "TASK_STATE_CANCELED"
    TASK_STATE_REJECTED = "TASK_STATE_REJECTED"

    @classmethod
    def Name(cls, value):
        return cls(value).value


class Part(Model):
    text: str | None = None
    data: Any = None
    url: str | None = None
    media_type: str | None = None
    filename: str | None = None

    @model_serializer(mode="wrap")
    def serialize_part(self, handler):
        result = handler(self)
        if "data" in self.model_fields_set:
            result["data"] = self.data
        return result


class Message(Model):
    message_id: str
    context_id: str | None = None
    task_id: str | None = None
    role: str = "ROLE_USER"
    parts: list[Part] = Field(default_factory=list)


class Artifact(Model):
    artifact_id: str
    name: str = ""
    parts: list[Part] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Status(Model):
    state: TaskState = TaskState.TASK_STATE_SUBMITTED
    timestamp: datetime = Field(default_factory=utc_now)
    message: Message | None = None


class Task(Model):
    id: str
    context_id: str
    status: Status = Field(default_factory=Status)
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStatusUpdateEvent(Model):
    task_id: str
    context_id: str
    status: Status
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskArtifactUpdateEvent(Model):
    task_id: str
    context_id: str
    artifact: Artifact
    last_chunk: bool = True


class Operation(Model):
    kind: Literal["assistant", "intent", "raw"] = "assistant"
    name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @property
    def family(self):
        return "raw" if self.kind == "raw" else "assistant"


class TaskQuery(Model):
    context_id: str = ""
    page_size: int = 50
    page_token: str = ""
    status: str | None = None
    status_timestamp_after: datetime | None = None
    family: str = "assistant"


class Invocation(Model):
    message: Message
    operation: Operation = Field(default_factory=Operation)
    origin: str = "a2a"
    metadata: dict[str, Any] = Field(default_factory=dict)
