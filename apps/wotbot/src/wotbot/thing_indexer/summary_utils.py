import hashlib
import json
from typing import Any, TypedDict


class ThingTDMetadata(TypedDict):
    id: str
    title: str
    description: str
    tags: list[str]
    propertyNames: list[str]
    actionNames: list[str]
    eventNames: list[str]


# Event envelope fields that are not part of the Thing Description. Keeping
# them out preserves the TD hash and keeps them out of summaries and RDF.
_EVENT_ENVELOPE_KEYS = frozenset({"eventType", "hash", "workspaceId"})


def normalize_thing_td_payload(thing_td: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in thing_td.items() if key not in _EVENT_ENVELOPE_KEYS}


def clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip()


def compute_td_hash(thing_td: dict[str, Any]) -> str:
    payload = json.dumps(
        normalize_thing_td_payload(thing_td),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_td_metadata(thing_td: dict[str, Any]) -> ThingTDMetadata:
    """Pull flat metadata fields directly from the raw TD dict for vector store storage."""
    thing_id = clean_text(thing_td.get("id")) or "unknown"
    title = clean_text(thing_td.get("title")) or thing_id
    description = clean_text(thing_td.get("description"))

    tags: list[str] = []
    raw_tags = thing_td.get("tags")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, str):
                cleaned = clean_text(tag)
                if cleaned:
                    tags.append(cleaned)

    def _names(section: Any) -> list[str]:
        if not isinstance(section, dict):
            return []
        return sorted(str(k) for k in section if isinstance(k, str))

    return {
        "id": thing_id,
        "title": title,
        "description": description,
        "tags": tags,
        "propertyNames": _names(thing_td.get("properties")),
        "actionNames": _names(thing_td.get("actions")),
        "eventNames": _names(thing_td.get("events")),
    }
