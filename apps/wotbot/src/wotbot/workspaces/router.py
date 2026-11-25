"""Admin API for the workspace registry. Mounted only with WORKSPACES_ENABLED."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from wotbot.auth import User, require_scopes
from wotbot.core.api_dependencies import SessionDep
from wotbot.workspaces.context import InvalidWorkspaceIdError
from wotbot.workspaces.models import WorkspaceRecord
from wotbot.workspaces.store import (
    WorkspaceConflictError,
    WorkspaceNotEmptyError,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
)

router = APIRouter(prefix="/api", tags=["workspaces"])
_MANAGE = require_scopes(["workspaces:manage"])


class CreateWorkspaceRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=63)
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)


def _serialize(record: WorkspaceRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


@router.get("/workspaces")
def list_workspace_records(session: SessionDep, _user: User = Depends(_MANAGE)) -> dict[str, Any]:
    return {"items": [_serialize(record) for record in list_workspaces(session)]}


@router.post("/workspaces", status_code=201)
def create_workspace_record(
    body: CreateWorkspaceRequest,
    session: SessionDep,
    _user: User = Depends(_MANAGE),
) -> dict[str, Any]:
    try:
        record = create_workspace(
            session, workspace_id=body.id, title=body.title, description=body.description
        )
        session.commit()
    except InvalidWorkspaceIdError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkspaceConflictError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(record)


@router.get("/workspaces/{workspace_id}")
def get_workspace_record(
    workspace_id: str, session: SessionDep, _user: User = Depends(_MANAGE)
) -> dict[str, Any]:
    record = get_workspace(session, workspace_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return _serialize(record)


@router.delete("/workspaces/{workspace_id}", status_code=204)
def delete_workspace_record(
    workspace_id: str, session: SessionDep, _user: User = Depends(_MANAGE)
) -> Response:
    try:
        deleted = delete_workspace(session, workspace_id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkspaceNotEmptyError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return Response(status_code=204)
