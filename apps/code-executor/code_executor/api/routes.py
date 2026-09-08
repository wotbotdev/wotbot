"""Code executor route handlers."""

import json
import os
import uuid

from fastapi import Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from code_executor.api.app import app
from code_executor.api.dependencies import verify_api_key
from code_executor.file_artifacts import FileArtifacts
from code_executor.models import (
    ExecuteRequest,
    ExecuteResponse,
    UploadResponse,
    WebArtifactRequest,
    WebArtifactResponse,
)
from code_executor.utils import plotly_json_to_html


@app.post(
    "/execute", response_model=ExecuteResponse, dependencies=[Depends(verify_api_key)]
)
async def execute(req: ExecuteRequest, request: Request):
    pool = request.app.state.pool
    try:
        result = await pool.execute(req.session_id, req.code)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return ExecuteResponse(**result)


@app.post(
    "/web-artifacts",
    response_model=WebArtifactResponse,
    dependencies=[Depends(verify_api_key)],
)
async def store_web_artifact(req: WebArtifactRequest, request: Request):
    """Persist a generated HTML interface and return its artifact filename."""
    settings = request.app.state.settings
    os.makedirs(settings.artifacts_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex}.html"
    filepath = os.path.join(settings.artifacts_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(req.html)

    return WebArtifactResponse(filename=filename)


@app.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(verify_api_key)],
)
async def upload_file(request: Request, file: UploadFile):
    """Upload a data file (CSV, JSON, image, etc.) that the Python sandbox can access.

    The file is saved to the artifacts directory with a UUID filename.
    Use ``/artifacts/{filename}`` to retrieve it, or reference it in Python code
    via ``/tmp/code-executor-artifacts/{filename}``.
    """
    settings = request.app.state.settings
    os.makedirs(settings.artifacts_dir, exist_ok=True)

    # Preserve original extension
    ext = ""
    if file.filename and "." in file.filename:
        ext = "." + file.filename.rsplit(".", 1)[1]
    filename = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(settings.artifacts_dir, filename)

    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    return UploadResponse(filename=filename, size_bytes=len(content))


@app.get("/artifacts/{filename}", dependencies=[Depends(verify_api_key)])
async def get_artifact(filename: str, request: Request):
    """Serve an artifact file (PNG image, Plotly HTML from JSON, or HTML interface)."""
    settings = request.app.state.settings

    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    try:
        store = FileArtifacts(settings)
        filepath = str(store.path(filename))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid artifact id") from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Artifact not found or expired"
        ) from exc
    if (store.root / ".manifests" / filename).is_file():
        return await get_artifact_content(filename, request)

    if filename.endswith(".png"):
        return FileResponse(filepath, media_type="image/png")

    if filename.endswith(".html"):
        with open(filepath, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())

    if filename.endswith(".json"):
        with open(filepath, "r") as f:
            fig_json = json.load(f)
        html = plotly_json_to_html(fig_json)
        return HTMLResponse(content=html)

    return FileResponse(filepath)


@app.get("/artifacts/{filename}/metadata", dependencies=[Depends(verify_api_key)])
async def get_artifact_metadata(filename: str, request: Request):
    try:
        return FileArtifacts(request.app.state.settings).describe(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid artifact id") from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Artifact not found or expired"
        ) from exc


@app.get("/artifacts/{filename}/content", dependencies=[Depends(verify_api_key)])
async def get_artifact_content(filename: str, request: Request):
    metadata = await get_artifact_metadata(filename, request)
    store = FileArtifacts(request.app.state.settings)
    try:
        path = store.path(filename)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Artifact not found or expired"
        ) from exc
    return FileResponse(
        path,
        media_type=metadata["mime_type"],
        filename=metadata["filename"],
        headers={
            "X-Artifact-SHA256": metadata["sha256"],
            "X-Artifact-Expires-At": metadata["expires_at"],
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/sessions/{session_id}", dependencies=[Depends(verify_api_key)])
async def delete_session(session_id: str, request: Request):
    pool = request.app.state.pool
    await pool.shutdown(session_id)
    return {"ok": True}


@app.get("/health")
async def health(request: Request):
    pool = request.app.state.pool
    return {"status": "ok", "active_sessions": pool.active_count}
