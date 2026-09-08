from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

from wotbot.core.text import truncate_text as _truncate_text
from wotbot.jobs.enums import (
    JobInteractionMode,
    JobRunStatus,
)
from wotbot.jobs.schemas import Job, JobRun, PromptAction, StructuredRecordOutput


@dataclass(frozen=True)
class ParsedGraphResult:
    assistant: str
    waiting_question: str | None
    submitted_record: dict[str, Any] | None
    failed_record_submission: dict[str, Any] | None
    code_result: dict[str, Any] | None
    has_interrupt: bool


def graph_input_for_run(job: Job, run: JobRun, message: str | None) -> Any:
    if message is None:
        return {"messages": [HumanMessage(content=job_run_prompt(job))]}
    if result_has_pending_interrupt(run.result):
        return Command(resume=str(message))
    # Compatibility for runs that were already waiting before ask_job_user
    # used LangGraph interrupts: append the reply and let the checkpointer
    # carry the prior context instead of resuming a pending interrupt.
    return {"messages": [HumanMessage(content=str(message))]}


def graph_config_for_run(job: Job, run: JobRun, recursion_limit: int) -> dict[str, Any]:
    return {
        "recursion_limit": recursion_limit,
        "configurable": {
            "thread_id": run.job_thread_id,
            "job_id": job.id,
            "run_id": run.id,
            "job_output_kind": job.output.kind,
            "record_schema": job.output.schema
            if isinstance(job.output, StructuredRecordOutput)
            else None,
            "record_schema_version": (
                job.output.schema_version
                if isinstance(job.output, StructuredRecordOutput)
                else None
            ),
            "virtual_thing_id": (
                job.output.virtual_thing_id
                if isinstance(job.output, StructuredRecordOutput)
                else None
            ),
        },
    }


def job_result_from_graph_result(
    result: Any,
    *,
    job: Job,
    message: str | None,
    trigger: dict[str, Any],
) -> dict[str, Any]:
    parsed = parse_graph_result(result)

    if parsed.waiting_question:
        return _waiting_result(result, parsed.waiting_question, trigger, parsed=parsed)
    if (
        isinstance(job.output, StructuredRecordOutput)
        and parsed.submitted_record is None
        and _record_submission_needs_user_repair(parsed.failed_record_submission)
    ):
        error = _record_submission_error(parsed.failed_record_submission)
        return _waiting_result(
            result,
            _record_repair_question(error),
            trigger,
            metadata={"record_submission_error": error},
            parsed=parsed,
        )
    if (
        message is None
        and job.interaction_mode == JobInteractionMode.REQUIRED_CHECKIN
        and parsed.assistant
    ):
        return _waiting_result(result, parsed.assistant, trigger, parsed=parsed)
    if isinstance(job.output, StructuredRecordOutput) and parsed.submitted_record is None:
        return _failed_result(
            "Structured record job finished without submitting a valid record.",
            result,
            trigger,
        )
    if not parsed.assistant and parsed.submitted_record is None and message is not None:
        return _failed_result(
            "Prompt job did not produce a response after the user reply.",
            result,
            trigger,
        )
    assistant = parsed.assistant or json.dumps(result, ensure_ascii=True, default=str)[:2000]
    job_result: dict[str, Any] = {
        "ok": True,
        "response": result,
        "assistant": assistant,
        "submitted_record": parsed.submitted_record,
        "metadata": {"trigger": trigger},
    }
    if parsed.code_result:
        job_result.update(parsed.code_result)
    return job_result


def parse_graph_result(result: Any) -> ParsedGraphResult:
    messages = _messages_after_latest_human(result)
    return ParsedGraphResult(
        assistant=_assistant_text_from_messages(messages),
        waiting_question=_waiting_question_from_messages(result, messages),
        submitted_record=_submitted_record_from_messages(messages),
        failed_record_submission=_failed_record_submission_from_messages(messages),
        code_result=_code_result_from_messages(messages),
        has_interrupt=_graph_result_has_interrupt(result),
    )


def job_run_status_from_result(result: dict[str, Any]) -> JobRunStatus:
    if result.get("status") == JobRunStatus.WAITING_FOR_INPUT.value:
        return JobRunStatus.WAITING_FOR_INPUT
    if result.get("status") == JobRunStatus.SKIPPED.value:
        return JobRunStatus.SKIPPED
    if result.get("ok"):
        return JobRunStatus.SUCCEEDED
    return JobRunStatus.FAILED


def assistant_text_from_graph_result(result: Any) -> str:
    return parse_graph_result(result).assistant


