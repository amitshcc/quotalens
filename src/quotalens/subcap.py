"""Check the "Fable's 100% is half of weekly" model against the data, continuously.

The dashboard asserts that the Fable meter's 100% is half the weekly pool (see
``dashboard.SUBCAP_NOTE``). That is the help centre's rule, not a measurement, so it is
verified on every poll rather than assumed. Two invariants follow from "Fable usage is a
subset of all usage, up to 50% of the pool", and both hold in the current database
(checked by hand: 6/2 ≤ 8, 85/2 ≤ 100, 82/2 ≤ 67):

1. At every reading, ``fable_pct / 2 ≤ all_pct + 1`` — Fable is a subset of all usage.
2. Over any session window, ``Δfable ≤ 2 * Δall + 1`` — Fable is at most half the pool.

The ``+ 1`` absorbs whole-point rounding in the reported percentages. If either ever
fails, the "half of weekly pool" label is wrong for this account, and the budget note
should say the rule is unverified.
"""

from __future__ import annotations

from typing import Any

SUBCAP_KIND = "subcap_violation"
ROUNDING_SLACK = 1.0  # the API reports whole percentage points; a one-point margin is noise
DEDUPE_S = 86400  # once per session window per day: a rare diagnostic must not spam


def reading_ok(all_pct: float, fable_pct: float) -> bool:
    """Invariant 1: Fable usage is a subset of all usage, so at most twice the all figure."""
    return fable_pct / 2 <= all_pct + ROUNDING_SLACK


def window_ok(delta_all: float, delta_fable: float) -> bool:
    """Invariant 2: over a window Fable can consume at most half the pool the total did."""
    return delta_fable <= 2 * delta_all + ROUNDING_SLACK


def reading_violation(all_pct: float | None, fable_pct: float | None) -> str | None:
    """The message for a broken invariant 1, or None when it holds or a reading is missing."""
    if all_pct is None or fable_pct is None or reading_ok(all_pct, fable_pct):
        return None
    return (
        f"Fable at {fable_pct:.0f}% would be over half the weekly pool, but all-models is "
        f"only {all_pct:.0f}%. Fable usage cannot exceed all usage, so the "
        "'half of weekly pool' rule does not hold for this account."
    )


def window_violation(delta_all: float | None, delta_fable: float | None) -> str | None:
    """The message for a broken invariant 2, or None when it holds or a delta is missing."""
    if delta_all is None or delta_fable is None or window_ok(delta_all, delta_fable):
        return None
    return (
        f"Over one session Fable rose {delta_fable:.0f} points while all-models rose "
        f"{delta_all:.0f}; Fable can use at most half the pool, so the "
        "'half of weekly pool' rule does not hold for this account."
    )


def any_recorded(store: Any) -> bool:
    """Has the check ever fired? The budget note reads this to say the rule is unverified."""
    return bool(store.recent_events(limit=1, kind=SUBCAP_KIND))


def latest_detail(store: Any) -> str | None:
    events = store.recent_events(limit=1, kind=SUBCAP_KIND)
    return events[0].detail if events else None


def _should_record(store: Any, window_start: int, now: int) -> bool:
    """Once per session window per day: skip if one was written for this window recently."""
    floor = max(window_start, now - DEDUPE_S)
    return not any(e.ts >= floor for e in store.recent_events(limit=50, kind=SUBCAP_KIND))


def check(
    store: Any,
    all_pct: float | None,
    fable_pct: float | None,
    delta_all: float | None,
    delta_fable: float | None,
    window_start: int,
    now: int,
) -> list[str]:
    """Run both invariants; record one event (deduped) if either fails. Returns the messages."""
    messages = [
        m
        for m in (reading_violation(all_pct, fable_pct), window_violation(delta_all, delta_fable))
        if m is not None
    ]
    if messages and _should_record(store, window_start, now):
        store.record_event(SUBCAP_KIND, " ".join(messages), ts=now)
    return messages
