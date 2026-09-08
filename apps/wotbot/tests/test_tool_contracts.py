import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ValidationError

from wotbot.agent.tools import LOCAL_TOOLS, REGISTRY_TOOLS
from wotbot.agent.tools.ask_job_user import ask_job_user
from wotbot.agent.tools.contracts import tool
from wotbot.agent.tools.external_discovery import discover_external, sources_search
from wotbot.agent.tools.job_scheduler import create_prompt_job, create_record_prompt_job
from wotbot.agent.tools.route_to import make_route_to_tool
from wotbot.agent.tools.run_code import run_code
from wotbot.agent.tools.submit_job_record import submit_job_record
from wotbot.agent.tools.wot_registry import things_search, things_sparql
from wotbot.search import set_active_search_service


@pytest.mark.parametrize(
    "registered",
    [*LOCAL_TOOLS, *REGISTRY_TOOLS, ask_job_user, submit_job_record, make_route_to_tool()],
    ids=lambda t: t.name,
)
def test_all_model_tools_advertise_closed_outer_arguments(registered):
    schema = convert_to_openai_tool(registered)["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert "tool_call_id" not in schema["properties"]
    if registered.name != "register_external_source":
        assert "config" not in schema["properties"]


def test_unknown_argument_is_rejected_before_execution_without_echoing_value():
    with patch(
        "wotbot.agent.tools.run_code._code_executor_client.execute", new=AsyncMock()
    ) as execute:
        result = asyncio.run(run_code.ainvoke({"code": "pass", "dryRun": "sensitive-sentinel"}))
    assert "dryRun: unknown argument" in result
    assert "no operation ran" in result
    assert "sensitive-sentinel" not in result
    execute.assert_not_awaited()


def test_nested_json_ld_and_schema_extension_fields_survive_validation():
    @tool
    def receive(document: dict[str, Any]) -> dict:
        """Receive a document."""
        return document

    document = {"@context": "urn:example", "x-custom": {"title": "keep", "@type": "Device"}}
    assert receive.invoke({"document": document}) == document
    assert receive.tool_call_schema["properties"]["document"]["additionalProperties"] is True


def test_handler_validation_failure_does_not_claim_no_operation_ran():
    applied = []

    class Response(BaseModel):
        count: int

    @tool
    def act() -> dict:
        """Apply an action and validate the response."""
        applied.append(True)
        return Response.model_validate({"count": "invalid"}).model_dump()

    with pytest.raises(ValidationError):
        act.invoke({})
    assert applied == [True]


def test_route_tool_keeps_injected_call_identity():
    route = make_route_to_tool()
    result = route.invoke(
        {"type": "tool_call", "name": "route_to", "id": "call-123", "args": {"intent": "analysis"}}
    )
    assert result.update["messages"][0].tool_call_id == "call-123"


@pytest.mark.parametrize(
    "registered,args",
    [
        (things_sparql, {"query": "ASK {}", "limit": 501}),
        (sources_search, {"query": "x" * 501}),
        (sources_search, {"limit": 0}),
        (discover_external, {"source_id": "unused", "limit": 26}),
    ],
)
def test_bounds_fail_before_contacting_services(registered, args):
    with (
        patch("wotbot.agent.tools.external_discovery.DiscoveryService") as discovery,
        patch("wotbot.agent.tools.wot_registry._rdf_client") as rdf,
    ):
        result = asyncio.run(registered.ainvoke(args))
    assert "no operation ran" in result
    discovery.assert_not_called()
    rdf.assert_not_called()


@pytest.mark.parametrize("registered", [create_prompt_job, create_record_prompt_job])
def test_job_interaction_mode_is_validated_before_contacting_services(registered):
    args = {
        "name": "test",
        "run_instructions": "test",
        "trigger_kind": "time",
        "interval_seconds": 60,
        "interaction_mode": "unsupported",
    }
    if registered is create_record_prompt_job:
        args["record_schema"] = {"type": "object"}
    with patch("wotbot.agent.tools.job_scheduler.get_active_job_service") as service:
        result = asyncio.run(registered.ainvoke(args))
    assert "interaction_mode" in result
    assert "no operation ran" in result
    service.assert_not_called()


def test_search_omits_index_prose_by_default_without_mutating_ranked_metadata():
    matches = [
        {
            "id": "urn:a",
            "title": "Meter",
            "description": "Energy meter",
            "score": 0.8,
            "tags": ["energy"],
            "summary": "verbose index prose " * 1000,
        }
    ]
    service = AsyncMock()
    service.search.return_value = matches
    set_active_search_service(service)
    try:
        compact = asyncio.run(things_search.ainvoke({"query": "energy"}))
        full = asyncio.run(things_search.ainvoke({"query": "energy", "include_summary": True}))
    finally:
        set_active_search_service(None)
    assert compact["items"] == [{k: v for k, v in matches[0].items() if k != "summary"}]
    assert full["items"] == matches
    assert "summary" in matches[0]
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 10
