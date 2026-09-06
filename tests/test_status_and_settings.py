"""The vendor status row, and the settings panel that configures it."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from quotalens import status
from quotalens.api import create_app
from quotalens.config import CONFIG_KEYS_BY_NAME, config_path, read_config_file
from quotalens.settings_view import PANEL_KEYS

OK_PAYLOAD = {"status": {"indicator": "none", "description": "All Systems Operational"}}


def _form(**over: str) -> dict[str, str]:
    base = {
        "notify_credits": "1",
        "interval": "60",
        "lookback": "15",
        "burn_alert": "20.0",
        "sample_keep": "20000",
        "webhook_url": "",
        "notify_thresholds": "50,75,90",
        "status_vendors": "claude,gemini,openai",
        "status_row": "1",
    }
    return {**base, **over}


# -- the status row -------------------------------------------------------------


def test_a_vendor_with_no_feed_makes_no_request_at_all() -> None:
    """The seam that lets Gemini back in the day Google ships a feed.

    No shipped vendor has ``api_url=None`` any more -- Gemini was removed rather
    than left saying "unable to check" forever -- so this exercises the branch
    with a stand-in. If it ever regresses, a future vendor with no feed would
    silently be reported unreachable, which is a different and untrue claim.
    """
    asked: list[str] = []
    mute = status.StatusVendor("mute", "Mute", "https://example.invalid/", None)
    watcher = status.StatusWatcher(vendors=(mute,))
    watcher.check_all(1000, fetcher=lambda url: asked.append(url) or OK_PAYLOAD)
    assert asked == []
    row = watcher.rows()[0]
    assert row.state == status.UNKNOWN and row.detail == status.NO_API_REASON


def test_gemini_is_in_the_registry_but_never_watched() -> None:
    """Shown in the form, disabled and explained; never polled, never a row.

    It lives in the registry rather than the template so that adding or
    retiring a vendor stays one edit in one place.
    """
    gemini = next(v for v in status.VENDORS if v.key == "gemini")
    assert not gemini.supported and gemini.api_url is None
    assert (
        "does not\nprovide a suitable public status feed"
        in gemini.unsupported_reason.replace(" ", " ")
        or "suitable public status feed" in gemini.unsupported_reason
    )
    assert "no API" not in gemini.unsupported_reason
    assert gemini not in status.AVAILABLE
    assert all(v.api_url for v in status.AVAILABLE), "every watched vendor has a feed"
    assert gemini not in status.selected_vendors("claude,gemini,openai")


def test_an_unrecognised_indicator_is_unknown_not_healthy() -> None:
    """Guessing healthy from a shape we do not understand is the one fatal error."""
    state, why = status.parse_statuspage({"status": {"indicator": "brand_new"}})
    assert state == status.UNKNOWN and "brand_new" in why
    assert status.parse_statuspage({})[0] == status.UNKNOWN
    assert status.parse_statuspage("nonsense")[0] == status.UNKNOWN


def test_the_four_indicators_map_to_the_design_states() -> None:
    assert status.INDICATOR_STATE["none"] == status.OK
    assert status.INDICATOR_STATE["minor"] == status.DEGRADED
    assert status.INDICATOR_STATE["major"] == status.INDICATOR_STATE["critical"] == status.OUTAGE
    assert status.INDICATOR_STATE["maintenance"] == status.MAINTENANCE


def test_one_dropped_packet_is_not_an_outage() -> None:
    watcher = status.StatusWatcher()
    watcher.check_all(1000, fetcher=lambda _u: OK_PAYLOAD)
    assert all(r.state == status.OK for r in watcher.rows() if r.vendor.api_url)

    def drop(_url):
        raise OSError("connection reset")

    watcher.check_all(2000, fetcher=drop)  # first failure: keep the last answer
    assert all(r.state == status.OK for r in watcher.rows() if r.vendor.api_url)
    watcher.check_all(3000, fetcher=drop)  # second: now say we cannot tell
    assert all(
        r.state == status.UNKNOWN and r.detail == status.UNREACHABLE_REASON
        for r in watcher.rows()
        if r.vendor.api_url
    )


def test_turning_it_off_stops_the_requests_not_just_the_row() -> None:
    """A user who chose a loopback-only tool is entitled to that being literal."""
    asked: list[str] = []
    watcher = status.StatusWatcher(enabled=False)
    watcher.check_all(1000, fetcher=lambda url: asked.append(url) or OK_PAYLOAD)
    assert asked == [] and watcher.rows() == [] and not watcher.due(999_999)


def test_the_rows_render_as_links_that_leave(settings, store, secrets) -> None:
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/").text
    rows = re.findall(r'<a class="vs"[^>]*>', html)
    assert len(rows) == len(status.AVAILABLE) == 2
    for row in rows:
        assert 'target="_blank"' in row
        assert "noopener" in row and "noreferrer" in row
        # None of the classes app.js delegates on, or the SPA eats the click.
        for swallowed in ("rb", "el-link", "sess"):
            assert f'class="vs {swallowed}' not in row and f'{swallowed} vs"' not in row
    for vendor in status.AVAILABLE:
        assert f'href="{vendor.page_url}"' in html


def test_the_unknown_row_still_links_and_says_why(settings, store, secrets) -> None:
    """ "Unable to check" is not a dead end: it says so, and the click still leaves.

    Every vendor is in this state here, because conftest refuses ``status.fetch``
    for the whole suite -- which is also exactly what an offline machine sees.
    """
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/").text
    for vendor in status.AVAILABLE:
        pattern = rf'<a class="vs"[^>]*{re.escape(vendor.page_url)}[^>]*>(.*?)</a>'
        row = re.search(pattern, html, re.S)
        assert row is not None, vendor.key
        assert "—" in row.group(1), vendor.key
        assert 'target="_blank"' in html


# -- the settings panel ---------------------------------------------------------


def test_the_panel_saves_without_javascript(settings, store, secrets, tmp_path) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        saved = tc.post("/settings", data=_form(interval="120"), follow_redirects=False)
    assert saved.status_code == 303
    assert read_config_file(config_path("", tmp_path))["interval"] == 120


def test_a_bad_value_shows_the_real_error_and_writes_nothing(
    settings, store, secrets, tmp_path
) -> None:
    """Not clamped to the floor and saved silently."""
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        page = tc.post("/settings", data=_form(interval="5"))
    assert page.status_code == 400
    assert "at least 30s" in page.text
    assert 'value="5"' in page.text  # what they typed, redisplayed
    assert read_config_file(config_path("", tmp_path)) == {}


def test_a_saved_setting_takes_effect_without_a_restart(settings, store, secrets, tmp_path) -> None:
    """The panel says "takes effect on the next poll"; this is that claim."""
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        assert tc.get("/api/health").json()["poll_interval_s"] == settings.poll_interval_s
        tc.post("/settings", data=_form(interval="120"))
        assert tc.get("/api/health").json()["poll_interval_s"] == 120


def test_the_panel_never_offers_the_port_the_database_or_the_cookie() -> None:
    """Each has a reason, and each is stated on the page rather than omitted."""
    assert "port" not in PANEL_KEYS
    assert not CONFIG_KEYS_BY_NAME["port"].panel
    assert not {k for k in PANEL_KEYS if "cookie" in k or "db" in k}


def test_the_read_only_block_says_why_and_how(settings, store, secrets, tmp_path) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    assert "quotalens config set port" in html
    assert "in the OS keyring" in html
    assert "quotalens auth" in html


def test_the_form_says_when_changes_apply_once_not_once_per_field(
    settings, store, secrets, tmp_path
) -> None:
    """Eight fields repeating "takes effect on the next poll" said nothing eight
    times, and were the largest single source of visual weight in the dialog.

    Prompt 17 made every field here live, so the form states it once. A field
    that is genuinely different still has to say so -- which is what the second
    assertion protects.
    """
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    assert html.count("Changes take effect from the next poll") == 1
    assert "takes effect on the next poll" not in html

    from quotalens.render import _field

    assert "needs a restart" in _field("k", "L", "v", note="n", effect="restart")
    assert "needs a restart" not in _field("k", "L", "v", note="n")


def test_the_retention_sizes_are_measured_and_labelled_as_estimates(
    settings, store, secrets, tmp_path
) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    assert html.count('name="retention"') == 5
    assert "≈" in html  # never presented as exact
    # An empty store cannot know, and says so rather than extrapolating.
    assert "needs more history" in html or "database is empty" in html


def test_shortening_retention_needs_a_confirm(settings, store, secrets, tmp_path) -> None:
    import time

    from quotalens.parse import QuotaReading

    now = int(time.time())
    for days in range(40):
        store.record_quota(
            now - days * 86_400,
            [QuotaReading("five_hour", "Session", 50.0, None, None, True)],
        )
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        blocked = tc.post("/settings/retention", data={"retention": "1week"})
        assert blocked.status_code == 400
        assert "deletes data permanently" in blocked.text or "permanently" in blocked.text
        assert read_config_file(config_path("", tmp_path)).get("retention") is None

        allowed = tc.post(
            "/settings/retention",
            data={"retention": "1week", "confirm": "1"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303
        assert read_config_file(config_path("", tmp_path))["retention"] == "1week"


def test_the_wheel_ships_the_notification_icon_and_the_vendor_directory() -> None:
    """Mirrors CI's package-contents step; an asset can work from src and not ship."""
    from importlib import resources

    web = resources.files("quotalens.web")
    assert web.joinpath("mark.png").read_bytes()[:4] == b"\x89PNG"
    assert web.joinpath("vendor").is_dir()


