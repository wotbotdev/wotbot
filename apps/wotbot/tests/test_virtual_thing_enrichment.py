from __future__ import annotations

import asyncio
from typing import Any, Self

import pytest

from wotbot.virtual_things.enrichment import VirtualThingEnrichmentScheduler
from wotbot.virtual_things.store import VirtualThingStore


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeRow:
    def __init__(self, version: int) -> None:
        self.version = version
        self.abstract_td: dict[str, Any] = {"properties": {}}
        self.updated_at = None


class _FakeSession:
    """Minimal stand-in for a SQLAlchemy session (the real store is Postgres-only)."""

    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row
        self.committed = False

    def get(self, _model: Any, _key: Any) -> _FakeRow | None:
        return self._row

    def add(self, _obj: Any) -> None:
        pass

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        pass


def _store_for(row: _FakeRow | None) -> tuple[VirtualThingStore, _FakeSession]:
    session = _FakeSession(row)
    return VirtualThingStore(lambda: session), session


def test_apply_enrichment_writes_back_when_version_matches():
    row = _FakeRow(version=1)
    store, session = _store_for(row)

    applied = store.apply_enrichment(
        "virtual:things:room", {"@type": "saref:TemperatureSensor"}, base_version=1
    )

    assert applied is True
    assert session.committed is True
    assert row.version == 2
    assert row.abstract_td["@type"] == "saref:TemperatureSensor"


def test_apply_enrichment_is_dropped_when_version_advanced():
    """The re-activation race: enrichment of v1 must not clobber v2."""
    row = _FakeRow(version=2)  # already re-activated past the enriched version
    store, session = _store_for(row)

    applied = store.apply_enrichment(
        "virtual:things:room", {"@type": "saref:WrongType"}, base_version=1
    )

    assert applied is False
    assert session.committed is False
    assert row.version == 2
    assert "@type" not in row.abstract_td


def test_apply_enrichment_is_dropped_when_thing_deleted():
    store, session = _store_for(None)

    applied = store.apply_enrichment("virtual:things:room", {"@type": "x"}, base_version=1)

    assert applied is False
    assert session.committed is False


@pytest.mark.anyio
async def test_scheduler_supersedes_in_flight_enrichment():
    started = asyncio.Event()
    release = asyncio.Event()
    applied: list[tuple[str, int]] = []

    async def slow_enrich(td):
        started.set()
        await release.wait()
        return td

    async def fake_apply(thing_id, enriched, base_version):
        applied.append((thing_id, base_version))
        return True

    scheduler = VirtualThingEnrichmentScheduler(enrich=slow_enrich, apply=fake_apply)

    first = scheduler.schedule("t", {"v": 1}, base_version=1)
    await started.wait()
    second = scheduler.schedule("t", {"v": 2}, base_version=2)

    release.set()
    await asyncio.gather(first, second, return_exceptions=True)

    assert first.cancelled()
    assert applied == [("t", 2)]


def test_record_thing_mint_schedules_enrichment(monkeypatch):
    from wotbot.jobs.resources import manager
    from wotbot.virtual_things import enrichment

    calls: list[tuple[str, dict[str, Any], int]] = []

    class _Spy:
        def schedule(self, thing_id, td, *, base_version):
            calls.append((thing_id, td, base_version))

    monkeypatch.setattr(enrichment, "_scheduler", _Spy())

    manager._schedule_record_thing_enrichment(
        {"thing_id": "virtual:things:room", "td": {"properties": {}}, "version": 3}
    )
    assert calls == [("virtual:things:room", {"properties": {}}, 3)]


def test_record_thing_mint_ignores_incomplete_result(monkeypatch):
    from wotbot.jobs.resources import manager
    from wotbot.virtual_things import enrichment

    class _Boom:
        def schedule(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("should not schedule on incomplete result")

    monkeypatch.setattr(enrichment, "_scheduler", _Boom())

    manager._schedule_record_thing_enrichment({"thing_id": "x", "td": {}})  # no version
    manager._schedule_record_thing_enrichment(None)


def test_silent_run_config_suppresses_chat_streaming():
    """Enrichment runs inside the agent turn; its LLM tokens must not stream to the UI."""
    from wotbot.virtual_things.enrichment import _SILENT_RUN_CONFIG

    assert "nostream" in _SILENT_RUN_CONFIG["tags"]
    assert "metadata" not in _SILENT_RUN_CONFIG


@pytest.mark.anyio
async def test_scheduler_enrichment_failure_is_swallowed():
    async def boom(td):
        raise RuntimeError("no API key")

    async def fake_apply(thing_id, enriched, base_version):  # pragma: no cover - must not run
        raise AssertionError("apply should not be called when enrichment fails")

    scheduler = VirtualThingEnrichmentScheduler(enrich=boom, apply=fake_apply)
    task = scheduler.schedule("t", {"v": 1}, base_version=1)
    await task  # does not raise
    assert task.done()
