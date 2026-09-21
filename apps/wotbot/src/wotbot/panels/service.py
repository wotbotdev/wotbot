from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wotbot.core.time import utc_now
from wotbot.panels.data import data_contents, load_data, prune_data, validate_data_refs
from wotbot.panels.models import Panel, PanelVersion

VersionSource = str
VERSION_SOURCES = {"initial", "manual", "ai", "restore"}
ALLOWED_CAPABILITY_OPS = {
    "readProperty",
    "writeProperty",
    "invokeAction",
    "observeProperty",
    "subscribeEvent",
}


def _serialize(panel: Panel, *, include_html: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": panel.id,
        "title": panel.title,
        "capabilities": panel.capabilities,
        "data": panel.data or {},
        "source_thread_id": panel.source_thread_id,
        "created_at": panel.created_at.isoformat() if panel.created_at else None,
        "updated_at": panel.updated_at.isoformat() if panel.updated_at else None,
    }
    if include_html:
        data["html"] = panel.html
    return data


def _serialize_version(
    version: PanelVersion,
    *,
    include_html: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": version.id,
        "panel_id": version.panel_id,
        "version": version.version_number,
        "source": version.source,
        "title": version.title,
        "capabilities": version.capabilities,
        "data": version.data or {},
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
    if include_html:
        data["html"] = version.html
    return data


class PanelService:
    def __init__(self, session: Session):
        self._session = session

    def list_panels(self) -> dict[str, Any]:
        panels = self._session.scalars(select(Panel).order_by(Panel.created_at.desc())).all()
        return {"items": [_serialize(panel) for panel in panels]}

    def create_panel(
        self,
        *,
        title: str,
        html: str,
        capabilities: list[dict[str, Any]],
        source_thread_id: str | None,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(html, str) or not html.strip():
            raise HTTPException(status_code=422, detail="html must not be empty")
        panel = Panel(
            id=uuid.uuid4().hex,
            title=title or "Untitled panel",
            html=html,
            capabilities=_clean_capabilities(capabilities),
            data=self._retain_data(data),
            source_thread_id=source_thread_id,
        )
        self._session.add(panel)
        self._session.flush()
        self._append_version(panel, source="initial")
        self._session.commit()
        self._session.refresh(panel)
        return _serialize(panel)

    def get_panel(self, panel_id: str, *, include_html: bool = False) -> dict[str, Any]:
        return _serialize(self._get_or_404(panel_id), include_html=include_html)

    def get_render_payload(self, panel_id: str) -> tuple[str, str, dict[str, str]]:
        """Read markup and its attachment references from the same panel revision."""
        panel = self._get_or_404(panel_id)
        try:
            return panel.html, panel.title, data_contents(self._session, panel.data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _retain_data(self, data: dict[str, str] | None) -> dict[str, str]:
        try:
            refs = validate_data_refs(data)
            load_data(self._session, refs, retain=True)
            return refs
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def update_panel(
        self,
        panel_id: str,
        *,
        title: str | None = None,
        html: str | None = None,
        capabilities: list[dict[str, Any]] | None = None,
        data: dict[str, str] | None = None,
        version_source: VersionSource = "manual",
    ) -> dict[str, Any]:
        panel = self._get_or_404(panel_id)
        next_title = panel.title
        next_html = panel.html
        next_capabilities = panel.capabilities or []
        next_data = panel.data or {}

        if title is not None and title.strip():
            next_title = title.strip()
        if html is not None:
            if not html.strip():
                raise HTTPException(status_code=422, detail="html must not be empty")
            next_html = html
        if capabilities is not None:
            next_capabilities = _clean_capabilities(capabilities)
        if data is not None:
            next_data = self._retain_data(data)

        if (
            next_title == panel.title
            and next_html == panel.html
            and next_capabilities == (panel.capabilities or [])
            and next_data == (panel.data or {})
        ):
            return _serialize(panel)

        self._ensure_initial_version(panel)
        panel.title = next_title
        panel.html = next_html
        panel.capabilities = next_capabilities
        panel.data = next_data
        panel.updated_at = utc_now()
        self._append_version(panel, source=version_source)
        self._session.commit()
        self._session.refresh(panel)
        return _serialize(panel)

    def list_versions(self, panel_id: str) -> dict[str, Any]:
        self._get_or_404(panel_id)
        versions = self._session.scalars(
            select(PanelVersion)
            .where(PanelVersion.panel_id == panel_id)
            .order_by(PanelVersion.version_number.desc())
        ).all()
        return {"items": [_serialize_version(version) for version in versions]}

    def restore_version(self, panel_id: str, version_id: str) -> dict[str, Any]:
        panel = self._get_or_404(panel_id)
        version = self._session.get(PanelVersion, version_id)
        if version is None or version.panel_id != panel.id:
            raise HTTPException(status_code=404, detail="Panel version not found")

        if (
            panel.title == version.title
            and panel.html == version.html
            and (panel.capabilities or []) == (version.capabilities or [])
            and (panel.data or {}) == (version.data or {})
        ):
            return _serialize(panel)

        panel.title = version.title
        panel.html = version.html
        panel.capabilities = version.capabilities or []
        panel.data = self._retain_data(version.data)
        panel.updated_at = utc_now()
        self._append_version(panel, source="restore")
        self._session.commit()
        self._session.refresh(panel)
        return _serialize(panel)

    def delete_panel(self, panel_id: str) -> None:
        panel = self._get_or_404(panel_id)
        candidates = set((panel.data or {}).values())
        for data in self._session.scalars(
            select(PanelVersion.data).where(PanelVersion.panel_id == panel_id)
        ):
            candidates.update((data or {}).values())
        self._session.delete(panel)
        self._session.flush()
        if candidates:
            prune_data(self._session, candidates)
        self._session.commit()

    def _get_or_404(self, panel_id: str) -> Panel:
        panel = self._session.get(Panel, panel_id)
        if panel is None:
            raise HTTPException(status_code=404, detail="Panel not found")
        return panel

    def _ensure_initial_version(self, panel: Panel) -> None:
        existing_id = self._session.scalar(
            select(PanelVersion.id).where(PanelVersion.panel_id == panel.id).limit(1)
        )
        if existing_id is None:
            self._append_version(panel, source="initial")

    def _append_version(self, panel: Panel, *, source: VersionSource) -> PanelVersion:
        current_max = self._session.scalar(
            select(func.coalesce(func.max(PanelVersion.version_number), 0)).where(
                PanelVersion.panel_id == panel.id
            )
        )
        version = PanelVersion(
            id=uuid.uuid4().hex,
            panel_id=panel.id,
            version_number=(current_max or 0) + 1,
            source=_clean_version_source(source),
            title=panel.title,
            html=panel.html,
            capabilities=panel.capabilities or [],
            data=panel.data or {},
        )
        self._session.add(version)
        return version


def _clean_capabilities(capabilities: Any) -> list[dict[str, Any]]:
    if not isinstance(capabilities, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for entry in capabilities:
        if not isinstance(entry, dict):
            continue
        thing_id = entry.get("thingId")
        ops = entry.get("ops")
        if not isinstance(thing_id, str) or not thing_id or not isinstance(ops, list):
            continue
        cleaned_ops = [op for op in ops if isinstance(op, str) and op in ALLOWED_CAPABILITY_OPS]
        if not cleaned_ops:
            continue
        affordances = entry.get("affordances")
        cleaned.append(
            {
                "thingId": thing_id,
                "affordances": [a for a in affordances if isinstance(a, str)]
                if isinstance(affordances, list)
                else [],
                "ops": cleaned_ops,
            }
        )
    return cleaned


def _clean_version_source(source: VersionSource) -> str:
    return source if source in VERSION_SOURCES else "manual"
