"""A redacted, read-only view of the settings this process actually resolved.

The point of this module is to answer "what is this deployment *actually*
configured with", which is otherwise only knowable by shelling into a container
and reading the environment. It exists because the same environment variables
are read independently by more than one service -- the chat UI parses
``REASONING_EFFORT_*`` out of its own process environment while the agent parses
them here -- so the two can silently disagree, and nothing surfaces it.

Redaction is an **allowlist**: a field is reported only if it is named in
``CONFIG_SECTIONS`` below. A denylist keyed on names like ``*_KEY``/``*_TOKEN``
would be wrong in both directions -- it flags ``MAX_CONTEXT_TOKENS``, and it
misses ``REGISTRY_DATABASE_URL``, whose value embeds the Postgres password. New
settings are therefore invisible here until someone deliberately adds them, and
``tests/test_config_report.py`` fails if an allowlisted name stops existing.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic_core import PydanticUndefined

from wotbot.core.settings import Settings

# Stand-in for a secret's value. Never carries any part of the real one: a
# masked suffix ("last 4") would help identify a mistyped provider key, but
# these values end up in screenshots and pasted bug reports, and presence alone
# answers the question this page is for -- "did the operator configure it".
SECRET_PLACEHOLDER = "•••••••• (set)"
SECRET_MISSING = "not set"
CREDENTIALS_PLACEHOLDER = "***"
QUERY_PLACEHOLDER = "(query removed)"
FRAGMENT_PLACEHOLDER = "(fragment removed)"


class Render(Enum):
    """How a setting's value survives the trip to the browser."""

    # Reported verbatim.
    PLAIN = "plain"
    # Reported only as set/not set.
    SECRET = "secret"
    # Reported with the userinfo and the query string stripped. Host, port and
    # path are the useful half for debugging.
    URL = "url"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One allowlisted setting, and how it may be shown."""

    # Attribute on ``Settings``; also the lowercase form of its env var.
    attribute: str
    render: Render = Render.PLAIN
    # Set when the env var an operator sets differs from the attribute name.
    env_var: str | None = None
    note: str | None = None

    @property
    def name(self) -> str:
        return self.env_var or self.attribute.upper()


@dataclass(frozen=True, slots=True)
class SectionSpec:
    key: str
    title: str
    fields: tuple[FieldSpec, ...]


CONFIG_SECTIONS: tuple[SectionSpec, ...] = (
    SectionSpec(
        key="model",
        title="Model",
        fields=(
            FieldSpec("openai_model"),
            FieldSpec("openai_base_url", Render.URL, env_var="OPENAI_API_BASE_URL"),
            FieldSpec("openai_api_key", Render.SECRET),
            FieldSpec("openai_temperature", note="Provider default when unset"),
            FieldSpec("openai_disable_streaming"),
            FieldSpec("openai_model_supports_vision"),
        ),
    ),
    SectionSpec(
        key="reasoning",
        title="Reasoning effort",
        fields=(
            FieldSpec("reasoning_effort_enabled"),
            FieldSpec("reasoning_effort_levels"),
            FieldSpec("reasoning_effort_default"),
            FieldSpec("reasoning_effort_style"),
        ),
    ),
    SectionSpec(
        key="workspaces",
        title="Workspaces",
        fields=(FieldSpec("workspaces_enabled"),),
    ),
    SectionSpec(
        key="agent",
        title="Agent runtime",
        fields=(
            FieldSpec("max_iterations"),
            FieldSpec("recursion_limit"),
            FieldSpec("max_context_tokens"),
            FieldSpec("parallel_tool_calls"),
            FieldSpec("agent_handoff_enabled"),
            FieldSpec("sse_heartbeat_seconds"),
            FieldSpec("agent_state_database_url", Render.URL),
        ),
    ),
    SectionSpec(
        key="embeddings",
        title="Embeddings and search",
        fields=(
            FieldSpec(
                "openai_embedding_api_base_url",
                Render.URL,
                note="Falls back to OPENAI_API_BASE_URL",
            ),
            FieldSpec(
                "openai_embedding_api_key",
                Render.SECRET,
                note="Falls back to OPENAI_API_KEY",
            ),
            FieldSpec("openai_embedding_model"),
            FieldSpec("search_vector_dimensions"),
        ),
    ),
    SectionSpec(
        key="voice",
        title="Voice and media",
        fields=(
            FieldSpec("livekit_url", Render.URL),
            FieldSpec("livekit_public_url", Render.URL),
            FieldSpec("livekit_api_key", Render.SECRET),
            FieldSpec("livekit_api_secret", Render.SECRET),
            FieldSpec("livekit_agent_name"),
            FieldSpec("livekit_room_prefix"),
            FieldSpec("livekit_token_ttl_seconds"),
            FieldSpec("camera_frame_max_dimension"),
            FieldSpec("camera_frame_jpeg_quality"),
            FieldSpec("stt_transcriptions_url", Render.URL),
            FieldSpec("stt_model"),
            FieldSpec("stt_api_key", Render.SECRET),
            FieldSpec("stt_language", note="Auto-detect when unset"),
            FieldSpec("tts_speech_url", Render.URL),
            FieldSpec("tts_model"),
            FieldSpec("tts_voice"),
            FieldSpec("tts_api_key", Render.SECRET),
            FieldSpec("tts_response_format"),
            FieldSpec("tts_speed"),
            FieldSpec("tts_stream_format"),
        ),
    ),
    SectionSpec(
        key="jobs",
        title="Jobs",
        fields=(
            FieldSpec("job_task_timeout_seconds"),
            FieldSpec("job_run_stale_after_seconds"),
            FieldSpec("jobs_default_timezone"),
            FieldSpec("jobs_stream_batch_size"),
            FieldSpec("jobs_run_events_stream"),
        ),
    ),
    SectionSpec(
        key="services",
        title="Connected services",
        fields=(
            FieldSpec("wot_runtime_url", Render.URL),
            FieldSpec("wot_runtime_timeout_seconds"),
            FieldSpec("wot_runtime_subscription_timeout_seconds"),
            FieldSpec("code_executor_url", Render.URL),
            FieldSpec("code_executor_timeout_seconds"),
            FieldSpec("code_executor_retry_attempts"),
            FieldSpec("rdf_service_url", Render.URL),
            FieldSpec("rdf_query_timeout_seconds"),
            FieldSpec("registry_public_url", Render.URL),
        ),
    ),
    SectionSpec(
        key="storage",
        title="Storage and logging",
        fields=(
            FieldSpec("registry_database_url", Render.URL),
            FieldSpec("redis_url", Render.URL),
            FieldSpec("rdf_store_path"),
            FieldSpec("log_level"),
        ),
    ),
    SectionSpec(
        key="security",
        title="Shared secrets",
        fields=(
            FieldSpec("internal_api_key", Render.SECRET),
            FieldSpec("init_admin_token", Render.SECRET),
            FieldSpec("wot_runtime_registry_token", Render.SECRET),
            FieldSpec("wot_runtime_api_token", Render.SECRET),
            FieldSpec(
                "virtual_servient_registry_token",
                Render.SECRET,
                note="Falls back to WOT_RUNTIME_REGISTRY_TOKEN",
            ),
        ),
    ),
)


def strip_url_credentials(value: str) -> str:
    """Remove anything credential-shaped from a URL before reporting it.

    ``REGISTRY_DATABASE_URL`` and friends carry the Postgres password in the
    ``user:password@`` userinfo, so they cannot be reported verbatim -- but
    their host, port and database name are exactly what someone reads this page
    to check.

    The query string and fragment go too, uninspected: provider endpoints are
    routinely deployed with the key in a parameter (``?api-key=...``), and this
    page promises secrets are never shown by value. A key embedded in the path
    (LiteLLM/Azure-style ``/v1/<key>``) is indistinguishable from a route and
    survives -- worth knowing before putting a key there.
    """
    if not value:
        return value

    try:
        parts = urlsplit(value)
    except ValueError:
        # Unparseable means we cannot prove it is credential-free.
        return CREDENTIALS_PLACEHOLDER

    # An unescaped ``#`` or ``?`` in a password ends the netloc early, so the
    # rest of the credential lands in the fragment or query and the userinfo
    # check below never sees an "@". The whole value is then suspect.
    if "@" in value and "@" not in parts.netloc:
        return CREDENTIALS_PLACEHOLDER

    if parts.query:
        parts = parts._replace(query=QUERY_PLACEHOLDER)
    if parts.fragment:
        parts = parts._replace(fragment=FRAGMENT_PLACEHOLDER)

    if not parts.netloc or "@" not in parts.netloc:
        return urlunsplit(parts)

    userinfo, _, host = parts.netloc.rpartition("@")
    user, separator, _ = userinfo.partition(":")
    masked = f"{user}:{CREDENTIALS_PLACEHOLDER}" if separator else CREDENTIALS_PLACEHOLDER
    return urlunsplit(parts._replace(netloc=f"{masked}@{host}"))


def _declared_default(field_name: str) -> Any:
    field = Settings.model_fields.get(field_name)
    if field is None or field.default is PydanticUndefined:
        # Includes ``default_factory`` fields (the per-process consumer names),
        # whose value is generated at startup and never "the default".
        return PydanticUndefined
    return field.default


def _render_value(spec: FieldSpec, raw: Any) -> tuple[Any, bool]:
    """Return the reportable value and whether the setting is configured."""
    if spec.render is Render.SECRET:
        configured = bool(raw)
        return (SECRET_PLACEHOLDER if configured else SECRET_MISSING), configured

    configured = raw is not None and raw != ""

    if spec.render is Render.URL and isinstance(raw, str):
        return strip_url_credentials(raw), configured

    return raw, configured


def _describe_field(settings: Settings, spec: FieldSpec) -> dict[str, Any]:
    raw = getattr(settings, spec.attribute)
    value, configured = _render_value(spec, raw)
    default = _declared_default(spec.attribute)

    return {
        "name": spec.name,
        "value": value,
        "configured": configured,
        # "Is this the stock value or has someone changed it" -- the question
        # that actually matters when a deployment misbehaves. Compared against
        # the declared default rather than tracked via ``model_fields_set``,
        # which the fallback validators in ``Settings`` write to after load.
        "is_default": default is not PydanticUndefined and raw == default,
        "secret": spec.render is Render.SECRET,
        "note": spec.note,
    }


def build_config_report(settings: Settings, *, version: str) -> dict[str, Any]:
    """Assemble the redacted report served by ``GET /api/config``."""
    return {
        "version": version,
        "sections": [
            {
                "key": section.key,
                "title": section.title,
                "fields": [_describe_field(settings, spec) for spec in section.fields],
            }
            for section in CONFIG_SECTIONS
        ],
    }
