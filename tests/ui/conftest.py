"""UI interaction tests: real browser against an EPHEMERAL coloom stack.

Default (hermetic): when the suite is opted in (run `pytest tests/ui` or set
COLOOM_UI_TESTS=1), `pytest_configure` launches a private stack for the whole
session — coloom-fake-openai + coloom-server on free ports, a fresh temp db,
and the server itself serving a vite build of web/ (`--static-dir web/dist`;
built once per session so tests exercise CURRENT web/src — skip the build with
COLOOM_TEST_NO_BUILD=1 to reuse the existing dist). Nothing touches the live
(:5555) or dev-playground (:4444) instances, and no restart coordination is
needed.

Escape hatch (fast iteration): export COLOOM_UI_BASE and/or COLOOM_API to
point at the running dev stack (vite :5174 / server :4444) — nothing is
launched, exactly the pre-isolation behavior.

Conventions for test files here:
- use `weave` for a freshly seeded, isolated weave id (never share weaves across
  test files — tests run in parallel during review sweeps)
- use `page_as(name)` to get a page with that identity already in localStorage
- verify mutations through the REST API (the `api` fixture), not just the DOM
- read COLOOM_API/COLOOM_UI_BASE from os.environ at module level if you need
  module constants — pytest_configure exports them before test modules import
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# Filled by pytest_configure (ephemeral mode) or the environment (dev mode).
_PROCS: list[subprocess.Popen] = []
_OPTED_IN = False


def BASE() -> str:
    return os.environ.get("COLOOM_UI_BASE", "http://localhost:5174")


def API() -> str:
    return os.environ.get("COLOOM_API", "http://localhost:4444")


def _explicitly_targeted(config) -> bool:
    return any("tests/ui" in str(a) for a in config.invocation_params.args)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=2).raise_for_status()
            return
        except Exception as e:  # boundary: polling an in-startup server
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"ephemeral stack: {url} never became healthy: {last_err}")


def _launch_ephemeral_stack() -> None:
    """Build web/dist, then start fake-openai + coloom-server on free ports.
    Exports COLOOM_API / COLOOM_UI_BASE (same origin: the server serves the
    SPA) so conftest fixtures AND test-module constants pick them up."""
    if os.environ.get("COLOOM_TEST_NO_BUILD") != "1":
        build = subprocess.run(
            ["npm", "run", "build"], cwd=REPO / "web",
            capture_output=True, text=True,
        )
        if build.returncode != 0:
            sys.stderr.write(build.stdout + build.stderr)
            raise RuntimeError("npm run build failed (output above)")
    if not (REPO / "web" / "dist" / "index.html").exists():
        raise RuntimeError("web/dist missing — build failed or was skipped without one")

    tmp = Path(tempfile.mkdtemp(prefix="coloom-ui-stack-"))
    fake_port, server_port = _free_port(), _free_port()

    # repo config with gpt-fake re-pointed at the ephemeral mock port
    config = yaml.safe_load((REPO / "coloom.yaml").read_text())
    for ep in config["endpoints"].values():
        if ":9999" in ep["base_url"]:
            ep["base_url"] = f"http://127.0.0.1:{fake_port}/v1"
    (tmp / "coloom.yaml").write_text(yaml.safe_dump(config))

    fake_log = (tmp / "fake-openai.log").open("w")
    _PROCS.append(subprocess.Popen(
        [sys.executable, "-m", "coloom.fake_openai", "--port", str(fake_port)],
        cwd=REPO, stdout=fake_log, stderr=subprocess.STDOUT,
    ))
    server_log = (tmp / "coloom-server.log").open("w")
    _PROCS.append(subprocess.Popen(
        [sys.executable, "-m", "coloom.server",
         "--db", str(tmp / "coloom.sqlite"),
         "--config", str(tmp / "coloom.yaml"),
         "--static-dir", str(REPO / "web" / "dist"),
         "--port", str(server_port)],
        cwd=REPO, stdout=server_log, stderr=subprocess.STDOUT,
    ))
    try:
        _wait_healthy(f"http://127.0.0.1:{fake_port}/v1/models")
        _wait_healthy(f"http://127.0.0.1:{server_port}/weaves")
    except RuntimeError:
        sys.stderr.write((tmp / "coloom-server.log").read_text())
        raise
    base = f"http://127.0.0.1:{server_port}"
    os.environ["COLOOM_API"] = base
    os.environ["COLOOM_UI_BASE"] = base
    sys.stderr.write(
        f"\n[tests/ui] ephemeral stack up: {base} (logs+db in {tmp})\n"
    )


def pytest_configure(config):
    global _OPTED_IN
    _OPTED_IN = (
        _explicitly_targeted(config) or os.environ.get("COLOOM_UI_TESTS") == "1"
    )
    if not _OPTED_IN:
        return
    if os.environ.get("COLOOM_UI_BASE") or os.environ.get("COLOOM_API"):
        # dev-stack escape hatch: must actually be up — fail loudly, never
        # silently skip an explicitly requested run
        try:
            httpx.get(f"{API()}/weaves", timeout=2).raise_for_status()
            httpx.get(BASE(), timeout=2).raise_for_status()
        except Exception as e:
            raise pytest.UsageError(
                f"COLOOM_UI_BASE/COLOOM_API point at a stack that isn't up: {e}"
            )
        return
    _launch_ephemeral_stack()


def pytest_unconfigure(config):
    for proc in _PROCS:
        proc.terminate()
    for proc in _PROCS:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _PROCS.clear()


@pytest.fixture(scope="session", autouse=True)
def _require_stack(request):
    # ~10 min of real-browser tests: only run when asked for by path
    # (uv run pytest tests/ui) or via COLOOM_UI_TESTS=1 — a bare `uv run pytest`
    # stays the fast unit suite.
    if not _OPTED_IN:
        pytest.skip("UI suite is opt-in: run `pytest tests/ui` or set COLOOM_UI_TESTS=1")


@pytest.fixture(scope="session")
def api():
    """REST client straight to the backend — verify UI effects through it."""
    with httpx.Client(base_url=API(), timeout=30) as client:
        yield client


@pytest.fixture()
def weave(api):
    """A freshly seeded weave (isolated per test), deleted afterwards."""
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "seed_dev_weave.py"), "--api", API(),
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


def _reset_profile_generators(identity: str) -> None:
    """Reset a test profile's GENERATORS to the freshly-seeded state.

    Generators are per-profile SERVER data (not settings), so they survive
    across runs: tests that created/edited generators would otherwise leak
    extra chips and param overrides into every later run. Seeded generators
    (named after a builtin template, inheriting it) are stripped back to
    no-overrides; everything else is deleted. NOTE: never delete a seeded
    generator here — seeding never resurrects a deleted pair (by design).
    """
    builtin = {
        t["name"]
        for t in httpx.get(f"{API()}/templates", timeout=5).json()
        if t["builtin"]
    }
    gens = httpx.get(
        f"{API()}/generators", params={"profile": identity}, timeout=5
    ).json()
    for g in gens:
        seeded = (
            g["name"] in builtin
            and g["parent"] is not None
            and g["parent"]["kind"] == "template"
        )
        if not seeded:
            httpx.delete(f"{API()}/generators/{g['id']}", timeout=5).raise_for_status()
        elif g["params"] or any(
            g[k] for k in ("base_url", "model", "api_key", "api_key_env")
        ):
            httpx.patch(
                f"{API()}/generators/{g['id']}",
                json={"base_url": None, "model": None, "api_key": None,
                      "api_key_env": None, "params": None},
                timeout=5,
            ).raise_for_status()


@pytest.fixture()
def page_as(browser):
    """Factory: page_as('uitest-clement') -> page logged into that PROFILE, on the picker.

    The app opens on a profile-select gate; we cache the profile name in
    localStorage so autoLogin skips the gate. The profile is RESET server-side
    first — settings AND its per-profile generators (back to the seeded set) —
    so a stale profile from a previous test/run never leaks state.
    """
    contexts = []

    def make(identity: str, weave_id: str | None = None):
        httpx.put(
            f"{API()}/profiles/{identity}", json={"settings": {}}, timeout=5
        ).raise_for_status()
        _reset_profile_generators(identity)
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
        url = f"{BASE()}/#/w/{weave_id}" if weave_id else f"{BASE()}/#/"
        page.goto(url, wait_until="networkidle")
        page.wait_for_timeout(600)  # profile login + weave fetch + ws + layout
        return page

    yield make
    for ctx in contexts:
        ctx.close()
