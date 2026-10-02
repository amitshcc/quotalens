"""A credit grant is a dollar credit: it is in none of the quota-window derivations."""

from __future__ import annotations

import copy

from conftest import USAGE_LIVE_2026_09
from quotalens.dashboard import weekly_limits
from quotalens.parse import parse_usage
from quotalens.store import Store
from quotalens.weeks import WEEK_RESET_KIND, record_live

GRANT = {
    "utilization": 8.51794,
    "resets_at": "2026-11-05T07:59:00+00:00",
    "limit_dollars": 250,
    "used_dollars": 21.29485,
    "remaining_dollars": 228.71,
    "locked_reason": None,
}
NOW = 1_790_000_000


def _poll(store: Store, ts: int, payload: dict) -> tuple[list, object]:
    previous = store.latest_quota()
    parsed = parse_usage(payload)
    store.record_quota(ts, parsed.readings)
    store.record_grants(ts, parsed.grants)
    return previous, parsed


def test_grant_not_in_budget(store: Store) -> None:
    _poll(store, NOW, {**copy.deepcopy(USAGE_LIVE_2026_09), "iguana_necktie": GRANT})
    assert "iguana_necktie" not in store.windows()
    assert [g.key for g in store.latest_grants()] == ["iguana_necktie"]
    keys = [w.key for w in weekly_limits(store.latest_quota())]
    assert "iguana_necktie" not in keys
    assert keys == ["seven_day", "limit:sonnet"]  # the fixture's own weekly windows


def test_grant_expiry_writes_no_week_reset(store: Store) -> None:
    base = copy.deepcopy(USAGE_LIVE_2026_09)
    _poll(store, NOW, {**base, "iguana_necktie": GRANT})
    # the grant's resets_at moves, then the block disappears on expiry
    moved = {**GRANT, "resets_at": "2026-12-05T07:59:00+00:00"}
    for ts, payload in ((NOW + 60, {**base, "iguana_necktie": moved}), (NOW + 120, base)):
        previous, parsed = _poll(store, ts, payload)
        assert record_live(store, previous, parsed.readings, ts, trusted=True) == []
    assert store.recent_events(kind=WEEK_RESET_KIND) == []
