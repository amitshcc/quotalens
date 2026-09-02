from __future__ import annotations

import copy

import pytest

from conftest import OVERAGE_DOCUMENTED, USAGE_DOCUMENTED, USAGE_LIVE_2026_09
from quotawatch.parse import (
    ParseError,
    QuotaReading,
    format_money,
    overage_pct,
    parse_spend,
    parse_usage,
)


def windows(payload: object) -> dict[str, QuotaReading]:
    return {r.window: r for r in parse_usage(payload).readings}


# -- documented and live shapes -------------------------------------------------


def test_documented_shape_yields_windows_and_scoped_limit() -> None:
    parsed = parse_usage(USAGE_DOCUMENTED)
    by_window = {r.window: r for r in parsed.readings}
    assert by_window["five_hour"] == QuotaReading(
        "five_hour", "5-hour", 42.0, "2026-09-02T18:00:00+00:00"
    )
    assert by_window["seven_day"].pct == 17.5
    assert by_window["limit:opus"] == QuotaReading(
        "limit:opus", "Opus", 12.0, "2026-09-05T09:00:00+00:00"
    )
    # utilization 0 with a null resets_at is not a window
    assert "seven_day_sonnet" not in by_window
    assert [(b.key, b.reason) for b in parsed.ignored] == [("seven_day_sonnet", "no resets_at")]


def test_live_shape_is_three_windows_not_six() -> None:
    parsed = parse_usage(USAGE_LIVE_2026_09)
    by_window = {r.window: r for r in parsed.readings}
    assert set(by_window) == {"five_hour", "seven_day", "limit:sonnet"}
    assert by_window["five_hour"].pct == 71
    assert by_window["five_hour"].resets_at == "2026-09-02T12:40:00.421772+00:00"
    assert by_window["seven_day"].pct == 38
    assert by_window["limit:sonnet"] == QuotaReading(
        "limit:sonnet", "Sonnet", 66.0, "2026-09-07T01:00:00.422121+00:00", "normal", False
    )
    assert not parsed.fallback_used


def test_scope_null_limits_fold_severity_and_is_active_into_top_level() -> None:
    payload = copy.deepcopy(USAGE_LIVE_2026_09)
    payload["limits"][0]["severity"] = "warning"
    payload["limits"][1]["severity"] = "critical"
    by_window = windows(payload)
    assert by_window["five_hour"].severity == "warning"
    assert by_window["five_hour"].is_active is True
    assert by_window["seven_day"].severity == "critical"
    assert by_window["seven_day"].is_active is False
    assert "limit:session" not in by_window and "limit:weekly_all" not in by_window


def test_scope_null_limit_with_unknown_kind_matches_by_value_else_kept() -> None:
    payload = {
        "five_hour": {"utilization": 20, "resets_at": "r1"},
        "limits": [
            {
                "kind": "mystery",
                "percent": 20,
                "resets_at": "r1",
                "scope": None,
                "severity": "warning",
            },
            {"kind": "other", "percent": 55, "resets_at": "r9", "scope": None},
        ],
    }
    by_window = windows(payload)
    assert by_window["five_hour"].severity == "warning"  # matched on (pct, resets_at)
    assert by_window["limit:other"].pct == 55  # unmatched: kept rather than lost


def test_scoped_limit_uses_display_name_not_null_id() -> None:
    payload = {
        "five_hour": {"utilization": 1, "resets_at": "r"},
        "limits": [
            {
                "percent": 9,
                "resets_at": "r",
                "scope": {"model": {"display_name": "Fable", "id": None}},
            }
        ],
    }
    assert windows(payload)["limit:fable"].label == "Fable"


def test_novel_codename_block_without_resets_at_is_ignored_not_charted() -> None:
    payload = copy.deepcopy(USAGE_LIVE_2026_09)
    payload["zebra_lantern"] = {"utilization": 0.0, "resets_at": None}
    payload["quartz_meadow"] = None
    parsed = parse_usage(payload)
    keys = {r.window for r in parsed.readings}
    assert "zebra_lantern" not in keys and "nimbus_quill" not in keys
    assert {b.key for b in parsed.ignored} == {"nimbus_quill", "zebra_lantern"}
    assert all(b.reason == "no resets_at" for b in parsed.ignored)


def test_novel_block_with_resets_at_is_a_window() -> None:
    payload = {
        "five_hour": {"utilization": 1, "resets_at": "r"},
        "opal_ridge": {"utilization": 9, "resets_at": "r2"},
    }
    assert windows(payload)["opal_ridge"].label == "opal ridge"


def test_severity_is_normalised_and_absent_is_none() -> None:
    payload = {
        "five_hour": {"utilization": 1, "resets_at": "r", "severity": "WARNING"},
        "seven_day": {"utilization": 1, "resets_at": "r", "severity": "bogus"},
        "limits": [
            {
                "kind": "session",
                "percent": 1,
                "resets_at": "r",
                "scope": None,
                "severity": "Critical",
            }
        ],
    }
    by_window = windows(payload)
    assert by_window["five_hour"].severity == "critical"  # limits entry wins
    assert by_window["seven_day"].severity is None


