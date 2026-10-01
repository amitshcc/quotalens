"""A stand-in for claude.ai so QA can observe every collector state in a real browser.

Run:  python qa/fake_claude.py 8799
Then: QUOTALENS_BASE_URL=http://127.0.0.1:8799 quotalens --data-dir <dir> start --port 8790 ...

Switch what it returns while running:
    curl -X POST http://127.0.0.1:8799/mode/ok         # healthy payload, climbing slowly
    curl -X POST http://127.0.0.1:8799/mode/401        # cookie rejected -> auth failed
    curl -X POST http://127.0.0.1:8799/mode/drift      # unrecognisable JSON -> unverified
    curl -X POST http://127.0.0.1:8799/mode/429        # rate limited (Retry-After 30)
    curl -X POST http://127.0.0.1:8799/mode/down       # connection closed -> stale later
    curl -X POST http://127.0.0.1:8799/mode/reset      # the session window rolls over now
    curl -X POST http://127.0.0.1:8799/mode/weekreset  # the weekly windows roll over now
    curl -X POST http://127.0.0.1:8799/mode/plan-max   # a Max account (the default)
    curl -X POST http://127.0.0.1:8799/mode/plan-pro   # a Pro account: no Fable meter

The plan modes change the payload's shape, not the failure mode: they set the plan
and leave the fake healthy. ``/api/bootstrap`` answers for the same plan, so the
dashboard shows its label.

Standard library only. Binds loopback only. Not part of the installed package.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {
    "mode": "ok",
    "plan": "max",
    "pct": 20.0,
    "session_end": time.time() + 3 * 3600,
    "weekly": 40.0,
    # A stable weekly reset time in STATE, so resets_at only moves when a week actually
    # rolls over (the weekreset mode). Recomputing it each poll would drift it forward.
    "weekly_end": time.time() + 3 * 86400,
    "grant_end": time.time() + 36 * 86400,
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="microseconds")


PLANS = ("max", "pro")

# A Pro payload has never been observed. This shape is ASSUMED FROM THE HELP CENTRE,
# NOT OBSERVED (2026-10-01): on Pro and standard seats Fable runs on usage credits
# only, so there is no Fable-scoped weekly limit, and the Claude Code cloud-session
# credit is $100 rather than Max's $250. Everything else is the Max payload. If you
# are on Pro, a redacted `quotalens probe` in an issue would let us replace it.
GRANT_DOLLARS = {"max": 250, "pro": 100}
BOOTSTRAP_PLAN = {
    "max": (["chat", "claude_max"], "default_claude_max_20x"),
    "pro": (["chat", "claude_pro"], "default_claude_pro"),
}


def build_usage(
    plan: str,
    now: float,
    session_pct: float,
    session_end: float,
    weekly_pct: float,
    weekly_end: float,
    grant_end: float,
) -> dict:
    """The usage payload for ``plan``; pure, so the test fixtures come from it too."""
    resets = _iso(session_end)
    weekly_resets = _iso(weekly_end)
    grant_limit = GRANT_DOLLARS[plan]
    grant_used = round(grant_limit * 0.085, 2)
    limits = [
        {
            "kind": "session",
            "group": "session",
            "percent": session_pct,
            "resets_at": resets,
            "scope": None,
            "severity": "normal",
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": weekly_pct,
            "resets_at": weekly_resets,
            "scope": None,
            "severity": "normal",
            "is_active": False,
        },
    ]
    if plan == "max":
        limits.append(
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 66,
                "resets_at": weekly_resets,
                "scope": {"model": {"display_name": "Fable", "id": None}, "surface": None},
                "severity": "normal",
                "is_active": False,
            }
        )
    return {
        "five_hour": {"utilization": session_pct, "resets_at": resets},
        "seven_day": {"utilization": weekly_pct, "resets_at": weekly_resets},
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        # A credit grant, not a quota window: a dollar credit with an expiry, never charted.
        "iguana_necktie": {
            "utilization": 8.5,
            "resets_at": _iso(grant_end),
            "limit_dollars": grant_limit,
            "used_dollars": grant_used,
            "remaining_dollars": round(grant_limit - grant_used, 2),
            "locked_reason": None,
        },
        # The vendor's own split of this week's usage by surface (shares sum to 100).
        "seven_day_breakdown": {
            "as_of": _iso(now),
            "window_started_at": _iso(weekly_end - 7 * 86400),
            "rows": [
                {"key": "claude_code", "display_name": "Claude Code", "percent": 23},
                {"key": "chat", "display_name": "Chats", "percent": 1},
                {"key": "cowork", "display_name": "Cowork", "percent": 76},
                {"key": "other", "display_name": "Other", "percent": 0},
            ],
        },
        "limits": limits,
        "extra_usage": {
            "used_credits": 316,
            "monthly_limit": 200,
            "currency": "USD",
            "decimal_places": 2,
            "is_enabled": False,
            "disabled_reason": "org_level_disabled_until",
            "spend_limit_reached": True,
        },
        "spend": {
            "used": {"amount_minor": 316, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 200, "currency": "USD", "exponent": 2},
            "percent": 100,
        },
    }


def build_bootstrap(plan: str, org_id: str) -> dict:
    """Just enough of /api/bootstrap for the plan: one membership, the active org."""
    capabilities, tier = BOOTSTRAP_PLAN[plan]
    organization = {
        "uuid": org_id,
        "capabilities": capabilities,
        "rate_limit_tier": tier,
        "billing_type": "stripe_subscription",
    }
    return {"account": {"lastActiveOrgId": org_id, "memberships": [{"organization": organization}]}}


def _usage() -> dict:
    now = time.time()
    if now >= STATE["session_end"]:
        STATE["session_end"] = now + 5 * 3600
        STATE["pct"] = 0.0
    STATE["pct"] = min(100.0, STATE["pct"] + 0.5)  # half a point per poll
    STATE["weekly"] = min(100.0, STATE["weekly"] + 0.1)
    return build_usage(
        STATE["plan"],
        now,
        round(STATE["pct"], 1),
        STATE["session_end"],
        round(STATE["weekly"], 1),
        STATE["weekly_end"],
        STATE["grant_end"],
    )


def _cookie_org(cookie: str) -> str:
    match = re.search(r"lastActiveOrg=([A-Za-z0-9\-]+)", cookie or "")
    return match.group(1) if match else "fake-org-0000-0000"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path.startswith("/mode/"):
            mode = self.path.split("/", 2)[2]
            if mode == "reset":
                STATE["session_end"] = time.time()
                mode = "ok"
            elif mode == "weekreset":
                # Roll the weekly windows over: a new resets_at seven days out and the
                # level back to zero. The poller sees resets_at move and records a
                # week_reset event, so the whole ledger path runs end to end.
                STATE["weekly_end"] = time.time() + 7 * 86400
                STATE["weekly"] = 0.0
                mode = "ok"
            elif mode.startswith("plan-") and mode[5:] in PLANS:
                STATE["plan"] = mode[5:]
                mode = "ok"
            STATE["mode"] = mode
            self._send(200, json.dumps({"mode": STATE["mode"]}).encode())
            return
        self._send(404, b"{}")

    def do_GET(self) -> None:
        mode = STATE["mode"]
        if self.path == "/mode":
            self._send(200, json.dumps(STATE).encode())
            return
        if mode == "down":
            self.connection.close()
            return
        if mode == "401":
            error = {"type": "authentication_error", "message": "Invalid session"}
            self._send(401, json.dumps({"type": "error", "error": error}).encode())
            return
        if mode == "429":
            self._send(429, b'{"error":"rate limited"}', extra={"Retry-After": "30"})
            return
        if self.path == "/api/bootstrap":
            org = _cookie_org(self.headers.get("Cookie", ""))
            self._send(200, json.dumps(build_bootstrap(STATE["plan"], org)).encode())
            return
        if self.path.endswith("/usage"):
            if mode == "drift":
                drifted = {"message": "usage moved", "status": "maintenance"}
                self._send(200, json.dumps(drifted).encode())
                return
            self._send(200, json.dumps(_usage()).encode())
            return
        if self.path.endswith("/overage_spend_limit"):
            self._send(404, b'{"error":"not here"}')
            return
        self._send(404, b"{}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"fake claude.ai on http://127.0.0.1:{port}  (POST /mode/<name>, names in the docstring)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
