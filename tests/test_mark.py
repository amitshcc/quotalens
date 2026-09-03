"""The ring mark: the arc is a reading, and an unknown reading draws no arc.

DESIGN.md §9 fixes the geometry. What these tests hold is the part that can drift:
that the arc length is the session window, that it is *the same* reading the 5-hour
meter shows, and that losing the reading empties the ring rather than freezing it.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import quotalens
from quotalens.api import create_app
from quotalens.dashboard import build_dashboard
from quotalens.parse import QuotaReading
from quotalens.render import (
    FAVICON_CIRC,
    FAVICON_LIGHT_STYLE,
    MARK_CIRC,
    MARK_COLOURS,
    MARK_GRID,
    MARK_R,
    MARK_SIZE,
    MARK_STROKE,
    header_mark,
    mark_reading,
    ring,
)

# Only the arc carries both a dash pair and the rotate that starts it at twelve
# o'clock, so this cannot pick up the dashed empty track or a dashed status icon.
ARC = re.compile(r'stroke-dasharray="([\d.]+) ([\d.]+)" transform="rotate\(-90')


def _dash_arc(svg: str) -> tuple[float, float] | None:
    """(used, circumference) from the arc circle, or None when there is no arc."""
    m = ARC.search(svg)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _mark(fraction: float | None, state: str = "normal") -> str:
    return ring(
        fraction, state, MARK_GRID, MARK_SIZE, MARK_R, MARK_STROKE, MARK_CIRC, 'aria-hidden="true"'
    )


# -- geometry -------------------------------------------------------------------


@pytest.mark.parametrize("pct", [0.0, 1.0, 12.5, 50.0, 68.0, 91.0, 100.0])
def test_the_arc_length_is_the_circumference_times_the_fraction(pct: float) -> None:
    used, circumference = _dash_arc(_mark(pct / 100))
    assert circumference == MARK_CIRC
    assert used == round(MARK_CIRC * pct / 100, 2)


def test_the_favicon_uses_its_own_circumference_not_a_scaled_one() -> None:
    """Redrawn on a true 16 grid so the strokes land on whole pixels, not scaled."""
    used, circumference = _dash_arc(ring(0.68, "normal", 16, 16, 5.5, 3.0, FAVICON_CIRC, ""))
    assert circumference == FAVICON_CIRC != MARK_CIRC
    assert used == round(FAVICON_CIRC * 0.68, 2)


def test_the_canonical_static_mark_matches_the_design_file() -> None:
    """68% on the 24 grid is the drawing in design/mark.svg, to the digit."""
    assert 'stroke-dasharray="38.45 56.55"' in _mark(0.68)


# -- state ----------------------------------------------------------------------


@pytest.mark.parametrize("state", ["normal", "elevated", "critical"])
def test_the_arc_takes_its_colour_from_the_meter_state(state: str) -> None:
    token, _light, dark = MARK_COLOURS[state]
    svg = _mark(0.9, state)
    assert f'stroke="var({token}, {dark})"' in svg
    assert svg.count("<circle") == 2  # the track is always behind it


def test_no_reading_draws_a_dashed_empty_track_and_no_arc() -> None:
    svg = _mark(None)
    assert _dash_arc(svg) is None, "a frozen arc would be a stale reading in the tab strip"
    assert svg.count("<circle") == 1
    assert 'stroke-dasharray="3 3"' in svg
    assert "--st-critical" not in svg and "--s1" not in svg


def test_every_colour_in_the_mark_carries_a_fallback() -> None:
    """A bare var() in the favicon is an invalid substitution, and `stroke` becomes `none`.

    That shipped: the icon was fully transparent in the tab strip. The rasterised
    proof is in test_favicon_render.py; this is the cheap guard that runs everywhere.
    """
    for svg in (_mark(0.68), _mark(None), _mark(0.91, "critical"), _mark(0.8, "elevated")):
        bare = re.findall(r"var\(--[a-z-]+\)", svg)
        assert bare == [], f"no fallback for {bare}"


def test_the_mark_colours_still_match_tokens_css() -> None:
    """The fallbacks are a copy of tokens.css, so this is what stops it drifting.

    There is no way to resolve a CSS custom property from Python, so the values
    have to be repeated in render.py. They may be repeated; they may not disagree.
    """
    css = (Path(quotalens.__file__).parent / "web" / "tokens.css").read_text()
    for token, light, dark in MARK_COLOURS.values():
        m = re.search(rf"{token}:\s*light-dark\(([^,]+),\s*([^)]+)\)", css)
        assert m, f"{token} is no longer a light-dark() pair in tokens.css"
        assert (m.group(1).strip().upper(), m.group(2).strip().upper()) == (light, dark), token


def test_only_the_favicon_carries_a_style_block() -> None:
    """The header mark is inlined, so its tokens resolve and follow the theme toggle.

    A media query inside it would override that with the OS preference, which is
    the one thing the toggle exists to escape.
    """
    assert "<style" not in _mark(0.68) and "<style" not in _mark(None)
    assert "prefers-color-scheme" in FAVICON_LIGHT_STYLE
    for token, light, _dark in MARK_COLOURS.values():
        assert f"{token}:{light}" in FAVICON_LIGHT_STYLE


def test_the_inlined_mark_is_attributes_only() -> None:
    """The header mark is built from attributes, so it needs no style attribute either.

    The standalone favicon does carry a <style>, because an image document has no
    tokens.css to resolve against. Only what the page inlines is covered here.
    """
    for svg in (_mark(0.68), _mark(None), _mark(0.91, "critical")):
        assert "<style" not in svg and 'style="' not in svg


# -- the same reading as the meter ----------------------------------------------


def _seed(store, now: int, pct: float, severity: str = "normal") -> None:
    for i in range(6):
        store.record_quota(
            now - (5 - i) * 60,
            [
                QuotaReading("five_hour", "5-hour", pct, None, severity, True),
                QuotaReading("seven_day", "7-day", 40.0, "wk", "normal", False),
            ],
        )


def test_the_header_mark_is_the_five_hour_meter(settings, store) -> None:
    from quotalens.poller import PollerStatus

    now = int(time.time())
    _seed(store, now, 62.0)
    status = PollerStatus()
    status.state, status.last_success_ts = "ok", now
    dash = build_dashboard(settings, store, status, now, burn_alert=20.0)

    meter = next(w for w in dash.windows if w.key == "five_hour")
    fraction, state = mark_reading(dash)
    assert fraction == meter.pct / 100 and state == meter.state
    used, _ = _dash_arc(header_mark(dash))
    assert used == round(MARK_CIRC * meter.pct / 100, 2)


def test_a_stale_dashboard_empties_the_mark(settings, store) -> None:
    from quotalens.poller import PollerStatus

    now = int(time.time())
    _seed(store, now - 3600, 62.0)
    status = PollerStatus()
    status.state, status.last_success_ts = "ok", now - 3600
    dash = build_dashboard(settings, store, status, now, burn_alert=20.0)

    assert dash.epistemic.kind == "stale"
    assert mark_reading(dash) == (None, "normal")
    assert _dash_arc(header_mark(dash)) is None


# -- the favicon route ----------------------------------------------------------


def test_the_favicon_reflects_the_current_reading_and_is_never_cached(
    settings, store, secrets
) -> None:
    now = int(time.time())
    _seed(store, now, 44.0)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        response = tc.get("/favicon.svg")

    assert response.headers["cache-control"] == "no-cache"  # revalidate, but cacheable
    assert response.headers["content-type"].startswith("image/svg+xml")
    used, circumference = _dash_arc(response.text)
    assert (used, circumference) == (round(FAVICON_CIRC * 0.44, 2), FAVICON_CIRC)
    assert 'role="img"' in response.text and "44% of the session window used" in response.text


def test_the_favicon_falls_back_to_the_static_file_before_the_first_poll(
    settings, store, secrets
) -> None:
    with TestClient(create_app(settings, store, secrets)) as tc:
        response = tc.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"  # revalidate, but cacheable
    assert "<title>QuotaLens</title>" in response.text


def test_the_page_and_the_favicon_agree(settings, store, secrets) -> None:
    """The tab strip and the meter are one reading, so they cannot drift apart."""
    now = int(time.time())
    _seed(store, now, 77.0)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        html = tc.get("/").text
        icon = tc.get("/favicon.svg").text

    assert _dash_arc(html) == (round(MARK_CIRC * 0.77, 2), MARK_CIRC)
    assert _dash_arc(icon) == (round(FAVICON_CIRC * 0.77, 2), FAVICON_CIRC)
    assert ">77<" in html  # the meter reads the same number