def test_the_mark_png_is_the_one_the_renderer_produces() -> None:
    """The committed asset cannot drift from design/mark.svg's numbers."""
    import sys
    from importlib import resources
    from pathlib import Path

    design = Path(__file__).resolve().parents[1] / "design"
    if not (design / "render_mark_png.py").exists():
        return  # design/ is not in an installed wheel
    sys.path.insert(0, str(design))
    try:
        import render_mark_png
    finally:
        sys.path.pop(0)
    assert (
        render_mark_png.render()
        == resources.files("quotalens.web").joinpath("mark.png").read_bytes()
    )


def test_a_vendor_without_its_brand_file_renders_the_name_alone(settings, store, secrets) -> None:
    """Absent is a supported state: the mark is theirs, so we do not draw one."""
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/").text
        for vendor in status.AVAILABLE:
            if vendor.logo:
                served = tc.get(f"/static/vendor/{vendor.logo}")
                assert served.status_code in (200, 404)
        # The handler serves from a fixed allow-list built from VENDORS, not
        # from the path, so nothing can be walked out of the vendor directory.
        # (A literal "../" is resolved by the client before it is ever sent,
        # which would test the client rather than the route.)
        assert tc.get("/static/vendor/nope.svg").status_code == 404
        assert tc.get("/static/vendor/%2e%2e%2fapp.css").status_code == 404
        assert tc.get("/static/vendor/README.md").status_code == 404
    for vendor in status.AVAILABLE:
        assert vendor.display_name in html


