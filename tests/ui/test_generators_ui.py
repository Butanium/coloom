"""Adversarial UI tests for templates + per-profile generators
(docs/generators-api.md). Supersedes the setups-era test_setups_ui.py —
coverage ported to the new model, plus the focus/dot chip semantics, the
quick-row → focused-generator binding, stale-ancestor badges, promote, and
inherit-vs-duplicate create modes.

Templates are SERVER-GLOBAL; generators are per-profile, and page_as() resets
its profile server-side, so each test's generators start from the builtin
seeding (one generator per coloom.yaml preset: default, fake-slow, real, wild,
single-token). Templates created by a test are deleted in a finally block; we
never assert the global template list is empty.

Effects are verified through the REST API (the `api` fixture), not the DOM alone.
"""

import json
import os
import re

import pytest
from playwright.sync_api import expect

SECRET = "sk-TEST-DO-NOT-LEAK-4242"
FAKE_URL = "http://localhost:9999/v1"
BASE = os.environ.get("COLOOM_UI_BASE", "http://localhost:5174")


# templates this file creates — swept at session start so a previously
# crashed run can't leak duplicates (templates are server-global; we only
# ever delete the names we own, never builtins or anyone else's)
_TEST_TEMPLATE_NAMES = {"badge-tpl", "promo-me"}


@pytest.fixture(autouse=True, scope="module")
def _sweep_leftover_test_templates(api):
    for t in api.get("/templates").json():
        if not t["builtin"] and t["name"] in _TEST_TEMPLATE_NAMES:
            api.delete(f"/templates/{t['id']}")
    yield


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
        if settings.get("generatorsDrawerOpen"):
            settings["generatorsDrawerOpen"] = False
            api.put(f"/profiles/{name}", json={"settings": settings})


def _templates(api):
    return api.get("/templates").json()


def _gens(api, profile):
    return api.get(f"/generators?profile={profile}").json()


def _find(items, name):
    return next((x for x in items if x["name"] == name), None)


def _builtin(api, name):
    t = next((t for t in _templates(api) if t["name"] == name and t["builtin"]), None)
    assert t is not None, f"builtin template {name!r} missing (yaml seeding broken?)"
    return t


def _open_drawer(page):
    """Open the generators drawer if not already open (the button toggles)."""
    drawer = page.get_by_test_id("generators-drawer")
    if drawer.count() == 0:
        page.get_by_test_id("open-generators").click()
        page.wait_for_timeout(250)
    assert drawer.is_visible()


def _fill_params(page, testid, params):
    """Drive the ParamsEditor key/value rows (the UI never asks for raw JSON)."""
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


def _save_form(page):
    page.get_by_test_id("f-save").click()
    page.wait_for_timeout(400)


def _create_scratch_generator(page, api, profile, name, *, base_url=FAKE_URL,
                              model="gpt-fake", api_key=None, params="{}"):
    """Create-from-scratch via the drawer's create panel, fill the edit form
    that opens on the fresh generator, save. Returns the id (REST-verified)."""
    _open_drawer(page)
    page.get_by_test_id("create-source").select_option("")
    page.get_by_test_id("create-name").fill(name)
    page.get_by_test_id("create-generator").click()
    page.wait_for_timeout(400)
    # the form jumps straight into editing the new generator
    expect(page.get_by_test_id("f-name")).to_have_value(name)
    page.get_by_test_id("f-base-url").fill(base_url)
    page.get_by_test_id("f-model").fill(model)
    if api_key is not None:
        page.get_by_role("radio", name="set", exact=True).check()
        page.get_by_test_id("f-api-key").fill(api_key)
    _fill_params(page, "f-params", params)
    _save_form(page)
    g = _find(_gens(api, profile), name)
    assert g is not None, f"generator {name!r} was not created"
    return g["id"]


def _create_child_generator(page, api, profile, name, source_value,
                            mode="inherit", params=None):
    """Create from template/generator via the create panel; optionally add
    param overrides through the edit form. source_value: 'template:<id>' or
    'generator:<id>'. Returns the id."""
    _open_drawer(page)
    page.get_by_test_id("create-source").select_option(source_value)
    page.get_by_test_id(f"create-mode-{mode}").check()
    page.get_by_test_id("create-name").fill(name)
    page.get_by_test_id("create-generator").click()
    page.wait_for_timeout(400)
    if params is not None:
        _fill_params(page, "f-params", params)
        _save_form(page)
    g = _find(_gens(api, profile), name)
    assert g is not None, f"generator {name!r} was not created"
    return g["id"]


def _activate(page, name):
    """Toggle a chip ACTIVE via its leading dot (never the body — that's focus)."""
    page.get_by_test_id(f"gc-dot-{name}").click()
    page.wait_for_timeout(100)


# --------------------------------------------------------------------------- #
# chips: focus vs active


