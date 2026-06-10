"""Picker folder structure: grouping by weave.metadata.folder, move, filter."""

import httpx
import os
from playwright.sync_api import expect

API = os.environ.get("COLOOM_API", "http://localhost:4444")


def _mkweave(title, folder=None):
    body = {"title": title, "metadata": {"folder": folder} if folder else {}}
    r = httpx.post(f"{API}/weaves", json=body, timeout=5)
    r.raise_for_status()
    return r.json()["id"]


def _cleanup(ids):
    for wid in ids:
        httpx.delete(f"{API}/weaves/{wid}", timeout=5)


def test_picker_groups_by_folder_and_collapses(page_as, api, weave):
    ids = [
        _mkweave("loose-one"),
        _mkweave("foldered-a", "research/personas"),
        _mkweave("foldered-b", "research/personas"),
    ]
    try:
        page = page_as("uitest-clement")
        head = page.get_by_test_id("folder-research/personas")
        expect(head).to_be_visible()
        expect(head).to_contain_text("2")
        # both grouped rows visible, loose one outside any folder section
        expect(page.get_by_text("foldered-a")).to_be_visible()
        expect(page.get_by_text("loose-one")).to_be_visible()
        # collapse hides the group's rows
        head.click()
        expect(page.get_by_text("foldered-a")).to_have_count(0)
        page.wait_for_timeout(1200)  # profile-settings save debounce
        # collapse state roams via the profile (fresh page, same profile —
        # NOT page_as, which resets the profile)
        page.reload(wait_until="networkidle")
        expect(page.get_by_test_id("folder-research/personas")).to_be_visible()
        expect(page.get_by_text("foldered-a")).to_have_count(0)
    finally:
        _cleanup(ids)


def test_move_weave_to_folder(page_as, api, weave):
    wid = _mkweave("homeless-weave")
    try:
        page = page_as("uitest-clement")
        page.get_by_test_id(f"move-{wid}").click()
        page.get_by_test_id(f"move-input-{wid}").fill("archive")
        page.get_by_test_id(f"move-save-{wid}").click()
        page.wait_for_timeout(600)
        meta = httpx.get(f"{API}/weaves/{wid}", timeout=5).json()["metadata"]
        assert meta["folder"] == "archive"
        expect(page.get_by_test_id("folder-archive")).to_be_visible()
    finally:
        _cleanup([wid])


def test_filter_narrows_titles_and_folders(page_as, api, weave):
    ids = [_mkweave("alpha-weave"), _mkweave("beta-weave", "zeta-folder")]
    try:
        page = page_as("uitest-clement")
        page.get_by_test_id("weave-filter").fill("alpha")
        expect(page.get_by_text("alpha-weave")).to_be_visible()
        expect(page.get_by_text("beta-weave")).to_have_count(0)
        # folder names match too
        page.get_by_test_id("weave-filter").fill("zeta")
        expect(page.get_by_text("beta-weave")).to_be_visible()
        expect(page.get_by_text("alpha-weave")).to_have_count(0)
    finally:
        _cleanup(ids)


def test_create_weave_into_folder(page_as, api, weave):
    page = page_as("uitest-clement")
    page.get_by_placeholder("new weave title…").fill("born-foldered")
    page.get_by_test_id("new-weave-folder").fill("nursery")
    page.get_by_role("button", name="create", exact=True).click()
    page.wait_for_timeout(600)
    weaves = httpx.get(f"{API}/weaves", timeout=5).json()
    w = next((x for x in weaves if x["title"] == "born-foldered"), None)
    try:
        assert w is not None
        assert w["metadata"]["folder"] == "nursery"
    finally:
        if w:
            _cleanup([w["id"]])


def test_weave_delete_is_inline_two_step(page_as, api, weave):
    """Weave delete never opens a native popup: the button ARMS on first click
    ("sure?"), fires on the second, and disarms on blur without deleting."""
    wid = _mkweave("doomed-weave")
    try:
        page = page_as("uitest-clement")
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        btn = page.get_by_test_id(f"weave-delete-{wid}")
        expect(btn).to_have_text("delete")

        btn.click()  # first click only ARMS
        expect(btn).to_have_text("sure?")
        assert httpx.get(f"{API}/weaves/{wid}", timeout=5).status_code == 200, (
            "arming must not delete"
        )

        # blur (focus something else) disarms without deleting
        page.get_by_test_id("weave-filter").click()
        expect(btn).to_have_text("delete")
        assert httpx.get(f"{API}/weaves/{wid}", timeout=5).status_code == 200, (
            "disarming must not delete"
        )

        btn.click()
        btn.click()  # armed → fires
        deadline = 4000
        while deadline > 0:
            if httpx.get(f"{API}/weaves/{wid}", timeout=5).status_code == 404:
                break
            page.wait_for_timeout(200)
            deadline -= 200
        assert httpx.get(f"{API}/weaves/{wid}", timeout=5).status_code == 404, (
            "second click on the armed button did not delete the weave"
        )
        assert dialogs == [], f"native popup fired on weave delete: {dialogs}"
    finally:
        _cleanup([wid])
