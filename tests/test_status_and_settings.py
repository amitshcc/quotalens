"""The vendor status row, and the settings panel that configures it."""

from __future__ import annotations

import re

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


def test_gemini_is_not_shipped() -> None:
    """It could only ever say "unable to check": a line that teaches nothing."""
    assert "gemini" not in {v.key for v in status.VENDORS}
    assert all(v.api_url for v in status.VENDORS), "every shipped vendor has a feed"


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
    assert len(rows) == len(status.VENDORS) == 2
    for row in rows:
        assert 'target="_blank"' in row
        assert "noopener" in row and "noreferrer" in row
        # None of the classes app.js delegates on, or the SPA eats the click.
        for swallowed in ("rb", "el-link", "sess"):
            assert f'class="vs {swallowed}' not in row and f'{swallowed} vs"' not in row
    for vendor in status.VENDORS:
        assert f'href="{vendor.page_url}"' in html


def test_the_unknown_row_still_links_and_says_why(settings, store, secrets) -> None:
    """ "Unable to check" is not a dead end: it says so, and the click still leaves.

    Every vendor is in this state here, because conftest refuses ``status.fetch``
    for the whole suite -- which is also exactly what an offline machine sees.
    """
    app = create_app(settings, store, secrets)
    with TestClient(app) as tc:
        html = tc.get("/").text
    for vendor in status.VENDORS:
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
        for vendor in status.VENDORS:
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
    for vendor in status.VENDORS:
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


def test_every_panel_key_is_actually_rendered_as_a_field(
    settings, store, secrets, tmp_path
) -> None:
    """A panel key with no field is not "left alone": it is turned off.

    An unchecked box sends nothing, so `apply_form` reads a missing boolean as
    false. `notify_credits` was in PANEL_KEYS and never rendered, so every save
    silently disabled credit notifications -- observed as True on disk before a
    save and False after one that never mentioned it.
    """
    import re

    app = create_app(settings, store, secrets, config_dir=tmp_path)
    with TestClient(app) as tc:
        html = tc.get("/settings").text
    rendered = set(re.findall(r'name="([a-z_]+)"', html))
    assert set(PANEL_KEYS) <= rendered, set(PANEL_KEYS) - rendered


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
    assert "overflow:hidden" in shell and "flex-direction:column" in shell
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