def test_chip_body_focuses_dot_activates(page_as, api, weave):
    """Body click = focus (exactly one focused, quick row binds); dot click =
    toggle active. Focus default: first active, else first visible."""
    page = page_as("uitest-alice", weave)
    chips = page.locator(".generators .chip")
    assert chips.count() >= 2, "expected seeded generator chips"
    focused = page.locator(".generators .chip.focused")

    # nothing active → default focus is the FIRST visible chip
    expect(page.locator(".generators .chip.active")).to_have_count(0)
    expect(focused).to_have_count(1)
    assert "default" in focused.inner_text()

    # body click on another chip moves focus (still exactly one), no activation
    page.get_by_test_id("gc-body-fake-slow").click()
    expect(focused).to_have_count(1)
    assert "fake-slow" in focused.inner_text()
    expect(page.locator(".generators .chip.active")).to_have_count(0)

    # dot click activates WITHOUT stealing focus
    _activate(page, "wild")
    expect(page.get_by_test_id("gc-gen-wild")).to_have_class(re.compile(r"\bactive\b"))
    assert "fake-slow" in focused.inner_text(), "dot click must not move focus"

    # dot click again deactivates
    _activate(page, "wild")
    expect(page.locator(".generators .chip.active")).to_have_count(0)

    # default focus prefers the first ACTIVE chip when the explicit focus is gone
    _activate(page, "wild")
    page.wait_for_timeout(1200)  # let the debounced settings PUT flush
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(800)
    focused = page.locator(".generators .chip.focused")
    expect(focused).to_have_count(1)
    assert "wild" in focused.inner_text()


def test_two_active_generators_fanout_button(page_as, api, weave):
    """Two chips active via their dots → gen button shows fan-out ×2."""
    page = page_as("uitest-bob", weave)
    _activate(page, "default")
    _activate(page, "fake-slow")
    expect(page.locator(".generators .chip.active")).to_have_count(2)
    assert "×2" in page.get_by_test_id("gen-button").inner_text()


def test_menu_hide_and_unhide_chips(page_as, api, weave):
    """The generators menu hides/shows chips; hiding never deletes."""
    page = page_as("uitest-clement", weave)
    before = page.locator(".generators .chip").count()
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-hide-fake-slow").click()
    expect(page.locator(".generators .chip")).to_have_count(before - 1)
    # still exists server-side: hide is a client setting, not a delete
    assert _find(_gens(api, "uitest-clement"), "fake-slow") is not None
    page.get_by_test_id("menu-hide-fake-slow").click()
    expect(page.locator(".generators .chip")).to_have_count(before)


def test_activating_hidden_generator_unhides_its_chip(page_as, api, weave):
    """Activating a hidden generator from the menu also un-hides it — an
    active-but-invisible chip would be confusing."""
    page = page_as("uitest-clement", weave)
    chip = page.get_by_test_id("gc-gen-default")
    expect(chip).to_be_visible()
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-hide-default").click()
    expect(chip).to_have_count(0)
    page.get_by_test_id("menu-active-default").check()
    expect(chip).to_be_visible()
    expect(chip).to_have_class(re.compile(r"\bactive\b"))
    # the eye button flipped back to "hide" mode (icon button — check the title)
    assert "hide" in page.get_by_test_id("menu-hide-default").get_attribute("title")


# --------------------------------------------------------------------------- #
# quick row → focused generator


def test_quick_row_edits_focused_generator_persisted(page_as, api, weave):
    """Typing temp in the quick row PATCHes the FOCUSED generator's params
    (debounced); the placeholder shows the resolved inherited value; emptying
    the field clears the override back to inherited. REST-verified."""
    page = page_as("uitest-alice", weave)
    page.get_by_test_id("gc-body-default").click()

    tpl = _builtin(api, "default")
    inherited_temp = tpl["params"]["temperature"]
    temp = page.get_by_test_id("param-temp")
    # no override yet → empty value, inherited placeholder
    expect(temp).to_have_value("")
    assert float(temp.get_attribute("placeholder")) == inherited_temp

    temp.fill("0.55")
    temp.dispatch_event("change")
    page.wait_for_timeout(900)  # 400ms debounce + PATCH + refresh
    g = _find(_gens(api, "uitest-alice"), "default")
    assert g["params"].get("temperature") == 0.55, f"override not persisted: {g['params']}"
    assert g["resolved"]["params"]["temperature"] == 0.55

    # emptying clears the override (params key → null) → inherited again
    temp.fill("")
    temp.dispatch_event("change")
    page.wait_for_timeout(900)
    g = _find(_gens(api, "uitest-alice"), "default")
    assert "temperature" not in g["params"], f"override not cleared: {g['params']}"
    assert g["resolved"]["params"]["temperature"] == inherited_temp


def test_quick_row_rebinds_on_focus_change(page_as, api, weave):
    """Focusing another chip re-seeds the quick row from THAT generator: own
    overrides as values, inherited template params as placeholders."""
    page = page_as("uitest-alice", weave)
    # give fake-slow an OWN override first (seeded generators start empty —
    # the preset params live in the builtin template)
    page.get_by_test_id("gc-body-fake-slow").click()
    mt = page.get_by_test_id("param-max-tokens")
    expect(mt).to_have_value("")  # no override yet
    assert mt.get_attribute("placeholder") == "48"  # inherited from its template
    mt.fill("64")
    mt.dispatch_event("change")
    page.wait_for_timeout(900)
    # focus default: no override there, placeholder = ITS template value
    page.get_by_test_id("gc-body-default").click()
    expect(mt).to_have_value("")
    assert mt.get_attribute("placeholder") == "24"
    # back to fake-slow: the override re-appears
    page.get_by_test_id("gc-body-fake-slow").click()
    expect(mt).to_have_value("64")


