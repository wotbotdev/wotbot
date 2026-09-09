"""Opaque JSON cursor encoding; callers retain their query and identity checks."""

import base64
import json


def encode_cursor(payload) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def decode_cursor(token: str, *, max_length: int = 2048):
    if not isinstance(token, str) or len(token) > max_length:
        raise ValueError("Invalid pagination cursor")
    try:
        return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid pagination cursor") from error
