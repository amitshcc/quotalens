# QuotaLens

[![CI](https://github.com/amitshcc/quotalens/actions/workflows/ci.yml/badge.svg)](https://github.com/amitshcc/quotalens/actions/workflows/ci.yml)

Claude tells you what is consuming your quota right now, and then forgets.
QuotaLens remembers.

It is a local, self-hosted monitor for Claude Pro and Max subscription usage. It
polls your account every minute, keeps the series in a SQLite file you own, and
leads with the question you open it to ask: **will this session window run out
before it resets, and when.** When something surprises you, the record is still
there — which five-hour session, how steep the climb, the minute it started.

macOS, Linux and Windows, each tested on every push — with one caveat about
Windows worth reading before you rely on it, in [Platforms](#platforms). Binds
loopback, keeps your cookie in the OS keychain, phones nothing home. MIT.

Status: **v0.1.0, pre-release.**

## Quick start

```sh
pipx install quotalens      # or: uv tool install quotalens
quotalens auth              # paste your claude.ai session cookie once (stored in the OS keychain)
quotalens probe             # one fetch, prints raw + parsed output for debugging
quotalens start             # background: pid file + rotating log in the data directory
open http://127.0.0.1:8787   # the dashboard
quotalens status            # exit 0 healthy, 1 not running, 2 stalled
quotalens logs -f
quotalens stop
curl 'http://127.0.0.1:8787/api/quota/current'
```

Then, as you need them:

```sh
curl 'http://127.0.0.1:8787/api/budget'                    # the weekly limit, in session windows
curl 'http://127.0.0.1:8787/metrics'                       # Prometheus, hand rolled
curl 'http://127.0.0.1:8787/api/export.csv?table=quota'    # or export.json
curl 'http://127.0.0.1:8787/api/events'                    # threshold crossings, anomalies
quotalens prune --dry-run                                  # what retention would remove
quotalens forget                                           # session windows, and their ids
```

Set `QUOTALENS_WEBHOOK_URL` to get one POST when the burn rate crosses
`QUOTALENS_BURN_ALERT` points per hour (default 20), and one when it falls back.
It feeds ntfy, Discord, Slack, Pushover or Home Assistant. The body:

```json
{
  "event": "burn_alert",
  "profile": "default",
  "ts": 1788456405,
  "rate_pts_per_hour": 42.5,
  "threshold_pts_per_hour": 20.0,
  "headroom_pct": 37.0,
  "session_resets_at": "2026-09-03T18:00:00+00:00",
  "text": "Burn rate 42.5 pts/hr crossed the 20 pts/hr threshold, 37% of the session left.",
  "url": "http://127.0.0.1:8787/"
}
```

`profile` is the local label you chose, so a receiver watching two of them can
tell them apart. There is no organisation id, no account identifier and no
cookie in it, by design.

## Two accounts

A profile is a second account. It gets its own keyring entry, its own database,
its own port and its own pid file, so the two never see each other:

```sh
quotalens --profile work auth
quotalens --profile work start        # a derived port, printed by `start`
quotalens --profile personal auth
quotalens --profile personal start
quotalens --profile work stop         # leaves the personal one running
```

Two accounts is two processes and two bookmarks, not an account picker inside
one process. The port is derived from the name and is the same on every run, so
`start`, `serve` and `status` all print the URL they landed on — you should
never have to work out which port a profile got. If something else already holds
it, the error names the port, the profile and the `--port` flag that settles it.
`QUOTALENS_PROFILE` works too, for a service unit.

## Running it as a service

One command makes it start on its own every time you log in, and one undoes it:

```sh
quotalens service install     # start at login, from now on
quotalens service status      # is it set up, and is it running?
quotalens service uninstall   # stop doing that, and remove what was written
```

What that installs depends on the OS:

| Platform | What is registered | Restarts if it crashes |
|---|---|---|
| macOS | LaunchAgent, `RunAtLoad` and `KeepAlive` | yes, launchd |
| Linux | systemd **user** unit, `Restart=on-failure` | yes, systemd |
| Windows | Task Scheduler task `QuotaLens`, trigger "at log on" | no, next login |

Every file written and command run is printed, so it can be undone by hand.
The installed command carries an explicit `--data-dir`, so the service collects
into the same place your shell does. `serve` stays the foreground command the
service manager execs, and `start`/`stop`/`status`/`logs` still work alongside.

On Linux the unit only runs while you are logged in unless you enable lingering;
the installer prints the exact `loginctl enable-linger` command. On Windows the
task starts at **login**, not at boot, and only for your account.

A background agent reading the OS keychain may prompt on first run or be
refused; `quotalens status` then says "keyring" specifically rather than "no
data".

## The weekly limit, in windows you can plan with

"Weekly is at 93%" is not a number you can act on. The dashboard puts the same
fact under the weekly meters in the unit the work actually arrives in:

```
Limit                 Left   Full sessions left   At your typical session   Each full session costs
Weekly — all models     6%                  0.5   0.5  at 89% used         12 pts  (10–15, from 6 sessions)
Weekly — Fable          0%            none left   none left

There is time for 13.6 more sessions before this resets Mon 09:30, and budget
for 0.5 — the budget is what runs out.
Weekly — Fable is spent, so none of the 6% left on Weekly — all models can be
used on it.
```

The note under the table says which of the two constraints binds, because that
is the finding: half a session of budget against thirteen sessions of wall clock
means rationing, not racing.

**The cost of a window is measured, not assumed.** Every complete session window
in your history carries both its own consumption and what it cost each weekly
limit, so the ratio is an observation about how *you* use models. The median is
the estimate and the range beside it is the spread, because the model mix moves
it — in one real history the same 100% window cost between 9.6 and 14.8 points.

Windows that would poison the ratio are excluded, strictly: one still running,
one only partially observed, one the weekly limit reset inside, one too small to
divide, and one where the limit was already at its cap and so could not move.
Below five usable sessions it prints the reason in the cell instead of a number:
*"Needs 5 complete session windows to estimate the cost of one; 3 so far."* An em
dash cannot be told apart from "the answer is nothing", and those are opposite
facts to plan against. It does not lower the threshold to make a number appear —
a confident "3.2 sessions left" drawn from two observations is worse than none.

It is on `/api/budget` and in `/metrics` as `quotalens_weekly_windows_remaining`,
`quotalens_weekly_window_cost_points` and
`quotalens_weekly_clock_windows_remaining`.

## The dashboard

The figure it leads with is how much of the session window is left. Below it: one meter per quota window with the API's own
severity, a 24-hour chart of every window with resets drawn as gaps, and the
extra-usage spend computed from minor units with the payload's exponent, never
clamped at 100%.

States are honest by construction. If the collector has not succeeded in three
poll intervals, the cookie was rejected, or the response could not be parsed,
every value is replaced by an em dash and the frame changes, so a stale page
never looks like a healthy one showing low usage. If the browser loses the
server, the same treatment applies from CSS alone.

The dashboard is interrogable and every view is a URL: range presets from 15
minutes to all, drag on the chart to zoom to any window (double-click resets),
click a series' end label to hide it, pick the burn-rate lookback, set
auto-refresh, and force a poll (one per 10 seconds). All of it works with
JavaScript disabled as plain links and forms.

The top of the page answers the one question the dashboard exists for: will
the session window run out before it resets? Beside the burn rate sit a ticking
countdown to the reset, the headroom left, and the sustainable rate, the points
per hour you could burn from now to the reset without exhausting it. The
verdict sentence changes with the situation: "Exhausted at 21:04, 2h 09m before
reset", "At this rate you finish with 37% unused", or "Flat for 4m. 37% left,
resets in 2h 14m". Once five complete windows exist it adds how the projected
finish compares with your median window. Five bars beneath show the points
consumed in each hour of the window. The chart's default range is the current
window from start to reset, with now inside it, hourly separators, and a dashed
projection at the current rate that turns critical where it crosses 100%.

Below the chart, the history table lists your session windows, derived from the
API's own `resets_at` values: when it jumps forward a new session started.

**Where a session *starts* is an inference.** Anthropic documents that the
session limit resets every five hours and has never published how the window is
anchored, so QuotaLens infers the start as the reset time minus five hours. The
evidence for it is that the server recomputes `resets_at` on every call and only
the sub-second part moves, which is what a fixed anchor looks like. Rather than
hedge, the collector checks the inference on every poll: if a reset time ever
moves forward by less than five hours without the percentage dropping, the model
is wrong, and QuotaLens records it and says so on the page. Anything derived from
session history — the table, the hour strip, the auto range, the coverage badge —
rests on that inference.

Each row shows the peak
utilisation reached in the window, how far each weekly limit moved during it,
a sparkline of its shape, and how much of it was observed. Sort by consumption to find
the expensive session, click it, and the chart shows exactly that window. On the
chart, session starts are vertical rules and spans with no session running are
shaded flat, which is a different mark from the hatched spans where the
collector was not running.

## How it reaches claude.ai

claude.ai sits behind Cloudflare bot protection that fingerprints the TLS
handshake. A plain Python HTTP client is served a challenge page even with a
valid session cookie, so QuotaLens talks to claude.ai through
[`curl_cffi`](https://github.com/lexiforest/curl_cffi), which impersonates a
browser's TLS and HTTP/2 fingerprint. If you ever see a `blocked` state on
`/api/health`, try `QUOTALENS_IMPERSONATE=safari` (default `chrome`).

## Security note

The session cookie is equivalent to your claude.ai password. QuotaLens stores
it only in the OS keychain (via `keyring`), never in a file, the database, or a
log line, and redacts it from error output. The server binds loopback only.
Treat `quotalens probe` output as sensitive and redact it before sharing.

## Storage, and what gets pruned

Two things accumulate, and only one of them is pruned.

**The readings are the product and are never pruned.** One row per window per
poll, about 100 bytes each. With three windows at a minute a poll that is
roughly 0.4 MB a day, 150 MB a year. If you want less, poll less often.

**The raw payloads are debugging material and are bounded.** Every response is
stored verbatim so that when the endpoint shape changes there is a record of it.
Measured on a real database: a usage payload averages **2.0 KB**, so at a minute
a poll the `sample` table grows **2.8 MB a day, about 1 GB a year** if nothing
prunes it. Something does:

```sh
quotalens prune --dry-run    # what it would remove
quotalens prune --keep 50000 # or set QUOTALENS_SAMPLE_KEEP
```

The default keeps the newest **20,000 samples, about 14 days, roughly 39 MB**,
plus the first sample of every distinct payload shape, forever — that set is the
endpoint-drift record and pruning it would defeat the point of keeping payloads
at all. The poller prunes on the same rule every six hours, so the default
applies whether or not you ever run the command.

(Those figures are measured, not arithmetic. Before v0.1.0 the overage endpoint
was fetched every poll too, which added a second 1.0 KB payload a minute; it is
now fetched once at startup.)

### Rows another collector wrote

If a second instance ever pointed at this database, its samples are in here too,
and the history shows session windows that were never yours. Version 0.1.0 fixed
the cause: a scratch `--data-dir` now implies a scratch database. It cannot fix
databases that already have the rows.

```sh
quotalens forget                       # every session window, with its id
quotalens forget <id> [<id>] --dry-run # what removing them would take
quotalens forget <id> [<id>]           # take it, then rebuild the history
```

The listing marks windows where only minutes of a five-hour span were ever
observed, which is what a collector that ran for two minutes leaves behind. It
is a reason to look, not a verdict: a window where you genuinely only had the
collector up for two minutes looks identical, and only you know which it was.

Removal is by **expiry, not by time range**. Two collectors writing to one
database interleave their samples second by second, so deleting a time range
takes real readings with it. The five-hour expiry is what separates one
collector's window from another's, and it is the same key the history is built
from. Stop the server first, so the startup rebuild runs on what is left:

```sh
quotalens stop && quotalens forget <id> && quotalens start
```

## What this doesn't do

Each of these is a decision, and in most cases something else already does it
better. Pointing at the better tool is a feature.

- **Per-project attribution.** Use [ccusage](https://github.com/ccusage/ccusage)
  for per-project token counts, and `/usage` inside Claude Code for attribution
  to skills, subagents, plugins, MCP servers and scheduled tasks. Quota is
  pooled across claude.ai, Claude Code and Claude Desktop, so local logs can
  only ever show correlation with a number they cannot see.
- **Desktop notifications.** Use
  [ClaudeUsageBar](https://github.com/Artzainnn/ClaudeUsageBar) on macOS. A
  desktop notification is three OS code paths and it is dead under a systemd
  user unit with no session bus, which is how this is meant to run. The webhook
  is one code path that works everywhere.
- **Anything but loopback.** There is no `--host`. The dashboard is account data
  with no authentication. If you want it elsewhere, put it behind a proxy you
  already trust; if enough people ask, the answer will be a token, not a flag.
- **A container image.** `pipx` or `uvx` only, for now. Docker needs a
  credential path that is not the OS keychain, which forks the security story,
  and that deserves its own decision rather than a Dockerfile.
- **Reading Claude Code's OAuth credentials.** The pasted cookie stays the only
  auth path in this release.
- **Any provider but Claude**, API-key cost tracking, a menu bar app, a proxy,
  or a hosted service.

## Linux servers: not yet

QuotaLens keeps your cookie in the OS keyring and has no file-based credential
store. On a Linux box with no desktop session there is usually no D-Bus session
and no keyring daemon, and then `python-keyring` has no backend at all:

```
$ quotalens auth
cannot use the keyring: this system has no usable keyring, so the session
cookie cannot be stored or read. On a Linux server that normally means there is
no D-Bus session and no keyring daemon; QuotaLens has no file-based credential
store, so a headless Linux box is not supported yet...
```

That check runs **before** you are asked for a cookie, so you find out in a
second rather than after pasting one and waiting for a network round trip. A
desktop Linux session with `gnome-keyring` or `kwallet` unlocked works normally,
and so does the systemd **user** unit under that session.

Supporting a real server means a credential path that is not the OS keyring, and
that forks the security story, so it is a deliberate post-1.0 decision rather
than something to bolt on. It is the first issue on the list.

## Platforms

macOS, Linux and Windows are in the CI matrix on Python 3.11 and 3.13, six jobs,
green on every push. Each one builds the wheel, installs *that* rather than the
source tree, runs the whole suite against it, and then runs a smoke test that
starts a real server against a fake upstream, polls it, reads the series back
through the API, and drives `start`, `status`, `logs` and `stop` on the pid
file. The Windows job additionally registers the logon task for real, queries
it, and removes it, so the task definition is checked by Task Scheduler rather
than by me.

**Be precise about what that proves.** It proves the wheel installs and the app
runs on all three. It does **not** prove the credential path on any of them: the
smoke test injects an in-memory store, so Windows Credential Manager and the
macOS Keychain are exercised by hand, not by CI. Two known Windows limits:
`stop` terminates the server rather than signalling it, because Windows has no
SIGTERM; and log rotation can fail to roll the 2 MB file while the server holds
it open, in which case it keeps appending rather than losing lines.

## The Terms, stated plainly

Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms),
section 3 ("Use of our Services"), prohibit:

> 7. Except when you are accessing our Services via an Anthropic API Key or
>    where we otherwise explicitly permit it, to access the Services through
>    automated or non-human means, whether through a bot, script, or otherwise.

**QuotaLens is automated access, and a subscription session cookie is not an API
Key.** There is no carve-out in the Terms for reading your own usage. I looked.

Two facts that bear on the risk, neither of which changes the clause:

- I know of no case of Anthropic acting against a read-only usage monitor. The
  one confirmed enforcement in this space was against third-party harnesses that
  *spent* subscription quota by running completions, and those were reinstated
  in May 2026 with metered credits.
- QuotaLens never sends a prompt and never spends a token. It reads two
  endpoints your own browser reads, at most once a minute.

You are the one accepting that risk, not me. Decide with the clause in front of
you.

Unofficial, and not affiliated with or endorsed by Anthropic. It uses
undocumented endpoints that may change without notice, and it only observes: it
cannot raise, extend or bypass a limit.

## License

MIT.
