"""HTML for the dashboard. No logic beyond escaping and layout; see :mod:`dashboard`."""

from __future__ import annotations

from html import escape as e

from quotalens import __version__
from quotalens.alerts import ALERT_KIND
from quotalens.dashboard import (
    PLOT_RIGHT,
    RATE_WINDOW,
    Control,
    Dashboard,
    SeriesView,
    SessionRowView,
    WindowView,
    clock,
)
from quotalens.runway import fmt_span
from quotalens.views import AUTO, RANGE_KEYS

ICONS = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
    '<symbol id="i-alert" viewBox="0 0 16 16"><path d="M8 2.4 14.6 13.6H1.4Z"/>'
    '<path d="M8 6.6v3.1M8 11.9v.1"/></symbol>'
    '<symbol id="i-stale" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.1" '
    'stroke-dasharray="2.6 2.2"/><path d="M8 4.6V8l2.3 1.6"/></symbol>'
    '<symbol id="i-auth" viewBox="0 0 16 16"><circle cx="5.3" cy="8" r="2.9"/>'
    '<path d="M8.2 8h6.2M12.1 8v2.6"/></symbol>'
    '<symbol id="i-rate" viewBox="0 0 16 16"><path d="M3 11.4 6.7 7l2.7 2.3L13.4 4"/>'
    '<path d="M9.9 4h3.5v3.4"/></symbol>'
    '<symbol id="i-sun" viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.1"/>'
    '<path d="M8 1.4V3M8 13v1.6M1.4 8H3M13 8h1.6'
    'M11.5 4.5 12.7 3.3M4.5 4.5 3.3 3.3M11.5 11.5l1.2 1.2M4.5 11.5l-1.2 1.2"/></symbol>'
    '<symbol id="i-moon" viewBox="0 0 16 16">'
    '<path d="M9.9 2.7a5.6 5.6 0 1 0 3.4 8.9A5.6 5.6 0 0 1 9.9 2.7Z"/></symbol>'
    '<pattern id="gap" width="6" height="6" patternUnits="userSpaceOnUse" '
    'patternTransform="rotate(-45)"><line x1="0" y1="0" x2="0" y2="6" '
    'stroke="var(--st-stale)" stroke-width="1.2" opacity=".45"/></pattern>'
    "</defs></svg>"
)

# The mark, per DESIGN.md §9: a faint full circle is the whole session window, and
# a solid arc over it, clockwise from twelve, is the part of it you have used. Both
# geometries are fixed by that section; the favicon is redrawn on a true 16 grid
# rather than scaled, so its strokes land on whole pixels.
MARK_GRID, MARK_R, MARK_STROKE, MARK_CIRC = 24, 9.0, 2.6, 56.55  # 2*pi*9
MARK_SIZE = 22  # as rendered in the header
FAVICON_GRID, FAVICON_R, FAVICON_STROKE, FAVICON_CIRC = 16, 5.5, 3.0, 34.56  # 2*pi*5.5
UNKNOWN_DASH = "3 3"  # the empty track when there is no reading to draw

# The arc follows the meters, not the brand: the same states, the same tokens.
#
# Every colour carries a fallback because the favicon is fetched as a standalone
# document with no tokens.css behind it. There, `var(--s1)` alone is an invalid
# substitution, `stroke` falls back to its initial value of `none`, and the icon
# renders as nothing at all — which is exactly what shipped.
#
# Both halves of each `light-dark()` pair in tokens.css are repeated here, light
# then dark. There is no way to resolve a CSS custom property from Python, so
# this is a copy; `test_mark.py` parses tokens.css and fails if either half
# drifts, which is what keeps it from becoming a second source of truth.
MARK_COLOURS = {
    "track": ("--txt-far", "#808887", "#626A6B"),
    "normal": ("--s1", "#A66A00", "#F2B33D"),
    "elevated": ("--st-elevated", "#8A5B00", "#F2B33D"),
    "critical": ("--st-critical", "#B3242B", "#F0575C"),
}


