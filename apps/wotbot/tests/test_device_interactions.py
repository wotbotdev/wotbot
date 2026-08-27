import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wotbot.agent.device_interactions import (
    DEVICE_INTERACTION_SUMMARY_TYPE,
    make_device_interaction_summary_node,
)


class DeviceInteractionSummaryNodeTestCase(unittest.TestCase):
    def test_summary_node_appends_latest_turn_interactions(self) -> None:
        node = make_device_interaction_summary_node()
        messages = [
            HumanMessage(content="Old request"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "wot_read_property",
                        "args": {
                            "thing_id": "urn:old",
                            "property_name": "temperature",
                        },
                        "id": "old_call",
                    }
                ],
            ),
            ToolMessage(content='{"ok": true}', tool_call_id="old_call"),
            AIMessage(content="Old answer"),
            HumanMessage(content="Compare the production lines"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_code",
                        "args": {"code": "print('ok')"},
                        "id": "run_code_call",
                    },
                    {
                        "name": "wot_write_property",
                        "args": {
                            "thing_id": "urn:wotbot:thing:line-3",
                            "property_name": "targetSpeed",
                            "value": 40,
                        },
                        "id": "write_call",
                    },
                ],
            ),
            ToolMessage(
                content=json.dumps(
                    {
                        "stdout": "ok",
                        "wot_calls": [
                            {
                                "type": "read_property",
                                "thing_id": "urn:wotbot:thing:counter",
                                "name": "unitsProduced",
                                "ok": True,
                            }
                        ],
                    }
                ),
                tool_call_id="run_code_call",
            ),
            ToolMessage(content='{"ok": true}', tool_call_id="write_call"),
            AIMessage(content="Done."),
        ]

        result = node({"messages": messages})

        summary_message = result["messages"][0]
        summary = json.loads(summary_message.content)
        self.assertEqual(summary["type"], DEVICE_INTERACTION_SUMMARY_TYPE)
        self.assertEqual(
            summary["interactions"],
            [
                {
                    "affordanceName": "unitsProduced",
                    "ok": True,
                    "thingId": "urn:wotbot:thing:counter",
                    "type": "read_property",
                },
                {
                    "affordanceName": "targetSpeed",
                    "ok": True,
                    "thingId": "urn:wotbot:thing:line-3",
                    "type": "write_property",
                    "value": 40,
                },
            ],
        )

    def test_summary_node_skips_turns_without_device_interactions(self) -> None:
        node = make_device_interaction_summary_node()

        self.assertEqual(
            node(
                {
                    "messages": [
                        HumanMessage(content="hello"),
                        AIMessage(content="hi"),
                    ]
                }
            ),
            {},
        )


if __name__ == "__main__":
    unittest.main()
