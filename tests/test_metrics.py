"""/metrics: the grammar, not just the substrings. Prometheus will scrape whatever
we emit and present it as fact, so a malformed exposition is worse than none."""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from quotalens.api import create_app
from quotalens.metrics import (
    CONTENT_TYPE,
    Family,
    collect,
    escape_help,
    escape_label,
    format_value,
    render,
)
from quotalens.parse import QuotaReading, SpendReading

SAMPLE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)$")
LABEL = re.compile(r'^(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"$')


def parse_exposition(text: str) -> dict[str, dict]:
    """A small, strict reader: every rule it enforces is one Prometheus enforces."""
    assert text.endswith("\n"), "the exposition must end with a newline"
    assert not text.endswith("\n\n"), "and with exactly one"
    families: dict[str, dict] = {}
    order: list[str] = []
    for line in text.split("\n")[:-1]:
        assert line, "no blank lines"
        if line.startswith("# HELP "):
            name, _, help_text = line[len("# HELP ") :].partition(" ")
            assert name not in families, f"{name} declared twice"
            families[name] = {"help": help_text, "type": None, "samples": []}
            order.append(name)
            continue
        if line.startswith("# TYPE "):
            name, _, kind = line[len("# TYPE ") :].partition(" ")
            assert name in families, f"TYPE before HELP for {name}"
            assert families[name]["type"] is None, f"{name} typed twice"
            assert kind in {"gauge", "counter", "histogram", "summary", "untyped"}
            families[name]["type"] = kind
            continue
        assert not line.startswith("#"), f"unknown comment line: {line}"
        match = SAMPLE.match(line)
        assert match, f"not a sample line: {line!r}"
        name = match["name"]
        assert name in families, f"sample {name} has no HELP"
        assert families[name]["type"] is not None, f"sample {name} has no TYPE"
        labels = {}
        if match["labels"]:
            for part in _split_labels(match["labels"]):
                lm = LABEL.match(part)
                assert lm, f"bad label pair: {part!r}"
                labels[lm["key"]] = _unescape(lm["value"])
        value = match["value"]
        assert value in {"NaN", "+Inf", "-Inf"} or _is_number(value), f"bad value {value!r}"
        families[name]["samples"].append((labels, value))
    assert order == sorted(order, key=order.index)
    return families


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), text)


def _split_labels(text: str) -> list[str]:
    parts, depth, current = [], False, ""
    for char in text:
        if char == '"' and not current.endswith("\\"):
            depth = not depth
        if char == "," and not depth:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


# -- the primitives ----------------------------------------------------------------


def test_escaping() -> None:
    assert escape_label('a"b') == 'a\\"b'
    assert escape_label("a\\b") == "a\\\\b"
    assert escape_label("a\nb") == "a\\nb"
    assert escape_help('a"b') == 'a"b'  # a quote is legal in help text
    assert escape_help("a\\b\nc") == "a\\\\b\\nc"


def test_values() -> None:
    assert format_value(None) == "NaN"
    assert format_value(float("nan")) == "NaN"
    assert format_value(float("inf")) == "+Inf"
    assert format_value(0) == "0"
    assert format_value(42.0) == "42"
    assert format_value(1.5) == "1.5"
    assert format_value(-3.25) == "-3.25"
    assert format_value(1_788_000_000) == "1788000000"


def test_a_family_renders_help_type_then_samples() -> None:
    family = Family("thing_total", "counter", "A thing.")
    family.add(2, window='five"hour')
    lines = list(family.render())
    assert lines[0] == "# HELP quotalens_thing_total A thing."
    assert lines[1] == "# TYPE quotalens_thing_total counter"
    assert lines[2] == 'quotalens_thing_total{window="five\\"hour"} 2'


def test_render_refuses_a_duplicate_family() -> None:
    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        render([Family("a", "gauge", "x"), Family("a", "gauge", "y")])


# -- the whole exposition ----------------------------------------------------------


def _seed(store, now: int) -> None:
    reset = datetime.fromtimestamp(now + 3600, UTC).isoformat()
    for i in range(20):
        store.record_quota(
            now - (19 - i) * 60,
            [
                QuotaReading("five_hour", "5-hour", 40 + i, reset, "normal", True),
                QuotaReading("seven_day", '7-day "all"', 30, reset),
            ],
        )
    store.record_overage(now, SpendReading(316, 200, 2, "USD", "spend"))


