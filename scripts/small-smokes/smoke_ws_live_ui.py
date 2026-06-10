"""Verify the UI live-syncs over WS: add a node via REST while the page is open,
the canvas must grow a card without a reload.

Usage: uv run --with playwright scripts/small-smokes/smoke_ws_live_ui.py --weave <id>
"""

import argparse
import json
import urllib.request

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:5174")
    parser.add_argument("--api", default="http://localhost:4444")
    parser.add_argument("--weave", required=True)
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.goto(f"{args.base}/#/w/{args.weave}", wait_until="networkidle")
        page.wait_for_timeout(800)

        cards = page.locator("svg .card")
        before = cards.count()
        assert before > 0, "no cards rendered"

        weave = json.loads(
            urllib.request.urlopen(f"{args.api}/weaves/{args.weave}").read()
        )
        req = urllib.request.Request(
            f"{args.api}/weaves/{args.weave}/nodes",
            data=json.dumps(
                {
                    "text": " — and the loom noticed.",
                    "parent_id": weave["roots"][0],
                    "creator": {"type": "human", "label": "ws-smoke"},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req)

        page.wait_for_timeout(1500)  # ws push + refetch + re-render
        after = cards.count()
        browser.close()

    assert after == before + 1, f"expected {before + 1} cards after WS push, got {after}"
    print(f"live WS sync OK: {before} -> {after} cards without reload")


if __name__ == "__main__":
    main()
