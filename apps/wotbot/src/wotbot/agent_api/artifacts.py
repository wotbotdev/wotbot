"""Durable generated artifact metadata and capability lookup.

Bytes are not stored here. Exported files stay in the code executor's artifact
store and are streamed on demand; panel markup stays in ``panel_versions`` and
is re-wrapped at read time. These rows bind an artifact to its owning API key
and record when its download link expires.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import undefer

from wotbot.agent_api.models import AgentArtifactRecord
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now


@dataclass(frozen=True, slots=True)
class ArtifactPage:
    items: list[AgentArtifactRecord]
    has_more: bool


class ArtifactStore:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    def _sessions(self):
        return self.session_factory or get_session_factory()

    async def put(
        self,
        *,
        artifact_id: str,
        task_id: str,
        owner: str,
        name: str,
        media_type: str,
        metadata: dict[str, Any],
        executor_artifact_id: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._put,
            artifact_id,
            task_id,
            owner,
            name,
            media_type,
            metadata,
            executor_artifact_id,
        )

    def _put(
        self,
        artifact_id: str,
        task_id: str,
        owner: str,
        name: str,
        media_type: str,
        metadata: dict[str, Any],
        executor_artifact_id: str | None,
    ) -> None:
        with self._sessions()() as session:
            session.merge(
                AgentArtifactRecord(
                    id=artifact_id,
                    task_id=task_id,
                    owner=owner,
                    executor_artifact_id=executor_artifact_id,
                    name=name,
                    media_type=media_type,
                    artifact_metadata=metadata,
                    created_at=utc_now(),
                    panel_version_id=metadata.get("panelVersionId"),
                    expires_at=datetime.fromisoformat(metadata["expiresAt"])
                    if metadata.get("expiresAt")
                    else None,
                )
            )
            session.commit()

    async def get(
        self, artifact_id: str, *, owner: str, include_content=False
    ) -> AgentArtifactRecord | None:
        return await asyncio.to_thread(self._get, artifact_id, owner, include_content)

    def _get(
        self, artifact_id: str, owner: str, include_content: bool
    ) -> AgentArtifactRecord | None:
        with self._sessions()() as session:
            query = select(AgentArtifactRecord).where(
                AgentArtifactRecord.id == artifact_id,
                AgentArtifactRecord.owner == owner,
            )
            if include_content:
                query = query.options(undefer(AgentArtifactRecord.legacy_content))
            row = session.scalar(query)
            if row is not None:
                session.expunge(row)
            return row

    async def list_mcp_apps(
        self,
        *,
        owner: str,
        limit: int,
        before: tuple[datetime, str] | None = None,
    ) -> ArtifactPage:
        return await asyncio.to_thread(self._list_mcp_apps, owner, limit, before)

    def _list_mcp_apps(
        self,
        owner: str,
        limit: int,
        before: tuple[datetime, str] | None,
    ) -> ArtifactPage:
        from wotbot.agent_api.constants import MCP_APP_MIME_TYPE

        query = select(AgentArtifactRecord).where(
            AgentArtifactRecord.owner == owner,
            AgentArtifactRecord.media_type == MCP_APP_MIME_TYPE,
            AgentArtifactRecord.artifact_metadata["bridgeVersion"].as_integer() == 2,
        )
        if before is not None:
            created_at, artifact_id = before
            query = query.where(
                or_(
                    AgentArtifactRecord.created_at < created_at,
                    and_(
                        AgentArtifactRecord.created_at == created_at,
                        AgentArtifactRecord.id < artifact_id,
                    ),
                )
            )
        query = query.order_by(
            AgentArtifactRecord.created_at.desc(),
            AgentArtifactRecord.id.desc(),
        ).limit(limit + 1)
        with self._sessions()() as session:
            rows = list(session.scalars(query))
            has_more = len(rows) > limit
            rows = rows[:limit]
            for row in rows:
                session.expunge(row)
            return ArtifactPage(items=rows, has_more=has_more)

    async def list_artifacts(self, *, owner, limit=50, before=None, task_id=None, context_id=None):
        return await asyncio.to_thread(
            self._list_artifacts, owner, limit, before, task_id, context_id
        )

    def _list_artifacts(self, owner, limit, before, task_id, context_id):
        from wotbot.agent_api.models import AgentTaskRecord

        query = select(AgentArtifactRecord).where(AgentArtifactRecord.owner == owner)
        if task_id:
            query = query.where(AgentArtifactRecord.task_id == task_id)
        if context_id:
            query = query.where(
                AgentArtifactRecord.task_id.in_(
                    select(AgentTaskRecord.id).where(
                        AgentTaskRecord.owner == owner,
                        AgentTaskRecord.payload["contextId"].as_string() == context_id,
                    )
                )
            )
        if before:
            created, identifier = before
            query = query.where(
                or_(
                    AgentArtifactRecord.created_at < created,
                    and_(
                        AgentArtifactRecord.created_at == created,
                        AgentArtifactRecord.id < identifier,
                    ),
                )
            )
        with self._sessions()() as session:
            rows = list(
                session.scalars(
                    query.order_by(
                        AgentArtifactRecord.created_at.desc(), AgentArtifactRecord.id.desc()
                    ).limit(limit + 1)
                )
            )
            for row in rows:
                session.expunge(row)
            return ArtifactPage(rows[:limit], len(rows) > limit)

    async def remember_subscription(
        self, artifact_id: str, *, owner: str, subscription_id: str
    ) -> None:
        await asyncio.to_thread(self._edit_subscriptions, artifact_id, owner, subscription_id, True)

    async def forget_subscription(
        self, artifact_id: str, *, owner: str, subscription_id: str
    ) -> None:
        await asyncio.to_thread(
            self._edit_subscriptions, artifact_id, owner, subscription_id, False
        )

    def _edit_subscriptions(
        self, artifact_id: str, owner: str, subscription_id: str, remember: bool
    ) -> None:
        """Maintain the allowlist of subscriptions this panel opened.

        Only membership is tracked. Stream position is carried by the panel and
        passed back with each poll, so an active subscription writes nothing.
        """
        with self._sessions()() as session:
            row = session.scalar(
                select(AgentArtifactRecord)
                .with_for_update()
                .where(
                    AgentArtifactRecord.id == artifact_id,
                    AgentArtifactRecord.owner == owner,
                )
            )
            if row is None:
                return
            metadata = dict(row.artifact_metadata or {})
            current = list(metadata.get("subscriptions") or [])
            if remember:
                if subscription_id in current:
                    return
                updated = [*current, subscription_id]
            else:
                updated = [value for value in current if value != subscription_id]
                if len(updated) == len(current):
                    return
            metadata["subscriptions"] = updated
            row.artifact_metadata = metadata
            session.commit()
