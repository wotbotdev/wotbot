from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

import aiohttp
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from wotbot.auth import User, require_scopes, require_service
from wotbot.catalog.ids import decode_thing_id
from wotbot.core.api_dependencies import verify_internal_api_key
from wotbot.core.config import get_settings
from wotbot.core.database import get_session_factory
from wotbot.core.settings import Settings
from wotbot.discovery.errors import (
    CredentialChallengeError,
    ProviderError,
    RefreshConflictError,
    SourceConfigurationError,
    SourceConflictError,
    SourceUnavailableError,
)

from wotbot.discovery.http import BoundedHttpClient
from wotbot.discovery.providers import PROVIDERS
from wotbot.discovery.service import DiscoveryService
from wotbot.discovery.source_store import (
    delete_source_credential,
    get_source,
    list_source_credentials,
    set_source_credential,
)
from wotbot.discovery.store import DownloadStore

# A misconfigured source can name internal configuration, so only this fixed
# text is ever returned to a caller.
_MISCONFIGURED_DETAIL = (
    "This discovery source's stored configuration is no longer valid. "
    "Re-register or edit the source; the external service was not contacted."
)

router = APIRouter(prefix="/api/discovery", tags=["discovery"])
RuntimeServiceDep = Annotated[User, Depends(require_service(["wot_runtime"]))]


class ProviderActionBody(BaseModel):
    thing_id: str
    action: str
    input: Any = None
    uri_variables: dict[str, Any] = Field(default_factory=dict)


class SourceRegistrationBody(BaseModel):
    provider: str
    title: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    config: dict[str, Any]
    security: dict[str, Any] | None = None
    network_access: str = "public"


class UrlSourceRegistrationBody(BaseModel):
    url: str
    network_access: str = "public"


class SetSourceCredentialBody(BaseModel):
    scheme: str
    credentials: dict[str, Any]


class ApplyRefreshBody(BaseModel):
    refresh_id: str


