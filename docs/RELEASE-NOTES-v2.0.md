# QuotaLens v2.0

*Draft for the GitHub release page. Written for someone who already runs 1.0, or
who arrives from a link and wants to know what changed — not a changelog.*

---

## 1.0 told you about this session. 2.0 remembers the weeks.

1.0 answered one question well: will this five-hour window run out before it
resets? But most people who hit a limit hit the *weekly* one, and the questions
about it span weeks. Is a session costing more of my week than it did last
month? Where did this week go? Am I ahead of last week or behind?

The vendor's page cannot answer any of those, because it forgets the week at
every reset. QuotaLens now writes each week down as it closes.

## What is new

- **Weeks.** Every weekly reset is recorded as it happens — Monday 01:00 UTC,
  give or take a second — and a ledger under History keeps one row per week:
  when it closed and at what, how much was left unused, how many full sessions
  the budget table showed at the reset, and **what one full five-hour session
  cost of that week**, as a median with its usual range. One sentence under the
  table compares the two newest complete weeks, and says *no change can be
  called* when their ranges overlap. It never says a limit was raised: a session
  getting cheaper is either the weekly pool growing or the session pool being
  re-weighted, and the ratio cannot tell which. Existing history is backfilled
  on the first start.
- **A series picker and week ranges.** Chips above the chart pick the series
  (All, Session, Weekly all, one per model meter), remembered in the browser.
  **This week** and **last week** run Monday 01:00 UTC to Monday, the vendor's
  own reset rather than your calendar.
- **Last week, under this week.** On the this-week range, last week's
  all-models line is drawn dashed beneath this week's. The **vs last week** chip
  turns it off.
- **Where the week is heading.** Under the budget table: *"At this pace the week
  ends near 93% (81–100%)"*, or when it runs out, and always with its range.
  The method was picked by back-testing three against real weeks; the straight
  line from the week so far missed by three times as much as the one shipped.
  Hidden for the first day of a week.
- **When you use it.** A 7 × 24 grid of weekday by local hour, each cell the
  average points gained in that hour over the last four complete weeks.
  Achromatic on purpose. Hours the collector missed are left out, not counted
  as zero.
- **Where the quota went.** Anthropic now reports its own split of the week's
  usage by surface — Claude Code, Chats, Cowork, Other. QuotaLens stores it and
  shows a stacked bar and a table, with an estimate of how much of the weekly
  limit each surface took, labelled as one. The Weeks ledger gains a **Mostly**
  column from it.
- **The Fable rule, checked.** The Fable meter's 100% is half the weekly pool,
  per the help centre, so the meter says *half of weekly pool* and no longer
  reads as an empty account. Because that is the vendor's rule and not a
  measurement, two invariants that follow from it are checked on every poll;
  if either ever breaks, a `subcap_violation` event is recorded and the budget
  note says the rule may not hold for your account.
- **The cloud session credit.** The promotional Claude Code cloud-session credit
  arrives in the usage payload looking like a quota window. It is not one, and
  1.0 drew it as one. 2.0 keeps it out of the chart, the budget, Weeks and the
  boost detector, and shows it under Usage credits in dollars, with what is left
  and when it expires. An event, and a desktop notification if they are on,
  seven days and one day before expiry while money is left on it.
- **Your plan, beside the mark.** The header shows it as a muted label after the
  name (*QuotaLens* Max 5x, or Pro, Team, …), and About has a Plan row. It comes from `/api/bootstrap` once a
  day; only capabilities, rate-limit tier and billing type are kept. Nothing on
  the page changes with it — the meters are whatever the readings contain.
- **About, and a daily update check.** `/about` shows the version, the latest
  on PyPI, the install method and where your data lives. Once a day QuotaLens
  asks pypi.org for the latest version — one GET, `User-Agent:
  quotalens/<version>`, nothing else — and when there is a newer one the footer
  says so and the gear gets a small dot. It never updates itself. Off in
  Settings, or with `QUOTALENS_NO_UPDATE_CHECK=1`.
- **`quotalens profiles list`** shows every profile in the data directory, its
  port, and whether it is running, stalled or stopped (`--json` for scripts).
- **The Terms, for two accounts.** The README's *Two accounts* section now
  states what Anthropic's Consumer Terms and Usage Policy say about profiles:
  every profile must be an account that is yours.

## For scripts: every API addition

All additive. Nothing that existed in 1.0 changed shape.

