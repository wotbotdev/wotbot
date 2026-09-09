"""Short-lived MCP App launch grants.

The same capability-token shape as artifact download links in
``wotbot.agent_api.downloads``, and held the same way: a hashed key in Redis with a
TTL. Losing one to a Redis restart costs the viewer a reopened panel, which is
why it does not need the durability the task tables have.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from wotbot.core.time import utc_now
from wotbot.discovery.store import client_for

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class IssuedPanelGrant:
    token: str
    artifact_id: str
    owner: str
    expires_at: datetime


class PanelGrantStore:
    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url

    @staticmethod
    def _key(token: str) -> str:
        return "mcp:panel-grant:" + hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, *, artifact_id: str, owner: str, ttl_seconds: int) -> IssuedPanelGrant:
        expires_at = utc_now() + timedelta(seconds=ttl_seconds)
        token = secrets.token_urlsafe(32)
        await client_for(self.redis_url).set(
            self._key(token),
            json.dumps(
                {
                    "artifactId": artifact_id,
                    "owner": owner,
                    "expiresAt": expires_at.isoformat(),
                }
            ),
            px=ttl_seconds * 1000,
        )
        return IssuedPanelGrant(
            token=token, artifact_id=artifact_id, owner=owner, expires_at=expires_at
        )

    async def resolve(self, token: str, *, owner: str) -> IssuedPanelGrant | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        payload = await client_for(self.redis_url).get(self._key(token))
        if not payload:
            return None
        grant = json.loads(payload)
        expires_at = datetime.fromisoformat(grant["expiresAt"])
        if grant.get("owner") != owner or expires_at <= utc_now():
            return None
        return IssuedPanelGrant(
            token=token,
            artifact_id=grant["artifactId"],
            owner=grant["owner"],
            expires_at=expires_at,
        )