def paint(key: str) -> str:
    """The token, with the dark value behind it: tokens.css declares dark as the base."""
    token, _light, dark = MARK_COLOURS[key if key in MARK_COLOURS else "normal"]
    return f"var({token}, {dark})"


# Only the standalone favicon needs this: an inlined mark follows the page's own
# theme, including a manual toggle, which a media query inside it could not.
FAVICON_LIGHT_STYLE = (
    "<style>@media (prefers-color-scheme:light){svg{"
    + ";".join(f"{token}:{light}" for token, light, _dark in MARK_COLOURS.values())
    + "}}</style>"
)


def mark_reading(dash: Dashboard) -> tuple[float | None, str]:
    """What the mark draws: the session window, as the 5-hour meter already has it.

    Read off the built view rather than recomputed from the store, so the mark in
    the header and the meter below it cannot disagree. ``None`` means there is no
    trusted reading, which is drawn as an empty ring rather than as a stale arc.
    """
    window = next((w for w in dash.windows if w.key == RATE_WINDOW), None)
    if window is None or window.withheld or window.pct is None:
        return None, "normal"
    return max(0.0, min(window.pct, 100.0)) / 100.0, window.state


def ring(
    fraction: float | None,
    state: str,
    grid: int,
    size: int,
    radius: float,
    width: float,
    circumference: float,
    tail: str,
    head: str = "",
) -> str:
    """The mark at one size. ``head`` is prepended inside the <svg>: the favicon's only.

    The header mark passes no ``head`` and so emits no <style>. It does not need
    one: inlined in the page, its tokens resolve from tokens.css and follow the
    theme toggle, which a media query inside the mark could not.
    """
    centre = grid / 2
    circle = (
        f'<circle cx="{centre:g}" cy="{centre:g}" r="{radius:g}" fill="none" '
        f'stroke-width="{width:g}"'
    )
    if fraction is None:
        # Stale, auth-failed or unverified. A mark frozen at the last good reading
        # is a lie in the tab strip, the same way a stale red number is one.
        body = f'{circle} stroke="{paint("track")}" stroke-dasharray="{UNKNOWN_DASH}"/>'
    else:
        used = circumference * fraction
        body = (
            f'{circle} stroke="{paint("track")}"/>'
            f'{circle} stroke="{paint(state)}" '
            f'stroke-dasharray="{used:.2f} {circumference:.2f}" '
            f'transform="rotate(-90 {centre:g} {centre:g})"/>'
        )
    return (
        f'<svg viewBox="0 0 {grid} {grid}" width="{size}" height="{size}" {tail}>{head}{body}</svg>'
    )


def header_mark(dash: Dashboard) -> str:
    """Hidden from screen readers: "QuotaLens" is beside it and the percentage is a meter."""
    fraction, state = mark_reading(dash)
    return ring(
        fraction,
        state,
        MARK_GRID,
        MARK_SIZE,
        MARK_R,
        MARK_STROKE,
        MARK_CIRC,
        'aria-hidden="true"',
    )


def favicon_svg(dash: Dashboard) -> str:
    """The tab strip, where the mark is the only thing you can read from another tab."""
    fraction, state = mark_reading(dash)
    reading = (
        "session window unknown"
        if fraction is None
        else f"{fraction * 100:.0f}% of the session window used"
    )
    label = f"QuotaLens \u2014 {reading}"
    return ring(
        fraction,
        state,
        FAVICON_GRID,
        FAVICON_GRID,
        FAVICON_R,
        FAVICON_STROKE,
        FAVICON_CIRC,
        f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{e(label)}"',
        head=f"<title>{e(label)}</title>{FAVICON_LIGHT_STYLE}",
    )


