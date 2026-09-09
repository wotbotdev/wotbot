"""The single HTTP client every discovery provider fetches through.

Two network policies share one redirect loop, one origin check, and one byte
cap, because those are the security-relevant parts and five hand-rolled copies
meant five places to fix when one was wrong:

``public``
    For sources anyone on the internet can name. Every hop re-resolves DNS,
    refuses any non-global address, and pins the connection to the addresses it
    just checked so a name cannot be re-resolved between the check and the
    connect.

``trusted``
    For sources an operator registered deliberately, including ones inside the
    deployment's own network. No address policy applies.

Redirects follow one rule in both modes: a request carrying a credential may
not change origin, and one that carries none may. Callers say which they are
with ``credentialed``, because only they know whether a header is a secret or
an ``Accept``. Refusing every origin change was measurably too strict -- it
rejects plain http-to-https upgrades and the bare-to-www redirects real portals
serve, while adding nothing against SSRF, which is what the per-hop address
policy above actually defends.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import ParseResult, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver

from wotbot.discovery.errors import (
    SourceProtocolError,
    SourceResponseTooLargeError,
    UnsafeUrlError,
)

Mode = Literal["public", "trusted"]

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_BYTES = 1_048_576


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname: str, addresses: list[str]) -> None:
        self._hostname = hostname
        self._addresses = addresses

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, Any]]:
        if host != self._hostname:
            raise OSError("Unexpected hostname")
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
            for address in self._addresses
        ]

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class HttpPayload:
    url: str
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def origin_of(value: str | ParseResult) -> tuple[str, str | None, int]:
    """Return the (scheme, host, port) triple two URLs must share to be same-origin."""

    parsed = urlparse(value) if isinstance(value, str) else value
    return (
        parsed.scheme,
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
    )


class BoundedHttpClient:
    """A redirect-following HTTP client with a request, redirect, and byte budget."""

    def __init__(
        self,
        *,
        mode: Mode = "public",
        timeout_seconds: float = 10,
        max_requests: int | None = 5,
        max_redirects: int = 3,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        self._mode: Mode = mode
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._remaining = max_requests
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes

    @property
    def mode(self) -> Mode:
        return self._mode

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        max_bytes: int | None = None,
        credentialed: bool = False,
    ) -> HttpPayload:
        """Perform one buffered request, following redirects within policy."""

        current = url
        current_method = method
        current_body = json_body
        for redirect_count in range(self._max_redirects + 1):
            self._spend()
            session, response = await self._open(
                current,
                headers=headers,
                method=current_method,
                json_body=current_body,
            )
            async with session, response:
                if response.status in _REDIRECT_STATUSES:
                    current, current_method, current_body = self._next_hop(
                        response,
                        current=current,
                        method=current_method,
                        body=current_body,
                        credentialed=credentialed,
                        exhausted=redirect_count == self._max_redirects,
                    )
                    continue
                body = await bounded_body(response, max_bytes or self._max_bytes)
                return HttpPayload(
                    url=str(response.url),
                    status=response.status,
                    content_type=response.headers.get("Content-Type", "").split(";", 1)[0].lower(),
                    body=body,
                    headers={key.lower(): value for key, value in response.headers.items()},
                )
        raise SourceProtocolError("Source redirect limit exceeded")

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        max_bytes: int | None = None,
        credentialed: bool = False,
    ) -> HttpPayload:
        return await self.request(
            "GET", url, headers=headers, max_bytes=max_bytes, credentialed=credentialed
        )

    async def stream(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        credentialed: bool = False,
    ) -> tuple[aiohttp.ClientSession, aiohttp.ClientResponse]:
        """Open an unbuffered response the caller must close.

        Downloads are proxied straight through to the user, so the body is
        never buffered and never counted against ``max_bytes``.
        """

        current = url
        for redirect_count in range(self._max_redirects + 1):
            self._spend()
            session, response = await self._open(
                current,
                headers=headers,
                stream=True,
            )
            if response.status not in _REDIRECT_STATUSES:
                return session, response
            location = response.headers.get("Location")
            response.release()
            await session.close()
            if not location or redirect_count == self._max_redirects:
                raise SourceProtocolError("Source redirect limit exceeded")
            current = self._checked_hop(
                current=current, location=location, credentialed=credentialed
            )
        raise SourceProtocolError("Source redirect limit exceeded")

    def _spend(self) -> None:
        if self._remaining is None:
            return
        if self._remaining <= 0:
            raise SourceProtocolError("Source probe request limit exceeded")
        self._remaining -= 1

    def _next_hop(
        self,
        response: aiohttp.ClientResponse,
        *,
        current: str,
        method: str,
        body: Any,
        credentialed: bool,
        exhausted: bool,
    ) -> tuple[str, str, Any]:
        location = response.headers.get("Location")
        if not location or exhausted:
            raise SourceProtocolError("Source redirect limit exceeded")
        target = self._checked_hop(current=current, location=location, credentialed=credentialed)
        if response.status == 303:
            method, body = "GET", None
        return target, method, body

    def _checked_hop(self, *, current: str, location: str, credentialed: bool) -> str:
        """Resolve one redirect target, refusing to carry a secret off-origin."""

        target = urljoin(current, location)
        if credentialed and origin_of(target) != origin_of(current):
            raise UnsafeUrlError("Authenticated requests cannot redirect across origins")
        return target

    async def _open(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        stream: bool = False,
        method: str = "GET",
        json_body: Any = None,
    ) -> tuple[aiohttp.ClientSession, aiohttp.ClientResponse]:
        connector: aiohttp.TCPConnector | None = None
        if self._mode == "public":
            # Re-resolved and re-checked on every hop, so a redirect cannot
            # reach a private address even though it may change origin.
            parsed, addresses = await resolve_public(url)
            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(parsed.hostname or "", addresses),
                use_dns_cache=False,
            )
        timeout = (
            aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
            if stream
            else self._timeout
        )
        session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            auto_decompress=not stream,
        )
        try:
            response = await session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                allow_redirects=False,
            )
        except BaseException:
            await session.close()
            raise
        return session, response


async def resolve_public(url: str, *, dns_timeout_seconds: float = 5) -> tuple[Any, list[str]]:
    """Resolve a public URL, refusing anything that is not a global address."""

    if len(url) > 2048:
        raise UnsafeUrlError("Public source URL is too long")
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise UnsafeUrlError("Public source must be an HTTP(S) URL without embedded credentials")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(parsed.hostname)
        addresses = [str(literal)]
    except ValueError:
        loop = asyncio.get_running_loop()
        info = await asyncio.wait_for(
            loop.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM),
            timeout=dns_timeout_seconds,
        )
        addresses = sorted({str(item[4][0]) for item in info})
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeUrlError("Public source resolves to a non-public network address")
    return parsed, addresses


async def bounded_body(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    """Read at most ``max_bytes``, refusing an oversized declared or actual body."""

    declared = response.content_length
    if declared is not None and declared > max_bytes:
        raise SourceResponseTooLargeError("Source response is too large")
    body = bytearray()
    async for chunk in response.content.iter_chunked(65_536):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise SourceResponseTooLargeError("Source response is too large")
    return bytes(body)
