# QuotaLens v0.1.0 — scope

Decisions, not options. Reasoning and evidence live in
[`FEATURE-REVIEW.md`](FEATURE-REVIEW.md); this file is what to build and what to
say no to.

## The pitch

> Claude tells you where you are. It doesn't tell you how fast you're moving,
> and it doesn't remember. Settings → Usage is a percentage. Claude Code's
> `/usage` adds a breakdown of the last day on that one machine, and then
> forgets. QuotaLens is a small local web app that polls your account every
> minute and keeps the series in a SQLite file you own. It leads with burn rate
> — points per hour — and with the one question that matters mid-session: will
> this window run out before it resets, and at what time. When something
> surprises you, the record is there: which five-hour session, how steep the
> climb, the minute it started. macOS, Linux and Windows. Binds loopback, keeps
> your cookie in the OS keychain, phones nothing home. MIT.

## Where the MVP line moves

`PLAN.md` puts the line after **M4**, with **M3** (per-project attribution from
Claude Code's JSONL) before it. I'd move it.

| | `PLAN.md` | Here | Why |
|---|---|---|---|
| M3 per-project attribution | pre-1.0 | **out, indefinitely** | Anthropic's `/usage` now attributes to skills, subagents, plugins, MCP servers and scheduled tasks; ccusage owns per-project at 18.3k★; and pooled quota means local logs can only show correlation. Link to both instead. |
| Retention / downsampling | post-1.0 | **in** | Unbounded `sample` growth plus an O(all-history) rebuild every poll. Not a feature; a bug in "leave it running." |
| Desktop notifications | pre-1.0 (M4) | **out** | Three OS paths, dead under systemd-without-a-bus and in Docker — the two ways we tell people to run it. |
| Webhook alerts | not planned | **in** | One code path, no dependency, works everywhere, right audience. |
| Multi-account | post-1.0 | **in, as `--profile`** | The ecosystem's most-repeated request. ~30 lines here, because this is a server, not a menu bar. |
| Docker | pre-1.0 (M5) | **out** | Needs a non-keychain credential path, which forks the security story. Do it deliberately, later. |
| `/metrics`, CSV/JSON | pre-1.0 (M4) | **in, both** | ~70 lines together, no dependencies. Cheaper than choosing. |

Net effect: the release gets **smaller and sooner**, and the largest remaining
build (the JSONL scanner) comes out of the critical path entirely.

## v0.1.0 ships with

Already built — keep, don't touch:

- Cookie in the OS keychain, redaction across logs, tracebacks and error bodies
- `curl_cffi` transport that tells a Cloudflare block apart from a rejected cookie
- Defensive parser: raw payload stored before parsing, generic fallback that
  flags itself, `ParseError` rather than a stored false zero
- Poller with per-condition backoff; forced poll with a floor
- The dashboard: burn rate, runway (countdown, headroom, sustainable rate,
  verdict, projection), five-hour strip, session history with peak, deltas,
  sparkline and coverage, drag-zoom chart, every view a URL
- The six-state system, including em dashes instead of numbers when the reading
  is unknown
- `start` / `stop` / `restart` / `status` / `logs` on a pid file

To build, in order:

1. **Fix reset detection.** Fall through to the percentage-drop rule only when a
   `resets_at` is missing. Today a server-side downward correction is silently
   recorded as a window boundary.
2. **Incremental session rebuild.** Full rebuild at startup only; per poll,
   maintain the current and previous window.
3. **Sample retention.** Keep the last N samples plus every sample with a novel
   top-level key set. A `quotalens prune` command and a documented default.
4. **`--profile NAME`.** Namespaces the keyring username, the database filename
   and the default port. Two accounts is two processes and two bookmarks.
5. **`/api/export.csv` and `/api/export.json`.** Raw `sample` rows only behind
   an explicit flag, with the warning `probe` already prints.
6. **Burn-rate threshold detection → `event`, `/api/events`, and a webhook.**
   `burn_alert_pts_per_hour` already exists in `Settings` and nothing detects
   the crossing. Surface events on the dashboard.
7. **`/metrics`.** Hand-rolled Prometheus text. No `prometheus_client`.
8. **A reset-model watchdog.** If `five_hour.resets_at` moves forward by less
   than five hours without the percentage dropping, the fixed-window assumption
   the session history rests on is wrong — record an event and say so on the
   page.

To delete:

- `legacy.py`, its call in `main()`, its tests, the `QUOTAWATCH_` env warning
  and the README paragraph. Nobody has a `quotawatch` install.
- The Windows Task Scheduler XML generator. Three lines of README instead.
- The per-poll `overage_spend_limit` fetch. Once at startup, then only when the
  usage payload lacks both `spend` and `extra_usage`. Halves our request rate
  against endpoints documented to rate-limit hard.

## v0.1.0 explicitly does not ship

Each of these is a decision, and the README should say so in a "what this
doesn't do" section — with links, because pointing at the better tool is a
feature.

- **Per-project attribution.** Use [ccusage](https://github.com/ccusage/ccusage)
  for per-project tokens and `/usage` in Claude Code for attribution to skills,
  subagents, plugins, MCP servers and scheduled tasks.
- **Desktop notifications.** Use
  [ClaudeUsageBar](https://github.com/Artzainnn/ClaudeUsageBar) on macOS.
- **Anything but loopback.** No `--host`. The dashboard is account data with no
  authentication. If people ask, the answer is a token, not a flag.
- **A container image.** `pipx` / `uvx` only.
- **Reading Claude Code's OAuth credentials.** The cookie stays the only auth
  path. Held in reserve — see `FEATURE-REVIEW.md` §5.6.
- **Any provider but Claude.**
- **API-key cost tracking, a menu bar app, a proxy, a hosted service.** Vision
  non-goals, all still right.

## Release gates

Not features. Things that must be true before tagging, because the README makes
claims that are currently untested.

- **Cross-platform is a headline feature with no CI.** GitHub Actions on
  `macos-latest`, `ubuntu-latest` and `windows-latest`: install from the wheel,
  run `ruff` and `pytest`, and run `quotalens serve` against a fake upstream
  long enough to write rows. Until that is green, the README should say "macOS
  and Linux; Windows untested" rather than claiming three platforms.
- **`service install` on a clean machine.** Install the LaunchAgent on a fresh
  macOS account and the systemd user unit on a fresh Linux user, from a `pipx`
  install, and confirm an hour of successful polling with no keychain prompt. If
  either fails, cut `service install` to *printing* the unit file and the
  command. A background service that silently reads no data is the worst bug
  report this project can generate.
- **The Terms are stated accurately.** Replace the disclaimer with the actual
  clause: Consumer Terms §3.7 prohibits automated access to the Services other
  than via an API Key, this tool is automated access, no enforcement against
  read-only usage monitors is known, and it never sends a prompt or spends a
  token. Say it, don't soften it.
- **The vision's opening paragraph is out of date.** It claims nothing joins
  level and attribution. Claude Code's `/usage` now attributes better than we
  planned to. Rewrite around what is actually unique: *it remembers*.
- **An issue template that tells people not to paste unredacted `probe`
  output.** `PLAN.md` already calls for this and it doesn't exist.
- **Measure the sample table.** Run for a day, check the file size, and put the
  real number in the README next to the retention default. My estimate in the
  review is arithmetic, not a measurement.

## What 1.0 is for

In rough order: the local-session *correlation* overlay (a band on the chart
saying a session was active in this repo — not a token table), reading Claude
Code's credentials as a second auth path, Docker with a file-based credential
store, and a Grafana dashboard JSON once someone reports using `/metrics`.

The open strategic question, which this review does not settle: every serious
competitor is going multi-provider. It violates no stated non-goal and it
multiplies the endpoint-drift surface — our single largest maintenance risk — by
the number of vendors. Decide it deliberately, not by drift.
