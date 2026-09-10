import json
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from wotbot.agent.device_interactions import DEVICE_INTERACTION_SUMMARY_TYPE
from wotbot.agent_api.runtime import final_reply_text


def _summary_message(message_id: str = "summary") -> AIMessage:
    return AIMessage(
        id=message_id,
        content=json.dumps(
            {
                "type": DEVICE_INTERACTION_SUMMARY_TYPE,
                "interactions": [
                    {
                        "affordanceName": "download_prices",
                        "ok": True,
                        "thingId": "urn:wotbot:external:udata:prices",
                        "type": "invoke_action",
                    }
                ],
            }
        ),
    )


class FinalReplyTextTestCase(unittest.TestCase):
    def test_skips_trailing_device_interaction_summary(self) -> None:
        messages = [
            HumanMessage(id="user", content="create a nice little plot"),
            AIMessage(
                id="call", content="", tool_calls=[{"id": "t1", "name": "run_code", "args": {}}]
            ),
            ToolMessage(id="tool", tool_call_id="t1", content="{}"),
            AIMessage(id="answer", content="Here is the plot of day-ahead prices."),
            _summary_message(),
        ]

        self.assertEqual(final_reply_text(messages, set()), "Here is the plot of day-ahead prices.")

    def test_returns_latest_answer_without_summary(self) -> None:
        messages = [
            HumanMessage(id="user", content="list my things"),
            AIMessage(id="answer", content="There is 1 Thing available."),
        ]

        self.assertEqual(final_reply_text(messages, set()), "There is 1 Thing available.")

    def test_ignores_messages_already_seen(self) -> None:
        messages = [
            AIMessage(id="old", content="Previous turn answer."),
            AIMessage(id="new", content="This turn answer."),
            _summary_message(),
        ]

        self.assertEqual(final_reply_text(messages, {"new"}), "Previous turn answer.")

    def test_returns_empty_when_only_a_summary_remains(self) -> None:
        self.assertEqual(final_reply_text([_summary_message()], set()), "")


if __name__ == "__main__":
    unittest.main()
