"""The boost mark on the chart: a crimson step, a rocket, and two lines of label.

The fall was instantaneous and the chart drew it as a twelve-hour diagonal, because
no samples exist across the outage and the renderer joined the last point before it
to the first point after. That reads as a decline the owner caused; it was a step he
was given. So the step comes first — a crimson diagonal would be a lie in a louder
colour — and the crimson *is* the marker, which is why there is no pointer rule.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

from quotalens.dashboard import BoostMark, ChartView, SeriesView, build_dashboard
from quotalens.render import BOOST_COLOUR, BOOST_MIN_PX, _boost_marks, _rocket, _series

DESIGN_SOURCE = Path(__file__).resolve().parents[1] / "design" / "marks" / "boost-rocket.svg"


def _mark(**kw) -> BoostMark:
    return BoostMark(
        kw.get("x", 370.0),
        kw.get("y", 20.0),
        "Limits Boosted",
        kw.get("detail", "08:09 · 98% → 7%"),
    )


def _chart(marks: list[BoostMark]) -> ChartView:
    chart = object.__new__(ChartView)
    chart.boost_marks = marks
    return chart


# -- the step ---------------------------------------------------------------------


def test_the_drop_is_a_vertical_in_the_boost_colour() -> None:
    series = SeriesView(
        "seven_day",
        "Weekly all",
        2,
        ["M10 20 L20 20"],
        20.0,
        20.0,
        20.0,
        False,
        "/",
        ["M100.0 20.0 L100.0 190.0"],
    )
    svg = _series(series)

    drop = re.search(
        r'<path d="([^"]+)" stroke="' + re.escape(BOOST_COLOUR) + r'" stroke-width="([\d.]+)"',
        svg,
    )
    assert drop is not None, "the step is drawn in the boost colour"
    assert drop.group(2) == "2.6"
    xs = re.findall(r"[ML]([\d.]+) [\d.]+", drop.group(1))
    assert xs == ["100.0", "100.0"], "the step is vertical: one instant, two levels"


def test_the_colour_stops_at_the_step() -> None:
    """The flat runs either side keep the window's own series colour."""
    series = SeriesView(
        "seven_day",
        "Weekly all",
        2,
        ["M10 20 L20 20", "M30 90 L40 90"],
        40.0,
        90.0,
        90.0,
        False,
        "/",
        ["M25.0 20.0 L25.0 90.0"],
    )
    svg = _series(series)

    assert svg.count(f'stroke="{BOOST_COLOUR}"') == 1
    assert svg.count('stroke="var(--s2)"') == 2  # both flat runs, unchanged
    assert BOOST_COLOUR not in svg.split('stroke="var(--s2)"')[1].split("/>")[0]


def test_a_series_with_no_boost_carries_no_crimson() -> None:
    series = SeriesView("five_hour", "Session", 1, ["M10 20 L20 20"], 20.0, 20.0, 20.0, False, "/")
    assert BOOST_COLOUR not in _series(series)


# -- one mark per moment ----------------------------------------------------------


def test_two_windows_boosting_in_one_poll_give_one_rocket_and_one_label() -> None:
    """A previous change found two glyphs stacked at one x. One moment, one mark."""
    svg = _boost_marks(
        _chart([_mark(detail="08:09 · Weekly Fable 100% → 1%, Weekly all 98% → 0%")])
    )
    assert svg.count(">Limits Boosted</text>") == 1  # once drawn, once in the aria-label
    assert svg.count('circle cx="12" cy="9.6"') == 1  # the rocket's porthole, once
    assert svg.count("<title>") == 1
    assert svg.count('<g class="boost"') == 1


def test_the_label_is_one_line_and_the_detail_is_on_hover() -> None:
    """The detail is reference material: with two windows it ran past the plot edge."""
    svg = _boost_marks(_chart([_mark(x=300.0, y=40.0, detail="08:09 · 98% → 7%")]))

    assert svg.count("<text") == 1, "one line only"
    assert ">Limits Boosted</text>" in svg
    assert "<title>08:09 · 98% → 7%</title>" in svg
    assert "98% → 7%" not in svg.split("</title>")[1], "the detail is not also drawn"


def test_the_group_wraps_both_the_rocket_and_the_heading() -> None:
    """Hovering either has to show the tooltip, so both live under one <g>."""
    svg = _boost_marks(_chart([_mark()]))
    group = svg[svg.index('<g class="boost"') : svg.rindex("</g>")]

    assert "<title>" in group
    assert 'circle cx="12" cy="9.6"' in group  # the rocket
    assert ">Limits Boosted</text>" in group  # and the heading
    assert 'role="img"' in svg and 'aria-label="Limits Boosted. ' in svg


