"""A read-only client for the UniFi Network Integration API.

Every request is a GET. Redirects are never followed, because a redirect would carry the
X-API-KEY header to another place. Each response is read in chunks and refused past a byte cap,
and a list is read for a bounded number of pages. The key is sent only in the header and is never
logged or put in an error message.
"""

from __future__ import annotations

import json
import ssl
from typing import Any

import httpx

PAGE_LIMIT = 200  # rows asked for per page
MAX_PAGES = 50  # a list longer than PAGE_LIMIT * MAX_PAGES is refused, not truncated
MAX_BODY_BYTES = 4_000_000  # per response


class AuthRejected(Exception):
    """The console answered 401 or 403 for the credential."""


class UniFiError(Exception):
    """The console answered something this client refuses or cannot use."""


def ssl_context(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=ca_bundle)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    return ctx


class IntegrationClient:
    def __init__(self, base_url: str, api_key: str, verify: bool, ca_bundle: str | None,
                 timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url
        self._key = api_key
        self._timeout = timeout
        self._transport = transport
        self._verify = (verify, ca_bundle)

    def session(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport, timeout=self._timeout,
                                     follow_redirects=False)
        return httpx.AsyncClient(verify=ssl_context(*self._verify), timeout=self._timeout,
                                 follow_redirects=False)

    async def get(self, c: httpx.AsyncClient, path: str,
                  params: dict[str, Any] | None = None) -> Any:
        headers = {"X-API-KEY": self._key, "Accept": "application/json"}
        async with c.stream("GET", self.base_url + path, headers=headers, params=params) as r:
            if r.status_code in (401, 403):
                raise AuthRejected(f"HTTP {r.status_code}")
            if 300 <= r.status_code < 400:
                raise UniFiError(f"{path} answered a redirect (HTTP {r.status_code}); "
                                 "redirects are not followed")
            if r.status_code == 404:
                raise UniFiError(f"{path} not found (404)")
            r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BODY_BYTES:
                    raise UniFiError(f"{path} response is larger than {MAX_BODY_BYTES} bytes")
        try:
            return json.loads(bytes(body))
        except ValueError as err:
            raise UniFiError(f"{path} did not return JSON") from err

    async def list_all(self, c: httpx.AsyncClient, path: str) -> list[Any]:
        """Every row of a paged list. Ends on an empty page, on reaching totalCount, or, when the
        console gives no totalCount, on a page shorter than the limit asked for."""
        out: list[Any] = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = await self.get(c, path, {"offset": offset, "limit": PAGE_LIMIT})
            if isinstance(body, list):  # an unpaged answer: there is nothing more to fetch
                return out + body
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise UniFiError(f"{path} did not return a list envelope")
            page = body["data"]
            out.extend(page)
            total = body.get("totalCount")
            if not page:
                return out
            if isinstance(total, int) and not isinstance(total, bool):
                if len(out) >= total:
                    return out
            elif len(page) < PAGE_LIMIT:
                return out
            offset += len(page)
        raise UniFiError(f"{path} has more than {MAX_PAGES} pages; refusing to read further")