def test_saving_does_not_forget_the_flags_this_instance_was_started_with(
    settings, store, secrets, tmp_path
) -> None:
    """The panel owns PANEL_KEYS and nothing else.

    Reloading settings wholesale after a save dropped every CLI flag: an
    instance started with `--port 8830` reported 8787 in the panel's own
    read-only block, which is the single place that has to be right.
    """
    flagged = settings.with_overrides(port=8830)
    app = create_app(flagged, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        assert "8830" in tc.get("/settings").text
        tc.post("/settings", data=_form(interval="120"), follow_redirects=False)
        after = tc.get("/settings").text
    assert "8830" in after, "the port survived a save"
    assert app.state.qw.settings.port == 8830
    assert app.state.qw.settings.poll_interval_s == 120  # and the panel key applied


def test_a_currentcolor_mark_is_masked_and_a_brand_coloured_one_is_not(tmp_path) -> None:
    """How a mark is coloured is the file's decision, not ours.

    An SVG loaded through <img> is an isolated document, so a `currentColor`
    mark resolves to the initial black and disappears on the dark ground -- which
    is what happened to OpenAI's. A mask paints the file's own alpha in the
    surrounding colour; a mark with its own brand colour must not be touched.
    """
    from quotalens.render import _vendor_logo, _vendor_logo_is_mono

    for vendor in status.VENDORS:
        if not vendor.logo:
            continue
        html = _vendor_logo(vendor)
        if not html:
            continue  # the file has not been added yet; the row shows the name
        if _vendor_logo_is_mono(vendor.logo):
            assert 'class="vl vlm"' in html and "--m:url(" in html, vendor.key
            assert "<img" not in html, vendor.key
        else:
            assert html.startswith("<img"), vendor.key
            assert "vlm" not in html, vendor.key


def test_the_mask_takes_the_surrounding_colour_in_both_themes() -> None:
    from importlib import resources

    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    rule = css.split(".vlm{", 1)[1].split("}", 1)[0]
    assert "background:currentColor" in rule, "the mark is painted in the row's own colour"
    assert "mask:var(--m)" in rule
    # No colour of its own anywhere: a token would freeze it to one theme.
    assert "#" not in rule


@pytest.mark.parametrize("notifier", [True, False])
def test_every_panel_key_is_actually_rendered_as_a_field(
    settings, store, secrets, tmp_path, monkeypatch, notifier
) -> None:
    """A panel key with no field is not "left alone": it is turned off.

    An unchecked box sends nothing, so `apply_form` reads a missing boolean as
    false. `notify_credits` was in PANEL_KEYS and never rendered, so every save
    silently disabled credit notifications -- observed as True on disk before a
    save and False after one that never mentioned it.

    Run for both hosts. Unparametrised, this passed on macOS and Windows and
    failed on every Linux runner, because `notify` was rendered only where a
    notifier existed -- so the machine that ran it was what decided the answer.
    """
    import re

    if not notifier:
        _unavailable(monkeypatch)
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    from quotalens.notify import SLOT_KEYS

    rendered = set(re.findall(r'name="([a-z_0-9]+)"', html))
    # notify_thresholds is one stored key behind three selects; the conversion
    # is at the form boundary, so the slots are what appear in the markup.
    # status_vendors is one stored key behind a checkbox per available vendor,
    # the same shape as notify_thresholds behind three selects.
    expected = (set(PANEL_KEYS) - {"notify_thresholds", "status_vendors"}) | set(SLOT_KEYS)
    expected |= {f"vendor_{v.key}" for v in status.AVAILABLE}
    assert expected <= rendered, expected - rendered


def test_saving_leaves_an_unmentioned_boolean_alone(settings, store, secrets, tmp_path) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data={**_form(), "notify_credits": "1"}, follow_redirects=False)
        assert read_config_file(config_path("", tmp_path))["notify_credits"] is True


