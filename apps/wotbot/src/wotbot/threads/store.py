"""Postgres-backed thread metadata store for sidebar chat summaries."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from wotbot.core.database import get_session_factory
from wotbot.core.time import utc_now
from wotbot.threads.models import DEFAULT_THREAD_TITLE, Thread, ThreadKind, ThreadRecord
from wotbot.threads.titles import MAX_THREAD_TITLE_LENGTH
from wotbot.workspaces.binding import workspaces_enabled
from wotbot.workspaces.context import all_workspaces_active, current_workspace_id


def _now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clean_title(title: str | None, *, default: str | None) -> str | None:
    if not isinstance(title, str):
        return default

    cleaned = title.strip()[:MAX_THREAD_TITLE_LENGTH]
    return cleaned or default


def _to_record(thread: Thread) -> ThreadRecord:
    return {
        "id": thread.id,
        "title": thread.title,
        "createdAt": thread.created_at,
        "updatedAt": thread.updated_at,
        "kind": thread.kind,
        "visible": thread.visible,
        "jobId": thread.job_id,
    }


class ThreadStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()

    def list(self, *, include_hidden: bool = False) -> list[ThreadRecord]:
        statement = select(Thread).order_by(
            Thread.updated_at.desc(),
            Thread.created_at.desc(),
        )
        if not include_hidden:
            statement = statement.where(
                Thread.kind == ThreadKind.CHAT.value,
                Thread.visible.is_(True),
            )
        if workspaces_enabled() and not all_workspaces_active():
            statement = statement.where(Thread.workspace_id == current_workspace_id())
        with self._session_factory() as session:
            threads = session.scalars(statement).all()

        return [_to_record(thread) for thread in threads]

    def get(self, thread_id: str) -> ThreadRecord | None:
        with self._session_factory() as session:
            thread = session.get(Thread, thread_id)

        return _to_record(thread) if thread is not None else None

    def create(
        self,
        *,
        thread_id: str | None = None,
        title: str = DEFAULT_THREAD_TITLE,
        created_at: str | None = None,
        updated_at: str | None = None,
        kind: ThreadKind = ThreadKind.CHAT,
        visible: bool = True,
        job_id: str | None = None,
    ) -> ThreadRecord:
        now = _now_iso()
        record_id = thread_id or str(uuid.uuid4())
        record_created_at = created_at or now
        record_updated_at = updated_at or record_created_at
        record_title = _clean_title(title, default=DEFAULT_THREAD_TITLE)

        with self._session_factory() as session:
            thread = session.get(Thread, record_id)
            if thread is None:
                thread = Thread(
                    id=record_id,
                    title=record_title or DEFAULT_THREAD_TITLE,
                    created_at=record_created_at,
                    updated_at=record_updated_at,
                    kind=kind.value,
                    visible=visible,
                    job_id=job_id,
                )
                session.add(thread)
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    thread = session.get(Thread, record_id)

        if thread is None:
            raise RuntimeError(f"Thread {record_id} could not be created")

        return _to_record(thread)

    def sync_after_run(
        self,
        thread_id: str,
        *,
        suggested_title: str | None = None,
    ) -> ThreadRecord | None:
        now = _now_iso()
        next_title = _clean_title(suggested_title, default=None)

        with self._session_factory() as session:
            thread = session.get(Thread, thread_id)
            if thread is None:
                return None

            if next_title is not None and thread.title == DEFAULT_THREAD_TITLE:
                thread.title = next_title
            thread.updated_at = now
            session.commit()

        return _to_record(thread)

    def touch(self, thread_id: str) -> ThreadRecord | None:
        return self.sync_after_run(thread_id)

    def update_title(
        self,
        *,
        thread_id: str,
        title: str,
        force: bool = False,
    ) -> ThreadRecord:
        next_title = _clean_title(title, default=None)
        if next_title is None:
            raise ValueError("Title is required")

        now = _now_iso()
        with self._session_factory() as session:
            thread = session.get(Thread, thread_id)
            if thread is None:
                thread = Thread(
                    id=thread_id,
                    title=next_title,
                    created_at=now,
                    updated_at=now,
                )
                session.add(thread)
            elif force or thread.title == DEFAULT_THREAD_TITLE:
                thread.title = next_title
                thread.updated_at = now

            session.commit()

        if thread is None:
            raise RuntimeError(f"Thread {thread_id} could not be updated")

        return _to_record(thread)

    def delete(self, thread_id: str) -> bool:
        with self._session_factory() as session:
            thread = session.get(Thread, thread_id)
            if thread is None:
                return False

            session.delete(thread)
            session.commit()

        return True


def list_threads() -> list[ThreadRecord]:
    return ThreadStore().list()


def get_thread(thread_id: str) -> ThreadRecord | None:
    return ThreadStore().get(thread_id)


def create_thread(
    *,
    thread_id: str | None = None,
    title: str = DEFAULT_THREAD_TITLE,
    created_at: str | None = None,
    updated_at: str | None = None,
    kind: ThreadKind = ThreadKind.CHAT,
    visible: bool = True,
    job_id: str | None = None,
) -> ThreadRecord:
    return ThreadStore().create(
        thread_id=thread_id,
        title=title,
        created_at=created_at,
        updated_at=updated_at,
        kind=kind,
        visible=visible,
        job_id=job_id,
    )


def sync_thread_after_run(
    thread_id: str,
    *,
    suggested_title: str | None = None,
) -> ThreadRecord | None:
    return ThreadStore().sync_after_run(thread_id, suggested_title=suggested_title)


def touch_thread(thread_id: str) -> ThreadRecord | None:
    return ThreadStore().touch(thread_id)


def update_thread_title(
    *,
    thread_id: str,
    title: str,
    force: bool = False,
) -> ThreadRecord:
    return ThreadStore().update_title(thread_id=thread_id, title=title, force=force)


def delete_thread(thread_id: str) -> bool:
    return ThreadStore().delete(thread_id)
