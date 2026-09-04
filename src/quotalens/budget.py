"""The weekly limit, expressed in five-hour session windows.

"Weekly is at 93%" is a fact you cannot act on. "Half a window of budget left,
and thirteen windows of clock before it resets" is the same fact in the unit the
work actually comes in, and it says the thing that matters: you are rationing,
not racing.

The cost of a window is measured, not assumed. Every complete session window in
the history carries both its own consumption and what it cost each weekly limit,
so the ratio of the two is an observation about how *this* account uses models,
and the median across windows is the estimate. A constant would be a guess about
someone else's model mix.

Pure functions over session windows and the current readings, in the shape of
:mod:`runway`. What it refuses to answer matters as much as what it answers:
below :data:`MIN_COMPARE_WINDOWS` usable windows it returns nothing and says why,
because a confident "3.2 windows left" drawn from two observations is worse than
an em dash — someone will plan a week around it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median

from quotalens.runway import MIN_COMPARE_WINDOWS, SESSION_LENGTH_S
from quotalens.sessions import SessionWindow, coverage_pct

# Why a window can fail to be a measurement of cost.
MIN_COVERAGE_PCT = 90.0  # "partial, 3% observed" is not an observation of a whole window
MIN_SESSION_DELTA_PCT = 10.0  # below this the ratio is mostly the noise in two readings
SATURATED_PCT = 100.0  # a limit already at its cap cannot move, so zero is not a cost


@dataclass(frozen=True)
class WindowCost:
    """One complete session window, and what it cost one weekly limit."""

    started_at: int
    session_pct: float  # how much of the five-hour window was consumed
    weekly_pts: float  # what that cost the weekly limit
    ratio: float  # weekly points per point of session

    @property
    def per_full_window(self) -> float:
        """Weekly points a session window run to 100% would cost at this ratio."""
        return self.ratio * 100.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "started_at": self.started_at,
            "session_pct": self.session_pct,
            "weekly_pts": round(self.weekly_pts, 2),
            "cost_per_full_window": round(self.per_full_window, 2),
        }


@dataclass(frozen=True)
class WeeklyLimit:
    """A weekly meter as the budget needs it: where it stands and when it resets."""

    key: str
    label: str
    pct: float | None
    reset_ts: int | None
    subcap: bool = False  # measures a slice of another limit (Fable is half the weekly pool)


@dataclass(frozen=True)
class Budget:
    key: str
    label: str
    subcap: bool
    headroom_pct: float | None
    cost_per_full: float | None  # median weekly points for a window run to 100%
    cost_low: float | None  # the spread, because the model mix moves it
    cost_high: float | None
    typical_peak: float | None  # the median window, from runway.median_peak's rule
    full_windows: float | None  # headroom / cost of a full window
    typical_windows: float | None  # headroom / cost of a typical window
    clock_windows: float | None  # five-hour windows of wall clock until the reset
    reset_ts: int | None
    usable: int  # complete windows the estimate rests on
    reason: str  # why there is no number, or "" when there is one

    @property
    def known(self) -> bool:
        return self.full_windows is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "window": self.key,
            "label": self.label,
            "subcap": self.subcap,
            "headroom_pct": self.headroom_pct,
            "cost_per_full_window": _round(self.cost_per_full),
            "cost_per_full_window_low": _round(self.cost_low),
            "cost_per_full_window_high": _round(self.cost_high),
            "typical_peak_pct": _round(self.typical_peak),
            "full_windows_remaining": _round(self.full_windows),
            "typical_windows_remaining": _round(self.typical_windows),
            "clock_windows_remaining": _round(self.clock_windows),
            "reset_ts": self.reset_ts,
            "usable_windows": self.usable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BudgetReport:
    budgets: list[Budget]
    constraint: str  # how the limits interact, when that changes what you can do

    def as_dict(self) -> dict[str, object]:
        return {
            "budgets": [b.as_dict() for b in self.budgets],
            "constraint": self.constraint,
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def is_complete(window: SessionWindow, now: int) -> bool:
    """A window still running has not finished costing anything yet."""
    return not window.is_current and window.ends_at <= now


def window_costs(windows: list[SessionWindow], key: str, now: int) -> list[WindowCost]:
    """Every complete session window that is a fair measurement of ``key``'s cost.

    Four exclusions, each for a different reason to distrust the pair of numbers:

    * still running — the cost is not final;
    * partially observed — the badge says "3% observed"; that is not a window;
    * the weekly limit reset inside it — the delta is not a consumption at all;
    * too small to divide — a two-point session makes the ratio mostly noise;
    * the weekly limit was already at its cap — it could not move, so zero is not
      evidence that the window was free.
    """
    out: list[WindowCost] = []
    for window in windows:
        delta = window.deltas.get(key)
        if delta is None or not is_complete(window, now):
            continue
        if delta.reset:
            continue
        elapsed = max(0, min(window.ends_at, now) - window.started_at)
        if coverage_pct(window.covered_s, elapsed) < MIN_COVERAGE_PCT:
            continue
        session_pct = window.peak_pct
        if session_pct < MIN_SESSION_DELTA_PCT:
            continue
        if delta.start >= SATURATED_PCT:
            continue
        weekly_pts = max(0.0, delta.end - delta.start)
        out.append(WindowCost(window.started_at, session_pct, weekly_pts, weekly_pts / session_pct))
    return out


def compute_budget(limit: WeeklyLimit, windows: list[SessionWindow], now: int) -> Budget:
    """How many more session windows ``limit`` will pay for, and how long there is to spend them."""
    headroom = None if limit.pct is None else max(0.0, 100.0 - limit.pct)
    clock = None
    if limit.reset_ts is not None and limit.reset_ts > now:
        clock = (limit.reset_ts - now) / SESSION_LENGTH_S

    costs = window_costs(windows, limit.key, now)
    empty = Budget(
        limit.key,
        limit.label,
        limit.subcap,
        headroom,
        None,
        None,
        None,
        None,
        None,
        None,
        clock,
        limit.reset_ts,
        len(costs),
        "",
    )
    if headroom is None:
        # Either nothing has been read, or the collector is stale and the readings
        # are withheld. Both mean the headroom is unknown, and neither is "zero".
        return replace(empty, reason="No trusted weekly reading.")
    if headroom == 0.0:
        # Nothing to divide, and nothing to estimate: no budget buys no windows
        # however much one costs. This is the Fable answer, and it is exact.
        return replace(empty, full_windows=0.0, typical_windows=0.0)
    if len(costs) < MIN_COMPARE_WINDOWS:
        return replace(
            empty,
            reason=(
                f"Needs {MIN_COMPARE_WINDOWS} complete session windows to estimate the cost of "
                f"one; {len(costs)} so far."
            ),
        )

    per_full = [c.per_full_window for c in costs]
    cost = float(median(per_full))
    peaks = [c.session_pct for c in costs]
    typical_peak = float(median(peaks))
    if cost <= 0:
        # Every usable window cost this limit nothing. True, and not a rate you can
        # divide by: it would read as an unlimited budget.
        return replace(
            empty,
            cost_low=min(per_full),
            cost_high=max(per_full),
            reason="No session window has cost this limit anything yet.",
        )
    typical_cost = cost * typical_peak / 100.0
    return Budget(
        limit.key,
        limit.label,
        limit.subcap,
        headroom,
        cost,
        min(per_full),
        max(per_full),
        typical_peak,
        headroom / cost,
        headroom / typical_cost if typical_cost > 0 else None,
        clock,
        limit.reset_ts,
        len(costs),
        "",
    )


def constraint_note(budgets: list[Budget]) -> str:
    """What the meters cannot say on their own: that one limit forbids spending another.

    Fable is half the weekly pool. When Fable is spent and the weekly pool is not,
    the remaining pool is real but cannot be spent on Fable models — and two
    meters that each look fine on their own will not tell you that.
    """
    spent = [b for b in budgets if b.subcap and b.headroom_pct == 0.0]
    parents = [b for b in budgets if not b.subcap and (b.headroom_pct or 0.0) > 0.0]
    if not spent or not parents:
        return ""
    names = " and ".join(b.label for b in spent)
    parent = parents[0]
    return (
        f"{names} is spent, so none of the {parent.headroom_pct:.0f}% left on "
        f"{parent.label} can be used on it."
    )


def compute_budgets(
    limits: list[WeeklyLimit], windows: list[SessionWindow], now: int
) -> BudgetReport:
    budgets = [compute_budget(limit, windows, now) for limit in limits]
    return BudgetReport(budgets, constraint_note(budgets))
