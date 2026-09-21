import asyncio
import base64
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from wotbot.core.settings import Settings
from wotbot.panels import visual_review as visual
from wotbot.panels.evidence import BrowserValidation

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()


def assessment(verdict="absent"):
    return visual.VisualAssessment(
        assessments=[
            {
                "viewport": width,
                "category": category,
                "verdict": verdict,
                "evidence": "Labels overlap the legend" if verdict != "absent" else "",
            }
            for width in ("normal", "narrow")
            for category in sorted(visual.CATEGORIES)
        ]
    )


@pytest.fixture
def model(monkeypatch):
    structured = AsyncMock()
    structured.ainvoke.return_value = {
        "parsed": assessment(),
        "parsing_error": None,
        "raw": AIMessage(
            content="",
            usage_metadata={"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
            response_metadata={"token_usage": {"cost": 0.001}},
        ),
    }
    llm = Mock()
    llm.with_structured_output.return_value = structured
    factory = Mock(return_value=llm)
    monkeypatch.setattr(visual, "make_llm", factory)
    return factory, structured


def run(**settings):
    return asyncio.run(
        visual.review_visuals(
            BrowserValidation(status="passed", screenshot_base64=PNG, narrow_screenshot_base64=PNG),
            Settings(_env_file=None, openai_model_supports_vision=True, **settings),
        )
    )


def test_review_has_separate_context_bounded_call_and_usage_without_images(model):
    factory, structured = model
    result = run(panel_visual_review_model="vision-reviewer")
    assert result["status"] == "clear" and result["mode"] == "advisory"
    assert result["usage"] == {
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
        "provider_reported_cost": 0.001,
    }
    assert factory.call_args.args[0].openai_model == "vision-reviewer"
    assert factory.call_args.kwargs == {"timeout": 30, "max_retries": 0, "max_tokens": 2500}
    messages = structured.ainvoke.call_args.args[0]
    assert len(messages) == 2 and messages[0].content == visual.PROMPT
    assert len([part for part in messages[1].content if part["type"] == "image_url"]) == 2
    assert PNG not in str(result)


@pytest.mark.parametrize("verdict", ["present", "uncertain"])
def test_both_definite_and_uncertain_findings_are_advisory_warnings(model, verdict):
    model[1].ainvoke.return_value["parsed"] = assessment(verdict)
    result = run()
    assert result["status"] == "warnings" and result["mode"] == "advisory"


@pytest.mark.parametrize("error", [RuntimeError("secret provider body"), TimeoutError()])
def test_provider_errors_are_explicit_and_do_not_leak_response_body(model, error):
    model[1].ainvoke.side_effect = error
    result = run()
    assert result["status"] == "unavailable" and "secret" not in str(result)


def test_incomplete_review_is_not_a_clean_verdict(model):
    model[1].ainvoke.return_value["parsed"] = None
    result = run()
    assert result["status"] == "unavailable" and result["usage"]["input_tokens"] == 123
    rows = assessment().model_dump()["assessments"]
    rows[-1] = rows[0]
    with pytest.raises(ValidationError):
        visual.VisualAssessment(assessments=rows)


def test_provider_json_is_validated_for_complete_coverage(model):
    model[1].ainvoke.return_value["parsed"] = assessment().model_dump()
    assert run()["status"] == "clear"
    rows = model[1].ainvoke.return_value["parsed"]["assessments"]
    rows[-1] = rows[0]
    assert run()["status"] == "unavailable"


def test_hung_model_call_has_a_hard_deadline(model, monkeypatch):
    async def never_finishes(*_args, **_kwargs):
        await asyncio.Event().wait()

    model[1].ainvoke.side_effect = never_finishes
    monkeypatch.setattr(visual, "REVIEW_SECONDS", 0.01)
    assert "deadline" in run()["reason"]


def test_disabled_missing_images_and_undeclared_vision_skip_model(model):
    assert run(panel_visual_review_enabled=False)["status"] == "disabled"
    for settings, images in [
        (Settings(_env_file=None), (PNG, PNG)),
        (Settings(_env_file=None, openai_model_supports_vision=True), (PNG, None)),
    ]:
        result = asyncio.run(
            visual.review_visuals(
                BrowserValidation(
                    status="passed", screenshot_base64=images[0], narrow_screenshot_base64=images[1]
                ),
                settings,
            )
        )
        assert result["status"] == "unavailable"
    model[0].assert_not_called()


def test_cancellation_propagates(model):
    model[1].ainvoke.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        run()
