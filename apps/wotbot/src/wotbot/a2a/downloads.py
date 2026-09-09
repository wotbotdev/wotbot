"""A2A SDK presentation of shared download capabilities."""

from urllib.parse import urlencode

from a2a.types import Artifact, Task, TaskArtifactUpdateEvent

from wotbot.agent_api.downloads import ArtifactDownloadLinks as SharedDownloadLinks
from wotbot.agent_api.downloads import InvalidDownloadLink as InvalidDownloadLink


class ArtifactDownloadLinks(SharedDownloadLinks):
    async def _present_artifact(self, artifact: Artifact, record):
        # Structured results and panel descriptors have no downloadable URL
        # parts. Panels keep their separate MCP launch-grant mechanism.
        if not any(part.HasField("url") for part in artifact.parts):
            return
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
        records = await self.artifacts.get_many(
            [a.artifact_id for a in artifacts if any(p.HasField("url") for p in a.parts)],
            owner=owner,
        )
        for artifact in artifacts:
            await self._present_artifact(artifact, records.get(artifact.artifact_id))
        return copy
