from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool, tool
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from wotbot.agent.tools._config import thread_id_from_config
from wotbot.core.config import get_settings
from wotbot.discovery import DiscoveryService
from wotbot.discovery.errors import CredentialChallengeError
from wotbot.discovery.providers import PROVIDERS
from wotbot.discovery.providers.base import is_http_endpoint


def _thread_id(config: RunnableConfig) -> str:
    thread_id = thread_id_from_config(config)
    if not thread_id:
        raise ValueError("External discovery requires a conversation thread")
    return thread_id


class RegisterExternalSourceInput(BaseModel):
    """Keep the public `config` field without colliding with RunnableConfig injection."""

    model_config = ConfigDict(populate_by_name=True)

    url: str | None = None
    provider: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    source_config: dict[str, Any] | None = Field(default=None, alias="config")
    security_scheme: str | None = None


class _AliasedArgsStructuredTool(StructuredTool):
    @property
    def tool_call_schema(self) -> Any:
        """Preserve Pydantic aliases that LangChain's subset model otherwise drops."""

        return self.args_schema


@tool
async def sources_search(query: str = "", limit: int = 10) -> dict[str, Any]:
    """Find registered external sources by their safe metadata.

    Results include stable source ids and credential status, but never source
    configuration or secret values. Use this before discover_external.
    """
    return await DiscoveryService(get_settings()).search_sources(query=query, limit=limit)


@tool
async def discover_external(
    source_id: str,
    config: RunnableConfig,
    query: str = "",
    limit: int = 10,
) -> dict[str, Any]:
    """Search or browse exactly one registered external source for resource candidates."""
    service = DiscoveryService(get_settings())
    try:
        return await service.discover(
            source_id=source_id.strip(),
            query=query,
            limit=limit,
            thread_id=_thread_id(config),
        )
    except CredentialChallengeError as exc:
        return _credential_interrupt(exc)


@tool
async def onboard_candidate(
    candidate_id: str,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Create one durable resource Thing from a current-thread candidate.

    If suggested_sources is returned, the dataset links to services that need
    their own sources. Find an existing source with sources_search or pass a
    relevant published URL to register_external_source for user confirmation.
    Then discover and onboard from that source and inspect its Thing with
    things_get. A suggested link is not itself a usable affordance.
    """
    try:
        return await DiscoveryService(get_settings()).onboard(
            candidate_id=candidate_id.strip(), thread_id=_thread_id(config)
        )
    except CredentialChallengeError as exc:
        return _credential_interrupt(exc)


def _register_external_source(
    url: str | None = None,
    provider: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    source_config: dict[str, Any] | None = None,
    security_scheme: str | None = None,
) -> dict[str, Any]:
    """Ask the user to confirm registration of an external discovery source.

    This tool only creates an approval request. It never probes a private URL,
    writes a source, or accepts credential values itself.
    """
    if not url and not provider:
        raise ValueError("Source registration requires a URL or an explicit provider")
    source_url = url.strip() if isinstance(url, str) else ""
    if source_url and not is_http_endpoint(source_url):
        raise ValueError("Source registration requires a valid HTTP(S) URL")
    provider_name = provider.strip() if isinstance(provider, str) else ""
    safe_config = dict(source_config or {})
    if safe_config and not provider_name:
        raise ValueError("Explicit source configuration requires a provider")
    if provider_name:
        implementation = PROVIDERS.get(provider_name)
        if implementation is None:
            raise ValueError(f"Unknown discovery provider '{provider_name}'")
        unknown = set(safe_config) - implementation.config.fields
        if unknown:
            raise ValueError(
                "Source registration contains unsupported configuration fields: "
                + ", ".join(sorted(unknown))
            )
        for field in (
            *implementation.config.url_fields,
            *implementation.config.optional_url_fields,
        ):
            value = str(safe_config.get(field) or "").strip()
            if value and not is_http_endpoint(value):
                raise ValueError(f"Source registration requires a valid '{field}' URL")
    scheme = security_scheme.strip().lower() if isinstance(security_scheme, str) else ""
    if scheme and scheme not in {"nosec", "apikey", "bearer", "basic", "oauth2"}:
        raise ValueError("Unsupported source security scheme")
    draft = {
        **({"url": source_url} if source_url else {}),
        **({"provider": provider_name} if provider_name else {}),
        **({"title": title.strip()} if isinstance(title, str) and title.strip() else {}),
        **(
            {"description": description.strip()}
            if isinstance(description, str) and description.strip()
            else {}
        ),
        "tags": [str(tag)[:100] for tag in (tags or []) if str(tag).strip()][:20],
        "config": safe_config,
        **({"security_scheme": scheme} if scheme else {}),
    }
    answer = interrupt({"kind": "source_registration", "draft": draft})
    if not isinstance(answer, dict) or answer.get("status") != "source_registered":
        return {"status": "source_registration_cancelled"}
    source_id = answer.get("source_id")
    return {
        "status": "source_registered",
        **({"source_id": source_id} if isinstance(source_id, str) else {}),
    }


register_external_source = _AliasedArgsStructuredTool.from_function(
    func=_register_external_source,
    name="register_external_source",
    args_schema=RegisterExternalSourceInput,
)


def _credential_interrupt(exc: CredentialChallengeError) -> dict[str, Any]:
    challenge = exc.public()
    answer = interrupt({"kind": "credential", **challenge})
    if not isinstance(answer, dict) or answer.get("status") != "credential_saved":
        return {**challenge, "status": "credential_cancelled"}
    return {**challenge, "retry_exhausted": True}
