"""Transactional admission and durable A2A tasks, isolated by API key ID."""

# Deferred annotations: this class defines a `list` method, which would
# otherwise shadow the builtin in its own siblings' return annotations.
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from a2a.types import Task, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict, ParseDict
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from wotbot.a2a.interrupts import validate_reply
from wotbot.a2a.models import A2AArtifactRecord, A2AMessageRecord, A2ATaskRecord
from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.threads.models import Thread, ThreadKind

ACTIVE = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
PAUSED = {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
RESERVED = ACTIVE | PAUSED
A2A_THREAD_TITLE = "Agent conversation"


def task_json(task: Task) -> dict:
    return MessageToDict(task)


def task_from_json(payload: dict) -> Task:
    return ParseDict(payload, Task())


def task_identity(owner: str, message_id: str) -> str:
    """Keep the task IDs issued by the cleanup; message records handle retries."""
    return str(uuid5(NAMESPACE_URL, f"wotbot:a2a:{owner}:{message_id}"))


def request_fingerprint(request: dict, *, legacy: bool = False) -> str:
    content = {"message": request["message"], "metadata": request.get("metadata", {})}
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
                raise RuntimeError("A2A requires one API execution process per database")
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
                {"identity": f"a2a-message:{owner}:{message_id}"},
            )
            prior = session.get(A2AMessageRecord, (owner, message_id))
            if prior is not None:
                row = session.get(A2ATaskRecord, prior.task_id)
                if row.expires_at > now or row.state in ACTIVE:
                    if prior.request_hash not in {
                        fingerprint,
                        request_fingerprint(request, legacy=True),
                    }:
                        raise InvalidParamsError(
                            "messageId was already used with different content"
                        )
                    return Admission(task_from_json(row.payload), row.context_id, False)
                session.delete(row)  # Cascades its expired retry identities.
                session.flush()
            if message.get("taskId"):
                row = self._owned(session, owner, message["taskId"])
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
                task_id = task_identity(owner, message_id)
                row = session.get(A2ATaskRecord, task_id)
                if row is not None:
                    if row.expires_at > now or row.state in ACTIVE:
                        raise InvalidParamsError(
                            "messageId was already used with different content"
                        )
                    # Retained only until the next sweep; make way for the retry.
                    session.delete(row)
                    session.flush()
                thread_id, context_id = self._context(session, owner, message.get("contextId"), now)
                task = Task(id=task_id, context_id=context_id)
                row = A2ATaskRecord(id=task_id, context_id=thread_id, owner=owner)
                session.add(row)
            # Only server-generated identity enters the graph; message identity remains public.
            from a2a.types import Message

            normalized = {**message, "contextId": task.context_id, "taskId": row.id}
            task.history.append(ParseDict(normalized, Message()))
            task.status.state = TaskState.TASK_STATE_SUBMITTED
            task.status.ClearField("message")
            task.status.timestamp.FromDatetime(now)
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
                A2AMessageRecord(
                    owner=owner, message_id=message_id, request_hash=fingerprint, task_id=row.id
                )
            )
            return Admission(task, row.context_id, True, resume)

    def _context(
        self, session, owner: str, context_id: str | None, now: datetime
    ) -> tuple[str, str]:
        """Resolve or open the hidden thread that backs this A2A context."""
        if context_id:
            thread = session.scalar(
                select(Thread).where(
                    func.coalesce(Thread.legacy_a2a_context_id, Thread.id) == context_id,
                    Thread.owner_api_key_id == owner,
                    Thread.kind == ThreadKind.A2A.value,
                )
            )
            if (
                thread is None
                or thread.kind != ThreadKind.A2A.value
                or thread.owner_api_key_id != owner
            ):
                raise InvalidParamsError("Unknown contextId")
            return thread.id, context_id
        thread = Thread(
            id=str(uuid4()),
            title=A2A_THREAD_TITLE,
            kind=ThreadKind.A2A.value,
            visible=False,
            owner_api_key_id=owner,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        session.add(thread)
        session.flush()
        return thread.id, thread.id

    def _owned(self, session, owner: str, task_id: str) -> A2ATaskRecord:
        row = session.scalar(
            select(A2ATaskRecord).where(
                A2ATaskRecord.id == task_id,
                A2ATaskRecord.owner == owner,
                A2ATaskRecord.expires_at > utc_now(),
            )
        )
        if row is None:
            raise TaskNotFoundError()
        return row

    def get(self, owner: str, task_id: str) -> Task:
        with self.session_factory() as session:
            return task_from_json(self._owned(session, owner, task_id).payload)

    def thread_id(self, owner: str, task_id: str) -> str:
        with self.session_factory() as session:
            return self._owned(session, owner, task_id).context_id

    def save(self, owner: str, task: Task, pending: list[dict] | None = None) -> None:
        with self.session_factory() as session, session.begin():
            row = self._owned(session, owner, task.id)
            row.payload = task_json(task)
            row.state = TaskState.Name(task.status.state)
            row.pending = pending
            row.updated_at = task.status.timestamp.ToDatetime(tzinfo=UTC)
            row.expires_at = utc_now() + timedelta(days=self.retention_days)

    def list(self, owner: str, params) -> tuple[list[Task], int, str]:
        if params.page_size < 0 or params.page_size > 100:
            raise InvalidParamsError("pageSize must be between 1 and 100")
        size = params.page_size or 50
        filter_hash = hashlib.sha256(
            json.dumps(
                [
                    owner,
                    params.context_id,
                    params.status,
                    params.status_timestamp_after.ToJsonString()
                    if params.HasField("status_timestamp_after")
                    else None,
                ]
            ).encode()
        ).hexdigest()
        cursor = None
        if params.page_token:
            try:
                if len(params.page_token) > 1024:
                    raise ValueError("Token too long")
                decoded = json.loads(
                    base64.urlsafe_b64decode(
                        params.page_token + "=" * (-len(params.page_token) % 4)
                    )
                )
                updated, identifier, filters = decoded
                cursor = (datetime.fromisoformat(updated), str(identifier))
                if not cursor[0].tzinfo or filters != filter_hash:
                    raise ValueError("Query does not match pageToken")
            except (ValueError, TypeError) as exc:
                raise InvalidParamsError("Invalid pageToken for this query") from exc
        query = select(A2ATaskRecord).where(
            A2ATaskRecord.owner == owner, A2ATaskRecord.expires_at > utc_now()
        )
        if params.context_id:
            query = query.where(A2ATaskRecord.payload["contextId"].as_string() == params.context_id)
        if params.status:
            query = query.where(A2ATaskRecord.state == TaskState.Name(params.status))
        if params.HasField("status_timestamp_after"):
            query = query.where(
                A2ATaskRecord.updated_at >= params.status_timestamp_after.ToDatetime(tzinfo=UTC)
            )

        with self.session_factory() as session:
            total = session.scalar(select(func.count()).select_from(query.subquery()))
            if cursor:
                updated, identifier = cursor
                query = query.where(
                    or_(
                        A2ATaskRecord.updated_at < updated,
                        and_(A2ATaskRecord.updated_at == updated, A2ATaskRecord.id < identifier),
                    )
                )
            rows = list(
                session.scalars(
                    query.order_by(A2ATaskRecord.updated_at.desc(), A2ATaskRecord.id.desc()).limit(
                        size + 1
                    )
                )
            )
            page, has_more = rows[:size], len(rows) > size
            token = ""
            if has_more:
                last = page[-1]
                token = (
                    base64.urlsafe_b64encode(
                        json.dumps(
                            [
                                last.updated_at.isoformat(),
                                last.id,
                                filter_hash,
                            ]
                        ).encode()
                    )
                    .decode()
                    .rstrip("=")
                )
            return [task_from_json(row.payload) for row in page], total, token

    def recover(self) -> int:
        """An interrupted process may have already performed a device action. Never retry it."""
        from a2a.types import Message, Part, Role

        with self.session_factory() as session, session.begin():
            rows = list(
                session.scalars(select(A2ATaskRecord).where(A2ATaskRecord.state.in_(ACTIVE)))
            )
            for row in rows:
                task = task_from_json(row.payload)
                task.status.state = TaskState.TASK_STATE_FAILED
                task.status.timestamp.FromDatetime(utc_now())
                task.status.message.CopyFrom(
                    Message(
                        message_id=str(uuid4()),
                        role=Role.ROLE_AGENT,
                        context_id=task.context_id,
                        task_id=task.id,
                        parts=[
                            Part(
                                text="The server stopped during execution. The outcome of device actions is uncertain; inspect device state before submitting new work. This task was not retried."
                            )
                        ],
                    )
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
                update(A2AArtifactRecord)
                .where(
                    A2AArtifactRecord.expires_at <= now,
                    A2AArtifactRecord.legacy_content.is_not(None),
                )
                .values(legacy_content=None)
            )
            session.execute(
                delete(A2ATaskRecord).where(
                    A2ATaskRecord.expires_at <= now, ~A2ATaskRecord.state.in_(ACTIVE)
                )
            )
            session.flush()
            # A download manifest stays available to explain a 410 for as long
            # as its task is retained, then goes with it. Panels outlive both.
            session.execute(
                delete(A2AArtifactRecord).where(
                    A2AArtifactRecord.panel_version_id.is_(None),
                    A2AArtifactRecord.expires_at <= now,
                    A2AArtifactRecord.task_id.not_in(select(A2ATaskRecord.id)),
                )
            )
            return list(
                session.scalars(
                    select(Thread.id).where(
                        Thread.kind == ThreadKind.A2A.value,
                        Thread.id.not_in(select(A2ATaskRecord.context_id)),
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
                    Thread.kind == ThreadKind.A2A.value,
                    Thread.id.in_(thread_ids),
                    Thread.id.not_in(select(A2ATaskRecord.context_id)),
                )
            )
            return int(result.rowcount or 0)
