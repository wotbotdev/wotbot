"""Shared decoding for WoT property-observation and event payloads."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any


def _binary_payload(payload_base64: str, content_type: str, size_bytes: int) -> dict[str, Any]:
    return {
        "kind": "binary",
        "contentType": content_type or "application/octet-stream",
        "bodyBase64": payload_base64,
        "sizeBytes": size_bytes,
    }


def _is_binary_content_type(content_type: str) -> bool:
    normalized = content_type.lower()
    return bool(content_type) and "json" not in normalized and not normalized.startswith("text/")


def decode_runtime_payload(payload_base64: str, content_type: str) -> Any:
    """Best-effort decode of a runtime stream payload into a JSON-friendly value."""
    normalized_content_type = (content_type or "").lower()
    if not payload_base64:
        if _is_binary_content_type(content_type):
            return _binary_payload("", content_type, 0)
        return None
    try:
        raw = base64.b64decode(payload_base64)
    except (binascii.Error, ValueError):
        return None
    if not normalized_content_type or "json" in normalized_content_type:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _binary_payload(payload_base64, content_type, len(raw))
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if normalized_content_type.startswith("text/"):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return _binary_payload(payload_base64, content_type, len(raw))
    return _binary_payload(payload_base64, content_type, len(raw))
