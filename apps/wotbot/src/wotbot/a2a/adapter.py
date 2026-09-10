"""Conversion and error mapping at the official A2A SDK boundary."""

from datetime import UTC
from functools import wraps

from a2a import types
from a2a.utils import errors as wire_errors
from google.protobuf.json_format import ParseDict

from wotbot.agent_api import errors
from wotbot.agent_api.types import TaskQuery


def to_wire(value):
    return ParseDict(_omit_empty_metadata(value.json()), getattr(types, type(value).__name__)())


def wire_error(error):
    return getattr(wire_errors, type(error).__name__)(str(error))


def query(params):
    return TaskQuery(
        context_id=params.context_id,
        page_size=params.page_size or 50,
        page_token=params.page_token,
        status=types.TaskState.Name(params.status) if params.status else None,
        status_timestamp_after=params.status_timestamp_after.ToDatetime().replace(tzinfo=UTC)
        if params.HasField("status_timestamp_after")
        else None,
    )


def translate(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except errors.AgentError as error:
            raise wire_error(error) from error

    return run


def _omit_empty_metadata(value):
    """Omit empty protocol metadata without walking opaque JSON payloads."""
    if isinstance(value, dict):
        return {
            key: item if key in {"data", "metadata"} else _omit_empty_metadata(item)
            for key, item in value.items()
            if key != "metadata" or item
        }
    if isinstance(value, list):
        return [_omit_empty_metadata(item) for item in value]
    return value