def test_param_inputs_drag_to_adjust_persists(page_as, api, weave):
    """Drag-to-adjust on the quick row edits the focused generator: drag right
    increases, far left clamps at the min, and the result lands in the
    generator's params via the same debounced PATCH."""
    page = page_as("uitest-clement", weave)
    page.get_by_test_id("gc-body-default").click()
    temp = page.get_by_test_id("param-temp")
    temp.fill("1")
    temp.dispatch_event("change")
    page.wait_for_timeout(900)

    box = temp.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    # drag right 50px → +0.5 at speed 0.01/px
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 50, cy, steps=8)
    page.mouse.up()
    assert temp.input_value() == "1.5", f"drag right: {temp.input_value()!r}"
    page.wait_for_timeout(900)
    g = _find(_gens(api, "uitest-clement"), "default")
    assert g["params"].get("temperature") == 1.5, "dragged value not persisted"

    # drag far left → clamped at min 0
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx - 400, cy, steps=8)
    page.mouse.up()
    assert temp.input_value() == "0", f"drag-left clamp: {temp.input_value()!r}"

    # a plain click (no movement past the 4px threshold) still focuses for typing
    temp.click()
    expect(temp).to_be_focused()


# --------------------------------------------------------------------------- #
# generation


def test_fanout_generation_merges_params(page_as, api, weave):
    """Two generators inheriting one scratch parent (api_key + logit_bias),
    each overriding temperature → weave fans out ×2, children carry the MERGED
    params; the api_key never leaks anywhere."""
    page = page_as("uitest-clement", weave)
    parent_id = _create_scratch_generator(
        page, api, "uitest-clement", "merge-parent", api_key=SECRET,
        params={"temperature": 0.8, "logit_bias": {"50256": -100},
                "logprobs": 5, "max_tokens": 6},
    )
    _create_child_generator(page, api, "uitest-clement", "hot",
                            f"generator:{parent_id}", params={"temperature": 1.3})
    _create_child_generator(page, api, "uitest-clement", "cold",
                            f"generator:{parent_id}", params={"temperature": 0.2})
    page.keyboard.press("Escape")  # collapse the drawer
    page.wait_for_timeout(200)
    _activate(page, "hot")
    _activate(page, "cold")
    assert "×2" in page.get_by_test_id("gen-button").inner_text()

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
    biased = [n for n in new
              if n["creator"].get("raw_request", {}).get("logit_bias") == {"50256": -100}]
    assert len(biased) >= 2, "parent logit_bias did not merge into both children"
    # api_key redacted in raw_request; secret nowhere in the weave json
    assert SECRET not in api.get(f"/weaves/{weave}").text
    assert SECRET not in api.get("/generators?profile=uitest-clement").text


def test_nothing_active_falls_back_to_focused(page_as, api, weave):
    """With NO active generator, the weave button generates with the FOCUSED
    one (fresh profiles can weave without ceremony)."""
    page = page_as("uitest-clement", weave)
    expect(page.locator(".generators .chip.active")).to_have_count(0)
    page.get_by_test_id("gc-body-fake-slow").click()
    before = set(api.get(f"/weaves/{weave}").json()["nodes"])
    page.get_by_test_id("gen-button").click()
    page.wait_for_timeout(2500)
    nodes = api.get(f"/weaves/{weave}").json()["nodes"]
    new = [n for nid, n in nodes.items() if nid not in before]
    assert len(new) >= 1, "focused-fallback generation produced nothing"
    reqs = [n["creator"].get("raw_request", {}) for n in new
            if n["creator"].get("raw_request")]
    assert all(r.get("max_tokens") == 48 for r in reqs), (
        f"fallback did not use the focused generator (fake-slow, max_tokens=48): {reqs}"
    )


def test_rapid_fire_generate_inflight_resets(page_as, api, weave):
    """Three active generators, click weave → inflight counter returns to 0."""
    page = page_as("uitest-clement", weave)
    parent_id = _create_scratch_generator(
        page, api, "uitest-clement", "rapid-parent",
        params={"logprobs": 5, "max_tokens": 5})
    for nm, t in [("s1", 0.5), ("s2", 0.9), ("s3", 1.4)]:
        _create_child_generator(page, api, "uitest-clement", nm,
                                f"generator:{parent_id}", params={"temperature": t})
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    for nm in ("s1", "s2", "s3"):
        _activate(page, nm)
    assert "×3" in page.get_by_test_id("gen-button").inner_text()
    page.get_by_test_id("gen-button").click()
    page.wait_for_timeout(3500)
    inflight = page.evaluate(
        "() => document.querySelector('.inflight')?.textContent ?? ''"
    )
    assert "weaving" not in inflight, f"inflight stuck: {inflight!r}"


# --------------------------------------------------------------------------- #
# drawer: create / edit / inherit / promote / delete


def test_create_generator_with_arbitrary_flags_and_key_redaction(page_as, api, weave):
    """Scratch generator with logit_bias object + stop array survives the round
    trip; api_key is redacted to '***' in every response (never echoed)."""
    page = page_as("uitest-alice", weave)
    _create_scratch_generator(
        page, api, "uitest-alice", "arb-flags", api_key=SECRET,
        params={
            "temperature": 0.8,
            "logit_bias": {"50256": -100, "123": 7},
            "stop": ["\n\n", "END"],
            "logprobs": 5,
        },
    )
    g = _find(_gens(api, "uitest-alice"), "arb-flags")
    assert g["api_key"] == "***", "api_key must be redacted in GET"
    assert g["params"]["logit_bias"] == {"50256": -100, "123": 7}
    assert g["params"]["stop"] == ["\n\n", "END"]
    assert SECRET not in api.get("/generators?profile=uitest-alice").text
    # nor in the rendered DOM (the open drawer renders from the redacted GET)
    assert page.get_by_test_id("generator-list").get_by_text("arb-flags").is_visible()
    assert SECRET not in page.content()


