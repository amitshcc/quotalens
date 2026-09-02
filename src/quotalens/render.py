"""HTML for the dashboard. No logic beyond escaping and layout; see :mod:`dashboard`."""

from __future__ import annotations

from html import escape as e

from quotalens import __version__
from quotalens.dashboard import Dashboard, SeriesView, WindowView

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
    '<symbol id="i-theme" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/>'
    '<path d="M8 2a6 6 0 0 1 0 12Z" fill="currentColor" stroke="none"/></symbol>'
    "</defs></svg>"
)

MARK = (
    '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke-linecap="round" '
    'aria-hidden="true"><path d="M11 2.9C13.5 8 13.5 16 11 21.1 8.5 16 8.5 8 11 2.9Z" '
    'fill="var(--txt-dim)" fill-opacity=".2" stroke="var(--txt-dim)" stroke-width="1.8" '
    'stroke-linejoin="round"/><path d="M1.6 12H8.85" stroke="var(--txt-dim)" stroke-width="1.8"/>'
    '<path d="M13.15 11.3 22.4 5.4" stroke="var(--s1)" stroke-width="2.3"/>'
    '<path d="M13.15 13 22.4 10.1" stroke="var(--s2)" stroke-width="1.9"/></svg>'
)

_CHIP = {
    "elevated": ("elev", "i-rate"),
    "critical": ("crit", "i-alert"),
    "stale": ("stale", "i-stale"),
    "unverified": ("stale", "i-stale"),
    "auth": ("auth", "i-auth"),
}
_VALUE_COLOUR = {"elevated": "var(--st-elevated)", "critical": "var(--st-critical)"}
DISCLAIMER = "Unofficial. Uses undocumented claude.ai endpoints. Observes only."


def chip(kind: str, text: str, extra_id: str = "") -> str:
    if kind not in _CHIP or not text:
        return ""
    cls, icon = _CHIP[kind]
    id_attr = f' id="{extra_id}"' if extra_id else ""
    return (
        f'<span class="chip {cls}"{id_attr}><svg class="ic" aria-hidden="true">'
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
        f'<span class="brand">{MARK}QuotaLens</span><span class="spacer"></span>'
        f"{lost}{chip(dash.chip, dash.chip_text)}"
        f'<span class="lbl m" id="polled" data-ts="{ts}" data-fallback="{e(fallback)}">'
        f"{e(dash.polled_text)}</span>"
        '<button id="t" type="button" aria-label="Switch theme"><svg class="ic" aria-hidden="true">'
        '<use href="#i-theme"/></svg>theme</button>'
        "</div></header>"
    )


def _main(dash: Dashboard) -> str:
    return (
        '<main class="wrap">'
        + _health_strip(dash)
        + _hero(dash)
        + _meters(dash)
        + _chart(dash)
        + '<div class="cols">'
        + _attribution()
        + _side(dash)
        + "</div>"
        + _footer(dash)
        + "</main>"
    )


def _health_strip(dash: Dashboard) -> str:
    if not dash.health_message and not dash.diagnostics:
        return ""
    kind = dash.epistemic.kind if dash.epistemic.kind != "ok" else "note"
    parts = []
    if dash.health_message:
        parts.append(f"<p>{e(dash.health_message)}</p>")
    for note in dash.diagnostics:
        parts.append(f'<p class="far">{e(note)}</p>')
    return f'<section class="hstrip {kind}" aria-live="polite">{"".join(parts)}</section>'


def _hero(dash: Dashboard) -> str:
    b = dash.burn
    cls = "readout off" if b.withheld else "readout"
    if b.withheld:
        value = '<span class="dash">—</span>'
    else:
        value = f'<span class="num">{e(b.rate_text)}</span><span class="dash">—</span>'
    trace = ""
    if b.trace:
        alert = (
            f'<line x1="0" y1="{b.alert_y:.1f}" x2="1272" y2="{b.alert_y:.1f}" class="gz" '
            'stroke-dasharray="2 3"/>'
            if b.alert_y is not None
            else ""
        )
        ticks = "".join(
            f'<text x="{x:.0f}" y="106" class="ax" text-anchor="{_anchor(x)}">{e(t)}</text>'
            for x, t in b.trace_ticks
        )
        trace = (
            '<svg class="htrace" viewBox="0 0 1272 108" preserveAspectRatio="none" role="img" '
            f'aria-label="Burn rate, last five hours">{alert}'
            f'<path d="{b.trace}" class="trace" stroke="var(--s1)" '
            f'stroke-width="var(--trace-hero)"/>{ticks}</svg>'
        )
    return (
        '<section class="screen hero" aria-labelledby="br"><h2 class="lbl" id="br">Burn rate'
        + (" " + chip("elevated", "elevated") if b.elevated else "")
        + '</h2><div class="hrow">'
        f'<div class="{cls}">{value}<span class="u">{e(b.unit)}</span></div>'
        f'<p class="why">{e(b.why)}</p></div>{trace}</section>'
    )


