"""Transactional admission and durable agent tasks, isolated by API key ID."""

# Deferred annotations: this class defines a `list` method, which would
# otherwise shadow the builtin in its own siblings' return annotations.
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from wotbot.agent_api.errors import InvalidParamsError, TaskNotFoundError
from wotbot.agent_api.interrupts import validate_reply
from wotbot.agent_api.models import (
    AgentArtifactRecord,
    AgentMessageRecord,
    AgentSubscriptionRecord,
    AgentTaskRecord,
)
from wotbot.agent_api.types import Message, Operation, Part, Task, TaskQuery, TaskState
from wotbot.core.cursors import decode_cursor, encode_cursor
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.threads.models import Thread, ThreadKind

ACTIVE = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
PAUSED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
RESERVED = ACTIVE | PAUSED
AGENT_THREAD_TITLE = "Agent conversation"


def task_json(task: Task) -> dict:
    return {**task.json(), "payloadVersion": 2}


def task_from_json(payload: dict) -> Task:
    return Task.model_validate({k: v for k, v in payload.items() if k != "payloadVersion"})


def task_identity(owner: str, message_id: str, family: str = "assistant") -> str:
    """Keep assistant IDs stable and isolate other families by UUID namespace.

    Message records resolve retained retries before this is called, including
    raw tasks issued under an earlier ID scheme.
    """
    namespace = NAMESPACE_URL
    if family != "assistant":
        namespace = uuid5(NAMESPACE_URL, f"wotbot:agent-tasks:{family}")
    return str(uuid5(namespace, f"wotbot:a2a:{owner}:{message_id}"))


def request_fingerprint(request: dict, *, legacy: bool = False) -> str:
    content = {"message": request["message"], "metadata": request.get("metadata", {})}
    if request.get("operation"):
        content["operation"] = request["operation"]
    if legacy:
        # Original 0008 records also hashed this configuration field. Do not
        # invalidate their retry identities during the storage migration.
        content["acceptedOutputModes"] = request.get("configuration", {}).get(
            "acceptedOutputModes", []
        )
    return hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


@dataclass
class Admission:
    task: Task
    thread_id: str
    execute: bool
    resume: dict[str, Any] | None = None
    operation: Operation = dataclass_field(default_factory=Operation)


