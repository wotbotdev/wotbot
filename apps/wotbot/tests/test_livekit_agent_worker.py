import asyncio
import json
import pickle
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, AIMessageChunk

from wotbot.agent.device_interactions import DEVICE_INTERACTION_SUMMARY_TYPE
from wotbot.agent.voice import assistant_text_from_graph_result
from wotbot.core.settings import Settings
from wotbot.media import SNAPSHOT_EVENT_TYPE, SNAPSHOT_TOPIC
from wotbot.workers import livekit
from wotbot.workers.livekit import speech as livekit_speech
from wotbot.workers.livekit import worker as livekit_worker
from wotbot.workers.livekit.graph import VoiceSafeGraphStream, compile_graph


def test_livekit_session_handler_is_spawn_pickleable() -> None:
    assert pickle.loads(pickle.dumps(livekit.wotbot)) is (livekit.wotbot)


def test_livekit_camera_snapshot_publisher_sends_reliable_data_packet() -> None:
    class FakeLocalParticipant:
        def __init__(self) -> None:
            self.calls = []

        async def publish_data(self, payload, **kwargs):
            self.calls.append((payload, kwargs))

    local_participant = FakeLocalParticipant()
    room = type("FakeRoom", (), {"local_participant": local_participant})()

    asyncio.run(livekit_worker._publish_camera_snapshot(room, "2026-06-16T09:00:00+00:00"))

    assert len(local_participant.calls) == 1
    payload, kwargs = local_participant.calls[0]
    assert kwargs == {"reliable": True, "topic": SNAPSHOT_TOPIC}
    assert json.loads(payload) == {
        "type": SNAPSHOT_EVENT_TYPE,
        "capturedAt": "2026-06-16T09:00:00+00:00",
    }


def test_livekit_stt_kwargs_use_transcriptions_endpoint_without_llm_key() -> None:
    settings = Settings(
        openai_api_key="llm-key",
        stt_transcriptions_url="http://stt:8000/v1/audio/transcriptions",
        stt_api_key="",
        stt_language="",
    )

    kwargs = livekit_speech.stt_kwargs(settings)

    assert kwargs["base_url"] == "http://stt:8000/v1"
    assert "api_key" not in kwargs
    assert kwargs["detect_language"] is True
    assert "language" not in kwargs


def test_livekit_stt_kwargs_use_dedicated_speech_key_and_language() -> None:
    settings = Settings(
        openai_api_key="llm-key",
        stt_transcriptions_url="http://stt:8000/v1/audio/transcriptions",
        stt_api_key="speech-key",
        stt_language="de",
    )

    kwargs = livekit_speech.stt_kwargs(settings)

    assert kwargs["api_key"] == "speech-key"
    assert kwargs["language"] == "de"
    assert "detect_language" not in kwargs


def test_livekit_tts_kwargs_use_speech_endpoint_without_llm_key() -> None:
    settings = Settings(
        openai_api_key="llm-key",
        tts_speech_url="http://tts:8880/v1/audio/speech",
        tts_api_key="",
    )

    kwargs = livekit_speech.tts_kwargs(settings)

    assert kwargs["base_url"] == "http://tts:8880/v1"
    assert "api_key" not in kwargs


def test_livekit_tts_kwargs_can_fall_back_to_openai_when_no_speech_url() -> None:
    settings = Settings(
        openai_api_key="llm-key",
        tts_speech_url="",
        tts_api_key="",
    )
    settings.openai_base_url = "http://openai-compatible:8080/v1"

    kwargs = livekit_speech.tts_kwargs(settings)

    assert kwargs["base_url"] == "http://openai-compatible:8080/v1"
    assert kwargs["api_key"] == "llm-key"


async def _synthesize_stream(settings: Settings):
    """Build the TTS plugin and return the reader it picks, without calling out."""
    tts = livekit_speech.make_tts(settings)
    stream = tts.synthesize("hello")
    try:
        return stream
    finally:
        await stream.aclose()
        await tts.aclose()