_CHIP = {
    "elevated": ("elev", "i-rate"),
    "critical": ("crit", "i-alert"),
    "stale": ("stale", "i-stale"),
    "unverified": ("stale", "i-stale"),
    "auth": ("auth", "i-auth"),
}
_VALUE_COLOUR = {"elevated": "var(--st-elevated)", "critical": "var(--st-critical)"}
# Visible em dash, plus the hidden twin the link-lost stylesheet reveals.
WITHHELD = '<span class="num">—</span><span class="dash">—</span>'
DISCLAIMER = "Unofficial. Uses undocumented claude.ai endpoints. Observes only."


def chip(kind: str, text: str) -> str:
    if kind not in _CHIP or not text:
        return ""
    cls, icon = _CHIP[kind]
    return (
        f'<span class="chip {cls}"><svg class="ic" aria-hidden="true">'
        f'<use href="#{icon}"/></svg>{e(text)}</span>'
    )


def render_page(dash: Dashboard) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>QuotaLens</title>\n"
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">\n'
        '<link rel="stylesheet" href="/static/tokens.css">\n'
        '<link rel="stylesheet" href="/static/app.css">\n'
        '<script src="/static/app.js"></script>\n'
        '<script src="/static/chart.js" defer></script>\n'
        "</head>\n"
        f'<body data-refresh="{dash.refresh_s}">\n{ICONS}\n'
        f'<div id="app">{render_app(dash)}</div>\n</body>\n</html>\n'
    )


def render_app(dash: Dashboard) -> str:
    """The refreshable part: header and main, so state chips update together."""
    return _header(dash) + _main(dash)


def _header(dash: Dashboard) -> str:
    fallback = f"last ok {dash.polled_text[8:]}" if dash.polled_text.startswith("last ok") else ""
    ts = dash.last_success_ts or 0
    lost = (
        '<span class="chip stale" id="link"><svg class="ic" aria-hidden="true">'
        '<use href="#i-stale"/></svg>dashboard unreachable since '
        '<span id="lost-since"></span></span>'
    )
    return (
        '<header><div class="wrap">'
        f'<span class="brand">{header_mark(dash)}QuotaLens</span><span class="spacer"></span>'
        f"{lost}{_alert_chip(dash)}{chip(dash.chip, dash.chip_text)}"
        f'<span class="lbl m" id="polled" data-ts="{ts}" data-fallback="{e(fallback)}">'
        f"{e(dash.polled_text)}</span>"
        # Both icons ship; CSS shows the one for the theme a click would give you,
        # because the theme is settled in the browser and the server cannot know it.
        '<button id="t" type="button" aria-label="Switch theme">'
        '<svg class="ic ic-sun" aria-hidden="true"><use href="#i-sun"/></svg>'
        '<svg class="ic ic-moon" aria-hidden="true"><use href="#i-moon"/></svg>theme</button>'
        "</div></header>"
    )


def _main(dash: Dashboard) -> str:
    q = dash.view.query()
    return (
        f'<main class="wrap" data-query="{e(q)}" data-lookback="{dash.lookback_s}">'
        + _health_strip(dash)
        + _hero(dash)
        + _meters(dash)
        + _budget(dash)
        + _toolbar(dash)
        + _chart(dash)
        + '<div class="cols">'
        + "<div>"
        + _history(dash)
        + _attribution()
        + "</div>"
        + _side(dash)
        + "</div>"
        + _footer(dash)
        + "</main>"
    )


def _alert_chip(dash: Dashboard) -> str:
    return chip("elevated", "burn alert") if dash.alert_standing else ""


def _events_block(dash: Dashboard) -> str:
    """The last few anomalies and threshold crossings, where the diagnostics live."""
    if not dash.events:
        return ""
    lines = []
    for event in dash.events:
        css = "ev ev-alert" if event["kind"] == ALERT_KIND else "ev"
        stamp = e(clock(int(event["ts"])))
        lines.append(
            f'<p class="{css}"><span class="m far">{stamp}</span> {e(str(event["detail"]))}</p>'
        )
    return '<div class="rule"></div><dl><dt>Recent events</dt><dd></dd></dl>' + "".join(lines)