def test_inherit_vs_duplicate_create_modes(page_as, api, weave):
    """inherit → parent=source, empty overrides (follows future changes);
    duplicate from a generator → literal row copy (same parent, same overrides,
    no link to the source)."""
    page = page_as("uitest-bob", weave)
    tpl = _builtin(api, "default")
    src_id = _create_child_generator(
        page, api, "uitest-bob", "dup-src", f"template:{tpl['id']}",
        params={"temperature": 1.1})

    inh_id = _create_child_generator(page, api, "uitest-bob", "via-inherit",
                                     f"generator:{src_id}", mode="inherit")
    dup_id = _create_child_generator(page, api, "uitest-bob", "via-duplicate",
                                     f"generator:{src_id}", mode="duplicate")
    gens = _gens(api, "uitest-bob")
    inh = next(g for g in gens if g["id"] == inh_id)
    dup = next(g for g in gens if g["id"] == dup_id)
    assert inh["parent"] == {"kind": "generator", "id": src_id}
    assert inh["params"] == {}, "inherit mode must start with empty overrides"
    assert inh["resolved"]["params"]["temperature"] == 1.1  # rides on the source
    assert dup["parent"] == {"kind": "template", "id": tpl["id"]}, (
        "duplicate must copy the SOURCE's parent, not link to the source"
    )
    assert dup["params"] == {"temperature": 1.1}, "duplicate must copy overrides"


def test_drawer_inherited_placeholders_and_clear_to_inherit(page_as, api, weave):
    """The edit form shows inherited values as placeholders on un-overridden
    fields; 'clear to inherit' empties an override and the save falls back to
    the inherited value (REST-verified)."""
    page = page_as("uitest-alice", weave)
    tpl = _builtin(api, "default")
    gid = _create_child_generator(page, api, "uitest-alice", "ph-test",
                                  f"template:{tpl['id']}")
    _open_drawer(page)
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    # un-overridden fields are empty with the template's values as placeholders
    expect(page.get_by_test_id("f-base-url")).to_have_value("")
    assert page.get_by_test_id("f-base-url").get_attribute("placeholder") == tpl["base_url"]
    assert page.get_by_test_id("f-model").get_attribute("placeholder") == tpl["model"]
    # inherited params surface in the form too
    inh = page.get_by_test_id("f-inherited-params").inner_text()
    assert "temperature" in inh

    # type an override, save → persisted; then clear-to-inherit → back to null
    page.get_by_test_id("f-model").fill("gpt-fake-other")
    _save_form(page)
    g = next(x for x in _gens(api, "uitest-alice") if x["id"] == gid)
    assert g["model"] == "gpt-fake-other"
    assert g["resolved"]["model"] == "gpt-fake-other"

    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    expect(page.get_by_test_id("f-model")).to_have_value("gpt-fake-other")
    page.get_by_test_id("f-clear-model").click()
    expect(page.get_by_test_id("f-model")).to_have_value("")
    _save_form(page)
    g = next(x for x in _gens(api, "uitest-alice") if x["id"] == gid)
    assert g["model"] is None, "clear-to-inherit did not null the override"
    assert g["resolved"]["model"] == tpl["model"]


