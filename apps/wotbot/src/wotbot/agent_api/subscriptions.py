"""Owned raw subscription handles with renewable idle leases."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, select

from wotbot.agent_api.models import AgentSubscriptionRecord
from wotbot.auth.providers import get_agent_principal
from wotbot.clients.runtime_stream import (
    next_subscription_event,
    requested_cursor,
    runtime_stream_tail,
    subscription_id_from_result,
)
from wotbot.clients.wot_runtime import WotRuntimeClient
from wotbot.core.agent_runs import RunRegistry
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now


class RawSubscriptions:
    TOOLS = {"wot_observe_property", "wot_subscribe_event", "wot_remove_subscription"}

    def __init__(self, settings, *, client=None, session_factory=None):
        self.settings = settings
        self.client = client or WotRuntimeClient(settings)
        self.sessions = session_factory or get_session_factory()
        # One lock per raw context. A runtime call for one Thing may take as
        # long as its subscribe timeout; nothing outside that context waits.
        self.locks = RunRegistry()

    def _owned(self, owner, context_id, identifier, *, renew=False):
        with self.sessions() as session, session.begin():
            row = session.scalar(
                select(AgentSubscriptionRecord)
                .where(
                    AgentSubscriptionRecord.id == identifier,
                    AgentSubscriptionRecord.owner == owner,
                    AgentSubscriptionRecord.context_id == context_id,
                    AgentSubscriptionRecord.expires_at > utc_now(),
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("Subscription not found or expired; create a new subscription")
            if renew:
                row.expires_at = utc_now() + timedelta(hours=1)
            return row.runtime_id

    def _remember(self, owner, context_id, runtime_id):
        with self.sessions() as session, session.begin():
            row = session.scalar(
                select(AgentSubscriptionRecord).where(
                    AgentSubscriptionRecord.owner == owner,
                    AgentSubscriptionRecord.context_id == context_id,
                    AgentSubscriptionRecord.runtime_id == runtime_id,
                )
            )
            if row is None:
                row = AgentSubscriptionRecord(
                    id=str(uuid4()), owner=owner, context_id=context_id, runtime_id=runtime_id
                )
                session.add(row)
            row.expires_at = utc_now() + timedelta(hours=1)
            return row.id

    def _lapsed(self, identifier, *, authorized):
        """Re-read under the caller's lock: a poll may have renewed the lease."""
        with self.sessions() as session:
            row = session.get(AgentSubscriptionRecord, identifier)
            if row is None or (authorized and row.expires_at > utc_now()):
                return None
            return row.runtime_id

    def _forget(self, identifier):
        with self.sessions() as session, session.begin():
            session.execute(
                delete(AgentSubscriptionRecord).where(AgentSubscriptionRecord.id == identifier)
            )

    async def execute(self, name, arguments, config):
        identity = config["configurable"]
        owner, context_id = identity["owner"], identity["thread_id"]
        async with self.locks.thread_lock(context_id):
            if name == "wot_remove_subscription":
                identifier = arguments["subscription_id"]
                runtime_id = await asyncio.to_thread(self._owned, owner, context_id, identifier)
                result = await self.client.remove_subscription(
                    **{**arguments, "subscription_id": runtime_id}
                )
                await asyncio.to_thread(self._forget, identifier)
                return result
            cursor = await runtime_stream_tail(
                redis_url=self.settings.redis_url, stream=self.settings.wot_runtime_stream
            )
            method = (
                self.client.observe_property
                if name == "wot_observe_property"
                else self.client.subscribe_event
            )
            result = await method(**arguments, subscription_namespace=f"mcp-raw:{context_id}")
            runtime_id = subscription_id_from_result(result)
            if runtime_id is None:
                return result
            identifier = await asyncio.to_thread(self._remember, owner, context_id, runtime_id)
            return {**_replace_id(result, runtime_id, identifier), "cursor": cursor}

    async def poll(self, owner, context_id, identifier, *, cursor=None, timeout_ms=25000):
        async with self.locks.thread_lock(context_id):
            runtime_id = await asyncio.to_thread(
                self._owned, owner, context_id, identifier, renew=True
            )
            status = await self.client.subscription_status(runtime_id)
            if not status.get("exists"):
                await asyncio.to_thread(self._forget, identifier)
                raise ValueError("Runtime subscription was lost; create a new subscription")
        cursor = requested_cursor({"cursor": cursor})
        if cursor is None:
            cursor = await runtime_stream_tail(
                redis_url=self.settings.redis_url, stream=self.settings.wot_runtime_stream
            )
        result, cursor = await next_subscription_event(
            redis_url=self.settings.redis_url,
            stream=self.settings.wot_runtime_stream,
            subscription_id=runtime_id,
            cursor=cursor,
            timeout_ms=timeout_ms,
        )
        # A cancellation or revocation during a long poll must not release more data.
        await asyncio.to_thread(self._owned, owner, context_id, identifier)
        if (
            await asyncio.to_thread(get_agent_principal, owner, session_factory=self.sessions)
            is None
        ):
            raise ValueError("The invoking API key is no longer authorized")
        return {**_replace_id(result, runtime_id, identifier), "nextCursor": cursor}

    async def sweep(self):
        with self.sessions() as session:
            rows = list(session.scalars(select(AgentSubscriptionRecord)))
        for row in rows:
            authorized = (
                await asyncio.to_thread(
                    get_agent_principal, row.owner, session_factory=self.sessions
                )
            ) is not None
            if authorized and row.expires_at > utc_now():
                continue
            async with self.locks.thread_lock(row.context_id):
                runtime_id = await asyncio.to_thread(self._lapsed, row.id, authorized=authorized)
                if runtime_id is None:
                    continue
                try:
                    status = await self.client.subscription_status(runtime_id)
                    if status.get("exists"):
                        await self.client.remove_subscription(subscription_id=runtime_id)
                except Exception:
                    # Retain the binding for the next cleanup attempt.
                    continue
                await asyncio.to_thread(self._forget, row.id)


def _replace_id(value, runtime_id, public_id):
    if isinstance(value, dict):
        return {
            key: _replace_id(item, runtime_id, public_id)
            for key, item in value.items()
            if key != "streamName"
        }
    if isinstance(value, list):
        return [_replace_id(item, runtime_id, public_id) for item in value]
    return public_id if value == runtime_id else value
