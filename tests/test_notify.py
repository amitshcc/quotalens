"""Threshold notifications: the three properties inherited from the burn alert."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from quotalens import notify


def test_fires_once_on_the_way_up_not_on_every_poll() -> None:
    """Otherwise: one notification a minute, for hours."""
    assert notify.crossings(pct=80, previous_pct=40, already_fired=set()) == [50.0, 75.0]
    assert notify.crossings(pct=80.5, previous_pct=80, already_fired={50.0, 75.0}) == []


def test_falling_back_below_a_threshold_does_not_fire() -> None:
    assert notify.crossings(pct=40, previous_pct=80, already_fired=set()) == []


def test_an_unknown_percentage_crosses_nothing() -> None:
    """A stale or unverified meter has no value; DESIGN.md 5 removes it."""
    assert notify.crossings(pct=None, previous_pct=40, already_fired=set()) == []


def test_the_first_reading_of_a_window_arms_rather_than_fires() -> None:
    """Otherwise a restart at 80% announces 50 and 75 as if they just happened."""
    assert notify.crossings(pct=80, previous_pct=None, already_fired=set()) == []


def test_a_threshold_that_already_fired_stays_quiet_across_a_restart() -> None:
    details = ["five_hour@R1 crossed 50", "five_hour@R1 crossed 75"]
    fired = notify.fired_thresholds(details, "five_hour", "R1")
    assert fired == {50.0, 75.0}
    assert notify.crossings(pct=80, previous_pct=40, already_fired=fired) == []


def test_a_new_window_re_arms_every_threshold() -> None:
    """The key is the window's own reset time, so R2 knows nothing about R1."""
    details = ["five_hour@R1 crossed 50", "five_hour@R1 crossed 75"]
    assert notify.fired_thresholds(details, "five_hour", "R2") == set()


def test_one_window_does_not_silence_another() -> None:
    details = ["five_hour@R1 crossed 50"]
    assert notify.fired_thresholds(details, "seven_day", "R1") == set()


def test_thresholds_parse_and_a_bad_entry_is_dropped_not_guessed() -> None:
    assert notify.parse_thresholds("60, 80") == (60.0, 80.0)
    assert notify.parse_thresholds("60, banana, 80") == (60.0, 80.0)
    assert notify.parse_thresholds("0, 150, 50") == (50.0,)  # out of range


def test_an_empty_configured_list_means_none_not_the_default() -> None:
    """Switching every threshold off used to hand them straight back.

    Both `""` and `None` returned DEFAULT_THRESHOLDS, so a user who disabled all
    three got 50, 75 and 90 again on the next poll. The documented default
    belongs to the key being *absent*, which is where ConfigKey.default supplies
    it -- an empty list is a setting, not a missing one.
    """
    assert notify.parse_thresholds("") == ()
    assert notify.parse_thresholds(None) == ()
    assert notify.parse_thresholds("nonsense") == ()
    assert notify.crossings(pct=95, previous_pct=10, already_fired=set(), thresholds=()) == []


def test_a_systemd_unit_with_no_session_bus_reports_why_rather_than_pretending() -> None:
    """The exact objection alerts.py raised against this feature."""
    cap = notify.detect_capability("linux", env={})
    assert not cap.available
    assert "no session bus" in cap.reason and "webhook" in cap.reason


def test_linux_with_a_bus_but_no_notify_send_names_the_missing_package(monkeypatch) -> None:
    monkeypatch.setattr(notify.shutil, "which", lambda _n: None)
    cap = notify.detect_capability("linux", env={"DBUS_SESSION_BUS_ADDRESS": "unix:/x"})
    assert not cap.available and "notify-send" in cap.reason


def test_nothing_is_sent_when_it_cannot_be_delivered() -> None:
    """A toggle that silently does nothing is worse than a disabled one."""
    crossing = notify.Crossing("five_hour", "Session", 90.0, 91.0, "13:00", "R1")

    def explode(*_a, **_kw):
        raise AssertionError("must not attempt delivery")

    assert notify.send(crossing, notify.Capability(False, "no bus"), runner=explode) is False


def test_a_delivery_failure_is_swallowed_not_raised() -> None:
    """It must never turn a good poll into a bad one."""
    crossing = notify.Crossing("five_hour", "Session", 90.0, 91.0, "13:00", "R1")
    cap = notify.Capability(True, tool="notify-send")

    def fail(*_a, **_kw):
        raise OSError("no such binary")

    assert notify.send(crossing, cap, runner=fail) is False
    assert notify.send(crossing, cap, runner=lambda *a, **k: SimpleNamespace(returncode=1)) is False
    assert notify.send(crossing, cap, runner=lambda *a, **k: SimpleNamespace(returncode=0)) is True


