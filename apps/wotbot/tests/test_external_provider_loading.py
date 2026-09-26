"""Deployment-owned discovery provider loading stays explicit and bounded."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

from wotbot.discovery.providers import PROVIDERS, _load_external_providers
from wotbot.discovery.providers.dcat import DcatProvider


class DemoProvider(DcatProvider):
    name = "deployment-test"


class ExternalProviderLoadingTest(unittest.TestCase):
    def test_loads_one_operator_configured_provider(self) -> None:
        module = types.ModuleType("deployment_test_provider")
        module.DemoProvider = DemoProvider
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch.dict(
                os.environ,
                {"WOTBOT_DISCOVERY_PROVIDER_MODULES": "deployment_test_provider:DemoProvider"},
            ),
        ):
            try:
                _load_external_providers()
                self.assertIsInstance(PROVIDERS["deployment-test"], DemoProvider)
            finally:
                PROVIDERS.pop("deployment-test", None)

    def test_rejects_duplicate_provider_name(self) -> None:
        module = types.ModuleType("deployment_test_provider")
        module.DemoProvider = DcatProvider
        with (
            patch.dict(sys.modules, {module.__name__: module}),
            patch.dict(
                os.environ,
                {"WOTBOT_DISCOVERY_PROVIDER_MODULES": "deployment_test_provider:DemoProvider"},
            ),
            self.assertRaisesRegex(ValueError, "Duplicate discovery provider"),
        ):
            _load_external_providers()

    def test_rejects_invalid_module_reference(self) -> None:
        with (
            patch.dict(os.environ, {"WOTBOT_DISCOVERY_PROVIDER_MODULES": "bare_module"}),
            self.assertRaisesRegex(ValueError, "Invalid discovery provider module"),
        ):
            _load_external_providers()
