import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wotbot.agent.builder import _agent_prompt_extra, build_graph


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


class BuilderVirtualToolsTestCase(unittest.TestCase):
    def test_agent_prompt_extra_is_normalized_as_a_static_section(self) -> None:
        self.assertEqual(_agent_prompt_extra("  "), "")
        self.assertEqual(
            _agent_prompt_extra("  first line\nsecond line  "),
            "\n\n## Deployment-specific guidance\nfirst line\nsecond line\n",
        )

    def test_virtual_branch_gets_dedicated_tools_and_catalog_mutation_is_excluded(
        self,
    ) -> None:
        captured: dict[str, list[str]] = {}

        def fake_make_control_node(_llm, tools, _max_tokens, **_kwargs):
            captured["control"] = [tool.name for tool in tools]
            return lambda state: state

        def fake_make_jobs_node(_llm, tools, _max_tokens, **_kwargs):
            captured["jobs"] = [tool.name for tool in tools]
            return lambda state: state

        def fake_make_virtual_things_node(_llm, tools, _max_tokens, **_kwargs):
            captured["virtual_things"] = [tool.name for tool in tools]
            return lambda state: state

        def fake_make_discovery_node(_llm, tools, _max_tokens, **_kwargs):
            captured["discovery"] = [tool.name for tool in tools]
            return lambda state: state

        with (
            patch("wotbot.agent.builder.make_router_node", return_value=lambda state: state),
            patch("wotbot.agent.builder.make_respond_node", return_value=lambda state: state),
            patch("wotbot.agent.builder.make_jobs_node", side_effect=fake_make_jobs_node),
            patch("wotbot.agent.builder.make_analysis_node", return_value=lambda state: state),
            patch("wotbot.agent.builder.make_control_node", side_effect=fake_make_control_node),
            patch("wotbot.agent.builder.make_discovery_node", side_effect=fake_make_discovery_node),
            patch(
                "wotbot.agent.builder.make_virtual_things_node",
                side_effect=fake_make_virtual_things_node,
            ),
            patch("wotbot.agent.builder.ToolNode", return_value=lambda state: state),
        ):
            build_graph(
                llm=SimpleNamespace(),
                registry_tools=[
                    _tool("things_search"),
                    _tool("things_get"),
                    _tool("things_upsert"),
                    _tool("things_delete"),
                    _tool("wot_get_runtime_health"),
                    _tool("wot_read_property"),
                    _tool("wot_invoke_action"),
                    _tool("wot_write_property"),
                    _tool("wot_subscribe_event"),
                    _tool("wot_remove_subscription"),
                ],
                local_tools=[
                    _tool("run_code"),
                    _tool("get_current_time"),
                    _tool("discover_external"),
                    _tool("onboard_candidate"),
                    _tool("create_prompt_job"),
                    _tool("create_analysis_job"),
                    _tool("create_virtual_thing"),
                    _tool("add_virtual_property"),
                    _tool("add_virtual_action"),
                    _tool("add_virtual_event"),
                    _tool("activate_virtual_thing"),
                    _tool("delete_virtual_thing"),
                    _tool("emit_virtual_thing_event"),
                ],
                max_tokens=1000,
            )

        self.assertNotIn("create_virtual_thing", captured["control"])
        self.assertNotIn("delete_virtual_thing", captured["control"])
        self.assertNotIn("create_virtual_thing", captured["jobs"])
        self.assertNotIn("delete_virtual_thing", captured["jobs"])
        self.assertEqual(
            captured["virtual_things"],
            [
                "things_search",
                "things_get",
                "wot_get_runtime_health",
                "wot_read_property",
                "wot_invoke_action",
                "wot_subscribe_event",
                "wot_remove_subscription",
                "get_current_time",
                "create_virtual_thing",
                "add_virtual_property",
                "add_virtual_action",
                "add_virtual_event",
                "activate_virtual_thing",
                "delete_virtual_thing",
                "emit_virtual_thing_event",
            ],
        )
        self.assertNotIn("things_upsert", captured["virtual_things"])
        self.assertNotIn("things_delete", captured["virtual_things"])
        self.assertNotIn("wot_write_property", captured["virtual_things"])
        self.assertIn("discover_external", captured["discovery"])
        self.assertIn("onboard_candidate", captured["discovery"])
        self.assertNotIn("run_code", captured["discovery"])

        # The action branches carry no Current Time block in their prompt, so the
        # only way they can learn the date is by calling for it. A branch without
        # this tool silently invents one instead.
        for branch in ("jobs", "virtual_things"):
            self.assertIn("get_current_time", captured[branch], branch)


if __name__ == "__main__":
    unittest.main()
