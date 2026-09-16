from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from wotbot.core.database import get_session_factory
from wotbot.jobs.records.db import VirtualRecord, VirtualRecordThing
from wotbot.jobs.records.schema import (
    field_name_for_property,
    resolve_history_field,
    validate_record_data,
    validate_record_schema,
)
from wotbot.jobs.stores import _json_safe, iso, utc_now
from wotbot.virtual_things.store import VirtualThingStore


class VirtualRecordStore:
    """Stores virtual Things and submitted records for structured-record jobs."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        virtual_thing_store: VirtualThingStore | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._virtual_thing_store = virtual_thing_store or VirtualThingStore()

    def thing_exists(self, thing_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(VirtualRecordThing, thing_id) is not None

    def create_or_update_thing(
        self,
        *,
        thing_id: str,
        source_job_id: str,
        schema_version: int,
        record_schema: dict[str, Any],
        title: str,
        description: str,
    ) -> dict[str, Any]:
        schema = validate_record_schema(record_schema)
        now = utc_now()
        with self._session_factory() as session:
            row = session.get(VirtualRecordThing, thing_id)
            if row is None:
                row = VirtualRecordThing(
                    id=thing_id,
                    source_job_id=source_job_id,
                    schema_version=schema_version,
                    record_schema=schema,
                    title=title,
                    description=description,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.source_job_id = source_job_id
                row.schema_version = schema_version
                row.record_schema = schema
                row.title = title
                row.description = description
                row.updated_at = now

            session.commit()
        definition = self._virtual_thing_store.register_record_thing(
            thing_id=thing_id,
            title=title,
            description=description,
            source_job_id=source_job_id,
            schema_version=schema_version,
            record_schema=schema,
        )
        return {"thing_id": thing_id, "td": definition.td, "version": definition.version}

    def delete_thing(self, thing_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(VirtualRecordThing, thing_id)
            if row is not None:
                session.delete(row)
            session.commit()
        try:
            self._virtual_thing_store.delete_thing(thing_id)
        except KeyError:
            pass

    def submit_record(
        self,
        *,
        thing_id: str,
        source_job_id: str,
        source_run_id: str,
        data: dict[str, Any],
        raw_input: str | None = None,
        confidence: float | None = None,
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._session_factory() as session:
            thing = session.get(VirtualRecordThing, thing_id)
            if thing is None:
                raise KeyError(thing_id)
            validate_record_data(thing.record_schema, data)
            existing = session.scalars(
                select(VirtualRecord).where(
                    VirtualRecord.thing_id == thing_id,
                    VirtualRecord.source_run_id == source_run_id,
                )
            ).one_or_none()
            if existing is not None:
                return _record_payload(existing)

            record = VirtualRecord(
                id=str(uuid4()),
                thing_id=thing_id,
                schema_version=thing.schema_version,
                source_job_id=source_job_id,
                source_run_id=source_run_id,
                recorded_at=recorded_at or now,
                data=_json_safe(data),
                raw_input=raw_input,
                confidence=confidence,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.commit()
            return _record_payload(record)

    def read_property(self, thing_id: str, property_name: str) -> Any:
        with self._session_factory() as session:
            self._get_thing_or_404(session, thing_id)
            if property_name == "record_count":
                return int(
                    session.scalar(
                        select(func.count())
                        .select_from(VirtualRecord)
                        .where(VirtualRecord.thing_id == thing_id)
                    )
                    or 0
                )

            latest = self._latest_record(session, thing_id)
            if property_name == "latest_record":
                return _record_payload(latest) if latest is not None else None
            if property_name == "last_recorded_at":
                return iso(latest.recorded_at) if latest is not None else None
            if property_name.startswith("latest_"):
                field = field_name_for_property(
                    self._get_thing_or_404(session, thing_id).record_schema,
                    property_name.removeprefix("latest_"),
                )
                if field is None:
                    raise KeyError(property_name)
                return latest.data.get(field) if latest is not None else None
            raise KeyError(property_name)

    def invoke_action(self, thing_id: str, action_name: str, input_data: Any) -> Any:
        if action_name == "query_records":
            return self.query_records(thing_id, input_data if isinstance(input_data, dict) else {})
        if action_name == "query_property_history":
            if not isinstance(input_data, dict):
                raise ValueError("query_property_history requires an object input")
            field = input_data.get("property")
            if not isinstance(field, str) or not field.strip():
                raise ValueError("query_property_history requires property")
            return self.query_property_history(thing_id, field.strip(), input_data)
        if action_name.startswith("history_"):
            query = input_data if isinstance(input_data, dict) else {}
            return self.query_property_history(
                thing_id, action_name.removeprefix("history_"), query
            )
        raise KeyError(action_name)

    def query_records(self, thing_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            self._get_thing_or_404(session, thing_id)
            statement = self._filtered_records_statement(thing_id, query)
            rows = session.scalars(statement).all()
            return [_record_payload(row) for row in rows]

    def query_property_history(
        self,
        thing_id: str,
        field_name: str,
        query: dict[str, Any],
    ) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            thing = self._get_thing_or_404(session, thing_id)
            real_field = resolve_history_field(thing.record_schema, field_name)
            if real_field is None:
                raise KeyError(field_name)
            statement = self._filtered_records_statement(thing_id, query)
            rows = session.scalars(statement).all()
            return [
                {
                    "recorded_at": iso(row.recorded_at),
                    "value": row.data.get(real_field),
                    "source_run_id": row.source_run_id,
                }
                for row in rows
                if isinstance(row.data, dict) and real_field in row.data
            ]

    def _get_thing_or_404(self, session: Session, thing_id: str) -> VirtualRecordThing:
        thing = session.get(VirtualRecordThing, thing_id)
        if thing is None:
            raise KeyError(thing_id)
        return thing

    def _latest_record(self, session: Session, thing_id: str) -> VirtualRecord | None:
        return session.scalars(
            select(VirtualRecord)
            .where(VirtualRecord.thing_id == thing_id)
            .order_by(VirtualRecord.recorded_at.desc(), VirtualRecord.created_at.desc())
            .limit(1)
        ).one_or_none()

    def _filtered_records_statement(self, thing_id: str, query: dict[str, Any]):
        statement = select(VirtualRecord).where(VirtualRecord.thing_id == thing_id)
        from_dt = _parse_datetime(query.get("from"))
        to_dt = _parse_datetime(query.get("to"))
        if from_dt is not None:
            statement = statement.where(VirtualRecord.recorded_at >= from_dt)
        if to_dt is not None:
            statement = statement.where(VirtualRecord.recorded_at <= to_dt)
        limit = _bounded_limit(query.get("limit"))
        return statement.order_by(VirtualRecord.recorded_at.asc()).limit(limit)


def _record_payload(row: VirtualRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "thing_id": row.thing_id,
        "schema_version": row.schema_version,
        "source_job_id": row.source_job_id,
        "source_run_id": row.source_run_id,
        "recorded_at": iso(row.recorded_at),
        "data": row.data,
        "raw_input": row.raw_input,
        "confidence": row.confidence,
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _bounded_limit(value: Any) -> int:
    if isinstance(value, int):
        return min(max(value, 1), 1000)
    return 100
