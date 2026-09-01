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
    )
}

__all__ = [
    "PROVIDERS",
    "DcatProvider",
    "DiscoveryProvider",
    "EdcV3Provider",
    "OpenApiProvider",
    "ToolHiveProvider",
    "TxBootstrapProvider",
    "UdataProvider",
    "edr_ttl",
    "prepare_search_intent",
    "resolve_private_toolhive_source",
    "resolve_public_source",
]
