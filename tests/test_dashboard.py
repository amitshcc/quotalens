"""Dashboard: state rules, view model, rendering, and the stale watchdog."""

from __future__ import annotations

import asyncio
import re
import time
from importlib import resources

from fastapi.testclient import TestClient

from conftest import USAGE_LIVE_2026_09, make_client, make_handler
from quotalens.api import create_app
from quotalens.dashboard import assign_slots, build_dashboard, display_label
from quotalens.parse import QuotaReading, SpendReading
from quotalens.poller import Poller, PollerStatus
from quotalens.secrets import Redactor
from quotalens.state import collector_state, magnitude_state
from quotalens.views import ViewOptions, parse_view, resolve_range

EM_DASH = "—"


def _status(**kwargs) -> PollerStatus:
    status = PollerStatus()
    for key, value in kwargs.items():
        setattr(status, key, value)
    return status


def _seed(
    store, now: int, minutes: int = 16, base: float = 20.0, reset_in: int | None = 3600
) -> None:
    from datetime import UTC, datetime

    # a window that resets in an hour, or no window at all when reset_in is None
    reset = datetime.fromtimestamp(now + reset_in, UTC).isoformat() if reset_in else None
    for i in range(minutes):
        ts = now - (minutes - 1 - i) * 60
        store.record_quota(
            ts,
            [
                QuotaReading("five_hour", "5-hour", base + i, reset, "normal", True),
                QuotaReading("seven_day", "7-day", 38, "r2", "normal", False),
                QuotaReading("limit:fable", "Fable", 69, "r3", "normal", False),
            ],
        )


# -- state rules ----------------------------------------------------------------


def test_severity_mapping_prefers_api_then_thresholds() -> None:
    assert magnitude_state(10, "warning") == "elevated"
    assert magnitude_state(10, "critical") == "critical"
    assert magnitude_state(99, "normal") == "normal"  # the API knows where the cliff is
    assert magnitude_state(74.9, None) == "normal"
    assert magnitude_state(75, None) == "elevated"
    assert magnitude_state(90, None) == "critical"
    assert magnitude_state(None, None) == "normal"


def test_stale_watchdog_fires_on_last_success_not_last_error() -> None:
    now = 10_000
    fresh = _status(state="ok", last_success_ts=now - 179)
    assert collector_state(fresh, 60, now).kind == "ok"
    # An error two minutes ago does not make recent data stale.
    erring = _status(state="error", last_success_ts=now - 100, last_error="boom")
    assert collector_state(erring, 60, now).kind == "ok"
    # A poller that stopped ticking with state still "ok" is stale after 3 intervals.
    dead = _status(state="ok", last_success_ts=now - 181)
    result = collector_state(dead, 60, now)
    assert result.kind == "stale"
    assert "stopped ticking" in result.message


def test_every_condition_has_distinct_wording() -> None:
    now = 10_000
    old = now - 1000

    def msg(**kw) -> str:
        return collector_state(_status(**kw), 60, now).message

    messages = {
        "no_cookie": msg(state="no_cookie"),
        "auth": msg(state="auth_expired", last_success_ts=old, last_error_ts=now),
        "unreachable": msg(state="error", last_success_ts=old, last_error="x"),
        "rate_limited": msg(state="rate_limited", last_success_ts=old, next_poll_ts=now + 300),
        "blocked": msg(state="blocked", last_success_ts=old),
        "parse_failed": msg(
            state="error",
            last_success_ts=old,
            extra={"last_error_kind": "parse_failed"},
            last_error_ts=now,
        ),
        "shape_drift": msg(state="ok", last_success_ts=now, generic_fallback=True),
        "stopped": msg(state="ok", last_success_ts=old),
    }
    assert len(set(messages.values())) == len(messages)
    assert "expired" in messages["auth"].lower() and "shape" not in messages["auth"].lower()
    assert "shape" in messages["shape_drift"].lower()
    assert "cookie" not in messages["shape_drift"].lower()
    assert "parsed" in messages["parse_failed"].lower()
    assert "next attempt" in messages["rate_limited"]


def test_slots_and_labels() -> None:
    slots = assign_slots(["seven_day", "limit:fable", "five_hour", "limit:opus", "unknown:x"])
    assert slots == {
        "five_hour": 1,
        "seven_day": 2,
        "limit:fable": 3,
        "limit:opus": 4,
        "unknown:x": 6,
    }
    assert display_label("limit:fable", "Fable") == "Weekly — Fable"
    assert display_label("limit:fable", None) == "Weekly — Fable"
    assert display_label("five_hour", "5-hour") == "Session"  # old stored labels still map
    assert display_label("seven_day", "7-day") == "Weekly — all models"
    from quotalens.dashboard import short_label

    assert short_label("five_hour", None) == "Session"
    assert short_label("seven_day", None) == "Weekly all"
    assert short_label("limit:fable", "Fable") == "Weekly Fable"
    assert display_label("unknown:data_session", None) == "Unlabelled data session"