@router.get("/providers")
async def list_discovery_providers(
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    return {"items": [provider.registration_schema() for provider in PROVIDERS.values()]}


@router.get("/sources")
async def list_discovery_sources(
    request: Request,
    q: str = Query("", max_length=500),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    return await DiscoveryService(_settings(request)).list_registered_sources(
        query=q,
        page=page,
        per_page=per_page,
    )


@router.get("/sources/{source_id}")
async def get_discovery_source(
    source_id: str,
    request: Request,
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    try:
        return await DiscoveryService(_settings(request)).get_registered_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/sources", status_code=201)
async def register_source(
    body: SourceRegistrationBody,
    request: Request,
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    try:
        result = await DiscoveryService(_settings(request)).register_source(
            provider=body.provider.strip(),
            title=body.title,
            description=body.description,
            tags=body.tags,
            config=body.config,
            security=body.security,
            network_access=body.network_access,
            allow_onboarding="things:write" in (_user.scopes or []),
        )
        return _registration_result(result)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/sources/detect")
async def detect_and_register_source(
    body: UrlSourceRegistrationBody,
    request: Request,
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    try:
        result = await DiscoveryService(_settings(request)).register_source_url(
            source=body.url,
            network_access=body.network_access,
            allow_onboarding="things:write" in (_user.scopes or []),
        )
        return _registration_result(result)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/sources/{source_id}")
async def update_source(
    source_id: str,
    body: SourceRegistrationBody,
    request: Request,
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> dict[str, Any]:
    try:
        result = await DiscoveryService(_settings(request)).update_registered_source(
            source_id=source_id,
            provider=body.provider.strip(),
            title=body.title,
            description=body.description,
            tags=body.tags,
            config=body.config,
            security=body.security,
            network_access=body.network_access,
            allow_onboarding="things:write" in (_user.scopes or []),
        )
        return _registration_result(result)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: str,
    request: Request,
    _user: User = Depends(require_scopes(["sources:manage"])),
) -> Response:
    try:
        await DiscoveryService(_settings(request)).delete_registered_source(source_id=source_id)
    except SourceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@router.get("/sources/{source_id}/credentials")
def list_discovery_source_credentials(
    source_id: str,
    _user: User = Depends(require_scopes(["credentials:read"])),
) -> dict[str, Any]:
    session_factory = get_session_factory()
    with session_factory() as session:
        if get_source(session, source_id) is None:
            raise HTTPException(status_code=404, detail="Discovery source was not found")
        return {"items": list_source_credentials(session, source_id)}


@router.put("/sources/{source_id}/credentials/{security_name}")
def upsert_discovery_source_credential(
    source_id: str,
    security_name: str,
    body: SetSourceCredentialBody = Body(...),
    _user: User = Depends(require_scopes(["credentials:write"])),
) -> dict[str, str]:
    session_factory = get_session_factory()
    with session_factory() as session:
        source = get_source(session, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Discovery source was not found")
        if security_name != source.security_name or body.scheme != source.security_scheme:
            raise HTTPException(
                status_code=422,
                detail="Credential does not match the source security definition",
            )
        if source.security_scheme == "nosec":
            raise HTTPException(status_code=422, detail="This source does not use credentials")
        if not body.credentials:
            raise HTTPException(status_code=422, detail="Credential values are required")
        set_source_credential(
            session,
            source_id=source_id,
            security_name=security_name,
            scheme=body.scheme,
            credentials=body.credentials,
        )
        session.commit()
    return {"status": "ok"}


@router.delete("/sources/{source_id}/credentials/{security_name}")
def remove_discovery_source_credential(
    source_id: str,
    security_name: str,
    _user: User = Depends(require_scopes(["credentials:write"])),
) -> dict[str, str]:
    session_factory = get_session_factory()
    with session_factory() as session:
        if get_source(session, source_id) is None:
            raise HTTPException(status_code=404, detail="Discovery source was not found")
        delete_source_credential(
            session,
            source_id=source_id,
            security_name=security_name,
        )
        session.commit()
    return {"status": "deleted"}


@router.post("/things/{thing_id:path}/refresh/preview")
async def preview_thing_refresh(
    thing_id: str,
    request: Request,
    user: User = Depends(require_scopes(["sources:manage", "things:write"])),
) -> dict[str, Any]:
    try:
        return await DiscoveryService(_settings(request)).preview_thing_refresh(
            thing_id=decode_thing_id(thing_id),
            user_id=user.user_id,
        )
    except CredentialChallengeError as exc:
        raise HTTPException(status_code=428, detail=exc.public()) from exc
    except SourceConfigurationError as exc:
        raise HTTPException(status_code=409, detail=_MISCONFIGURED_DETAIL) from exc
    except RefreshConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SourceUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/things/{thing_id:path}/refresh")
async def apply_thing_refresh(
    thing_id: str,
    body: ApplyRefreshBody,
    request: Request,
    user: User = Depends(require_scopes(["sources:manage", "things:write"])),
) -> dict[str, Any]:
    try:
        return await DiscoveryService(_settings(request)).apply_thing_refresh(
            thing_id=decode_thing_id(thing_id),
            refresh_id=body.refresh_id,
            user_id=user.user_id,
        )
    except RefreshConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runtime/invoke")
async def invoke_provider_action(
    body: ProviderActionBody,
    request: Request,
    _service: RuntimeServiceDep,
) -> dict[str, object]:
    try:
        return await DiscoveryService(_settings(request)).invoke_thing_action(
            thing_id=body.thing_id,
            action=body.action,
            input_data=body.input,
            uri_variables=body.uri_variables,
        )
    except CredentialChallengeError as exc:
        raise HTTPException(status_code=428, detail=exc.public()) from exc
    except SourceConfigurationError as exc:
        raise HTTPException(status_code=409, detail=_MISCONFIGURED_DETAIL) from exc
    except SourceUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    return settings if isinstance(settings, Settings) else get_settings()


def _registration_result(result: dict[str, Any]) -> dict[str, Any]:
    challenge = result.get("credential_challenge")
    if isinstance(challenge, dict):
        raise HTTPException(
            status_code=428,
            detail={
                key: value
                for key, value in challenge.items()
                if key
                in {
                    "status",
                    "owner_kind",
                    "source_id",
                    "security_name",
                    "scheme",
                }
                and isinstance(value, str)
            }
            | {"message": "This discovery source requires credentials."},
        )
    return result


@router.get("/downloads/{handle}", dependencies=[Depends(verify_internal_api_key)])
async def download(handle: str, request: Request) -> Response:
    return await _download_response(handle, request)


@router.get("/runtime/downloads/{handle}")
async def runtime_download(
    handle: str,
    request: Request,
    _service: RuntimeServiceDep,
) -> Response:
    return await _download_response(handle, request)


async def _download_response(handle: str, request: Request) -> Response:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        settings = get_settings()
    try:
        record = await DownloadStore(
            settings.redis_url, ttl_seconds=settings.discovery_download_ttl_seconds
        ).get(handle)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    headers = dict(record.headers)
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header
    client = BoundedHttpClient(mode="public" if record.public else "trusted", max_requests=None)
    try:
        session, upstream = await client.stream(
            record.endpoint,
            headers=headers,
            # record.headers carries the acquired endpoint's bearer secret.
            credentialed=bool(record.headers),
        )
    except (aiohttp.ClientError, OSError, TimeoutError, ProviderError, ValueError):
        raise HTTPException(status_code=502, detail="Acquired download endpoint is unavailable")
    if upstream.status < 200 or upstream.status >= 300:
        status = upstream.status
        upstream.release()
        await session.close()
        return Response(status_code=status, headers={"Cache-Control": "private, no-store"})

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.content.iter_chunked(65_536):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    forwarded: dict[str, str] = {
        "Cache-Control": "private, no-store, max-age=0",
        "Content-Type": upstream.headers.get("Content-Type", record.content_type),
        "Content-Disposition": upstream.headers.get(
            "Content-Disposition", f'attachment; filename="{_safe_filename(record.filename)}"'
        ),
        "X-Content-Type-Options": "nosniff",
    }
    for name in (
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Content-Encoding",
        "ETag",
        "Last-Modified",
    ):
        if value := upstream.headers.get(name):
            forwarded[name] = value
    return StreamingResponse(body(), status_code=upstream.status, headers=forwarded)


def _safe_filename(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return normalized[:200] or "download"