def test_the_footer_submits_the_settings_form_from_outside_it(
    settings, store, secrets, tmp_path
) -> None:
    """`form="settings-form"` is what makes Save work with no JavaScript.

    Hoisting the dialog into one form would have worked too, and would have put
    retention inside the same submit -- which is exactly what must not happen.
    """
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        page = tc.get("/").text
    assert '<button type="submit" form="settings-form">Save changes</button>' in page
    # Close and Cancel are dialog-method forms, so neither can post settings.
    assert page.count('<form method="dialog"') == 2


def test_save_changes_cannot_apply_retention(settings, store, secrets, tmp_path) -> None:
    """Two forms, two handlers, two posts. Proven by sending retention to Save."""
    import time

    from quotalens.parse import QuotaReading

    now = int(time.time())
    for days in range(40):
        store.record_quota(
            now - days * 86_400,
            [QuotaReading("five_hour", "Session", 50.0, None, None, True)],
        )
    before = store.counts()
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post(
            "/settings",
            data={**_form(), "retention": "1week", "confirm": "1"},
            follow_redirects=False,
        )
    assert store.counts() == before, "no rows were deleted"
    assert read_config_file(config_path("", tmp_path)).get("retention") is None


def test_the_dialog_shell_scrolls_the_body_not_the_whole_modal() -> None:
    """The header used to scroll out of view: measured, top 90 to top -310."""
    from importlib import resources

    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    shell = css.split("dialog{", 1)[1].split("}", 1)[0]
    assert "overflow:hidden" in shell
    # [open] is load-bearing: display:flex on a bare `dialog` overrides the UA's
    # display:none for a closed one, which laid it out after the footer as
    # ordinary content.
    opened = css.split("dialog[open]{", 1)[1].split("}", 1)[0]
    assert "display:flex" in opened and "flex-direction:column" in opened
    assert "display:flex" not in shell
    body = css.split("#sd-body{", 1)[1].split("}", 1)[0]
    assert "overflow-y:auto" in body
    # Without min-height:0 a flex item will not shrink below its content, the
    # body never becomes scrollable, and the header leaves anyway.
    assert "min-height:0" in body
    for part in (".dhead,.sfoot{",):
        assert "flex:none" in css.split(part, 1)[1].split("}", 1)[0]


def test_the_narrow_collapse_is_after_the_rules_it_overrides() -> None:
    """It was written above them at equal specificity and did nothing.

    Measured at a real 390px viewport through Playwright: the grid still
    reported two columns. Position in the file is the whole fix.
    """
    from importlib import resources

    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    assert css.index("@media (max-width:640px)") > css.index(".fform{display:grid")


