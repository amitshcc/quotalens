"""Threshold notifications: the three properties inherited from the burn alert."""

from __future__ import annotations

from types import SimpleNamespace

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
    assert notify.parse_thresholds("") == notify.DEFAULT_THRESHOLDS
    assert notify.parse_thresholds("nonsense") == notify.DEFAULT_THRESHOLDS
    assert notify.parse_thresholds("0, 150, 50") == (50.0,)  # out of range


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
