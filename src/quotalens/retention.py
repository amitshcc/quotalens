"""How long data is kept, and what that costs on disk.

Only one table was ever bounded: ``prune_samples`` caps ``sample`` at a row
count. ``quota`` grows at roughly 7,200 rows a day at a 60-second poll and
nothing stopped it, which is the actual reason a 16 MB database appears after a
fortnight.

**Retention is tiered, and deliberately not one number for everything.**
``session_window`` and ``event`` are what ``median_peak`` and ``compute_budgets``
read, and ``MIN_COMPARE_WINDOWS`` means the budget table needs history to say
anything at all. At five session windows a day, a *year* of them is under 2,000
rows: deleting them would break a feature to reclaim a rounding error. So the
user-facing period governs the detail tables, and the load-bearing ones sit on a
long fixed floor.

**Every size on the panel is measured from the user's own database.** A shipped
"about 4 MB" is a guess about someone else's poll interval, window count and
retention. Below two days of history the answer is not knowable, and
:class:`SizeEstimate` says so rather than extrapolating from one day -- DESIGN.md
5, the same rule the dashboard applies to every other unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_S = 86_400

# session_window and event, regardless of the chosen period. Two years of both is
# a few thousand rows; the budget table and the baseline read them.
FLOOR_DAYS = 730
AGED_TABLES = ("quota", "overage", "sample")
KEPT_TABLES = ("session_window", "event")

# The timestamp column to compare against, per table.
TS_COLUMN = {
    "quota": "ts",
    "overage": "ts",
    "sample": "ts",
    "event": "ts",
    "session_window": "started_at",
}

# Below this there is not enough history to divide by, so no estimate is offered.
MIN_SPAN_DAYS = 2.0


@dataclass(frozen=True)
class RetentionOption:
    key: str  # what `config set retention` takes, and the form value
    label: str  # what the panel shows
    days: int


RETENTION_OPTIONS: tuple[RetentionOption, ...] = (
    RetentionOption("1week", "1 week", 7),
    RetentionOption("1month", "1 month", 30),
    RetentionOption("3months", "3 months", 90),
    RetentionOption("6months", "6 months", 180),
    RetentionOption("1year", "1 year", 365),
)
RETENTION_BY_KEY = {o.key: o for o in RETENTION_OPTIONS}

# A new database may default to three months. A database that predates this
# setting must not: see `initial_retention`.
DEFAULT_RETENTION = "3months"
UPGRADE_RETENTION = "1year"


def option(key: str) -> RetentionOption:
    try:
        return RETENTION_BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"unknown retention {key!r}; one of: {', '.join(RETENTION_BY_KEY)}"
        ) from None


def initial_retention(existing_history_days: float | None) -> str:
    """What to store the first time this setting is written.

    A destructive default applied to data the user already has, without being
    asked for, is the worst thing this feature could do. A database that predates
    the setting gets the *longest* option, so writing the default deletes nothing
    and the choice stays theirs.
    """
    if existing_history_days is None or existing_history_days <= 0:
        return DEFAULT_RETENTION
    return UPGRADE_RETENTION


@dataclass(frozen=True)
class SizeEstimate:
    """Projected total size at one retention period.

    ``bytes`` is ``None`` when there is too little history to divide by; the
    caller renders an em dash and ``reason``, and never a number.
    """

    option: RetentionOption
    bytes: int | None
    reason: str = ""


@dataclass(frozen=True)
class Measurement:
    """What this database actually holds right now, per table."""

    span_days: float
    rows: dict[str, int]
    row_bytes: dict[str, float]  # measured average, calibrated to the real file size
    total_bytes: int

    @property
    def usable(self) -> bool:
        return self.span_days >= MIN_SPAN_DAYS and self.total_bytes > 0


def measure(
    *,
    span_s: float | None,
    rows: dict[str, int],
    raw_row_bytes: dict[str, float],
    total_bytes: int | None,
) -> Measurement:
    """Calibrate measured row widths so they sum to the real file size.

    ``raw_row_bytes`` is what ``length()`` reports for the stored values, which
    ignores page overhead, indexes and the free list. Scaling the whole set by
    one factor so the projected total equals the file's actual size makes today's
    number exactly right, and that is the number every projection is relative to.
    """
    span_days = (span_s or 0) / DAY_S
    modelled = sum(raw_row_bytes.get(t, 0.0) * rows.get(t, 0) for t in rows)
    factor = (total_bytes / modelled) if (modelled > 0 and total_bytes) else 1.0
    return Measurement(
        span_days=span_days,
        rows=dict(rows),
        row_bytes={t: raw_row_bytes.get(t, 0.0) * factor for t in rows},
        total_bytes=total_bytes or 0,
    )


def estimate(m: Measurement, sample_keep: int) -> list[SizeEstimate]:
    """Projected size at each option, from this database's own rates."""
    if not m.usable:
        reason = "needs more history" if m.span_days < MIN_SPAN_DAYS else "database is empty"
        return [SizeEstimate(o, None, reason) for o in RETENTION_OPTIONS]

    per_day = {t: m.rows.get(t, 0) / m.span_days for t in TS_COLUMN}
    out = []
    for opt in RETENTION_OPTIONS:
        total = 0.0
        for table in AGED_TABLES:
            projected = per_day[table] * opt.days
            if table == "sample":
                # Independently capped by a row count, so it stops growing first.
                projected = min(projected, sample_keep)
            total += projected * m.row_bytes.get(table, 0.0)
        for table in KEPT_TABLES:
            # Unaffected by the dropdown: they sit on the floor either way.
            total += per_day[table] * min(opt.days, FLOOR_DAYS) * m.row_bytes.get(table, 0.0)
        out.append(SizeEstimate(opt, int(total)))
    return out


def cutoffs(retention: str, now: int) -> dict[str, int]:
    """The oldest timestamp each table keeps: the aged ones, then the floor."""
    days = option(retention).days
    aged = now - days * DAY_S
    floor = now - FLOOR_DAYS * DAY_S
    return {**{t: aged for t in AGED_TABLES}, **{t: floor for t in KEPT_TABLES}}


def format_bytes(n: int | None) -> str:
    """``None`` never becomes a number. An em dash is the honest rendering."""
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1_048_576:.0f} MB"


RETENTION_SET_KIND = "retention_initialised"


def initialise(
    *,
    stored: dict[str, object],
    history_days: float | None,
    write: object,
    record_event: object,
    now: int,
) -> tuple[str, str | None]:
    """Decide and persist the first retention value. Returns (value, notice).

    The dangerous moment in this whole feature is the first run after upgrade:
    a default applied to data the user already has, without being asked for,
    deletes history that cannot come back. So a database with history gets the
    longest option and a notice, never the shipped default, and this returns the
    notice so the dashboard can say what happened rather than saying nothing.
    """
    if "retention" in stored:
        return str(stored["retention"]), None
    value = initial_retention(history_days)
    stored = {**stored, "retention": value}
    write(stored)  # type: ignore[operator]
    if history_days and history_days > 0:
        notice = (
            f"Retention is now configurable and has been set to "
            f"{option(value).label} \u2014 the longest option, so nothing you already "
            f"have was deleted. Change it in settings."
        )
    else:
        notice = None
    record_event(RETENTION_SET_KIND, notice or f"set to {value} on a new database", ts=now)  # type: ignore[operator]
    return value, notice