def _health_strip(dash: Dashboard) -> str:
    if not dash.health_message:
        return ""
    kind = dash.epistemic.kind if dash.epistemic.kind != "ok" else "note"
    return (
        f'<section class="hstrip {kind}" aria-live="polite">'
        f"<p>{e(dash.health_message)}</p></section>"
    )


def _hero(dash: Dashboard) -> str:
    b = dash.burn
    r = b.runway
    withheld = b.withheld
    cls = "readout off" if withheld else ("readout is-crit" if b.critical else "readout")
    value = (
        WITHHELD
        if withheld
        else (f'<span class="num">{e(b.headroom_text)}</span><span class="dash">—</span>')
    )
    if withheld or r is None:
        resets = f'<span class="v m">{WITHHELD}</span>'
    elif r.reset_ts and r.remaining_s > 0:
        resets = (
            f'<span class="v m"><span class="num" id="reset-in" data-reset="{r.reset_ts}">'
            f'{e(fmt_span(r.remaining_s))}</span><span class="dash">—</span></span>'
        )
    else:
        resets = '<span class="v m"><span class="num">no window</span></span>'
    verdict = e(b.why) if withheld else f"<b>{e(b.why)}</b>"
    detail = f'<br><span class="far">{e(b.detail)}</span>' if b.detail else ""
    state_chip = ""
    if not withheld and b.critical:
        state_chip = " " + chip("critical", "critical")
    elif b.elevated:
        state_chip = " " + chip("elevated", "elevated")
    return (
        '<section class="screen hero" aria-labelledby="br">'
        f'<h2 class="lbl" id="br">Session{state_chip}</h2><div class="hrow">'
        f'<div class="{cls}">{value}<span class="u">% left</span></div>'
        f'<div class="resets"><span class="lbl">resets in</span>{resets}</div>'
        f'<p class="why">{verdict}{detail}</p></div>' + _hour_strip(dash) + "</section>"
    )


def _hour_strip(dash: Dashboard) -> str:
    bars = dash.burn.hours
    if not bars:
        return ""
    scale = dash.burn.hours_max or 20.0
    cells = []
    for bar in bars:
        label = clock(bar.start_ts)
        if bar.state == "future":
            cells.append(
                f'<div class="hb future" title="{label}: not started"><i></i>'
                f'<span class="ax-l">{label}</span></div>'
            )
            continue
        if bar.consumed is None:
            cells.append(
                f'<div class="hb nodata" title="{label}: no samples"><i class="hatch"></i>'
                f'<span class="ax-l">{label}</span></div>'
            )
            continue
        height = max(2.0, min(100.0, bar.consumed / scale * 100))
        cells.append(
            f'<div class="hb {bar.state}" title="{label}: {bar.consumed:.0f} pts">'
            f'<span class="hv">{bar.consumed:.0f}</span><i style="height:{height:.0f}%"></i>'
            f'<span class="ax-l">{label}</span></div>'
        )
    return (
        '<div class="hours" role="img" aria-label="Points consumed per hour of this window">'
        + "".join(cells)
        + "</div>"
    )


def _meters(dash: Dashboard) -> str:
    if not dash.windows:
        body = (
            '<div class="meter"><div class="lbl">No windows yet</div>'
            f'<div class="v m">{WITHHELD}</div><div class="bar hatch"></div>'
            '<div class="foot m"><span>waiting for the first poll</span></div></div>'
        )
    else:
        body = "".join(_meter(w) for w in dash.windows)
    n = max(1, min(len(dash.windows), 4)) if dash.windows else 1
    return f'<div class="screen meters" style="--n:{n}">{body}</div>'


