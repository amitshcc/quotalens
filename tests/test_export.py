"""Export: derived rows freely, raw responses only on request, always streamed."""

from __future__ import annotations

import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import COOKIE
from quotalens.api import create_app
from quotalens.export import EXPORTS, ExportError, csv_stream, resolve
from quotalens.parse import QuotaReading, SpendReading
from quotalens.store import Store

SECRET = "sk-ant-sid01-SECRETSECRETSECRET-abc"


def _seed(store: Store, now: int, rows: int = 40) -> None:
    for i in range(rows):
        ts = now - (rows - i) * 60
        store.record_quota(ts, [QuotaReading("five_hour", "5-hour", i, "r1", "normal", True)])
        store.record_sample(ts, "usage", {"five_hour": {"utilization": i}, "cookie": COOKIE})
    store.record_event("burn_alert", "crossed the line", ts=now)
    store.record_overage(now, SpendReading(316, 200, 2, "USD", "spend"))


def _client(settings, store, secrets) -> TestClient:
    return TestClient(create_app(settings, store, secrets))


def test_csv_round_trips(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        response = tc.get("/api/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in response.headers["content-disposition"]
    parsed = list(csv.DictReader(io.StringIO(response.text)))
    assert len(parsed) == 40
    assert parsed[0]["window"] == "five_hour" and parsed[0]["label"] == "5-hour"
    assert [int(float(r["pct"])) for r in parsed] == list(range(40))
    assert parsed[0]["severity"] == "normal" and parsed[0]["is_active"] == "1"


def test_json_is_one_valid_document(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/export.json").json()  # parses, so the streaming is well formed
    assert body["table"] == "quota" and body["columns"][0] == "ts"
    assert len(body["rows"]) == 40 and body["rows"][0]["window"] == "five_hour"
    assert "warning" not in body  # derived rows need no warning


def test_every_table_exports_and_an_unknown_one_is_rejected(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        for table in ("quota", "events", "overage", "sessions"):
            body = tc.get(f"/api/export.json?table={table}").json()
            assert body["table"] == EXPORTS[table].table
        assert tc.get("/api/export.csv?table=quota;DROP TABLE quota").status_code == 400
        assert tc.get("/api/export.json?table=nope").status_code == 400
    assert set(EXPORTS) == {"quota", "events", "overage", "sessions", "samples"}


def test_raw_samples_need_the_flag_and_carry_the_warning(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    with _client(settings, store, secrets) as tc:
        refused = tc.get("/api/export.json?table=samples")
        assert refused.status_code == 400
        assert "raw=1" in refused.json()["detail"] and "redact" in refused.json()["detail"]

        allowed = tc.get("/api/export.json?table=samples&raw=1")
        body = allowed.json()
        assert "redact before sharing" in body["warning"]
        assert "redact before sharing" in allowed.headers["x-quotalens-warning"]
        assert len(body["rows"]) == 40 and body["rows"][0]["keysig"] == "cookie,five_hour"


def test_the_cookie_never_reaches_an_export(settings, store, secrets) -> None:
    """A payload that somehow contained a cookie is still not an export of one."""
    now = int(time.time())
    _seed(store, now)  # the seeded payloads deliberately embed the cookie value
    with _client(settings, store, secrets) as tc:
        for path in ("/api/export.csv", "/api/export.json", "/api/export.csv?table=events"):
            assert SECRET not in tc.get(path).text
        raw = tc.get("/api/export.json?table=samples&raw=1")
    assert SECRET in raw.text  # only here, only behind the flag, only with the warning
    assert "redact" in raw.headers["x-quotalens-warning"]


def test_hours_narrows_the_window(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now, rows=200)
    with _client(settings, store, secrets) as tc:
        every = tc.get("/api/export.json").json()["rows"]
        recent = tc.get("/api/export.json?hours=0.5").json()["rows"]
    assert len(every) == 200 and 25 <= len(recent) <= 31
    assert tuple(r["ts"] for r in recent) == tuple(sorted(r["ts"] for r in recent))


def test_the_exporter_pages_rather_than_buffering(settings, store, secrets) -> None:
    """A month of history must not become a month of memory."""
    now = int(time.time())
    _seed(store, now, rows=1200)
    queries: list[str] = []
    original = store.query

    def counting(sql: str, params=()):
        queries.append(sql)
        return original(sql, params)

    store.query = counting  # type: ignore[method-assign]
    spec = resolve("quota", raw_allowed=False)
    chunks = list(csv_stream(store, spec, None))
    assert len(chunks) == 1201  # a header plus one per row, yielded as it goes
    assert len(queries) >= 3  # 1200 rows at a 500-row page
    assert all("LIMIT 500" in q and "_rid" in q for q in queries)


def test_resolve_guards_the_raw_table_directly() -> None:
    with pytest.raises(ExportError, match="raw=1"):
        resolve("samples", raw_allowed=False)
    assert resolve("samples", raw_allowed=True).raw is True
    with pytest.raises(ExportError, match="unknown table"):
        resolve("secrets", raw_allowed=True)


def test_json_export_of_an_empty_table_is_still_valid(settings, store, secrets) -> None:
    with _client(settings, store, secrets) as tc:
        body = tc.get("/api/export.json?table=events").json()
        text = tc.get("/api/export.csv?table=events").text
    assert body["rows"] == []
    assert json.dumps(body)  # round-trips
    assert text.strip() == "ts,kind,detail"
