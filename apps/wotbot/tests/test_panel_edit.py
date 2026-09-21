import asyncio
import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wotbot.panels.edit import PanelEditResult, _extract_edit_result, run_panel_edit


def _panel_tool_messages(content: str, call_id="call_1") -> list:
    return [
        HumanMessage(content="edit it"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "create_web_interface",
                    "args": {"html": "<div>new</div>", "title": "P"},
                    "id": call_id,
                }
            ],
        ),
        ToolMessage(tool_call_id=call_id, content=content),
    ]


class ExtractUpdatedPanelTestCase(unittest.TestCase):
    def test_run_panel_edit_formats_prompt_with_literal_binary_payload_shape(self) -> None:
        class FakeGraph:
            prompt = ""

            async def ainvoke(self, state, config):
                self.prompt = state["messages"][0].content
                self.config = config
                return {"messages": []}

        graph = FakeGraph()

        result = asyncio.run(
            run_panel_edit(
                graph=graph,
                checkpointer=None,
                html="<button>toggle</button>",
                capabilities=[],
                instruction="make the button blue",
            )
        )

        self.assertEqual(result, PanelEditResult())
        self.assertIn('{ kind: "binary", contentType, bodyBase64,', graph.prompt)
        self.assertEqual(graph.config["configurable"]["thread_id"][:11], "panel-edit-")

    def test_extracts_latest_html_and_capabilities(self) -> None:
        messages = _panel_tool_messages(
            json.dumps(
                {
                    "artifacts": [
                        {
                            "ref": "ui_1",
                            "kind": "web",
                            "filename": "x.html",
                            "capabilities": [{"thingId": "urn:lamp", "ops": ["writeProperty"]}],
                        }
                    ]
                }
            )
        )
        result = _extract_edit_result(messages)
        self.assertEqual(result.html, "<div>new</div>")
        self.assertEqual(result.capabilities, [{"thingId": "urn:lamp", "ops": ["writeProperty"]}])
        self.assertEqual(result.data, {})

    def test_edits_keep_snapshot_references_out_of_html_and_prompt_data(self):
        refs = {"areas": "panel-data-example"}

        class FakeGraph:
            async def ainvoke(self, state, config):
                self.prompt = state["messages"][0].content
                return {
                    "messages": _panel_tool_messages(
                        json.dumps(
                            {"artifacts": [{"kind": "web", "filename": "new.html", "data": refs}]}
                        )
                    )
                }

        graph = FakeGraph()
        result = asyncio.run(
            run_panel_edit(
                graph=graph,
                checkpointer=None,
                html='<script>panelData.read("areas")</script>',
                capabilities=[],
                data=refs,
                instruction="Change the colour",
            )
        )
        self.assertEqual(result, PanelEditResult(html="<div>new</div>", data=refs))
        self.assertIn(json.dumps(refs), graph.prompt)

    def test_returns_no_update_when_no_tool_call(self) -> None:
        messages = [
            HumanMessage(content="edit it"),
            AIMessage(content="I cannot do that"),
        ]
        self.assertEqual(_extract_edit_result(messages), PanelEditResult())

    def test_returns_no_update_when_tool_result_is_not_json(self) -> None:
        self.assertEqual(
            _extract_edit_result(_panel_tool_messages("plain text failure")), PanelEditResult()
        )

    def test_returns_no_update_when_artifact_is_not_mapping(self) -> None:
        self.assertEqual(
            _extract_edit_result(_panel_tool_messages(json.dumps({"artifacts": ["broken"]}))),
            PanelEditResult(),
        )

    def test_failed_edits_retain_the_last_real_attempt_when_a_later_call_is_blocked(self):
        failure = {
            "status": "failed",
            "report_id": "b" * 32,
            "previous_reports": ["a" * 32],
            "diagnostics": [{"kind": "javascript", "message": "Chart failed to load"}],
        }
        messages = _panel_tool_messages(
            json.dumps({"error": "No panel delivered", "browser_validation": failure})
        ) + _panel_tool_messages(
            json.dumps(
                {
                    "error": "Repair limit reached",
                    "browser_validation": {"status": "blocked", "report_id": "b" * 32},
                }
            ),
            call_id="blocked",
        )
        self.assertEqual(
            _extract_edit_result(messages), PanelEditResult(browser_validation=failure)
        )

    def test_repaired_edits_keep_warnings_for_the_successful_artifact(self):
        passed = {
            "status": "passed",
            "report_id": "b" * 32,
            "previous_reports": ["a" * 32],
            "visual_review": {"status": "warnings", "assessments": []},
        }
        messages = _panel_tool_messages(
            json.dumps({"error": "Failed", "browser_validation": {"status": "failed"}})
        ) + _panel_tool_messages(
            json.dumps(
                {
                    "artifacts": [{"kind": "web"}],
                    "browser_validation": passed,
                }
            ),
            call_id="repaired",
        )
        # Later tool failures must not replace the evidence for the edit being saved.
        messages += _panel_tool_messages(
            json.dumps({"error": "Failed", "browser_validation": {"status": "unavailable"}}),
            call_id="later",
        )
        self.assertEqual(
            _extract_edit_result(messages),
            PanelEditResult(html="<div>new</div>", browser_validation=passed),
        )

    def test_failed_verdict_never_applies_an_artifact(self):
        report = {"status": "failed", "report_id": "a" * 32}
        messages = _panel_tool_messages(
            json.dumps(
                {
                    "artifacts": [{"kind": "web"}],
                    "browser_validation": report,
                }
            )
        )
        self.assertEqual(_extract_edit_result(messages), PanelEditResult(browser_validation=report))


if __name__ == "__main__":
    unittest.main()