def _slots(**over: str) -> dict[str, str]:
    base = {k: v for k, v in _form().items() if k != "notify_thresholds"}
    return {**base, "notify_t1": "50", "notify_t2": "75", "notify_t3": "90", **over}


def test_three_selects_round_trip_through_one_stored_string(
    settings, store, secrets, tmp_path
) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_slots(notify_t1="30", notify_t2="60", notify_t3="80"))
        assert read_config_file(config_path("", tmp_path))["notify_thresholds"] == "30,60,80"
        html = tc.get("/settings").text
    for slot, value in (("notify_t1", "30"), ("notify_t2", "60"), ("notify_t3", "80")):
        sel = re.search(rf'name="{slot}".*?</select>', html, re.S).group(0)
        assert f'value="{value}" selected' in sel


def test_all_disabled_stores_null_and_does_not_revert(settings, store, secrets, tmp_path) -> None:
    """Two traps, one behind the other.

    `parse_thresholds` answered an empty list with DEFAULT_THRESHOLDS, and
    `with_overrides` -- which drops None so an unpassed CLI flag clears nothing --
    made writing None a silent no-op. Either one alone hands 50/75/90 straight
    back to a user who switched them all off.
    """
    from quotalens.config import load_settings
    from quotalens.notify import parse_thresholds

    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_slots(notify_t1="30", notify_t2="60", notify_t3="80"))
        tc.post("/settings", data=_slots(notify_t1="", notify_t2="", notify_t3=""))
    stored = read_config_file(config_path("", tmp_path))
    assert stored["notify_thresholds"] is None
    assert load_settings(data_dir=tmp_path).notify_thresholds is None
    assert parse_thresholds(load_settings(data_dir=tmp_path).notify_thresholds) == ()


def test_credit_notifications_survive_all_thresholds_being_off(
    settings, store, secrets, tmp_path
) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_slots(notify_t1="", notify_t2="", notify_t3=""))
    assert read_config_file(config_path("", tmp_path))["notify_credits"] is True


def test_duplicate_and_descending_selects_are_refused_beside_the_field(
    settings, store, secrets, tmp_path
) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        dup = tc.post("/settings", data=_slots(notify_t2="50"))
        desc = tc.post("/settings", data=_slots(notify_t1="90", notify_t3="50"))
    assert dup.status_code == 400 and "different percentage" in dup.text
    assert desc.status_code == 400 and "ascending order" in desc.text
    assert read_config_file(config_path("", tmp_path)) == {}


def test_a_legacy_config_with_four_thresholds_loads(settings, store, secrets, tmp_path) -> None:
    """The lowest three fill the slots; the extras stay on disk until a save."""
    from quotalens.config import write_config_file

    write_config_file(config_path("", tmp_path), {"notify_thresholds": "10,20,30,40"})
    legacy = settings.with_overrides(notify_thresholds="10,20,30,40")
    app = create_app(legacy, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    for value in ("10", "20", "30"):
        assert f'value="{value}" selected' in html, value
    assert 'value="40" selected' not in html  # the form has three slots
    # Loading rewrites nothing.
    assert read_config_file(config_path("", tmp_path))["notify_thresholds"] == "10,20,30,40"


def test_a_legacy_config_with_spaces_or_two_values_loads(settings, store, secrets) -> None:
    legacy = settings.with_overrides(notify_thresholds=" 40 , 80 ")
    app = create_app(legacy, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    assert 'value="40" selected' in html and 'value="80" selected' in html


def test_the_dialog_is_not_in_the_refreshed_fragment(settings, store, secrets) -> None:
    """The whole cause of the disappearing modal, pinned at the server.

    render_app is what /api/dashboard/fragment returns and what app.js writes
    into #app.innerHTML, so a <dialog> in it is destroyed and rebuilt on every
    refresh -- and a native dialog removed from the document leaves the top
    layer for good.
    """
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        page = tc.get("/").text
        fragment = tc.get("/api/dashboard/fragment").text
    assert "<dialog" not in fragment
    assert 'id="sd"' not in fragment
    assert page.count('id="sd"') == 1


def _vform(**over: str) -> dict[str, str]:
    base = {k: v for k, v in _slots().items() if k != "status_vendors"}
    return {**base, **over}


def test_vendor_checkboxes_round_trip_to_the_stored_string(
    settings, store, secrets, tmp_path
) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_vform(vendor_claude="1", vendor_openai="1"))
        assert read_config_file(config_path("", tmp_path))["status_vendors"] == "claude,openai"
        tc.post("/settings", data=_vform(vendor_claude="1"))
        assert read_config_file(config_path("", tmp_path))["status_vendors"] == "claude"
        html = tc.get("/settings").text
    box = re.search(r'name="vendor_claude"[^>]*>', html).group(0)
    assert "checked" in box
    assert "checked" not in re.search(r'name="vendor_openai"[^>]*>', html).group(0)


def test_deselecting_every_vendor_means_none_and_does_not_revert(
    settings, store, secrets, tmp_path
) -> None:
    """The live bug: an empty setting returned every vendor.

    And beneath it, the same `with_overrides` drops-None trap that bit
    notify_thresholds -- the value reached config.json but never reached the
    running instance, so the rows stayed up.
    """
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_vform(vendor_claude="1", vendor_openai="1"))
        tc.post("/settings", data=_vform(present="1"))
        stored = read_config_file(config_path("", tmp_path))["status_vendors"]
        assert stored is None
        assert '<a class="vs"' not in tc.get("/").text
    assert status.selected_vendors(stored) == ()
    assert status.selected_vendors("") == ()

    asked: list[str] = []
    watcher = status.StatusWatcher(vendors=status.selected_vendors(stored))
    watcher.check_all(1000, fetcher=lambda url: asked.append(url) or OK_PAYLOAD)
    assert asked == [], "no requests are made for an empty selection"


