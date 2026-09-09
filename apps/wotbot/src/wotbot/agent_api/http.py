"""Authenticated artifact HTTP routes shared by all external agent transports."""

import asyncio
import logging
import re
from urllib.parse import quote, urlencode

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from wotbot.agent_api.downloads import ArtifactDownloadLinks, InvalidDownloadLink
from wotbot.auth.providers import get_api_key_user
from wotbot.core.time import utc_now

logger = logging.getLogger(__name__)


class AgentAuthMiddleware:
    """ASGI middleware: a disconnected stream does not cancel the independent runner."""

    def __init__(self, app, *, download_links):
        self.app = app
        self.download_links = download_links

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and (
            scope["path"].startswith("/a2a/") or scope["path"].startswith("/agent/artifacts/")
        ):
            request = Request(scope)
            tokens = request.query_params.getlist("downloadToken")
            if tokens:
                # Resolve the capability here, then remove it from the scope
                # used by Uvicorn access logs and downstream request handling.
                scope["query_string"] = urlencode(
                    [
                        (key, value)
                        for key, value in request.query_params.multi_items()
                        if key != "downloadToken"
                    ]
                ).encode()
            download = re.fullmatch(r"/(?:a2a|agent)/artifacts/([^/]+)", scope["path"])
            if (
                tokens
                and download
                and scope["method"] in {"GET", "HEAD"}
                and not request.headers.get("Authorization")
            ):
                try:
                    if len(tokens) != 1:
                        raise InvalidDownloadLink()
                    user = await self.download_links.authorize(tokens[0], download[1])
                except InvalidDownloadLink:
                    return await JSONResponse(
                        {
                            "error": "Download link is invalid or expired. Retrieve the task or artifact again with your API key for a fresh link."
                        },
                        status_code=410,
                        headers={
                            "Cache-Control": "private, no-store",
                            "Referrer-Policy": "no-referrer",
                        },
                    )(scope, receive, send)
            else:
                user = await asyncio.to_thread(get_api_key_user, request)
            if user is None or not user.api_key_id:
                response = JSONResponse(
                    {
                        "error": {
                            "code": 401,
                            "status": "UNAUTHENTICATED",
                            "message": "A valid API key is required",
                        }
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                return await response(scope, receive, send)
            if "agent:invoke" not in (user.scopes or []):
                return await JSONResponse(
                    {
                        "error": {
                            "code": 403,
                            "status": "PERMISSION_DENIED",
                            "message": "Missing scope: agent:invoke",
                        }
                    },
                    status_code=403,
                )(scope, receive, send)
            scope.setdefault("state", {})["a2a_user"] = user
        await self.app(scope, receive, send)


def install_downloads(app, settings):
    from wotbot.agent_api.artifacts import ArtifactStore

    if getattr(app.state, "agent_downloads_installed", False):
        return
    app.state.agent_downloads_installed = True
    app.add_middleware(AgentAuthMiddleware, download_links=ArtifactDownloadLinks(settings))

    @app.api_route(
        "/agent/artifacts/{artifact_id}", methods=["GET", "HEAD"], include_in_schema=False
    )
    @app.api_route("/a2a/artifacts/{artifact_id}", methods=["GET", "HEAD"], include_in_schema=False)
    async def download(artifact_id: str, request: Request):
        """Stream an export straight from the code executor.

        New bytes stay in the executor. Copies retained by the original A2A
        implementation remain downloadable until their original expiry.
        """
        from wotbot.agent_api.outputs import executor_headers

        record = await ArtifactStore().get(
            artifact_id, owner=request.state.a2a_user.api_key_id, include_content=True
        )
        if record is None or record.panel_version_id:
            return _artifact_error("Artifact not found", 404)
        if record.expires_at and record.expires_at <= utc_now():
            return _artifact_error("Artifact has expired", 410)

        headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(record.name, safe=""),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        }
        if "artifact" in record.artifact_metadata:
            response = JSONResponse(record.artifact_metadata["artifact"], headers=headers)
            if request.method == "HEAD":
                response.body = b""
            return response
        if record.legacy_content is not None:
            headers["Content-Length"] = str(len(record.legacy_content))
            return Response(
                b"" if request.method == "HEAD" else record.legacy_content,
                media_type=record.media_type,
                headers=headers,
            )
        if not record.executor_artifact_id:
            return _artifact_error("Artifact not found", 404)

        url = (
            f"{settings.code_executor_url.rstrip('/')}"
            f"/artifacts/{quote(record.executor_artifact_id, safe='')}/content"
        )
        client = httpx.AsyncClient(timeout=settings.code_executor_timeout_seconds)
        try:
            upstream = await client.send(
                client.build_request("GET", url, headers=executor_headers(settings)),
                stream=True,
            )
        except httpx.HTTPError:
            await client.aclose()
            logger.exception("Could not reach the code executor for artifact=%s", artifact_id)
            return _artifact_error("Artifact is temporarily unavailable", 503)
        if upstream.status_code != 200:
            await upstream.aclose()
            await client.aclose()
            # The executor's own retention swept it before this link expired.
            return _artifact_error(
                "Artifact has expired"
                if upstream.status_code == 404
                else "Artifact is unavailable",
                410 if upstream.status_code == 404 else 502,
            )

        if "content-length" in upstream.headers:
            headers["Content-Length"] = upstream.headers["content-length"]

        async def close():
            await upstream.aclose()
            await client.aclose()

        if request.method == "HEAD":
            await close()
            return Response(status_code=200, media_type=record.media_type, headers=headers)
        return StreamingResponse(
            upstream.aiter_bytes(),
            media_type=record.media_type,
            headers=headers,
            background=BackgroundTask(close),
        )


def _artifact_error(message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": message},
        status_code=status,
        headers={"Cache-Control": "private, no-store"},
    )
