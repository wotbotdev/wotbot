"""A2A input validation and SDK facade over shared application execution."""

import json
from contextlib import aclosing
from dataclasses import replace

from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InvalidParamsError,
    UnsupportedOperationError,
)
from google.protobuf.json_format import MessageToDict

from wotbot.a2a.adapter import to_wire, wire_error
from wotbot.a2a.store import TaskStore
from wotbot.agent_api.errors import AgentError
from wotbot.agent_api.runtime import AgentRuntime


def validate_send(params) -> dict:
    request = MessageToDict(params)
    message = request.get("message", {})
    if params.tenant:
        raise InvalidParamsError("Tenants are not supported")
    if not message.get("messageId") or len(message["messageId"]) > 200:
        raise InvalidParamsError("A messageId of 1–200 characters is required")
    if message.get("role") != "ROLE_USER" or not message.get("parts"):
        raise InvalidParamsError("A nonempty ROLE_USER message is required")
    if message.get("referenceTaskIds") or message.get("extensions"):
        raise UnsupportedOperationError("Task references and extensions are not supported")
    for part in message["parts"]:
        if not ("text" in part or "data" in part) or "raw" in part or "url" in part:
            raise ContentTypeNotSupportedError("Only text and JSON message parts are supported")
        expected = "text/plain" if "text" in part else "application/json"
        if part.get("mediaType", expected).split(";", 1)[0].lower() != expected:
            raise ContentTypeNotSupportedError(f"This message part requires {expected}")
    if len(json.dumps(request)) > 1_000_000:
        raise InvalidParamsError("Message is too large")
    config = request.get("configuration", {})
    if config.get("taskPushNotificationConfig"):
        raise UnsupportedOperationError("Push notifications are not supported")
    if params.configuration.history_length < 0:
        raise InvalidParamsError("historyLength must be nonnegative")
    return request


class A2ARuntime:
    def __init__(self, *, service=None, **kwargs):
        if "store" in kwargs:
            kwargs["store"] = getattr(kwargs["store"], "neutral", kwargs["store"])
        self.service = service or AgentRuntime(**kwargs)
        self.store = TaskStore(neutral=self.service.store)

    def __getattr__(self, name):
        return getattr(self.service, name)

    async def admit(self, owner, params):
        try:
            admission = await self.service.admit(owner, validate_send(params))
            return replace(admission, task=to_wire(admission.task))
        except AgentError as error:
            raise wire_error(error) from error

    async def cancel(self, owner, task_id):
        try:
            return to_wire(await self.service.cancel(owner, task_id, family="assistant"))
        except AgentError as error:
            raise wire_error(error) from error

    async def subscribe(self, owner, task_id, **kwargs):
        try:
            async with aclosing(
                self.service.subscribe(owner, task_id, family="assistant", **kwargs)
            ) as events:
                async for event in events:
                    yield to_wire(event)
        except AgentError as error:
            raise wire_error(error) from error
