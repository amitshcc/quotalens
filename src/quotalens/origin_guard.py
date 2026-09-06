"""One gate in front of every route: who may read the dashboard, and who may write to it.

Loopback is a *reachability* boundary, not an authentication one. Nothing off
this machine can open a TCP connection to ``127.0.0.1:8787`` -- but a web page
the user visits is running *on* this machine, in their browser, and the browser
will happily connect for it. Two things follow, and this module is both answers.
Detail and the reproductions: ``docs/SECURITY-AUDIT-2026-09-06.md`` §2, F1-F4.

**Rule 1, the `Host` allow-list, closes reads (F4).** The server used to answer
``Host: attacker.example`` with real readings. That is the whole precondition
for DNS rebinding: an attacker points a short-TTL domain at ``127.0.0.1``, the
browser re-resolves, and their page is then *same-origin* with the dashboard and
can read ``/api/quota/series``, ``/api/export.json``, ``/api/budget`` and the
rendered page outright. The same-origin policy cannot help once the origin has
been collapsed; refusing a `Host` we do not serve is what stops the collapse.
A missing `Host` is refused too: HTTP/1.1 requires one, and every client that
reaches this code -- browsers, `curl`, `urllib`, the smoke harness -- sends it.

**Rule 2, cross-site writes, closes the four state-changing routes (F1-F3).**
`POST /settings` rewrote `webhook_url` from any origin, turning the dashboard
into a permanent private feed of the user's burn rate and working hours;
`POST /settings/retention` with `confirm=1` deleted months of history, the
"confirm" being a form field a forged form supplies. The decision is made from
`Sec-Fetch-Site` and `Origin` because **a browser sets both on a plain
`<form>` POST with no script running** -- which is what lets this coexist with
the product's rule that every control works with JavaScript disabled. A
double-submit token needs a script to echo it and a `SameSite` cookie needs a
session cookie the design deliberately does not have; the header check needs
neither.

**Absent both headers is allowed, on purpose.** That is `curl`, `qa/smoke.py`'s
``POST /api/poll``, and anything else non-browser. A non-browser client is not
the adversary here: it is already running as the user, and §5 of the audit puts
"software running as you on your machine" outside the threat model. What is
being defended is the browser, which is the one client that can be made to act
by a stranger and which always announces itself.

`Referer` is not consulted anywhere: referrer policies strip it and its absence
proves nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

# Reads are not the CSRF problem, and a GET carrying a foreign `Origin` is just
# a browser navigation. Rule 1 is what closes reads.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_NAMES = ("127.0.0.1", "localhost")
# `none` is a direct navigation -- the address bar, a bookmark. `same-origin` is
# the dashboard's own form or its own fetch(). Everything else, `cross-site` and
# `same-site` included, came from another page.
SAME_SITE_VALUES = frozenset({"same-origin", "none"})

MISDIRECTED = 421  # "the Host you asked for is not one this server answers for"
FORBIDDEN = 403


def allowed_hosts(host: str) -> frozenset[str]:
    """The names this instance answers to. ``host`` is ``settings.host``."""
    return frozenset({*LOOPBACK_NAMES, host})


def _hostname(authority: str) -> str:
    """The name out of a `Host` header or an origin's authority, port removed.

    ``[::1]:8787`` keeps its brackets rather than splitting at the first colon,
    which is why this is not ``.split(":")[0]``.
    """
    authority = authority.strip().lower()
    if authority.startswith("["):
        return authority.partition("]")[0] + "]"
    return authority.partition(":")[0]


def refuse(
    method: str,
    headers: Mapping[str, str],
    hosts: frozenset[str],
    port: int,
) -> tuple[int, str] | None:
    """``(status, plain-text body)`` to refuse with, or ``None`` to let it through.

    Pure, so the rules can be read and tested without a request.
    """
    host = _hostname(headers.get("host") or "")
    if host not in hosts:
        named = " or ".join(f"http://{n}:{port}" for n in LOOPBACK_NAMES)
        return (
            MISDIRECTED,
            f"refused: the Host header was {host or 'absent'}; "
            f"this dashboard binds loopback only and answers for {named}.\n",
        )

    if method.upper() in SAFE_METHODS:
        return None

    site = (headers.get("sec-fetch-site") or "").strip().lower()
    origin = (headers.get("origin") or "").strip()
    cross = f"cross-site request refused; open the dashboard from http://127.0.0.1:{port}\n"
    if site:
        return None if site in SAME_SITE_VALUES else (FORBIDDEN, cross)
    if origin:
        # The port is not compared: rule 1 has already established that the name
        # is loopback, and a user reaching their own instance through a
        # forwarded port is not the adversary.
        parts = urlsplit(origin)
        if parts.scheme != "http" or _hostname(parts.netloc) not in hosts:
            return (FORBIDDEN, cross)
    return None
