import unittest

from wotbot.panels.render import wrap_panel_document
from wotbot.panels.service import _clean_capabilities


class WrapPanelDocumentTestCase(unittest.TestCase):
    def test_inlines_bridge_without_external_src(self) -> None:
        doc = wrap_panel_document("<div>hi</div>", "Operations panel")
        self.assertIn("window.wot", doc)  # bridge inlined
        self.assertNotIn('src="/wot-bridge.js"', doc)
        self.assertIn("<div>hi</div>", doc)

    def test_escapes_title(self) -> None:
        doc = wrap_panel_document("<div></div>", "Conveyor <panel>")
        self.assertIn("Conveyor &lt;panel&gt;", doc)
        self.assertNotIn("<title>Conveyor <panel></title>", doc)


class CleanCapabilitiesTestCase(unittest.TestCase):
    def test_keeps_valid_and_drops_invalid(self) -> None:
        cleaned = _clean_capabilities(
            [
                {
                    "thingId": "urn:conveyor",
                    "affordances": ["speed", 5],
                    "ops": ["writeProperty", 1, "bogus"],
                },
                {"thingId": "", "ops": ["readProperty"]},  # no thing
                {"thingId": "urn:x", "ops": []},  # no valid ops
                "not a dict",
            ]
        )
        self.assertEqual(
            cleaned,
            [
                {
                    "thingId": "urn:conveyor",
                    "affordances": ["speed"],
                    "ops": ["writeProperty"],
                },
            ],
        )

    def test_non_list_returns_empty(self) -> None:
        self.assertEqual(_clean_capabilities("nope"), [])


if __name__ == "__main__":
    unittest.main()