def _anchor(x: float) -> str:
    return "start" if x < 10 else ("end" if x > 1262 else "middle")


def _meters(dash: Dashboard) -> str:
    if not dash.windows:
        body = (
            '<div class="meter"><div class="lbl">No windows yet</div>'
            '<div class="v m"><span class="dash">—</span></div><div class="bar hatch"></div>'
            '<div class="foot m"><span>waiting for the first poll</span></div></div>'
        )
    else:
        body = "".join(_meter(w) for w in dash.windows)
    n = max(1, min(len(dash.windows), 4)) if dash.windows else 1
    return f'<div class="screen meters" style="--n:{n}">{body}</div>'


def _meter(w: WindowView) -> str:
    label = e(w.label) + " window" if w.slot in (1, 2) else e(w.label)
    active = ' <span class="far">active</span>' if w.is_active and not w.withheld else ""
    if w.withheld:
        return (
            f'<div class="meter is-stale" data-slot="{w.slot}"><div class="lbl">{label}</div>'
            '<div class="v m"><span class="dash">—</span></div><div class="bar hatch"></div>'
            f'<div class="foot m"><span>{e(w.resets_text)}</span><span></span></div></div>'
        )
    colour = _VALUE_COLOUR.get(w.state)
    style = f' style="color:{colour}"' if colour else ""
    bar_colour = "var(--st-critical)" if w.state == "critical" else f"var(--s{w.slot})"
    state_chip = " " + chip(w.state, w.state) if w.state != "normal" else ""
    return (
        f'<div class="meter" data-slot="{w.slot}">'
        f'<div class="lbl">{label}{state_chip}{active}</div>'
        f'<div class="v m"{style}><span class="num">{e(w.pct_text)}</span><span class="u">%</span>'
        '<span class="dash">—</span></div>'
        f'<div class="bar"><i style="width:{w.bar_pct:.1f}%;background:{bar_colour}"></i></div>'
        f'<div class="foot m"><span>{e(w.resets_text)}</span><span>{e(w.delta_text)}</span></div>'
        "</div>"
    )


def _chart(dash: Dashboard) -> str:
    c = dash.chart
    if not c.has_data:
        inner = (
            '<text x="636" y="112" class="ax" text-anchor="middle">'
            "No readings in the last 24 hours</text>"
        )
    else:
        grid = "".join(
            f'<line x1="44" y1="{y:.1f}" x2="1150" y2="{y:.1f}" class="{_gclass(t)}"/>'
            f'<text x="36" y="{y + 4:.1f}" class="ax" text-anchor="end">{e(t)}</text>'
            for y, t in c.y_ticks
        )
        xt = "".join(
            f'<text x="{x:.1f}" y="211" class="ax" text-anchor="middle">{e(t)}</text>'
            for x, t in c.x_ticks
        )
        inner = grid + xt + "".join(_series(s) for s in c.series)
    return (
        '<section class="screen chart" aria-label="All windows, last 24 hours">'
        '<svg viewBox="0 0 1272 216" role="img" '
        'aria-label="Utilisation of all quota windows over the last 24 hours">'
        f'<g class="trace">{inner}</g></svg></section>'
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
    return (
        paths + f'<circle cx="{s.end_x:.1f}" cy="{s.end_y:.1f}" r="2.4" fill="var(--s{s.slot})"/>'
        f'<text x="{s.end_x + 9:.1f}" y="{s.label_y + 4:.1f}" fill="var(--s{s.slot})" class="el">'
        f"{e(s.label)}</text>"
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
            figure = '<span class="dash">—</span>'
            pct = '<span class="dash">—</span>'
            bar = '<div class="bar hatch"></div>'
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
            bar = (
                f'<div class="bar"><i style="width:{s.bar_pct:.1f}%;background:var(--s4)"></i>'
                "</div>"
            )
        spend = (
            '<div class="rule"></div><dl><dt>Extra usage</dt>'
            f'<dd class="m">{figure}</dd></dl>'
            f'<div class="v m spend-pct">{pct}</div>{bar}'
            + (f'<p class="far">{e(s.status_text)}</p>' if s.status_text else "")
        )
    return f'<aside class="side"><dl>{rows}</dl>{spend}</aside>'


def _footer(dash: Dashboard) -> str:
    return (
        f"<footer><span>{e(dash.footer['bind'])}</span><span>{e(dash.footer['db'])}</span>"
        f"<span>{DISCLAIMER}</span><span>QuotaLens {e(__version__)}</span></footer>"
    )