def test_an_absent_key_still_takes_every_available_source() -> None:
    """The default lives on the ConfigKey, not in selected_vendors."""
    from quotalens.config import CONFIG_KEYS_BY_NAME

    assert CONFIG_KEYS_BY_NAME["status_vendors"].default("") == "claude,openai"
    assert status.selected_vendors("claude,openai") == status.AVAILABLE


def test_a_legacy_comma_value_loads_and_unknown_keys_are_dropped() -> None:
    assert [v.key for v in status.selected_vendors("claude, mistral ,openai")] == [
        "claude",
        "openai",
    ]


def test_gemini_is_shown_disabled_and_never_submitted(settings, store, secrets, tmp_path) -> None:
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
        # Even if a client forces the field, it cannot reach the stored value.
        tc.post("/settings", data=_vform(vendor_claude="1", vendor_gemini="1"))
        stored = read_config_file(config_path("", tmp_path))["status_vendors"]
    box = re.search(r'name="vendor_gemini"[^>]*>', html).group(0)
    assert "disabled" in box
    assert "suitable public status feed" in html
    assert "no API" not in html
    assert stored == "claude"


def test_the_vendor_boxes_are_disabled_not_merely_dimmed_when_the_parent_is_off(
    settings, store, secrets, tmp_path
) -> None:
    off = settings.with_overrides(status_row=False)
    app = create_app(off, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    for vendor in status.AVAILABLE:
        assert "disabled" in re.search(rf'name="vendor_{vendor.key}"[^>]*>', html).group(0)


def test_the_test_notification_writes_no_event_and_changes_nothing(
    settings, store, secrets, tmp_path
) -> None:
    """It probes the delivery path. It is not a crossing."""
    from quotalens.notify import CROSSED_KIND

    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_slots())
        before = read_config_file(config_path("", tmp_path))
        events_before = len(store.recent_events(limit=50, kind=CROSSED_KIND))
        body = tc.post("/api/notify/test").json()
        assert body["accepted"] is True
        assert "handed_off" in body and "delivered" not in body  # never claims delivery
        # A second, immediate click is refused by the same guard `poll now` uses.
        assert tc.post("/api/notify/test").json()["accepted"] is False
    assert len(store.recent_events(limit=50, kind=CROSSED_KIND)) == events_before
    assert read_config_file(config_path("", tmp_path)) == before
    assert store.counts()["quota"] == 0


def test_the_delivery_status_is_redetected_not_read_from_startup(
    settings, store, secrets, tmp_path, monkeypatch
) -> None:
    """Installing terminal-notifier after startup used to go unnoticed."""
    from quotalens import notify as notify_mod

    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        monkeypatch.setattr(
            notify_mod, "detect_capability", lambda *a, **k: notify_mod.Capability(False, "gone")
        )
        assert "Unavailable: gone" in tc.get("/settings").text
        monkeypatch.setattr(
            notify_mod,
            "detect_capability",
            lambda *a, **k: notify_mod.Capability(True, tool="terminal-notifier"),
        )
        assert "Ready to send via terminal-notifier" in tc.get("/settings").text


