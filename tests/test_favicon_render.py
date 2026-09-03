"""The favicon, rasterised in a real browser.

This file exists because a string assertion cannot see a colour that fails to
resolve. `/favicon.svg` shipped drawing nothing at all: the colours were bare
`var(--s1)` references, the standalone image document has no `tokens.css` behind
it, an invalid `var()` substitution makes `stroke` fall back to its initial value
of `none`, and both circles already carry `fill="none"`. Every string test passed.
The tab was empty.

So the assertions here are about pixels and computed styles, in Chromium, in both
colour schemes. The SVG is loaded as its own document (and, for the pixel count,
through an `<img>`), never with `set_content`, because embedding it in an HTML
page would let it inherit styles the browser never gives a favicon.

Skipped when Playwright or its browser is missing. `pip install -e '.[browser]'`
then `playwright install chromium`. CI runs it on Linux only: this is a question
about SVG and CSS, not about the operating system, so paying for a browser
download on six matrix jobs would buy nothing.
"""

from __future__ import annotations

import base64
import json

import pytest

from quotalens.render import (
    FAVICON_CIRC,
    FAVICON_GRID,
    FAVICON_LIGHT_STYLE,
    FAVICON_R,
    FAVICON_STROKE,
    MARK_CIRC,
    MARK_GRID,
    MARK_R,
    MARK_SIZE,
    MARK_STROKE,
    ring,
)

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Exception as exc:  # the package is present but the browser is not
            pytest.skip(f"chromium unavailable: {exc}")
        yield launched
        launched.close()


def favicon(fraction: float | None, state: str = "normal") -> str:
    """Exactly what the route serves, including the light-scheme block."""
    return ring(
        fraction,
        state,
        FAVICON_GRID,
        FAVICON_GRID,
        FAVICON_R,
        FAVICON_STROKE,
        FAVICON_CIRC,
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="QuotaLens"',
        head=f"<title>QuotaLens</title>{FAVICON_LIGHT_STYLE}",
    )


def _data_url(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


def strokes(browser, svg: str, scheme: str) -> list[str]:
    """The computed stroke of every circle, with the SVG as its own document."""
    context = browser.new_context(color_scheme=scheme)
    try:
        page = context.new_page()
        page.goto(_data_url(svg))
        return page.evaluate(
            "() => [...document.querySelectorAll('circle')].map(c => getComputedStyle(c).stroke)"
        )
    finally:
        context.close()


def opaque_pixels(browser, svg: str, scheme: str) -> int:
    """Draw it through an <img>, the way a favicon is used, and count what lands."""
    context = browser.new_context(color_scheme=scheme)
    try:
        page = context.new_page()
        page.goto("about:blank")
        return page.evaluate(
            """async (src) => {
                const img = new Image(); img.src = src;
                await img.decode();
                const size = 128;
                const cv = document.createElement("canvas");
                cv.width = cv.height = size;
                const cx = cv.getContext("2d");
                cx.drawImage(img, 0, 0, size, size);
                const d = cx.getImageData(0, 0, size, size).data;
                let n = 0;
                for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
                return n;
            }""",
            _data_url(svg),
        )
    finally:
        context.close()


CASES = [("normal", 0.55), ("elevated", 0.8), ("critical", 0.96), ("unknown", None)]


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize(("state", "fraction"), CASES)
def test_every_stroke_resolves_to_a_real_colour(browser, scheme, state, fraction) -> None:
    resolved = strokes(browser, favicon(fraction, state), scheme)
    assert resolved, "no circles in the document"
    for value in resolved:
        assert value.startswith("rgb"), f"{state}/{scheme}: stroke computed to {value!r}"


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize(("state", "fraction"), CASES)
def test_the_favicon_is_not_blank(browser, scheme, state, fraction) -> None:
    drawn = opaque_pixels(browser, favicon(fraction, state), scheme)
    assert drawn > 0, f"{state}/{scheme}: rasterised to {drawn} visible pixels of 16384"


@pytest.mark.parametrize(("state", "fraction"), CASES)
def test_the_two_colour_schemes_actually_differ(browser, state, fraction) -> None:
    """If the media query were dead every test above would still pass, so check it."""
    dark = strokes(browser, favicon(fraction, state), "dark")
    light = strokes(browser, favicon(fraction, state), "light")
    assert dark != light, f"{state}: both schemes drew {json.dumps(dark)}"


def test_the_header_mark_still_resolves_inside_the_page(browser) -> None:
    """The inlined mark carries no <style>, so this proves the tokens reach it."""
    mark = ring(
        0.55, "normal", MARK_GRID, MARK_SIZE, MARK_R, MARK_STROKE, MARK_CIRC, 'aria-hidden="true"'
    )
    assert "<style" not in mark
    context = browser.new_context(color_scheme="dark")
    try:
        page = context.new_page()
        page.set_content(
            "<style>:root{--txt-far:#626A6B;--s1:#F2B33D}</style>" + mark,
        )
        resolved = page.evaluate(
            "() => [...document.querySelectorAll('circle')].map(c => getComputedStyle(c).stroke)"
        )
    finally:
        context.close()
    assert resolved == ["rgb(98, 106, 107)", "rgb(242, 179, 61)"]
