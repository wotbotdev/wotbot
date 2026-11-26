from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from wotbot.clients._base import setting_value as _setting_value
from wotbot.core.config import Settings
from wotbot.workspaces.visibility import visible_thing_ids


class RdfServiceError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        category: str = "unknown",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.category = category
        self.retryable = retryable


def _decode_response_payload(status: int, text: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = None

    if status >= 400:
        if isinstance(data, dict):
            detail = data.get("detail")
            if isinstance(detail, dict):
                message = detail.get("message")
                if not isinstance(message, str) or not message.strip():
                    message = json.dumps(detail, ensure_ascii=False, default=str)
                category = detail.get("category")
                retryable = detail.get("retryable")
                raise RdfServiceError(
                    message,
                    status=status,
                    category=category if isinstance(category, str) else "unknown",
                    retryable=bool(retryable),
                )
            if isinstance(detail, str) and detail.strip():
                raise RdfServiceError(detail, status=status)
            if detail is not None:
                raise RdfServiceError(
                    json.dumps(detail, ensure_ascii=False, default=str),
                    status=status,
                )
        body = text.strip()
        raise RdfServiceError(
            body or f"rdf_service request failed with status {status}",
            status=status,
        )

    if not isinstance(data, dict):
        raise ValueError("rdf_service returned a non-object response")
    return data


class RdfServiceClient:
    def __init__(self, settings: Settings | Any) -> None:
        self._base_url = str(
            _setting_value(
                settings,
                "RDF_SERVICE_URL",
                "rdf_service_url",
                default="http://localhost:8124",
            )
        ).rstrip("/")
        token = _setting_value(settings, "internal_api_key", "INTERNAL_API_KEY", default="")
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(
            total=_setting_value(
                settings,
                "RDF_QUERY_TIMEOUT_SECONDS",
                "rdf_query_timeout_seconds",
                default=20,
            )
        )

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def query(
        self,
        *,
        query: str,
        limit: int,
        use_default_graph_as_union: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "use_default_graph_as_union": use_default_graph_as_union,
        }
        # With workspaces enabled the RDF service only sees this workspace's Things.
        thing_ids = await asyncio.to_thread(visible_thing_ids)
        if thing_ids is not None:
            payload["thing_ids"] = thing_ids
        return await self._request("POST", "/rdf/query", payload)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        async with (
            aiohttp.ClientSession(timeout=self._timeout) as session,
            session.request(
                method,
                url,
                json=payload,
                headers=self._headers,
            ) as response,
        ):
            text = await response.text()
            return _decode_response_payload(response.status, text)
