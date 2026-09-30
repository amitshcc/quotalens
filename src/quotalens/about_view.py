"""The About page: what is installed, whether a newer one exists, and where to go from here.

A pure function of a value, like the settings page. The layout reuses the
settings form's rows (``.fform`` / ``.fld``) rather than adding CSS: the sheet is
at its byte budget, and an About page is the same shape as a settings page with
nothing to edit. The one control is a real ``<form>``, so it works with
JavaScript off; app.js swaps the fragment in place when it is on.
"""

from __future__ import annotations

import platform
import time
from dataclasses import dataclass
from html import escape as e

from quotalens import updates
from quotalens.config import Settings
from quotalens.render import render_shell
from quotalens.store import Store

WEBSITE = "https://quotalens.com"
SOURCE = "https://github.com/amitshcc/quotalens"
ISSUES = f"{SOURCE}/issues/new/choose"
TERMS = f"{SOURCE}#the-terms-stated-plainly"
TAGLINE = (
    "Local monitor for your Claude subscription quota. Reads your own usage; never sends a prompt."
)
DISCLAIMER = "Unofficial; not affiliated with or endorsed by Anthropic."


@dataclass(frozen=True)
class AboutView:
    state: updates.UpdateState
    upgrade_command: str
    install_method: str
    python: str
    data_dir: str
    db_path: str
    profile: str
    port: int
    poll_interval_s: int
    schema_version: int | None
    note: str = ""  # said after a manual check that was refused by the 60 s limit


def build_about(settings: Settings, store: Store, *, note: str = "") -> AboutView:
    rows = store.query("SELECT MAX(version) AS v FROM schema_version")
    return AboutView(
        state=updates.stored_state(store),
        upgrade_command=updates.upgrade_command(),
        install_method=updates.install_method(),
        python=platform.python_version(),
        data_dir=str(settings.db_path.parent),
        db_path=str(settings.db_path),
        profile=settings.profile or "default",
        port=settings.port,
        poll_interval_s=settings.poll_interval_s,
        schema_version=rows[0]["v"] if rows else None,
        note=note,
    )


def _when(ts: int) -> str:
    t = time.localtime(ts)
    return f"{time.strftime('%H:%M', t)}, {t.tm_mday} {time.strftime('%b', t)}"


def _row(label: str, value_html: str) -> str:
    return f'<div class="fld"><label>{e(label)}</label><code>{value_html}</code></div>'


def _latest(state: updates.UpdateState) -> str:
    if state.latest is None:
        return "not checked yet"
    if state.available:
        tag = e(state.latest)
        return (
            f"{tag} available "
            f'<a href="{SOURCE}/releases/tag/v{tag}" target="_blank" '
            'rel="noopener noreferrer">What’s new</a>'
        )
    return f"{e(state.latest)} — you’re up to date"


def _last_checked(state: updates.UpdateState) -> str:
    if state.checked_ts is None:
        return "never"
    if state.error:
        return f"Couldn’t reach PyPI at {e(_when(state.checked_ts).split(',')[0])}."
    return e(_when(state.checked_ts))


def _link(label: str, href: str, shown: str | None = None) -> str:
    text = shown or href
    return (
        f'<div class="fld"><label>{e(label)}</label>'
        f'<a href="{e(href)}" target="_blank" rel="noopener noreferrer">{e(text)}</a></div>'
    )


def render_about(view: AboutView) -> str:
    """The fragment: what the settings dialog shows and what a check swaps in."""
    state = view.state
    upgrade = _row("Upgrade", e(view.upgrade_command)) if state.available else ""
    note = f'<p class="far" role="status">{e(view.note)}</p>' if view.note else ""
    return (
        '<section class="sset" id="about" data-about>'
        '<p class="cap"><img src="/favicon.svg" alt="" width="20" height="20"> QuotaLens</p>'
        f'<p class="far lede">{e(TAGLINE)}</p>'
        '<div class="fform">'
        + _row("Version", e(state.current))
        + _row("Latest", _latest(state))
        + upgrade
        + _row("Last checked", _last_checked(state))
        + '<form method="post" action="/about/check" id="about-check-form" class="ctl">'
        '<button type="submit" id="about-check">Check for updates</button></form>'
        + note
        + '<p class="cap">Details</p>'
        + _row("Python", e(view.python))
        + _row("Installed with", e(view.install_method))
        + _row("Data directory", e(view.data_dir))
        + _row("Database", e(view.db_path))
        + _row("Profile", e(view.profile))
        + _row("Port", str(view.port))
        + _row("Poll interval", f"{view.poll_interval_s} s")
        + _row("Schema version", str(view.schema_version or "unknown"))
        + '<p class="cap">Links</p>'
        + _link("Website", WEBSITE)
        + _link("Source", SOURCE)
        + _link("Report an issue", ISSUES, "github.com/amitshcc/quotalens/issues")
        + _row("Licence", "MIT")
        + f'<p class="far">{e(DISCLAIMER)} '
        f'<a href="{TERMS}" target="_blank" rel="noopener noreferrer">'
        "The Terms, stated plainly</a>.</p></div></section>"
    )


def render_about_page(view: AboutView) -> str:
    return render_shell("About QuotaLens", render_about(view))
