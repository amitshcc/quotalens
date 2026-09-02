# Claude Code kickoff prompt

Put `VISION.md` and `PLAN.md` in the empty repo first, then paste everything
below the line into Claude Code.

It deliberately scopes the first session to M0 and M1 only. A prompt that asks
for the whole app produces a large, shallow, hard-to-review first commit.

---

We're building **quotawatch**, a free MIT-licensed local monitoring tool for
Claude subscription usage. Read `VISION.md` and `PLAN.md` in this repo before
writing any code — they define the problem, the architecture, the schema, and
the milestones. Follow them; if you think something in them is wrong, say so and
wait rather than quietly diverging.

**This session covers M0 and M1 only.** Skeleton, credential handling, the quota
poller, storage, and the read APIs. No dashboard, no local log scanning, no
notifications. I'd rather have two milestones that work than five that half do.

## Stack

Python 3.11+, FastAPI, uvicorn, stdlib `sqlite3` (no ORM), `httpx`, `keyring`.
`pyproject.toml` with a `quotawatch` console entry point. `ruff` and `pytest`.
Nothing else without asking me first — a short dependency list is a feature
here, since people install this to watch a security-sensitive credential.

## Security rules, non-negotiable

The app handles a claude.ai session cookie, which is equivalent to a password
for the user's account. In a public repo, leaking one is the worst thing this
project could do.

1. The cookie is stored **only** via `keyring`. Never written to a config file,
   never to the database, never to stdout, never to a log.
2. Redact it everywhere: log filters, exception handlers, and any API response.
   If a traceback could contain a request header, scrub headers before it is
   printed or returned. Write a test that asserts the cookie value never appears
   in captured log output or in an error response body.
3. The server binds `127.0.0.1` by default. Do not add a `--host` flag in this
   session.
4. `.gitignore` must cover `.env`, `*.db`, and any local scratch file before the
   first commit.

## What to build

**M0**

- Repo layout: `src/quotawatch/{__init__,config,secrets,client,store,api,cli}.py`,
  `tests/`, `pyproject.toml`, `LICENSE` (MIT), `.gitignore`, a `README.md` stub.
- `quotawatch auth` — prompt for the cookie, store it in the keyring, confirm by
  making one authenticated call. Never echo the value back.
- `quotawatch probe` — one fetch, pretty-print the raw JSON and the parsed
  result, so a user can debug endpoint drift and paste output into an issue.
  Print a warning above the output telling them to redact it before sharing.

**M1**

- Client for the three endpoints documented in `PLAN.md`. Org id from the
  `lastActiveOrg` cookie with `/api/bootstrap` as fallback; cache it in memory.
  Send the header set the reference implementation uses (Cookie, Accept,
  Content-Type, Origin, Referer, User-Agent).
- A parser that handles the documented shape (`five_hour`, `seven_day`,
  `seven_day_sonnet`, and the model-scoped `limits` array) and falls back to a
  generic tree walk if those keys are absent. When it can't parse at all, record
  an `event` row and surface it on `/api/health` — never silently store zero,
  since a false 0% is worse than no data.
- Storage per the schema in `PLAN.md`. Migrations can be a simple
  `CREATE TABLE IF NOT EXISTS` plus a `schema_version` row.
- A background poller task on the FastAPI lifespan. Default 60s, floor 30s,
  exponential backoff on failure, clear handling of 401 (cookie expired — say so
  in `/api/health`, don't retry aggressively) and 429 (back off hard).
- Routes: `/api/quota/current`, `/api/quota/series?hours=`, `/api/burn`,
  `/api/health`.
- Burn rate: change in percentage points per hour over a configurable lookback,
  default 15 minutes. Handle the window reset — when a value drops sharply
  because the 5-hour window rolled over, that is not a negative burn rate, it's
  a discontinuity. Split the series at resets and don't compute across the
  boundary. Get this right; it's the core metric and the easiest thing to get
  subtly wrong.

## How I want you to work

- Read `PLAN.md`, then tell me your implementation plan and wait for me to
  approve it before writing code.
- Small commits with clear messages, one logical change each.
- Write the tests as you go, not at the end. I care most about: the parser
  against several payload shapes including a drifted one, the burn-rate
  calculation including a window reset in the middle of the series, and the
  cookie-redaction assertions.
- Mock all HTTP in tests. No test may touch the network.
- Type hints throughout. `ruff check` clean before you say you're done.
- When you hit a decision `PLAN.md` doesn't cover, ask rather than guessing —
  but batch the questions instead of stopping every few minutes.

## What "done" means for this session

I can run `quotawatch auth`, then `quotawatch serve`, leave it for an hour, and
`curl localhost:8787/api/quota/series?hours=1` returns roughly 60 readings with
sane percentages and reset timestamps. `/api/health` tells me when it last
polled and what went wrong if anything did. `pytest` passes. The word "cookie"
appears in no log line with a value after it.

Start by reading the two docs and giving me your plan.
