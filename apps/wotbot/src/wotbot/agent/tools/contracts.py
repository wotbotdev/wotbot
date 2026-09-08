"""Shared tool contracts with strict outer arguments and preserved nested schemas."""

import inspect
from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, ConfigDict, ValidationError, create_model, model_validator


def validation_message(error: ValidationError) -> str:
    """Explain how to repair a call without reflecting its argument values."""
    problems = []
    for item in error.errors(include_input=False, include_url=False)[:8]:
        path = ".".join(str(part) for part in item["loc"])
        reason = "unknown argument" if item["type"] == "extra_forbidden" else item["msg"]
        problems.append(f"{path}: {reason}")
    return (
        "Invalid tool arguments; no operation ran. "
        + "; ".join(problems)
        + ". Correct the fields and retry."
    )


class ContractTool(StructuredTool):
    handle_tool_error: bool = True

    def _parse_input(self, tool_input: str | dict, tool_call_id: str | None):
        try:
            return super()._parse_input(tool_input, tool_call_id)
        except ValidationError as exc:
            # Only input validation can establish that the handler did not run.
            raise ToolException(validation_message(exc)) from exc

    @model_validator(mode="after")
    def close_arguments(self):
        if (
            isinstance(self.args_schema, type)
            and issubclass(self.args_schema, BaseModel)
            and self.args_schema.model_config.get("extra") != "forbid"
        ):
            self.args_schema = create_model(
                self.args_schema.__name__,
                __base__=self.args_schema,
                __config__=ConfigDict(extra="forbid"),
            )
        return self

    @property
    def tool_call_schema(self) -> dict[str, Any]:
        # LangChain's subset model drops aliases and model config. Use it only
        # to identify model-facing fields, retaining our original JSON schema.
        visible = super().tool_call_schema
        schema = self.args_schema.model_json_schema()
        fields = {
            field.alias or name
            for name, field in self.args_schema.model_fields.items()
            if name in visible.model_fields
        }
        schema["properties"] = {k: v for k, v in schema["properties"].items() if k in fields}
        if "required" in schema:
            schema["required"] = [key for key in schema["required"] if key in fields]
        return {
            **schema,
            "title": self.name,
            "description": self.description,
            "additionalProperties": False,
        }


def tool(function: Callable) -> ContractTool:
    """Decorate the project's typed sync/async tools using the same boundary."""
    kwargs = {"coroutine" if inspect.iscoroutinefunction(function) else "func": function}
    return ContractTool.from_function(**kwargs)
