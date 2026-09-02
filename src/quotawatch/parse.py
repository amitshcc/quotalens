"""Turn raw claude.ai payloads into quota readings, defensively.

Strategy, in order:

1. The documented shape: top-level objects with ``utilization`` + ``resets_at``
   (``five_hour``, ``seven_day``, ``seven_day_sonnet``, and any sibling that
   looks the same) plus the model-scoped ``limits`` array with ``percent``.
2. A generic tree walk for any object carrying a percent-like number, so a
   renamed key degrades to "unlabelled reading" rather than "no data".
3. Failure: :class:`ParseError`. Callers record an event; nobody stores zero.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

PCT_KEYS = ("utilization", "percent", "pct", "percentage", "usage_percent")
RESET_KEYS = ("resets_at", "reset_at", "resetsAt", "reset_time")
MAX_WALK_DEPTH = 6

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class QuotaReading:
    window: str  # stable machine key, e.g. "five_hour", "limit:opus"
    label: str  # human label, e.g. "5-hour", "Opus"
    pct: float  # 0-100 percentage points consumed
    resets_at: str | None  # ISO-8601 as given by the server


@dataclass(frozen=True)
class OverageReading:
    spent_minor: int
    cap_minor: int
    currency: str


class ParseError(ValueError):
    """The payload had no recognizable quota data. Message names keys only, never values."""


_WINDOW_LABELS = {
    "five_hour": "5-hour",
    "seven_day": "7-day",
    "seven_day_sonnet": "7-day Sonnet",
    "seven_day_opus": "7-day Opus",
    "seven_day_oauth_apps": "7-day OAuth apps",
}


def slugify(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_") or "unknown"


def humanize(key: str) -> str:
    return _WINDOW_LABELS.get(key, key.replace("_", " "))


def _as_pct(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _reset_of(obj: dict[str, Any]) -> str | None:
    for key in RESET_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _pct_of(obj: dict[str, Any]) -> float | None:
    for key in PCT_KEYS:
        if key in obj:
            pct = _as_pct(obj[key])
            if pct is not None:
                return pct
    return None


def _limit_name(entry: dict[str, Any], index: int) -> str:
    scope = entry.get("scope")
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict):
            for key in ("display_name", "name", "id"):
                if isinstance(model.get(key), str) and model[key]:
                    return model[key]
        for key in ("display_name", "name", "type"):
            if isinstance(scope.get(key), str) and scope[key]:
                return scope[key]
    for key in ("display_name", "name", "id"):
        if isinstance(entry.get(key), str) and entry[key]:
            return entry[key]
    return f"limit {index}"


def _parse_documented(payload: dict[str, Any]) -> list[QuotaReading]:
    readings: list[QuotaReading] = []
    for key, value in payload.items():
        if key == "limits" or not isinstance(value, dict):
            continue
        pct = _pct_of(value)
        if pct is None:
            continue
        readings.append(QuotaReading(key, humanize(key), pct, _reset_of(value)))

    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, entry in enumerate(limits):
            if not isinstance(entry, dict):
                continue
            pct = _pct_of(entry)
            if pct is None:
                continue
            name = _limit_name(entry, index)
            readings.append(QuotaReading(f"limit:{slugify(name)}", name, pct, _reset_of(entry)))
    return readings


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
            _walk(value, (*path, str(key)), out, depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, (*path, str(index)), out, depth + 1)


def parse_usage(payload: Any) -> list[QuotaReading]:
    """Parse a ``/usage`` payload. Raises :class:`ParseError` if nothing usable is found."""
    if not isinstance(payload, dict):
        raise ParseError(f"usage payload is {type(payload).__name__}, expected object")
    readings = _parse_documented(payload)
    if readings:
        return _dedupe(readings)
    fallback: list[QuotaReading] = []
    _walk(payload, (), fallback, 0)
    if fallback:
        return _dedupe(fallback)
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
        seen[window] = QuotaReading(window, reading.label, reading.pct, reading.resets_at)
    return list(seen.values())


def parse_overage(payload: Any) -> OverageReading | None:
    """Parse ``/overage_spend_limit``. Returns ``None`` when overage is not configured."""
    if not isinstance(payload, dict):
        return None
    spent = payload.get("used_credits")
    cap = payload.get("monthly_credit_limit")
    currency = payload.get("currency")
    if not isinstance(spent, (int, float)) or isinstance(spent, bool):
        return None
    if not isinstance(cap, (int, float)) or isinstance(cap, bool):
        cap = 0
    return OverageReading(
        spent_minor=round(spent),
        cap_minor=round(cap),
        currency=currency if isinstance(currency, str) and currency else "USD",
    )
