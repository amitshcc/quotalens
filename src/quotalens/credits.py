"""When usage credits are being spent — the moments, not the balance.

A balance is a number you can go and look up. *"You are spending money right
now, and you have been since 00:42"* is the thing only a collector watching every
minute can tell you, and it is the same argument that justifies the whole
project: an agent can burn quota while nobody is looking, and once credits are on
that same agent burns money.

**Measured, never inferred.** ``store.record_overage`` already writes one row per
poll, so the signal is on disk: credits were spent between two polls when
``spent_minor`` rose between them. Do *not* infer spending from a session meter
reading 100% — an exhausted window means the included quota is gone, not that
money is moving. The user may simply have stopped working, or credits may be off.
That inference is the confident wrong number, and this one costs real money to be
wrong about.

**A gap means unknown, not zero.** If ``spent_minor`` rose across a stretch with
no samples, the money was spent *somewhere* in that stretch and not at the
instant we noticed. The span is attributed and marked approximate, which is
DESIGN.md 5's rule applied to time rather than to a value.

**There is no balance in the payload.** Verified against stored samples on
2026-09-06: ``extra_usage`` carries ``used_credits``, ``monthly_limit``,
``utilization``, ``currency``, ``decimal_places``, ``is_enabled``,
``disabled_reason``, ``user_disabled``, ``spend_limit_reached``,
``credits_ever_enabled``, ``daily``, ``weekly`` — and the overage endpoint adds
``monthly_credit_limit``, ``used_credits_basis``, ``out_of_credits``,
``limits_by_period``, ``pooled_*``, ``resolved_group_limit``. No ``balance``, no
``remaining``, no ``prepaid``. So the figure shown is month-to-date against a
monthly cap, and the panel says exactly that rather than implying a balance.
Never synthesise one by subtracting.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

SPEND_KIND = "credits_spend"
# Two polls further apart than this had a collection gap between them, so the
# money moved somewhere inside it rather than at the moment we noticed.
GAP_FACTOR = 2.5
# A stretch ends when spending stops for this long: one quiet poll is a pause
# between requests, not the end of a session on credits.
QUIET_POLLS = 3


@dataclass(frozen=True)
class OverageRow:
    ts: int
    spent_minor: int
    cap_minor: int
    currency: str
    exponent: int


@dataclass(frozen=True)
class Stretch:
    """A run of polls over which ``spent_minor`` rose."""

    start_ts: int  # first moment money could have moved
    end_ts: int  # last poll that saw a rise
    from_minor: int
    to_minor: int
    currency: str
    exponent: int
    approximate: bool = False  # the start is a span, because a gap preceded it

    @property
    def amount_minor(self) -> int:
        return self.to_minor - self.from_minor

    @property
    def seconds(self) -> int:
        return max(0, self.end_ts - self.start_ts)

    def money(self, minor: int | None = None) -> str:
        from quotalens.parse import format_money

        amount = self.amount_minor if minor is None else minor
        return format_money(amount, self.exponent, self.currency)

    def detail(self) -> str:
        """The event line. No timestamp: the event row is written *at* the start.

        The events list prefixes every row with its own time, so repeating it
        here produced "02:08 credits started 6 Sep 02:08".
        """
        about = "started at some point in a collection gap" if self.approximate else "started"
        return f"credits {about} · {self.money()} so far"

    def notification(self, clock) -> str:
        """The banner. Carries the time, because nothing else around it does."""
        about = "about " if self.approximate else ""
        return f"Credits started {about}{clock(self.start_ts)} · {self.money()} so far"


def stretches(rows: list[OverageRow], interval_s: int) -> list[Stretch]:
    """Every run of rising ``spent_minor``, from the stored series.

    Consecutive rises are one stretch; ``QUIET_POLLS`` of no movement ends it. A
    rise whose preceding sample is further back than ``GAP_FACTOR`` intervals is
    marked approximate: the money moved inside that gap, not at its end.
    """
    out: list[Stretch] = []
    open_stretch: dict | None = None
    quiet = 0
    gap_s = max(1, int(interval_s * GAP_FACTOR))

    for previous, row in pairwise(rows):
        rose = row.spent_minor > previous.spent_minor
        if rose:
            quiet = 0
            if open_stretch is None:
                open_stretch = {
                    "start_ts": previous.ts,
                    "from_minor": previous.spent_minor,
                    "approximate": (row.ts - previous.ts) > gap_s,
                }
            open_stretch["end_ts"] = row.ts
            open_stretch["to_minor"] = row.spent_minor
            continue
        if open_stretch is not None:
            quiet += 1
            if quiet >= QUIET_POLLS:
                out.append(_close(open_stretch, row))
                open_stretch = None
    if open_stretch is not None:
        out.append(_close(open_stretch, rows[-1]))
    return out


def _close(state: dict, last: OverageRow) -> Stretch:
    return Stretch(
        start_ts=state["start_ts"],
        end_ts=state["end_ts"],
        from_minor=state["from_minor"],
        to_minor=state["to_minor"],
        currency=last.currency,
        exponent=last.exponent,
        approximate=state["approximate"],
    )


def spent_in_range(rows: list[OverageRow], start: int, end: int) -> int:
    """Minor units spent inside a range, from the first and last rows in it.

    A difference of the endpoints, not a sum of the rises, so a counter that
    resets at the start of a month cannot double-count.
    """
    inside = [r for r in rows if start <= r.ts <= end]
    if len(inside) < 2:
        return 0
    return max(0, inside[-1].spent_minor - inside[0].spent_minor)


def seconds_on_credits(runs: list[Stretch], start: int, end: int) -> int:
    """How much of a range was inside a spending stretch."""
    total = 0
    for run in runs:
        lo, hi = max(run.start_ts, start), min(run.end_ts, end)
        if hi > lo:
            total += hi - lo
    return total


def backfill(rows: list[OverageRow], interval_s: int, recorded: set[int]) -> list[Stretch]:
    """Stretches with no event yet, so existing history lights up.

    Mirrors ``boost.backfill``: the detector is worth nothing to someone who
    already has months of data if it only ever sees the next poll.
    """
    return [s for s in stretches(rows, interval_s) if s.start_ts not in recorded]


def recorded_starts(events: list) -> set[int]:
    """Stretch starts already written to ``event``.

    The event is recorded *at* the stretch's start, so its own ``ts`` is the key
    and nothing has to be parsed back out of prose.
    """
    return {int(e.ts) for e in events}
