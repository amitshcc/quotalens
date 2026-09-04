"""The weekly limit expressed in session windows, and what it refuses to answer.

The exclusions carry as much weight as the arithmetic here: a cost ratio drawn
from a partially observed window, or from one the weekly limit reset inside, is
a wrong number that looks like a right one. Each test that excludes a window also
proves the answer changes when it is included, because an exclusion that makes no
difference to the result is not tested by asserting the result.
"""

from __future__ import annotations

import time

import pytest

from quotalens.budget import (
    MIN_COVERAGE_PCT,
    MIN_SESSION_DELTA_PCT,
    WeeklyLimit,
    compute_budget,
    compute_budgets,
    window_costs,
)
from quotalens.runway import MIN_COMPARE_WINDOWS, SESSION_LENGTH_S
from quotalens.sessions import Delta, SessionWindow

NOW = 1_800_000_000
WEEK = 7 * 86400


def window(
    index: int,
    session_pct: float,
    weekly_pts: float,
    *,
    reset: bool = False,
    covered_s: int | None = None,
    current: bool = False,
    weekly_start: float = 10.0,
    key: str = "seven_day",
) -> SessionWindow:
    """A complete session window that consumed ``session_pct`` and cost ``weekly_pts``."""
    started = NOW - (index + 1) * SESSION_LENGTH_S
    ends = started + SESSION_LENGTH_S
    return SessionWindow(
        started_at=started,
        ends_at=ends,
        is_current=current,
        peak_pct=session_pct,
        final_pct=session_pct,
        samples=300,
        first_ts=started,
        last_ts=ends,
        deltas={key: Delta(weekly_start, weekly_start + weekly_pts, reset)},
        covered_s=SESSION_LENGTH_S if covered_s is None else covered_s,
    )


def clean_history(cost_per_full: float = 10.0, n: int = MIN_COMPARE_WINDOWS) -> list[SessionWindow]:
    """``n`` windows that each cost exactly ``cost_per_full`` points per full window."""
    return [window(i, 100.0, cost_per_full) for i in range(n)]


def limit(pct: float, key: str = "seven_day", subcap: bool = False) -> WeeklyLimit:
    label = "Weekly — Fable" if subcap else "Weekly — all models"
    return WeeklyLimit(key, label, pct, NOW + WEEK, subcap)


# -- the arithmetic ---------------------------------------------------------------


def test_a_known_cost_ratio_becomes_windows_of_budget() -> None:
    """Five windows that each cost 10 points, and 25 points of headroom: 2.5 windows."""
    budget = compute_budget(limit(75.0), clean_history(10.0), NOW)
    assert budget.usable == MIN_COMPARE_WINDOWS
    assert budget.cost_per_full == pytest.approx(10.0)
    assert budget.headroom_pct == pytest.approx(25.0)
    assert budget.full_windows == pytest.approx(2.5)


def test_typical_windows_use_the_median_window_not_a_full_one() -> None:
    """A 50% window costs half as much, so the same headroom buys twice as many."""
    history = [window(i, 50.0, 5.0) for i in range(MIN_COMPARE_WINDOWS)]
    budget = compute_budget(limit(75.0), history, NOW)

    assert budget.typical_peak == pytest.approx(50.0)
    assert budget.cost_per_full == pytest.approx(10.0)  # 5 points for half a window
    assert budget.full_windows == pytest.approx(2.5)
    assert budget.typical_windows == pytest.approx(5.0)


def test_the_median_is_taken_not_the_mean() -> None:
    """One freak window must not move the estimate; that is the whole point of a median."""
    history = [window(i, 100.0, 10.0) for i in range(MIN_COMPARE_WINDOWS)]
    history.append(window(99, 100.0, 500.0))
    budget = compute_budget(limit(90.0), history, NOW)
    assert budget.cost_per_full == pytest.approx(10.0)
    assert budget.cost_high == pytest.approx(500.0)  # but the spread still shows it


def test_the_spread_is_reported_because_the_model_mix_moves_it() -> None:
    history = [window(i, 100.0, cost) for i, cost in enumerate([8.0, 9.0, 11.0, 14.0, 15.0])]
    budget = compute_budget(limit(89.0), history, NOW)
    assert (budget.cost_low, budget.cost_per_full, budget.cost_high) == (8.0, 11.0, 15.0)


def test_clock_windows_count_wall_time_to_the_reset() -> None:
    reset = NOW + 3 * SESSION_LENGTH_S
    budget = compute_budget(
        WeeklyLimit("seven_day", "Weekly", 50.0, reset, False), clean_history(), NOW
    )
    assert budget.clock_windows == pytest.approx(3.0)


# -- what must not count ----------------------------------------------------------


