"""One place that builds the outbound HTTP client the checks and discovery use.

Redirects are not followed unless a caller asks, because a probe that follows a redirect can be
sent to a host the operator never configured. Callers pass the TLS verification setting they
resolved themselves (a bool or an SSLContext).
"""

from __future__ import annotations

import ssl

import httpx


def http_client(verify: bool | ssl.SSLContext, timeout: float, *,
                follow_redirects: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=verify, timeout=timeout, follow_redirects=follow_redirects)
