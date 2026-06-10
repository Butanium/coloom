"""Adversarial UI tests: configurable keybindings (keybindings.svelte.ts,
KeybindingsDialog.svelte, and the keyboard.svelte.ts table-dispatch refactor).

Every mutation is verified through the REST API, not just the DOM. Each test
seeds its own weave (the `weave` fixture). Identity under test: "clement"
(seed parks clement's cursor on a bookmarked node — see seed_dev_weave.py).
"""

import re
import time

import pytest
from playwright.sync_api import expect

GEN_DEADLINE = 8.0


# ---------------------------------------------------------------- helpers


def poll(fn, *, timeout=GEN_DEADLINE, interval=0.2, desc="condition"):
    """Poll `fn` until truthy; return its value. Fail loudly on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    pytest.fail(f"timed out ({timeout}s) waiting for {desc}; last={last!r}")


def weave_json(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def cursor_node(api, wid, name="clement"):
    r = api.get(f"/weaves/{wid}/cursors")
    r.raise_for_status()
    return r.json()[name]["node_id"]


def children_of(api, wid, nid):
    return weave_json(api, wid)["nodes"][nid]["children"]


def gen_count(api, wid) -> int:
    r = api.get(f"/events?since=0&weave_id={wid}")
    r.raise_for_status()
    return sum(1 for e in r.json()["events"] if e["type"] == "gen_started")


def open_dialog(page):
    page.get_by_test_id("kb-open").click()
    expect(page.get_by_test_id("kb-dialog")).to_be_visible()


def close_dialog(page):
    page.get_by_test_id("kb-close").click()
    expect(page.get_by_test_id("kb-dialog")).to_have_count(0)


def arm(page, action: str):
    """Click an action's combo button so the dialog starts capturing."""
    btn = page.get_by_test_id(f"kb-binding-{action}")
    btn.click()
    expect(btn).to_have_text("press a key…")
    return btn


def rebind(page, action: str, combo: str):
    """Full capture flow: arm, then press the new combo."""
    arm(page, action)
    page.keyboard.press(combo)


def assert_no_generation(page, api, wid, key: str):
    """Press `key`, give a wrongly-fired generation generous room to appear."""
    before = gen_count(api, wid)
    page.keyboard.press(key)
    page.wait_for_timeout(2000)
    assert gen_count(api, wid) == before, f"{key!r} triggered a generation"


# ---------------------------------------------------------------- tests


def test_default_g_generates(page_as, weave, api):
    page = page_as("clement", weave)
    cur = cursor_node(api, weave)
    before = list(children_of(api, weave, cur))
    page.keyboard.press("g")
    poll(
        lambda: len(children_of(api, weave, cur)) > len(before),
        desc="default 'g' generated children at cursor",
    )


def test_rebind_generate_to_ctrl_space(page_as, weave, api):
    page = page_as("clement", weave)
    open_dialog(page)
    rebind(page, "generate_at_cursor", "Control+Space")
    expect(page.get_by_test_id("kb-binding-generate_at_cursor")).to_have_text(
        "Ctrl+Space"
    )
    close_dialog(page)

    # the new combo generates…
    gens0 = gen_count(api, weave)
    page.keyboard.press("Control+Space")
    poll(lambda: gen_count(api, weave) > gens0, desc="Ctrl+Space generation started")
    page.wait_for_timeout(1500)  # let the burst finish

    # …and the old key is fully detached
    assert_no_generation(page, api, weave, "g")


def test_keys_during_capture_do_not_dispatch(page_as, weave, api):
    page = page_as("clement", weave)
    cur = cursor_node(api, weave)
    assert weave_json(api, weave)["nodes"][cur]["bookmarked"] is True  # seeded

    open_dialog(page)
    arm(page, "focus_search")
    # 'b' is captured as the new combo — it must NOT toggle the bookmark
    page.keyboard.press("b")
    expect(page.get_by_test_id("kb-binding-focus_search")).to_have_text("B")
    page.wait_for_timeout(1500)
    assert weave_json(api, weave)["nodes"][cur]["bookmarked"] is True, (
        "a key pressed during capture dispatched its old action"
    )

    # 'b' now collides with toggle_bookmark — both rows flag the conflict in red
    expect(page.get_by_test_id("kb-binding-focus_search")).to_have_class(
        re.compile(r"\bconflict\b")
    )
    expect(page.get_by_test_id("kb-binding-toggle_bookmark")).to_have_class(
        re.compile(r"\bconflict\b")
    )


def test_escape_during_capture_unbinds(page_as, weave, api):
    page = page_as("clement", weave)
    cur = cursor_node(api, weave)
    assert weave_json(api, weave)["nodes"][cur]["bookmarked"] is True  # seeded

    open_dialog(page)
    arm(page, "toggle_bookmark")
    page.keyboard.press("Escape")
    expect(page.get_by_test_id("kb-binding-toggle_bookmark")).to_have_text("unbound")
    # the Escape was consumed by the capture, not the dialog
    expect(page.get_by_test_id("kb-dialog")).to_be_visible()
    close_dialog(page)

    # unbound: 'b' does nothing now
    page.keyboard.press("b")
    page.wait_for_timeout(1500)
    assert weave_json(api, weave)["nodes"][cur]["bookmarked"] is True, (
        "unbound 'b' still toggled the bookmark"
    )


def test_reset_all_restores_defaults(page_as, weave, api):
    page = page_as("clement", weave)
    open_dialog(page)
    rebind(page, "generate_at_cursor", "Control+Space")
    expect(page.get_by_test_id("kb-binding-generate_at_cursor")).to_have_text(
        "Ctrl+Space"
    )
    page.get_by_test_id("kb-reset").click()
    expect(page.get_by_test_id("kb-binding-generate_at_cursor")).to_have_text("G")
    close_dialog(page)

    gens0 = gen_count(api, weave)
    page.keyboard.press("g")
    poll(lambda: gen_count(api, weave) > gens0, desc="'g' generates again after reset")


def test_rebind_persists_across_reload(page_as, weave, api):
    page = page_as("clement", weave)
    open_dialog(page)
    rebind(page, "generate_at_cursor", "Control+Space")
    close_dialog(page)

    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)  # weave fetch + ws + layout (mirrors page_as)

    open_dialog(page)
    expect(page.get_by_test_id("kb-binding-generate_at_cursor")).to_have_text(
        "Ctrl+Space"
    )
    close_dialog(page)

    gens0 = gen_count(api, weave)
    page.keyboard.press("Control+Space")
    poll(
        lambda: gen_count(api, weave) > gens0,
        desc="rebound combo still generates after reload",
    )
    page.wait_for_timeout(1500)
    assert_no_generation(page, api, weave, "g")
