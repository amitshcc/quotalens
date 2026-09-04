# QuotaLens — project plan

> **This document is the original plan, kept for its architecture and schema.
> The current scope is [`docs/MVP-SCOPE.md`](docs/MVP-SCOPE.md), which is the
> output of a pre-release review and wins wherever the two disagree.** The
> milestones below described the road to a release; the scope document moved the
> line. In particular **M3 (per-project attribution from Claude Code's JSONL) is
> deferred indefinitely**, because Claude Code's own `/usage` now attributes
> recent usage to skills, subagents, plugins, MCP servers and scheduled tasks —
> better than local logs could, with no cookie — and because quota is pooled
> across surfaces, so local token counts can only ever show correlation with a
> number they cannot see. The evidence is in
> [`docs/FEATURE-REVIEW.md`](docs/FEATURE-REVIEW.md) §2.1 and §2.5.
>
> Retention and multi-account moved the other way, from post-1.0 into v0.1.0.

## Decisions made up front

These are the calls I made to keep the kickoff simple. Change any of them before
you start; changing them later is expensive.

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fastest iteration with Claude Code, cross-platform, and you already read and write it. Go would give a single binary and better distribution — pick it instead if download-and-run matters more than build speed. |
| Web framework | FastAPI + uvicorn | Async polling and HTTP in one process, automatic OpenAPI docs, small. |
| Storage | SQLite via stdlib `sqlite3` | One file, no service, trivial to back up or delete. No ORM. |
| Frontend | Server-rendered HTML + vanilla JS, charts as inline SVG | No build step, no `npm install`, no CDN. The whole app stays one `pip install`. |
| Secrets | `keyring` (Keychain / libsecret / Windows Credential Manager) | Cross-platform, no plaintext cookie on disk. |
| Distribution | `pipx install` / `uvx`, plus a Docker image | Covers developers and homelab users. |
| License | MIT | Matches ClaudeUsageBar, lowest friction for contributions. |

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│ quotawatch (one process, binds 127.0.0.1:8787)            │
│                                                           │
│  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │ quota poller│   │ local scanner│   │ analyzer       │  │
│  │ claude.ai   │   │ ~/.claude/   │   │ burn rate,     │  │
│  │ every 60s   │   │ projects/    │   │ anomalies      │  │
│  └──────┬──────┘   └──────┬───────┘   └───────┬────────┘  │
│         └─────────────────┴───────────────────┘           │
│                           ▼                               │
│                    SQLite (one file)                      │
│                           ▼                               │
│   FastAPI  ──  /  dashboard   /api/*  JSON               │
│               /metrics Prometheus   /export CSV|JSON      │
└───────────────────────────────────────────────────────────┘
```

Two independent collectors write to one store. Neither blocks the other: if the
cookie expires, local scanning keeps working; if Claude Code isn't installed,
quota polling keeps working.

### Data sources

**Account quota** — internal claude.ai endpoints, as used by ClaudeUsageBar
(MIT):

1. Org id from the `lastActiveOrg` cookie, else `GET /api/bootstrap` →
   `account.lastActiveOrgId`
2. `GET /api/organizations/{org}/usage` → `five_hour`, `seven_day`,
   `seven_day_sonnet`, each with `utilization` and `resets_at`, plus a `limits`
   array of model-scoped entries carrying `percent` and
   `scope.model.display_name`
3. `GET /api/organizations/{org}/overage_spend_limit` → `used_credits`,
   `monthly_credit_limit`, `currency`

**Local Claude Code sessions** — `~/.claude/projects/**/*.jsonl`. Directory name
maps to the working directory, giving per-project attribution. Tail
incrementally by file offset; never re-read whole files.

The two sources measure different things and must not be summed. Quota is a
server-side percentage; local logs are token counts. They share a timeline, not
a unit. The UI should overlay them, never add them.

### Schema

```sql
-- raw payloads, for debugging endpoint drift
sample(ts INTEGER, source TEXT, payload TEXT)

-- one row per window per poll
quota(ts INTEGER, window TEXT, label TEXT, pct REAL, resets_at TEXT,
      PRIMARY KEY (ts, window))

overage(ts INTEGER PRIMARY KEY, spent_minor INTEGER, cap_minor INTEGER,
        currency TEXT)

-- one row per Claude Code turn, from JSONL
local_turn(id TEXT PRIMARY KEY, ts INTEGER, project TEXT, session_id TEXT,
           model TEXT, input_tokens INTEGER, output_tokens INTEGER,
           cache_read INTEGER, cache_creation INTEGER)

-- ingestion bookmarks so restarts don't re-scan
scan_state(path TEXT PRIMARY KEY, offset INTEGER, mtime INTEGER)

-- detected climbs, threshold crossings, poll failures
event(ts INTEGER, kind TEXT, detail TEXT)
```

Index `quota(window, ts)` and `local_turn(project, ts)`.

### HTTP surface

| Route | Purpose |
|---|---|
| `GET /` | dashboard |
| `GET /api/quota/current` | latest reading per window |
| `GET /api/quota/series?hours=` | time series |
| `GET /api/burn?window=&lookback=` | points/hour |
| `GET /api/local/projects?hours=` | tokens by project |
| `GET /api/events` | anomalies and failures |
| `GET /api/export.csv`, `/api/export.json` | full dump |
| `GET /metrics` | Prometheus text format |
| `POST /api/session` | store cookie in keyring |
| `GET /api/health` | collector status, last poll, last error |

## Milestones

Ship each one working before starting the next. Tag and release from M4.

**M0 — skeleton.** Repo, MIT license, `pyproject.toml`, config module,
keyring-backed cookie storage, `quotawatch auth` CLI to set it, `--probe`
command that fetches once and prints raw JSON. No UI. Done when `--probe`
returns your real numbers.

**M1 — the poller.** Org resolution, usage and overage fetch, SQLite writes,
`/api/quota/current` and `/api/quota/series`. Backoff on failure, no crash on
401. Done when it runs unattended for an hour and the DB has 60 rows.

**M2 — the dashboard.** Burn rate as the hero figure, SVG time series, per-window
table with reset countdowns, overage spend when non-zero. Dark and light. Done
when you can watch it climb in real time.

**M3 — local attribution. Not being built** (see the note at the top). JSONL
scanner with offset bookmarks, project breakdown, overlay on the quota timeline.
The dashboard points at `claude /usage` and `ccusage` in this slot instead.

**M4 — alerts and export.** Burn-rate threshold detection writing to `event`,
desktop notification (`plyer` or per-OS shell-out), CSV/JSON export,
`/metrics`. Done when a simulated spike fires a notification.

**M5 — ship it.** README with screenshots, install docs for all three OSes,
Dockerfile and compose file, GitHub Actions for lint and tests, `v0.1.0`
release. Done when someone who isn't you can install it from the README alone.

Post-1.0 candidates, deliberately out of scope for now: multi-account support,
a Grafana dashboard JSON, a menu bar companion, retention policies and
downsampling, and correlating scheduled-task run times with quota steps.

## Risks

**Endpoint drift.** These are internal APIs. Mitigation: store every raw
payload, parse defensively with a generic fallback, surface a clear "response
shape changed" error rather than silently reporting zero, and keep `--probe` so
a user can send you the payload in an issue.

**Cookie handling in a public repo.** The highest-consequence bug here would be
leaking someone's session cookie. Mitigations, all non-negotiable: never log it,
redact it from tracebacks and error responses, keyring only, `.gitignore` any
local env file, a pre-commit secret scan, and a security note in the README
explaining that the cookie is equivalent to a password. Also add an issue
template that warns people not to paste `--probe` output without redacting.

**Loopback binding.** Default `127.0.0.1`. If you add a `--host` flag for
homelab use, require an explicit `--i-understand-this-is-unauthenticated` style
confirmation or a token, because the dashboard exposes account data.

**Self-inflicted rate limiting.** Default to 60s polling, document the
trade-off, cap the floor at 30s, and back off on HTTP 429.

**Terms of service.** You're reading your own account's data with your own
credentials, which is what every tool in this space does, but say plainly in the
README that this is unofficial, uses undocumented endpoints, is not affiliated
with or endorsed by Anthropic, and may break. Mirror ClaudeUsageBar's disclaimer.

**Scope creep.** The temptation is to become a general AI cost platform. The
vision's non-goals exist to prevent that. Resist API-key cost tracking in
particular; that's a solved problem.
