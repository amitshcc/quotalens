from __future__ import annotations

import pytest

from conftest import OVERAGE_DOCUMENTED, USAGE_DOCUMENTED
from quotawatch.parse import ParseError, QuotaReading, parse_overage, parse_usage


def test_documented_shape_yields_all_windows_and_limits() -> None:
    readings = parse_usage(USAGE_DOCUMENTED)
    by_window = {r.window: r for r in readings}
    assert by_window["five_hour"] == QuotaReading(
        "five_hour", "5-hour", 42.0, "2026-09-02T18:00:00+00:00"
    )
    assert by_window["seven_day"].pct == 17.5
    assert (
        by_window["seven_day_sonnet"].pct == 0 and by_window["seven_day_sonnet"].resets_at is None
    )
    assert by_window["limit:opus"] == QuotaReading(
        "limit:opus", "Opus", 12.0, "2026-09-05T09:00:00+00:00"
    )


def test_unknown_sibling_window_is_picked_up_under_its_key() -> None:
    payload = {"five_hour": {"utilization": 1}, "seven_day_haiku": {"utilization": 3.5}}
    windows = {r.window: r.pct for r in parse_usage(payload)}
    assert windows == {"five_hour": 1.0, "seven_day_haiku": 3.5}


def test_drifted_shape_falls_back_to_tree_walk() -> None:
    drifted = {"data": {"rate_limits": [{"kind": "session", "percent": 66, "reset_at": "x"}]}}
    readings = parse_usage(drifted)
    assert len(readings) == 1
    assert readings[0].window.startswith("unknown:")
    assert readings[0].pct == 66.0
    assert readings[0].resets_at == "x"


def test_unparseable_payload_raises_and_names_keys_only() -> None:
    with pytest.raises(ParseError) as exc:
        parse_usage({"message": "Please log in", "token": "should-not-appear"})
    assert "message" in str(exc.value)
    assert "should-not-appear" not in str(exc.value)


@pytest.mark.parametrize("payload", [None, [], "nope", 3])
def test_non_object_payload_raises(payload: object) -> None:
    with pytest.raises(ParseError):
        parse_usage(payload)


def test_bool_and_nan_are_not_percentages() -> None:
    payload = {"five_hour": {"utilization": True}, "seven_day": {"utilization": float("nan")}}
    with pytest.raises(ParseError):
        parse_usage(payload)


def test_limits_without_scope_get_positional_names() -> None:
    payload = {"limits": [{"percent": 5}, {"percent": 7}]}
    windows = [r.window for r in parse_usage(payload)]
    assert windows == ["limit:limit_0", "limit:limit_1"]


def test_duplicate_windows_are_kept_distinct() -> None:
    payload = {"limits": [{"percent": 5, "name": "Opus"}, {"percent": 9, "name": "Opus"}]}
    windows = [r.window for r in parse_usage(payload)]
    assert windows == ["limit:opus", "limit:opus_2"]


def test_overage_documented() -> None:
    reading = parse_overage(OVERAGE_DOCUMENTED)
    assert reading is not None
    assert (reading.spent_minor, reading.cap_minor, reading.currency) == (1250, 5000, "USD")


@pytest.mark.parametrize("payload", [None, {}, {"used_credits": "12"}, {"used_credits": True}])
def test_overage_missing_returns_none(payload: object) -> None:
    assert parse_overage(payload) is None
