"""Whether each vendor's own API is up, from each vendor's own status page.

**A status vendor is not a :class:`~quotalens.config.Provider`.** A provider has
a client, a cookie, a parser and a store schema; a status vendor has a URL and a
state word. Gemini and OpenAI are not providers today, and making them
half-providers with no client is how that seam rots -- so this is a separate,
small list, and Claude's entry borrows only its display name from ``CLAUDE`` so
the vendor is still named once.

**Gemini is not here, and that is the finished answer rather than a gap.**
``aistudio.google.com/status`` is a JavaScript app with no JSON behind it, and
Google Cloud's ``products.json`` carries only Vertex surfaces -- "Vertex Gemini
API", "Gemini Code Assist" -- which have their own availability records and are
not the AI Studio one. A row that can only ever say "unable to check" is a row
that costs a line and teaches nothing, so it was removed. ``api_url`` staying
optional is the whole seam: a Gemini entry is four lines the day Google ships a
feed.

**The logos are third-party marks and are not ours to draw.** Each vendor's own
brand file goes in ``quotalens/web/vendor/`` under the name its ``StatusVendor``
gives, taken from that vendor's brand page and shipped unmodified -- never
hand-redrawn, never traced from a screenshot, never restyled to match the theme.
A missing file is a supported state: the row renders with the name alone, which
is what it looked like before logos existed. DESIGN.md 8 records why this is a
deliberate exception to the icon rules.

"Unable to check" is still an ordinary state, not an edge case -- every vendor
enters it whenever this machine is offline. It takes DESIGN.md 5's treatment
exactly: the value is removed and explained, never frozen at the last good
reading and never assumed healthy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from quotalens.config import CLAUDE

log = logging.getLogger(__name__)

CHECK_EVERY_S = 300  # its own timer; a status check must never delay a quota poll
CHECK_TIMEOUT_S = 5.0
FAILURES_BEFORE_UNKNOWN = 2  # one dropped packet is not an outage

# DESIGN.md 5's states, not four coloured circles. Every one carries a word.
OK = "ok"
DEGRADED = "degraded"
OUTAGE = "outage"
MAINTENANCE = "maintenance"
UNKNOWN = "unknown"

INDICATOR_STATE = {
    "none": OK,
    "minor": DEGRADED,
    "major": OUTAGE,
    "critical": OUTAGE,
    "maintenance": MAINTENANCE,
}

NO_API_REASON = "no public status API"
UNREACHABLE_REASON = "unreachable"


class StatusFetchError(RuntimeError):
    """The feed answered, but not with something usable."""


@dataclass(frozen=True)
class StatusVendor:
    """One row. The two URLs are separate values, never derived from each other.

    They happen to share a host for two of the three, and for Gemini there is no
    API URL at all -- so deriving one from the other by string surgery would be a
    coincidence dressed up as a rule.
    """

    key: str
    display_name: str
    page_url: str  # the human page: what the row links to
    api_url: str | None  # the machine feed, or None when the vendor ships none
    # A file in quotalens/web/vendor/, or None. Absent is a supported state and
    # the row renders with the name alone: a third-party brand file is not ours
    # to redraw or to invent.
    logo: str | None = None

    @property
    def logo_url(self) -> str | None:
        return f"/static/vendor/{self.logo}" if self.logo else None


VENDORS: tuple[StatusVendor, ...] = (
    StatusVendor(
        key="claude",
        display_name=CLAUDE.display_name,
        page_url="https://status.claude.com/",
        api_url="https://status.claude.com/api/v2/status.json",
        logo="claude.svg",
    ),
    StatusVendor(
        key="openai",
        display_name="OpenAI",
        page_url="https://status.openai.com/",
        api_url="https://status.openai.com/api/v2/status.json",
        logo="openai.svg",
    ),
)
VENDORS_BY_KEY = {v.key: v for v in VENDORS}


@dataclass(frozen=True)
class VendorStatus:
    """What one row says. ``state`` is UNKNOWN when there is nothing to say."""

    vendor: StatusVendor
    state: str
    detail: str = ""  # the vendor's own description, or why we cannot tell
    checked_at: int | None = None

    @property
    def word(self) -> str:
        """The text channel, which is the one that works for everyone."""
        return "—" if self.state == UNKNOWN else self.state

    @property
    def title(self) -> str:
        """Hover text: the vendor's own words, then where the click goes."""
        head = self.detail or self.state
        return f"{self.vendor.display_name}: {head}. Opens {self.vendor.page_url}"


