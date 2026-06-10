"""UI interaction tests: real browser against the live dev stack.

Requires: coloom-fake-openai (:9999), coloom-server (:4444), vite dev (:5174).
Auto-skips when the stack isn't up, so `uv run pytest` stays green elsewhere.

Conventions for test files here:
- use `weave` for a freshly seeded, isolated weave id (never share weaves across
  test files — tests run in parallel during review sweeps)
- use `page_as(name)` to get a page with that identity already in localStorage
- verify mutations through the REST API (the `api` fixture), not just the DOM
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

BASE = os.environ.get("COLOOM_UI_BASE", "http://localhost:5174")
API = os.environ.get("COLOOM_API", "http://localhost:4444")
REPO = Path(__file__).resolve().parents[2]


def _stack_up() -> bool:
    try:
        httpx.get(f"{API}/weaves", timeout=2).raise_for_status()
        httpx.get(BASE, timeout=2).raise_for_status()
        return True
    except Exception:
        return False


def _explicitly_targeted(config) -> bool:
    return any("tests/ui" in str(a) for a in config.invocation_params.args)


@pytest.fixture(scope="session", autouse=True)
def _require_stack(request):
    # ~10 min of real-browser tests: only run when asked for by path
    # (uv run pytest tests/ui) or via COLOOM_UI_TESTS=1 — a bare `uv run pytest`
    # stays the fast unit suite.
    if not (
        _explicitly_targeted(request.config) or os.environ.get("COLOOM_UI_TESTS") == "1"
    ):
        pytest.skip("UI suite is opt-in: run `pytest tests/ui` or set COLOOM_UI_TESTS=1")
    if not _stack_up():
        pytest.skip("dev stack not running (fake-openai + coloom-server + vite)")


@pytest.fixture(scope="session")
def api():
    """REST client straight to the backend — verify UI effects through it."""
    with httpx.Client(base_url=API, timeout=30) as client:
        yield client


@pytest.fixture()
def weave(api):
    """A freshly seeded weave (isolated per test), deleted afterwards."""
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "seed_dev_weave.py"), "--api", API,
         "--title", "ui-test weave"],
        capture_output=True,
        text=True,
        check=True,
    )
    wid = json.loads(out.stdout)["weave_id"]
    yield wid
    api.delete(f"/weaves/{wid}")


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture()
def page_as(browser):
    """Factory: page_as('uitest-clement') -> page logged into that PROFILE, on the picker.

    The app opens on a profile-select gate; we cache the profile name in
    localStorage so autoLogin skips the gate. The profile is RESET server-side
    first — profile settings roam (ui prefs, active generators, keybindings),
    so a stale profile from a previous test would leak state across tests.
    """
    contexts = []

    def make(identity: str, weave_id: str | None = None):
        httpx.put(
            f"{API}/profiles/{identity}", json={"settings": {}}, timeout=5
        ).raise_for_status()
        ctx = browser.new_context(
            viewport={"width": 1500, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.add_init_script(
            f"localStorage.setItem('coloom.identity', {identity!r});"
            f"localStorage.setItem('coloom.profile', {identity!r})"
        )
        url = f"{BASE}/#/w/{weave_id}" if weave_id else f"{BASE}/#/"
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(600)  # profile login + weave fetch + ws + layout
        return page

    yield make
    for ctx in contexts:
        ctx.close()
