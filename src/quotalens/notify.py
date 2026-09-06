"""Level crossings on a window's consumption, and how to push them.

:mod:`quotalens.alerts` rejected desktop notifications once, for a reason that is
still true and is quoted in its docstring: a systemd user unit without a session
bus has nowhere to put one. So this is an **additional sink, not a replacement**.
The webhook keeps firing regardless, and where a desktop notification cannot be
delivered the settings panel says so with the reason rather than offering a
switch that silently does nothing. A notification feature that fails quietly is
worse than none, because the user stops watching the dashboard on its strength.

The detector is a different shape from ``ThresholdDetector`` -- levels on a
percentage, not edges on a rate -- but inherits three of its properties, each
with a specific failure if dropped:

* **Edge, not level.** Fire once when 50% is crossed upward, or you get a
  notification every 60 seconds for hours.
* **Reset per window.** A new window re-arms every threshold, or you get silence
  for the whole of the next one.
* **Survive a restart.** Which thresholds fired is persisted in ``event``, keyed
  to the window, or a restart re-fires all three.

And one rule the burn alert did not need: **never notify on a value the
dashboard would not show.** A stale, auth-failed or unverified meter has no
percentage -- DESIGN.md 5 removes it -- so there is nothing to cross. Nor does a
percentage that moved because the *limit* changed count: a boost is not
consumption.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

CROSSED_KIND = "notify_crossed"
DEFAULT_THRESHOLDS = (50.0, 75.0, 90.0)
NOTIFY_TIMEOUT_S = 5.0
ICON_FILE = "mark.png"  # the ring mark, in quotalens.web; see design/render_mark_png.py
GENERIC_ICON_NOTE = (
    "notifications will show the system icon; install terminal-notifier "
    "(brew install terminal-notifier) for the QuotaLens mark"
)


def icon_path() -> str | None:
    """A real filesystem path to the mark, or None.

    ``importlib.resources`` may hand back a zip member, so this materialises one
    and keeps it for the process. The notifiers take a path, not bytes.
    """
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return _ICON_CACHE or None
    try:
        with resources.as_file(resources.files("quotalens.web").joinpath(ICON_FILE)) as path:
            target = Path(tempfile.gettempdir()) / f"quotalens-{ICON_FILE}"
            if not target.exists() or target.stat().st_size != path.stat().st_size:
                target.write_bytes(path.read_bytes())
            _ICON_CACHE = str(target)
    except (OSError, ModuleNotFoundError, FileNotFoundError):
        _ICON_CACHE = ""
    return _ICON_CACHE or None


_ICON_CACHE: str | None = None


@dataclass(frozen=True)
class Crossing:
    """One threshold crossed upward, in one window."""

    window: str
    label: str
    threshold: float
    pct: float
    resets_at_text: str
    window_key: str  # identifies *this instance* of the window: its reset time
    # An override for events that are not threshold crossings -- credits, where
    # "at 91%, resets 02:10" would be meaningless.
    body: str = ""

    @property
    def event_detail(self) -> str:
        """What is written to ``event``, and what a restart reads back."""
        return f"{self.window}@{self.window_key} crossed {self.threshold:.0f}"

    def message(self) -> str:
        """One line: which window, the percentage, the reset time. No emoji."""
        if self.body:
            return self.body
        return f"{self.label} at {self.pct:.0f}%, resets {self.resets_at_text}"


def fired_thresholds(details: list[str], window: str, window_key: str) -> set[float]:
    """Which thresholds already fired for this window instance, from ``event``.

    The key is the window's own reset time. When that changes the window has
    rolled over and every threshold re-arms, which is also why a boost re-arms
    anything now below its level: a raised limit moves the percentage down
    without the window having ended.
    """
    prefix = f"{window}@{window_key} crossed "
    out: set[float] = set()
    for detail in details:
        if detail.startswith(prefix):
            try:
                out.add(float(detail[len(prefix) :]))
            except ValueError:
                continue
    return out


def crossings(
    *,
    pct: float | None,
    previous_pct: float | None,
    already_fired: set[float],
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
) -> list[float]:
    """Thresholds crossed *upward* since the previous reading.

    ``pct`` of ``None`` is an unknown value, not a low one: nothing crosses.
    ``previous_pct`` of ``None`` is the first reading of a window, which arms the
    thresholds rather than crossing them -- otherwise a restart mid-window at 80%
    would announce 50 and 75 as if they had just happened.
    """
    if pct is None or previous_pct is None:
        return []
    return sorted(t for t in thresholds if t not in already_fired and previous_pct < t <= pct)


# -- delivery -------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """Whether an OS notification can actually be delivered here, and why not."""

    available: bool
    reason: str = ""
    tool: str = ""
    # A caveat about a capability that works but not fully -- shown beside the
    # toggle, so the wrong icon is explained rather than shipped silently.
    note: str = ""

    @property
    def explanation(self) -> str:
        return "" if self.available else self.reason


def detect_capability(platform: str | None = None, env: dict[str, str] | None = None) -> Capability:
    """Can this process put a notification on a screen?

    Checked at startup so the panel can disable the toggle *with the reason*.
    The systemd-without-a-session-bus case is the one that matters: it is how
    this tool is meant to run, and it is where a silent failure would be
    invisible for weeks.
    """
    import os

    platform = platform if platform is not None else sys.platform
    env = env if env is not None else dict(os.environ)

    if platform == "darwin":
        # terminal-notifier first, and not for taste: macOS attaches the icon of
        # the *posting process*, and `osascript` posts as Script Editor. There is
        # no argument to `display notification` that changes it, so the osascript
        # path can never show the QuotaLens mark. terminal-notifier takes
        # -appIcon and can.
        if shutil.which("terminal-notifier"):
            return Capability(True, tool="terminal-notifier")
        if shutil.which("osascript"):
            return Capability(True, tool="osascript", note=GENERIC_ICON_NOTE)
        return Capability(False, "neither terminal-notifier nor osascript is on PATH")

    if platform.startswith("win"):
        if not shutil.which("powershell"):
            return Capability(False, "powershell is not on PATH")
        # Untested on real Windows from this machine: CI has no interactive
        # session to show a toast in. The argv is a genuine WinRT toast, not a
        # no-op, and a failure returns False rather than reporting success.
        return Capability(True, tool="powershell")

    # Linux and the BSDs: a notification needs a session bus and a daemon on it.
    if not (
        env.get("DBUS_SESSION_BUS_ADDRESS") or env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    ):
        return Capability(
            False,
            "no session bus; this is how a systemd user unit runs. Use the webhook instead.",
        )
    if not shutil.which("notify-send"):
        return Capability(False, "notify-send is not installed (libnotify)")
    return Capability(True, tool="notify-send")


def _argv(tool: str, title: str, body: str) -> list[str]:
    icon = icon_path()
    if tool == "terminal-notifier":
        argv = [
            "terminal-notifier",
            "-title",
            title,
            "-message",
            body,
            "-sender",
            "com.apple.Terminal",
        ]
        if icon:
            argv += ["-appIcon", icon]
        return argv
    if tool == "osascript":
        # Quotes are stripped from both parts, so neither can close the string
        # and start AppleScript of its own. The body is our own text today, but
        # a window label comes from the provider's payload.
        safe = body.replace('"', "").replace("\\", "")
        head = title.replace('"', "").replace("\\", "")
        return ["osascript", "-e", f'display notification "{safe}" with title "{head}"']
    if tool == "powershell":
        # A real toast, not a Write-Output that exits 0 and shows nothing --
        # which is the silent failure this module exists to avoid. PowerShell
        # single-quoted strings escape a quote by doubling it, and nothing else.
        head = title.replace("'", "''")
        safe = body.replace("'", "''")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
            "ContentType=WindowsRuntime]>$null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            "$n=$t.GetElementsByTagName('text');"
            f"$n.Item(0).AppendChild($t.CreateTextNode('{head}'))>$null;"
            f"$n.Item(1).AppendChild($t.CreateTextNode('{safe}'))>$null;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
            "'QuotaLens').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    argv = ["notify-send", "-a", "QuotaLens"]
    if icon:
        argv += ["-i", icon]
    return [*argv, title, body]


def send(crossing: Crossing, capability: Capability, runner: object = None) -> bool:
    """Deliver one notification. Never raises; returns whether it went out.

    A failure here must not cost a poll or a webhook, so every error is logged
    and swallowed. Prefers a subprocess to a new dependency: three per-OS argv
    lists are cheaper than a package that has to be kept working on all three.
    """
    if not capability.available:
        return False
    argv = _argv(capability.tool, "QuotaLens", crossing.message())
    run = runner or subprocess.run
    try:
        result = run(  # type: ignore[operator]
            argv, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("desktop notification failed: %s", exc)
        return False
    if getattr(result, "returncode", 0) != 0:
        log.warning("desktop notification exited %s", result.returncode)
        return False
    return True


# What the three selects offer, so the shipped default 50,75,90 is exactly
# representable and nothing has to be typed.
THRESHOLD_CHOICES: tuple[int, ...] = (20, 30, 40, 50, 60, 70, 75, 80, 90, 100)
THRESHOLD_SLOTS = 3
SLOT_KEYS = tuple(f"notify_t{i + 1}" for i in range(THRESHOLD_SLOTS))


def parse_thresholds(raw: str | None) -> tuple[float, ...]:
    """``"50,75,90"`` to a tuple. An unparseable entry is dropped, not guessed at.

    **An empty configured list is not an absent one.** This used to answer both
    with ``DEFAULT_THRESHOLDS``, so a user who switched every threshold off got
    50, 75 and 90 back on the next poll. ``None`` and ``""`` now mean *none*;
    the documented default belongs to the key being absent from the config
    entirely, which is where ``ConfigKey.default`` supplies it.
    """
    if not raw:
        return ()
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = float(part)
        except ValueError:
            continue
        if 0 < value <= 100:
            out.append(value)
    return tuple(sorted(set(out)))


def to_slots(raw: str | None) -> list[str]:
    """The stored string as three select values, lowest first, unused blank.

    A stored list longer than three keeps its lowest three here, because the
    form has three slots and cannot show more. Nothing is rewritten by loading:
    the extra values stay in ``config.json`` until the user actually saves, and
    saving then stores exactly what the form showed.
    """
    values = [f"{v:g}" for v in parse_thresholds(raw)][:THRESHOLD_SLOTS]
    return values + [""] * (THRESHOLD_SLOTS - len(values))


def from_slots(values: list[str]) -> tuple[str | None, str]:
    """Three select values to the stored string, or an error.

    Returns ``(None, "")`` when every slot is Disabled -- which is a real
    setting meaning "no threshold notifications", not a missing one.
    """
    chosen = []
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        try:
            chosen.append(int(float(text)))
        except ValueError:
            return None, f"{text!r} is not a percentage"
    if not chosen:
        return None, ""
    if len(set(chosen)) != len(chosen):
        return None, "each threshold must be a different percentage"
    if chosen != sorted(chosen):
        return None, "thresholds must be in ascending order"
    return ",".join(str(v) for v in chosen), ""
