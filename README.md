# QuotaLens

Claude tells you what is consuming your quota right now, and then forgets.
QuotaLens remembers.

It is a local, self-hosted monitor for Claude Pro and Max subscription usage. It
polls your account every minute, keeps the series in a SQLite file you own, and
leads with the question you open it to ask: **will this session window run out
before it resets, and when.** When something surprises you, the record is still
there — which five-hour session, how steep the climb, the minute it started.

macOS and Linux; **Windows untested** (it should work, and there is no CI
evidence yet — see [Platforms](#platforms)). Binds loopback, keeps your cookie
in the OS keychain, phones nothing home. MIT.

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
curl 'http://127.0.0.1:8787/metrics'                       # Prometheus, hand rolled
curl 'http://127.0.0.1:8787/api/export.csv?table=quota'    # or export.json
curl 'http://127.0.0.1:8787/api/events'                    # threshold crossings, anomalies
quotalens prune --dry-run                                  # what retention would remove
```

Set `QUOTALENS_WEBHOOK_URL` to get one POST when the burn rate crosses
`QUOTALENS_BURN_ALERT` points per hour (default 20), and one when it falls back.
The body carries the rate, the threshold, the headroom and the reset time, and
no account identifier of any kind. It feeds ntfy, Discord, Slack, Pushover or
Home Assistant.

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
one process. The port is derived from the name and is the same on every run;
pass `--port` if two names happen to collide. `QUOTALENS_PROFILE` works too, for
a service unit.

## Running it as a service

```sh
quotalens service install   # macOS LaunchAgent (RunAtLoad, KeepAlive) or systemd user unit
quotalens service status
quotalens service uninstall
```

Every file written and command run is printed so it can be undone by hand. On
Linux the unit only runs while you are logged in unless you enable lingering;
the installer prints the exact `loginctl enable-linger` command. `serve` stays
the foreground command the service manager execs.

**Windows has no user-level service manager this installs into**, so
`service install` says so and stops. Run `quotalens start` at logon instead:
either a Task Scheduler task with the trigger "At log on" and the action
`quotalens start`, or a shortcut to it in `shell:startup`. `start`, `stop`,
`status` and `logs` work the same way on Windows.

A background agent reading the OS keychain may prompt on first run or be
refused; `quotalens status` then says "keyring" specifically rather than "no
data".

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

## Platforms

macOS and Linux are what this has actually run on. Windows is expected to work —
paths, the pid file and the service commands are written for it — but until the
CI matrix has been green on a Windows runner, treat it as untested rather than
supported. `service install` is macOS and Linux only in any case.

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
