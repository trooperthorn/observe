"""Bug plan WP4: the map page in a real browser with the seven-device UniFi console of the bug
report, in the Graph, Tiers and Table views, and the UniFi page's footer.

Needs the Python playwright package and a Chromium build; skipped when either is missing. The
screenshots go to OBSERVE_SCREENSHOT_DIR when it is set, else to the test's temporary directory,
so none is ever written into the repository.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from .test_map_device_types import SEVEN, UPLINKS
from .test_unifi_network import WithDetails, api_env

sync_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

PASSWORD = "correct horse battery"


def chromium() -> str | None:
    """A Chromium the installed playwright can drive: PLAYWRIGHT_BROWSERS_PATH's, else on PATH."""
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for cand in [root / "chromium", *sorted(root.glob("chromium-*/chrome-linux/chrome"))]:
        if cand.exists():
            return str(cand)
    return shutil.which("chromium") or shutil.which("chromium-browser")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def served(tmp_path: Any) -> Any:
    exe = chromium()
    if exe is None:
        pytest.skip("no Chromium build for playwright")
    env = api_env(tmp_path, WithDetails(SEVEN, UPLINKS))
    env.inner.now = time.time()
    env.collect()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(env.client.app, host="127.0.0.1", port=port,
                                           lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    out = Path(os.environ.get("OBSERVE_SCREENSHOT_DIR") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    try:
        yield f"http://localhost:{port}", exe, out
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        env.close()


def test_map_views_and_unifi_footer_in_a_browser(served):
    base, exe, out = served
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=exe)
        except sync_api.Error as err:  # a build this playwright cannot drive
            pytest.skip(f"Chromium did not start: {type(err).__name__}")
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(base + "/login")
        r = page.request.post(base + "/api/login",
                              data={"username": "bob", "password": PASSWORD})
        assert r.ok

        page.goto(base + "/map#graph")
        page.wait_for_selector("#footer:has-text('refreshed')")
        # The standard heading and container; the filters and the footer are in the page.
        assert page.inner_text("main#page h1") == "Map"
        assert "Network / Map" in page.inner_text("main#page .crumbs")
        assert page.locator("main#page #site").count() == 1
        assert page.locator("#shell-header #site").count() == 0
        assert page.locator("main#page #footer").count() == 1
        page.wait_for_timeout(600)
        page.screenshot(path=str(out / "map-graph.png"), full_page=True)
        # Select a device from the keyboard, then clear it with the button.
        page.focus("#graphcanvas")
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#selected button:has-text('Clear selection')")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "map-graph-selected.png"), full_page=True)
        page.click("#selected button:has-text('Clear selection')")
        page.wait_for_selector("#selected:has-text('Select a device')")
        page.focus("#graphcanvas")
        page.keyboard.press("ArrowRight")
        page.wait_for_selector("#selected button:has-text('Clear selection')")
        page.keyboard.press("Escape")
        page.wait_for_selector("#selected:has-text('Select a device')")

        page.click("button.viewbtn[data-view='tiers']")
        page.wait_for_selector("#layers .layer-core")
        assert "UCG Fiber" in page.inner_text("#layers .layer-core")
        assert "Gateway" in page.inner_text("#layers .layer-core")
        access = page.inner_text("#layers .layer-access")
        assert all(n in access for n in ("AP hall", "AP office", "AP lab", "AP garden"))
        assert "UCG Fiber" not in access and "Access point" in access
        page.screenshot(path=str(out / "map-tiers.png"), full_page=True)

        page.click("button.viewbtn[data-view='table']")
        page.wait_for_selector("#devicestable:not([hidden])")
        rows = page.locator("#devices tbody tr")
        assert rows.count() == 7
        assert rows.nth(0).inner_text().startswith("UCG Fiber\tGateway")
        assert page.locator("#links tbody tr").count() == 6
        heads = page.locator("#links thead th")
        assert [heads.nth(i).inner_text() for i in range(5)] == ["From", "To", "Source", "Seen",
                                                                 "State"]
        page.screenshot(path=str(out / "map-table.png"), full_page=True)

        page.goto(base + "/plugins/unifi")
        page.wait_for_selector("main#page #footer:has-text('on a schedule')")
        page.screenshot(path=str(out / "unifi.png"), full_page=True)
        browser.close()
