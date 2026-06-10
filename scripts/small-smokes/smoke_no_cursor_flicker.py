"""Smoke: fast keyboard navigation must not flicker the cursor backward.

Repro of Clément's 2026-06-10 complaint: navigating fast made late cursor_moved
echoes bounce the optimistic cursor back then forward. Fix: state.svelte.ts
skips my-cursor echoes while my own moves are in flight (myPendingCursorEchoes).

Method: seed a weave with a deep single chain, rapid-press ArrowRight down the
chain, then sample the thread doc's text length at ~30ms while echoes drain.
Depth == doc length, so any decrease after presses stop = a backward flicker.

Run (dev stack up: server :4444, vite :5174):
    uv run scripts/small-smokes/smoke_no_cursor_flicker.py
"""

import argparse
import time

import httpx
from playwright.sync_api import sync_playwright

DEPTH = 8


def seed_chain(api: httpx.Client) -> str:
    w = api.post(
        "/weaves",
        json={"title": "flicker-smoke", "metadata": {"folder": "testing"}},
    ).json()
    wid = w["id"]
    parent = None
    for i in range(DEPTH):
        body = {"content": {"type": "snippet", "text": f" hop{i} this is segment {i}."}}
        if parent is not None:
            body["parent_id"] = parent
        node = api.post(f"/weaves/{wid}/nodes", json=body).json()
        parent = node["id"]
    root = api.get(f"/weaves/{wid}").json()["roots"][0]
    api.put(
        f"/weaves/{wid}/cursors/uitest-clement",
        json={"node_id": root, "moved_by": "uitest-clement"},
    ).raise_for_status()
    return wid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:4444")
    ap.add_argument("--ui", default="http://localhost:5174")
    args = ap.parse_args()

    api = httpx.Client(base_url=args.api, timeout=10)
    api.put("/profiles/uitest-clement", json={"settings": {}}).raise_for_status()
    wid = seed_chain(api)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1500, "height": 900})
            page.add_init_script(
                "localStorage.setItem('coloom.identity', 'uitest-clement');"
                "localStorage.setItem('coloom.profile', 'uitest-clement')"
            )
            page.goto(f"{args.ui}/#/w/{wid}", wait_until="networkidle")
            page.wait_for_timeout(1000)

            doc_len = lambda: len(page.locator(".doc").text_content() or "")  # noqa: E731
            start_len = doc_len()

            # rapid navigation: descend the whole chain with ~60ms between presses
            for _ in range(DEPTH - 1):
                page.keyboard.press("ArrowRight")
                page.wait_for_timeout(60)

            # echoes drain over the next ~2s; doc length must never shrink
            samples = [doc_len()]
            end = time.monotonic() + 2.5
            while time.monotonic() < end:
                samples.append(doc_len())
                time.sleep(0.03)

            peak = samples[0]
            for i, s in enumerate(samples[1:], 1):
                assert s >= peak, (
                    f"FLICKER: doc shrank from {peak} to {s} chars at sample {i} "
                    f"(thread jumped backward after navigation stopped)"
                )
                peak = max(peak, s)
            final = samples[-1]
            assert final > start_len, "navigation never advanced the thread?"

            cur = api.get(f"/weaves/{wid}/cursors").json()["uitest-clement"]["node_id"]
            leaf = [
                nid
                for nid, n in api.get(f"/weaves/{wid}").json()["nodes"].items()
                if not n["children"]
            ]
            assert cur == leaf[0], f"cursor ended at {cur}, expected the chain leaf"
            browser.close()
        print(f"OK no flicker: doc {start_len} -> {final} chars, {len(samples)} samples, cursor at leaf")
    finally:
        api.delete(f"/weaves/{wid}")


if __name__ == "__main__":
    main()