| Route or field | What it returns |
|---|---|
| `GET /api/weeks` | One row per week: `week` (Monday), `closed_at`, `closed_pct`, `left_unused_pct`, `reset_slip_s`, `weekly_all_cost` / `_low` / `_high` / `_n`, `fable_cost` / `_low` / `_high` / `_n`, `full_windows_at_reset`, `full_windows_at_last_weeks_rate`, `mostly` (`{label, percent}` or null) |
| `GET /api/pace` | The Weekly — all models projection: `shown` (and `reason` when not), `estimate: true`, `end_pct` with `end_low` / `end_high`, `runs_out` and `runs_out_ts` with its low and high, `weeks_used`, `method`, `sentence`, `basis` |
| `GET /api/heatmap` | `rows` of 7 days × 24 local hours of average points per hour (null where never collected), `collecting` below 2 complete weeks, `timezone`, `top`, `max` |
| `GET /api/breakdown` | The vendor's split of the week by surface: `rows` of `{key, label, percent, of_limit}`; `of_limit` is the estimate, current week only |
| `GET /api/credits` | `grants`: `{key, label, used, limit, remaining, pct, expires_at, locked_reason}`, dollars as floats; expired grants listed for 7 days |
| `GET /api/version`, `POST /api/version/check` | `{current, latest, checked_ts, error, update_available, upgrade_command}`; the POST asks now, at most once a minute |
| `GET /api/health` | New fields: `profile` (`"default"` when none), `latest_version`, `update_checked_ts`, `plan` (`{label, tier, capabilities}` or null) |
| `GET /api/events?after_id=<id>` | Exclusive, in the order written, with `next_after_id` to page forward; every event row carries its `id`. Without `after_id`, newest first as before |
| Event kinds | `week_reset` (JSON detail: the closed week's figures), `subcap_violation`, `credit_grant_seen`, `credit_grant_expiring`, `update_available` |
| Export tables | `table=weeks`, `table=credits`, `table=surfaces` on `/api/export.csv` and `.json` |
| `GET /about`, `POST /about/check` | The About page (`?fragment=1` for the dialog) and its check button |
| CLI | `quotalens profiles list [--json]` |

The database moves from schema 5 to schema 9 on first start, forward only:
tables `credit_grant`, `surface_share`, `update_check` and `plan` are added.
Weeks are backfilled from the readings already stored, and grants and the
surface split from the raw samples still kept. One thing is removed: readings of the cloud-session credit that 1.0
stored as if it were a quota window, which are re-read from the raw samples as
a credit where those samples still exist.

## What it still deliberately does not do

- **Say a limit was raised because a ratio moved.** The Weeks ledger shows the
  cost of a session moving and by how much. What moved it is not in the data.
- **Change anything because of your plan.** The plan label is shown, never
  branched on; a reading is evidence, a label is a claim.
- **Update itself.** It tells you the command; you run it.
- **Per-project attribution.** `claude /usage` does it better, and the *Where
  the quota went* section says so in one line under the vendor's own split.
- **Raise, extend or bypass a limit.** It only observes.

## Upgrade

```sh
pipx upgrade quotalens      # or: uv tool upgrade quotalens
quotalens stop && quotalens start
# or, if it runs as a service:
quotalens service uninstall && quotalens service install
```

Your database, cookie and settings carry over. The first start migrates the
database and backfills Weeks from the readings you already have, so the ledger
is not empty on day one.

New installs are as before:

```sh
pipx install quotalens      # or: uv tool install quotalens
quotalens auth              # paste your claude.ai session cookie once
quotalens start
open http://127.0.0.1:8787
```

Still four runtime dependencies, no build step, and nothing loaded from anywhere
but the local server. MIT.

## Unofficial, and what that means

**QuotaLens is not affiliated with, endorsed by, or associated with Anthropic.**
It reads undocumented `claude.ai` endpoints with your own session cookie. 2.0
adds one: `/api/bootstrap`, once a day, for the plan. Those endpoints can change
without notice, and when they do this will break until it is fixed. The Consumer
Terms clause that bears on that risk is quoted in full in the README's *"The
Terms, stated plainly"*. **You are the one accepting that risk.**

## Known limits

- **The cost of a session is a ratio, not a ceiling.** The payload carries no
  limit anywhere, so the Weeks ledger can show a session getting cheaper
  relative to the week, never by how much either limit moved.
- **The pace projection learns from your own weeks.** With three or four
  complete weeks behind it, its range is wide and it says so. It is hidden for
  the first day of a week and until one complete week exists.
- **The surface split is the vendor's.** QuotaLens shows it as received; the
  *≈ of the week's limit* column is an estimate built on it.
- **Pro accounts are tested from an assumed payload.** No Pro account has been
  observed; the Pro fixture follows the help centre (no Fable meter). If you are
  on Pro and something looks wrong, the bug template asks for one redacted
  `quotalens probe`.
- **A window that stops arriving keeps its meter, withheld.** If your plan
  changes so that a meter disappears (Max to Pro drops Fable), the old meter
  stays on the page with its value removed rather than leaving. Open work.
- Everything in the 1.0 notes' known limits still applies.
