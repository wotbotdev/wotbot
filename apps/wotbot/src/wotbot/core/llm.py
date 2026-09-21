"""Helpers for creating the wotbot LLM."""

import logging
from typing import Any

from langchain_openai import ChatOpenAI

from wotbot.core.openrouter import ChatOpenRouter
from wotbot.core.reasoning_effort import reasoning_effort_kwargs
from wotbot.core.settings import Settings

logger = logging.getLogger(__name__)


def make_llm(
    settings: Settings, *, timeout: float = 120, max_retries: int = 2, max_tokens: int | None = None
) -> ChatOpenAI:
    llm = settings.llm
    kwargs: dict[str, Any] = {
        "model": llm.openai_model,
        "api_key": llm.openai_api_key,
        "base_url": llm.openai_base_url or None,
        "disable_streaming": llm.openai_disable_streaming,
        "timeout": timeout,
        "max_retries": max_retries,
    }
    if llm.openai_temperature is not None:
        kwargs["temperature"] = llm.openai_temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    reasoning_effort = settings.reasoning_effort
    if reasoning_effort.enabled and reasoning_effort.default:
        kwargs.update(reasoning_effort_kwargs(reasoning_effort.default, reasoning_effort.style))
    # OpenRouter returns its reasoning text in a field the stock parser drops.
    if reasoning_effort.style == "openrouter":
        return ChatOpenRouter(**kwargs)
    return ChatOpenAI(**kwargs)