def parse_statuspage(payload: object) -> tuple[str, str]:
    """Atlassian Statuspage v2 ``status.json`` to (state, description).

    An unrecognised indicator is UNKNOWN, not OK. Guessing healthy from a shape
    we do not understand is the one failure this row must not have.
    """
    if not isinstance(payload, dict):
        return UNKNOWN, "unrecognised response"
    status = payload.get("status")
    if not isinstance(status, dict):
        return UNKNOWN, "no status in response"
    indicator = str(status.get("indicator", "")).lower()
    description = str(status.get("description", "") or "")
    state = INDICATOR_STATE.get(indicator)
    if state is None:
        return UNKNOWN, f"unrecognised indicator {indicator!r}"
    return state, description


def fetch(url: str, timeout_s: float = CHECK_TIMEOUT_S) -> object:
    """One plain GET. No cookie, no identifier, no query string.

    Through ``curl_cffi``, which is already a dependency for the provider client
    and ships its own CA bundle. Stdlib ``urllib`` uses the interpreter's trust
    store, and a Python installed without one -- which is easy to end up with on
    macOS -- fails every request with CERTIFICATE_VERIFY_FAILED. Observed here.
    Every row would then read "unreachable" forever, blaming three vendors for a
    local misconfiguration.
    """
    from curl_cffi import requests

    response = requests.get(url, timeout=timeout_s, headers={"Accept": "application/json"})
    if response.status_code >= 400:
        raise StatusFetchError(f"HTTP {response.status_code}")
    return json.loads(response.text)


@dataclass
class StatusWatcher:
    """Polls the vendor feeds on its own timer and caches the answers in memory.

    Deliberately not on the quota poller's schedule. A status check must never
    delay, block or fail a quota poll -- that is the one job this tool has -- so
    this owns its own interval and its own timeout, and the dashboard reads
    whatever is cached without ever waiting on a network call.
    """

    enabled: bool = True
    vendors: tuple[StatusVendor, ...] = VENDORS
    _cache: dict[str, VendorStatus] = field(default_factory=dict)
    _failures: dict[str, int] = field(default_factory=dict)
    last_check_ts: int | None = None

    def rows(self) -> list[VendorStatus]:
        """What to render. Never performs I/O."""
        if not self.enabled:
            return []
        out = []
        for vendor in self.vendors:
            cached = self._cache.get(vendor.key)
            if cached is not None:
                out.append(cached)
            elif vendor.api_url is None:
                out.append(VendorStatus(vendor, UNKNOWN, NO_API_REASON))
            else:
                out.append(VendorStatus(vendor, UNKNOWN, "not checked yet"))
        return out

    def due(self, now: int) -> bool:
        return self.enabled and (
            self.last_check_ts is None or now - self.last_check_ts >= CHECK_EVERY_S
        )

    def check_all(self, now: int, fetcher: object = None) -> None:
        """Blocking; call from a worker thread. Never raises."""
        if not self.enabled:
            # Turning it off stops the requests, not just the row: a user who
            # chose a loopback-only tool is entitled to that being literal.
            return
        self.last_check_ts = now
        for vendor in self.vendors:
            self._check_one(vendor, now, fetcher or fetch)

    def _check_one(self, vendor: StatusVendor, now: int, fetcher: object) -> None:
        if vendor.api_url is None:
            # No request is made at all -- not one that fails and is reported as
            # unreachable, which would be a different and untrue claim.
            self._cache[vendor.key] = VendorStatus(vendor, UNKNOWN, NO_API_REASON, now)
            return
        try:
            payload = fetcher(vendor.api_url)  # type: ignore[operator]
        except Exception as exc:  # a status check must never propagate anywhere
            self._record_failure(vendor, now, exc)
            return
        state, description = parse_statuspage(payload)
        if state == UNKNOWN:
            self._record_failure(vendor, now, description)
            return
        self._failures[vendor.key] = 0
        self._cache[vendor.key] = VendorStatus(vendor, state, description, now)

    def _record_failure(self, vendor: StatusVendor, now: int, why: object) -> None:
        """Two failures before declaring it, matching app.js's watchdog discipline."""
        count = self._failures.get(vendor.key, 0) + 1
        self._failures[vendor.key] = count
        log.debug("status check for %s failed (%d): %s", vendor.key, count, why)
        if count < FAILURES_BEFORE_UNKNOWN and vendor.key in self._cache:
            return  # keep the previous answer for one more round
        self._cache[vendor.key] = VendorStatus(vendor, UNKNOWN, UNREACHABLE_REASON, now)


def selected_vendors(keys: str | None) -> tuple[StatusVendor, ...]:
    """The vendors named in the setting, in the canonical order.

    An unknown key is dropped rather than raising: a config file naming a vendor
    a later version removed should not stop the dashboard rendering.
    """
    if not keys:
        return VENDORS
    wanted = {k.strip().lower() for k in keys.split(",") if k.strip()}
    return tuple(v for v in VENDORS if v.key in wanted)
