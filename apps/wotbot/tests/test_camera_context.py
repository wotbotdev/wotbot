import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from wotbot.agent.camera_context import (
    CAMERA_CONTEXT_INSTRUCTION,
    attach_latest_camera_frame,
    clear_frozen_camera_frame,
)
from wotbot.media import media_sessions, snapshot_notifiers


class CameraContextTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_current_frame_without_mutating_graph_messages(self) -> None:
        thread_id = "thread-camera-context"
        session_id = "session-camera-context"
        notifications: list[str | None] = []
        original_human = HumanMessage(content="What color is my shirt?")
        messages = [SystemMessage(content="system"), original_human]

        unregister = snapshot_notifiers.register(thread_id, notifications.append)
        media_sessions.set_metadata(session_id, thread_id=thread_id)
        media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"camera-frame")

        try:
            result = await attach_latest_camera_frame(messages, thread_id=thread_id)
        finally:
            unregister()
            media_sessions.close(session_id)

        self.assertTrue(result.attached)
        self.assertEqual(notifications, [result.captured_at])
        self.assertEqual(original_human.content, "What color is my shirt?")
        self.assertIs(messages[1], original_human)

        prepared_system = result.messages[0]
        self.assertIsInstance(prepared_system, SystemMessage)
        self.assertIn(CAMERA_CONTEXT_INSTRUCTION.strip(), prepared_system.content)

        prepared_human = result.messages[1]
        self.assertIsInstance(prepared_human, HumanMessage)
        self.assertEqual(
            prepared_human.content[0], {"type": "text", "text": original_human.content}
        )
        self.assertEqual(prepared_human.content[1]["type"], "image_url")
        self.assertEqual(
            prepared_human.content[1]["image_url"]["url"],
            "data:image/jpeg;base64,Y2FtZXJhLWZyYW1l",
        )

    async def test_keeps_prompt_unchanged_when_no_current_frame_exists(self) -> None:
        messages = [SystemMessage(content="system"), HumanMessage(content="List my devices")]

        result = await attach_latest_camera_frame(messages, thread_id="thread-without-camera")

        self.assertFalse(result.attached)
        self.assertEqual(result.messages, messages)
        self.assertNotIn(CAMERA_CONTEXT_INSTRUCTION.strip(), result.messages[0].content)

    async def test_preserves_existing_multimodal_content_parts(self) -> None:
        thread_id = "thread-camera-existing-parts"
        session_id = "session-camera-existing-parts"
        existing = [{"type": "text", "text": "What is this?"}]
        message = HumanMessage(content=existing)
        media_sessions.set_metadata(session_id, thread_id=thread_id)
        media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"new-frame")

        try:
            result = await attach_latest_camera_frame([message], thread_id=thread_id)
        finally:
            media_sessions.close(session_id)

        self.assertTrue(result.attached)
        self.assertEqual(message.content, existing)
        self.assertEqual(result.messages[-1].content[0], existing[0])
        self.assertEqual(result.messages[-1].content[-1]["type"], "image_url")

    async def test_keeps_frame_on_latest_user_turn_during_tool_loop(self) -> None:
        thread_id = "thread-camera-tool-loop"
        session_id = "session-camera-tool-loop"
        human = HumanMessage(content="Turn this off")
        messages = [
            human,
            AIMessage(
                content="",
                tool_calls=[{"name": "things_search", "args": {}, "id": "call-1"}],
            ),
            ToolMessage(content="[]", tool_call_id="call-1"),
        ]
        media_sessions.set_metadata(session_id, thread_id=thread_id)
        media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"tool-loop-frame")

        try:
            result = await attach_latest_camera_frame(messages, thread_id=thread_id)
        finally:
            media_sessions.close(session_id)

        self.assertTrue(result.attached)
        self.assertIsInstance(result.messages[1], HumanMessage)
        self.assertEqual(result.messages[1].content[-1]["type"], "image_url")
        self.assertIs(result.messages[-1], messages[-1])
        self.assertEqual(human.content, "Turn this off")

    async def test_reuses_frozen_frame_and_notifies_once_during_tool_loop(self) -> None:
        thread_id = "thread-camera-frozen-tool-loop"
        session_id = "session-camera-frozen-tool-loop"
        notifications: list[str | None] = []
        human = HumanMessage(content="Turn this machine on", id="turn-1")
        media_sessions.set_metadata(session_id, thread_id=thread_id)
        media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"first-frame")
        unregister = snapshot_notifiers.register(thread_id, notifications.append)

        try:
            first = await attach_latest_camera_frame([human], thread_id=thread_id)
            media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"newer-frame")
            second = await attach_latest_camera_frame(
                [
                    human,
                    AIMessage(
                        content="",
                        tool_calls=[{"name": "things_search", "args": {}, "id": "call-1"}],
                    ),
                    ToolMessage(content="[]", tool_call_id="call-1"),
                ],
                thread_id=thread_id,
            )
        finally:
            unregister()
            clear_frozen_camera_frame(thread_id)
            media_sessions.close(session_id)

        first_prepared_human = next(
            message for message in reversed(first.messages) if isinstance(message, HumanMessage)
        )
        second_prepared_human = next(
            message for message in reversed(second.messages) if isinstance(message, HumanMessage)
        )
        first_url = first_prepared_human.content[-1]["image_url"]["url"]
        second_url = second_prepared_human.content[-1]["image_url"]["url"]
        self.assertEqual(first_url, "data:image/jpeg;base64,Zmlyc3QtZnJhbWU=")
        self.assertEqual(second_url, first_url)
        self.assertEqual(notifications, [first.captured_at])
        self.assertEqual(second.captured_at, first.captured_at)

    async def test_new_user_turn_freezes_newest_frame_and_notifies_again(self) -> None:
        thread_id = "thread-camera-new-turn"
        session_id = "session-camera-new-turn"
        notifications: list[str | None] = []
        first_human = HumanMessage(content="What is this?", id="turn-1")
        second_human = HumanMessage(content="And now?", id="turn-2")
        media_sessions.set_metadata(session_id, thread_id=thread_id)
        media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"first-frame")
        unregister = snapshot_notifiers.register(thread_id, notifications.append)

        try:
            first = await attach_latest_camera_frame([first_human], thread_id=thread_id)
            media_sessions.store_video_frame_jpeg(session_id, jpeg_bytes=b"second-frame")
            second = await attach_latest_camera_frame(
                [first_human, AIMessage(content="It is a conveyor."), second_human],
                thread_id=thread_id,
            )
        finally:
            unregister()
            clear_frozen_camera_frame(thread_id)
            media_sessions.close(session_id)

        first_prepared_human = next(
            message for message in reversed(first.messages) if isinstance(message, HumanMessage)
        )
        second_prepared_human = next(
            message for message in reversed(second.messages) if isinstance(message, HumanMessage)
        )
        first_url = first_prepared_human.content[-1]["image_url"]["url"]
        second_url = second_prepared_human.content[-1]["image_url"]["url"]
        self.assertEqual(first_url, "data:image/jpeg;base64,Zmlyc3QtZnJhbWU=")
        self.assertEqual(second_url, "data:image/jpeg;base64,c2Vjb25kLWZyYW1l")
        self.assertEqual(notifications, [first.captured_at, second.captured_at])


if __name__ == "__main__":
    unittest.main()
