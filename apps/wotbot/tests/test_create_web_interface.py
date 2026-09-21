import importlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from wotbot.agent.tools.create_web_interface import create_web_interface
from wotbot.panels.evidence import BrowserValidation

# The tools package re-exports the tool under the same dotted path, shadowing the
# submodule attribute, so reach the real module object via sys.modules.
web_interface_module = importlib.import_module("wotbot.agent.tools.create_web_interface")


class CreateWebInterfaceToolTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._captured: dict[str, str] = {}

        async def fake_store(*, html: str) -> str:
            self._captured["html"] = html
            return "generated.html"

        self._original = web_interface_module._code_executor_client.store_web_artifact
        web_interface_module._code_executor_client.store_web_artifact = fake_store
        self._original_dependency_validator = web_interface_module.validate_external_dependencies

        async def dependencies_ok(_html: str) -> list[str]:
            return []

        web_interface_module.validate_external_dependencies = dependencies_ok
        self._browser = AsyncMock(
            return_value=BrowserValidation(
                status="passed", checks={"status": "passed", "checks": []}
            )
        )
        browser_patch = patch.object(web_interface_module, "validate_in_browser", self._browser)
        browser_patch.start()
        self.addCleanup(browser_patch.stop)

        async def saved_report(**kwargs):
            return {
                **kwargs["report"],
                "report_id": "a" * 32,
                "has_screenshot": bool(kwargs["screenshot_base64"]),
            }

        self._reports = AsyncMock(side_effect=saved_report)
        report_patch = patch.object(web_interface_module, "save_report", self._reports)
        report_patch.start()
        self.addCleanup(report_patch.stop)

    def tearDown(self) -> None:
        web_interface_module._code_executor_client.store_web_artifact = self._original
        web_interface_module.validate_external_dependencies = self._original_dependency_validator

    async def test_advisory_warning_or_outage_does_not_withhold_a_browser_pass(self):
        for status in ("warnings", "unavailable"):
            with patch.object(
                web_interface_module,
                "review_visuals",
                AsyncMock(return_value={"status": status, "mode": "advisory", "assessments": []}),
            ):
                result = await create_web_interface.ainvoke(
                    {
                        "html": "<h1>Ready</h1>",
                        "capabilities": [
                            {
                                "thing_id": "urn:lamp",
                                "affordances": ["power"],
                                "ops": ["readProperty"],
                            }
                        ],
                    }
                )
                self.assertTrue(result["artifacts"])
                self.assertEqual(result["browser_validation"]["status"], "passed")
                self.assertEqual(result["browser_validation"]["visual_review"]["status"], status)

    async def test_wraps_html_and_normalizes_capabilities(self) -> None:
        result = await create_web_interface.ainvoke(
            {
                "html": "<div>hi</div>",
                "title": "Lamp <panel>",
                "capabilities": [
                    {
                        "thing_id": "urn:lamp",
                        "affordances": ["brightness", ""],
                        "ops": ["writeProperty", "observeProperty", "bogus"],
                    }
                ],
            }
        )

        artifacts = result["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(result["browser_validation"]["status"], "passed")
        self._browser.assert_awaited_once()
        self.assertEqual(
            self._browser.call_args.kwargs["capabilities"], artifacts[0]["capabilities"]
        )
        self.assertEqual(artifacts[0]["kind"], "web")
        self.assertEqual(artifacts[0]["filename"], "generated.html")
        self.assertEqual(
            artifacts[0]["capabilities"],
            [
                {
                    "thingId": "urn:lamp",
                    "affordances": ["brightness"],
                    "ops": ["writeProperty", "observeProperty"],
                }
            ],
        )

        wrapped = self._captured["html"]
        self.assertIn("window.wot", wrapped)  # bridge inlined
        self.assertNotIn('src="/wot-bridge.js"', wrapped)
        self.assertIn("<div>hi</div>", wrapped)
        # Title is HTML-escaped to avoid breaking out of the <title> element.
        self.assertIn("Lamp &lt;panel&gt;", wrapped)

    async def test_rejects_a_panel_whose_script_cannot_parse(self) -> None:
        # The failure this exists for: broken code used to be stored and only
        # showed up as a console SyntaxError nobody was watching.
        result = await create_web_interface.ainvoke(
            {
                "html": "<div></div><script>foo(bar, baz\nqux();</script>",
                "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
            }
        )

        self.assertIn("error", result)
        self.assertIn("does not parse", result["error"])
        self.assertNotIn("html", self._captured)

    async def test_rejects_a_call_no_declared_capability_permits(self) -> None:
        result = await create_web_interface.ainvoke(
            {
                "html": "<script>wot.writeProperty('urn:other', 'state', 1);</script>",
                "capabilities": [
                    {
                        "thing_id": "urn:lamp",
                        "affordances": ["state"],
                        "ops": ["readProperty"],
                    }
                ],
            }
        )

        self.assertIn("error", result)
        self.assertIn("urn:other", result["error"])
        self.assertNotIn("html", self._captured)

    async def test_rejects_an_unverifiable_integrity_attribute(self) -> None:
        result = await create_web_interface.ainvoke(
            {
                "html": (
                    '<link rel="stylesheet" href="https://unpkg.com/leaflet.css" '
                    'integrity="sha256-invented">'
                ),
                "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
            }
        )

        self.assertIn("error", result)
        self.assertIn("Remove every integrity attribute", result["error"])
        self.assertNotIn("html", self._captured)

    async def test_rejects_unavailable_external_dependency(self) -> None:
        async def dependency_fails(_html: str) -> list[str]:
            return ["The external script URL is unavailable (HTTP 404)."]

        web_interface_module.validate_external_dependencies = dependency_fails
        result = await create_web_interface.ainvoke(
            {
                "html": '<script src="https://cdn.jsdelivr.net/npm/example/missing.js"></script>',
                "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
            }
        )

        self.assertIn("error", result)
        self.assertIn("HTTP 404", result["error"])
        self.assertNotIn("html", self._captured)

    async def test_rejects_interface_without_valid_capabilities(self) -> None:
        result = await create_web_interface.ainvoke(
            {
                "html": "<div>hi</div>",
                "capabilities": [{"thing_id": "", "ops": ["writeProperty"]}],
            }
        )

        self.assertIn("error", result)
        self.assertNotIn("html", self._captured)

    async def test_data_only_panel_delivers_large_exact_data_without_tool_result_payload(self):
        payload = json.dumps({"coordinates": [[7.12345678912345, 49.98765432198765]] * 500})
        refs = {"areas": "panel-data-example"}
        with patch.object(
            web_interface_module,
            "resolve_data",
            AsyncMock(
                return_value=(
                    refs,
                    {"areas": payload},
                    {"areas": {"id": refs["areas"], "size_bytes": len(payload)}},
                )
            ),
        ):
            result = await create_web_interface.ainvoke(
                {
                    "html": '<script>const areas = panelData.read("areas");</script>',
                    "data": {"areas": "file-original.geojson"},
                }
            )
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["capabilities"], [])
        self.assertEqual(artifact["data"], refs)
        self.assertNotIn("7.123456789", json.dumps(result))
        self.assertIn("7.12345678912345", self._captured["html"])
        self.assertEqual(result["browser_validation"]["status"], "passed")
        self.assertEqual(self._browser.call_args.args[0], self._captured["html"])

    async def test_browser_failure_or_inconclusive_result_never_stores_a_panel(self):
        for status in ("failed", "inconclusive", "unavailable"):
            with self.subTest(status=status):
                self._browser.return_value = BrowserValidation(
                    status=status, diagnostics=[{"kind": "test", "message": "Map failed"}]
                )
                with patch.object(
                    web_interface_module,
                    "resolve_data",
                    AsyncMock(return_value=({"areas": "panel-data-id"}, {"areas": "{}"}, {})),
                ):
                    result = await create_web_interface.ainvoke(
                        {"html": "<div>Map</div>", "data": {"areas": "file-id"}}
                    )
                self.assertIn("error", result)
                self.assertEqual(result["browser_validation"]["status"], status)
                self.assertNotIn("artifacts", result)
                self.assertNotIn("html", self._captured)

    async def test_data_does_not_grant_thing_access(self):
        result = await create_web_interface.ainvoke(
            {
                "html": '<script>wot.writeProperty("lamp", "power", true)</script>',
                "data": {"areas": "file-original.geojson"},
            }
        )
        self.assertIn("error", result)
        self.assertNotIn("html", self._captured)

    async def test_live_and_mixed_panels_must_pass_browser_validation(self):
        for data in ({}, {"areas": "file-id"}):
            for status in ("failed", "inconclusive", "unavailable"):
                with self.subTest(data=data, status=status):
                    self._browser.return_value = BrowserValidation(status=status)
                    with patch.object(
                        web_interface_module,
                        "resolve_data",
                        AsyncMock(return_value=(data, {"areas": "{}"} if data else {}, {})),
                    ):
                        result = await create_web_interface.ainvoke(
                            {
                                "html": "<h1>Live panel</h1>",
                                "data": data,
                                "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
                            }
                        )
                    self.assertIn("error", result)
                    self.assertEqual(result["browser_validation"]["status"], status)
                    self.assertNotIn("artifacts", result)
                    self.assertNotIn("html", self._captured)

    async def test_edit_preserves_attachments_when_model_omits_data(self):
        refs = {"areas": "panel-data-existing"}
        resolver = AsyncMock(return_value=(refs, {"areas": '{"value":42}'}, {}))
        with patch.object(web_interface_module, "resolve_data", resolver):
            result = await create_web_interface.ainvoke(
                {"html": '<script>panelData.read("areas")</script>'},
                config={"configurable": {"panel_data": refs}},
            )
        self.assertEqual(result["artifacts"][0]["data"], refs)
        self.assertEqual(resolver.call_args.args[0], refs)

    async def test_explicit_empty_data_clears_edit_attachments(self):
        resolver = AsyncMock(return_value=({}, {}, {}))
        with patch.object(web_interface_module, "resolve_data", resolver):
            result = await create_web_interface.ainvoke(
                {
                    "html": "<div>Live panel</div>",
                    "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
                    "data": {},
                },
                config={"configurable": {"panel_data": {"areas": "panel-data-existing"}}},
            )
        self.assertEqual(result["artifacts"][0]["data"], {})
        self.assertEqual(resolver.call_args.args[0], {})

    async def test_missing_attachment_does_not_publish_a_panel(self):
        with patch.object(
            web_interface_module,
            "resolve_data",
            AsyncMock(side_effect=ValueError("Attachment expired")),
        ):
            result = await create_web_interface.ainvoke(
                {"html": "<div>Map</div>", "data": {"areas": "panel-data-expired"}}
            )
        self.assertIn("expired", result["error"])
        self.assertNotIn("html", self._captured)

    async def test_report_storage_failure_withholds_delivery_and_stops_retries(self):
        self._reports.side_effect = OSError("storage unavailable")
        result = await create_web_interface.ainvoke(
            {
                "html": "<h1>Map</h1>",
                "capabilities": [{"thing_id": "urn:lamp", "ops": ["readProperty"]}],
            }
        )
        self.assertNotIn("artifacts", result)
        self.assertEqual(result["browser_validation"]["status"], "unavailable")
        self.assertFalse(result["browser_validation"]["retry_allowed"])
        self.assertEqual(result["browser_validation"]["diagnostics"][0]["kind"], "report_storage")

    async def test_executor_connection_dropped_mid_response_stops_retries_with_a_report(self):
        with patch.object(
            web_interface_module,
            "resolve_data",
            AsyncMock(side_effect=httpx.RemoteProtocolError("Server disconnected")),
        ):
            result = await create_web_interface.ainvoke(
                {"html": "<div>Map</div>", "data": {"areas": "file-map.json"}}
            )
        self.assertNotIn("artifacts", result)
        self.assertEqual(result["browser_validation"]["status"], "unavailable")
        self.assertFalse(result["browser_validation"]["retry_allowed"])
        self._reports.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
