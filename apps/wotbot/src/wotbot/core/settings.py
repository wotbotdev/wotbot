import logging
import os
import socket
from dataclasses import dataclass
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DisableStreamingMode = bool | Literal["tool_calling"]
# How a resolved reasoning-effort level gets sent to the model:
# - "openai": the OpenAI/vLLM-standard ``reasoning_effort`` request field.
# - "qwen": Qwen's own ``enable_thinking`` chat-template flag via
#   ``extra_body``. Some vLLM/Qwen deployments don't reliably translate
#   ``reasoning_effort`` into ``enable_thinking`` themselves (see
#   https://github.com/vllm-project/vllm/issues/35574), so this talks to the
#   model natively instead. Qwen's switch is binary, not graduated: the
#   literal level "none" means thinking off, every other configured level
#   means on.
# - "openrouter": OpenRouter's own ``reasoning`` object via ``extra_body``.
#   Needed to see the reasoning at all: given a plain ``reasoning_effort``
#   OpenRouter bills the reasoning tokens and reports them in
#   ``output_token_details.reasoning``, but returns the ``reasoning`` field
#   empty. Only the object form makes it send the text back.
ReasoningEffortStyle = Literal["openai", "qwen", "openrouter"]
# How ``/audio/speech`` streams synthesized audio back:
# - "audio": the response body is the raw audio byte stream. Every
#   OpenAI-compatible server speaks this dialect, and some speak only this one
#   -- OpenRouter ignores ``stream_format`` and always answers with raw PCM.
# - "sse": OpenAI's token-billed models (gpt-4o-mini-tts and newer) can wrap
#   the audio in ``speech.audio.delta`` server-sent events instead.
# - "auto": let livekit-plugins-openai choose from the model name, which
#   means SSE for everything except the literal "tts-1"/"tts-1-hd".
TtsStreamFormat = Literal["audio", "sse", "auto"]


def _normalize_database_url(value: str) -> str:
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


def _optional(value: str) -> str | None:
    return value or None


def _fallback_value(value: str, fallback: str) -> str:
    return value or fallback


@dataclass(frozen=True, slots=True)
class LlmSettings:
    openai_api_key: str
    openai_model: str
    supports_vision: bool
    openai_temperature: float | None
    openai_disable_streaming: DisableStreamingMode
    openai_base_url: str


@dataclass(frozen=True, slots=True)
class ReasoningEffortSettings:
    enabled: bool
    levels: tuple[str, ...]
    default: str | None
    style: ReasoningEffortStyle


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    api_base_url: str
    api_key: str
    model: str


@dataclass(frozen=True, slots=True)
class AgentRuntimeSettings:
    max_iterations: int
    recursion_limit: int
    max_context_tokens: int
    parallel_tool_calls: bool
    state_database_url: str
    sse_heartbeat_seconds: float


@dataclass(frozen=True, slots=True)
class RegistrySettings:
    internal_api_key: str
    init_admin_token: str
    database_url: str
    public_url: str


@dataclass(frozen=True, slots=True)
class MediaSettings:
    livekit_url: str
    livekit_public_url: str
    livekit_api_key: str
    livekit_api_secret: str
    livekit_agent_name: str
    livekit_room_prefix: str
    livekit_token_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    transcriptions_url: str
    model: str
    api_key: str
    language: str


@dataclass(frozen=True, slots=True)
class TtsSettings:
    speech_url: str
    model: str
    voice: str
    api_key: str
    response_format: str
    speed: float
    stream_format: TtsStreamFormat


@dataclass(frozen=True, slots=True)
class CodeExecutorSettings:
    url: str
    timeout_seconds: int
    retry_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True, slots=True)
class IndexingSettings:
    search_vector_dimensions: int
    thing_events_stream: str
    thing_event_outbox_batch_size: int
    thing_event_outbox_poll_interval_seconds: float
    search_indexer_events_group: str
    search_indexer_events_consumer: str
    search_indexer_poll_block_ms: int
    search_indexer_batch_size: int
    search_indexer_claim_idle_ms: int
    search_indexer_retry_seconds: float


