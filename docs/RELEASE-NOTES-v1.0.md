# QuotaLens v1.0

*Draft for the GitHub release page. Written for someone arriving from a link who
has never heard of this project — not a changelog.*

---

## Claude tells you what is left. It does not tell you what happened.

Open `/usage` and you get a number: 43% of your five-hour session, 12% of your
week. Close it and that number is gone. There is no record of how fast it
climbed, no way to see that last Tuesday afternoon burned a third of your weekly
allowance in ninety minutes, and no warning while it is happening.

QuotaLens keeps the series. It runs on your own machine, polls your account once
a minute, stores the readings in a SQLite file you own, and answers the question
you actually open it to ask:

> **Will this session window run out before it resets, and when?**

Not "you are at 43%". *"At this rate you will finish with 31% unused"*, or
*"you will run out at 01:34, three hours before the reset."*

## What you get

- **A dashboard on `localhost`** that leads with the projection, not the
  percentage. Five-hour session, weekly, and per-model weekly limits, each with
  its own trace.
- **History that survives restarts.** Every five-hour window you have used, how
  steep the climb was, and the minute it started.
- **Burn-rate alerts.** A webhook fires when consumption crosses a threshold you
  set — ntfy, Discord, Slack, Pushover, Home Assistant, anything that takes a
  POST. Optionally a desktop notification too, at 50%, 75% and 90% of a window.
- **A weekly budget table** that converts "88% of your week left" into "7.5 more
  full sessions, and your typical one costs 12 points".
- **Quota boosts, detected and marked.** When Anthropic raises a limit, your
  percentage falls without the window resetting. That is not you using less, and
  the chart says so with a crimson step rather than drawing a decline you did
  not earn.
- **`/metrics` for Prometheus**, and CSV/JSON export of everything.

## What it deliberately does not do

- **Per-project attribution.** Your provider's own `claude /usage` does that
  better. Quota is pooled across claude.ai, Claude Code and Claude Desktop, so a
  local log can show that a project correlates with a climb — it cannot attribute
  pooled quota to it.
- **Guess.** When a reading is stale, unverified, or a window has lapsed, the
  number is *removed and explained*, never frozen at its last value and never
  shown as zero. A dashboard that quietly serves you yesterday's figure is worse
  than one that admits it does not know.
- **Listen on anything but loopback.** There is no `--host`. The dashboard is
  account data with no authentication in front of it.
- **Raise, extend or bypass a limit.** It only observes.

## Install

```sh
pipx install quotalens      # or: uv tool install quotalens
quotalens auth              # paste your claude.ai session cookie once
quotalens start             # runs in the background
open http://127.0.0.1:8787
```

`quotalens service install` registers it to start at login — a LaunchAgent on
macOS, a systemd user unit on Linux, a Task Scheduler logon task on Windows.

The cookie lives in your OS keychain, never in a config file and never in a log
line. Four runtime dependencies. No account, no signup, no telemetry, nothing
phones home. MIT.

## Unofficial, and what that means

**QuotaLens is not affiliated with, endorsed by, or associated with Anthropic.**
It reads undocumented `claude.ai` endpoints — the same ones your own browser
calls — with your own session cookie, at most once a minute. Those endpoints can
change without notice, and when they do this will break until it is fixed.

It only observes. It cannot raise, extend or bypass a limit.

Anthropic's Consumer Terms prohibit accessing the services through automated
means without an API key, and a subscription session cookie is not an API key.
There is no carve-out for reading your own usage. That clause is quoted in full,
with the two facts that bear on the risk, in the README's *"The Terms, stated
plainly"* section. **You are the one accepting that risk.** Read it before you
install, not after.

## Known limits

- The five-hour session window is *inferred* from the reset timestamp the API
  returns. A watchdog reports it if that inference stops holding.
- Gaps in collection are drawn as a straight line between the samples either
  side, which invents a shape for hours nobody observed. The gap itself is
  marked; deciding what it should look like instead is open work.
- The Windows credential path has been exercised by CI but never by a person
  reading a real cookie out of the Windows Credential Manager.
- Boost magnitude is unknowable. The payload carries no ceiling anywhere, so
  QuotaLens can say a limit was raised and when, but not by how much.