# -- view model ------------------------------------------------------------------


def test_healthy_model_shows_values_and_burn(settings, store) -> None:
    now = int(time.time())
    _seed(store, now)
    status = _status(
        state="ok", last_success_ts=now, last_windows=["five_hour", "seven_day", "limit:fable"]
    )
    dash = build_dashboard(settings, store, status, now, burn_alert=20.0)
    assert dash.epistemic.kind == "ok"
    assert [w.label for w in dash.windows] == ["Session", "Weekly — all models", "Weekly — Fable"]
    assert [w.slot for w in dash.windows] == [1, 2, 3]
    assert dash.windows[0].pct_text == "35" and not dash.windows[0].withheld
    assert dash.burn.rate_text == "60.00" and dash.burn.elevated  # 1 pt/min beats 20 pts/hr
    assert dash.windows[0].state == "elevated"
    assert dash.chip == "elevated"
    assert dash.chart.has_data
    assert {s.label for s in dash.chart.series} == {"Session", "Weekly all", "Weekly Fable"}


def test_stale_model_withholds_every_value(settings, store) -> None:
    now = int(time.time())
    _seed(store, now - 3600)
    status = _status(state="ok", last_success_ts=now - 3600)
    dash = build_dashboard(settings, store, status, now, burn_alert=20.0)
    assert dash.epistemic.kind == "stale"
    assert all(w.withheld and w.pct is None and w.pct_text == EM_DASH for w in dash.windows)
    assert dash.burn.withheld and dash.burn.rate_text == EM_DASH
    assert dash.chip == "stale"


def test_overage_reads_unclamped_with_clipped_bar(settings, store) -> None:
    now = int(time.time())
    spend = SpendReading(
        316,
        200,
        2,
        "USD",
        "spend",
        is_enabled=False,
        disabled_reason="org_level_disabled_until",
        disabled_until="2026-10-01T00:00:00Z",
        spend_limit_reached=True,
    )
    status = _status(state="ok", last_success_ts=now, spend=spend)
    dash = build_dashboard(settings, store, status, now, burn_alert=20.0)
    assert dash.spend is not None
    assert (dash.spend.used_text, dash.spend.limit_text) == ("$3.16", "$2.00")
    assert dash.spend.pct_text == "158"
    assert dash.spend.bar_pct == 100.0
    assert dash.spend.status_text.startswith("Extra usage off until 1 Oct")


def test_quota_window_over_100_keeps_number_clips_bar(settings, store) -> None:
    now = int(time.time())
    store.record_quota(now, [QuotaReading("five_hour", "5-hour", 112, "r1")])
    dash = build_dashboard(settings, store, _status(state="ok", last_success_ts=now), now, 20.0)
    assert dash.windows[0].pct_text == "112" and dash.windows[0].bar_pct == 100.0
    assert dash.chart.y_max == 125


def test_reset_renders_as_gap_not_line(settings, store) -> None:
    now = int(time.time())
    for i in range(6):
        pct, reset = (80 + i, "r1") if i < 3 else (2 + i, "r2")
        store.record_quota(now - (5 - i) * 60, [QuotaReading("five_hour", "5-hour", pct, reset)])
    dash = build_dashboard(settings, store, _status(state="ok", last_success_ts=now), now, 20.0)
    assert len(dash.chart.series[0].paths) == 2


# -- rendering -------------------------------------------------------------------


def test_page_renders_offline_with_no_external_resources(settings, store, secrets) -> None:
    with TestClient(create_app(settings, store, secrets)) as tc:
        page = tc.get("/")
        assert page.status_code == 200
        html = page.text
        for name in ("tokens.css", "app.css", "app.js", "favicon.svg"):
            assert tc.get(f"/static/{name}").status_code == 200
        assert tc.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
        assert tc.get("/api/dashboard/fragment").status_code == 200
        assert tc.get("/api/dashboard").json()["collector"]["kind"] == "stale"
        css = tc.get("/static/app.css").text
    assert "<title>QuotaLens</title>" in html
    assert "font-variant-numeric" in css
    assert not re.search(r'(src|href)="https?://', html)
    assert "<img" not in html
    assert '<symbol id="i-alert"' in html and html.count("<symbol") == 5


def test_never_polled_renders_em_dash_not_zero(settings, store, secrets) -> None:
    with TestClient(create_app(settings, store, secrets)) as tc:
        html = tc.get("/").text
    assert 'class="readout off"' in html
    assert f'<span class="num">{EM_DASH}</span>' in html  # visible, not only the hidden twin
    assert "0.00" not in html