def test_the_rocket_is_not_hidden_from_the_mouse_or_the_reader() -> None:
    """`.trace` sets fill:none; a hidden, unfillable shape receives no hover."""
    svg = _boost_marks(_chart([_mark()]))
    assert 'aria-hidden="true"' not in svg, "the wrapper carries the name instead"

    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    assert ".boost{pointer-events:all}" in css


def test_the_rocket_alone_is_still_hidden_where_nothing_wraps_it() -> None:
    assert 'aria-hidden="true"' in _rocket(0, 0, 18)


def test_no_pointer_rule_is_drawn() -> None:
    """An earlier draft stood a grey rule on the axis; the crimson marks it alone."""
    assert "<line" not in _boost_marks(_chart([_mark()]))


# -- the drawing ------------------------------------------------------------------


def test_the_rocket_is_omitted_below_sixteen_pixels_never_scaled() -> None:
    """Four fills inside sixteen pixels is four pixels each. It does not degrade."""
    assert _rocket(0, 0, BOOST_MIN_PX) != ""
    assert _rocket(0, 0, BOOST_MIN_PX - 1) == ""
    assert _rocket(0, 0, 8) == ""


def test_the_rocket_is_upright_and_scaled_only_uniformly() -> None:
    svg = _rocket(100.0, 50.0, 18)
    transform = re.search(r'transform="translate\([\d. ]+\) scale\(([\d.]+)\)"', svg)
    assert transform and float(transform.group(1)) == pytest.approx(18 / 24)
    assert "rotate" not in svg, "upright: never tilted"


@pytest.mark.skipif(not DESIGN_SOURCE.exists(), reason="design/ is not in an installed wheel")
def test_the_packaged_rocket_is_the_design_file_verbatim() -> None:
    """The drawing has one source. A redraw would drift silently."""
    packaged = resources.files("quotalens.web").joinpath("boost-rocket.svg").read_text()
    assert packaged == DESIGN_SOURCE.read_text()


def test_every_path_in_the_emitted_rocket_comes_from_the_file() -> None:
    packaged = resources.files("quotalens.web").joinpath("boost-rocket.svg").read_text()
    from_file = set(re.findall(r'\sd="([^"]+)"', packaged))
    emitted = set(re.findall(r'\sd="([^"]+)"', _rocket(0, 0, 18)))

    assert emitted == from_file, "the mark is inlined, not redrawn"
    assert re.findall(r'fill="(#[0-9A-Fa-f]{6})"', _rocket(0, 0, 18)) == re.findall(
        r'fill="(#[0-9A-Fa-f]{6})"', packaged
    )


def test_the_boost_colour_appears_in_exactly_two_places() -> None:
    """The rocket and the drop segment. Nothing else may borrow it — DESIGN.md §1."""
    css = resources.files("quotalens.web").joinpath("app.css").read_text()
    tokens = resources.files("quotalens.web").joinpath("tokens.css").read_text()
    assert BOOST_COLOUR not in css and BOOST_COLOUR not in tokens


# -- the range the mark vanished on -----------------------------------------------


def _seeded_store(tmp_path, boost_ts: int, gap_s: int):
    """A store whose boost sits just after a gap, the shape the real one has."""
    from quotalens.boost import BOOST_KIND
    from quotalens.config import settings_from_env
    from quotalens.parse import QuotaReading
    from quotalens.store import Store

    settings = settings_from_env().with_overrides(db_path=tmp_path / "t.db")
    store = Store(settings.db_path)
    reset = datetime.fromtimestamp(boost_ts + 3 * 86400, UTC).isoformat()
    for i in range(60):  # climbing to 98%, ending where the gap begins
        store.record_quota(
            boost_ts - gap_s - (60 - i) * 60,
            [QuotaReading("seven_day", "Weekly — all models", 38.0 + i, reset, None, False)],
        )
    for i in range(60):  # and resuming after it, at 0%
        store.record_quota(
            boost_ts + i * 60,
            [QuotaReading("seven_day", "Weekly — all models", float(i) / 10, reset, None, False)],
        )
    store.record_event(
        BOOST_KIND, "Weekly — all models fell 98% -> 0% with no reset. Limit raised.", ts=boost_ts
    )
    return settings, store


