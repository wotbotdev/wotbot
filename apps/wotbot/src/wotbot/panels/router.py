from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from wotbot.auth import User, require_scopes
from wotbot.core.api_dependencies import SessionDep
from wotbot.panels.edit import run_panel_edit
from wotbot.panels.render import wrap_panel_document
from wotbot.panels.reports import get_report
from wotbot.panels.service import PanelService

router = APIRouter(prefix="/api", tags=["panels"])


class CreatePanelBody(BaseModel):
    title: str = ""
    html: str
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    source_thread_id: str | None = None
    data: dict[str, str] = Field(default_factory=dict)


class UpdatePanelBody(BaseModel):
    title: str | None = None
    html: str | None = None
    capabilities: list[dict[str, Any]] | None = None
    data: dict[str, str] | None = None


class EditPanelBody(BaseModel):
    instruction: str


@router.get("/panel-validation/{report_id}")
def validation_report(
    report_id: str,
    session: SessionDep,
    response: Response,
    _user: User = Depends(require_scopes(["things:read"])),
):
    row = get_report(session, report_id)
    response.headers["Cache-Control"] = "private, no-store"
    return {
        **row.report,
        "title": row.title,
        "document_sha256": row.document_sha256,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/panel-validation/{report_id}/screenshot")
def validation_screenshot(
    report_id: str,
    session: SessionDep,
    viewport: Literal["normal", "narrow"] = "normal",
    _user: User = Depends(require_scopes(["things:read"])),
):
    row = get_report(session, report_id)
    screenshot = row.narrow_screenshot if viewport == "narrow" else row.screenshot
    if screenshot is None:
        raise HTTPException(status_code=404, detail="No screenshot captured for this attempt")
    return Response(
        screenshot,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/panels")
def list_panels(
    session: SessionDep,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return PanelService(session).list_panels()


@router.post("/panels")
def create_panel(
    session: SessionDep,
    body: CreatePanelBody = Body(...),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    return PanelService(session).create_panel(
        title=body.title,
        html=body.html,
        capabilities=body.capabilities,
        source_thread_id=body.source_thread_id,
        data=body.data,
    )


@router.get("/panels/{panel_id}")
def get_panel(
    panel_id: str,
    session: SessionDep,
    include_html: bool = False,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return PanelService(session).get_panel(panel_id, include_html=include_html)


@router.get("/panels/{panel_id}/versions")
def list_panel_versions(
    panel_id: str,
    session: SessionDep,
    _user: User = Depends(require_scopes(["things:read"])),
) -> dict[str, Any]:
    return PanelService(session).list_versions(panel_id)


@router.post("/panels/{panel_id}/versions/{version_id}/restore")
def restore_panel_version(
    panel_id: str,
    version_id: str,
    session: SessionDep,
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    return PanelService(session).restore_version(panel_id, version_id)


@router.get("/panels/{panel_id}/render", response_class=HTMLResponse)
def render_panel(
    panel_id: str,
    session: SessionDep,
    _user: User = Depends(require_scopes(["things:read"])),
) -> HTMLResponse:
    html, title, contents = PanelService(session).get_render_payload(panel_id)
    return HTMLResponse(content=wrap_panel_document(html, title, data=contents))


@router.patch("/panels/{panel_id}")
def update_panel(
    panel_id: str,
    session: SessionDep,
    body: UpdatePanelBody = Body(...),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    return PanelService(session).update_panel(
        panel_id,
        title=body.title,
        html=body.html,
        capabilities=body.capabilities,
        data=body.data,
    )


@router.post("/panels/{panel_id}/edit")
async def edit_panel(
    panel_id: str,
    request: Request,
    session: SessionDep,
    body: EditPanelBody = Body(...),
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, Any]:
    if not body.instruction.strip():
        raise HTTPException(status_code=422, detail="instruction must not be empty")

    service = PanelService(session)
    panel = service.get_panel(panel_id, include_html=True)

    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="Agent is not ready")
    checkpointer = getattr(request.app.state, "checkpointer", None)

    updated = await run_panel_edit(
        graph=graph,
        checkpointer=checkpointer,
        html=panel["html"],
        capabilities=panel["capabilities"],
        instruction=body.instruction,
        data=panel["data"],
    )
    if updated.html is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "Panel edit failed validation. The saved panel was not changed."
                    if updated.browser_validation
                    else "The assistant could not produce an updated panel for that request."
                ),
                "browser_validation": updated.browser_validation,
            },
        )

    saved = service.update_panel(
        panel_id,
        html=updated.html,
        capabilities=updated.capabilities,
        data=updated.data,
        version_source="ai",
    )
    return {**saved, "browser_validation": updated.browser_validation}


@router.delete("/panels/{panel_id}")
def delete_panel(
    panel_id: str,
    session: SessionDep,
    _user: User = Depends(require_scopes(["things:write"])),
) -> dict[str, str]:
    PanelService(session).delete_panel(panel_id)
    return {"status": "deleted"}
