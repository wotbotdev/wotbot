"""Admin API for the workspace registry. Mounted only with WORKSPACES_ENABLED."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from wotbot.auth import User, require_scopes
from wotbot.core.api_dependencies import SessionDep
from wotbot.workspaces.context import InvalidWorkspaceIdError
from wotbot.workspaces.models import WorkspaceRecord
from wotbot.workspaces.prompting import invalidate_workspace_guidance
from wotbot.workspaces.store import (
    WorkspaceConflictError,
    WorkspaceNotEmptyError,
    create_workspace,
    delete_workspace,
    get_workspace,
    list_workspaces,
    update_workspace,
)

router = APIRouter(prefix="/api", tags=["workspaces"])
_MANAGE = require_scopes(["workspaces:manage"])
_PROMPT_MAX_LENGTH = 8000


class CreateWorkspaceRequest(BaseModel):
    id: str = Field(..., min_length=1, max_length=63)
    title: str = Field(..., min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    prompt_extra: str = Field(default="", max_length=_PROMPT_MAX_LENGTH)


class UpdateWorkspaceRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    prompt_extra: str | None = Field(default=None, max_length=_PROMPT_MAX_LENGTH)


def _serialize(record: WorkspaceRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "title": record.title,
        "description": record.description,
        "prompt_extra": record.prompt_extra,
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
            session,
            workspace_id=body.id,
            title=body.title,
            description=body.description,
            prompt_extra=body.prompt_extra,
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


@router.patch("/workspaces/{workspace_id}")
def update_workspace_record(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    session: SessionDep,
    _user: User = Depends(_MANAGE),
) -> dict[str, Any]:
    record = update_workspace(
        session,
        workspace_id,
        title=body.title,
        description=body.description,
        prompt_extra=body.prompt_extra,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    session.commit()
    # Other processes (job worker, voice agent) pick the change up within the cache TTL.
    invalidate_workspace_guidance(workspace_id)
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
    invalidate_workspace_guidance(workspace_id)
    return Response(status_code=204)
