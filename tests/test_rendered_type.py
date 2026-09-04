"""Type sizes as a browser computes them, for the one panel that got this wrong twice.

The budget table's answer column was set at `--fs-sub` (18px), then at `--fs-lg`
(15px), inside a table set at `--fs-sm` (12px). Both times the row read as two
rows, and both times every string assertion passed: a stylesheet cascade is not
visible in the HTML. So this asks Chromium.

The rule the file holds: inside a budget row every cell is one size, including the
dim gloss inside the answer cell, and emphasis is carried by colour and weight
instead. Header cells are excluded — `thead th` is `--fs-xs` across every table
here, which is the convention rather than the bug.

What is *not* here: whether the mono answer reads heavier than the sans beside it
at the same pixel size. A Range's height reports the font's line-box metrics, not
apparent size, so it differs between families that look identical and it differs
again between headless fallbacks and the desktop's real fonts — an assertion on it
would fail for reasons that have nothing to do with the question. That comparison
was made by eye in Chrome, in both themes, which is how the design instruction
asked for it to be settled.

Skipped when Playwright or its browser is missing; CI runs it on Linux, because
this is a question about CSS rather than about the operating system.
"""

from __future__ import annotations

from importlib import resources

import pytest

from quotalens.budget import WeeklyLimit, compute_budgets
from quotalens.dashboard import Dashboard, _budget_view
from quotalens.render import _budget
from quotalens.runway import SESSION_LENGTH_S
from quotalens.sessions import Delta, SessionWindow

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")

NOW = 1_800_000_000
ROW_HEIGHT = 28  # --row-h, the height every table row in this design holds


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Exception as exc:  # the package is present but the browser is not
            pytest.skip(f"chromium unavailable: {exc}")
        yield launched
        launched.close()


def _window(index: int, peak: float, points: float) -> SessionWindow:
    start = NOW - (index + 1) * SESSION_LENGTH_S
    return SessionWindow(
        start,
        start + SESSION_LENGTH_S,
        False,
        peak,
        peak,
        300,
        start,
        start + SESSION_LENGTH_S,
        {"seven_day": Delta(10.0, 10.0 + points, False)},
        SESSION_LENGTH_S,
    )


def panel_html() -> str:
    """A panel holding all three row states at once: a number, an exact zero, an unknown."""
    history = [
        _window(i, p, c)
        for i, (p, c) in enumerate([(100, 11), (95, 11), (83, 8), (61, 8), (61, 9)])
    ]
    limits = [
        WeeklyLimit("seven_day", "Weekly — all models", 97.0, NOW + 68 * 3600, False),
        WeeklyLimit("limit:fable", "Weekly — Fable", 100.0, NOW + 68 * 3600, True),
        WeeklyLimit("limit:opus", "Weekly — Opus", 20.0, NOW + 68 * 3600, False),
    ]
    dash = object.__new__(Dashboard)
    dash.budget_view = _budget_view(compute_budgets(limits, history, NOW), NOW)
    return _budget(dash)


def document() -> str:
    """The panel under the real stylesheets, which is where the bug lived."""
    css = "".join(
        resources.files("quotalens.web").joinpath(name).read_text()
        for name in ("tokens.css", "app.css")
    )
    return f"<!doctype html><style>{css}</style><body><div class='wrap'>{panel_html()}</div>"


@pytest.fixture(scope="module")
def page(browser):
    context = browser.new_context(color_scheme="dark")
    try:
        p = context.new_page()
        p.set_content(document())
        yield p
    finally:
        context.close()


def test_every_cell_in_a_budget_row_is_one_size(page) -> None:
    sizes = page.evaluate(
        """() => {
            const out = {};
            document.querySelectorAll('.budget tbody td, .budget tbody td span')
                .forEach(el => {
                    const size = getComputedStyle(el).fontSize;
                    (out[size] = out[size] || []).push(el.textContent.trim().slice(0, 24));
                });
            return out;
        }"""
    )
    assert len(sizes) == 1, f"more than one size in the body of the table: {sizes}"
    assert next(iter(sizes)) == "12px"  # --fs-sm, the table's own size


def test_all_three_row_states_are_present_and_the_same_height(page) -> None:
    rows = page.evaluate(
        """() => [...document.querySelectorAll('.budget tbody tr')].map(r => ({
            text: r.textContent.trim(),
            height: Math.round(r.getBoundingClientRect().height),
            cells: r.querySelectorAll('td').length,
        }))"""
    )
    assert len(rows) == 3
    assert [r["height"] for r in rows] == [ROW_HEIGHT] * 3
    assert "none left" in rows[1]["text"]  # the exact zero, with its gloss
    assert "Needs 5 complete session windows" in rows[2]["text"]  # the unknown, with its reason


def test_the_answer_is_marked_by_weight_and_colour_not_by_size(page) -> None:
    """Size was the wrong lever twice. Emphasis that costs no vertical rhythm."""
    measured = page.evaluate(
        """() => {
            const cell = document.querySelector('.budget .bignum');
            const neighbour = document.querySelectorAll('.budget tbody td')[1];
            const gloss = document.querySelector('.budget .bignum .far');
            const read = el => { const c = getComputedStyle(el);
                return {size: c.fontSize, weight: c.fontWeight, colour: c.color,
                        family: c.fontFamily.split(',')[0].trim()}; };
            return {cell: read(cell), neighbour: read(neighbour), gloss: read(gloss)};
        }"""
    )
    cell, neighbour, gloss = measured["cell"], measured["neighbour"], measured["gloss"]

    assert cell["size"] == neighbour["size"] == gloss["size"]
    assert int(cell["weight"]) > int(neighbour["weight"])  # --w-med against the row
    assert gloss["colour"] != cell["colour"]  # the gloss recedes by colour alone
    assert cell["family"] == "ui-monospace"  # DESIGN.md §6: mono is here for the digits