def _excluded_changes_the_answer(bad: SessionWindow, good: SessionWindow) -> None:
    """One window short of the floor, so excluding it turns a number into an em dash.

    Excluding a single window cannot move a median — that is what a median is for,
    and it is tested above. What it does move is how much is known at all, which is
    the difference that matters: a wrong window does not shift the estimate, it
    manufactures one that should not exist.
    """
    short = clean_history(10.0, n=MIN_COMPARE_WINDOWS - 1)

    excluded = compute_budget(limit(75.0), [*short, bad], NOW)
    assert excluded.usable == MIN_COMPARE_WINDOWS - 1
    assert not excluded.known and "complete session windows" in excluded.reason

    counted = compute_budget(limit(75.0), [*short, good], NOW)
    assert counted.usable == MIN_COMPARE_WINDOWS
    assert counted.known and counted.cost_high == pytest.approx(40.0)  # the outlier shows


def test_a_partially_observed_window_is_not_a_measurement() -> None:
    """The row badged "partial, 3% observed" is not a window."""
    _excluded_changes_the_answer(
        window(9, 100.0, 40.0, covered_s=int(SESSION_LENGTH_S * 0.05)),
        window(9, 100.0, 40.0, covered_s=SESSION_LENGTH_S),
    )


def test_a_window_the_weekly_limit_reset_inside_is_not_a_consumption() -> None:
    """This is the shape the contaminated rows produced in the real database."""
    _excluded_changes_the_answer(
        window(9, 100.0, 40.0, reset=True),
        window(9, 100.0, 40.0, reset=False),
    )


def test_a_session_too_small_to_divide_is_noise() -> None:
    tiny = window(9, MIN_SESSION_DELTA_PCT - 1, 4.0)  # a huge ratio from two readings
    history = [*clean_history(10.0), tiny]
    assert len(window_costs(history, "seven_day", NOW)) == MIN_COMPARE_WINDOWS
    assert compute_budget(limit(75.0), history, NOW).cost_per_full == pytest.approx(10.0)


def test_a_saturated_limit_did_not_make_the_window_free() -> None:
    """A weekly limit already at 100% cannot move, so a zero delta is not a zero cost."""
    saturated = window(9, 100.0, 0.0, weekly_start=100.0)
    history = [*clean_history(10.0), saturated]
    assert len(window_costs(history, "seven_day", NOW)) == MIN_COMPARE_WINDOWS
    assert compute_budget(limit(75.0), history, NOW).cost_per_full == pytest.approx(10.0)


def test_the_running_window_has_not_finished_costing_anything() -> None:
    history = [*clean_history(10.0), window(-1, 20.0, 1.0, current=True)]
    assert len(window_costs(history, "seven_day", NOW)) == MIN_COMPARE_WINDOWS


def test_coverage_exactly_at_the_floor_still_counts() -> None:
    """The boundary is inclusive, so a window is not silently dropped at 90.0%."""
    edge = window(9, 100.0, 10.0, covered_s=int(SESSION_LENGTH_S * MIN_COVERAGE_PCT / 100))
    assert len(window_costs([*clean_history(10.0), edge], "seven_day", NOW)) == (
        MIN_COMPARE_WINDOWS + 1
    )


# -- saying nothing rather than something wrong -----------------------------------


def test_below_the_floor_it_says_so_instead_of_guessing() -> None:
    budget = compute_budget(limit(75.0), clean_history(10.0, n=MIN_COMPARE_WINDOWS - 1), NOW)
    assert budget.full_windows is None and not budget.known
    assert str(MIN_COMPARE_WINDOWS) in budget.reason and "complete session windows" in budget.reason
    assert budget.clock_windows is not None  # the clock is known regardless


def test_an_untrusted_weekly_reading_says_so_rather_than_reading_zero() -> None:
    """A stale collector withholds the percentage, and unknown headroom is not no headroom."""
    budget = compute_budget(limit(None), clean_history(), NOW)  # type: ignore[arg-type]
    assert not budget.known and budget.reason == "No trusted weekly reading."
    assert budget.full_windows is None  # not 0.0, which would read as "spent"


def test_a_limit_nothing_has_cost_yet_is_not_an_unlimited_budget() -> None:
    history = [window(i, 100.0, 0.0) for i in range(MIN_COMPARE_WINDOWS)]
    budget = compute_budget(limit(50.0), history, NOW)
    assert not budget.known  # dividing by zero cost would read as an infinite budget
    assert "cost this limit anything" in budget.reason


# -- Fable, and the interaction the meters cannot show ----------------------------


def test_a_spent_sub_cap_is_zero_windows_without_needing_a_cost() -> None:
    """No budget buys no windows however much one costs, and that needs no history."""
    budget = compute_budget(limit(100.0, "limit:fable", subcap=True), [], NOW)
    assert budget.full_windows == 0.0 and budget.typical_windows == 0.0
    assert budget.reason == ""
    assert budget.clock_windows is not None


def test_the_report_says_the_weekly_headroom_cannot_be_spent_on_fable() -> None:
    history = clean_history(10.0)
    report = compute_budgets([limit(93.0), limit(100.0, "limit:fable", subcap=True)], history, NOW)
    all_models, fable = report.budgets
    assert all_models.full_windows == pytest.approx(0.7)
    assert fable.full_windows == 0.0
    assert "Fable" in report.constraint and "7%" in report.constraint