def _assistant_text_from_messages(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        return _text_from_message_content(message.content).strip()
    return ""


def waiting_question_from_graph_result(result: Any) -> str | None:
    return parse_graph_result(result).waiting_question


def _waiting_question_from_messages(result: Any, messages: list[Any]) -> str | None:
    interrupt_question = _interrupt_question_from_graph_result(result)
    if interrupt_question:
        return interrupt_question

    message = _latest_tool_message(messages, "ask_job_user")
    if message is None:
        return None
    content = message.content
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return content.strip() or None
    if isinstance(content, dict):
        if content.get("status") == "input_received" or "answer" in content:
            return None
        question = content.get("question")
        if isinstance(question, str) and question.strip():
            return question.strip()
    return None


def submitted_record_from_graph_result(result: Any) -> dict[str, Any] | None:
    return parse_graph_result(result).submitted_record


def _submitted_record_from_messages(messages: list[Any]) -> dict[str, Any] | None:
    message = _latest_tool_message(messages, "submit_job_record")
    if message is None:
        return None
    content = _parsed_tool_message_content(message)
    if not isinstance(content, dict) or not content.get("ok"):
        return None
    record = content.get("record")
    return record if isinstance(record, dict) else content


def failed_record_submission_from_graph_result(result: Any) -> dict[str, Any] | None:
    return parse_graph_result(result).failed_record_submission


def _failed_record_submission_from_messages(messages: list[Any]) -> dict[str, Any] | None:
    message = _latest_tool_message(messages, "submit_job_record")
    if message is None:
        return None
    content = _parsed_tool_message_content(message)
    if not isinstance(content, dict) or content.get("ok") is not False:
        return None
    return content


def code_result_from_graph_result(result: Any) -> dict[str, Any] | None:
    return parse_graph_result(result).code_result


def _code_result_from_messages(messages: list[Any]) -> dict[str, Any] | None:
    stdout_parts: list[str] = []
    error_parts: list[str] = []
    artifacts: list[dict[str, Any]] = []

    for message in _tool_messages(messages, "run_code"):
        content = _parsed_tool_message_content(message)
        if not isinstance(content, dict):
            continue
        message_artifacts = _artifacts_from_run_code_result(content)
        if not message_artifacts:
            continue
        artifacts.extend(message_artifacts)
        stdout = content.get("stdout")
        if isinstance(stdout, str) and stdout.strip():
            stdout_parts.append(stdout.strip())
        error = content.get("error")
        if isinstance(error, str) and error.strip():
            error_parts.append(error.strip())

    artifacts = _dedupe_and_renumber_artifacts(artifacts)
    if not artifacts:
        return None

    code_result: dict[str, Any] = {"artifacts": artifacts}
    if stdout_parts:
        code_result["stdout"] = _truncate_text("\n\n".join(stdout_parts), max_length=4000)
    if error_parts:
        code_result["error"] = _truncate_text("\n\n".join(error_parts), max_length=2000)
    return code_result


def _artifacts_from_run_code_result(content: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = _artifacts_from_mappings(content.get("artifacts"))
    if artifacts:
        return artifacts

    return [
        *_artifacts_from_filenames(content.get("images"), ref_prefix="image", kind="image"),
        *_artifacts_from_filenames(content.get("plotly"), ref_prefix="chart", kind="plotly"),
        *_artifacts_from_mappings(content.get("files")),
    ]


def _artifacts_from_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, Any]] = []
    for raw_artifact in value:
        artifact = _artifact_from_mapping(raw_artifact)
        if artifact:
            artifacts.append(artifact)
    return artifacts


def _artifacts_from_filenames(
    value: Any,
    *,
    ref_prefix: str,
    kind: str,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"ref": f"{ref_prefix}_{index}", "kind": kind, "filename": filename}
        for index, filename in enumerate(value, start=1)
        if isinstance(filename, str) and filename
    ]


def _artifact_from_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    filename = value.get("filename")
    ref = value.get("ref")
    if kind not in {"image", "plotly", "file"}:
        return None
    if not isinstance(filename, str) or not filename:
        return None
    if not isinstance(ref, str) or not ref:
        ref = "artifact"
    if kind == "file":
        if not isinstance(value.get("id"), str) or not value["id"]:
            return None
        return {**value, "ref": ref}
    return {"ref": ref, "kind": kind, "filename": filename}


