"""Short-lived download capabilities presented alongside durable agent artifacts."""

import asyncio
from urllib.parse import urlencode

from wotbot.agent_api.artifacts import ArtifactStore
from wotbot.auth.models import User
from wotbot.auth.providers import get_agent_principal
from wotbot.core.artifact_grants import ArtifactGrantStore
from wotbot.core.time import utc_now


class InvalidDownloadLink(ValueError):
    pass


class ArtifactDownloadLinks:
    def __init__(self, settings, *, artifact_store=None, session_factory=None):
        self.settings = settings
        self.artifacts = artifact_store or ArtifactStore(session_factory)
        self.session_factory = session_factory
        self.grants = ArtifactGrantStore(settings.redis_url, namespace="a2a:download:")

    async def _issue(self, record):
        grant = await self.grants.issue(
            artifact_id=record.id,
            owner=record.owner,
            ttl_seconds=self.settings.a2a_download_url_ttl_seconds,
            expires_at=record.expires_at,
        )
        return (grant.token, grant.expires_at) if grant else None

    async def authorize(self, token: str, artifact_id: str) -> User | None:
        grant = await self.grants.resolve(token)
        if grant is None or grant.artifact_id != artifact_id:
            raise InvalidDownloadLink()
        return await asyncio.to_thread(
            get_agent_principal,
            grant.owner,
            session_factory=self.session_factory,
            auth_type="artifact_grant",
        )

    async def descriptor(self, owner, record, *, path="/agent/artifacts"):
        """Present a record already loaded for this response; never cache it across requests."""
        if record is None or record.owner != owner:
            raise ValueError("Artifact not found")
        if record.expires_at and record.expires_at <= utc_now():
            raise ValueError("Artifact has expired")
        descriptor = {
            "artifactId": record.id,
            "name": record.name,
            "mimeType": record.media_type,
            **record.artifact_metadata,
        }
        if record.expires_at:
            descriptor["expiresAt"] = record.expires_at.isoformat()
        if (
            not record.panel_version_id
            and "artifact" not in record.artifact_metadata
            and record.expires_at
        ):
            issued = await self._issue(record)
            if issued:
                token, expiry = issued
                canonical = f"{self.settings.registry_public_url.rstrip('/')}{path}/{record.id}"
                descriptor.update(
                    authenticatedDownloadUrl=canonical,
                    downloadUrl=canonical + "?" + urlencode({"downloadToken": token}),
                    downloadUrlExpiresAt=expiry.isoformat(),
                )
        return descriptor
