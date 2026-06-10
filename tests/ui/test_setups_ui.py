"""Adversarial UI tests for inference setups (docs/setups-api.md).

Setups are SERVER-GLOBAL state shared across all weaves and test files, so every
test scopes its assertions to ids it created and deletes them in a finally block.
We never assert the global list is empty (a parallel test file may have its own).

Effects are verified through the REST API (the `api` fixture), not the DOM alone.
"""

import json

import pytest
from playwright.sync_api import expect

SECRET = "sk-TEST-DO-NOT-LEAK-4242"


@pytest.fixture(autouse=True)
def _drawer_closed_after(api):
    """Drawer open/closed persists per profile (setSetting): reset it so tests
    in OTHER files sharing the uitest-* profiles never inherit an open drawer."""
    yield
    for p in api.get("/profiles").json():
        name = p["name"]
        if not name.startswith("uitest-"):
            continue
        settings = api.get(f"/profiles/{name}").json()["settings"]
        if settings.get("setupsDrawerOpen"):
            settings["setupsDrawerOpen"] = False
            api.put(f"/profiles/{name}", json={"settings": settings})


def _models(api):
    return api.get("/setups").json()["models"]


def _samplers(api):
    return api.get("/setups").json()["samplers"]


def _find(items, name):
    return next((x for x in items if x["name"] == name), None)


def _open_dialog(page):
    """Open the setups drawer if not already open (the setups button toggles)."""
    drawer = page.get_by_test_id("setups-drawer")
    if drawer.count() == 0:
        page.get_by_test_id("open-setups").click()
        page.wait_for_timeout(250)
    assert drawer.is_visible()


def _fill_params(page, testid, params):
    """Drive the ParamsEditor key/value rows (the UI never asks for raw JSON).
    `params` is a dict (or a JSON-object string, legacy callers); values are
    typed as JSON literals into the value field — strings go in bare."""
    if isinstance(params, str):
        params = json.loads(params) if params.strip() else {}
    # clear any pre-existing rows (edit flows repopulate the editor)
    while True:
        removes = page.locator(f'[data-testid^="{testid}-remove-"]')
        if removes.count() == 0:
            break
        removes.first.click()
    for i, (k, v) in enumerate(params.items()):
        page.get_by_test_id(f"{testid}-add").click()
        page.get_by_test_id(f"{testid}-name-{i}").fill(k)
        page.get_by_test_id(f"{testid}-value-{i}").fill(
            v if isinstance(v, str) else json.dumps(v)
        )


def _create_model(page, api, name, *, base_url="http://localhost:9999/v1",
                  model="gpt-fake", api_key=None, params="{}"):
    """Fill + submit the model form, return the created model's id (REST-verified)."""
    page.get_by_test_id("m-name").fill(name)
    page.get_by_test_id("m-base-url").fill(base_url)
    page.get_by_test_id("m-model").fill(model)
    if api_key is not None:
        page.get_by_test_id("m-api-key").fill(api_key)
    _fill_params(page, "m-params", params)
    page.get_by_test_id("m-save").click()
    page.wait_for_timeout(400)
    m = _find(_models(api), name)
    assert m is not None, f"model {name!r} was not created"
    return m["id"]


def _create_sampler(page, api, name, model_id, params="{}"):
    page.get_by_test_id("s-name").fill(name)
    page.get_by_test_id("s-model").select_option(model_id)
    _fill_params(page, "s-params", params)
    page.get_by_test_id("s-save").click()
    page.wait_for_timeout(400)
    s = _find(_samplers(api), name)
    assert s is not None, f"sampler {name!r} was not created"
    return s["id"]


# --------------------------------------------------------------------------- #