def test_dollar_denominated_variant_is_tolerated() -> None:
    payload = copy.deepcopy(USAGE_LIVE_2026_09)
    payload["five_hour"].update(
        {
            "limit_dollars": 50.0,
            "used_dollars": 12.5,
            "remaining_dollars": 37.5,
            "locked_reason": "x",
        }
    )
    assert windows(payload)["five_hour"].pct == 71


# -- drift and failure ----------------------------------------------------------


def test_drifted_shape_falls_back_to_tree_walk() -> None:
    drifted = {"data": {"rate_limits": [{"kind": "session", "percent": 66, "reset_at": "x"}]}}
    parsed = parse_usage(drifted)
    assert parsed.fallback_used
    assert len(parsed.readings) == 1
    assert parsed.readings[0].window.startswith("unknown:")
    assert parsed.readings[0].pct == 66.0
    assert parsed.readings[0].resets_at == "x"


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
    payload = {
        "five_hour": {"utilization": True, "resets_at": "r"},
        "seven_day": {"utilization": float("nan"), "resets_at": "r"},
    }
    with pytest.raises(ParseError):
        parse_usage(payload)


def test_duplicate_scoped_windows_are_kept_distinct() -> None:
    payload = {
        "five_hour": {"utilization": 1, "resets_at": "r"},
        "limits": [
            {"percent": 5, "resets_at": "r", "scope": {"model": {"display_name": "Opus"}}},
            {"percent": 9, "resets_at": "r2", "scope": {"model": {"display_name": "Opus"}}},
        ],
    }
    assert set(windows(payload)) == {"five_hour", "limit:opus", "limit:opus_2"}


# -- spend ----------------------------------------------------------------------


def test_money_conversion_renders_316_as_3_16_and_200_as_2_00() -> None:
    assert format_money(316, 2, "USD") == "$3.16"
    assert format_money(200, 2, "USD") == "$2.00"
    assert format_money(316, 0, "USD") == "$316"
    assert format_money(123456, 2, "EUR") == "€1,234.56"
    assert format_money(5, 2, "JPY") == "0.05 JPY"
    with pytest.raises(ValueError):
        format_money(1, 9, "USD")


def test_overage_pct_is_unclamped() -> None:
    assert overage_pct(316, 200) == 158.0
    assert overage_pct(0, 200) == 0.0
    assert overage_pct(5, 0) is None
    assert overage_pct(5, None) is None


def test_spend_prefers_spend_block_with_explicit_exponent() -> None:
    spend = parse_spend(USAGE_LIVE_2026_09)
    assert spend is not None
    assert spend.source == "spend"
    assert (spend.used_text, spend.limit_text, spend.pct) == ("$3.16", "$2.00", 158.0)
    assert spend.is_enabled is False
    assert spend.disabled_reason == "org_level_disabled_until"
    assert spend.spend_limit_reached is True
    assert spend.conflict is False


def test_spend_falls_back_to_extra_usage_with_decimal_places() -> None:
    payload = {
        "extra_usage": {
            "used_credits": 316,
            "monthly_limit": 200,
            "currency": "USD",
            "decimal_places": 2,
            "is_enabled": True,
        }
    }
    spend = parse_spend(payload)
    assert spend is not None
    assert spend.source == "extra_usage"
    assert (spend.used_text, spend.limit_text, spend.pct) == ("$3.16", "$2.00", 158.0)


def test_spend_sources_disagree_suppresses_money_keeps_pct() -> None:
    payload = copy.deepcopy(USAGE_LIVE_2026_09)
    payload["extra_usage"]["used_credits"] = 31600
    spend = parse_spend(payload)
    assert spend is not None
    assert spend.conflict is True
    assert spend.used_text is None and spend.limit_text is None
    assert spend.pct == 158.0


def test_spend_disabled_until_comes_from_overage_endpoint() -> None:
    spend = parse_spend(USAGE_LIVE_2026_09, {"disabled_until": "2026-10-01T00:00:00+00:00"})
    assert spend is not None and spend.disabled_until == "2026-10-01T00:00:00+00:00"


def test_spend_last_resort_is_overage_endpoint() -> None:
    spend = parse_spend({"five_hour": {"utilization": 1, "resets_at": "r"}}, OVERAGE_DOCUMENTED)
    assert spend is not None
    assert spend.source == "overage_endpoint"
    assert (spend.used_text, spend.limit_text, spend.pct) == ("$12.50", "$50.00", 25.0)


@pytest.mark.parametrize(
    "payload", [None, {}, {"extra_usage": {"used_credits": "12"}}, {"spend": {"used": {}}}]
)
def test_spend_missing_returns_none(payload: object) -> None:
    assert parse_spend(payload) is None