def test_livekit_tts_reads_raw_audio_bytes_by_default() -> None:
    # OpenAI-compatible endpoints (OpenRouter, Speaches, LocalAI) answer
    # /audio/speech with raw audio whatever the model is called; the plugin's
    # SSE reader would find no data: lines in that body and push no frames.
    settings = Settings(
        tts_speech_url="https://openrouter.ai/api/v1/audio/speech",
        tts_api_key="speech-key",
        tts_model="hexgrad/kokoro-82m",
        tts_voice="af_alloy",
    )

    stream = asyncio.run(_synthesize_stream(settings))

    assert type(stream).__name__ == "AudioChunkedStream"


def test_livekit_tts_can_read_sse_for_openai_token_billed_voices() -> None:
    settings = Settings(
        tts_api_key="speech-key",
        tts_model="gpt-4o-mini-tts",
        tts_stream_format="sse",
    )

    stream = asyncio.run(_synthesize_stream(settings))

    assert type(stream).__name__ == "SSEChunkedStream"


def test_livekit_tts_auto_defers_to_the_plugin_model_heuristic() -> None:
    settings = Settings(tts_api_key="speech-key", tts_model="tts-1", tts_stream_format="auto")

    stream = asyncio.run(_synthesize_stream(settings))

    assert type(stream).__name__ == "AudioChunkedStream"


def test_livekit_voice_graph_filters_tool_and_router_output_chunks() -> None:
    class FakeGraph:
        def astream(self, *_args, **_kwargs):
            async def events():
                yield (
                    AIMessageChunk(content='{"intent":"control"}'),
                    {"langgraph_node": "router"},
                )
                yield (
                    AIMessageChunk(
                        content="",
                        tool_call_chunks=[
                            {
                                "name": "get_current_time",
                                "args": "{}",
                                "id": "call-1",
                                "index": 0,
                            }
                        ],
                    ),
                    {"langgraph_node": "respond"},
                )
                yield (
                    AIMessageChunk(content='{"raw":"tool result"}'),
                    {"langgraph_node": "respond_tools"},
                )
                yield (
                    AIMessageChunk(content="The light is on."),
                    {"langgraph_node": "respond"},
                )
                yield (
                    AIMessageChunk(content="The morning check-in is scheduled."),
                    {"langgraph_node": "jobs_llm"},
                )
                yield (
                    AIMessageChunk(content="The virtual sensor is active."),
                    {"langgraph_node": "virtual_things_llm"},
                )

            return events()

    async def collect_events():
        return [
            event
            async for event in VoiceSafeGraphStream(FakeGraph()).astream(
                {"messages": []},
                {"configurable": {"thread_id": "thread-a"}},
                stream_mode="messages",
            )
        ]

    events = asyncio.run(collect_events())

    assert [(message.content, metadata) for message, metadata in events] == [
        ("The light is on.", {"langgraph_node": "respond"}),
        (
            "The morning check-in is scheduled.",
            {"langgraph_node": "jobs_llm"},
        ),
        (
            "The virtual sensor is active.",
            {"langgraph_node": "virtual_things_llm"},
        ),
    ]


def test_livekit_graph_enables_voice_response_mode() -> None:
    settings = Settings(
        openai_api_key="test-key",
        agent_system_prompt_extra="deployment guidance",
    )
    checkpointer = object()
    compiled_graph = MagicMock()
    configured_graph = object()
    compiled_graph.with_config.return_value = configured_graph

    with (
        patch("wotbot.workers.livekit.graph.make_llm", return_value=object()),
        patch(
            "wotbot.workers.livekit.graph.build_graph",
            return_value=compiled_graph,
        ) as build_graph,
    ):
        result = compile_graph(settings, checkpointer)

    assert result is configured_graph
    assert build_graph.call_args.kwargs["voice_mode"] is True
    assert build_graph.call_args.kwargs["agent_system_prompt_extra"] == "deployment guidance"


def test_voice_final_text_skips_device_summary_marker() -> None:
    result = {
        "messages": [
            AIMessage(content="The light is on."),
            AIMessage(
                content=json.dumps(
                    {
                        "type": DEVICE_INTERACTION_SUMMARY_TYPE,
                        "interactions": [
                            {
                                "type": "write_property",
                                "thingId": "urn:lamp",
                                "affordanceName": "state",
                                "ok": True,
                            }
                        ],
                    }
                )
            ),
        ]
    }

    assert assistant_text_from_graph_result(result) == "The light is on."