def test_stale_page_shows_em_dash_and_hatch_instead_of_numbers(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now - 3600, base=42)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now - 3600
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert 'class="chip stale"' in html and "stale" in html
    assert html.count('class="meter is-stale"') == 3
    assert html.count('class="bar hatch"') >= 3
    assert '<span class="num">57</span>' not in html  # the last stored value must not show
    assert html.count(f'<span class="num">{EM_DASH}</span>') >= 4  # readout + three meters
    assert "stopped ticking" in html
    assert 'class="readout off"' in html


def test_healthy_page_shows_three_windows_and_values(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    app.state.qw.poller.status.spend = SpendReading(316, 200, 2, "USD", "spend")
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert html.count('class="meter"') == 3
    assert ">limit:fable<" not in html and ">Weekly — Fable<" in html  # never a raw key
    assert '<span class="num">35</span>' in html
    assert "$3.16 / $2.00" in html and '<span class="num">158</span>' in html
    assert 'style="width:100.0%;background:var(--hair-firm)"' in html  # neutral: off
    assert 'stroke="var(--s1)" stroke-width="var(--trace-hero)"' in html
    assert 'stroke-dasharray="var(--dash-3)"' in html
    assert "No local session data yet" in html  # honest empty attribution


def test_auth_failure_renders_auth_chip_and_message(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "auth_expired"
    app.state.qw.poller.status.last_success_ts = now - 30
    app.state.qw.poller.status.last_error_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert 'class="chip auth"' in html
    assert "Cookie expired" in html
    assert 'class="hstrip auth"' in html


def test_app_css_stays_within_budget() -> None:
    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    tokens = resources.files("quotalens.web").joinpath("tokens.css").read_text()

    def minify(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"\s+", " ", text)
        return re.sub(r"\s*([{};:,])\s*", r"\1", text)

    assert len(minify(css).encode()) + len(minify(tokens).encode()) < 14_000
    # no colour literal outside tokens.css
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", minify(css))


def test_live_payload_end_to_end_renders_three_windows(settings, store, secrets) -> None:
    now = int(time.time())
    handler = make_handler(usage=USAGE_LIVE_2026_09, overage_status=404)
    poller = Poller(
        settings,
        store,
        secrets,
        Redactor(),
        client_factory=lambda c: make_client(handler, c),
        clock=lambda: float(now),
    )
    asyncio.run(poller.poll_once())
    dash = build_dashboard(settings, store, poller.status, now, 20.0)
    assert [w.label for w in dash.windows] == ["Session", "Weekly — all models", "Weekly — Sonnet"]
    assert dash.spend is not None and dash.spend.pct_text == "158"
    assert dash.diagnostics == ["Payload blocks without a reset time, not charted: nimbus_quill."]


# -- Job A: fixes from the running dashboard -------------------------------------


def test_cold_start_picks_smallest_covering_range_or_says_collecting() -> None:
    now = 1_000_000
    assert resolve_range(ViewOptions(), None, now).collecting
    r = resolve_range(ViewOptions(), now - 27 * 60, now)
    assert (r.key, r.auto, r.collecting) == ("1h", True, False)
    r = resolve_range(ViewOptions(), now - 5 * 60, now)
    assert (r.key, r.collecting) == ("15m", True)
    assert resolve_range(ViewOptions(), now - 30 * 3600, now).key == "7d"
    assert resolve_range(ViewOptions(), now - 40 * 86400, now).key == "all"
    explicit = resolve_range(ViewOptions(range_key="24h"), now - 60, now)
    assert (explicit.key, explicit.auto) == ("24h", False)


def test_collecting_text_replaces_the_grid(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now, minutes=5, reset_in=None)  # cold start, no session yet
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert "Collecting: 4m of data" in html
    assert '<line x1="44"' not in html  # no near-empty grid


def test_gap_is_hatched_and_counted_while_reset_is_a_clean_break(settings, store) -> None:
    now = int(time.time())
    # 10 minutes of samples, a 20-minute hole, then a reset and 10 more minutes.
    for i in range(10):
        store.record_quota(
            now - 40 * 60 + i * 60, [QuotaReading("five_hour", "5-hour", 50 + i, "r1")]
        )
    for i in range(10):
        store.record_quota(
            now - 10 * 60 + i * 60, [QuotaReading("five_hour", "5-hour", 1 + i, "r2")]
        )
    dash = build_dashboard(settings, store, _status(state="ok", last_success_ts=now), now, 20.0)
    assert len(dash.chart.series[0].paths) == 2  # the reset splits the line
    assert len(dash.chart.gaps) == 1  # the hole is marked
    assert dash.chart.gap_minutes == 21  # last sample of the first block to first of the second
    assert dash.side["Not collected"] == "21 min in range"
    from quotalens.render import render_app

    html = render_app(dash)
    assert html.count('fill="url(#gap)"') == 1


def test_trailing_gap_counts_when_collector_stopped(settings, store) -> None:
    now = int(time.time())
    _seed(store, now - 30 * 60)
    dash = build_dashboard(
        settings, store, _status(state="ok", last_success_ts=now - 30 * 60), now, 20.0
    )
    assert dash.chart.gap_minutes == 30


def test_diagnostics_live_in_side_panel_not_hero(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    st = app.state.qw.poller.status
    st.state, st.last_success_ts = "ok", now
    st.ignored_blocks = [{"key": "nimbus_quill", "reason": "no resets_at"}]
    with TestClient(app) as tc:
        html = tc.get("/").text
    assert "hstrip" not in html
    assert "Diagnostics" in html and "nimbus_quill" in html
    assert html.index("nimbus_quill") > html.index('class="cols"')


def test_extra_usage_neutral_when_off_critical_when_on_and_over(settings, store) -> None:
    now = int(time.time())
    off = SpendReading(316, 200, 2, "USD", "spend", is_enabled=False)
    on = SpendReading(316, 200, 2, "USD", "spend", is_enabled=True)
    under = SpendReading(50, 200, 2, "USD", "spend", is_enabled=True)
    dash = build_dashboard(
        settings, store, _status(state="ok", last_success_ts=now, spend=off), now, 20.0
    )
    assert dash.spend.state == "normal" and dash.chip == ""
    dash = build_dashboard(
        settings, store, _status(state="ok", last_success_ts=now, spend=on), now, 20.0
    )
    assert dash.spend.state == "critical" and dash.chip == "critical"
    dash = build_dashboard(
        settings, store, _status(state="ok", last_success_ts=now, spend=under), now, 20.0
    )
    assert dash.spend.state == "normal"


def test_burn_rate_withheld_under_five_minutes(settings, store) -> None:
    now = int(time.time())
    _seed(store, now, minutes=4, base=10)
    dash = build_dashboard(settings, store, _status(state="ok", last_success_ts=now), now, 20.0)
    assert (
        not dash.burn.withheld and dash.burn.rate_text == EM_DASH
    )  # headroom shows, rate does not
    assert dash.burn.why.startswith("Collecting: 3m of samples")
    assert dash.burn.headroom_text == "87"
    _seed(store, now, minutes=7, base=10)
    dash = build_dashboard(settings, store, _status(state="ok", last_success_ts=now), now, 20.0)
    assert not dash.burn.withheld
    assert "lookback 15m" in dash.burn.detail


def test_view_query_parsing_and_invalid_input() -> None:
    now = 1_800_000_000
    v = parse_view(
        {"range": "6h", "hide": "seven_day,limit:fable", "lookback": "1h", "refresh": "off"}, now
    )
    assert v.range_key == "6h" and v.hidden == {"seven_day", "limit:fable"}
    assert v.lookback_s(900) == 3600 and v.refresh_s(30) == 0
    assert v.query() == "range=6h&hide=limit%3Afable%2Cseven_day&lookback=1h&refresh=off"
    bad = parse_view(
        {"range": "yesterday", "hide": "<script>", "lookback": "2h", "refresh": "9s"}, now
    )
    assert bad == ViewOptions()
    custom = parse_view({"range": f"{now - 3600}-{now + 999}"}, now)
    assert custom.range_key == "custom" and custom.custom == (now - 3600, now)
    assert parse_view({"range": f"{now}-{now + 10}"}, now).range_key == "auto"  # span too short
    assert ViewOptions(range_key="1h").toggled("x") == {"x"}
    assert ViewOptions(hidden=frozenset({"x"})).toggled("x") == frozenset()


def test_meter_change_is_over_the_selected_range(settings, store) -> None:
    now = int(time.time())
    _seed(store, now, minutes=16)
    dash = build_dashboard(
        settings,
        store,
        _status(state="ok", last_success_ts=now),
        now,
        20.0,
        ViewOptions(range_key="1h"),
    )
    assert dash.windows[0].delta_text == "+15 pts in range"


def test_five_minute_lookback_can_actually_display(settings, store) -> None:
    now = int(time.time())
    _seed(store, now, minutes=5, base=10)  # samples at now-4m .. now: a 4-minute span
    dash = build_dashboard(
        settings,
        store,
        _status(state="ok", last_success_ts=now),
        now,
        20.0,
        ViewOptions(lookback_key="5m"),
    )
    assert not dash.burn.withheld and dash.burn.rate_text == "60.00"
    assert "lookback 5m" in dash.burn.detail
