"""A stand-in for claude.ai so QA can observe every collector state in a real browser.

Run:  python qa/fake_claude.py 8799
Then: QUOTALENS_BASE_URL=http://127.0.0.1:8799 quotalens --data-dir <dir> start --port 8790 ...

Switch what it returns while running:
    curl -X POST http://127.0.0.1:8799/mode/ok         # healthy payload, climbing slowly
    curl -X POST http://127.0.0.1:8799/mode/401        # cookie rejected -> auth failed
    curl -X POST http://127.0.0.1:8799/mode/drift      # unrecognisable JSON -> unverified
    curl -X POST http://127.0.0.1:8799/mode/429        # rate limited (Retry-After 30)
    curl -X POST http://127.0.0.1:8799/mode/down       # connection closed -> unreachable, then stale
    curl -X POST http://127.0.0.1:8799/mode/reset      # the session window rolls over now

Standard library only. Binds loopback only. Not part of the installed package.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = {"mode": "ok", "pct": 20.0, "session_end": time.time() + 3 * 3600, "weekly": 40.0}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="microseconds")


def _usage() -> dict:
    now = time.time()
    if now >= STATE["session_end"]:
        STATE["session_end"] = now + 5 * 3600
        STATE["pct"] = 0.0
    STATE["pct"] = min(100.0, STATE["pct"] + 0.5)  # half a point per poll
    STATE["weekly"] = min(100.0, STATE["weekly"] + 0.1)
    end = STATE["session_end"]
    weekly_end = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    pct = round(STATE["pct"], 1)
    return {
        "five_hour": {"utilization": pct, "resets_at": _iso(end)},
        "seven_day": {"utilization": round(STATE["weekly"], 1), "resets_at": weekly_end},
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        "limits": [
            {
                "kind": "session",
                "percent": pct,
                "resets_at": _iso(end),
                "scope": None,
                "severity": "normal",
                "is_active": True,
            },
            {
                "kind": "weekly_scoped",
                "percent": 66,
                "resets_at": weekly_end,
                "scope": {"model": {"display_name": "Fable", "id": None}},
                "severity": "normal",
                "is_active": False,
            },
        ],
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
            body = {"type": "error", "error": {"type": "authentication_error", "message": "Invalid session"}}
            self._send(401, json.dumps(body).encode())
            return
        if mode == "429":
            self._send(429, b'{"error":"rate limited"}', extra={"Retry-After": "30"})
            return
        if self.path.endswith("/usage"):
            if mode == "drift":
                self._send(200, json.dumps({"message": "usage moved", "status": "maintenance"}).encode())
                return
            self._send(200, json.dumps(_usage()).encode())
            return
        if self.path.endswith("/overage_spend_limit"):
            self._send(404, b'{"error":"not here"}')
            return
        self._send(404, b"{}")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    print(f"fake claude.ai on http://127.0.0.1:{port}  (POST /mode/<ok|401|drift|429|down|reset>)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
