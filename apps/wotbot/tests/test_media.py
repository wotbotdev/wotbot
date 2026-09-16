import asyncio
import unittest
from types import SimpleNamespace

from wotbot.core.settings import Settings
from wotbot.media import (
    MediaSessionRegistry,
    SnapshotNotifierRegistry,
)
from wotbot.media.livekit import (
    create_livekit_connection_details,
    dispatch_livekit_agent,
    livekit_room_name,
)


class MediaSessionRegistryTestCase(unittest.TestCase):
    def test_metadata_and_frame_counts_share_livekit_session(self) -> None:
        registry = MediaSessionRegistry()

        registry.set_metadata("livekit-a", thread_id="thread-a")
        registry.record_video(
            "livekit-a",
            width=640,
            height=360,
        )

        sessions = registry.snapshots()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], "livekit-a")
        self.assertEqual(sessions[0]["thread_id"], "thread-a")
        self.assertEqual(sessions[0]["video_frames"], 1)
        self.assertEqual(sessions[0]["video_width"], 640)
        self.assertEqual(sessions[0]["video_height"], 360)

    def test_latest_video_frame_only_returned_while_camera_active(self) -> None:
        registry = MediaSessionRegistry()
        registry.set_metadata("livekit-a", thread_id="thread-a")
        registry.store_video_frame_jpeg("livekit-a", jpeg_bytes=b"fresh-frame")

        snapshot = registry.latest_video_frame_for_thread("thread-a")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot[0], b"fresh-frame")

        self.assertIsNone(registry.latest_video_frame_for_thread("thread-a", max_age_seconds=0.0))

        registry.store_video_frame_jpeg("livekit-a", jpeg_bytes=b"fresh-frame")
        registry.close("livekit-a")
        self.assertIsNone(registry.latest_video_frame_for_thread("thread-a"))

    def test_clearing_video_frame_removes_stale_camera_context(self) -> None:
        registry = MediaSessionRegistry()
        registry.set_metadata("livekit-a", thread_id="thread-a")
        registry.store_video_frame_jpeg("livekit-a", jpeg_bytes=b"fresh-frame")

        registry.clear_video_frame("livekit-a")

        self.assertIsNone(registry.latest_video_frame_for_thread("thread-a"))

    def test_snapshot_notifier_invokes_and_unregisters_thread_callbacks(self) -> None:
        registry = SnapshotNotifierRegistry()
        events: list[str | None] = []

        unregister = registry.register("thread-a", lambda captured_at: events.append(captured_at))

        asyncio.run(registry.notify_snapshot_sent("thread-a", "frame-a"))
        unregister()
        asyncio.run(registry.notify_snapshot_sent("thread-a", "frame-b"))

        self.assertEqual(events, ["frame-a"])

    def test_livekit_room_names_are_safe_for_thread_ids(self) -> None:
        settings = Settings(livekit_room_prefix="my copilot")

        self.assertEqual(
            livekit_room_name(settings, "thread a/b", session_id="session 1"),
            "my-copilot-thread-a-b-session-1",
        )

    def test_livekit_connection_uses_public_browser_url_when_configured(self) -> None:
        class FakeApi:
            class VideoGrants:
                def __init__(self, **_kwargs):
                    pass

            class RoomAgentDispatch:
                def __init__(self, **_kwargs):
                    pass

            class RoomConfiguration:
                def __init__(self, **_kwargs):
                    pass

            class AccessToken:
                def __init__(self, *_args):
                    pass

                def with_ttl(self, _ttl):
                    return self

                def with_identity(self, _identity):
                    return self

                def with_name(self, _name):
                    return self

                def with_metadata(self, _metadata):
                    return self

                def with_attributes(self, _attributes):
                    return self

                def with_grants(self, _grants):
                    return self

                def with_room_config(self, _config):
                    raise AssertionError("agent dispatch should not be embedded in the join token")

                def to_jwt(self):
                    return "fake-token"

        settings = Settings(
            livekit_url="ws://livekit:7880",
            livekit_public_url="ws://localhost:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
        )

        details = create_livekit_connection_details(
            settings,
            thread_id="thread-a",
            api_module=FakeApi,
        )

        self.assertEqual(details.url, "ws://localhost:7880")
        self.assertRegex(details.room, r"^wotbot-thread-a-[0-9a-f-]{36}$")
        self.assertEqual(details.token, "fake-token")

    def test_livekit_connection_uses_unique_room_per_session(self) -> None:
        class FakeApi:
            class VideoGrants:
                def __init__(self, **_kwargs):
                    pass

            class AccessToken:
                def __init__(self, *_args):
                    pass

                def with_ttl(self, _ttl):
                    return self

                def with_identity(self, _identity):
                    return self

                def with_name(self, _name):
                    return self

                def with_metadata(self, _metadata):
                    return self

                def with_attributes(self, _attributes):
                    return self

                def with_grants(self, _grants):
                    return self

                def to_jwt(self):
                    return "fake-token"

        settings = Settings(
            livekit_url="ws://livekit:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
        )

        first = create_livekit_connection_details(
            settings,
            thread_id="thread-a",
            api_module=FakeApi,
        )
        second = create_livekit_connection_details(
            settings,
            thread_id="thread-a",
            api_module=FakeApi,
        )

        self.assertNotEqual(first.room, second.room)

    def test_livekit_agent_dispatch_waits_for_running_job(self) -> None:
        class FakeApi:
            class AgentDispatchState:
                DESCRIPTOR = SimpleNamespace(
                    fields_by_name={
                        "jobs": SimpleNamespace(
                            message_type=SimpleNamespace(
                                fields_by_name={
                                    "state": SimpleNamespace(
                                        message_type=SimpleNamespace(
                                            fields_by_name={
                                                "status": SimpleNamespace(
                                                    enum_type=SimpleNamespace(
                                                        values_by_number={
                                                            1: SimpleNamespace(name="JS_RUNNING"),
                                                        }
                                                    )
                                                )
                                            }
                                        )
                                    )
                                }
                            )
                        )
                    }
                )

            class CreateAgentDispatchRequest:
                def __init__(self, **kwargs):
                    self.__dict__.update(kwargs)

            class LiveKitAPI:
                def __init__(self, *_args):
                    self.agent_dispatch = SimpleNamespace(
                        create_dispatch=self.create_dispatch,
                        list_dispatch=self.list_dispatch,
                    )
                    self.created_requests = []
                    FakeApi.client = self

                async def create_dispatch(self, request):
                    self.created_requests.append(request)
                    return SimpleNamespace(id="AD_test")

                async def list_dispatch(self, _room):
                    return [
                        SimpleNamespace(
                            id="AD_test",
                            state=SimpleNamespace(
                                jobs=[
                                    SimpleNamespace(
                                        state=SimpleNamespace(status=1),
                                    )
                                ]
                            ),
                        )
                    ]

                async def aclose(self):
                    self.closed = True

        settings = Settings(
            livekit_url="ws://livekit:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
        )

        asyncio.run(
            dispatch_livekit_agent(
                settings,
                room="wotbot-thread-a-session-a",
                thread_id="thread-a",
                participant_identity="web-a",
                api_module=FakeApi,
                timeout_seconds=0.1,
                poll_interval_seconds=0.0,
            )
        )

        request = FakeApi.client.created_requests[0]
        self.assertEqual(request.room, "wotbot-thread-a-session-a")
        self.assertEqual(request.agent_name, "wotbot")
        self.assertTrue(FakeApi.client.closed)


if __name__ == "__main__":
    unittest.main()
