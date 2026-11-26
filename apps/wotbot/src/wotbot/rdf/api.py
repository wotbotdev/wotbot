from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import select

from wotbot.catalog.models import Thing
from wotbot.catalog.store import to_record
from wotbot.core.api_dependencies import verify_internal_api_key
from wotbot.core.config import get_settings
from wotbot.core.database import get_session_factory, init_db
from wotbot.rdf.consumer import RdfConsumerState, RdfStreamConsumer
from wotbot.rdf.models import (
    RdfQueryRequest,
    RdfQueryResponse,
    RdfReindexResponse,
)
from wotbot.rdf.store import RdfStoreService
from wotbot.workspaces import all_workspaces_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RdfErrorDetail:
    category: str
    message: str
    retryable: bool
    status_code: int

    def as_response_detail(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
        }


def _query_error_detail(exc: Exception) -> RdfErrorDetail:
    message = str(exc) or exc.__class__.__name__
    normalized = message.lower()
    if isinstance(exc, SyntaxError) or "syntax" in normalized or "parse" in normalized:
        return RdfErrorDetail(
            category="syntax",
            message=message,
            retryable=True,
            status_code=400,
        )
    if any(marker in normalized for marker in ("401", "unauthorized", "credential")):
        return RdfErrorDetail(
            category="auth",
            message=message,
            retryable=False,
            status_code=401,
        )
    if any(marker in normalized for marker in ("403", "forbidden", "security", "scheme")):
        return RdfErrorDetail(
            category="auth",
            message=message,
            retryable=False,
            status_code=403,
        )
    if any(marker in normalized for marker in ("timeout", "timed out", "504")):
        return RdfErrorDetail(
            category="timeout",
            message=message,
            retryable=True,
            status_code=504,
        )
    if any(
        marker in normalized for marker in ("response exceeded", "size limit", "too large", "413")
    ):
        return RdfErrorDetail(
            category="response_size",
            message=message,
            retryable=True,
            status_code=413,
        )
    if isinstance(exc, ValueError):
        return RdfErrorDetail(
            category="validation",
            message=message,
            retryable=True,
            status_code=400,
        )
    return RdfErrorDetail(
        category="executor",
        message=message,
        retryable=True,
        status_code=500,
    )


def _settings_from_app(request: Request) -> Any:
    return request.app.state.settings


def _rdf_store(request: Request) -> RdfStoreService:
    return request.app.state.rdf_store


def _load_all_things() -> list[tuple[str, dict[str, Any]]]:
    session_factory = get_session_factory()
    # The RDF store is an internal service and still holds every workspace's
    # Things, so a reindex must not be limited to the default workspace.
    with all_workspaces_scope(), session_factory() as session:
        things = session.scalars(select(Thing).order_by(Thing.id)).all()
        return [(record.id, record.document) for record in (to_record(thing) for thing in things)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await asyncio.to_thread(init_db)
    rdf_store = RdfStoreService(settings.RDF_STORE_PATH)
    state = RdfConsumerState()
    stop_event = asyncio.Event()
    consumer = RdfStreamConsumer(
        settings=settings,
        state=state,
        rdf_store=rdf_store,
    )
    task = asyncio.create_task(consumer.run_forever(stop_event))

    app.state.settings = settings
    app.state.rdf_store = rdf_store
    app.state.rdf_consumer_state = state
    app.state.rdf_consumer_task = task
    app.state.rdf_consumer_stop_event = stop_event
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await consumer.close()


app = FastAPI(title="WoTBot RDF Service", lifespan=lifespan)


@app.get("/health")
@app.get("/health/live")
async def health(request: Request) -> dict[str, Any]:
    settings = _settings_from_app(request)
    state = request.app.state.rdf_consumer_state
    return {
        "status": "ok",
        "store_path": settings.RDF_STORE_PATH,
        "consumer_running": bool(state.loop_running),
        "last_entry_id": state.last_entry_id,
        "last_error": state.last_error,
    }


@app.post("/rdf/query", response_model=RdfQueryResponse)
async def query_rdf(request: Request, payload: RdfQueryRequest) -> dict[str, Any]:
    verify_internal_api_key(request)
    try:
        return await _rdf_store(request).query(
            query=payload.query,
            limit=payload.limit,
            use_default_graph_as_union=payload.use_default_graph_as_union,
            thing_ids=payload.thing_ids,
        )
    except Exception as exc:
        detail = _query_error_detail(exc)
        if detail.category == "executor":
            logger.exception("RDF query failed")
        raise HTTPException(
            status_code=detail.status_code,
            detail=detail.as_response_detail(),
        ) from exc


@app.post("/rdf/reindex", response_model=RdfReindexResponse)
async def reindex_rdf(request: Request) -> dict[str, Any]:
    verify_internal_api_key(request)
    things = await asyncio.to_thread(_load_all_things)
    result = await _rdf_store(request).reindex(things)
    return {
        "indexed": result.indexed,
        "failed": result.failed,
        "errors": result.errors,
    }