def test_create_model_with_arbitrary_flags_and_key_redaction(page_as, api, weave):
    """Model with logit_bias object + stop array survives the round trip;
    api_key is redacted to '***' in every response (never echoed)."""
    page = page_as("uitest-alice", weave)
    _open_dialog(page)
    mid = None
    try:
        mid = _create_model(
            page, api, "arb-flags", api_key=SECRET,
            params=json.dumps({
                "temperature": 0.8,
                "logit_bias": {"50256": -100, "123": 7},
                "stop": ["\n\n", "END"],
                "logprobs": 5,
            }),
        )
        m = _find(_models(api), "arb-flags")
        assert m["api_key"] == "***", "api_key must be redacted in GET"
        assert m["params"]["logit_bias"] == {"50256": -100, "123": 7}
        assert m["params"]["stop"] == ["\n\n", "END"]
        # secret never visible in any /setups response body
        assert SECRET not in api.get("/setups").text
        # nor in the rendered DOM (the open dialog renders from the redacted GET)
        assert page.get_by_test_id("model-list").get_by_text("arb-flags").is_visible()
        assert SECRET not in page.content()
    finally:
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_sampler_override_and_fanout_button(page_as, api, weave):
    """Two samplers over one model, both active → gen button shows fan-out ×2."""
    page = page_as("uitest-bob", weave)
    _open_dialog(page)
    mid = s_wild = s_safe = None
    try:
        mid = _create_model(page, api, "fanout-model",
                            params=json.dumps({"temperature": 0.8, "logprobs": 5}))
        s_wild = _create_sampler(page, api, "wild", mid, '{"temperature": 1.2}')
        s_safe = _create_sampler(page, api, "safe", mid, '{"temperature": 0.4}')
        page.get_by_test_id(f"s-active-{s_wild}").check()
        page.get_by_test_id(f"s-active-{s_safe}").check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert "×2" in page.get_by_test_id("gen-button").inner_text()
    finally:
        for sid in (s_wild, s_safe):
            if sid:
                api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_fanout_generation_merges_params(page_as, api, weave):
    """Activate 2 samplers, weave at cursor → children carry the MERGED params
    (model logit_bias + per-sampler temperature); the api_key never leaks."""
    # uitest-clement has a cursor in the seeded weave, so the gen button is enabled
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = s_hi = s_lo = None
    try:
        mid = _create_model(page, api, "merge-model", api_key=SECRET,
                            params=json.dumps({
                                "temperature": 0.8,
                                "logit_bias": {"50256": -100},
                                "logprobs": 5, "max_tokens": 6,
                            }))
        s_hi = _create_sampler(page, api, "hot", mid, '{"temperature": 1.3}')
        s_lo = _create_sampler(page, api, "cold", mid, '{"temperature": 0.2}')
        page.get_by_test_id(f"s-active-{s_hi}").check()
        page.get_by_test_id(f"s-active-{s_lo}").check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)

        cursor = api.get(f"/weaves/{weave}").json()["cursors"]["uitest-clement"]["node_id"]
        before = set(api.get(f"/weaves/{weave}").json()["nodes"])
        btn = page.get_by_test_id("gen-button")
        assert not btn.is_disabled()
        btn.click()
        page.wait_for_timeout(2500)

        nodes = api.get(f"/weaves/{weave}").json()["nodes"]
        new = [n for nid, n in nodes.items()
               if nid not in before and cursor in n["parents"]]
        temps = sorted(
            n["creator"]["raw_request"]["temperature"]
            for n in new if n["creator"].get("raw_request")
        )
        assert 1.3 in temps and 0.2 in temps, f"merged temps missing: {temps}"
        # model-level logit_bias rode through to both
        biased = [n for n in new
                  if n["creator"].get("raw_request", {}).get("logit_bias") == {"50256": -100}]
        assert len(biased) >= 2, "logit_bias did not merge into both children"
        # api_key redacted in raw_request; secret nowhere in the weave json
        assert SECRET not in api.get(f"/weaves/{weave}").text
    finally:
        for sid in (s_hi, s_lo):
            if sid:
                api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_rapid_fire_generate_inflight_resets(page_as, api, weave):
    """Three active samplers, click weave → inflight counter returns to 0."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = sids = None
    sids = []
    try:
        mid = _create_model(page, api, "rapid-model",
                            params=json.dumps({"logprobs": 5, "max_tokens": 5}))
        for nm, t in [("s1", "0.5"), ("s2", "0.9"), ("s3", "1.4")]:
            sids.append(_create_sampler(page, api, nm, mid, '{"temperature": %s}' % t))
        for sid in sids:
            page.get_by_test_id(f"s-active-{sid}").check()
        page.wait_for_timeout(150)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        assert "×3" in page.get_by_test_id("gen-button").inner_text()
        page.get_by_test_id("gen-button").click()
        page.wait_for_timeout(3500)
        # inflight counter back to 0 → the header pill is gone or shows 0
        inflight = page.evaluate(
            "() => document.querySelector('.inflight')?.textContent ?? ''"
        )
        assert "weaving" not in inflight, f"inflight stuck: {inflight!r}"
    finally:
        for sid in sids:
            api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_edit_model_while_sampler_active(page_as, api, weave):
    """Editing a model setup that an active sampler references keeps the sampler
    active and the new params take effect."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = sid = None
    try:
        mid = _create_model(page, api, "edit-model",
                            params=json.dumps({"temperature": 0.8, "logprobs": 5}))
        sid = _create_sampler(page, api, "rides", mid, '{}')
        page.get_by_test_id(f"s-active-{sid}").check()
        page.wait_for_timeout(150)
        # edit the model: change its model field + params
        page.get_by_test_id("model-list").get_by_role("button", name="edit").first.click()
        page.wait_for_timeout(150)
        _fill_params(page, "m-params", {"temperature": 0.55, "logprobs": 5, "top_p": 0.9})
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        m = _find(_models(api), "edit-model")
        assert m["params"]["top_p"] == 0.9, "edit did not persist"
        assert m["params"]["temperature"] == 0.55
        # sampler still active (localStorage untouched by an unrelated edit)
        page.wait_for_timeout(100)
        assert page.get_by_test_id(f"s-active-{sid}").is_checked()
    finally:
        if sid:
            api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_delete_referenced_model_surfaces_conflict(page_as, api, weave):
    """Deleting a model that a sampler references → 409, surfaced as a toast,
    not silently swallowed; the model stays."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = sid = None
    try:
        mid = _create_model(page, api, "referenced", params="{}")
        sid = _create_sampler(page, api, "ref-sampler", mid, '{}')
        # delete the model via its row's delete button (it's in the model list)
        page.get_by_test_id("model-list").get_by_role(
            "button", name="delete"
        ).first.click()
        page.wait_for_timeout(400)
        # model still present (delete was rejected)
        assert _find(_models(api), "referenced") is not None, "model wrongly deleted"
        # a toast surfaced the conflict (not swallowed)
        body = page.content().lower()
        assert "referenc" in body or "409" in body or "conflict" in body or \
            page.locator(".toast, [class*=toast]").count() > 0, \
            "conflict was not surfaced to the user"
    finally:
        if sid:
            api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_delete_active_sampler_drops_from_localstorage(page_as, api, weave):
    """Deleting a sampler that is active → UI drops it gracefully (no stale id,
    gen button reverts to plain weave)."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = sid = None
    try:
        mid = _create_model(page, api, "drop-model", params=json.dumps({"logprobs": 5}))
        sid = _create_sampler(page, api, "to-delete", mid, '{}')
        page.get_by_test_id(f"s-active-{sid}").check()
        page.wait_for_timeout(150)
        # delete it from its row
        page.get_by_test_id("sampler-list").get_by_role(
            "button", name="delete"
        ).first.click()
        page.wait_for_timeout(400)
        assert _find(_samplers(api), "to-delete") is None
        sid = None  # deleted
        # active set pruned: localStorage no longer holds it; gen button plain
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        gen = page.get_by_test_id("gen-button").inner_text()
        assert "×" not in gen, f"gen button still shows fan-out after delete: {gen!r}"
        stored = page.evaluate("() => localStorage.getItem('coloom.activeSamplers')")
        assert "to-delete" not in (stored or "")
    finally:
        if sid:
            api.delete(f"/setups/samplers/{sid}")
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_bad_param_rows_surface_not_swallowed(page_as, api, weave):
    """Invalid param rows (value without a name, duplicate names) must surface
    an inline error and NOT create the setup. (Values themselves can't be
    'malformed' anymore: non-JSON input is a legitimate string param.)"""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    try:
        page.get_by_test_id("m-name").fill("bad-rows")
        page.get_by_test_id("m-base-url").fill("http://localhost:9999/v1")
        page.get_by_test_id("m-model").fill("gpt-fake")
        # a value with no param name
        _fill_params(page, "m-params", {})
        page.get_by_test_id("m-params-add").click()
        page.get_by_test_id("m-params-value-0").fill("0.7")
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(300)
        assert page.get_by_test_id("m-error").is_visible(), "no error surfaced"
        assert "name" in page.get_by_test_id("m-error").inner_text().lower()
        assert _find(_models(api), "bad-rows") is None, "setup created despite bad row"
        # duplicate param names must also be rejected
        _fill_params(page, "m-params", {})
        for i, val in enumerate(["0.7", "0.9"]):
            page.get_by_test_id("m-params-add").click()
            page.get_by_test_id(f"m-params-name-{i}").fill("temperature")
            page.get_by_test_id(f"m-params-value-{i}").fill(val)
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(300)
        assert page.get_by_test_id("m-error").is_visible()
        assert "duplicate" in page.get_by_test_id("m-error").inner_text().lower()
        assert _find(_models(api), "bad-rows") is None
    finally:
        # nothing created on success path, but guard anyway
        m = _find(_models(api), "bad-rows")
        if m:
            api.delete(f"/setups/models/{m['id']}")