def test_the_message_is_one_line_with_window_percentage_and_reset() -> None:
    crossing = notify.Crossing("five_hour", "Session", 90.0, 91.4, "13:00", "R1")
    assert crossing.message() == "Session at 91%, resets 13:00"
    assert "\n" not in crossing.message()


def test_a_quote_in_a_label_cannot_close_the_applescript_string() -> None:
    argv = notify._argv("osascript", 'a" & do shell script "id', "body")
    assert "do shell script" in argv[-1]  # the text survives
    assert argv[-1].count('"') == 4  # exactly the four we opened and closed


def test_the_windows_toast_actually_shows_a_toast() -> None:
    """Not a Write-Output that exits 0 and displays nothing."""
    script = notify._argv("powershell", "QuotaLens", "Session at 90%")[-1]
    assert "ToastNotification" in script and "Show(" in script
    assert "Write-Output" not in script


def test_a_powershell_quote_is_doubled_not_dropped() -> None:
    script = notify._argv("powershell", "QuotaLens", "resets o'clock")[-1]
    assert "o''clock" in script


# -- the three threshold slots -----------------------------------------------------


def test_the_default_round_trips_through_three_slots() -> None:
    assert notify.to_slots("50,75,90") == ["50", "75", "90"]
    assert notify.from_slots(["50", "75", "90"]) == ("50,75,90", "")


def test_a_partially_disabled_set_keeps_only_what_was_chosen() -> None:
    assert notify.to_slots("40,80") == ["40", "80", ""]
    assert notify.from_slots(["", "60", ""]) == ("60", "")


def test_all_disabled_stores_none_and_fires_nothing() -> None:
    """The failure this is guarding: an `or DEFAULT` handing 50/75/90 back."""
    stored, error = notify.from_slots(["", "", ""])
    assert stored is None and error == ""
    assert notify.parse_thresholds(stored) == ()
    assert notify.crossings(pct=99, previous_pct=1, already_fired=set(), thresholds=()) == []


def test_duplicates_and_descending_order_are_refused_with_a_reason() -> None:
    assert notify.from_slots(["50", "50", "90"])[1] == (
        "each threshold must be a different percentage"
    )
    assert notify.from_slots(["90", "75", "50"])[1] == "thresholds must be in ascending order"
    assert notify.from_slots(["50", "banana", ""])[1].startswith("'banana'")


def test_a_legacy_list_longer_than_three_keeps_its_lowest_three() -> None:
    """Loading rewrites nothing: the extras stay on disk until a save."""
    assert notify.to_slots("10,20,30,40,50") == ["10", "20", "30"]
    assert notify.parse_thresholds("10,20,30,40,50") == (10.0, 20.0, 30.0, 40.0, 50.0)


def test_a_legacy_list_with_spaces_or_two_values_loads() -> None:
    assert notify.to_slots(" 40 , 80 ") == ["40", "80", ""]
    assert notify.to_slots("70") == ["70", "", ""]


def test_the_choices_can_express_the_shipped_default() -> None:
    for value in notify.DEFAULT_THRESHOLDS:
        assert int(value) in notify.THRESHOLD_CHOICES


def test_a_stored_value_outside_the_offered_set_is_still_representable() -> None:
    """Otherwise the select falls back to Disabled and the next save loses it."""
    assert notify.to_slots("10,20,30") == ["10", "20", "30"]
    assert 10 not in notify.THRESHOLD_CHOICES  # the case this guards


# -- delivery outcome and the test probe -------------------------------------------


def test_a_failed_delivery_is_never_recorded_as_notified() -> None:
    """The event still suppresses a retry; the note stops it reading as success.

    This assertion used to read `== set()`, which contradicted its own docstring
    and matched the bug rather than the intent: the note was appended straight
    onto the parsed key, `float("50 (not delivered)")` raised, and the level was
    retried once a minute for the rest of the window.
    """
    crossing = _crossing()
    detail = crossing.failed_detail()
    assert "notified" not in detail
    assert notify.FAILED_NOTE in detail
    assert notify.fired_thresholds([detail], "five_hour", "R1") == {50.0}


def _crossing(threshold: float = 50.0) -> notify.Crossing:
    return notify.Crossing("five_hour", "Session", threshold, 51.0, "02:10", "R1")