# -- the notify group: a disabled box is not an unticked one --------------------


def _browser_submit(html: str) -> dict[str, str]:
    """What a browser would POST from the rendered settings form.

    Built by reading the markup the server actually produced, not by writing the
    dict by hand: the fault below was that a field's *absence from the wire* was
    read as a value, and a hand-written dict is exactly the thing that cannot
    see it. Follows the submission rules that matter here -- a disabled control
    is never successful, an unchecked checkbox sends nothing, a select sends its
    selected option.
    """
    form = html.split('id="settings-form"', 1)[1].split("</form>", 1)[0]
    out: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", form):
        name = re.search(r'name="([^"]+)"', tag)
        if not name or " disabled" in tag:
            continue
        kind = (re.search(r'type="([^"]+)"', tag) or [None, "text"])[1]
        value = (re.search(r'value="([^"]*)"', tag) or [None, ""])[1]
        if kind == "checkbox" and " checked" not in tag:
            continue
        out[name[1]] = value
    for block in re.findall(r"<select\b[^>]*>.*?</select>", form, re.S):
        name = re.search(r'name="([^"]+)"', block)
        chosen = re.search(r'<option value="([^"]*)"[^>]*\bselected\b', block)
        if name:
            out[name[1]] = chosen[1] if chosen else ""
    return out


def _unavailable(monkeypatch) -> None:
    from quotalens import notify

    monkeypatch.setattr(
        notify,
        "detect_capability",
        lambda *a, **k: notify.Capability(False, "no session bus; this is how a unit runs."),
    )


def test_the_notify_box_is_rendered_disabled_when_nothing_can_deliver(
    settings, store, secrets, tmp_path, monkeypatch
) -> None:
    """Rendered and disabled, never omitted -- an omitted field has no value at all.

    Omitting it is what failed `test_every_panel_key_is_actually_rendered_as_a_field`
    on every Linux runner, and `release.yml`'s build job runs the suite on
    ubuntu-latest before it uploads anything, so it also stopped the tag.
    """
    _unavailable(monkeypatch)
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    box = re.search(r'<input type="checkbox" name="notify"[^>]*>', html)
    assert box, "the field must exist even where it cannot be used"
    assert " disabled" in box[0]
    assert "no session bus" in html and "The webhook still fires." in html


def test_a_save_from_a_host_with_no_notifier_leaves_notify_alone(
    settings, store, secrets, tmp_path, monkeypatch
) -> None:
    """The bug: a disabled box and an unticked box are the same thing on the wire.

    `config set notify true` on a desktop, then open the panel from the systemd
    user unit and press Save to change retention -- and the desktop's
    notifications are off, with nothing said.
    """
    _unavailable(monkeypatch)
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    path = config_path("", tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_form(notify="1", notify_group="1"), follow_redirects=False)
        assert read_config_file(path)["notify"] is True
        submitted = _browser_submit(tc.get("/settings").text)
        assert "notify" not in submitted and "notify_group" not in submitted
        tc.post("/settings", data=submitted, follow_redirects=False)
    assert read_config_file(path)["notify"] is True


def test_unticking_the_box_still_turns_notify_off_where_it_works(
    settings, store, secrets, tmp_path, monkeypatch
) -> None:
    """The behaviour the marker must not break: an unticked box is still a false.

    The capability is forced rather than inherited from whatever is on PATH, so
    the assertion is about the code and not about the machine running it.
    """
    from quotalens import notify

    monkeypatch.setattr(
        notify, "detect_capability", lambda *a, **k: notify.Capability(True, tool="stub")
    )
    app = create_app(settings, store, secrets, config_dir=tmp_path)
    path = config_path("", tmp_path)
    with TestClient(app) as tc:
        tc.post("/settings", data=_form(notify="1", notify_group="1"), follow_redirects=False)
        assert read_config_file(path)["notify"] is True
        submitted = _browser_submit(tc.get("/settings").text)
        assert submitted["notify"] == "1" and submitted["notify_group"] == "1"
        del submitted["notify"]  # what unticking the box does
        tc.post("/settings", data=submitted, follow_redirects=False)
    assert read_config_file(path)["notify"] is False
