from __future__ import annotations

from quotawatch.burn import burn_rate, is_reset, split_at_resets
from quotawatch.store import QuotaRow

T0 = 1_000_000
R1 = "2026-09-02T18:00:00+00:00"
R2 = "2026-09-02T23:00:00+00:00"


def row(offset_min: int, pct: float, resets_at: str | None = R1) -> QuotaRow:
    return QuotaRow(T0 + offset_min * 60, "five_hour", "5-hour", pct, resets_at)


def test_steady_climb_gives_positive_rate() -> None:
    rows = [row(m, 10 + m) for m in range(0, 16)]  # +1 point per minute
    result = burn_rate("five_hour", rows, lookback_s=15 * 60, now=T0 + 15 * 60)
    assert result.rate_pct_per_hour == 60.0
    assert result.points == 16
    assert result.reason is None


def test_flat_series_gives_zero() -> None:
    rows = [row(m, 40) for m in range(0, 16)]
    result = burn_rate("five_hour", rows, 15 * 60, T0 + 15 * 60)
    assert result.rate_pct_per_hour == 0.0


def test_reset_in_middle_is_a_discontinuity_not_negative_burn() -> None:
    before = [row(m, 80 + m * 0.5, R1) for m in range(0, 8)]  # climbing to 83.5
    after = [row(m, 2 + (m - 8) * 0.5, R2) for m in range(8, 16)]  # rolled over, climbing again
    rows = before + after
    result = burn_rate("five_hour", rows, 15 * 60, T0 + 15 * 60)
    assert result.rate_pct_per_hour is not None and result.rate_pct_per_hour > 0
    assert result.rate_pct_per_hour == 30.0  # 0.5 per minute inside the new segment
    assert result.segment_start_ts == T0 + 8 * 60
    assert result.points == 8


def test_reset_detected_by_sharp_drop_without_resets_at() -> None:
    rows = [row(m, 90 + m, None) for m in range(0, 5)] + [
        row(m, 1 + (m - 5), None) for m in range(5, 16)
    ]
    result = burn_rate("five_hour", rows, 15 * 60, T0 + 15 * 60)
    assert result.rate_pct_per_hour == 60.0
    assert result.segment_start_ts == T0 + 5 * 60


def test_small_dip_is_not_a_reset() -> None:
    # A one-point wobble (server-side rounding) must not split the series.
    assert not is_reset(row(0, 50.0), row(1, 49.0))
    assert is_reset(row(0, 50.0), row(1, 40.0))
    assert is_reset(row(0, 50.0, R1), row(1, 50.0, R2))
    assert not is_reset(row(0, 50.0, R1), row(1, 50.0, None))


def test_split_at_resets_segments() -> None:
    rows = [row(0, 10), row(1, 20), row(2, 1), row(3, 2), row(4, 3, R2)]
    segments = split_at_resets(rows)
    assert [[r.pct for r in s] for s in segments] == [[10, 20], [1, 2], [3]]


def test_reset_just_now_reports_insufficient_points() -> None:
    rows = [row(m, 50 + m) for m in range(0, 14)] + [row(14, 0, R2)]
    result = burn_rate("five_hour", rows, 15 * 60, T0 + 14 * 60)
    assert result.rate_pct_per_hour is None
    assert "fewer than 2" in (result.reason or "")
    assert result.segment_start_ts == T0 + 14 * 60


def test_lookback_excludes_old_points() -> None:
    rows = [row(m, m) for m in range(0, 61)]  # one hour, +1/min
    result = burn_rate("five_hour", rows, 15 * 60, T0 + 60 * 60)
    assert result.points == 16
    assert result.from_ts == T0 + 45 * 60
    assert result.rate_pct_per_hour == 60.0


def test_empty_and_single_point() -> None:
    assert burn_rate("five_hour", [], 900, T0).reason == "no readings"
    assert burn_rate("five_hour", [row(0, 5)], 900, T0).rate_pct_per_hour is None


def test_too_short_span_rejected() -> None:
    rows = [QuotaRow(T0, "w", "w", 1, None), QuotaRow(T0 + 10, "w", "w", 2, None)]
    assert "span" in (burn_rate("w", rows, 900, T0 + 10).reason or "")