class TaskStore:
    def __init__(self, *, retention_days: int = 30, session_factory=None):
        self.retention_days = retention_days
        self.session_factory = session_factory or get_session_factory()
        self._execution_lease = None

    def acquire_execution_lease(self) -> None:
        """Fail before recovery if another API process owns execution in this DB."""
        with self.session_factory() as session:
            engine = session.get_bind()
        connection = engine.connect()
        try:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(1936286819, 2)"))
            connection.commit()
            if not acquired:
                raise RuntimeError(
                    "Agent execution requires one API execution process per database"
                )
        except BaseException:
            connection.close()
            raise
        self._execution_lease = connection

    def release_execution_lease(self) -> None:
        connection, self._execution_lease = self._execution_lease, None
        if connection is not None:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(1936286819, 2)"))
                connection.commit()
            finally:
                connection.close()

    def admit(self, owner: str, request: dict) -> Admission:
        operation = Operation.model_validate(request.get("operation") or {})
        family = operation.family
        message = request["message"]
        message_id = message["messageId"]
        fingerprint = request_fingerprint(request)
        now = utc_now()
        with self.session_factory() as session, session.begin():
            # The composite primary key is the durable guarantee. Serialize
            # competing inserts too, so simultaneous identical retries return
            # the winner instead of failing a task/context uniqueness check.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
                {"identity": f"a2a-message:{owner}:{family}:{message_id}"},
            )
            prior = session.get(
                AgentMessageRecord,
                {"owner": owner, "family": family, "message_id": message_id},
            )
            if prior is not None:
                row = session.get(AgentTaskRecord, prior.task_id)
                if row.expires_at > now or row.state in ACTIVE:
                    if prior.request_hash not in {
                        fingerprint,
                        request_fingerprint(request, legacy=True),
                    }:
                        raise InvalidParamsError(
                            "messageId was already used with different content"
                        )
                    self._check_family(row, family)
                    return Admission(
                        task_from_json(row.payload),
                        row.context_id,
                        False,
                        operation=Operation.model_validate(row.operation or {}),
                    )
                session.delete(row)  # Cascades its expired retry identities.
                session.flush()
                # The cascade deleted this record in the database, but the loaded
                # copy stays in the identity map and would collide with the
                # identity re-added below.
                session.expunge(prior)
            if message.get("taskId"):
                row = self._owned(session, owner, message["taskId"])
                self._check_family(row, family)
                operation = Operation.model_validate(row.operation or {})
                task = task_from_json(row.payload)
                if message.get("contextId") and message["contextId"] != task.context_id:
                    raise InvalidParamsError("taskId and contextId do not match")
                if row.state not in PAUSED:
                    raise InvalidParamsError(
                        "Only the corresponding paused task accepts a continuation"
                    )
                resume = validate_reply(row.pending or [], message["parts"])
                row.pending = None
            else:
                resume = None
                task_id = task_identity(owner, message_id, family)
                row = session.get(AgentTaskRecord, task_id)
                if row is not None:
                    if row.expires_at > now or row.state in ACTIVE:
                        raise InvalidParamsError(
                            "messageId was already used with different content"
                        )
                    # Retained only until the next sweep; make way for the retry.
                    session.delete(row)
                    session.flush()
                thread_id, context_id = self._context(
                    session, owner, message.get("contextId"), now, family
                )
                task = Task(id=task_id, context_id=context_id)
                row = AgentTaskRecord(
                    id=task_id,
                    context_id=thread_id,
                    owner=owner,
                    family=family,
                    operation=operation.json(),
                    origin=request.get("origin", "a2a"),
                )
                session.add(row)
            # Only server-generated identity enters the graph; message identity remains public.
            normalized = {**message, "contextId": task.context_id, "taskId": row.id}
            task.history.append(Message.model_validate(normalized))
            task.status.state = TaskState.TASK_STATE_SUBMITTED
            task.status.message = None
            task.status.timestamp = now
            row.state = "TASK_STATE_SUBMITTED"
            row.payload = task_json(task)
            row.updated_at = now
            row.expires_at = now + timedelta(days=self.retention_days)
            try:
                session.flush()
            except IntegrityError as exc:
                if "uq_a2a_tasks_active_context" in str(exc.orig):
                    raise InvalidParamsError(
                        "This context already has an executing or suspended task"
                    ) from exc
                raise
            session.add(
                AgentMessageRecord(
                    owner=owner,
                    family=family,
                    message_id=message_id,
                    request_hash=fingerprint,
                    task_id=row.id,
                )
            )
            return Admission(task, row.context_id, True, resume, operation)

    def _context(
        self, session, owner: str, context_id: str | None, now: datetime, family: str = "assistant"
    ) -> tuple[str, str]:
        """Resolve or open the hidden thread that backs this A2A context."""
        kind = ThreadKind.MCP_RAW.value if family == "raw" else ThreadKind.A2A.value
        if context_id:
            thread = session.scalar(
                select(Thread).where(
                    func.coalesce(Thread.legacy_a2a_context_id, Thread.id) == context_id,
                    Thread.owner_api_key_id == owner,
                    Thread.kind == kind,
                )
            )
            if thread is None or thread.kind != kind or thread.owner_api_key_id != owner:
                raise InvalidParamsError("Unknown contextId")
            return thread.id, context_id
        thread = Thread(
            id=str(uuid4()),
            title=AGENT_THREAD_TITLE,
            kind=kind,
            visible=False,
            owner_api_key_id=owner,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        session.add(thread)
        session.flush()
        return thread.id, thread.id

    @staticmethod
    def _check_family(row, family):
        if row.family != family:
            raise TaskNotFoundError()

    def operation(self, owner, task_id, *, family=None):
        with self.session_factory() as session:
            return Operation.model_validate(
                self._owned(session, owner, task_id, family).operation or {}
            )

    def _owned(self, session, owner: str, task_id: str, family=None) -> AgentTaskRecord:
        row = session.scalar(
            select(AgentTaskRecord).where(
                AgentTaskRecord.id == task_id,
                AgentTaskRecord.owner == owner,
                AgentTaskRecord.expires_at > utc_now(),
            )
        )
        if row is None:
            raise TaskNotFoundError()
        if family is not None:
            self._check_family(row, family)
        return row

    def get(self, owner: str, task_id: str, family: str | None = None) -> Task:
        with self.session_factory() as session:
            row = self._owned(session, owner, task_id, family)
            return task_from_json(row.payload)

    def thread_id(self, owner: str, task_id: str) -> str:
        with self.session_factory() as session:
            return self._owned(session, owner, task_id).context_id

    def save(self, owner: str, task: Task, pending: list[dict] | None = None) -> None:
        with self.session_factory() as session, session.begin():
            row = self._owned(session, owner, task.id)
            row.payload = task_json(task)
            row.state = TaskState.Name(task.status.state)
            row.pending = pending
            row.updated_at = task.status.timestamp
            row.expires_at = utc_now() + timedelta(days=self.retention_days)

    def list(self, owner: str, params: TaskQuery) -> tuple[list[Task], int, str]:
        if params.page_size < 0 or params.page_size > 100:
            raise InvalidParamsError("pageSize must be between 1 and 100")
        size = params.page_size or 50
        filter_hash = hashlib.sha256(
            json.dumps(
                [
                    owner,
                    params.family,
                    params.context_id,
                    params.status,
                    params.status_timestamp_after.isoformat()
                    if params.status_timestamp_after is not None
                    else None,
                ]
            ).encode()
        ).hexdigest()
        cursor = None
        if params.page_token:
            try:
                decoded = decode_cursor(params.page_token, max_length=1024)
                updated, identifier, filters = decoded
                cursor = (datetime.fromisoformat(updated), str(identifier))
                if not cursor[0].tzinfo or filters != filter_hash:
                    raise ValueError("Query does not match pageToken")
            except (ValueError, TypeError) as exc:
                raise InvalidParamsError("Invalid pageToken for this query") from exc
        query = select(AgentTaskRecord).where(
            AgentTaskRecord.owner == owner,
            AgentTaskRecord.expires_at > utc_now(),
            AgentTaskRecord.family == params.family,
        )
        if params.context_id:
            query = query.where(
                AgentTaskRecord.payload["contextId"].as_string() == params.context_id
            )
        if params.status:
            query = query.where(AgentTaskRecord.state == TaskState.Name(params.status))
        if params.status_timestamp_after is not None:
            query = query.where(AgentTaskRecord.updated_at >= params.status_timestamp_after)

        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            if cursor:
                updated, identifier = cursor
                query = query.where(
                    or_(
                        AgentTaskRecord.updated_at < updated,
                        and_(
                            AgentTaskRecord.updated_at == updated, AgentTaskRecord.id < identifier
                        ),
                    )
                )
            rows = list(
                session.scalars(
                    query.order_by(
                        AgentTaskRecord.updated_at.desc(), AgentTaskRecord.id.desc()
                    ).limit(size + 1)
                )
            )
            page, has_more = rows[:size], len(rows) > size
            token = ""
            if has_more:
                last = page[-1]
                token = encode_cursor([last.updated_at.isoformat(), last.id, filter_hash])
            return [task_from_json(row.payload) for row in page], total, token

    def recover(self) -> int:
        """An interrupted process may have already performed a device action. Never retry it."""

        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(select(AgentTaskRecord).where(AgentTaskRecord.state.in_(ACTIVE)))
            )
            for row in rows:
                task = task_from_json(row.payload)
                task.status.state = TaskState.TASK_STATE_FAILED
                task.status.timestamp = utc_now()
                task.status.message = Message(
                    message_id=str(uuid4()),
                    role="ROLE_AGENT",
                    context_id=task.context_id,
                    task_id=task.id,
                    parts=[
                        Part(
                            text="The server stopped during execution. The outcome of device actions is uncertain; inspect device state before submitting new work. This task was not retried."
                        )
                    ],
                )
                row.payload, row.state, row.updated_at = (
                    task_json(task),
                    "TASK_STATE_FAILED",
                    utc_now(),
                )
            return len(rows)

    def prune(self) -> list[str]:
        """Delete expired tasks and their tombstones.

        Returns the IDs of A2A threads that no longer have any task, so the
        caller can drop their graph checkpoints before ``delete_contexts``
        removes the rows. Doing it in that order means a crash in between
        retries the sweep rather than orphaning a checkpoint.
        """
        now = utc_now()
        with self.session_factory() as session, session.begin():
            session.execute(
                update(AgentArtifactRecord)
                .where(
                    AgentArtifactRecord.expires_at <= now,
                    AgentArtifactRecord.legacy_content.is_not(None),
                )
                .values(legacy_content=None)
            )
            session.execute(
                delete(AgentTaskRecord).where(
                    AgentTaskRecord.expires_at <= now, ~AgentTaskRecord.state.in_(ACTIVE)
                )
            )
            session.flush()
            # A download manifest stays available to explain a 410 for as long
            # as its task is retained, then goes with it. Panels outlive both.
            session.execute(
                delete(AgentArtifactRecord).where(
                    AgentArtifactRecord.panel_version_id.is_(None),
                    AgentArtifactRecord.expires_at <= now,
                    AgentArtifactRecord.task_id.not_in(select(AgentTaskRecord.id)),
                )
            )
            return list(
                session.scalars(
                    select(Thread.id).where(
                        Thread.kind.in_([ThreadKind.A2A.value, ThreadKind.MCP_RAW.value]),
                        Thread.id.not_in(select(AgentTaskRecord.context_id)),
                        Thread.id.not_in(select(AgentSubscriptionRecord.context_id)),
                    )
                )
            )

    def delete_contexts(self, thread_ids: list[str]) -> int:
        """Remove retired A2A threads once their checkpoints are gone."""
        if not thread_ids:
            return 0
        with self.session_factory() as session, session.begin():
            result = session.execute(
                delete(Thread).where(
                    Thread.kind.in_([ThreadKind.A2A.value, ThreadKind.MCP_RAW.value]),
                    Thread.id.in_(thread_ids),
                    Thread.id.not_in(select(AgentTaskRecord.context_id)),
                    Thread.id.not_in(select(AgentSubscriptionRecord.context_id)),
                )
            )
            return int(result.rowcount or 0)
