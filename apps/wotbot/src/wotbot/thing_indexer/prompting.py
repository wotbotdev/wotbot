from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

SUMMARY_PROMPT_VERSION = "v7"

SYSTEM_PROMPT = (
    "You create concise, search-friendly summaries for Web of Things (WoT) Thing Descriptions."
)

PROMPT_TEMPLATE = """Given the raw Thing Description JSON below, produce a concise,
search-friendly plain-text summary.

Rules:
1. Preserve exact WoT terms: property, action, and event names must appear verbatim.
2. Do not invent capabilities, locations, or descriptions that are not in the TD.
3. Infer a likely deployment context or location only when the title, description, tags, or
   affordance names support it (e.g. "line 3 vibration monitor" -> line 3). State it as a
   candidate, not a fact, and do not assume every Thing has a physical installation.
4. Include alternative search phrasings a user might type
   (e.g. "pause the conveyor", "check the API quota").
5. Keep the output plain text with short labeled sections. No markdown.

Thing Description JSON:
{td_json}
"""


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text)
        return "\n".join(parts)
    return ""


async def generate_summary(
    client: AsyncOpenAI,
    *,
    model: str,
    thing_td: dict[str, Any],
) -> str:
    td_json = json.dumps(thing_td, indent=2, ensure_ascii=False)
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": PROMPT_TEMPLATE.format(td_json=td_json)},
        ],
    )
    content = response.choices[0].message.content if response.choices else ""
    result = _extract_message_text(content).strip()
    if not result:
        raise RuntimeError("LLM returned an empty summary")
    return result