def _render(settings, store, raw_range: str, now: int) -> str:
    from quotalens.poller import PollerStatus
    from quotalens.render import _chart
    from quotalens.views import parse_view

    status = PollerStatus()
    status.state, status.last_success_ts = "ok", now
    dash = build_dashboard(
        settings, store, status, now, 20.0, parse_view({"range": raw_range}, now)
    )
    return _chart(dash)


def test_a_dragged_range_across_the_gap_still_shows_the_boost(tmp_path) -> None:
    """The bug: the range's left edge landed inside the outage before the boost.

    The boost row was then the *first* row in range with nothing to step from, so
    the crimson drop and the mark were both silently dropped — on exactly the ranges
    someone drags around a boost to look at it. A `_chart_view` unit test passes for
    every range, because it never sees a range that starts inside the gap.
    """
    boost_ts = 1_800_000_000
    gap_s = 6 * 3600 + 34 * 60  # the real outage
    now = boost_ts + 6 * 3600
    settings, store = _seeded_store(tmp_path, boost_ts, gap_s)
    try:
        preset = _render(settings, store, "24h", now)
        inside_gap = _render(settings, store, f"{boost_ts - gap_s + 600}-{now}", now)
        wide = _render(settings, store, f"{boost_ts - gap_s - 3600}-{now}", now)
        starts_at_boost = _render(settings, store, f"{boost_ts}-{now}", now)
        excludes = _render(settings, store, f"{boost_ts + 600}-{now}", now)
    finally:
        store.close()

    for name, svg in (
        ("24h preset", preset),
        ("dragged into the gap", inside_gap),
        ("dragged before the gap", wide),
        ("starting at the boost", starts_at_boost),
    ):
        assert BOOST_COLOUR in svg, f"{name}: no crimson drop"
        assert ">Limits Boosted</text>" in svg, f"{name}: no mark"

    assert BOOST_COLOUR not in excludes, "a range after the boost must not invent one"
    assert ">Limits Boosted</text>" not in excludes


# -- what else a custom range was hiding ------------------------------------------


def _runway_chart(end_offset_s: int, exhaust_offset_s: int | None = None):
    """A chart marked up for a range ending ``end_offset_s`` from now."""
    from quotalens.dashboard import _mark_runway
    from quotalens.runway import Runway
    from quotalens.views import ResolvedRange

    now = 1_800_000_000
    reset_ts = now + 3 * 3600
    chart = object.__new__(ChartView)
    chart.y_max, chart.now_x, chart.future = 100.0, 0.0, False
    chart.hour_x, chart.projection, chart.projection_note = [], "", ""
    chart.projection_critical, chart.cross = False, None
    runway = Runway(
        reset_ts,
        reset_ts - now,
        40.0,
        60.0,
        20.0,
        now + exhaust_offset_s if exhaust_offset_s else None,
        90.0,
        20.0,
        "",
        "",
    )
    rng = ResolvedRange(now - 6 * 3600, now + end_offset_s, "custom", "custom", False, False, 0)
    _mark_runway(chart, runway, (now - 3600, reset_ts), rng, now, False)
    return chart


def test_the_now_rule_is_drawn_whenever_now_is_inside_the_range() -> None:
    """A custom range ending at now had no marker at all, so nothing said where now was."""
    assert _runway_chart(+3600).future is True  # range runs past now
    assert _runway_chart(0).future is True  # ends exactly at now
    assert _runway_chart(-1800).future is False  # now is genuinely outside


def test_a_projection_hidden_by_the_range_says_so_instead_of_vanishing() -> None:
    """Outside the frame rather than unmeasured, and the same rule applies."""
    inside = _runway_chart(+3 * 3600 + 60, exhaust_offset_s=3600)
    assert inside.projection and not inside.projection_note

    clipped = _runway_chart(+1800, exhaust_offset_s=3600)
    assert not clipped.projection
    assert "falls outside this range" in clipped.projection_note
    assert "exhaustion" in clipped.projection_note


def test_a_custom_range_over_a_near_empty_database_still_says_collecting() -> None:
    """`collecting` is about how much data exists, not which slice is on screen."""
    from quotalens.views import ViewOptions, resolve_range

    now = 1_800_000_000
    opts = ViewOptions(range_key="custom", custom=(now - 3600, now))
    thin = resolve_range(opts, now - 300, now)
    plenty = resolve_range(opts, now - 86400, now)

    assert thin.collecting is True
    assert plenty.collecting is False