def _dedupe_and_renumber_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    image_count = 0
    chart_count = 0
    file_count = 0
    normalized: list[dict[str, Any]] = []
    for artifact in artifacts:
        key = (artifact["kind"], artifact.get("id", artifact["filename"]))
        if key in seen:
            continue
        seen.add(key)
        if artifact["kind"] == "image":
            image_count += 1
            ref = f"image_{image_count}"
        elif artifact["kind"] == "file":
            file_count += 1
            ref = f"file_{file_count}"
        else:
            chart_count += 1
            ref = f"chart_{chart_count}"
        normalized.append({**artifact, "ref": ref})
    return normalized


def result_has_pending_interrupt(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata.get("pending_interrupt") is True:
        return True
    response = result.get("response")
    return _graph_result_has_interrupt(response) or _graph_result_has_interrupt(result)


def job_run_prompt(job: Job) -> str:
    prompt = job.action.prompt if isinstance(job.action, PromptAction) else ""
    if not isinstance(job.output, StructuredRecordOutput):
        return prompt
    schema = json.dumps(job.output.schema or {}, ensure_ascii=True, indent=2)
    return (
        f"{prompt}\n\n"
        "## Structured Record Contract\n"
        "This background job must store exactly one validated record before it "
        "finishes successfully. If user input is needed, call ask_job_user and stop. "
        "After receiving enough information, call submit_job_record with data that "
        "matches this JSON Schema. Do not claim success until submit_job_record "
        "returns ok=true.\n\n"
        f"JSON Schema:\n{schema}"
    )


def _failed_result(error: str, result: Any, trigger: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "response": result,
        "metadata": {"trigger": trigger},
    }


def _waiting_result(
    result: Any,
    question: str,
    trigger: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    parsed: ParsedGraphResult | None = None,
) -> dict[str, Any]:
    metadata = {"trigger": trigger, **(metadata or {})}
    if (parsed is not None and parsed.has_interrupt) or (
        parsed is None and _graph_result_has_interrupt(result)
    ):
        metadata["pending_interrupt"] = True
    return {
        "ok": True,
        "status": JobRunStatus.WAITING_FOR_INPUT.value,
        "response": result,
        "assistant": question,
        "waiting_question": question,
        "metadata": metadata,
    }


def _latest_tool_message(messages: list[Any], tool_name: str) -> ToolMessage | None:
    for message in reversed(_tool_messages(messages, tool_name)):
        return message
    return None


def _tool_messages(messages: list[Any], tool_name: str) -> list[ToolMessage]:
    return [
        message
        for message in messages
        if isinstance(message, ToolMessage) and message.name == tool_name
    ]


def _messages_after_latest_human(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    messages = result.get("messages")
    if not isinstance(messages, list):
        return []
    latest_human_index = -1
    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            latest_human_index = index
    return messages[latest_human_index + 1 :]


def _parsed_tool_message_content(message: ToolMessage) -> Any:
    content = message.content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return content


def _record_submission_needs_user_repair(submission: dict[str, Any] | None) -> bool:
    if submission is None:
        return False
    if submission.get("repairable") is True:
        return True
    if submission.get("repairable") is False:
        return False
    error = _record_submission_error(submission)
    return (
        error.startswith("record data failed schema validation")
        or error == "structured record data must be an object"
    )


def _record_submission_error(submission: dict[str, Any] | None) -> str:
    if not isinstance(submission, dict):
        return "structured record data did not match the schema"
    error = submission.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "structured record data did not match the schema"


def _record_repair_question(error: str) -> str:
    return (
        "I could not store the structured record because the answer did not match "
        f"the required schema: {error}. Please clarify or correct the values."
    )


def _graph_result_has_interrupt(result: Any) -> bool:
    return bool(_interrupt_values_from_graph_result(result))


def _interrupt_question_from_graph_result(result: Any) -> str | None:
    for value in _interrupt_values_from_graph_result(result):
        question = _question_from_interrupt_value(value)
        if question:
            return question
    return None


def _interrupt_values_from_graph_result(result: Any) -> list[Any]:
    if not isinstance(result, dict):
        return []
    interrupts = result.get("__interrupt__")
    if not isinstance(interrupts, (list, tuple)):
        return []

    values: list[Any] = []
    for interrupt in interrupts:
        if isinstance(interrupt, dict) and "value" in interrupt:
            values.append(interrupt.get("value"))
        elif hasattr(interrupt, "value"):
            values.append(getattr(interrupt, "value"))
        else:
            values.append(interrupt)
    return values


def _question_from_interrupt_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    for key in ("question", "message", "instruction"):
        question = value.get(key)
        if isinstance(question, str) and question.strip():
            return question.strip()
    return None


def _text_from_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    text_parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            text_parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            text_parts.append(item["text"])
    return "".join(text_parts)
