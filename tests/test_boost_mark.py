"""The boost mark on the chart: a crimson step, a rocket, and two lines of label.

The fall was instantaneous and the chart drew it as a twelve-hour diagonal, because
no samples exist across the outage and the renderer joined the last point before it
to the first point after. That reads as a decline the owner caused; it was a step he
was given. So the step comes first — a crimson diagonal would be a lie in a louder
colour — and the crimson *is* the marker, which is why there is no pointer rule.
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import pytest

from quotalens.dashboard import BoostMark, ChartView, SeriesView
from quotalens.render import BOOST_COLOUR, BOOST_MIN_PX, _boost_marks, _rocket, _series

DESIGN_SOURCE = Path(__file__).resolve().parents[1] / "design" / "marks" / "boost-rocket.svg"


def _mark(**kw) -> BoostMark:
    return BoostMark(
        kw.get("x", 370.0),
        kw.get("y", 20.0),
        "limits boosted",
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
    assert svg.count("limits boosted") == 1
    assert svg.count('circle cx="12" cy="9.6"') == 1  # the rocket's porthole, once
    assert "Weekly Fable" in svg and "Weekly all" in svg


def test_the_label_is_two_lines_beside_the_rocket_not_under_it() -> None:
    svg = _boost_marks(_chart([_mark(x=300.0, y=40.0)]))
    heading = re.search(r'<text x="([\d.]+)" y="([\d.]+)" class="ax bx">limits boosted</text>', svg)
    detail = re.search(r'<text x="([\d.]+)" y="([\d.]+)" class="ax">([^<]+)</text>', svg)
    rocket = re.search(r'<g transform="translate\(([\d.]+) ([\d.]+)\)', svg)

    assert heading and detail and rocket
    assert float(heading.group(1)) == float(detail.group(1)), "both lines start at one x"
    assert float(detail.group(2)) > float(heading.group(2)), "the detail is the second line"
    assert float(rocket.group(1)) < float(heading.group(1)), "the rocket is to their left"


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
