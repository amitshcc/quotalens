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
