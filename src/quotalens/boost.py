"""A quota boost: the level fell without the window resetting.

Anthropic raised this account's weekly limit mid-window on 5 Sep 2026 and nothing
recorded it. That is the shape of event this project exists to remember — the
number moved for a reason that is not consumption, and by the next day there is
no trace of it in a tool that only shows the current value.

**What a boost looks like in the payload.** Taken from the stored samples either
side of the real transition, not from a guess:

    before  seven_day {"utilization": 98.0, "resets_at": "2026-09-07T00:59:59.875080+00:00"}
    after   seven_day {"utilization":  0.0, "resets_at": "2026-09-07T01:00:00.703741+00:00"}

The two reset times are **0.83 seconds** apart: the ordinary sub-second jitter
this server puts on every response, far inside :data:`RESET_TIME_TOLERANCE_S`. The
window did not reset. It is still the same Monday-01:00 window; the count inside
it went away.

**There is no ceiling in the payload.** A window block carries `utilization`,
`resets_at`, and `limit_dollars` / `used_dollars` / `remaining_dollars` which are
all null on this plan; a `limits[]` entry carries `percent`, `severity`, `scope`,
`is_active`. Nowhere is the denominator exposed. So a boost is visible only as
*the number falling*, and its **size cannot be recovered**: an observed fall is
consumption plus boost, and nothing in the response separates them. That is why
:mod:`quotalens.budget` excludes a boosted window from its cost estimate instead
of trying to subtract one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from quotalens.burn import resets_at_changed
from quotalens.parse import QuotaReading
from quotalens.store import QuotaRow

BOOST_KIND = "quota_boost"

# A fall smaller than this is the server revising its own number, not a boost. The
# API reports whole percentage points, and in ~7,000 stored readings the only
# non-reset fall that was not the boost was a single point: five_hour going 42 to
# 41 inside one window. Five points is the same floor `burn.RESET_DROP_PCT` uses
# for "a drop this large is structural", so the codebase has one answer, not two.
BOOST_MIN_DROP_PCT = 5.0


@dataclass(frozen=True)
class Boost:
    """One window's level falling while its window kept running."""

    window: str
    label: str
    from_pct: float
    to_pct: float
    ts: int  # when the lower reading arrived, which is the first moment we could know

    @property
    def drop(self) -> float:
        return self.from_pct - self.to_pct

    def detail(self) -> str:
        """The event text. Says what was observed, then the only reading of it."""
        return (
            f"{self.label} fell {self.from_pct:.0f}% -> {self.to_pct:.0f}% with no reset. "
            "Limit raised."
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "window": self.window,
            "label": self.label,
            "from_pct": self.from_pct,
            "to_pct": self.to_pct,
            "ts": self.ts,
        }


def detect_boost(
    previous: QuotaRow | None, current: QuotaReading, ts: int, trusted: bool = True
) -> Boost | None:
    """A boost, or ``None`` and the reason it is not one.

    Everything else that can make a number fall is refused here rather than
    downstream, so every consumer inherits one conclusion:

    * a genuine reset — ``resets_at`` moved, which the session model already
      handles and which is not a boost;
    * an undated block on either side — without a reset time there is no way to
      say the window did *not* reset, so no claim is made;
    * an unverified reading — recovered by the generic tree walk, which the design
      already refuses to trust; a claim must not be built on one;
    * a fall inside the noise floor — see :data:`BOOST_MIN_DROP_PCT`.

    A gap in collection is deliberately *not* disqualifying: the real boost was
    observed across six and a half hours of downtime, and refusing to look across a
    gap would have missed the only one that has ever happened.
    """
    if not trusted or previous is None or current.pct is None:
        return None
    if previous.resets_at is None or current.resets_at is None:
        return None
    if resets_at_changed(previous.resets_at, current.resets_at):
        return None
    if previous.pct - current.pct < BOOST_MIN_DROP_PCT:
        return None
    return Boost(
        current.window,
        current.label or current.window,
        previous.pct,
        current.pct,
        ts,
    )


def detect_boosts(
    previous: Iterable[QuotaRow], readings: Iterable[QuotaReading], ts: int, trusted: bool = True
) -> list[Boost]:
    """Every window that was boosted between the last stored reading and this one."""
    by_window = {row.window: row for row in previous}
    found = []
    for reading in readings:
        boost = detect_boost(by_window.get(reading.window), reading, ts, trusted)
        if boost is not None:
            found.append(boost)
    return found


def boosted_windows(boost_ts: Sequence[int], started_at: int, ends_at: int) -> bool:
    """Did a boost land inside this session window?

    The window's own weekly delta then understates what was consumed, because part
    of the climb was cancelled by the raise.
    """
    return any(started_at <= ts <= ends_at for ts in boost_ts)
