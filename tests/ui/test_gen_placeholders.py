"""Gen placeholder nodes (task #24): while a generation is in flight, the tree
and canvas show one skeleton node per expected completion (gen_started carries
`n`), attached under the target node; they are filled in (replaced) by the real
nodes when completions arrive, and removed — with the error surfaced as a toast
— when the generation fails.

The in-flight window is held open deterministically via the gpt-fake mock's
per-request `delay` param (riding GenRequest.params straight into the fake's
request body). Generations are POSTed from a background thread (the REST call
blocks until inference finishes); the page only watches.
"""

import threading

import pytest

IDENTITY = "uitest-genph"


def get_weave(api, wid):
    r = api.get(f"/weaves/{wid}")
    r.raise_for_status()
    return r.json()


def node_text(node):
    c = node["content"]
    return c["text"] if c["type"] == "snippet" else "".join(t["text"] for t in c["tokens"])


def seeded_generator(api, profile):
    """A seeded generator that resolves to the ephemeral gpt-fake endpoint
    (generators are seeded per builtin template — preset names like 'default')."""
    gens = api.get("/generators", params={"profile": profile}).json()
    g = next(
        (g for g in gens if g["usable"] and "127.0.0.1" in (g["resolved"]["base_url"] or "")),
        None,
    )
    assert g, f"profile {profile} has no usable local-endpoint generator: " + str(
        [(x["name"], x["resolved"]["base_url"]) for x in gens]
    )
    return g


def generate_bg(api, wid, body):
    """POST /gen without blocking the test (it returns after inference)."""
    result = {}

    def run():
        r = api.post(f"/weaves/{wid}/gen", json=body, timeout=30)
        result["status"] = r.status_code

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, result


@pytest.fixture()
def root_node(api, weave):
    w = get_weave(api, weave)
    return next(r for r in w["roots"] if "loom hummed" in node_text(w["nodes"][r]))


def test_placeholders_appear_then_resolve_to_real_nodes(weave, page_as, api, root_node):
    page = page_as(IDENTITY, weave)
    gen = seeded_generator(api, IDENTITY)
    n_before = len(get_weave(api, weave)["nodes"])

    t, result = generate_bg(
        api, weave,
        {
            "node_id": root_node,
            "generator_id": gen["id"],
            # delay: held-open in-flight window; n=2: TWO placeholder skeletons
            "params": {"delay": 2.5, "n": 2, "max_tokens": 4},
        },
    )

    # skeletons appear in BOTH views while the generation is in flight
    tree_rows = page.locator(".sidebar .row.pending")
    canvas_cards = page.locator(".canvas rect.bg.pending")
    tree_rows.first.wait_for(state="visible", timeout=2000)
    assert tree_rows.count() == 2, f"expected 2 skeleton rows, got {tree_rows.count()}"
    assert canvas_cards.count() == 2, (
        f"expected 2 skeleton cards on the canvas, got {canvas_cards.count()}"
    )
    # placeholders are not click targets: clicking one must not move my cursor
    cursors = get_weave(api, weave)["cursors"]
    tree_rows.first.click()
    page.wait_for_timeout(300)
    assert get_weave(api, weave)["cursors"] == cursors, "clicking a skeleton moved a cursor"

    # completions arrive -> skeletons gone, the REAL nodes are in tree + server
    t.join(timeout=15)
    assert result.get("status") == 201, f"generation failed: {result}"
    tree_rows.first.wait_for(state="detached", timeout=4000)
    assert canvas_cards.count() == 0, "skeleton cards survived the finished generation"
    w = get_weave(api, weave)
    assert len(w["nodes"]) == n_before + 2, (
        f"expected exactly the 2 real completions, nodes {n_before} -> {len(w['nodes'])}"
    )
    assert page.locator(".sidebar .row.pending").count() == 0


def test_failed_generation_removes_placeholders_and_surfaces_error(
    weave, page_as, api, root_node
):
    page = page_as(IDENTITY, weave)
    # a generator pointing at a dead endpoint -> gen_started, then a failed finish
    r = api.post(
        "/generators",
        json={
            "profile": IDENTITY,
            "name": "uitest-dead-endpoint",
            "base_url": "http://127.0.0.1:9/v1",  # discard port: connection refused
            "model": "nope",
        },
    )
    r.raise_for_status()
    dead = r.json()

    t, result = generate_bg(
        api, weave,
        # retries: 0 — the dead endpoint would otherwise back off through 5
        # retry attempts (~20s) before the terminal 502
        {"node_id": root_node, "generator_id": dead["id"], "params": {"n": 3, "retries": 0}},
    )
    t.join(timeout=15)
    assert result.get("status") == 502, f"expected 502 from the dead endpoint: {result}"

    # placeholders (if their window was even observable) must be gone...
    page.wait_for_timeout(600)
    assert page.locator(".sidebar .row.pending").count() == 0
    assert page.locator(".canvas rect.bg.pending").count() == 0
    # ...and the failure is surfaced to everyone watching the weave
    toasts = page.locator(".toast")
    toasts.first.wait_for(state="visible", timeout=4000)
    assert any(
        "failed" in (toasts.nth(i).text_content() or "") for i in range(toasts.count())
    ), "no failure toast after a failed generation"


def test_placeholder_shows_retrying_label(weave, page_as, api, root_node):
    """gen_retrying events relabel the in-flight skeletons "retrying k/max — reason"
    (gpt-fake's fail_times/fail_key make the first request fail, then succeed)."""
    page = page_as(IDENTITY, weave)
    gen = seeded_generator(api, IDENTITY)

    t, result = generate_bg(
        api, weave,
        {
            "node_id": root_node,
            "generator_id": gen["id"],
            "params": {
                "n": 1,
                "max_tokens": 4,
                # 2 synthetic failures -> the retrying window spans two
                # backoffs (~3s): plenty for the poll to observe the label
                "fail_times": 2,
                "fail_key": f"uitest-retry-{weave[:8]}",
            },
        },
    )

    # during the backoff after a synthetic failure, the skeleton says so
    row = page.locator(".sidebar .row.pending")
    row.first.wait_for(state="visible", timeout=4000)
    assert wait_until(
        page,
        lambda: "retrying " in (row.first.text_content() or ""),
        deadline_s=8,
    ), f"skeleton not relabelled on retry: {row.first.text_content()!r}"

    # the retry succeeds: skeleton gone, real node landed
    t.join(timeout=20)
    assert result.get("status") == 201, f"retried generation failed: {result}"
    row.first.wait_for(state="detached", timeout=4000)


def wait_until(page, predicate, deadline_s=8.0, interval_ms=200):
    import time as _time

    end = _time.monotonic() + deadline_s
    val = predicate()
    while not val and _time.monotonic() < end:
        page.wait_for_timeout(interval_ms)
        val = predicate()
    return val
