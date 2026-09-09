"""Canonical authorization scopes for external API keys and service users."""

API_KEY_SCOPES: tuple[str, ...] = (
    "agent:invoke",
    "things:read",
    "things:write",
    "things:delete",
    "search:read",
    "credentials:read",
    "credentials:write",
    "sources:manage",
    "keys:manage",
)

VALID_SCOPES = frozenset(API_KEY_SCOPES)

SERVICE_SCOPES: dict[str, tuple[str, ...]] = {
    "wot_runtime": ("things:read",),
    "virtual_servient": ("things:read", "things:write", "things:delete"),
}
