"""Explicit public raw capabilities. New assistant tools are private until opted in."""

from copy import deepcopy
from functools import lru_cache

from jsonschema import Draft202012Validator

from wotbot.agent.tools import LOCAL_TOOLS, REGISTRY_TOOLS

PUBLIC_NAMES = frozenset(
    {
        "run_code",
        "create_web_interface",
        "get_current_time",
        "sources_search",
        "discover_external",
        "onboard_candidate",
        "register_external_source",
        "create_prompt_job",
        "create_analysis_job",
        "list_jobs",
        "create_record_prompt_job",
        "create_virtual_thing",
        "add_virtual_property",
        "add_virtual_action",
        "add_virtual_event",
        "activate_virtual_thing",
        "delete_virtual_thing",
        "emit_virtual_thing_event",
        "run_job_now",
        "delete_job",
        "registry_health",
        "things_list",
        "things_search",
        "things_sparql",
        "describe_rdf_schema",
        "things_get",
        "wot_get_property",
        "wot_get_action",
        "wot_get_event",
        "things_validate",
        "things_upsert",
        "things_delete",
        "wot_get_runtime_health",
        "wot_read_property",
        "wot_write_property",
        "wot_invoke_action",
        "wot_observe_property",
        "wot_subscribe_event",
        "wot_remove_subscription",
    }
)
PRIVATE_PARAMETERS = {
    "config",
    "thread_id",
    "created_from_thread_id",
    "owner",
    "owner_api_key_id",
    "subscription_namespace",
}


@lru_cache
def public_tools():
    tools = {
        tool.name: tool for tool in [*LOCAL_TOOLS, *REGISTRY_TOOLS] if tool.name in PUBLIC_NAMES
    }
    if tools.keys() != PUBLIC_NAMES:
        raise RuntimeError("The public tool catalog references missing tools")
    return tools


def argument_schema(name):
    contract = public_tools()[name].tool_call_schema
    schema = deepcopy(contract if isinstance(contract, dict) else contract.model_json_schema())
    for parameter in PRIVATE_PARAMETERS:
        schema.get("properties", {}).pop(parameter, None)
    schema["required"] = [
        key for key in schema.get("required", []) if key not in PRIVATE_PARAMETERS
    ]
    schema["additionalProperties"] = False
    return schema


def validate_arguments(name, arguments):
    if name not in public_tools():
        raise ValueError("Unknown raw tool")
    errors = list(Draft202012Validator(argument_schema(name)).iter_errors(arguments))
    if errors:
        # Never echo input values (which can contain credentials) into diagnostics.
        raise ValueError("Arguments do not match the published tool schema; no operation ran")
