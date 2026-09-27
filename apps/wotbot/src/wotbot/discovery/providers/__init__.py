import importlib
import os

from wotbot.discovery.providers.base import DiscoveryProvider
from wotbot.discovery.providers.dcat import DcatProvider
from wotbot.discovery.providers.edc_v3 import EdcV3Provider, edr_ttl
from wotbot.discovery.providers.openapi import OpenApiProvider
from wotbot.discovery.providers.public import (
    resolve_private_toolhive_source,
    resolve_public_source,
)
from wotbot.discovery.providers.toolhive import ToolHiveProvider
from wotbot.discovery.providers.tx_bootstrap import TxBootstrapProvider
from wotbot.discovery.providers.udata import UdataProvider
from wotbot.discovery.providers.wot_tdd import WotTddProvider
from wotbot.discovery.search import prepare_search_intent

PROVIDERS: dict[str, DiscoveryProvider] = {
    provider.name: provider
    for provider in (
        ToolHiveProvider(),
        UdataProvider(),
        DcatProvider(),
        EdcV3Provider(),
        TxBootstrapProvider(),
        OpenApiProvider(),
        WotTddProvider(),
    )
}


def _load_external_providers() -> None:
    """Load deployment-owned providers named by module:attribute.

    The modules must already be on PYTHONPATH. Discovery source configuration
    cannot import code; only an operator-controlled environment variable can.
    """

    for entry in os.environ.get("WOTBOT_DISCOVERY_PROVIDER_MODULES", "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        module_name, separator, attribute = entry.partition(":")
        if not separator or not module_name or not attribute:
            raise ValueError(f"Invalid discovery provider module: {entry!r}")
        provider_type = getattr(importlib.import_module(module_name), attribute)
        provider = provider_type() if isinstance(provider_type, type) else provider_type
        if not isinstance(provider, DiscoveryProvider):
            raise TypeError(f"External discovery provider {entry!r} is invalid")
        if provider.name in PROVIDERS:
            raise ValueError(f"Duplicate discovery provider: {provider.name}")
        PROVIDERS[provider.name] = provider


_load_external_providers()

__all__ = [
    "PROVIDERS",
    "DcatProvider",
    "DiscoveryProvider",
    "EdcV3Provider",
    "OpenApiProvider",
    "ToolHiveProvider",
    "TxBootstrapProvider",
    "UdataProvider",
    "WotTddProvider",
    "edr_ttl",
    "prepare_search_intent",
    "resolve_private_toolhive_source",
    "resolve_public_source",
]
