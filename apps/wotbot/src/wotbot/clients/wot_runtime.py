from __future__ import annotations

from typing import Any

import aiohttp

from wotbot.clients._base import setting_value as _setting_value
from wotbot.core.config import Settings


class WotRuntimeClient:
    def __init__(self, settings: Settings | Any) -> None:
        self._base_url = str(
            _setting_value(
                settings,
                "WOT_RUNTIME_URL",
                "wot_runtime_url",
                default="http://localhost:3003",
            )
        ).rstrip("/")
        token = _setting_value(
            settings,
            "WOT_RUNTIME_API_TOKEN",
            "wot_runtime_api_token",
        )
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._default_timeout = aiohttp.ClientTimeout(
            total=_setting_value(
                settings,
                "WOT_RUNTIME_TIMEOUT_SECONDS",
                "wot_runtime_timeout_seconds",
                default=15,
            )
        )
        self._subscription_timeout = aiohttp.ClientTimeout(
            total=_setting_value(
                settings,
                "WOT_RUNTIME_SUBSCRIPTION_TIMEOUT_SECONDS",
                "wot_runtime_subscription_timeout_seconds",
                default=20,
            )
        )
        self._action_timeout = aiohttp.ClientTimeout(
            total=_setting_value(
                settings,
                "WOT_RUNTIME_ACTION_TIMEOUT_SECONDS",
                "wot_runtime_action_timeout_seconds",
                default=135,
            )
        )

    async def get_runtime_health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def subscription_status(self, subscription_id):
        return await self._request("POST", "/runtime/subscription-status", {"subscription_id": subscription_id})

    async def describe_endpoint(self, *, url: str) -> dict[str, Any]:
        return await self._request("POST", "/runtime/describe-endpoint", {"url": url})

    async def read_property(
        self,
        *,
        thing_id: str,
        property_name: str,
        uri_variables: dict[str, Any] | None = None,
        form_index: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/read-property",
            {
                "thing_id": thing_id,
                "property_name": property_name,
                "uri_variables": uri_variables or {},
                "form_index": form_index,
            },
        )

    async def write_property(
        self,
        *,
        thing_id: str,
        property_name: str,
        value: Any,
        value_content_type: str | None = None,
        value_base64: str | None = None,
        uri_variables: dict[str, Any] | None = None,
        form_index: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/write-property",
            {
                "thing_id": thing_id,
                "property_name": property_name,
                "value": value,
                "value_content_type": value_content_type,
                "value_base64": value_base64,
                "uri_variables": uri_variables or {},
                "form_index": form_index,
            },
        )

    async def invoke_action(
        self,
        *,
        thing_id: str,
        action_name: str,
        input: Any = None,
        input_content_type: str | None = None,
        input_base64: str | None = None,
        uri_variables: dict[str, Any] | None = None,
        form_index: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/invoke-action",
            {
                "thing_id": thing_id,
                "action_name": action_name,
                "input": input,
                "input_content_type": input_content_type,
                "input_base64": input_base64,
                "uri_variables": uri_variables or {},
                "form_index": form_index,
                "idempotency_key": idempotency_key,
            },
            timeout=self._action_timeout,
        )

    async def observe_property(
        self,
        *,
        thing_id: str,
        property_name: str,
        uri_variables: dict[str, Any] | None = None,
        form_index: int | None = None,
        subscription_namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/observe-property",
            {
                "thing_id": thing_id,
                "property_name": property_name,
                "uri_variables": uri_variables or {},
                "form_index": form_index,
                "subscription_namespace": subscription_namespace,
            },
            timeout=self._subscription_timeout,
        )

    async def subscribe_event(
        self,
        *,
        thing_id: str,
        event_name: str,
        subscription_input: Any = None,
        subscription_input_content_type: str | None = None,
        subscription_input_base64: str | None = None,
        uri_variables: dict[str, Any] | None = None,
        form_index: int | None = None,
        subscription_namespace: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/subscribe-event",
            {
                "thing_id": thing_id,
                "event_name": event_name,
                "subscription_input": subscription_input,
                "subscription_input_content_type": subscription_input_content_type,
                "subscription_input_base64": subscription_input_base64,
                "uri_variables": uri_variables or {},
                "form_index": form_index,
                "subscription_namespace": subscription_namespace,
            },
            timeout=self._subscription_timeout,
        )

    async def remove_subscription(
        self,
        *,
        subscription_id: str,
        cancellation_input: Any = None,
        cancellation_input_content_type: str | None = None,
        cancellation_input_base64: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/runtime/remove-subscription",
            {
                "subscription_id": subscription_id,
                "cancellation_input": cancellation_input,
                "cancellation_input_content_type": cancellation_input_content_type,
                "cancellation_input_base64": cancellation_input_base64,
            },
            timeout=self._subscription_timeout,
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        request_timeout = timeout or self._default_timeout

        async with (
            aiohttp.ClientSession(timeout=request_timeout) as session,
            session.request(
                method,
                url,
                json=payload,
                headers=self._headers,
            ) as response,
        ):
            data = await response.json(content_type=None)
            if response.status >= 400:
                detail = data.get("detail") if isinstance(data, dict) else None
                if not detail and response.status == 413:
                    detail = "Request payload too large"
                raise ValueError(
                    detail or f"wot_runtime request failed with status {response.status}"
                )
            if not isinstance(data, dict):
                raise TypeError("wot_runtime returned a non-object response")
            return data
