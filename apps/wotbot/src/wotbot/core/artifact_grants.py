"""Expiring capability tokens for temporary artifact download links."""

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
class ArtifactGrant:
    token: str
    artifact_id: str
    owner: str
    expires_at: datetime


class ArtifactGrantStore:
    def __init__(self, redis_url: str, *, namespace: str):
        self.redis_url = redis_url
        self.namespace = namespace

    def key(self, token: str) -> str:
        return self.namespace + hashlib.sha256(token.encode()).hexdigest()

    async def issue(self, *, artifact_id, owner, ttl_seconds, expires_at=None):
        now = utc_now()
        expiry = now + timedelta(seconds=ttl_seconds)
        if expires_at is not None:
            expiry = min(expiry, expires_at)
        ttl_ms = int((expiry - now).total_seconds() * 1000)
        if ttl_ms <= 0:
            return None
        token = secrets.token_urlsafe(32)
        await client_for(self.redis_url).set(
            self.key(token),
            json.dumps(
                {"artifactId": artifact_id, "owner": owner, "expiresAt": expiry.isoformat()}
            ),
            px=ttl_ms,
        )
        return ArtifactGrant(token, artifact_id, owner, expiry)

    async def resolve(self, token: str, *, owner: str | None = None) -> ArtifactGrant | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        payload = await client_for(self.redis_url).get(self.key(token))
        if not payload:
            return None
        grant = json.loads(payload)
        expires_at = datetime.fromisoformat(grant["expiresAt"])
        if expires_at <= utc_now() or (owner is not None and grant["owner"] != owner):
            return None
        return ArtifactGrant(token, grant["artifactId"], grant["owner"], expires_at)
