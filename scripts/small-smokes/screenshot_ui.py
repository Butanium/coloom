"""Screenshot the web UI (picker + editor) against a running dev server.

Usage: uv run --with playwright scripts/small-smokes/screenshot_ui.py --weave <id> [--base http://localhost:5174]
Requires a chromium from `playwright install chromium`.
"""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:5174")
    parser.add_argument("--weave", required=True, help="weave id for the editor view")
    parser.add_argument("--out", type=Path, default=Path("/tmp/coloom-ui"))
    parser.add_argument("--identity", default="clement")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.add_init_script(
            # both keys: identity for attribution, profile so autoLogin skips
            # the gate (without it every shot is the login page)
            f"localStorage.setItem('coloom.identity', {args.identity!r});"
            f"localStorage.setItem('coloom.profile', {args.identity!r})"
        )
        page.goto(f"{args.base}/#/", wait_until="networkidle")
        page.wait_for_timeout(500)
        page.screenshot(path=args.out / "picker.png")

        page.goto(f"{args.base}/#/w/{args.weave}", wait_until="networkidle")
        page.wait_for_timeout(1200)  # weave fetch + ws + layout
        page.screenshot(path=args.out / "editor.png")

        # the view auto-centers on the identity's cursor node — hover canvas center
        canvas = page.locator(".canvas").bounding_box()
        if canvas:
            page.mouse.move(canvas["x"] + canvas["width"] / 2, canvas["y"] + canvas["height"] / 2)
            page.wait_for_timeout(300)
            page.screenshot(path=args.out / "editor-hover.png")

        # token tooltip with the counterfactual row (hover a token in the text pane)
        tokens = page.locator(".doc .token")
        if tokens.count() > 3:
            tokens.nth(3).hover()
            page.wait_for_timeout(700)  # hover-intent delay
            page.screenshot(path=args.out / "token-tooltip.png")
            page.mouse.move(10, 10)
            page.wait_for_timeout(400)

        # sidebar tabs + graph tab
        for tab, name in [("activity", "activity"), ("marks", "bookmarks")]:
            page.get_by_role("button", name=tab, exact=True).click()
            page.wait_for_timeout(300)
            page.screenshot(path=args.out / f"tab-{name}.png")
        page.get_by_role("button", name="tree", exact=True).click()
        page.get_by_role("button", name="graph", exact=True).click()
        page.wait_for_timeout(500)
        page.screenshot(path=args.out / "tab-graph.png")

        browser.close()
    print(f"screenshots in {args.out}/")


if __name__ == "__main__":
    main()