def test_bare_string_param_value_round_trips(page_as, api, weave):
    """A value typed without JSON quoting (e.g. a stop sequence 'END') is kept
    as a string; numbers/objects typed as JSON literals keep their types."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = None
    try:
        mid = _create_model(
            page, api, "bare-string",
            params={"stop": "END", "temperature": 0.5, "logit_bias": {"1": -5}},
        )
        m = _find(_models(api), "bare-string")
        assert m["params"]["stop"] == "END"
        assert m["params"]["temperature"] == 0.5
        assert m["params"]["logit_bias"] == {"1": -5}
    finally:
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_empty_name_rejected(page_as, api, weave):
    """A whitespace-only / empty name must be rejected client-side, no POST."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    before = len(_models(api))
    page.get_by_test_id("m-name").fill("   ")
    page.get_by_test_id("m-base-url").fill("http://localhost:9999/v1")
    page.get_by_test_id("m-model").fill("gpt-fake")
    page.get_by_test_id("m-save").click()
    page.wait_for_timeout(300)
    assert page.get_by_test_id("m-error").is_visible()
    assert len(_models(api)) == before, "an empty-name model was created"


def test_patch_api_key_set_keep_clear(page_as, api, weave):
    """PATCH api_key semantics through the UI: keep (omit) preserves, set replaces,
    clear removes. Verified via REST (a literal key shows '***', cleared shows null)."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    mid = None
    try:
        mid = _create_model(page, api, "key-model", api_key="sk-ORIGINAL-111", params="{}")
        assert _find(_models(api), "key-model")["api_key"] == "***"

        # edit, KEEP key (default mode on edit) → still set
        page.get_by_test_id("model-list").get_by_role("button", name="edit").first.click()
        page.wait_for_timeout(150)
        page.get_by_test_id("m-model").fill("gpt-fake-2")
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        m = _find(_models(api), "key-model")
        assert m["api_key"] == "***", "keep mode wrongly cleared the key"
        assert m["model"] == "gpt-fake-2"

        # edit, CLEAR key → null
        page.get_by_test_id("model-list").get_by_role("button", name="edit").first.click()
        page.wait_for_timeout(150)
        page.get_by_role("radio", name="clear").check()
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        assert _find(_models(api), "key-model")["api_key"] is None, "clear did not remove key"

        # edit, SET a new key → '***' again
        page.get_by_test_id("model-list").get_by_role("button", name="edit").first.click()
        page.wait_for_timeout(150)
        page.get_by_role("radio", name="set key").check()
        page.get_by_test_id("m-api-key").fill("sk-REPLACED-222")
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        assert _find(_models(api), "key-model")["api_key"] == "***", "set did not store new key"
        assert "sk-REPLACED-222" not in api.get("/setups").text
    finally:
        if mid:
            api.delete(f"/setups/models/{mid}")


def test_mutual_exclusion_api_key_and_env(page_as, api, weave):
    """Setting both api_key and api_key_env must be rejected (400 from server,
    surfaced as an inline error)."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    try:
        page.get_by_test_id("m-name").fill("mutex")
        page.get_by_test_id("m-base-url").fill("http://localhost:9999/v1")
        page.get_by_test_id("m-model").fill("gpt-fake")
        page.get_by_test_id("m-api-key").fill("sk-BOTH-SET")
        page.get_by_test_id("m-api-key-env").fill("OPENAI_API_KEY")
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        assert page.get_by_test_id("m-error").is_visible(), "mutual-exclusion not surfaced"
        assert _find(_models(api), "mutex") is None, "setup created despite key+env both set"
    finally:
        m = _find(_models(api), "mutex")
        if m:
            api.delete(f"/setups/models/{m['id']}")