def test_the_exposition_parses_and_says_what_it_should(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now
    app.state.qw.poller.status.polls_ok = 7
    with TestClient(app) as tc:
        response = tc.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE

    families = parse_exposition(response.text)
    assert families["quotalens_up"]["samples"] == [({}, "1")]
    assert families["quotalens_poll_success_total"]["type"] == "counter"
    assert families["quotalens_poll_success_total"]["samples"] == [({}, "7")]

    quota = dict(
        (labels["window"], value)
        for labels, value in families["quotalens_quota_percent"]["samples"]
    )
    assert quota["five_hour"] == "59" and quota["seven_day"] == "30"
    labels = [lb for lb, _ in families["quotalens_quota_percent"]["samples"]]
    assert any(lb["label"] == '7-day "all"' for lb in labels)  # the quote survived the round trip

    burn = families["quotalens_burn_pts_per_hour"]["samples"][0][1]
    assert math.isclose(float(burn), 60.0, rel_tol=0.05)
    assert families["quotalens_session_headroom_percent"]["samples"][0][1] == "41"
    spend = families["quotalens_spend_used_minor"]["samples"][0]
    assert spend[0]["currency"] == "USD" and spend[1] == "316"
    rows = {lb["table"]: v for lb, v in families["quotalens_rows"]["samples"]}
    assert rows["quota"] == "40"
    assert families["quotalens_build_info"]["samples"][0][0]["profile"] == "default"


def test_a_stale_collector_reports_nan_not_zero(settings, store, secrets) -> None:
    """A gauge that reads 0 while the collector is down is a lie Prometheus will graph."""
    now = int(time.time())
    _seed(store, now)
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now - 4 * settings.poll_interval_s
    with TestClient(app) as tc:
        text = tc.get("/metrics").text
    families = parse_exposition(text)
    assert families["quotalens_up"]["samples"] == [({}, "0")]
    assert all(v == "NaN" for _, v in families["quotalens_quota_percent"]["samples"])
    assert families["quotalens_burn_pts_per_hour"]["samples"][0][1] == "NaN"
    assert families["quotalens_session_headroom_percent"]["samples"][0][1] == "NaN"
    # counters and the last-success stamp are still true and still reported
    assert families["quotalens_last_success_timestamp_seconds"]["samples"][0][1] != "NaN"


def test_an_empty_database_still_scrapes(settings, store, secrets) -> None:
    with TestClient(create_app(settings, store, secrets)) as tc:
        text = tc.get("/metrics").text
    families = parse_exposition(text)
    assert families["quotalens_up"]["samples"] == [({}, "0")]
    assert families["quotalens_quota_percent"]["samples"] == []
    assert families["quotalens_last_success_timestamp_seconds"]["samples"] == [({}, "NaN")]


def test_collect_is_ordered_and_prefixed(settings, store, secrets) -> None:
    now = int(time.time())
    _seed(store, now)
    families = collect(settings, store, app_status(now), now)
    names = [f.name for f in families]
    assert len(names) == len(set(names))
    assert names[0] == "build_info" and "up" in names
    assert all(not f.name.startswith("quotalens_") for f in families)  # the prefix is added once
    assert render(families).startswith("# HELP quotalens_build_info ")


def app_status(now: int):
    from quotalens.poller import PollerStatus

    status = PollerStatus()
    status.last_success_ts = now
    return status


# -- a gauge for a window the same scrape has already called unknown -------------


def _families(settings, store, secrets, now: int, last_ok: int | None = None):
    app = create_app(settings, store, secrets)
    app.state.qw.poller.status.state = "ok"
    app.state.qw.poller.status.last_success_ts = now if last_ok is None else last_ok
    with TestClient(app) as tc:
        return parse_exposition(tc.get("/metrics").text)


def _one(families, name: str) -> str:
    return families[f"quotalens_{name}"]["samples"][0][1]


def test_a_lapsed_session_withholds_the_headroom_and_the_burn_rate_too(
    settings, store, secrets
) -> None:
    """`quota_percent{five_hour} NaN` beside `session_headroom_percent 60`, observed.

    The window's reset has passed and no new one has opened, so its last value is
    not this window's percentage -- which the scrape said about the percentage
    and then contradicted twice in the same response.
    """
    now = int(time.time())
    lapsed = datetime.fromtimestamp(now - 120, UTC).isoformat()
    live = datetime.fromtimestamp(now + 3600, UTC).isoformat()
    for i in range(20):
        store.record_quota(
            now - (19 - i) * 60,
            [
                QuotaReading("five_hour", "5-hour", 40 + i, lapsed, "normal", True),
                QuotaReading("seven_day", "7-day", 30, live),
            ],
        )
    families = _families(settings, store, secrets, now)
    quota = {lb["window"]: v for lb, v in families["quotalens_quota_percent"]["samples"]}
    assert quota["five_hour"] == "NaN"
    assert quota["seven_day"] == "30", "a healthy neighbour is untouched"
    assert _one(families, "session_headroom_percent") == "NaN"
    assert _one(families, "burn_pts_per_hour") == "NaN"
    assert families["quotalens_up"]["samples"] == [({}, "1")], "the collector itself is fine"


def test_a_session_block_that_stopped_arriving_withholds_the_same_three(
    settings, store, secrets
) -> None:
    """Per-window staleness: `five_hour` goes dark while its neighbours refresh.

    A healthy collector is not evidence that every meter is current, and
    `burn_pts_per_hour 908.571` computed from rows that stopped moving is the
    number somebody alerts on.
    """
    now = int(time.time())
    reset = datetime.fromtimestamp(now + 3600, UTC).isoformat()
    stale_at = now - 4 * settings.poll_interval_s
    for i in range(10):  # both windows, then five_hour stops
        store.record_quota(
            stale_at - (9 - i) * 60,
            [
                QuotaReading("five_hour", "5-hour", 40 + i, reset, "normal", True),
                QuotaReading("seven_day", "7-day", 30, reset),
            ],
        )
    for i in range(5):
        store.record_quota(now - (4 - i) * 60, [QuotaReading("seven_day", "7-day", 31, reset)])
    families = _families(settings, store, secrets, now)
    quota = {lb["window"]: v for lb, v in families["quotalens_quota_percent"]["samples"]}
    assert quota["five_hour"] == "NaN" and quota["seven_day"] == "31"
    assert _one(families, "session_headroom_percent") == "NaN"
    assert _one(families, "burn_pts_per_hour") == "NaN"


def test_the_spend_gauges_take_the_collector_gate(settings, store, secrets) -> None:
    """They had none at all: a collector down for hours kept publishing its last figure."""
    now = int(time.time())
    _seed(store, now)
    fresh = _families(settings, store, secrets, now)
    assert _one(fresh, "spend_used_minor") == "316" and _one(fresh, "spend_limit_minor") == "200"
    stale = _families(settings, store, secrets, now, last_ok=now - 4 * settings.poll_interval_s)
    assert _one(stale, "spend_used_minor") == "NaN"
    assert _one(stale, "spend_limit_minor") == "NaN"


def test_metrics_and_the_budget_api_agree_about_a_boost(settings, store, secrets) -> None:
    """/metrics printed `weekly_windows_remaining 2.5` where the page said "4 so far".

    A threshold counted before its exclusions: `compute_budgets` was called with
    no `boost_ts`, and the word "boost" did not occur in metrics.py. Both callers
    now read the events through `boost.recorded`, so they cannot drift again.
    """
    from quotalens.boost import BOOST_KIND
    from quotalens.runway import SESSION_LENGTH_S
    from quotalens.sessions import Delta, SessionWindow

    now = int(time.time())

    def window(index: int, points: float) -> SessionWindow:
        start = now - (index + 1) * SESSION_LENGTH_S
        return SessionWindow(
            start,
            start + SESSION_LENGTH_S,
            False,
            100.0,
            100.0,
            300,
            start,
            start + SESSION_LENGTH_S,
            {"seven_day": Delta(10.0, 10.0 + points, False)},
            SESSION_LENGTH_S,
        )

    # Four clean windows costing 10 points each, plus one full window that
    # "cost" almost nothing because a boost landed inside it.
    boosted = window(4, 1.0)
    store.replace_sessions([window(i, 10.0) for i in range(4)] + [boosted])
    reset = datetime.fromtimestamp(now + 3600, UTC).isoformat()
    store.record_quota(now, [QuotaReading("seven_day", "7-day", 20, reset)])

    def scrape() -> tuple[list[str], dict]:
        app = create_app(settings, store, secrets)
        app.state.qw.poller.status.state = "ok"
        app.state.qw.poller.status.last_success_ts = now
        with TestClient(app) as tc:
            families = parse_exposition(tc.get("/metrics").text)
            api = tc.get("/api/budget").json()["budgets"][0]
        return [
            v
            for lb, v in families["quotalens_weekly_windows_remaining"]["samples"]
            if lb["basis"] == "full"
        ], api

    # Without the boost recorded, five windows are usable and both surfaces
    # answer with a number -- so the assertion below is about the exclusion and
    # not about the store being too empty to answer at all.
    before, api_before = scrape()
    assert before != ["NaN"] and api_before["full_windows_remaining"] is not None
    assert api_before["usable_windows"] == 5

    store.record_event(BOOST_KIND, "Weekly fell 98% -> 60%. Limit raised.", ts=boosted.started_at)
    after, api_after = scrape()
    assert after == ["NaN"], "five windows, one boosted, is four usable -- below the threshold"
    assert api_after["full_windows_remaining"] is None
    assert api_after["usable_windows"] == 4
    assert "4 so far" in api_after["reason"], api_after["reason"]
