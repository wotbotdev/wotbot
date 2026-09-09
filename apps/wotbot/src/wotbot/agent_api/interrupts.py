"""Explicit public reply schemas; never accept caller-supplied graph commands."""

from typing import Any

from jsonschema import ValidationError, validate

from wotbot.agent_api.errors import InvalidParamsError


def describe_interrupt(interrupt: Any) -> dict[str, Any]:
    value = interrupt.value
    detail = dict(value) if isinstance(value, dict) else {"question": str(value)}
    # Internal graph identity is never a public continuation handle.
    for key in ("thread_id", "run_id", "job_id"):
        detail.pop(key, None)
    kind = str(detail.get("kind") or "input")
    if kind == "credential":
        schema = _object({"status": {"enum": ["credential_saved", "cancelled"]}}, ["status"])
        explanation = "Provision credentials through the existing credential API, then reply credential_saved. Do not send credentials in this conversation."
    elif kind == "source_registration":
        schema = _object(
            {
                "status": {"enum": ["source_registered", "cancelled"]},
                "source_id": {"type": "string", "minLength": 1},
                "thing_id": {"type": "string", "minLength": 1},
            },
            ["status"],
        )
        schema["if"] = {"properties": {"status": {"const": "source_registered"}}}
        schema["then"] = {"required": ["source_id"]}
        explanation = "Review the source draft and register it through the source API, then return its source_id, or cancel."
    elif kind == "confirmation":
        schema = _object({"approved": {"type": "boolean"}}, ["approved"])
        explanation = str(detail.get("question") or "Confirm whether execution may continue.")
    else:
        schema = {"type": "string", "minLength": 1, "maxLength": 10000}
        explanation = str(detail.get("question") or "Additional input is required.")
    return {
        "requestId": interrupt.id,
        "kind": kind,
        "explanation": explanation,
        "details": detail,
        "responseSchema": schema,
    }


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_reply(pending: list[dict], parts: list[dict]) -> dict[str, Any]:
    replies: dict[str, Any] = {}
    by_id = {p["requestId"]: p for p in pending}
    for part in parts:
        body = part.get("data")
        if not isinstance(body, dict) or set(body) != {"requestId", "response"}:
            raise InvalidParamsError(
                "A continuation needs JSON parts containing requestId and response"
            )
        request_id = body["requestId"]
        if not isinstance(request_id, str) or request_id not in by_id or request_id in replies:
            raise InvalidParamsError("Unknown or duplicate interruption requestId")
        try:
            validate(body["response"], by_id[request_id]["responseSchema"])
        except ValidationError as exc:
            raise InvalidParamsError(
                "Response does not match the interruption responseSchema"
            ) from exc
        replies[request_id] = body["response"]
    if set(replies) != set(by_id):
        raise InvalidParamsError("Reply to every pending interruption in this task")
    return replies