def test_edit_parent_generator_propagates_to_active_child(page_as, api, weave):
    """Editing a parent generator's params while an inheriting child is active:
    the child stays active and resolves the new value."""
    page = page_as("uitest-clement", weave)
    parent_id = _create_scratch_generator(
        page, api, "uitest-clement", "prop-parent",
        params={"temperature": 0.8, "logprobs": 5})
    child_id = _create_child_generator(page, api, "uitest-clement", "prop-child",
                                       f"generator:{parent_id}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    _activate(page, "prop-child")
    _open_drawer(page)
    page.get_by_test_id(f"g-edit-{parent_id}").click()
    page.wait_for_timeout(150)
    _fill_params(page, "f-params", {"temperature": 0.55, "logprobs": 5, "top_p": 0.9})
    _save_form(page)
    child = next(g for g in _gens(api, "uitest-clement") if g["id"] == child_id)
    assert child["resolved"]["params"]["temperature"] == 0.55
    assert child["resolved"]["params"]["top_p"] == 0.9
    assert page.get_by_test_id(f"g-active-{child_id}").is_checked(), (
        "active set must survive an unrelated parent edit"
    )


def test_promote_generator_to_template(page_as, api, weave):
    """'promote to template' materializes the generator's RESOLVED fields into
    a new server-global template; the literal api_key stays redacted."""
    page = page_as("uitest-bob", weave)
    parent_id = _create_scratch_generator(
        page, api, "uitest-bob", "promo-base", api_key=SECRET,
        params={"temperature": 0.7, "logprobs": 5})
    gid = _create_child_generator(page, api, "uitest-bob", "promo-me",
                                  f"generator:{parent_id}",
                                  params={"temperature": 1.2})
    tid = None
    try:
        _open_drawer(page)
        page.get_by_test_id(f"g-promote-{gid}").click()
        page.wait_for_timeout(400)
        t = _find(_templates(api), "promo-me")
        assert t is not None, "promoted template not created"
        tid = t["id"]
        assert not t["builtin"]
        assert t["base_url"] == FAKE_URL, "resolved base_url not materialized"
        assert t["model"] == "gpt-fake"
        assert t["params"]["temperature"] == 1.2, "leaf override must win in the promote"
        assert t["params"]["logprobs"] == 5, "inherited params must materialize"
        assert SECRET not in api.get("/templates").text
    finally:
        if tid:
            api.delete(f"/templates/{tid}")


def test_delete_parent_generator_flattens_children(page_as, api, weave):
    """Deleting a generator with children warns (confirm) and FLATTENS them:
    the resolved fields are materialized into the child, which keeps working."""
    page = page_as("uitest-alice", weave)
    parent_id = _create_scratch_generator(
        page, api, "uitest-alice", "flat-parent",
        params={"temperature": 0.9, "logprobs": 5})
    child_id = _create_child_generator(page, api, "uitest-alice", "flat-child",
                                       f"generator:{parent_id}")
    _open_drawer(page)
    page.get_by_test_id(f"g-delete-{parent_id}").click()
    # the IN-APP confirm (no native popup) warns about the flattened child
    dialog = page.get_by_test_id("confirm-dialog")
    expect(dialog).to_be_visible()
    assert "flat-child" in dialog.inner_text(), (
        f"delete confirm must warn about flattened children: {dialog.inner_text()!r}"
    )
    page.get_by_test_id("confirm-ok").click()
    page.wait_for_timeout(400)
    expect(dialog).to_have_count(0)
    gens = _gens(api, "uitest-alice")
    assert _find(gens, "flat-parent") is None
    child = next((g for g in gens if g["id"] == child_id), None)
    assert child is not None, "child must survive the parent delete"
    assert child["base_url"] == FAKE_URL, "flatten must materialize base_url"
    assert child["model"] == "gpt-fake"
    assert child["params"].get("temperature") == 0.9
    assert child["resolved"]["base_url"] == FAKE_URL


def test_delete_active_generator_drops_from_active_set(page_as, api, weave):
    """Deleting a generator that is active → UI drops it gracefully (no stale
    id in the profile settings, gen button reverts to plain weave)."""
    page = page_as("uitest-clement", weave)
    gid = _create_scratch_generator(page, api, "uitest-clement", "to-delete",
                                    params={"logprobs": 5})
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    _activate(page, "to-delete")
    assert "×" not in page.get_by_test_id("gen-button").inner_text()  # 1 active = plain
    _activate(page, "default")
    assert "×2" in page.get_by_test_id("gen-button").inner_text()
    _open_drawer(page)
    page.get_by_test_id(f"g-delete-{gid}").click()
    page.get_by_test_id("confirm-ok").click()
    page.wait_for_timeout(400)
    assert _find(_gens(api, "uitest-clement"), "to-delete") is None
    page.keyboard.press("Escape")
    page.wait_for_timeout(1200)  # active-set prune persists via debounced PUT
    gen = page.get_by_test_id("gen-button").inner_text()
    assert "×" not in gen, f"gen button still shows fan-out after delete: {gen!r}"
    settings = api.get("/profiles/uitest-clement").json()["settings"]
    assert gid not in (settings.get("activeGenerators") or []), (
        "deleted generator id must be pruned from the persisted active set"
    )


def test_builtin_template_read_only_with_new_generator_from_this(page_as, api, weave):
    """Builtin templates show no edit/delete; 'new generator from this' prefills
    the create panel and the created generator inherits the template."""
    page = page_as("uitest-bob", weave)
    tpl = _builtin(api, "default")
    _open_drawer(page)
    row = page.get_by_test_id("template-list").locator("li", has_text="default").first
    assert row.get_by_test_id(f"t-edit-{tpl['id']}").count() == 0, "builtin must not be editable"
    assert row.get_by_test_id(f"t-delete-{tpl['id']}").count() == 0, "builtin must not be deletable"
    page.get_by_test_id(f"t-new-gen-{tpl['id']}").click()
    expect(page.get_by_test_id("create-source")).to_have_value(f"template:{tpl['id']}")
    page.get_by_test_id("create-name").fill("from-builtin")
    page.get_by_test_id("create-generator").click()
    page.wait_for_timeout(400)
    g = _find(_gens(api, "uitest-bob"), "from-builtin")
    assert g is not None
    assert g["parent"] == {"kind": "template", "id": tpl["id"]}
    assert g["resolved"]["base_url"] == tpl["base_url"]
    # and the API agrees it's read-only (403, surfaced — adversarial belt+braces)
    r = api.patch(f"/templates/{tpl['id']}", json={"model": "evil"})
    assert r.status_code == 403


def test_bad_param_rows_surface_not_swallowed(page_as, api, weave):
    """Invalid param rows (value without a name, duplicate names) must surface
    an inline error and NOT persist."""
    page = page_as("uitest-clement", weave)
    gid = _create_scratch_generator(page, api, "uitest-clement", "bad-rows")
    _open_drawer(page)
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    # a value with no param name
    _fill_params(page, "f-params", {})
    page.get_by_test_id("f-params-add").click()
    page.get_by_test_id("f-params-value-0").fill("0.7")
    page.get_by_test_id("f-save").click()
    page.wait_for_timeout(300)
    assert page.get_by_test_id("f-error").is_visible(), "no error surfaced"
    assert "name" in page.get_by_test_id("f-error").inner_text().lower()
    g = _find(_gens(api, "uitest-clement"), "bad-rows")
    assert g["params"] == {}, "bad rows must not persist"
    # duplicate param names must also be rejected
    _fill_params(page, "f-params", {})
    for i, val in enumerate(["0.7", "0.9"]):
        page.get_by_test_id("f-params-add").click()
        page.get_by_test_id(f"f-params-name-{i}").fill("temperature")
        page.get_by_test_id(f"f-params-value-{i}").fill(val)
    page.get_by_test_id("f-save").click()
    page.wait_for_timeout(300)
    assert page.get_by_test_id("f-error").is_visible()
    assert "duplicate" in page.get_by_test_id("f-error").inner_text().lower()
    g = _find(_gens(api, "uitest-clement"), "bad-rows")
    assert g["params"] == {}


def test_bare_string_param_value_round_trips(page_as, api, weave):
    """A value typed without JSON quoting (e.g. a stop sequence 'END') is kept
    as a string; numbers/objects typed as JSON literals keep their types."""
    page = page_as("uitest-clement", weave)
    _create_scratch_generator(
        page, api, "uitest-clement", "bare-string",
        params={"stop": "END", "temperature": 0.5, "logit_bias": {"1": -5}},
    )
    g = _find(_gens(api, "uitest-clement"), "bare-string")
    assert g["params"]["stop"] == "END"
    assert g["params"]["temperature"] == 0.5
    assert g["params"]["logit_bias"] == {"1": -5}


def test_empty_name_rejected(page_as, api, weave):
    """A whitespace-only scratch-create name is rejected client-side, no POST."""
    page = page_as("uitest-clement", weave)
    _open_drawer(page)
    before = len(_gens(api, "uitest-clement"))
    page.get_by_test_id("create-source").select_option("")
    page.get_by_test_id("create-name").fill("   ")
    page.get_by_test_id("create-generator").click()
    page.wait_for_timeout(300)
    assert page.get_by_test_id("create-error").is_visible()
    assert len(_gens(api, "uitest-clement")) == before, "an empty-name generator was created"


def test_patch_api_key_set_keep_clear(page_as, api, weave):
    """api_key semantics through the edit form: keep (omit) preserves, set
    replaces, clear removes. Verified via REST ('***' vs null)."""
    page = page_as("uitest-clement", weave)
    gid = _create_scratch_generator(page, api, "uitest-clement", "key-gen",
                                    api_key="sk-ORIGINAL-111")
    assert _find(_gens(api, "uitest-clement"), "key-gen")["api_key"] == "***"
    _open_drawer(page)

    # edit, KEEP key (default mode on edit) → still set
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    page.get_by_test_id("f-model").fill("gpt-fake-2")
    _save_form(page)
    g = _find(_gens(api, "uitest-clement"), "key-gen")
    assert g["api_key"] == "***", "keep mode wrongly cleared the key"
    assert g["model"] == "gpt-fake-2"

    # edit, CLEAR key → null
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    page.get_by_role("radio", name="clear to inherit").check()
    _save_form(page)
    assert _find(_gens(api, "uitest-clement"), "key-gen")["api_key"] is None

    # edit, SET a new key → '***' again, never echoed
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    page.get_by_role("radio", name="set", exact=True).check()
    page.get_by_test_id("f-api-key").fill("sk-REPLACED-222")
    _save_form(page)
    assert _find(_gens(api, "uitest-clement"), "key-gen")["api_key"] == "***"
    assert "sk-REPLACED-222" not in api.get("/generators?profile=uitest-clement").text


def test_mutual_exclusion_api_key_and_env(page_as, api, weave):
    """Setting both api_key and api_key_env must be rejected (400 from the
    server, surfaced as an inline form error)."""
    page = page_as("uitest-clement", weave)
    gid = _create_scratch_generator(page, api, "uitest-clement", "mutex")
    _open_drawer(page)
    page.get_by_test_id(f"g-edit-{gid}").click()
    page.wait_for_timeout(150)
    page.get_by_role("radio", name="set", exact=True).check()
    page.get_by_test_id("f-api-key").fill("sk-BOTH-SET")
    page.get_by_test_id("f-api-key-env").fill("OPENAI_API_KEY")
    page.get_by_test_id("f-save").click()
    page.wait_for_timeout(400)
    assert page.get_by_test_id("f-error").is_visible(), "mutual-exclusion not surfaced"
    g = _find(_gens(api, "uitest-clement"), "mutex")
    assert g["api_key"] is None and g["api_key_env"] is None, (
        "key+env both set must not persist"
    )


def test_drawer_collapses_and_canvas_stays_interactive(page_as, api, weave):
    """The drawer is NON-MODAL: chips and canvas stay usable while it's open;
    collapse/expand works; open state roams with the profile."""
    page = page_as("uitest-clement", weave)
    _open_drawer(page)
    drawer = page.get_by_test_id("generators-drawer")

    # chips row above the drawer stays clickable (no backdrop intercepts)
    _activate(page, "default")
    expect(page.locator(".generators .chip.active")).to_have_count(1)
    _activate(page, "default")  # restore
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
    page.get_by_test_id("generators-collapse").click()
    expect(drawer).to_have_count(0)
    page.wait_for_timeout(1200)  # setSetting debounce (800ms) must flush its PUT
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    expect(page.get_by_test_id("generators-drawer")).to_have_count(0)

    # reopen via the toggle button; persisted open across reload
    page.get_by_test_id("open-generators").click()
    expect(page.get_by_test_id("generators-drawer")).to_be_visible()
    page.wait_for_timeout(1200)
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(600)
    expect(page.get_by_test_id("generators-drawer")).to_be_visible()


def test_menu_delete_with_in_app_confirm_and_cancel(page_as, api, weave):
    """The chips menu has a delete action behind the IN-APP confirm dialog
    (no native popup); cancel keeps the generator, confirm deletes it."""
    page = page_as("uitest-bob", weave)
    gid = _create_scratch_generator(page, api, "uitest-bob", "menu-del")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-delete-menu-del").click()
    dialog = page.get_by_test_id("confirm-dialog")
    expect(dialog).to_be_visible()
    # cancel → still there
    page.get_by_test_id("confirm-cancel").click()
    expect(dialog).to_have_count(0)
    page.wait_for_timeout(300)
    assert _find(_gens(api, "uitest-bob"), "menu-del") is not None, "cancel deleted it!"
    # confirm → gone (REST-verified), chip disappears. (Clicking the dialog
    # counted as an outside click and closed the menu — reopen it.)
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-delete-menu-del").click()
    page.get_by_test_id("confirm-ok").click()
    page.wait_for_timeout(400)
    assert _find(_gens(api, "uitest-bob"), "menu-del") is None
    expect(page.get_by_test_id("gc-gen-menu-del")).to_have_count(0)
    assert gid is not None  # silence lint


def test_shift_click_delete_skips_confirm(page_as, api, weave):
    """Shift+click on a delete button skips the confirmation entirely."""
    page = page_as("uitest-bob", weave)
    gid = _create_scratch_generator(page, api, "uitest-bob", "shift-del")
    _open_drawer(page)
    page.get_by_test_id(f"g-delete-{gid}").click(modifiers=["Shift"])
    page.wait_for_timeout(400)
    expect(page.get_by_test_id("confirm-dialog")).to_have_count(0)
    assert _find(_gens(api, "uitest-bob"), "shift-del") is None, (
        "shift+click delete did not delete immediately"
    )
    # same from the chips menu
    gid2 = _create_scratch_generator(page, api, "uitest-bob", "shift-del-2")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.get_by_test_id("open-gen-menu").click()
    page.get_by_test_id("menu-delete-shift-del-2").click(modifiers=["Shift"])
    page.wait_for_timeout(400)
    expect(page.get_by_test_id("confirm-dialog")).to_have_count(0)
    assert _find(_gens(api, "uitest-bob"), "shift-del-2") is None
    assert gid2 is not None


# --------------------------------------------------------------------------- #
# endpoint probe


def test_probe_indicator_and_model_autocomplete(page_as, api, weave):
    """Typing a reachable base_url in the edit form → debounced probe shows
    'endpoint reachable' and the listed models feed the model <datalist>; an
    unreachable port shows 'unreachable' + the error."""
    page = page_as("uitest-alice", weave)
    _open_drawer(page)
    page.get_by_test_id("create-source").select_option("")
    page.get_by_test_id("create-name").fill("probe-gen")
    page.get_by_test_id("create-generator").click()
    page.wait_for_timeout(400)

    page.get_by_test_id("f-base-url").fill(FAKE_URL)
    probe = page.get_by_test_id("f-probe")
    expect(probe).to_contain_text("reachable", timeout=4000)
    assert "unreachable" not in probe.inner_text()
    opts = page.evaluate(
        "() => [...document.querySelectorAll('#f-model-suggestions option')]"
        ".map(o => o.value)"
    )
    assert "gpt-fake" in opts, f"probe models not fed into the datalist: {opts}"
    assert page.get_by_test_id("f-model").get_attribute("list") == "f-model-suggestions"

    page.get_by_test_id("f-base-url").fill("http://127.0.0.1:59999/v1")
    expect(probe).to_contain_text("unreachable", timeout=6000)


def test_probe_runs_on_inherited_base_url(page_as, api, weave):
    """A generator that does NOT override base_url still probes the INHERITED
    endpoint when its form opens (the indicator reflects the effective URL)."""
    page = page_as("uitest-bob", weave)
    gens = _gens(api, "uitest-bob")
    gid = _find(gens, "default")["id"]
    _open_drawer(page)
    page.get_by_test_id(f"g-edit-{gid}").click()
    expect(page.get_by_test_id("f-base-url")).to_have_value("")
    expect(page.get_by_test_id("f-probe")).to_contain_text("reachable", timeout=4000)


# --------------------------------------------------------------------------- #
# events: stale-ancestor badges, activity feed, X-Coloom-Profile attribution


def test_remote_template_edit_badges_inheriting_chip(page_as, api, weave):
    """A template_updated event by SOMEONE ELSE tints every chip inheriting
    from that template until the chip is focused; the activity feed logs
    'mallory edited template …'."""
    page = page_as("uitest-alice", weave)
    tid = None
    try:
        tid = api.post("/templates", json={
            "name": "badge-tpl", "base_url": FAKE_URL, "model": "gpt-fake",
            "params": {"temperature": 0.5, "logprobs": 5},
        }).json()["id"]
        # alice creates a generator inheriting from it (via the drawer)
        _create_child_generator(page, api, "uitest-alice", "badge-gen",
                                f"template:{tid}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        chip = page.get_by_test_id("gc-gen-badge-gen")
        expect(chip).to_be_visible()
        assert "stale" not in (chip.get_attribute("class") or "")

        # mallory (another profile, another client) edits the template
        r = api.patch(
            f"/templates/{tid}",
            json={"params": {"temperature": 0.9, "logprobs": 5}},
            headers={"X-Coloom-Profile": "uitest-mallory",
                     "X-Coloom-Client": "mallory-tab"},
        )
        r.raise_for_status()
        expect(chip).to_have_class(re.compile(r"\bstale\b"), timeout=4000)
        # the inherited (placeholder) value moved with the remote edit
        g = _find(_gens(api, "uitest-alice"), "badge-gen")
        assert g["resolved"]["params"]["temperature"] == 0.9

        # the activity feed names the actor (.first: global events persist in
        # the server log, so entries from earlier runs may also match)
        page.locator(".tabs button", has_text="activity").click()
        expect(
            page.locator(".tab-body li", has_text="edited template badge-tpl").first
        ).to_be_visible(timeout=4000)
        expect(page.locator(".tab-body li", has_text="uitest-mallory").first).to_be_visible()

        # focusing the chip dismisses the badge
        page.get_by_test_id("gc-body-badge-gen").click()
        expect(chip).not_to_have_class(re.compile(r"\bstale\b"))
    finally:
        if tid:
            api.delete(f"/templates/{tid}")


def test_mutations_carry_profile_attribution(page_as, api, weave):
    """Every mutating request sends X-Coloom-Profile: generator events carry
    by == the logged-in profile."""
    page = page_as("uitest-alice", weave)
    # global events have weave_id "" — filter server-side and only look past
    # the current cursor (the unfiltered log paginates at 1000)
    since = api.get("/events?since=0&weave_id=").json()["cursor"]
    page.get_by_test_id("gc-body-default").click()
    temp = page.get_by_test_id("param-temp")
    temp.fill("0.77")
    temp.dispatch_event("change")
    page.wait_for_timeout(900)
    events = api.get(f"/events?since={since}&weave_id=").json()["events"]
    upd = [e for e in events if e["type"] == "generator_updated"
           and e["payload"].get("name") == "default"
           and e["payload"].get("by") == "uitest-alice"]
    assert upd, "generator_updated with by=uitest-alice not found in the event log"


def test_accented_profile_name_attribution(page_as, api, weave):
    """Non-ASCII profile names ('clément'!) survive the X-Coloom-Profile header
    round trip (the client percent-encodes; the server decodes)."""
    page = page_as("uitest-clémént", weave)
    page.wait_for_timeout(400)
    gens = _gens(api, "uitest-clémént")
    assert gens, "accented profile did not get seeded generators"
    since = api.get("/events?since=0&weave_id=").json()["cursor"]
    page.get_by_test_id("gc-body-default").click()
    temp = page.get_by_test_id("param-temp")
    temp.fill("0.66")
    temp.dispatch_event("change")
    page.wait_for_timeout(900)
    g = _find(_gens(api, "uitest-clémént"), "default")
    assert g["params"].get("temperature") == 0.66
    events = api.get(f"/events?since={since}&weave_id=").json()["events"]
    upd = [e for e in events if e["type"] == "generator_updated"
           and e["payload"].get("by") == "uitest-clémént"]
    assert upd, "accented profile name mangled in event attribution"


# --------------------------------------------------------------------------- #
# login settings hygiene (dev mode: no legacy migration — invalid refs discard)


def test_login_discards_invalid_generator_refs_keeps_other_settings(browser, api, weave):
    """Pre-redesign refs / ids of deleted generators in activeGenerators /
    hiddenGenerators are DISCARDED on login (no legacy migration — dev mode),
    valid ids survive, and unrelated settings keys are merged, never wiped
    (Clément's real profile must survive intact)."""
    name = "uitest-staleref"
    # the profile must exist (and be seeded) BEFORE we plant the settings:
    # PUT triggers seeding, so fetch the seeded ids first
    api.put(f"/profiles/{name}", json={"settings": {}})
    gens = _gens(api, name)
    default_id = _find(gens, "default")["id"]
    api.put(f"/profiles/{name}", json={"settings": {
        "activeGenerators": [
            {"kind": "preset", "id": "default"},  # pre-redesign shape → discard
            "no-such-generator-id",               # deleted/unknown id → discard
            default_id,                           # valid → keep
        ],
        "hiddenGenerators": ["preset:fake-slow"],  # pre-redesign key → discard
        "unrelatedKeepMe": {"nested": 42},
    }})
    ctx = browser.new_context(viewport={"width": 1500, "height": 900})
    try:
        page = ctx.new_page()
        page.add_init_script(
            f"localStorage.setItem('coloom.identity', {name!r});"
            f"localStorage.setItem('coloom.profile', {name!r})"
        )
        page.goto(f"{BASE}/#/w/{weave}", wait_until="networkidle")
        page.wait_for_timeout(1800)  # login + generators fetch + cleanup PUT

        # the valid id stayed active; fake-slow is NOT hidden (ref discarded)
        expect(page.get_by_test_id("gc-gen-default")).to_have_class(
            re.compile(r"\bactive\b"))
        expect(page.get_by_test_id("gc-gen-fake-slow")).to_be_visible()

        settings = api.get(f"/profiles/{name}").json()["settings"]
        assert settings["activeGenerators"] == [default_id], (
            f"cleaned active set wrong: {settings['activeGenerators']}"
        )
        assert settings["hiddenGenerators"] == []
        assert settings["unrelatedKeepMe"] == {"nested": 42}, (
            "cleanup must merge settings, never overwrite unrelated keys"
        )
    finally:
        ctx.close()
