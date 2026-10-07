"""The UniFi page under Network: devices, clients and Protect cameras.

The page is one static shell (pages/unifi.html) whose script reads the /api/v2/unifi resources
(api.py), so the core's sign-in, role check and rate limit apply to the data, and the page itself
needs a login like every console page. Every string in a row came from the console or a client on
the network (a client names itself), so the script writes it with textContent only and the API only
hands it over as JSON.
"""

from __future__ import annotations

from pathlib import Path

from observe.plugins import PluginPage

HERE = Path(__file__).parent
PAGE_PATH = "/plugins/unifi"


def page_files() -> list[PluginPage]:
    return [PluginPage(PAGE_PATH, HERE / "pages" / "unifi.html")]
