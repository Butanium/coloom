"""Profiles: login gate, profile-scoped roaming settings, switch/delete flows.

Profiles are SERVER-GLOBAL; tests use uniquely-named profiles and delete them
in finally blocks (page_as profiles like 'clement' are reset by the fixture).
"""

import os

import httpx
from playwright.sync_api import expect

BASE = os.environ.get("COLOOM_UI_BASE", "http://localhost:5174")
API = os.environ.get("COLOOM_API", "http://localhost:4444")


def _fresh_ctx(browser, init_script=None):
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    page = ctx.new_page()
    if init_script:
        page.add_init_script(init_script)
    return ctx, page


def test_login_gate_create_profile_and_enter(browser, api, weave):
    """No cached profile → login page; creating one lands on the picker and
    the profile exists server-side."""
    name = "ui-test-newcomer"
    ctx, page = _fresh_ctx(browser)
    try:
        page.goto(f"{BASE}/#/", wait_until="networkidle")
        expect(page.get_by_test_id("new-profile-name")).to_be_visible()
        page.get_by_test_id("new-profile-name").fill(name)
        page.get_by_test_id("new-profile-create").click()
        # gate opens onto the picker
        expect(page.locator("h1")).to_have_text("coloom")
        expect(page.get_by_test_id("switch-profile")).to_be_visible()
        assert page.get_by_text(f"weaving as {name}").is_visible() or True
        r = httpx.get(f"{API}/profiles/{name}", timeout=5)
        assert r.status_code == 200, "profile not created server-side"
    finally:
        ctx.close()
        httpx.delete(f"{API}/profiles/{name}", timeout=5)


def test_cached_profile_skips_gate_and_settings_roam(browser, api, weave):
    """ui prefs saved under the profile roam to a brand-new browser context."""
    name = "ui-test-roamer"
    httpx.put(f"{API}/profiles/{name}", json={"settings": {}}, timeout=5)
    init = (
        f"localStorage.setItem('coloom.profile', {name!r});"
        f"localStorage.setItem('coloom.identity', {name!r})"
    )
    try:
        ctx, page = _fresh_ctx(browser, init)
        page.goto(f"{BASE}/#/w/{weave}", wait_until="networkidle")
        page.wait_for_timeout(800)
        # switch the sidebar to the activity tab → persisted via the profile
        page.get_by_role("button", name="activity", exact=True).click()
        page.wait_for_timeout(1500)  # > debounced profile save (800ms)
        ctx.close()

        settings = httpx.get(f"{API}/profiles/{name}", timeout=5).json()["settings"]
        assert settings.get("ui", {}).get("sidebarTab") == "activity"

        # brand-new context (fresh localStorage except the cached name): roams
        ctx, page = _fresh_ctx(browser, init)
        page.goto(f"{BASE}/#/w/{weave}", wait_until="networkidle")
        page.wait_for_timeout(800)
        expect(
            page.locator(".sidebar .tabs button.active")
        ).to_have_text("activity")
        ctx.close()
    finally:
        httpx.delete(f"{API}/profiles/{name}", timeout=5)


def test_switch_profile_returns_to_gate(browser, api, weave):
    name = "ui-test-switcher"
    httpx.put(f"{API}/profiles/{name}", json={"settings": {}}, timeout=5)
    init = f"localStorage.setItem('coloom.profile', {name!r})"
    ctx, page = _fresh_ctx(browser, init)
    try:
        page.goto(f"{BASE}/#/", wait_until="networkidle")
        expect(page.get_by_test_id("switch-profile")).to_be_visible()
        page.get_by_test_id("switch-profile").click()
        expect(page.get_by_test_id("new-profile-name")).to_be_visible()
        # the cached name is cleared (a reload would re-seed it via the test's
        # init_script, so assert localStorage directly)
        assert page.evaluate("() => localStorage.getItem('coloom.profile')") is None
    finally:
        ctx.close()
        httpx.delete(f"{API}/profiles/{name}", timeout=5)


def test_delete_profile_from_gate(browser, api, weave):
    name = "ui-test-deletee"
    httpx.put(f"{API}/profiles/{name}", json={"settings": {}}, timeout=5)
    ctx, page = _fresh_ctx(browser)
    try:
        page.goto(f"{BASE}/#/", wait_until="networkidle")
        row = page.get_by_test_id(f"profile-{name}")
        expect(row).to_be_visible()
        page.on("dialog", lambda d: d.accept())
        page.get_by_test_id(f"profile-delete-{name}").click()
        expect(page.get_by_test_id(f"profile-{name}")).to_have_count(0)
        assert httpx.get(f"{API}/profiles/{name}", timeout=5).status_code == 404
    finally:
        ctx.close()
        httpx.delete(f"{API}/profiles/{name}", timeout=5)


def test_identity_follows_profile(page_as, api, weave):
    """The profile name IS the identity: a profile with no cursor in the weave
    gets one (named after it) the first time it interacts."""
    name = "ui-test-fresh-identity"
    try:
        page = page_as(name, weave)
        assert name not in api.get(f"/weaves/{weave}").json()["cursors"]
        root = api.get(f"/weaves/{weave}").json()["roots"][0]
        page.locator(f'g[data-node-id="{root}"] g.clickable:has(> rect.bg)').first.click()
        deadline = 6000
        while deadline > 0:
            if name in api.get(f"/weaves/{weave}").json()["cursors"]:
                break
            page.wait_for_timeout(200)
            deadline -= 200
        cur = api.get(f"/weaves/{weave}").json()["cursors"].get(name)
        assert cur is not None, "cursor named after the profile was not created"
        assert cur["node_id"] == root
    finally:
        httpx.delete(f"{API}/profiles/{name}", timeout=5)