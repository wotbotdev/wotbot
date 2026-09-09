"""Short-lived download capabilities presented alongside durable A2A artifacts."""

import asyncio
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from a2a.types import Artifact, Task, TaskArtifactUpdateEvent

from wotbot.a2a.artifacts import ArtifactStore
from wotbot.api_keys.models import ApiKey
from wotbot.auth.models import User
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.discovery.store import client_for

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class InvalidDownloadLink(ValueError):
    pass


class ArtifactDownloadLinks:
    def __init__(self, settings, *, artifact_store=None, session_factory=None):
        self.settings = settings
        self.artifacts = artifact_store or ArtifactStore(session_factory)
        self.session_factory = session_factory

    @staticmethod
    def _key(token):
        return "a2a:download:" + hashlib.sha256(token.encode()).hexdigest()

    async def _issue(self, record):
        now = utc_now()
        expiry = min(
            now + timedelta(seconds=self.settings.a2a_download_url_ttl_seconds),
            record.expires_at,
        )
        ttl_ms = int((expiry - now).total_seconds() * 1000)
        if ttl_ms <= 0:
            return None
        token = secrets.token_urlsafe(32)
        await client_for(self.settings.redis_url).set(
            self._key(token),
            json.dumps(
                {
                    "artifactId": record.id,
                    "owner": record.owner,
                    "expiresAt": expiry.isoformat(),
                }
            ),
            px=ttl_ms,
        )
        return token, expiry

    async def authorize(self, token: str, artifact_id: str) -> User | None:
        if not _TOKEN.fullmatch(token):
            raise InvalidDownloadLink()
        payload = await client_for(self.settings.redis_url).get(self._key(token))
        if not payload:
            raise InvalidDownloadLink()
        grant = json.loads(payload)
        if (
            grant["artifactId"] != artifact_id
            or datetime.fromisoformat(grant["expiresAt"]) <= utc_now()
        ):
            raise InvalidDownloadLink()
        return await asyncio.to_thread(self._principal, grant["owner"])

    def _principal(self, owner):
        # A capability ceases to work when its original API key is revoked,
        # expires, or loses agent:invoke. The full key is never put in the URL.
        with (self.session_factory or get_session_factory())() as session:
            key = session.get(ApiKey, owner)
            if not key or not key.is_active or "agent:invoke" not in (key.scopes or []):
                return None
            if key.expires_at is not None and key.expires_at <= utc_now():
                return None
            return User(
                user_id=key.user_id,
                api_key_id=owner,
                auth_type="artifact_grant",
                scopes=["agent:invoke"],
            )

    async def _present_artifact(self, owner: str, artifact: Artifact):
        # Structured results and panel descriptors have no downloadable URL
        # parts. Panels keep their separate MCP launch-grant mechanism.
        if not any(part.HasField("url") for part in artifact.parts):
            return
        record = await self.artifacts.get(artifact.artifact_id, owner=owner)
        if record is None or record.panel_version_id or record.expires_at is None:
            return
        issued = await self._issue(record)
        if issued is None:
            return
        token, expiry = issued
        canonical = f"{self.settings.registry_public_url.rstrip('/')}/a2a/artifacts/{record.id}"
        temporary = canonical + "?" + urlencode({"downloadToken": token})
        for part in artifact.parts:
            if part.HasField("url"):
                part.url = temporary
        artifact.metadata["authenticatedDownloadUrl"] = canonical
        artifact.metadata["downloadUrl"] = temporary
        artifact.metadata["downloadUrlExpiresAt"] = expiry.isoformat()

    async def present(self, owner: str, event):
        # Never persist a temporary grant in a Task, or mutate a shared stream
        # event. Every authenticated retrieval can issue fresh links, including
        # for artifacts generated before this feature was enabled.
        if not isinstance(event, (Task, TaskArtifactUpdateEvent)):
            return event
        copy = type(event)()
        copy.CopyFrom(event)
        artifacts = copy.artifacts if isinstance(copy, Task) else [copy.artifact]
        for artifact in artifacts:
            await self._present_artifact(owner, artifact)
        return copy