def _budget(dash: Dashboard) -> str:
    """The weekly limits in session windows. Under the meters, never in the hero.

    The hero is the session window and stays that way; this answers the other
    question, which is about the week.
    """
    view = dash.budget_view
    if view is None or not view.rows:
        return ""
    body = "".join(
        f"<tr{f' title={_q(r.reason)}' if r.reason else ''}>"
        f"<td>{e(r.label)}</td>"
        f'<td class="n">{e(r.budget_text)}</td>'
        f'<td class="n">{e(r.clock_text)}</td>'
        f'<td class="n">{e(r.cost_text)}'
        + (f' <span class="far">{e(r.spread_text)}</span>' if r.spread_text else "")
        + "</td></tr>"
        for r in view.rows
    )
    note = (
        f'<tfoot><tr><td colspan="4" class="far">{e(view.constraint)}</td></tr></tfoot>'
        if view.constraint
        else ""
    )
    return (
        '<section class="screen budget"><table>'
        "<caption>Weekly budget — session windows the headroom will pay for, costed from "
        "this history</caption>"
        '<thead><tr><th>Limit</th><th class="n">Windows of budget</th>'
        '<th class="n">Windows of clock</th><th class="n">Cost per full window</th></tr></thead>'
        f"<tbody>{body}</tbody>{note}</table></section>"
    )


def _q(text: str) -> str:
    return f'"{e(text)}"'


def _meter(w: WindowView) -> str:
    label = e(w.label)
    active = ' <span class="far">active</span>' if w.is_active and not w.withheld else ""
    # Sits on the label line like "active" does, so no meter changes height.
    note = f' <span class="far" title="{e(w.note_title)}">{e(w.note)}</span>' if w.note else ""
    if w.withheld:
        return (
            f'<div class="meter is-stale" data-slot="{w.slot}">'
            f'<div class="lbl">{label}{note}</div>'
            f'<div class="v m">{WITHHELD}</div><div class="bar hatch"></div>'
            f'<div class="foot m"><span>{e(w.resets_text)}</span><span></span></div></div>'
        )
    colour = _VALUE_COLOUR.get(w.state)
    style = f' style="color:{colour}"' if colour else ""
    bar_colour = "var(--st-critical)" if w.state == "critical" else f"var(--s{w.slot})"
    state_chip = " " + chip(w.state, w.state) if w.state != "normal" else ""
    return (
        f'<div class="meter" data-slot="{w.slot}">'
        f'<div class="lbl">{label}{note}{state_chip}{active}</div>'
        f'<div class="v m"{style}><span class="num">{e(w.pct_text)}</span><span class="u">%</span>'
        '<span class="dash">—</span></div>'
        f'<div class="bar"><i style="width:{w.bar_pct:.1f}%;background:{bar_colour}"></i></div>'
        f'<div class="foot m"><span>{e(w.resets_text)}</span><span>{e(w.delta_text)}</span></div>'
        "</div>"
    )


def _controls(name: str, controls: list[Control], legend: str) -> str:
    links = []
    for c in controls:
        cls = "rb on" if c.active else "rb"
        current = ' aria-current="true"' if c.active else ""
        links.append(
            f'<a class="{cls}" href="{e(c.href)}" data-{name}="{e(c.key)}"{current}>'
            f"{e(c.label)}</a>"
        )
    return (
        f'<span class="ctl" role="group" aria-label="{e(legend)}">'
        f'<span class="lbl">{e(legend)}</span>{"".join(links)}</span>'
    )


def _range_form(dash: Dashboard) -> str:
    """A GET form: works without script; script submits it on change."""
    view = dash.view
    current = view.range_param() or AUTO
    options = [(AUTO, "auto")] + [(k, k) for k in RANGE_KEYS]
    if view.range_key == "custom" and view.range_param():
        options.append((view.range_param() or "", f"custom: {dash.rng.label}"))
    opts = "".join(
        f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(t)}</option>'
        for v, t in options
    )
    hidden = "".join(
        f'<input type="hidden" name="{n}" value="{e(v)}">'
        for n, v in (
            ("hide", ",".join(sorted(view.hidden))),
            ("lookback", view.lookback_key or ""),
            ("refresh", view.refresh_key or ""),
            ("sort", view.sort_key or ""),
        )
        if v
    )
    return (
        '<form method="get" action="/" class="ctl" id="range-form">'
        '<label class="lbl" for="range">range</label>'
        f'<select name="range" id="range">{opts}</select>{hidden}'
        '<button type="submit" class="go">go</button></form>'
    )


