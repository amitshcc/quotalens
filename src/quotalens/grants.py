"""Credit grants as the page and the API show them.

A grant is a dollar credit with an expiry (the Claude Code cloud-session credit), not a
quota window: it has no reset, it is never a weekly limit, and nothing here touches the
chart, the budget or the Weeks ledger. ``parse.py`` keeps grants out of the readings; this
module only decides what of the stored rows is on screen and how it reads.

An expired grant stays visible for a week as "expired", so the figure does not vanish
the moment it stops being usable; after that it leaves the page and stays in the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from quotalens.store import GrantRow

EXPIRED_VISIBLE_S = 7 * 86400
MINOR_PER_MAJOR = 100  # grants are USD in cents, see parse.GRANT_CURRENCY


def expiry_ts(expires_at: str | None) -> int | None:
    """Epoch seconds of an ISO-8601 expiry, or None when absent or unreadable."""
    if not expires_at:
        return None
    try:
        return int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def is_expired(row: GrantRow, now: int) -> bool:
    end = expiry_ts(row.expires_at)
    return end is not None and now >= end


def is_visible(row: GrantRow, now: int) -> bool:
    """On the page: not expired, or expired less than a week ago. No expiry: always."""
    end = expiry_ts(row.expires_at)
    return end is None or now < end + EXPIRED_VISIBLE_S


def dollars(minor: int) -> float:
    return minor / MINOR_PER_MAJOR


def money(minor: int, whole: bool = False) -> str:
    """``21.29`` as "$21.29"; with ``whole`` a round amount drops its cents ("$250")."""
    if whole and minor % MINOR_PER_MAJOR == 0:
        return f"${minor // MINOR_PER_MAJOR:,}"
    return f"${minor / MINOR_PER_MAJOR:,.2f}"


def _day(ts: int) -> str:
    local = datetime.fromtimestamp(ts).astimezone()
    return f"{local.day} {local.strftime('%b')}"


@dataclass(frozen=True)
class GrantView:
    key: str
    label: str
    figure: str  # "$21.29 of $250"
    detail: str  # "$228.71 left · expires 4 Nov"
    pct: float
    bar_pct: float
    expired: bool


def _pct(row: GrantRow) -> float:
    return row.used_minor / row.limit_minor * 100 if row.limit_minor > 0 else 0.0


def _detail(row: GrantRow, now: int) -> str:
    end = expiry_ts(row.expires_at)
    if end is not None and is_expired(row, now):
        text = f"expired {_day(end)} · {money(row.remaining_minor)} unused"
    else:
        text = f"{money(row.remaining_minor)} left"
        if end is not None:
            text += f" · expires {_day(end)}"
    if row.locked_reason:
        text += f" · locked: {row.locked_reason}"
    return text


def build_grant_views(rows: list[GrantRow], now: int, withheld: bool = False) -> list[GrantView]:
    """``withheld`` is the page's "readings not trusted" state: the label stays, the figures go."""
    views = []
    for row in rows:
        if not is_visible(row, now):
            continue
        if withheld:
            views.append(GrantView(row.key, row.label, "—", "", 0.0, 0.0, is_expired(row, now)))
            continue
        pct = _pct(row)
        views.append(
            GrantView(
                key=row.key,
                label=row.label,
                figure=f"{money(row.used_minor)} of {money(row.limit_minor, whole=True)}",
                detail=_detail(row, now),
                pct=round(pct, 1),
                bar_pct=max(0.0, min(pct, 100.0)),
                expired=is_expired(row, now),
            )
        )
    return views


def grant_as_dict(row: GrantRow) -> dict[str, Any]:
    """The API row: dollars as floats, ``pct`` of the limit used."""
    return {
        "key": row.key,
        "label": row.label,
        "used": dollars(row.used_minor),
        "limit": dollars(row.limit_minor),
        "remaining": dollars(row.remaining_minor),
        "pct": round(_pct(row), 2),
        "expires_at": row.expires_at,
        "locked_reason": row.locked_reason,
    }
