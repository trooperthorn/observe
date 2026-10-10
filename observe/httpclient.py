"""One place that builds the outbound HTTP client the checks and discovery use.

Redirects are not followed unless a caller asks, because a probe that follows a redirect can be
sent to a host the operator never configured. Callers pass the TLS verification setting they
resolved themselves (a bool or an SSLContext). A bool gets the context httpx would build for it,
built once and shared (observe/tlscontext.py); every client still opens its own connections.
"""

from __future__ import annotations

import ssl

import httpx

from .tlscontext import shared


@shared
def _httpx_context(verify: bool, ca_bundle: str | None) -> ssl.SSLContext:
    return httpx.create_ssl_context(verify=verify)  # certifi's roots, or no verification


def http_client(verify: bool | ssl.SSLContext, timeout: float, *,
                follow_redirects: bool = False) -> httpx.AsyncClient:
    if isinstance(verify, bool):
        verify = _httpx_context(verify, None)
    return httpx.AsyncClient(verify=verify, timeout=timeout, follow_redirects=follow_redirects)
