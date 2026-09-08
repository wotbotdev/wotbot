"""Short-lived discovery capabilities held in Redis.

Candidates, downloads, and refresh previews are all capability tokens: an
unguessable id, a fixed lifetime, and an owner check on read. They share one
pooled client per Redis URL and event loop, because opening and closing a
connection per operation meant a search that returned twenty-five candidates
opened twenty-five connections.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from dataclasses import asdict
from typing import Any

import redis.asyncio as redis

from wotbot.discovery.models import CandidateRecord, DownloadRecord, RefreshRecord

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9_-]{32}$")
_DOWNLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_REFRESH_ID = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MISSING_CANDIDATE = (
    "Candidate was not found or has expired. Repeat discovery against "
    "the same source in the current session; do not invent a candidate ID."
)

# Keyed by the event loop as well as the URL: a redis.asyncio client binds to
# the loop that created it, and tests run each case on a fresh loop. Holding the
# loop object (rather than its id) keeps a finished loop's address from being
# reused for a live entry.
_clients: dict[tuple[str, asyncio.AbstractEventLoop], Any] = {}


def client_for(redis_url: str) -> Any:
    """Return the pooled client for this URL on the running loop."""

    loop = asyncio.get_running_loop()
    for stale in [key for key in _clients if key[1].is_closed()]:
        _clients.pop(stale, None)
    client = _clients.get((redis_url, loop))
    if client is None:
        client = redis.from_url(redis_url, decode_responses=True)
        _clients[(redis_url, loop)] = client
    return client


async def reset_clients() -> None:
    """Close every pooled client. Intended for shutdown and tests."""

    clients = list(_clients.values())
    _clients.clear()
    for client in clients:
        await client.aclose()


def _encode(record: Any) -> str:
    return json.dumps(asdict(record), separators=(",", ":"))


class CandidateStore:
    def __init__(self, redis_url: str, *, ttl_seconds: int = 1800) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds

    async def put(self, candidate: CandidateRecord) -> str:
        [candidate_id] = await self.put_many([candidate])
        return candidate_id

    async def put_many(self, candidates: list[CandidateRecord]) -> list[str]:
        """Store a whole page of candidates in one round trip."""

        if not candidates:
            return []
        identifiers = [secrets.token_urlsafe(24) for _ in candidates]
        client = client_for(self._redis_url)
        pipeline = client.pipeline(transaction=False)
        for candidate_id, candidate in zip(identifiers, candidates, strict=True):
            pipeline.set(
                f"discovery:candidate:{candidate_id}",
                _encode(candidate),
                ex=self._ttl_seconds,
            )
        await pipeline.execute()
        return identifiers

    async def get(
        self,
        candidate_id: str,
        *,
        scope_kind: str,
        scope_id: str,
    ) -> CandidateRecord:
        if not _CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError(_MISSING_CANDIDATE)
        payload = await client_for(self._redis_url).get(f"discovery:candidate:{candidate_id}")
        if not payload:
            raise ValueError(_MISSING_CANDIDATE)
        candidate = CandidateRecord.from_dict(json.loads(payload))
        if candidate.scope_kind != scope_kind or candidate.scope_id != scope_id:
            raise ValueError(
                "Candidate belongs to a different discovery scope. Repeat discovery "
                "against the same source in the current session."
            )
        return candidate


class DownloadStore:
    def __init__(self, redis_url: str, *, ttl_seconds: int = 300) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds

    async def put(self, download: DownloadRecord, *, ttl_seconds: int | None = None) -> str:
        handle = secrets.token_urlsafe(32)
        await client_for(self._redis_url).set(
            f"discovery:download:{handle}",
            _encode(download),
            ex=max(1, min(ttl_seconds or self._ttl_seconds, self._ttl_seconds)),
        )
        return handle

    async def get(self, handle: str) -> DownloadRecord:
        if not _DOWNLOAD_ID.fullmatch(handle):
            raise ValueError("Download was not found or has expired")
        payload = await client_for(self._redis_url).get(f"discovery:download:{handle}")
        if not payload:
            raise ValueError("Download was not found or has expired")
        return DownloadRecord.from_dict(json.loads(payload))


class RefreshStore:
    def __init__(self, redis_url: str, *, ttl_seconds: int = 600) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds

    async def put(self, refresh: RefreshRecord) -> str:
        refresh_id = secrets.token_urlsafe(32)
        await client_for(self._redis_url).set(
            f"discovery:refresh:{refresh_id}",
            _encode(refresh),
            ex=self._ttl_seconds,
        )
        return refresh_id

    async def get(self, refresh_id: str, *, user_id: str) -> RefreshRecord:
        if not _REFRESH_ID.fullmatch(refresh_id):
            raise ValueError("Refresh preview was not found or has expired")
        payload = await client_for(self._redis_url).get(f"discovery:refresh:{refresh_id}")
        if not payload:
            raise ValueError("Refresh preview was not found or has expired")
        refresh = RefreshRecord.from_dict(json.loads(payload))
        if refresh.user_id != user_id:
            raise ValueError("Refresh preview belongs to a different user")
        return refresh

    async def delete(self, refresh_id: str) -> None:
        if not _REFRESH_ID.fullmatch(refresh_id):
            return
        await client_for(self._redis_url).delete(f"discovery:refresh:{refresh_id}")