def _toolbar(dash: Dashboard) -> str:
    q = dash.view.query()
    action = "/poll" + (f"?{q}" if q else "")
    return (
        '<nav class="toolbar" aria-label="Chart controls">'
        + _range_form(dash)
        + _controls("lookback", dash.lookback_controls, "lookback")
        + '<span class="spacer"></span>'
        + _controls("refresh", dash.refresh_controls, "auto")
        + f'<form method="post" action="{e(action)}" class="ctl" id="poll-form">'
        '<button type="submit" id="poll" title="Force a poll now" '
        f'data-cooldown="{dash.cooldown_s}">'
        '<svg class="ic" aria-hidden="true"><use href="#i-rate"/></svg>'
        '<span id="poll-label">poll now</span></button></form>'
        + "".join(
            f'<span class="lbl" id="poll-note" aria-live="polite">{e(note)}</span>'
            for note in dash.notes[:1]
        )
        + "</nav>"
    )


def _chart(dash: Dashboard) -> str:
    c = dash.chart
    if c.collecting_text:
        inner = (
            f'<text x="636" y="112" class="ax" text-anchor="middle">{e(c.collecting_text)}</text>'
        )
    elif not c.has_data:
        inner = (
            '<text x="636" y="112" class="ax" text-anchor="middle">No readings in this range</text>'
        )
    else:
        idle = "".join(
            f'<rect x="{a:.1f}" y="14" width="{max(b - a, 1.5):.1f}" height="182" class="idle"/>'
            + (
                f'<text x="{(a + b) / 2:.1f}" y="26" class="ax" text-anchor="middle">'
                "no session</text>"
                if b - a > 90
                else ""
            )
            for a, b in c.idle
        )
        gaps = idle + "".join(
            f'<rect x="{a:.1f}" y="14" width="{max(b - a, 1.5):.1f}" height="182" '
            'fill="url(#gap)" class="gap"/>'
            for a, b in c.gaps
        )
        gaps += "".join(
            f'<line x1="{x:.1f}" y1="14" x2="{x:.1f}" y2="196" class="sess"/>' for x in c.session_x
        )
        # The horizontal rules run the full width of the plot, including the part of
        # the window that has not happened yet. Stopping them at "now" left the
        # chart looking cropped, in an area that is still live to the pointer; the
        # `now` line is what marks the present.
        grid_end = PLOT_RIGHT
        grid = "".join(
            f'<line x1="44" y1="{y:.1f}" x2="{grid_end:.1f}" y2="{y:.1f}" class="{_gclass(t)}"/>'
            f'<text x="36" y="{y + 4:.1f}" class="ax" text-anchor="end">{e(t)}</text>'
            for y, t in c.y_ticks
        )
        grid += "".join(
            f'<line x1="{x:.1f}" y1="14" x2="{x:.1f}" y2="196" class="hr"/>' for x in c.hour_x
        )
        if c.future:
            grid += (
                f'<line x1="{c.now_x:.1f}" y1="14" x2="{c.now_x:.1f}" y2="196" class="now"/>'
                f'<text x="{c.now_x + 4:.1f}" y="24" class="ax">now</text>'
            )
        if c.projection:
            colour = "var(--st-critical)" if c.projection_critical else "var(--s1)"
            grid += (
                f'<path d="{c.projection}" class="proj" stroke="{colour}" '
                'stroke-width="var(--trace-dim)" stroke-dasharray="var(--dash-3)"/>'
            )
        if c.cross:
            cx, cy, text = c.cross
            grid += (
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="var(--st-critical)"/>'
                f'<text x="{cx:.1f}" y="{cy - 6:.1f}" class="ax cross" text-anchor="middle">'
                f"{e(text)}</text>"
            )
        xt = "".join(
            f'<text x="{x:.1f}" y="211" class="ax" text-anchor="middle">{e(t)}</text>'
            for x, t in c.x_ticks
        )
        inner = gaps + grid + xt + "".join(_series(s) for s in c.series)
    return (
        '<section class="screen chart" aria-label="All windows, selected range">'
        f'<script type="application/json" id="chart-data">{c.data_json}</script>'
        '<svg id="chart" viewBox="0 0 1272 216" role="img" '
        'aria-label="Utilisation of all quota windows over the selected range">'
        f'<g class="trace">{inner}</g>'
        '<g id="hover" hidden><line class="xh" y1="14" y2="196"/></g>'
        '<rect id="sel" class="sel" y="14" height="182" hidden/></svg>'
        '<div id="readout" class="readout-box m" hidden></div></section>'
    )


