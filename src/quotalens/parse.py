"""Turn raw claude.ai payloads into quota readings, defensively.

Written against the real ``/usage`` shape observed 2026-09-02. Rules:

* A top-level block is a quota window only if it carries a percentage **and** a
  ``resets_at``. Blocks with a percentage but no reset time (feature codenames
  such as ``nimbus_quill``) are reported as ignored, never charted.
* The ``limits`` array re-presents the top-level windows. Entries with a null
  ``scope`` are duplicates: their ``severity`` and ``is_active`` are folded into
  the matching window and the entry is dropped. Only model-scoped entries add a
  window of their own, keyed ``limit:<slug>`` from ``scope.model.display_name``.
* Spend comes from the ``spend`` object with its explicit ``exponent``; the
  ``extra_usage`` block is a fallback. The percentage is computed here,
  unclamped, because the API clamps its own at 100.
* If none of that matches, a generic tree walk keeps *something* on screen, and
  total failure raises :class:`ParseError` so nobody stores a false zero.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any

PCT_KEYS = ("utilization", "percent", "pct", "percentage", "usage_percent")
RESET_KEYS = ("resets_at", "reset_at", "resetsAt", "reset_time")
MAX_WALK_DEPTH = 6
SEVERITIES = frozenset({"normal", "warning", "critical"})

# Top-level objects that carry a ``utilization`` but are not quota windows.
NOT_A_WINDOW = frozenset({"extra_usage", "spend"})
# Scope-less ``limits`` entries that mirror a top-level window, by ``kind``.
LIMIT_KIND_TO_WINDOW = {"session": "five_hour", "weekly_all": "seven_day"}
RATE_WINDOW = "five_hour"  # the one window whose "not running" state is normal

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


@dataclass(frozen=True)
class QuotaReading:
    window: str  # stable machine key, e.g. "five_hour", "limit:fable"
    label: str  # human label, e.g. "5-hour", "Fable"
    pct: float  # percentage points consumed; may exceed 100
    resets_at: str | None  # ISO-8601 as given by the server
    severity: str | None = None  # the API's own: normal | warning | critical
    is_active: bool | None = None  # the currently binding limit, per the API


@dataclass(frozen=True)
class IgnoredBlock:
    key: str
    reason: str


@dataclass(frozen=True)
class UsageParse:
    readings: list[QuotaReading]
    ignored: list[IgnoredBlock] = field(default_factory=list)
    fallback_used: bool = False  # readings came from the generic tree walk


@dataclass(frozen=True)
class SpendReading:
    """Extra-usage spend. Amounts are minor units; ``exponent`` converts to major."""

    used_minor: int
    limit_minor: int | None
    exponent: int
    currency: str
    source: str  # spend | extra_usage | overage_endpoint
    is_enabled: bool | None = None
    disabled_reason: str | None = None
    disabled_until: str | None = None
    spend_limit_reached: bool | None = None
    conflict: bool = False  # the two payload sources disagree: hide absolute figures

    @property
    def pct(self) -> float | None:
        return overage_pct(self.used_minor, self.limit_minor)

    @property
    def used_text(self) -> str | None:
        if self.conflict:
            return None
        return format_money(self.used_minor, self.exponent, self.currency)

    @property
    def limit_text(self) -> str | None:
        if self.conflict or self.limit_minor is None:
            return None
        return format_money(self.limit_minor, self.exponent, self.currency)


class ParseError(ValueError):
    """The payload had no recognizable quota data. Message names keys only, never values."""


_WINDOW_LABELS = {
    "five_hour": "Session",
    "seven_day": "Weekly — all models",
    "seven_day_sonnet": "Weekly — Sonnet",
    "seven_day_opus": "Weekly — Opus",
    "seven_day_oauth_apps": "Weekly — OAuth apps",
}


# -- small helpers --------------------------------------------------------------


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_") or "unknown"


def humanize(key: str) -> str:
    return _WINDOW_LABELS.get(key, key.replace("_", " "))


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int | None:
    number = _as_number(value)
    return round(number) if number is not None else None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _reset_of(obj: dict[str, Any]) -> str | None:
    for key in RESET_KEYS:
        if (value := _as_str(obj.get(key))) is not None:
            return value
    return None


def _pct_of(obj: dict[str, Any]) -> float | None:
    for key in PCT_KEYS:
        if key in obj and (pct := _as_number(obj[key])) is not None:
            return pct
    return None


def _severity_of(obj: dict[str, Any]) -> str | None:
    raw = _as_str(obj.get("severity"))
    return raw.lower() if raw and raw.lower() in SEVERITIES else None


# -- usage ----------------------------------------------------------------------


def _scope_name(entry: dict[str, Any]) -> str | None:
    """Display name of a model-scoped limit; None when the entry has no real scope."""
    scope = entry.get("scope")
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    if isinstance(model, dict):
        for key in ("display_name", "name", "id"):
            if (name := _as_str(model.get(key))) is not None:
                return name
    for key in ("display_name", "name", "surface"):
        if (name := _as_str(scope.get(key))) is not None:
            return name
    return None


def _top_level_windows(payload: dict[str, Any]) -> tuple[list[QuotaReading], list[IgnoredBlock]]:
    readings: list[QuotaReading] = []
    ignored: list[IgnoredBlock] = []
    for key, value in payload.items():
        if key == "limits" or key in NOT_A_WINDOW or not isinstance(value, dict):
            continue
        pct = _pct_of(value)
        if pct is None:
            continue
        reset = _reset_of(value)
        if reset is None and key != RATE_WINDOW:
            # A block we cannot date is not a window. For everything but the session
            # this stays diagnostics-only: an undated weekly block is not a state the
            # endpoint is documented to have, and inventing a meter from one would
            # put a model tier on the page that the account may not even use.
            ignored.append(IgnoredBlock(key, "no resets_at"))
            continue
        if reset is None:
            # The session window is the exception, because "not running" is its
            # normal resting state and the product leads with it. Dropping the value
            # is what let a closed window's reading sit on the page as though it were
            # live: the server said `five_hour: 0.0` for twenty-five consecutive
            # polls while the meter went on showing 40%. Stored undated, so
            # `sessions.py` still reads it as no window running.
            ignored.append(IgnoredBlock(key, "no resets_at, value kept"))
        readings.append(QuotaReading(key, humanize(key), pct, reset, _severity_of(value)))
    return readings, ignored


def _fold_limits(
    payload: dict[str, Any], readings: list[QuotaReading], ignored: list[IgnoredBlock]
) -> list[QuotaReading]:
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return readings
    by_window = {r.window: r for r in readings}
    for index, entry in enumerate(limits):
        if not isinstance(entry, dict):
            continue
        pct = _pct_of(entry)
        if pct is None:
            continue
        reset = _reset_of(entry)
        severity = _severity_of(entry)
        is_active = _as_bool(entry.get("is_active"))
        kind = _as_str(entry.get("kind"))
        scope_name = _scope_name(entry)

        if scope_name is None:
            target = _duplicate_of(kind, pct, reset, by_window)
            if target is not None:
                by_window[target] = replace(
                    by_window[target], severity=severity, is_active=is_active
                )
                continue
            name = kind or _as_str(entry.get("group")) or f"limit {index}"
        else:
            name = scope_name

        if reset is None:
            ignored.append(IgnoredBlock(f"limits[{index}]", "no resets_at"))
            continue
        window = f"limit:{slugify(name)}"
        key, suffix = window, 2
        while key in by_window:  # two scoped limits sharing a display name stay distinct
            key = f"{window}_{suffix}"
            suffix += 1
        by_window[key] = QuotaReading(key, name, pct, reset, severity, is_active)
    return list(by_window.values())


def _duplicate_of(
    kind: str | None, pct: float, reset: str | None, by_window: dict[str, QuotaReading]
) -> str | None:
    """Which top-level window a scope-less limits entry re-presents, if any."""
    mapped = LIMIT_KIND_TO_WINDOW.get(kind or "")
    if mapped in by_window:
        return mapped
    for window, reading in by_window.items():
        if reading.pct == pct and reading.resets_at == reset and not window.startswith("limit:"):
            return window
    return None


def _walk(node: Any, path: tuple[str, ...], out: list[QuotaReading], depth: int) -> None:
    if depth > MAX_WALK_DEPTH:
        return
    if isinstance(node, dict):
        pct = _pct_of(node)
        if pct is not None and path:
            key = "/".join(path)
            out.append(QuotaReading(f"unknown:{slugify(key)}", key, pct, _reset_of(node)))
            return
        for key, value in node.items():
            if key in NOT_A_WINDOW:
                continue
            _walk(value, (*path, str(key)), out, depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, (*path, str(index)), out, depth + 1)


def parse_usage(payload: Any) -> UsageParse:
    """Parse a ``/usage`` payload. Raises :class:`ParseError` if nothing usable is found."""
    if not isinstance(payload, dict):
        raise ParseError(f"usage payload is {type(payload).__name__}, expected object")
    readings, ignored = _top_level_windows(payload)
    readings = _fold_limits(payload, readings, ignored)
    if readings:
        return UsageParse(_dedupe(readings), ignored)
    fallback: list[QuotaReading] = []
    _walk(payload, (), fallback, 0)
    if fallback:
        return UsageParse(_dedupe(fallback), ignored, fallback_used=True)
    keys = sorted(str(k) for k in payload)[:20]
    raise ParseError(f"usage payload had no recognizable quota fields; top-level keys: {keys}")


def _dedupe(readings: list[QuotaReading]) -> list[QuotaReading]:
    seen: dict[str, QuotaReading] = {}
    for reading in readings:
        window = reading.window
        suffix = 2
        while window in seen and seen[window] != reading:
            window = f"{reading.window}_{suffix}"
            suffix += 1
        seen[window] = replace(reading, window=window)
    return list(seen.values())


# -- spend ----------------------------------------------------------------------


def format_money(amount_minor: int, exponent: int, currency: str) -> str:
    """``316`` with exponent ``2`` in USD renders as ``$3.16``. The one conversion."""
    if exponent < 0 or exponent > 6:
        raise ValueError(f"implausible currency exponent {exponent}")
    major = amount_minor / (10**exponent)
    text = f"{major:,.{exponent}f}"
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    return f"{symbol}{text}" if symbol else f"{text} {currency.upper()}"


def overage_pct(used_minor: int, limit_minor: int | None) -> float | None:
    """Used over limit, unclamped, so an over-limit account reads honestly (158%)."""
    if limit_minor is None or limit_minor <= 0:
        return None
    return round(used_minor / limit_minor * 100, 1)


def _spend_from_spend_block(spend: dict[str, Any]) -> SpendReading | None:
    used = spend.get("used")
    if not isinstance(used, dict) or (used_minor := _as_int(used.get("amount_minor"))) is None:
        return None
    limit = spend.get("limit")
    limit_minor = _as_int(limit.get("amount_minor")) if isinstance(limit, dict) else None
    exponent = _as_int(used.get("exponent"))
    if exponent is None and isinstance(limit, dict):
        exponent = _as_int(limit.get("exponent"))
    currency = _as_str(used.get("currency")) or (
        _as_str(limit.get("currency")) if isinstance(limit, dict) else None
    )
    return SpendReading(
        used_minor=used_minor,
        limit_minor=limit_minor,
        exponent=2 if exponent is None else exponent,
        currency=currency or "USD",
        source="spend",
        is_enabled=_as_bool(spend.get("enabled")),
        disabled_reason=_as_str(spend.get("disabled_reason")),
    )


def _spend_from_extra_usage(extra: dict[str, Any]) -> SpendReading | None:
    if (used_minor := _as_int(extra.get("used_credits"))) is None:
        return None
    exponent = _as_int(extra.get("decimal_places"))
    return SpendReading(
        used_minor=used_minor,
        limit_minor=_as_int(extra.get("monthly_limit")),
        exponent=2 if exponent is None else exponent,
        currency=_as_str(extra.get("currency")) or "USD",
        source="extra_usage",
        is_enabled=_as_bool(extra.get("is_enabled")),
        disabled_reason=_as_str(extra.get("disabled_reason")),
        disabled_until=_as_str(extra.get("disabled_until")),
        spend_limit_reached=_as_bool(extra.get("spend_limit_reached")),
    )


def _spend_from_overage_endpoint(payload: dict[str, Any]) -> SpendReading | None:
    if (used_minor := _as_int(payload.get("used_credits"))) is None:
        return None
    return SpendReading(
        used_minor=used_minor,
        limit_minor=_as_int(payload.get("monthly_credit_limit")),
        exponent=2,  # assumed: the endpoint carries no exponent
        currency=_as_str(payload.get("currency")) or "USD",
        source="overage_endpoint",
        disabled_until=_as_str(payload.get("disabled_until")),
    )


def parse_spend(usage_payload: Any, overage_payload: Any = None) -> SpendReading | None:
    """Spend from the usage payload, preferring ``spend`` over ``extra_usage``.

    ``extra_usage`` still contributes its state fields (enabled, reason, limit
    reached). If the two blocks disagree on the amounts the result is flagged
    ``conflict`` and the absolute figures are suppressed. The dedicated overage
    endpoint is used only when the usage payload has neither block.
    """
    primary: SpendReading | None = None
    extra: SpendReading | None = None
    if isinstance(usage_payload, dict):
        spend = usage_payload.get("spend")
        if isinstance(spend, dict):
            primary = _spend_from_spend_block(spend)
        extra_block = usage_payload.get("extra_usage")
        if isinstance(extra_block, dict):
            extra = _spend_from_extra_usage(extra_block)
    if primary is None and extra is None and isinstance(overage_payload, dict):
        return _spend_from_overage_endpoint(overage_payload)
    if primary is None:
        return _with_disabled_until(extra, overage_payload)
    if extra is None:
        return _with_disabled_until(primary, overage_payload)
    conflict = primary.used_minor != extra.used_minor or (
        primary.limit_minor is not None
        and extra.limit_minor is not None
        and primary.limit_minor != extra.limit_minor
    )
    merged = replace(
        primary,
        is_enabled=extra.is_enabled if primary.is_enabled is None else primary.is_enabled,
        disabled_reason=primary.disabled_reason or extra.disabled_reason,
        disabled_until=extra.disabled_until,
        spend_limit_reached=extra.spend_limit_reached,
        conflict=conflict,
    )
    return _with_disabled_until(merged, overage_payload)


def _with_disabled_until(reading: SpendReading | None, overage_payload: Any) -> SpendReading | None:
    if reading is None or reading.disabled_until is not None:
        return reading
    if isinstance(overage_payload, dict):
        until = _as_str(overage_payload.get("disabled_until"))
        if until:
            return replace(reading, disabled_until=until)
    return reading
