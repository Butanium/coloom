"""Quick-row interaction authority (docs/optimistic-state.md LEG 3, incident 4):
while the user is mid-gesture on a quick param (typing in the input, dragging
the dragnum), a PATCH response / refreshGenerators must NOT re-seed the input
under their hands — the value must never jump backwards.

Scenario engineered to overlap: PATCH responses are route-DELAYED, a typed
value's PATCH is still in flight when a drag begins; the response (and its
refresh) lands mid-drag. The dragged value must rise monotonically, and the
released value must both persist server-side (commit-on-release) and survive
its own echo coming home.
"""

import time

IDENTITY = "uitest-quickrow"


def focused_generator(api, page):
    """The chip the quick row edits: the page's focused generator id."""
    gid = page.locator(".chip.focused").get_attribute("data-generator-id")
    assert gid, "no focused generator chip"
    return gid


def get_generator(api, gid, profile):
    gens = api.get("/generators", params={"profile": profile}).json()
    g = next((g for g in gens if g["id"] == gid), None)
    assert g, f"generator {gid} not found"
    return g


def test_drag_never_jumps_backwards_while_patch_lands(weave, page_as, api):
    page = page_as(IDENTITY, weave)
    gid = focused_generator(api, page)

    # hold every generator PATCH response back ~1.2s so it lands mid-drag
    def delay_patch(route):
        if route.request.method == "PATCH":
            page.wait_for_timeout(1200)
        route.continue_()

    page.route("**/generators/*", delay_patch)

    temp = page.locator('[data-testid="param-temp"]')
    # 1) type a value -> change fires -> debounced PATCH (held by the route)
    temp.click()
    temp.fill("0.5")
    temp.dispatch_event("change")
    page.wait_for_timeout(500)  # past the 400ms debounce: PATCH 1 in flight

    # 2) drag right while PATCH 1's response (+ refresh) lands underneath
    box = temp.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 6, cy)  # cross the 4px drag threshold
    samples = []
    for i in range(25):
        page.mouse.move(cx + 6 + (i + 1) * 4, cy)
        page.wait_for_timeout(40)
        v = temp.input_value()
        if v != "":
            samples.append(float(v))
    page.mouse.up()

    assert len(samples) >= 10, f"too few drag samples: {samples}"
    assert all(b >= a for a, b in zip(samples, samples[1:])), (
        f"value jumped BACKWARDS under the pointer (authority violated): {samples}"
    )
    assert samples[0] >= 0.5, f"drag did not start from the typed value: {samples}"

    # 3) commit-on-release: the released value persists and survives its echo
    final = float(temp.input_value())
    assert final >= samples[-1]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        g = get_generator(api, gid, IDENTITY)
        if g["params"].get("temperature") == final:
            break
        page.wait_for_timeout(200)
    assert get_generator(api, gid, IDENTITY)["params"].get("temperature") == final, (
        "released drag value did not persist (commit-on-release broken)"
    )
    page.wait_for_timeout(1000)  # the echo + refresh have long landed
    assert float(temp.input_value()) == final, (
        "the PATCH echo re-seeded the input away from the user's final value"
    )