def test_generator_menu_hide_and_multi_active_presets(page_as, api, weave):
    """The generators menu hides/shows chips; preset chips toggle multi-active."""
    page = page_as("uitest-clement", weave)
    presets = list(api.get("/presets").json()["presets"])
    # activate two presets via their chips
    page.get_by_test_id(f"gc-preset-{presets[0]}").click()
    page.get_by_test_id(f"gc-preset-{presets[1]}").click()
    expect(page.locator(".generators .chip.active")).to_have_count(2)
    expect(page.get_by_test_id("gen-button")).to_contain_text("×2")
    # hide the first chip via the menu (stays ACTIVE, just not displayed)
    page.get_by_test_id("open-gen-menu").click()
    before = page.locator(".generators .chip").count()
    page.get_by_test_id(f"menu-hide-preset-{presets[0]}").click()
    expect(page.locator(".generators .chip")).to_have_count(before - 1)
    # unhide restores it
    page.get_by_test_id(f"menu-hide-preset-{presets[0]}").click()
    expect(page.locator(".generators .chip")).to_have_count(before)


def test_drawer_collapses_and_canvas_stays_interactive(page_as, api, weave):
    """The drawer is NON-MODAL: chips and canvas stay usable while it's open;
    collapse/expand works from both buttons; open state roams with the profile."""
    page = page_as("uitest-clement", weave)
    _open_dialog(page)
    drawer = page.get_by_test_id("setups-drawer")

    # chips row above the drawer stays clickable (no backdrop intercepts)
    presets = list(api.get("/presets").json()["presets"])
    chip = page.get_by_test_id(f"gc-preset-{presets[0]}")
    chip.click()
    expect(page.locator(".generators .chip.active")).to_have_count(1)
    chip.click()  # restore
    expect(page.locator(".generators .chip.active")).to_have_count(0)

    # the canvas behind it is interactive: right-click a card opens the menu
    page.keyboard.press("Control+0")  # fit weave so cards are in view
    page.wait_for_timeout(200)
    card = page.locator(".canvas .text").filter(has_text="The loom hummed").first
    card.click(button="right")
    expect(page.locator(".menu[role='menu']")).to_be_visible()
    # click-away closes the menu but NOT the drawer (there is no overlay)
    canvas_box = page.locator(".canvas").bounding_box()
    page.mouse.click(canvas_box["x"] + 8, canvas_box["y"] + 8)
    expect(page.locator(".menu[role='menu']")).to_have_count(0)
    expect(drawer).to_be_visible()

    # collapse via the drawer's own button; persisted closed across reload
    page.get_by_test_id("setups-collapse").click()
    expect(drawer).to_have_count(0)
    page.wait_for_timeout(1200)  # setSetting debounce (800ms) must flush its PUT
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    expect(page.get_by_test_id("setups-drawer")).to_have_count(0)

    # reopen via the toggle button; persisted open across reload
    page.get_by_test_id("open-setups").click()
    expect(page.get_by_test_id("setups-drawer")).to_be_visible()
    page.wait_for_timeout(1200)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    expect(page.get_by_test_id("setups-drawer")).to_be_visible()


def test_clone_preset_into_editable_setup(page_as, api, weave):
    """'→ setup' on a preset prefills the model form (name/base_url/model/params)
    — create it and the params survive as an editable model setup."""
    page = page_as("uitest-clement", weave)
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-clone-preset-default").click()
    page.wait_for_timeout(400)
    assert page.get_by_test_id("m-name").input_value() == "default"
    assert page.get_by_test_id("m-base-url").input_value() != ""
    assert page.get_by_test_id("m-model").input_value() == "gpt-fake"
    # the preset's params arrived as editable rows
    assert page.get_by_test_id("m-params-name-0").input_value() != ""
    mid = None
    try:
        page.get_by_test_id("m-name").fill("cloned-from-default")
        page.get_by_test_id("m-save").click()
        page.wait_for_timeout(400)
        m = _find(_models(api), "cloned-from-default")
        assert m is not None, "cloned setup not created"
        mid = m["id"]
        preset = api.get("/presets").json()["presets"]["default"]
        assert m["params"] == preset["params"]
        assert m["base_url"] == preset["base_url"]
    finally:
        if mid:
            api.delete(f"/setups/models/{mid}")
