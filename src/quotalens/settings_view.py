"""The view model behind the settings page, and what it takes to change a setting.

Kept out of ``render.py`` so the page stays a pure function of a value, and out
of ``api.py`` so the rules -- which keys the panel may write, what a shortened
retention would destroy -- are testable without a request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quotalens import retention
from quotalens.config import (
    CONFIG_KEYS_BY_NAME,
    Settings,
    SettingsError,
    parse_config_value,
    read_config_file,
    validate,
    write_config_file,
)
from quotalens.notify import Capability
from quotalens.store import Store

# Everything the panel may write. The port, the database path and the cookie are
# absent on purpose: see `render_settings`'s read-only block for each reason.
PANEL_KEYS = (
    "interval",
    "lookback",
    "burn_alert",
    "sample_keep",
    "webhook_url",
    "notify",
    "notify_thresholds",
    "status_row",
    "status_vendors",
)
BOOLEAN_KEYS = ("notify", "status_row")


@dataclass(frozen=True)
class SettingsView:
    values: dict[str, str]
    port: int
    db_path: Path
    notify_capability: Capability
    estimates: list[retention.SizeEstimate] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    shrink_rows: int | None = None  # set only when the choice would delete something
    shrink_span: str = ""


def _shown(settings: Settings, key: str) -> str:
    value = getattr(settings, CONFIG_KEYS_BY_NAME[key].field)
    if isinstance(value, bool):
        return "1" if value else ""
    return "" if value is None else str(value)


def build_view(
    settings: Settings,
    store: Store,
    capability: Capability,
    *,
    errors: dict[str, str] | None = None,
    values: dict[str, str] | None = None,
) -> SettingsView:
    """The current settings, with measured retention sizes.

    ``values`` overrides what is shown, so a rejected submission redisplays what
    the user typed rather than silently reverting it under an error message.
    """
    shown = {key: _shown(settings, key) for key in PANEL_KEYS}
    shown["retention"] = settings.retention or ""
    if values:
        shown.update(values)
    oldest = store.oldest_ts()
    now = int(time.time())
    measurement = retention.measure(
        span_s=(now - oldest) if oldest else None,
        rows=store.counts(),
        raw_row_bytes=store.row_widths(),
        total_bytes=store.db_size_bytes(),
    )
    return SettingsView(
        values=shown,
        port=settings.port,
        db_path=settings.db_path,
        notify_capability=capability,
        estimates=retention.estimate(measurement, settings.sample_keep),
        errors=dict(errors or {}),
    )


def apply_form(
    settings: Settings, form: dict[str, Any], path: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate a submission and persist it. Returns (errors, submitted values).

    Runs the same ``validate()`` the CLI runs, over the whole merged result, so
    the panel cannot store a combination the command line would refuse. Nothing
    is written unless every field passes: a half-applied form is worse than a
    rejected one.
    """
    errors: dict[str, str] = {}
    submitted: dict[str, str] = {}
    candidate = settings
    for key in PANEL_KEYS:
        spec = CONFIG_KEYS_BY_NAME[key]
        raw: Any = form.get(key)
        if key in BOOLEAN_KEYS:
            raw = "1" if raw else "0"  # an unchecked box sends nothing at all
        submitted[key] = "" if raw is None else str(raw)
        try:
            value = parse_config_value(spec, raw, key)
            candidate = candidate.with_overrides(**{spec.field: value})
        except SettingsError as exc:
            errors[key] = str(exc)
    if errors:
        return errors, submitted
    try:
        validate(candidate)
    except SettingsError as exc:
        # validate() speaks about one invariant at a time and names the setting in
        # its message; attach it to the field it is about when we can tell.
        field_key = next((k for k in PANEL_KEYS if k in str(exc).lower()), PANEL_KEYS[0])
        errors[field_key] = str(exc)
        return errors, submitted

    stored = read_config_file(path)
    for key in PANEL_KEYS:
        spec = CONFIG_KEYS_BY_NAME[key]
        stored[key] = getattr(candidate, spec.field)
    write_config_file(path, stored)
    return {}, submitted


def shrink_impact(store: Store, current: str | None, chosen: str) -> tuple[int, str] | None:
    """What moving to ``chosen`` would destroy, or ``None`` if it destroys nothing.

    Shortening retention is irreversible, so the panel says how many rows and how
    much history *before* applying, not in a toast afterwards.
    """
    if current is not None and retention.option(chosen).days >= retention.option(current).days:
        return None
    now = int(time.time())
    result = store.prune_by_age(retention.cutoffs(chosen, now), dry_run=True)
    if not result.candidates:
        return None
    oldest = store.oldest_ts()
    days = (
        (now - retention.option(chosen).days * retention.DAY_S) - (oldest or 0)
    ) / retention.DAY_S
    span = f"{days:.0f} days" if days >= 1 else "less than a day"
    return result.candidates, span