def test_no_constraint_note_while_both_limits_have_room() -> None:
    report = compute_budgets(
        [limit(50.0), limit(50.0, "limit:fable", subcap=True)], clean_history(), NOW
    )
    assert report.constraint == ""


# -- the seam the page and /metrics share -----------------------------------------


def test_the_api_reports_the_same_numbers_as_the_page(settings, store, secrets) -> None:
    from fastapi.testclient import TestClient

    from quotalens.api import create_app
    from quotalens.parse import QuotaReading

    now = int(time.time())
    reset = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now + WEEK))
    for i in range(6):
        store.record_quota(
            now - (5 - i) * 60,
            [
                QuotaReading("five_hour", "5-hour", 30.0, None, "normal", True),
                QuotaReading("seven_day", "7-day", 75.0, reset, "normal", False),
            ],
        )
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    with TestClient(app) as tc:
        payload = tc.get("/api/budget").json()
        dashboard = tc.get("/api/dashboard").json()
        metrics = tc.get("/metrics").text

    assert [b["window"] for b in payload["budgets"]] == ["seven_day"]
    assert payload["budgets"] == dashboard["budget"]["budgets"]
    assert "quotalens_weekly_windows_remaining" in metrics
    assert 'basis="typical"' in metrics
    # Not enough history to cost a window, so the metric is NaN and never a zero.
    assert 'quotalens_weekly_windows_remaining{basis="full",window="seven_day"} NaN' in metrics


# -- how the panel words it -------------------------------------------------------


def _panel(limits: list[WeeklyLimit], history: list[SessionWindow], now: int = NOW) -> str:
    """The rendered panel for a given budget, without going through a whole dashboard."""
    from quotalens.dashboard import Dashboard, _budget_view
    from quotalens.render import _budget

    view = _budget_view(compute_budgets(limits, history, now), now)
    dash = object.__new__(Dashboard)
    dash.budget_view = view
    return _budget(dash)


def _cells(html: str) -> list[str]:
    import re

    return [re.sub(r"<[^>]+>", " ", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", html)]


def test_an_unknown_budget_shows_its_reason_not_an_em_dash() -> None:
    """ "—" cannot be told apart from "the answer is nothing", and those are opposites."""
    html = _panel([limit(75.0)], clean_history(10.0, n=2))
    assert "Needs 5 complete session windows" in html
    assert "3 so far" in html or "2 so far" in html
    assert "—" not in _cells(html), "the label carries an em dash; no cell may be one"


def test_a_spent_limit_reads_none_left() -> None:
    html = _panel([limit(100.0, "limit:fable", subcap=True)], clean_history())
    assert "none left" in html
    assert "Needs" not in html


def test_a_known_budget_shows_sessions_the_typical_peak_and_the_spread() -> None:
    history = [window(i, 80.0, cost) for i, cost in enumerate([8.0, 9.0, 11.0, 14.0, 15.0])]
    html = _panel([limit(75.0)], history)
    cells = _cells(html)

    assert "25%" in cells  # what is left
    assert any("at 80% used" in c for c in cells)  # "typical" is not a mystery
    assert any("14 pts" in c and "10–19, from 5 sessions" in c for c in cells)
    assert 'class="n bignum"' in html  # the answer is set as a readout, not body text


def test_the_panel_says_session_not_window_in_its_own_words() -> None:
    """The unit a person plans in is a session. `reason` is quoted from the API verbatim."""
    html = _panel([limit(75.0)], clean_history(10.0))
    headings = html.split("<tbody>")[0]
    assert "session" in headings.lower() and "window" not in headings.lower()


def test_the_note_names_whichever_of_budget_and_clock_binds() -> None:
    plenty_of_budget = WeeklyLimit("seven_day", "Weekly", 5.0, NOW + 2 * SESSION_LENGTH_S, False)
    html = _panel([plenty_of_budget], clean_history(10.0))
    assert "the clock is what runs out" in html  # 9.5 sessions of budget, 2 of clock

    little_budget = WeeklyLimit("seven_day", "Weekly", 95.0, NOW + 20 * SESSION_LENGTH_S, False)
    html = _panel([little_budget], clean_history(10.0))
    assert "the budget is what runs out" in html  # 0.5 of budget, 20 of clock
    assert "There is time for 20.0 more sessions" in html


def test_the_note_still_gives_the_clock_when_the_budget_is_unknown() -> None:
    html = _panel([limit(75.0)], clean_history(10.0, n=2))
    assert "There is time for" in html and "runs out" not in html


def test_the_sub_cap_constraint_survives_the_rewording() -> None:
    html = _panel([limit(93.0), limit(100.0, "limit:fable", subcap=True)], clean_history(10.0))
    assert "none of the 7% left on Weekly — all models can be used on it" in html