def _gclass(tick: str) -> str:
    return "gz" if tick == "0" else "g"


def _series(s: SeriesView) -> str:
    width = (
        "var(--trace-hero)"
        if s.slot == 1
        else ("var(--trace-dim)" if s.slot == 6 else "var(--trace)")
    )
    dash = f' stroke-dasharray="var(--dash-{s.slot})"' if s.slot > 2 else ""
    paths = "".join(
        f'<path d="{p}" stroke="var(--s{s.slot})" stroke-width="{width}"{dash}/>' for p in s.paths
    )
    marker = (
        ""
        if s.hidden
        else (f'<circle cx="{s.end_x:.1f}" cy="{s.end_y:.1f}" r="2.4" fill="var(--s{s.slot})"/>')
    )
    state = "hidden" if s.hidden else "shown"
    return (
        paths + marker + f'<a href="{e(s.toggle_href)}" class="el-link" data-series="{e(s.key)}" '
        f'aria-label="{e(s.label)}: {state}, activate to toggle">'
        f'<text x="{s.end_x + 9:.1f}" y="{s.label_y + 4:.1f}" fill="var(--s{s.slot})" '
        f'class="el{" off" if s.hidden else ""}">{e(s.label)}</text></a>'
    )


def _history(dash: Dashboard) -> str:
    h = dash.history
    recent_on = h.sort == "recent"
    sort_recent = ' aria-sort="descending"' if recent_on else ""
    sort_consumed = ' aria-sort="descending"' if not recent_on else ""
    heads = (
        f'<th><a href="{e(h.sort_links["recent"])}" data-sort="recent"{sort_recent}>Window</a></th>'
        f'<th class="n"><a href="{e(h.sort_links["consumed"])}" data-sort="consumed"'
        f"{sort_consumed}>Session</a></th>"
        + "".join(f'<th class="n">{e(x)}</th>' for x in h.headers)
        + '<th class="sc"></th>'
    )
    cols = 3 + len(h.headers)
    if not h.rows:
        body = (
            f'<tr><td colspan="{cols}" class="empty">'
            "No session windows yet. They appear once the first session has samples.</td></tr>"
        )
    else:
        body = "".join(_history_row(r) for r in h.rows)
    caption = " by consumption" if not recent_on else ", most recent first"
    foot = ""
    if h.show_all_href:
        foot = (
            f'<tfoot><tr><td colspan="{cols}"><a href="{e(h.show_all_href)}" class="sess">'
            f"show all {h.total} windows</a></td></tr></tfoot>"
        )
    elif h.show_less_href:
        foot = (
            f'<tfoot><tr><td colspan="{cols}"><a href="{e(h.show_less_href)}" class="sess">'
            "show the first 20</a></td></tr></tfoot>"
        )
    return (
        '<section class="screen history"><table>'
        f"<caption>History — session windows{caption}. Weekly columns: change in the window, "
        "then the level it reached</caption>"
        f"<thead><tr>{heads}</tr></thead><tbody>{body}</tbody>{foot}</table></section>"
    )


