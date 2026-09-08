import importlib
import unittest

from wotbot.agent.tools.create_web_interface import create_web_interface

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

    def tearDown(self) -> None:
        web_interface_module._code_executor_client.store_web_artifact = self._original

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

    async def test_rejects_interface_without_valid_capabilities(self) -> None:
        result = await create_web_interface.ainvoke(
            {
                "html": "<div>hi</div>",
                "capabilities": [{"thing_id": "", "ops": ["writeProperty"]}],
            }
        )

        self.assertIn("error", result)
        self.assertNotIn("html", self._captured)


if __name__ == "__main__":
    unittest.main()