@pytest.mark.parametrize("threshold", [20.0, 50.0, 75.0, 90.0, 100.0])
def test_delivered_and_failed_details_parse_identically(threshold: float) -> None:
    """A round trip through the production path, not a hand-built string.

    Constructing the detail by hand in a test is what let the parser and the
    writer drift apart in the first place.
    """
    crossing = _crossing(threshold)
    delivered = notify.fired_thresholds([crossing.event_detail], "five_hour", "R1")
    failed = notify.fired_thresholds([crossing.failed_detail()], "five_hour", "R1")
    assert delivered == failed == {threshold}


def test_a_note_never_reaches_the_number() -> None:
    """The key ends at the separator; anything after it is commentary."""
    crossing = _crossing()
    assert crossing.failed_detail().startswith(crossing.event_detail + notify.DETAIL_SEP)
    assert notify.fired_thresholds(
        [crossing.event_detail + notify.DETAIL_SEP + "anything at all"], "five_hour", "R1"
    ) == {50.0}


def test_the_status_line_never_claims_a_notification_was_seen() -> None:
    ready = notify.delivery_status(notify.Capability(True, tool="terminal-notifier"))
    assert ready == "Ready to send via terminal-notifier"
    assert notify.delivery_status(notify.Capability(True, tool="osascript")).startswith(
        "Ready to send via macOS"
    )
    assert notify.delivery_status(notify.Capability(False, "no session bus")) == (
        "Unavailable: no session bus"
    )
    assert notify.delivery_status(
        notify.Capability(True, tool="osascript"), "exited non-zero"
    ).startswith("Last test could not be handed to macOS")
    for text in (ready,):
        assert "delivered" not in text and "seen" not in text


def test_the_test_probe_reports_the_command_result() -> None:
    from types import SimpleNamespace

    cap = notify.Capability(True, tool="notify-send")
    ok, reason = notify.send_test(cap, runner=lambda *a, **k: SimpleNamespace(returncode=0))
    assert ok and reason == ""
    bad, why = notify.send_test(cap, runner=lambda *a, **k: SimpleNamespace(returncode=1))
    assert not bad and "non-zero" in why


def test_the_test_probe_refuses_when_delivery_is_unavailable() -> None:
    ok, reason = notify.send_test(notify.Capability(False, "no session bus"))
    assert not ok and reason == "no session bus"


def test_the_test_message_is_identifiable() -> None:
    assert notify.TEST_MESSAGE == "QuotaLens test notification"


def test_a_failed_delivery_is_attempted_once_and_not_retried(settings, store) -> None:
    """The level the bug actually bit: send call counts, not event text.

    A failed delivery was retried on every poll -- once a minute for the rest of
    the window -- because the event written to stop that could not be read back.
    """
    import time

    from quotalens.parse import QuotaReading, UsageParse
    from quotalens.poller import Poller
    from quotalens.secrets import MemorySecretStore, Redactor

    calls: list[str] = []
    poller = Poller(
        settings.with_overrides(notify=True, notify_thresholds="50"),
        store,
        MemorySecretStore(None),
        Redactor(),
    )
    poller.notify_capability = notify.Capability(True, tool="fake")

    reset = "2026-09-07T08:00:00+00:00"

    def parsed(pct: float) -> UsageParse:
        return UsageParse(
            readings=[QuotaReading("five_hour", "Session", pct, reset, None, True)],
            ignored=[],
            fallback_used=False,
        )

    def step(pct: float, at: int) -> None:
        previous = store.latest_quota()
        current = parsed(pct)
        store.record_quota(at, current.readings)
        poller._check_notify(at, previous, current)

    original = notify.send
    notify.send = lambda c, cap, runner=None: calls.append(c.message()) or False
    try:
        now = int(time.time())
        step(10.0, now)  # arms
        step(60.0, now + 60)  # the crossing: one attempt, which fails
        assert len(calls) == 1, calls
        step(61.0, now + 120)  # same level, next poll
        step(62.0, now + 180)  # and the one after
        assert len(calls) == 1, f"a failed delivery was retried: {calls}"
    finally:
        notify.send = original


def test_the_credit_path_does_not_share_the_fault(store) -> None:
    """Credits key their event on the stretch start, not on a parsed detail.

    So there is no number inside a string for a note to corrupt -- the same class
    of fault cannot exist there.
    """
    from quotalens import credits

    class Row:
        def __init__(self, ts: int) -> None:
            self.ts = ts

    assert credits.recorded_starts([Row(10), Row(20)]) == {10, 20}
    run = credits.stretches(
        [
            credits.OverageRow(t, v, 3000, "USD", 2)
            for t, v in ((0, 0), (60, 500), (120, 500), (180, 500), (240, 500))
        ],
        60,
    )[0]
    # The detail is prose only; nothing is parsed back out of it.
    assert "credits started" in run.detail()
    assert credits.recorded_starts([Row(run.start_ts)]) == {run.start_ts}