@dataclass(frozen=True, slots=True)
class RdfSettings:
    service_url: str
    store_path: str
    thing_events_stream: str
    events_group: str
    events_consumer: str
    events_batch_size: int
    events_poll_block_ms: int
    events_claim_idle_ms: int
    events_retry_seconds: float
    query_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class JobsSettings:
    task_timeout_seconds: int
    run_stale_after_seconds: int
    default_timezone: str
    redis_url: str
    events_group: str
    events_consumer: str
    stream_batch_size: int
    stream_poll_block_ms: int
    stream_claim_idle_ms: int
    run_events_stream: str


@dataclass(frozen=True, slots=True)
class WotRuntimeSettings:
    url: str
    registry_token: str
    api_token: str
    stream: str
    timeout_seconds: int
    subscription_timeout_seconds: int
    action_timeout_seconds: int
    virtual_servient_registry_token: str


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    level: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    # LLM
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_temperature: float | None = Field(default=None, ge=0, le=2)
    openai_disable_streaming: DisableStreamingMode = "tool_calling"
    openai_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_BASE_URL", "OPENAI_API_BASE_URL"),
    )
    # OpenAI-compatible endpoints do not expose a portable capability query.
    # Operators declare image-input support once; every fresh camera frame is
    # then attached directly to the main model rather than sent to a second LLM.
    openai_model_supports_vision: bool = False
    openai_embedding_api_base_url: str = Field(
        default="",
        validation_alias=AliasChoices(
            "OPENAI_EMBEDDING_API_BASE_URL",
            "OPENAI_API_BASE_URL",
            "OPENAI_BASE_URL",
        ),
    )
    openai_embedding_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_EMBEDDING_API_KEY", "OPENAI_API_KEY"),
    )
    openai_embedding_model: str = "mxbai-embed-large"

    # Reasoning effort: lets the chat UI ask a reasoning-capable model (o-series,
    # gpt-5, etc.) how hard to think. Off by default; when disabled, no
    # reasoning_effort is ever sent and the UI selector stays hidden.
    reasoning_effort_enabled: bool = False
    reasoning_effort_levels: str = "low,medium,high"
    reasoning_effort_default: str = ""
    reasoning_effort_style: ReasoningEffortStyle = "openai"

    # Agent
    max_iterations: int = 20
    recursion_limit: int = 50
    max_context_tokens: int = 120000
    parallel_tool_calls: bool = False
    # When enabled, action branches (control/analysis/jobs/virtual_things) may
    # hand off to one another via the route_to tool instead of ending. Off by
    # default: the compiled graph is identical to the single-branch graph.
    agent_handoff_enabled: bool = False
    agent_state_database_url: str = ""
    # Seconds of silence on the chat SSE stream before we emit a keepalive
    # comment. Long tool calls (e.g. a slow plot in the code executor) produce
    # no events; without a heartbeat the consuming undici client aborts the
    # stream with UND_ERR_BODY_TIMEOUT before the final answer. <=0 disables it.
    sse_heartbeat_seconds: float = 15.0

    # Registry and security
    internal_api_key: str = ""
    init_admin_token: str = ""
    wot_runtime_registry_token: str = ""
    registry_database_url: str = "postgresql://wotbot:wotbot@localhost:5432/wotbot"
    registry_public_url: str = "http://localhost:8000"

    # Inbound agents and generated MCP Apps share the existing assistant stack.
    a2a_enabled: bool = False
    mcp_enabled: bool = False
    public_ui_origin: str = "http://localhost:3000"
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    a2a_task_retention_days: int = Field(default=30, ge=1)
    a2a_artifact_retention_days: int = Field(default=7, ge=1)
    a2a_download_url_ttl_seconds: int = Field(default=3600, ge=1, le=86400)
    a2a_max_artifact_bytes: int = Field(default=25 * 1024 * 1024, ge=1)

    # External discovery candidates and downloads are short-lived capabilities.
    discovery_candidate_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    discovery_download_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    discovery_refresh_ttl_seconds: int = Field(default=600, ge=60, le=3600)

    # LiveKit media ingress
    livekit_url: str = ""
    livekit_public_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    livekit_agent_name: str = "wotbot"
    livekit_room_prefix: str = "wotbot"
    livekit_token_ttl_seconds: int = 600
    camera_frame_max_dimension: int = Field(default=1024, gt=0)
    camera_frame_jpeg_quality: int = Field(default=85, ge=1, le=100)

    # Speech-to-text
    stt_transcriptions_url: str = ""
    stt_model: str = "whisper-large-turbo"
    stt_api_key: str = ""
    stt_language: str = ""

    # Text-to-speech
    tts_speech_url: str = ""
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"
    tts_api_key: str = ""
    tts_response_format: str = "pcm"
    tts_speed: float = 1.0
    # Raw bytes by default: it is the one dialect every OpenAI-compatible
    # speech endpoint serves, while the plugin's model-name heuristic sends
    # anything not called "tts-1" down the SSE path that only some of them have.
    tts_stream_format: TtsStreamFormat = "audio"

    # Code Executor
    code_executor_url: str = "http://localhost:8888"
    code_executor_timeout_seconds: int = 330
    code_executor_retry_attempts: int = 3
    code_executor_retry_backoff_seconds: float = 1.0

    # Search and Thing indexing
    search_vector_dimensions: int = Field(default=1024, gt=0)
    thing_events_stream: str = "thing_events"
    thing_event_outbox_batch_size: int = Field(default=20, gt=0)
    thing_event_outbox_poll_interval_seconds: float = Field(default=0.5, gt=0)
    search_indexer_events_group: str = "thing_search_indexer"
    search_indexer_events_consumer: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}"
    )
    search_indexer_poll_block_ms: int = 5000
    search_indexer_batch_size: int = 20
    search_indexer_claim_idle_ms: int = 60000
    search_indexer_retry_seconds: float = 5
    rdf_service_url: str = "http://rdf-service:8124"
    rdf_store_path: str = "/data/rdf"
    rdf_events_group: str = "thing_rdf_indexer"
    rdf_events_consumer: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}"
    )
    rdf_events_batch_size: int = 20
    rdf_events_poll_block_ms: int = 5000
    rdf_events_claim_idle_ms: int = 60000
    rdf_events_retry_seconds: float = 5
    rdf_query_timeout_seconds: int = 20
    thing_enrichment_config_path: str = ""
    thing_enrichment_max_repair_attempts: int = Field(default=2, ge=0, le=5)

    # Jobs and WoT runtime
    job_task_timeout_seconds: int = 300
    job_run_stale_after_seconds: int = 900
    jobs_default_timezone: str = "Europe/Berlin"
    redis_url: str = "redis://valkey:6379"
    wot_runtime_url: str = "http://wot-runtime:3003"
    wot_runtime_api_token: str = ""
    wot_runtime_stream: str = "wot_runtime_events"
    wot_runtime_timeout_seconds: int = 15
    wot_runtime_subscription_timeout_seconds: int = 5
    wot_runtime_action_timeout_seconds: int = 135
    virtual_servient_registry_token: str = ""
    jobs_events_group: str = "job_runner"
    jobs_events_consumer: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}"
    )
    jobs_stream_batch_size: int = 20
    jobs_stream_poll_block_ms: int = 5000
    jobs_stream_claim_idle_ms: int = 60000
    jobs_run_events_stream: str = "jobs_run_events"

    # Logging
    log_level: str = "INFO"

    @field_validator("registry_database_url")
    @classmethod
    def _normalize_registry_database_url(cls, value: str) -> str:
        return _normalize_database_url(value)

    @field_validator("openai_temperature", mode="before")
    @classmethod
    def _empty_temperature_uses_provider_default(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @field_validator("openai_disable_streaming", mode="before")
    @classmethod
    def _normalize_disable_streaming(cls, value: object) -> object:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "tool_calling":
                return normalized
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return value

    @model_validator(mode="after")
    def _validate_reasoning_effort_default(self) -> "Settings":
        default = self.reasoning_effort_default.strip()
        if default and default not in self._reasoning_effort_level_tuple():
            logging.getLogger(__name__).warning(
                "REASONING_EFFORT_DEFAULT=%r is not in REASONING_EFFORT_LEVELS=%r; "
                "ignoring the default.",
                default,
                self.reasoning_effort_levels,
            )
            self.reasoning_effort_default = ""
        return self

    @model_validator(mode="after")
    def _apply_fallback_settings(self) -> "Settings":
        self.openai_embedding_api_base_url = _fallback_value(
            self.openai_embedding_api_base_url,
            self.openai_base_url,
        )
        self.openai_embedding_api_key = _fallback_value(
            self.openai_embedding_api_key,
            self.openai_api_key,
        )
        return self

    def validate_runtime_security_settings(self) -> None:
        missing: list[str] = []
        if not self.wot_runtime_registry_token:
            missing.append("WOT_RUNTIME_REGISTRY_TOKEN")
        if not self.wot_runtime_api_token:
            missing.append("WOT_RUNTIME_API_TOKEN")

        if missing:
            missing_values = ", ".join(missing)
            raise RuntimeError(
                "WoT runtime integration requires shared auth token(s). "
                f"Missing required setting(s): {missing_values}."
            )

    @property
    def llm(self) -> LlmSettings:
        return LlmSettings(
            openai_api_key=self.openai_api_key,
            openai_model=self.openai_model,
            supports_vision=self.openai_model_supports_vision,
            openai_temperature=self.openai_temperature,
            openai_disable_streaming=self.openai_disable_streaming,
            openai_base_url=self.openai_base_url,
        )

    def _reasoning_effort_level_tuple(self) -> tuple[str, ...]:
        return tuple(
            level for raw in self.reasoning_effort_levels.split(",") if (level := raw.strip())
        )

    @property
    def reasoning_effort(self) -> ReasoningEffortSettings:
        return ReasoningEffortSettings(
            enabled=self.reasoning_effort_enabled,
            levels=self._reasoning_effort_level_tuple(),
            default=self.reasoning_effort_default.strip() or None,
            style=self.reasoning_effort_style,
        )

    @property
    def embeddings(self) -> EmbeddingSettings:
        return EmbeddingSettings(
            api_base_url=self.openai_embedding_api_base_url,
            api_key=self.openai_embedding_api_key,
            model=self.openai_embedding_model,
        )

    @property
    def agent(self) -> AgentRuntimeSettings:
        return AgentRuntimeSettings(
            max_iterations=self.max_iterations,
            recursion_limit=self.recursion_limit,
            max_context_tokens=self.max_context_tokens,
            parallel_tool_calls=self.parallel_tool_calls,
            state_database_url=self.agent_state_database_url,
            sse_heartbeat_seconds=self.sse_heartbeat_seconds,
        )

    @property
    def registry(self) -> RegistrySettings:
        return RegistrySettings(
            internal_api_key=self.internal_api_key,
            init_admin_token=self.init_admin_token,
            database_url=self.registry_database_url,
            public_url=self.registry_public_url,
        )

    @property
    def media(self) -> MediaSettings:
        return MediaSettings(
            livekit_url=self.livekit_url,
            livekit_public_url=self.livekit_public_url,
            livekit_api_key=self.livekit_api_key,
            livekit_api_secret=self.livekit_api_secret,
            livekit_agent_name=self.livekit_agent_name,
            livekit_room_prefix=self.livekit_room_prefix,
            livekit_token_ttl_seconds=self.livekit_token_ttl_seconds,
        )

    @property
    def speech(self) -> SpeechSettings:
        return SpeechSettings(
            transcriptions_url=self.stt_transcriptions_url,
            model=self.stt_model,
            api_key=self.stt_api_key,
            language=self.stt_language,
        )

    @property
    def tts(self) -> TtsSettings:
        return TtsSettings(
            speech_url=self.tts_speech_url,
            model=self.tts_model,
            voice=self.tts_voice,
            api_key=self.tts_api_key,
            response_format=self.tts_response_format,
            speed=self.tts_speed,
            stream_format=self.tts_stream_format,
        )

    @property
    def code_executor(self) -> CodeExecutorSettings:
        return CodeExecutorSettings(
            url=self.code_executor_url,
            timeout_seconds=self.code_executor_timeout_seconds,
            retry_attempts=self.code_executor_retry_attempts,
            retry_backoff_seconds=self.code_executor_retry_backoff_seconds,
        )

    @property
    def indexing(self) -> IndexingSettings:
        return IndexingSettings(
            search_vector_dimensions=self.search_vector_dimensions,
            thing_events_stream=self.thing_events_stream,
            thing_event_outbox_batch_size=self.thing_event_outbox_batch_size,
            thing_event_outbox_poll_interval_seconds=self.thing_event_outbox_poll_interval_seconds,
            search_indexer_events_group=self.search_indexer_events_group,
            search_indexer_events_consumer=self.search_indexer_events_consumer,
            search_indexer_poll_block_ms=self.search_indexer_poll_block_ms,
            search_indexer_batch_size=self.search_indexer_batch_size,
            search_indexer_claim_idle_ms=self.search_indexer_claim_idle_ms,
            search_indexer_retry_seconds=self.search_indexer_retry_seconds,
        )

    @property
    def rdf(self) -> RdfSettings:
        return RdfSettings(
            service_url=self.rdf_service_url,
            store_path=self.rdf_store_path,
            thing_events_stream=self.thing_events_stream,
            events_group=self.rdf_events_group,
            events_consumer=self.rdf_events_consumer,
            events_batch_size=self.rdf_events_batch_size,
            events_poll_block_ms=self.rdf_events_poll_block_ms,
            events_claim_idle_ms=self.rdf_events_claim_idle_ms,
            events_retry_seconds=self.rdf_events_retry_seconds,
            query_timeout_seconds=self.rdf_query_timeout_seconds,
        )

    @property
    def jobs(self) -> JobsSettings:
        return JobsSettings(
            task_timeout_seconds=self.job_task_timeout_seconds,
            run_stale_after_seconds=self.job_run_stale_after_seconds,
            default_timezone=self.jobs_default_timezone,
            redis_url=self.redis_url,
            events_group=self.jobs_events_group,
            events_consumer=self.jobs_events_consumer,
            stream_batch_size=self.jobs_stream_batch_size,
            stream_poll_block_ms=self.jobs_stream_poll_block_ms,
            stream_claim_idle_ms=self.jobs_stream_claim_idle_ms,
            run_events_stream=self.jobs_run_events_stream,
        )

    @property
    def wot_runtime(self) -> WotRuntimeSettings:
        return WotRuntimeSettings(
            url=self.wot_runtime_url,
            registry_token=self.wot_runtime_registry_token,
            api_token=self.wot_runtime_api_token,
            stream=self.wot_runtime_stream,
            timeout_seconds=self.wot_runtime_timeout_seconds,
            subscription_timeout_seconds=self.wot_runtime_subscription_timeout_seconds,
            action_timeout_seconds=self.wot_runtime_action_timeout_seconds,
            virtual_servient_registry_token=(
                self.virtual_servient_registry_token or self.wot_runtime_registry_token
            ),
        )

    @property
    def logging(self) -> LoggingSettings:
        return LoggingSettings(level=self.log_level)

    @property
    def DATABASE_URL(self) -> str:
        return self.registry_database_url

    @property
    def REDIS_URL(self) -> str:
        return self.redis_url

    @property
    def THING_EVENTS_STREAM(self) -> str:
        return self.thing_events_stream

    @property
    def THING_EVENT_OUTBOX_BATCH_SIZE(self) -> int:
        return self.thing_event_outbox_batch_size

    @property
    def THING_EVENT_OUTBOX_POLL_INTERVAL_SECONDS(self) -> float:
        return self.thing_event_outbox_poll_interval_seconds

    @property
    def INIT_ADMIN_TOKEN(self) -> str | None:
        return _optional(self.init_admin_token)

    @property
    def WOT_RUNTIME_REGISTRY_TOKEN(self) -> str | None:
        return _optional(self.wot_runtime_registry_token)

    @property
    def WOT_RUNTIME_API_TOKEN(self) -> str | None:
        return _optional(self.wot_runtime_api_token)

    @property
    def VIRTUAL_SERVIENT_REGISTRY_TOKEN(self) -> str | None:
        return _optional(self.virtual_servient_registry_token or self.wot_runtime_registry_token)

    @property
    def SEARCH_VECTOR_DIMENSIONS(self) -> int:
        return self.search_vector_dimensions

    @property
    def SEARCH_INDEXER_EVENTS_GROUP(self) -> str:
        return self.search_indexer_events_group

    @property
    def SEARCH_INDEXER_EVENTS_CONSUMER(self) -> str:
        return self.search_indexer_events_consumer

    @property
    def SEARCH_INDEXER_POLL_BLOCK_MS(self) -> int:
        return self.search_indexer_poll_block_ms

    @property
    def SEARCH_INDEXER_BATCH_SIZE(self) -> int:
        return self.search_indexer_batch_size

    @property
    def SEARCH_INDEXER_CLAIM_IDLE_MS(self) -> int:
        return self.search_indexer_claim_idle_ms

    @property
    def SEARCH_INDEXER_RETRY_SECONDS(self) -> float:
        return self.search_indexer_retry_seconds

    @property
    def RDF_SERVICE_URL(self) -> str:
        return self.rdf_service_url

    @property
    def RDF_STORE_PATH(self) -> str:
        return self.rdf_store_path

    @property
    def RDF_EVENTS_GROUP(self) -> str:
        return self.rdf_events_group

    @property
    def RDF_EVENTS_CONSUMER(self) -> str:
        return self.rdf_events_consumer

    @property
    def RDF_EVENTS_BATCH_SIZE(self) -> int:
        return self.rdf_events_batch_size

    @property
    def RDF_EVENTS_POLL_BLOCK_MS(self) -> int:
        return self.rdf_events_poll_block_ms

    @property
    def RDF_EVENTS_CLAIM_IDLE_MS(self) -> int:
        return self.rdf_events_claim_idle_ms

    @property
    def RDF_EVENTS_RETRY_SECONDS(self) -> float:
        return self.rdf_events_retry_seconds

    @property
    def RDF_QUERY_TIMEOUT_SECONDS(self) -> int:
        return self.rdf_query_timeout_seconds

    @property
    def OPENAI_API_BASE_URL(self) -> str | None:
        return _optional(self.openai_base_url)

    @property
    def OPENAI_API_KEY(self) -> str | None:
        return _optional(self.openai_api_key)

    @property
    def OPENAI_MODEL(self) -> str | None:
        return _optional(self.openai_model)

    @property
    def OPENAI_TEMPERATURE(self) -> float | None:
        return self.openai_temperature

    @property
    def OPENAI_EMBEDDING_API_BASE_URL(self) -> str | None:
        return _optional(self.openai_embedding_api_base_url)

    @property
    def OPENAI_EMBEDDING_API_KEY(self) -> str | None:
        return _optional(self.openai_embedding_api_key)

    @property
    def OPENAI_EMBEDDING_MODEL(self) -> str:
        return self.openai_embedding_model

    @property
    def WOT_RUNTIME_URL(self) -> str:
        return self.wot_runtime_url

    @property
    def WOT_RUNTIME_TIMEOUT_SECONDS(self) -> int:
        return self.wot_runtime_timeout_seconds

    @property
    def WOT_RUNTIME_SUBSCRIPTION_TIMEOUT_SECONDS(self) -> int:
        return self.wot_runtime_subscription_timeout_seconds

    @property
    def WOT_RUNTIME_ACTION_TIMEOUT_SECONDS(self) -> int:
        return self.wot_runtime_action_timeout_seconds

    @property
    def REGISTRY_PUBLIC_URL(self) -> str:
        return self.registry_public_url
