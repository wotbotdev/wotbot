import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import wotbot.api.main as wotbot_app
from wotbot.core.settings import Settings


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


class AgentAppRoutesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._original_lifespan_context = wotbot_app.app.router.lifespan_context
        wotbot_app.app.router.lifespan_context = _noop_lifespan

    @classmethod
    def tearDownClass(cls) -> None:
        wotbot_app.app.router.lifespan_context = cls._original_lifespan_context

    def setUp(self) -> None:
        self._original_app_state = dict(wotbot_app.app.state._state)
        self.client = TestClient(wotbot_app.app)

    def tearDown(self) -> None:
        self.client.close()
        wotbot_app.app.state._state.clear()
        wotbot_app.app.state._state.update(self._original_app_state)

    def _set_settings(self, settings: Settings) -> None:
        wotbot_app.app.state.agent_settings = settings

    def test_health_route(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_ag_ui_routes_are_removed(self) -> None:
        self.assertEqual(self.client.get("/ag-ui/health").status_code, 404)
        self.assertEqual(self.client.post("/ag-ui", json={}).status_code, 404)

    def test_lifespan_exposes_compiled_graph(self) -> None:
        class FakeGraph:
            def __init__(self) -> None:
                self.configs: list[dict] = []

            def with_config(self, **config):
                self.configs.append(config)
                return self

        class FakeSaverContext:
            async def __aenter__(self):
                return fake_saver

            async def __aexit__(self, *_args):
                return False

        fake_saver = object()
        fake_graph = FakeGraph()
        fake_settings = Settings(agent_handoff_enabled=True, a2a_enabled=False)
        fake_job_service = AsyncMock()

        async def exercise() -> None:
            with (
                patch.object(wotbot_app, "AgentSettings", return_value=fake_settings),
                patch.object(wotbot_app, "init_db"),
                patch.object(wotbot_app, "get_registry_settings"),
                patch.object(wotbot_app, "get_connection_pool", return_value=object()),
                patch.object(wotbot_app, "start_backend_runtime", AsyncMock()),
                patch.object(wotbot_app, "shutdown_backend_runtime", AsyncMock()),
                patch.object(wotbot_app, "make_llm", return_value=object()),
                patch.object(
                    wotbot_app,
                    "_checkpoint_saver_context",
                    return_value=FakeSaverContext(),
                ),
                patch.object(wotbot_app, "build_graph", return_value=fake_graph) as build_graph,
                patch.object(wotbot_app, "JobService", return_value=fake_job_service),
                patch.object(wotbot_app, "set_active_job_service"),
            ):
                async with wotbot_app.lifespan(wotbot_app.app):
                    self.assertIs(wotbot_app.app.state.graph, fake_graph)
                    self.assertIs(wotbot_app.app.state.checkpointer, fake_saver)
                    self.assertIs(wotbot_app.app.state.agent_settings, fake_settings)
                    self.assertIs(build_graph.call_args.kwargs["checkpointer"], fake_saver)
                    self.assertTrue(build_graph.call_args.kwargs["handoff_enabled"])
                    fake_job_service.start.assert_awaited_once()
                fake_job_service.stop.assert_awaited_once()

        asyncio.run(exercise())

    def test_configure_logging_forces_uvicorn_logging_setup(self) -> None:
        loggers: dict[str | None, Mock] = {}

        def fake_get_logger(name: str | None = None) -> Mock:
            logger = Mock()
            loggers[name] = logger
            return logger

        with (
            patch.object(wotbot_app.logging, "basicConfig") as basic_config,
            patch.object(wotbot_app.logging, "getLogger", side_effect=fake_get_logger),
        ):
            wotbot_app.configure_logging("DEBUG")

        basic_config.assert_called_once_with(
            level="DEBUG",
            format=wotbot_app.LOG_FORMAT,
            force=True,
        )
        for logger_name in ("wotbot", "uvicorn", "uvicorn.error", "uvicorn.access"):
            loggers[logger_name].setLevel.assert_called_once_with("DEBUG")

    def test_livekit_token_endpoint_reports_disabled_when_unconfigured(self) -> None:
        self._set_settings(Settings(internal_api_key="test-internal-key"))

        response = self.client.post(
            "/media/livekit/token",
            json={"threadId": "thread-a"},
            headers={"Authorization": "Bearer test-internal-key"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False})

    def test_legacy_media_endpoints_are_removed(self) -> None:
        self._set_settings(Settings(internal_api_key="test-internal-key"))

        rtc_response = self.client.get(
            "/media/rtc-configuration",
            headers={"Authorization": "Bearer test-internal-key"},
        )
        offer_response = self.client.post(
            "/media/webrtc/offer",
            json={},
            headers={"Authorization": "Bearer test-internal-key"},
        )

        self.assertEqual(rtc_response.status_code, 404)
        self.assertEqual(offer_response.status_code, 404)

    def test_livekit_token_endpoint_creates_connection_details(self) -> None:
        class FakeLiveKitDetails:
            def as_response(self):
                return {
                    "enabled": True,
                    "url": "ws://livekit:7880",
                    "token": "token-a",
                    "room": "wotbot-thread-a",
                    "participantIdentity": "web-a",
                    "agentName": "wotbot",
                    "expiresInSeconds": 600,
                }

        settings = Settings(
            internal_api_key="test-internal-key",
            livekit_url="ws://livekit:7880",
            livekit_public_url="ws://localhost:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
        )
        self._set_settings(settings)

        with patch(
            "wotbot.media.routes.create_livekit_connection_details",
            return_value=FakeLiveKitDetails(),
        ) as create_details:
            response = self.client.post(
                "/media/livekit/token",
                json={"threadId": "thread-a"},
                headers={"Authorization": "Bearer test-internal-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], "token-a")
        create_details.assert_called_once_with(settings, thread_id="thread-a")

    def test_livekit_dispatch_endpoint_dispatches_agent(self) -> None:
        settings = Settings(
            internal_api_key="test-internal-key",
            livekit_url="ws://livekit:7880",
            livekit_api_key="devkey",
            livekit_api_secret="secret",
        )
        self._set_settings(settings)

        with patch("wotbot.media.routes.dispatch_livekit_agent") as dispatch_agent:
            response = self.client.post(
                "/media/livekit/dispatch",
                json={
                    "room": "wotbot-thread-a-session-a",
                    "threadId": "thread-a",
                    "participantIdentity": "web-a",
                },
                headers={"Authorization": "Bearer test-internal-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": True, "dispatched": True})
        dispatch_agent.assert_awaited_once_with(
            settings,
            room="wotbot-thread-a-session-a",
            thread_id="thread-a",
            participant_identity="web-a",
        )

    def test_delete_thread_removes_langgraph_checkpoint_rows(self) -> None:
        class FakeCheckpointer:
            def __init__(self) -> None:
                self.deleted_threads: list[str] = []

            async def adelete_thread(self, thread_id: str) -> None:
                self.deleted_threads.append(thread_id)

        fake_checkpointer = FakeCheckpointer()
        self._set_settings(Settings(internal_api_key="test-internal-key"))
        wotbot_app.app.state.checkpointer = fake_checkpointer

        with (
            patch("wotbot.threads.routes.get_thread", return_value={"kind": "chat"}),
            patch("wotbot.threads.routes.delete_thread_metadata", return_value=True),
        ):
            response = self.client.delete(
                "/threads/thread-a",
                headers={"Authorization": "Bearer test-internal-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"ok": True, "thread_id": "thread-a", "deleted": True},
        )
        self.assertEqual(fake_checkpointer.deleted_threads, ["thread-a"])


if __name__ == "__main__":
    unittest.main()
