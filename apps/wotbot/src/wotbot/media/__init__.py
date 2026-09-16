"""LiveKit media support for direct camera context.

The media package provides a session registry for camera streams and helpers to
encode raw frames into JPEG payloads for foreground model prompts.
"""

from wotbot.media.ingress import MediaSessionRegistry, encode_frame_to_jpeg, media_sessions
from wotbot.media.models import MediaSessionStats
from wotbot.media.snapshot_notifications import (
    SNAPSHOT_EVENT_TYPE,
    SNAPSHOT_TOPIC,
    SnapshotNotifierRegistry,
    snapshot_notifiers,
)

__all__ = [
    "SNAPSHOT_EVENT_TYPE",
    "SNAPSHOT_TOPIC",
    "MediaSessionRegistry",
    "MediaSessionStats",
    "SnapshotNotifierRegistry",
    "encode_frame_to_jpeg",
    "media_sessions",
    "snapshot_notifiers",
]
