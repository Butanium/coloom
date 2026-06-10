"""Regression check: hover-revealed buttons must be actually clickable.

Hovers the cursor-centered card, then CLICKS the '+' generate button and expects
new cards to appear (real generation). Then clicks the '…' collapse toggle and
expects the subtree to hide.

Usage: uv run --with playwright scripts/small-smokes/smoke_click_hover_buttons.py --weave <id>
"""

import argparse

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:5174")
    parser.add_argument("--weave", required=True)
    parser.add_argument("--identity", default="clement")
    args = parser.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 900})
        page.add_init_script(
            f"localStorage.setItem('coloom.identity', {args.identity!r})"
        )
        page.goto(f"{args.base}/#/w/{args.weave}", wait_until="networkidle")
        page.wait_for_timeout(800)

        cards = page.locator("svg .card")
        before = cards.count()
        assert before > 0, "no cards rendered"

        def hover_my_card():
            # my cursor's card has the on-thread stroke; layout may move it, so
            # re-resolve its screen position every time
            box = page.locator("svg rect.bg.on-thread").last.bounding_box()
            assert box, "no on-thread card found"
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            page.wait_for_timeout(200)

        hover_my_card()
        gen_btn = page.locator("svg .gen")
        assert gen_btn.count() == 1, "hover toolbar did not appear"
        gen_btn.click(timeout=5000)  # must survive the pointer travelling to it
        page.wait_for_timeout(6000)  # real generation + ws + refetch
        after_gen = cards.count()
        assert after_gen > before, f"click '+' generated nothing ({before} -> {after_gen})"

        # collapse: hover my card again (layout shifted), click '…'
        hover_my_card()
        collapse_btn = page.locator("svg .collapse")
        assert collapse_btn.count() == 1, "collapse toggle did not appear on hover"
        collapse_btn.click(timeout=5000)
        page.wait_for_timeout(400)
        after_collapse = cards.count()
        assert after_collapse < after_gen, (
            f"collapse hid nothing ({after_gen} -> {after_collapse})"
        )
        browser.close()

    print(
        f"hover buttons clickable: gen {before}->{after_gen} cards, "
        f"collapse -> {after_collapse}"
    )


if __name__ == "__main__":
    main()