def _history_row(r: SessionRowView) -> str:
    cls = " ".join(c for c in ("r-thin" if r.thin else "", "r-on" if r.selected else "") if c)
    title = f' title="{e(r.note)}"' if r.note else ""
    marks = ""
    if r.is_current:
        marks += ' <span class="far">current</span>'
    if r.badge:
        marks += f' <span class="chip stale">{e(r.badge)}</span>'
    cells = "".join(_delta_td(d, end, reset) for d, end, reset in r.columns)
    return (
        f'<tr class="{cls}"{title}><th scope="row"><a href="{e(r.href)}" class="sess" '
        f'data-session="{r.started_at}">{e(r.window_text)}</a>{marks}</th>'
        f'<td class="m n rt">{e(r.peak_text)}</td>{cells}'
        f'<td class="sc">{_spark(r)}</td></tr>'
    )


def _delta_td(delta: str, end: str, reset: bool) -> str:
    if not end:
        return '<td class="m n dim">—</td>'
    tail = f' <span class="dim">→ {e(end)}</span>'
    if reset:
        tail += ' <span class="far">(reset)</span>'
    return f'<td class="m n"><span class="rt">{e(delta)}</span>{tail}</td>'


def _spark(r: SessionRowView) -> str:
    if not r.spark:
        return ""
    return (
        '<svg class="sp" viewBox="0 0 60 18" width="60" height="18" aria-hidden="true">'
        f'<polyline points="{r.spark}" fill="none" stroke="var(--s1)" '
        'stroke-width="var(--trace-ghost)"/></svg>'
    )


def _attribution() -> str:
    return (
        '<section class="screen"><table>'
        "<caption>Attribution — local Claude Code sessions, last 24h</caption>"
        '<thead><tr><th>Project</th><th>Model</th><th class="n">Turns</th><th class="n">In</th>'
        '<th class="n">Out</th><th class="n">Cache rd</th><th class="n">Burn</th>'
        '<th class="n">Last</th>'
        '</tr></thead><tbody><tr><td colspan="8" class="empty">No local session data yet. '
        "Per-project attribution reads Claude Code's local logs; that scanner is milestone M3."
        "</td></tr></tbody></table></section>"
    )


def _side(dash: Dashboard) -> str:
    rows = "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in dash.side.items())
    spend = ""
    if dash.spend is not None:
        s = dash.spend
        if s.withheld:
            figure, pct, bar = WITHHELD, WITHHELD, '<div class="bar hatch"></div>'
            style = ""
        else:
            money = (
                f"{s.used_text} / {s.limit_text}"
                if s.used_text and s.limit_text
                else "figures hidden"
            )
            figure = f'<span class="num">{e(money)}</span><span class="dash">—</span>'
            pct = (
                f'<span class="num">{e(s.pct_text)}</span><span class="u">%</span>'
                '<span class="dash">—</span>'
            )
            fill = "var(--st-critical)" if s.state == "critical" else "var(--hair-firm)"
            bar = f'<div class="bar"><i style="width:{s.bar_pct:.1f}%;background:{fill}"></i></div>'
            style = ' style="color:var(--st-critical)"' if s.state == "critical" else ""
        state_chip = " " + chip(s.state, s.state) if s.state != "normal" else ""
        spend = (
            f'<div class="rule"></div><dl><dt>Extra usage{state_chip}</dt>'
            f'<dd class="m">{figure}</dd></dl>'
            f'<div class="v m spend-pct"{style}>{pct}</div>{bar}'
            + (f'<p class="far">{e(s.status_text)}</p>' if s.status_text else "")
        )
    diag = ""
    if dash.diagnostics:
        diag = '<div class="rule"></div><dl><dt>Diagnostics</dt><dd></dd></dl>' + "".join(
            f'<p class="far">{e(d)}</p>' for d in dash.diagnostics
        )
    return f'<aside class="side"><dl>{rows}</dl>{spend}{diag}{_events_block(dash)}</aside>'


def _footer(dash: Dashboard) -> str:
    return (
        f"<footer><span>{e(dash.footer['bind'])}</span><span>{e(dash.footer['db'])}</span>"
        f"<span>{DISCLAIMER}</span><span>QuotaLens {e(__version__)}</span></footer>"
    )
