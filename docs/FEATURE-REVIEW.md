# QuotaLens — pre-release feature review

An outside read of the repo at `b83f579`, against the tools in this space, what
their users ask for, and Anthropic's own documentation. Written 2026-09-03.

Every claim below links to something I actually fetched. Where I am inferring
rather than citing, the line starts with **(inference)**. Where I looked and
found nothing, I say so — an absence of demand is a finding, and a padded
section would be worse than a short one.

---

## 0. The short version

Three things in this project are wrong, and one of them is load-bearing.

1. **Anthropic shipped the attribution feature.** Claude Code's `/usage` now
   breaks recent usage down by skill, subagent, plugin, MCP server, *and
   scheduled task*, on Pro and Max. That is a better answer to "what consumed
   my quota" than per-project attribution from JSONL, and it is official, free,
   and needs no cookie. M3 as planned is no longer the differentiator. What
   survives — and it is still a real gap — is **history**: `/usage` shows the
   last 24 hours or 7 days from *this machine*, and forgets.
2. **The Consumer Terms contain a clause that squarely covers what this tool
   does**, and the README's current disclaimer is softer than the actual text.
   That does not mean don't ship. It means say it accurately.
3. **`resets_at` minus five hours is an inference, not a documented fact**, and
   it is presented as fact in a docstring, the README, and the derived session
   history. Anthropic has never published the anchoring algorithm.

Plus two real bugs found while reading, both in the reset-detection path, both
cheap to fix, both of a kind that corrupts stored history silently (§2.4).

The feature verdict, compressed: **the MVP line in `PLAN.md` is in roughly the
right place but drawn around the wrong things.** M3 should move out of the
pre-1.0 path entirely. Retention should move *in* — it is not a nicety, it is
the difference between "leave it running for a month" being true and false.
Notifications should ship as a webhook and not as a desktop notification.
Multi-account, which every competitor's users ask for and none of the popular
ones ship, costs about thirty lines here and should go in.

---

## 1. What actually exists

Assessed from source, not from `PLAN.md`. The README's "M0 to M2 are done" is a
significant undersell.

| Area | State |
|---|---|
| Credentials | Keyring only, `Redactor` scrubs logs/tracebacks/exception args, `probe` masks UUIDs. Genuinely careful. |
| Transport | `curl_cffi` with browser impersonation; Cloudflare challenge distinguished from cookie rejection (`BlockedError` vs `AuthError`). |
| Parser | Defensive: window detection requires pct **and** `resets_at`; `limits` de-duplicated against top-level blocks; model-scoped entries become their own windows; generic tree-walk fallback that flags itself; `ParseError` rather than a stored false zero. This is the best-engineered part of the repo. |
| Storage | SQLite, WAL, `schema_version` with real migrations. `local_turn` and `scan_state` exist but nothing writes them. |
| Poller | Backoff with separate paths for auth / blocked / rate-limited / parse failure; forced poll with a 10s floor; raw payload stored before parsing. |
| Dashboard | Burn rate, runway (countdown, headroom, sustainable rate, verdict sentence, projection crossing 100%), five-hour strip, session history table with peak/deltas/sparkline/coverage, drag-zoom chart, series toggles, range presets, every view a URL, full no-JS fallback. |
| States | Six, with the magnitude/epistemic split enforced in `state.py`. A stale reading shows an em dash, never a number. Correct and unusual. |
| Service | `start`/`stop`/`restart`/`status`/`logs` on a pid file, plus `service install` writing a LaunchAgent, a systemd user unit, or a Windows Task Scheduler XML. |
| **Not built** | `/metrics`, CSV/JSON export, `/api/events`, alerts, notifications, local JSONL scanning, Docker, CI. |

Four runtime dependencies (`fastapi`, `uvicorn`, `curl_cffi`, `keyring`). No
build step, no CDN, no chart library. That restraint is worth defending.

---

## 2. Things that contradict the project's assumptions

### 2.1 Anthropic shipped attribution, and it is better than M3

[Claude Code's cost docs](https://code.claude.com/docs/en/costs) describe the
current `/usage` command. On a Pro, Max, Team, or Enterprise plan it shows:

> **Attribution**: recent usage attributed to skills, subagents, plugins, and
> individual MCP servers, each shown as a percentage of the total.
>
> **Behavior flags**: behaviors such as long context or cache misses, flagged
> when one accounts for 10% or more of recent usage.
>
> **Loops**: a row for each of the heaviest `/loop` or other scheduled tasks
> that ran recently, ordered by total tokens, with a count of the rest. Claude
> Code reports how often each task fires, how many times it ran, its total and
> per-run tokens, and when it last ran.

`VISION.md` opens with this problem:

> A scheduled task or a background agent session runs on Anthropic's
> infrastructure, not on your machine, so nothing appears in `ps` or in your
> task manager. It consumes quota silently. You find out at 100%, hours after
> the fact, with no record of when the climb started or which project caused it.

The Loops row answers "which scheduled task." The Attribution block answers
"which skill or MCP server," which per-project attribution never could. The
["Why usage climbs in a long session"](https://code.claude.com/docs/en/costs)
section names cross-session messages, goal check-ins, agent teammates and
compaction as causes — a catalogue of exactly the invisible consumers the
vision is about.

**What still doesn't exist, and it is the good half of the vision.** The same
doc says of that breakdown:

> Press `d` or `w` to switch between the last 24 hours and the last 7 days. The
> figures are approximate and computed from local session history on this
> machine, so usage from other devices or claude.ai is not included.

So: no history beyond 7 days, no record you can look back at, one machine only,
Claude Code only, terminal only, and gone when you close it. And it reads the
same rate-limited endpoint, with a documented degradation:

> When the request for your plan limits fails, most often because the usage
> endpoint is rate limited, `/usage` shows the last usage bars it loaded on this
> machine within the past 60 minutes […] Without a snapshot from the past 60
> minutes, `/usage` reports that the usage endpoint is rate limited.

**Consequence for this project.** The pitch is no longer "nobody joins level and
attribution." It is: *Anthropic will tell you what is consuming your quota right
now, on this machine, and then forget. QuotaLens remembers.* That is a smaller
claim and a truer one, and it happens to be the claim `VISION.md` already makes
under "History is the product." Rewrite the opening of `VISION.md` and the
README around that, and drop the framing that implies nobody can see
attribution.

### 2.2 The Terms clause is real, and the README is softer than it

`PLAN.md` says: *"You're reading your own account's data with your own
credentials, which is what every tool in this space does."* True, and not the
whole picture. The
[Consumer Terms](https://www.anthropic.com/legal/consumer-terms), Section 3
("Use of our Services"), prohibits, verbatim:

> 4. To crawl, scrape, or otherwise harvest data or information from our
>    Services other than as permitted under these Terms.
>
> 7. Except when you are accessing our Services via an Anthropic API Key or
>    where we otherwise explicitly permit it, to access the Services through
>    automated or non-human means, whether through a bot, script, or otherwise.

Clause 7 is on point. A polling daemon is automated access, and a subscription
session cookie is not an API Key. I found **no carve-out** anywhere for reading
your own usage.

What I also found, and this matters: **no evidence of enforcement against
read-only usage pollers.** No takedown, no ban attributable to it, in any of the
trackers I looked at. The one confirmed enforcement action in this space —
Anthropic blocking third-party agent harnesses (OpenClaw, Conductor, Zed) from
subscription OAuth in April 2026, [reinstated in May with metered Agent SDK
credits](https://venturebeat.com/technology/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch)
— targeted tools that *spend* quota by running completions. QuotaLens spends
none. The one ToS objection filed against a tool in this space,
[`howincodes/claude-code-limiter#4`](https://github.com/howincodes/claude-code-limiter/issues/4),
is about credential *sharing* between people, which doesn't apply here either.

**Recommendation.** Replace the README's disclaimer with the clause. Something
like: *"Anthropic's Consumer Terms §3.7 prohibit automated access to the
Services other than via an API Key. This tool is automated access. I know of no
case of Anthropic acting against a read-only usage monitor, and this one never
sends a prompt or spends a token, but you are the one accepting that risk, not
me."* That is more honest than the current wording and it is also, in my view,
better marketing — the audience for a local MIT monitoring tool rewards being
told the truth.

### 2.3 `resets_at` − 5h is an inference presented as documentation

`sessions.py` states it as fact:

> The 5-hour window is not a clock schedule: it starts at the first message and
> expires five hours later. The API's `five_hour.resets_at` *is* the expiry of
> the running window, so a jump forward means a new session started, and the
> start is that value minus five hours.

The whole session-history table, the hour strip, the auto range and the coverage
badge rest on that. Anthropic has never published the algorithm. What the docs
do say:

- ["Your session-based usage limit will reset every five hours."](https://support.claude.com/en/articles/11049741-what-is-the-max-plan)
  — no anchoring detail.
- Team/Enterprise seats draw from an allowance that "resets on a **rolling**
  five-hour window and a weekly window"
  ([Claude Code costs](https://code.claude.com/docs/en/costs)) — "rolling" is
  used loosely and is not defined.
- Weekly is different and *is* documented: ["Weekly limits reset at a fixed time
  each week that is assigned to your account… Your reset day and time stay the
  same regardless of when you start using Claude."](https://support.claude.com/en/articles/8325606-what-is-the-pro-plan)

There is a piece of evidence in your own repo that supports the inference, and
it is better than anything in the docs. `burn.py` records that the server
recomputes `resets_at` on every call and only the sub-second part moves
(`12:40:00.421772` then `12:40:00.656558`). A sliding window would move the
whole second. **(inference)** That is consistent with a window anchored at first
message and fixed thereafter.

**Recommendation, and it is cheap.** Don't just soften the wording — make the
assumption falsifiable. You already store every raw sample. Add one check: if
`five_hour.resets_at` moves forward by *less than* five hours between polls
without the percentage dropping, the fixed-window model is wrong, and that gets
an `event` row and a line on the dashboard. Twenty lines, and it converts a
buried assumption into a monitored invariant. Given the field's history of
reset-time weirdness — [#52469 "Displayed reset time jumped to 3:00 PM PDT.
Window extended by ~4 hours… No visible trigger"](https://github.com/anthropics/claude-code/issues/52469),
[#8926 Claude Code and claude.ai disagreeing about the reset
time](https://github.com/anthropics/claude-code/issues/8926) — you will
eventually see it fire.

### 2.4 Two bugs in reset detection

**(a) The percentage-drop rule fires even when `resets_at` says otherwise.**
`burn.py`:

```python
def is_reset(prev, cur):
    if prev.resets_at and cur.resets_at and resets_at_changed(prev.resets_at, cur.resets_at):
        return True
    return (prev.pct - cur.pct) > RESET_DROP_PCT
```

The module docstring says the drop rule "covers payloads where `resets_at` is
absent or null." The code applies it unconditionally. So a server-side downward
correction of more than 5 points, with `resets_at` unchanged, is classified as a
window reset. That is not hypothetical: Anthropic has publicly confirmed
shipping a bug that "showed an incorrect weekly usage limit" to ~3% of Claude
Code Max and Pro users, and users report corrections in both directions
([#12149: "My weekly usage limit jumped from 13% to 84% after just 2
prompts"](https://github.com/anthropics/claude-code/issues/12149)). When it
fires wrongly it splits the burn-rate segment, truncates a session window, and
writes a bad row into `session_window` that survives the rebuild. Fix: only fall
through to the drop rule when at least one `resets_at` is missing.

**(b) `rebuild_sessions` is O(all history) on every poll.** `poller._collect`
calls it each cycle; `sessions.rebuild` calls `store.quota_series(0)` — the
entire `quota` table — derives every window from the beginning of time, then
`DELETE`s and re-inserts the whole `session_window` table. At 60s with six
windows, a month of history is ~260,000 rows read, re-parsed and re-grouped
every minute, forever, growing without bound. It gets slowest for exactly the
user the product is for: the one who left it running. Fix: full rebuild at
startup and on migration; incrementally maintain only the current and previous
window per poll.

Both belong in v0.1.0, before anyone has data to corrupt or accumulate.

### 2.5 Cross-surface pooling is confirmed, which cuts both ways

> "your usage of all different Claude product surfaces (claude.ai, Claude Code,
> Claude Desktop) counts towards the same usage limit"
> — [How do usage and length limits work?](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work)

Good news: it verifies the vision's central claim that `ccusage` cannot see web,
Cowork or scheduled-task usage, because the quota number is whole-account and
the local logs are not.

Bad news for M3: it means local JSONL is a *fraction* of an unknown
denominator. A per-project table sitting under a quota chart will be read as
"these projects account for the climb," and it cannot be. `DESIGN.md` §12
already knows this — *"Token counts and quota points are never summed or plotted
on one axis. They share a timeline, not a unit"* — which is the right rule and
also an admission that the feature can only ever produce correlation.

### 2.6 Fable has a sub-cap the tool doesn't model

> "You can use **up to 50% of your weekly usage limits** on Fable models at no
> extra cost."
> — [Claude Fable models on your plan](https://support.claude.com/en/articles/15424964-claude-fable-models-on-your-plan)

The parser will chart a Fable entry generically as `limit:fable`, which is fine.
But a Fable limit at 100% means *half the weekly pool*, not an exhausted
account. Low severity; worth a label rather than code.

---

## 3. Competitive matrix

Everything observed 2026-09-03. "History" means a durable time series you can
look back at, not a live sparkline.

| Tool | Platform | Source | Quota % | History | Burn/pace | Attribution | Notify | Export | Prom | Multi-acct | Windows |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **QuotaLens** | Py, web | claude.ai `/usage` | ✅ | ✅ SQLite | ✅ hero | ❌ | ❌ | ❌ | ❌ | ❌ | claimed, untested |
| [ccusage](https://github.com/ccusage/ccusage) 18.3k★ | CLI, all | local JSONL | ❌ none | ❌ | ❌ | ✅ per-project/model | ❌ | ✅ JSON | ❌ | ❌ | ✅ |
| [ClaudeUsageBar](https://github.com/Artzainnn/ClaudeUsageBar) 281★ | macOS bar | same endpoints | ✅ | ❌ | ❌ | ❌ | ✅ 25/50/75/90% | ❌ | ❌ | ❌ (asked) | ❌ |
| [Claude-Usage-Tracker](https://github.com/hamed-elfayome/Claude-Usage-Tracker) 3.4k★ | macOS bar | OAuth creds | ✅ | ✅ charts | ✅ 6-tier pace | ❌ | ✅ | ❌ | ❌ | ✅ unlimited | ❌ |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) 8.6k★ | CLI, Py | local logs | ❌ | ✅ opt-in | ✅ token burn | partial | via state | ✅ CSV/JSON | ❌ | ❌ | ✅ |
| [phuryn/claude-usage](https://github.com/phuryn/claude-usage) ~2k★ | local web | local logs | ❌ | ✅ SQLite | ❌ | token/cost | ❌ | ❌ | ❌ | ❌ | ✅ |
| [usage-monitor-for-claude](https://github.com/jens-duttke/usage-monitor-for-claude) 270★ | Win/Linux tray | — | ✅ | ❌ ([open ask](https://github.com/jens-duttke/usage-monitor-for-claude/discussions/24)) | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ | ✅ |
| [ai-usagebar](https://github.com/akitaonrails/ai-usagebar) 328★ | Rust, Waybar | multi-provider | ✅ | ❌ explicit | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |
| [Usage4Claude](https://github.com/f-is-h/usage4claude) 301★ | macOS bar | — | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| [aistat](https://github.com/drogers0/aistat) | CLI | `api/oauth/usage` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ switching | ✅ |
| **Claude Code `/usage`** (official) | CLI | official | ✅ | ❌ 24h/7d, forgets | ❌ | ✅ **best in class** | ❌ | ❌ | ❌ | ❌ | ✅ |

**What we do that nobody does:** durable subscription-quota history in a
cross-platform, self-hosted, MIT tool, with burn rate and runway as the lead
figure. Two things nobody has shipped at all in this space: a Prometheus
endpoint, and a projection that says *when* you will run out.

**What they do that we don't:** notifications (ClaudeUsageBar, Claude-Usage-
Tracker, usage-monitor-for-claude — table stakes), multi-account
(Claude-Usage-Tracker, ai-usagebar, aistat), export (ccusage,
Claude-Code-Usage-Monitor), and credential reuse instead of a pasted cookie
(Claude-Usage-Tracker, aistat).

**Prior art on the actual idea.** Three projects have tried subscription-quota
history and none has traction:
[jimdawdy-hub/claude-usage-tracker](https://github.com/jimdawdy-hub/claude-usage-tracker)
(1★, hits the same endpoint, SQLite, "historical charts"),
[cooco119/claude-quota-tracker](https://github.com/cooco119/claude-quota-tracker)
(0★, 4 commits, macOS-only), and
[tugrulcank-netizen/claude-usage-tracker](https://github.com/tugrulcank-netizen/claude-usage-tracker)
(0★, Chrome extension, scrapes the settings page). That last one states its
reason for existing plainly: *"Anthropic shows your usage on the Settings page,
but there's no history, no export, and no cost estimation."* The only polished
quota-history product is
[Usage for Claude](https://hayek.github.io/ClaudeUsagePage/), which is
proprietary and macOS/iOS-only — it validates the demand and does not compete
on this project's terms.

The niche is real and it is empty. The percentage-in-a-menu-bar niche is
saturated with ten-plus tools, several with hundreds of stars, and the README is
right to cede it.

---

## 4. The demand evidence, in the users' own words

The strongest and most repeated complaint is not "I want a feature," it is
opacity. From the 705-comment [HN thread on the weekly-limits
announcement](https://news.ycombinator.com/item?id=44713757), July 2025:

> "I'm ok using a limited resource _if_ I know how much of it I am using. The
> lack of visible progress towards limits is annoying." — steveklabnik

> "I live in constant anxiety not knowing how far into my usage I am" — blalezarian

> "Rationing implies an ability to measure: this amount per day. But measuring
> the remaining amount is exactly what Claude Code API does not provide" — nine_k

Planning around resets is a real behaviour, not a hypothetical. Someone wrote an
essay about restructuring his sleep around the five-hour window
([mattwie.se](https://mattwie.se/no-sleep-till-agi), 221 points on
[HN](https://news.ycombinator.com/item?id=44860015)):

> "What if I sleep like a sailor in order to maximize my Claude usage limit? So I did."

That is the runway feature's user, and the runway feature is already built. It
is the best thing in the repo and the README should lead with it.

The background-consumption story has one vivid, well-documented case
([#75314](https://github.com/anthropics/claude-code/issues/75314), July 2026) —
ten background agents running 34+ hours, ~1.08M tokens, *"i didnt notice it was
running in the background"* — and I could not find a pattern of independent
reports. **The vision's central scenario is real but the sample is thin.** Worth
knowing before you build the whole README around it.

On whether users want to be told or want to look: they ask for both, in the same
breath. [#26177](https://github.com/anthropics/claude-code/issues/26177) asks
for a `/usage` command **and** a threshold alert **and** a status-line display.
[#17431](https://github.com/anthropics/claude-code/issues/17431) proposes the
same three. I found no evidence of two camps.

And Anthropic has declined to build it. #17431 ("users must manually navigate to
claude.ai > Settings > Usage… This leads to unexpected rate limiting
mid-task") was closed **not planned**. So was
[#1325](https://github.com/anthropics/claude-code/issues/1325), the per-project
usage request. `/usage` shipping later is a partial reversal on attribution, not
on history.

---

## 5. Ranked candidates

Eight, ordered by (value × strength of evidence) ÷ cost.

---

### 1. Retention, and an incremental session rebuild — **MVP**

**Problem.** The pitch is "leave it running for a week." Someone who does that
finds a database that grows without bound and a poll loop that gets slower every
day.

**Evidence.** Read from the code, not from users. `record_sample` writes every
raw payload forever, twice per poll (usage + overage). **(inference, arithmetic
— check it with `quotalens probe | wc -c` before believing me)**: at a plausible
2–4 KB per payload and 60s polling, that is roughly 5–10 MB/day, 2–4 GB/year, in
a file nobody prunes. Plus the O(all-history) rebuild in §2.4(b). `PLAN.md` puts
retention post-1.0; that is a misclassification — this is an unbounded-growth
bug wearing a feature's clothes.

**Cost.** No dependency. Retention: keep the last N samples plus every sample
whose top-level key set is novel — which preserves exactly the debugging value
the `sample` table exists for while bounding it. ~30 lines and a `VACUUM`.
Rebuild: ~40 lines. A `quotalens prune` command and a documented default.

**Fits the vision.** Directly: "History is the product" requires history to be
survivable.

**Verdict: MVP.** Cheaper now than after anyone has a 3 GB file.

---

### 2. `--profile` for multi-account — **MVP**

**Problem.** "I have a Claude subscription for work and a personal Claude
subscription. It would be nice if I could easily switch between accounts to view
usage without having to set the cookie every time."
([ClaudeUsageBar#40](https://github.com/Artzainnn/ClaudeUsageBar/issues/40),
July 2026, unanswered.)

**Evidence.** The single most repeated request across the whole ecosystem, and
it is your specific question — real demand, not padding. Four independent
trackers: ClaudeUsageBar [#40](https://github.com/Artzainnn/ClaudeUsageBar/issues/40)
and [#35](https://github.com/Artzainnn/ClaudeUsageBar/issues/35) plus an
unmerged community PR ([#102](https://github.com/Artzainnn/ClaudeUsageBar/pulls));
ccusage [#317 "Account Based?"](https://github.com/ccusage/ccusage/discussions/317)
and [#358](https://github.com/ccusage/ccusage/discussions/358); ai-usagebar and
aistat both ship account switching; Claude-Usage-Tracker ships unlimited
profiles as a headline feature and it is the most mature tool in the space.

**Cost.** Near zero *because of the architecture you chose*. This is a server on
a port, not a menu bar with one status item. `--profile work` namespaces three
constants: `KEYRING_USERNAME` (currently the fixed
`"claude.ai-session-cookie"`), the db filename, and the default port. No UI, no
account picker, no session juggling. Two accounts is two processes and two
bookmarks. ~30 lines plus a README paragraph.

**Fits the vision.** Yes. Nothing in the non-goals touches it.

**Verdict: MVP.** The best value-per-line item in this document. It converts the
ecosystem's #1 open request into a documented one-liner.

---

### 3. CSV and JSON export — **MVP**

**Problem.** "Anthropic shows your usage on the Settings page, but there's no
history, no export, and no cost estimation" — the stated reason
[tugrulcank-netizen/claude-usage-tracker](https://github.com/tugrulcank-netizen/claude-usage-tracker)
exists.

**Evidence.** ccusage ships JSON; Claude-Code-Usage-Monitor ships CSV/JSON;
Team/Enterprise get a [spend report
CSV](https://support.claude.com/en/articles/12883420-view-usage-analytics-for-team-and-enterprise-plans).
Export is table stakes for a data tool. **(inference)** It also has a second job
here: it is how a user sends you a reproduction that isn't their raw payload.

**Cost.** ~30 lines, stdlib `csv` and `json`, two routes. One decision to make:
`sample` rows are raw payloads and could carry identifiers, so export them only
behind an explicit flag with the same warning `probe` already prints.

**Fits the vision.** It is a stated principle: "Your data leaves easily."

**Verdict: MVP.**

---

### 4. Threshold alerts → `event` + webhook — **MVP**

**Problem.** You look at a dashboard when you already suspect something. The
vision's scenario — a background agent burning quota overnight — is the one
where nobody is looking.

**Evidence.** Strong. Users ask for push *and* pull in the same issue
([#26177](https://github.com/anthropics/claude-code/issues/26177),
[#17431](https://github.com/anthropics/claude-code/issues/17431)). Threshold
notifications at 25/50/75/90% are one of ClaudeUsageBar's four headline
features; Claude-Usage-Tracker and usage-monitor-for-claude both ship them. This
is the clearest table-stakes gap in the matrix.

**Cost, and why *not* desktop notifications.** `PLAN.md` M4 says "desktop
notification (`plyer` or per-OS shell-out)." I'd cut that and ship a webhook
instead:

- Desktop notification is three OS code paths and three failure modes, plus a
  dependency if you use `plyer`.
- It is **dead in exactly the deployments this tool is designed for**: a systemd
  user unit without a session bus can't reach a notification daemon, and a
  container has no desktop at all. You would be shipping a headline feature that
  doesn't work when the tool runs the way you recommend running it.
- The audience is, in the vision's own words, "disproportionately developers and
  infra people." A webhook feeds ntfy, Discord, Slack, Pushover and Home
  Assistant — one code path, identical everywhere, no dependency (`curl_cffi` is
  already there).
- Anyone who genuinely wants a macOS desktop notification already has
  ClaudeUsageBar, which the README already links to and calls complementary.

~40 lines: a rule (`burn_alert_pts_per_hour` already exists in `Settings` and is
already threaded through, but nothing detects the crossing), an `event` row, a
POST, and the `/api/events` route `PLAN.md` specifies and the code never grew.
Plus dashboard surfacing — you already store events and only expose them under
`/api/health`.

**Fits the vision.** Yes. A webhook is not a hosted service.

**Verdict: MVP for detection + `/api/events` + webhook. Desktop notifications:
no, for v0.1.0.**

---

### 5. `/metrics` in Prometheus text format — **MVP**

**Problem.** The infra user wants this next to their other graphs, not in a
twelfth browser tab.

**Evidence.** Mixed, and worth stating honestly. **No tool in the ecosystem
ships a metrics endpoint** — searched across the tools in §3 and found nothing.
That is either an open gap or an absent demand, and I cannot distinguish them
from the evidence. **(inference)** I lean gap, for three reasons: the vision
explicitly targets homelab and infra people; a self-hosted loopback daemon
already selects for that audience; and a Grafana panel is a *reason to leave it
running for a month*, which is the mechanism by which this product accrues its
own value. That last point is why I'd rank it above its raw demand evidence.

**Cost.** ~40 lines of string formatting. **Do not add `prometheus_client`** —
five gauges rendered by hand keeps the dependency list at four.

**The catch, and it is real.** Loopback-only means only a Prometheus on the same
host can scrape it. `PLAN.md` already flags `--host` as a risk, correctly: the
dashboard exposes account data with no authentication. **Ship `/metrics`; do not
ship `--host` in v0.1.0.** Document "run Prometheus on the same host, or put it
behind your existing reverse proxy." If people ask for `--host`, that is
evidence, and the answer is a token, not a flag.

CSV (#3) and `/metrics` are not alternatives — they serve different jobs.
Together they are ~70 lines and no dependencies, which is cheaper than the
argument about which one to pick.

**Fits the vision.** Stated principle.

**Verdict: MVP for `/metrics`. `--host`: no.**

---

### 6. Read Claude Code's credentials instead of a pasted cookie — **post-1.0**

**Problem.** Onboarding is "open DevTools on claude.ai/settings/usage, copy the
whole Cookie header, paste it into a terminal," and then do it again when it
expires. This is the single highest-friction step in the product, and it is a
recurring failure mode:
[ClaudeUsageBar#39 "HTTP 403 persists after updating Claude session
cookie"](https://github.com/Artzainnn/ClaudeUsageBar/issues/39) — *"Previously,
whenever this happened, resetting the Claude usage cookie fixed the issue.
However, today the same workaround no longer works."*

**Evidence.** Strong that the alternative works. Claude Code's credentials are
[officially documented](https://code.claude.com/docs/en/authentication): macOS
Keychain under service `"Claude Code-credentials"`, `~/.claude/.credentials.json`
(mode 0600) on Linux and Windows, relocatable via `CLAUDE_CONFIG_DIR`.
`claude setup-token` mints a documented **one-year** OAuth token, which sidesteps
implementing a refresh flow entirely. `aistat` and
[Claude-Code-Usage-Monitor#202](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor/issues/202)
both hit `https://api.anthropic.com/api/oauth/usage` with
`Authorization: Bearer <token>`, and Claude-Usage-Tracker (3.4k★) reuses Claude
Code credentials as one of its three auth paths. The response carries the same
`five_hour` / `seven_day` / model-scoped shape your parser already handles.

**Cost, and why not now.** Three reasons to wait:

1. The endpoint is *documented-fragile*. Three issues — [#31637 "aggressively
   rate limits, making usage monitoring
   unusable"](https://github.com/anthropics/claude-code/issues/31637),
   [#31021](https://github.com/anthropics/claude-code/issues/31021), #30930 —
   all closed **not planned**, all reporting persistent unrecoverable 429s with
   no `Retry-After`. Anthropic's own docs describe a last-known-value fallback
   because `/usage` hits this routinely. Community reports say a
   `claude-code/<version>` User-Agent is required to avoid instant 429s
   ([#202](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor/issues/202))
   — i.e. it gates on client fingerprint, which is a nastier dependency than the
   Cloudflare impersonation you already handle.
2. It is a second full client, a second parser path, and a second auth story to
   document — the largest cost item in this list.
3. Reading credentials another program wrote, to make a request that program
   didn't make, is a *greyer* posture than reading your own cookie, not a
   lighter one. §2.2 applies identically either way.

**Verdict: post-1.0.** But hold it ready: if the cookie path breaks (and every
competitor's changelog says it will), this is the escape hatch. The cheap
version — accept `CLAUDE_CODE_OAUTH_TOKEN` from the environment, per
`setup-token`, and skip credential-file reading and refresh entirely — is a
credible first cut.

---

### 7. Local Claude Code correlation overlay (M3, reframed) — **post-1.0**

**Problem.** "The rate spiked at 03:00; what was running?"

**Evidence, and it does not support the feature as planned.** Four things:

- **Anthropic now answers the question better** (§2.1). `/usage` attributes to
  skills, subagents, plugins, MCP servers and scheduled tasks. Per-project is a
  coarser cut of a finer answer.
- **ccusage already does per-project well**: 18.3k★, 68k weekly npm downloads,
  `--project` and `--instances` grouping, Windows binaries. Doing it again is
  rebuilding the most-used tool in the space, worse.
- **The demand is team-shaped, not subscriber-shaped.**
  [#1325](https://github.com/anthropics/claude-code/issues/1325) asks for
  per-project so that "in the console an additional filter could be added that
  shows cost and token usage on a per project basis… across all users." That is
  cost allocation for an org. I found **no** individual Pro/Max subscriber asking
  to compare their own repos. This is the one place where I think the project
  has assumed a developer's want rather than observed a user's.
- **It cannot deliver attribution, only correlation** (§2.5). Quota is a
  server-side percentage of a whole-account pool; local logs are token counts
  from one surface on one machine. `DESIGN.md` §12 already forbids putting them
  on one axis, which is correct and is also the feature admitting its ceiling.

**Cost.** The largest in the list: a JSONL scanner, offset bookmarks, mtime and
rotation handling, and a *format you don't control* — ccusage has 132 npm
releases largely chasing exactly that drift. A new failure mode ("attribution
stopped working") on a tool whose current failure modes are all honest and
well-modelled.

**What is worth building eventually** is the small version, and it isn't a
table: mark on the existing quota chart *that a Claude Code session was active,
in which repo*, as a rug or a band on a second axis, with no token numbers at
all. That answers "what was running" without implying "this caused 40%." It is a
tenth of the work and it does not lie.

**Verdict: post-1.0, reframed as correlation.** For v0.1.0, delete the
per-project column from the README's ambitions and link to
[ccusage](https://github.com/ccusage/ccusage) and `/usage` in a "what this
doesn't do" section. Linking to the better tool is a feature.

Note: the `local_turn` and `scan_state` tables already ship in the schema. Leave
them — they cost nothing and removing them costs a migration.

---

### 8. Docker image and compose file — **post-1.0**

**Problem.** The homelab user wants it running on the NAS, not on a laptop that
closes.

**Evidence.** Weak. `VISION.md` and `PLAN.md` both assert it; I found no user
asking for a containerised Claude usage monitor in any of the trackers I read.
**(inference)** The audience overlaps heavily with the `/metrics` audience, so
if #5 lands, this follows.

**Cost, and a genuine conflict with a stated principle.** `keyring` has no
backend in a container. So Docker requires reading the cookie from an
environment variable or a mounted secret — which contradicts *"The session
cookie lives in the OS keychain, never in a config file."* That is not fatal
(Docker secrets and `*_FILE` conventions exist and are respectable), but it is a
real fork in the credential story, and doing it badly is how a cookie ends up in
`docker inspect` output and in someone's shell history.

**Verdict: post-1.0.** Ship `pipx` / `uvx` for v0.1.0. When you do it, add a
`SecretStore` implementation that reads a file path from
`QUOTALENS_COOKIE_FILE`, never a bare env var, and say plainly in the README
that the container path trades keychain storage for filesystem permissions.

---

### Named and declined

- **Desktop notifications** — see #4. Three code paths, dead in the deployments
  you recommend, and ClaudeUsageBar already does it well on the platform where
  it matters most. *No for v0.1.0.*
- **`--host` / network binding** — an unauthenticated dashboard of account data.
  *No.* `PLAN.md` is right.
- **Menu bar companion** — explicit vision non-goal, and the niche has ten tools.
  *No.*
- **API-key cost tracking** — explicit vision non-goal, and correctly so; it is
  a solved problem with a documented API. *No.*
- **Multi-provider (OpenAI/Codex/Copilot)** — **this bends the vision and I am
  naming it because the market is moving there.** Claude-Usage-Tracker v3.3.0
  shipped a provider registry with Codex; ClaudeUsageBar has unmerged PRs for
  six providers; `ai-usagebar` (328★) and `aistat` are both multi-provider by
  design. Nothing in the non-goals forbids it. But it multiplies the
  undocumented-endpoint drift surface — your single largest maintenance risk —
  by the number of vendors, and it dilutes a name that means "Claude quota."
  *No for v0.1.0; a real strategic question for 1.0.*
- **Grafana dashboard JSON** — near-free once `/metrics` exists, and `PLAN.md`
  already has it post-1.0. Agreed: ship it when someone asks, as evidence that
  `/metrics` is being used.

---

## 6. The cut list

Same rigour: what it cost, what breaks, and whether removal is cheaper now.

### Cut outright: `legacy.py` (quotawatch migration)

90 lines plus tests plus a call in `main()` plus a stale-env-var warning plus a
README paragraph, migrating a database and keyring entry from a name that has
**never been publicly released**. Zero users can have a `quotawatch` install.

**What breaks:** your own machine, once, and you know where the file is.
**Cheaper now:** yes, trivially — after release it becomes indistinguishable
from real migration code and nobody will dare delete it. Delete the module, the
call, the tests, the `QUOTAWATCH_` warning, and the README paragraph.

### Cut to conditional: the `overage_spend_limit` fetch

`parse_spend` prefers the `spend` block inside the *usage* payload; the dedicated
endpoint only supplies `disabled_until` or acts as a last resort when the usage
payload has neither block. So the second request per poll is usually redundant —
and it **doubles your request rate against endpoints that are documented to rate
limit aggressively** ([#31637](https://github.com/anthropics/claude-code/issues/31637)).

**Cost to build:** already spent, and the parser's merge/conflict logic is good
work worth keeping. **What breaks:** `disabled_until` becomes stale between
startups. **Fix:** fetch it once at startup, then only when the usage payload
lacks both `spend` and `extra_usage`, or every Nth poll. ~10 lines, halves the
traffic. **Cheaper now:** yes — after release, changing poll behaviour is a
behaviour change people notice.

### Cut: the Windows Task Scheduler XML

`service.py` is 529 lines carrying three OS-specific install paths. The Windows
one already isn't a real install — the README calls it "a Task Scheduler XML and
instructions rather than a pretend install," which is honest and also an
admission that it is documentation shaped like code, generated by a code path
you cannot test on CI you don't have.

**What breaks:** nothing a three-line README section doesn't cover.
**Cheaper now:** yes.

### Gate, don't cut: `service install` for macOS and Linux

I nearly put this in the cut list. The argument for cutting: it is the largest
single investment outside the UI, it is three code paths, and it creates the
worst class of bug report — *silently produces no data*. The README already
carries an apology for it: *"A background agent reading the OS keychain may
prompt on first run or be refused."* A launchd agent that cannot read the
Keychain is the most likely way a new user's install produces an empty database
and a shrug.

The argument for keeping: history is the product, history requires uptime, and
"remember to run it" doesn't produce a week of data.

**Verdict: keep, conditional on a gate.** Before v0.1.0, install the LaunchAgent
on a clean macOS account and the systemd unit on a clean Linux user, from a
`pipx` install, and confirm both poll successfully for an hour without a
keychain prompt. If either doesn't, cut `service install` to *printing* the unit
file and the command and let the user run it. `start`/`stop`/`status`/`logs` on
the pid file is one code path and stays either way.

### Don't cut, but stop the policy: the no-JS dual path

Every control works both as a plain link/form and as JS. It is admirable and it
is already built, so cutting it costs more than keeping it. But it is a
permanent 2× tax on every control you add for the rest of the project's life,
and it is why one commit added 490 lines to make a chart interrogable.

**Recommendation:** keep what exists, write the rule down as "the *dashboard*
degrades without JS; individual controls may not," and stop paying it for new
work. Note this contradicts nothing in `DESIGN.md`, which is silent on it.

### Explicitly keep: the chart interrogation

Drag-zoom, crosshair, series toggles, custom ranges, history sort and
pagination. The case for cutting is that nobody has installed the tool yet and
these serve a user who already has weeks of data. The case for keeping is
stronger: it is built, tested, and it is the whole difference between this and
the ten tools that render a percentage. **Keep.**

---

## 7. Your six questions, answered

**Is M3 the differentiator?** No, and it was already less of one than you
thought before Anthropic shipped `/usage` attribution. ccusage does per-project
well at 18.3k stars; `/usage` does *better than per-project* officially and for
free; the demand is team cost-allocation shaped, not subscriber shaped; and it
can only produce correlation, not attribution, because the quota pool spans
surfaces. **Link to them.** Your differentiator is history and runway, both of
which are already built. (§2.1, §5.7)

**Multi-account: real or padding?** Real. Four independent trackers, one
unanswered request with a concrete use case, one unmerged community PR, and the
most mature competitor ships it as a headline. And here it is nearly free
because you built a server, not a menu bar. Ship `--profile`. (§5.2)

**Prometheus or is CSV enough?** Both, they are ~70 lines together with no
dependencies, and arguing about it costs more than building it. `/metrics` is a
genuinely empty space across the entire ecosystem, and a Grafana panel is a
reason to leave the tool running — which is how it accrues its own value. But
ship it loopback-only and do **not** ship `--host`. (§5.3, §5.5)

**Notifications: desktop, webhook, both, neither?** Webhook. Detection and the
`event` row are the substrate; delivery is pluggable. Desktop notifications are
three code paths that don't work under systemd-without-a-session-bus or in a
container — the two ways you tell people to run this. And the person who wants a
macOS notification already has ClaudeUsageBar. (§5.4)

**Risk profile of the undocumented endpoints?** Two separate risks, and the
project currently conflates them.
*Terms:* Consumer Terms §3.7 prohibits automated non-API access. The clause is
real, verbatim in §2.2, and applies. No enforcement against read-only usage
monitors surfaced anywhere. The one enforcement action in this space targeted
tools that *spend* quota, not tools that read it.
*Operational:* high and well-evidenced. ClaudeUsageBar
[#39](https://github.com/Artzainnn/ClaudeUsageBar/issues/39) (403s that a fresh
cookie no longer fixes); Claude-Usage-Tracker shipping fixes for Cloudflare
challenges, E1000 and E3000 errors across three consecutive releases; three
`/api/oauth/usage` rate-limit issues all closed **not planned**; Anthropic's own
`/usage` carrying a last-known-value fallback because the endpoint routinely
429s. Your backoff, `BlockedError`, `unverified` state and raw-sample retention
are the right mitigations and are better than anything else in this space. The
one gap: **you should expect to break, and `probe` should make a good bug report
trivial to send.** (§2.2)

**Windows?** Grounded, with one honest caveat. Windows is 49.5% of professional
developers ([2025 Stack Overflow
survey](https://survey.stackoverflow.co/2025/technology)) against macOS 32.9%.
Claude Code went native on Windows in v1.0.51, July 2025. Every macOS-only
tracker in the matrix is architecturally locked out, and someone cared enough to
maintain [a Windows port of
Claude-Usage-Tracker](https://github.com/xMazaki/Claude-Usage-Tracker-Windows),
which is stronger evidence than a feature request. **The caveat:** I could not
find a direct "please add Windows" issue on any macOS-only tracker, so the claim
rests on platform share plus the existence of ports, not on stated demand. And
your own risk is different: cross-platform is a *headline* feature with **no CI
and, as far as I can tell, no evidence it has been run on Windows or Linux.** A
claim with no test is a claim. See the release gates in `MVP-SCOPE.md`.

---

## 8. Where I found nothing

Stated plainly, because the absences are findings.

- **No Prometheus or metrics endpoint in any tool in this space.** Gap or absent
  demand — I cannot tell which from the evidence.
- **No tool markets a points-per-hour burn rate as its lead figure.**
  Claude-Usage-Tracker's "6-tier pace system" is the nearest thing, and it is a
  colour, not a number.
- **No individual subscriber asking to compare quota across their own repos.**
  The per-project demand I found is all team/console cost allocation.
- **No user complaint distinguishing "the web app ate it" from "Claude Code ate
  it."** The pooling is documented; the confusion about it apparently is not
  voiced that way.
- **No evidence of Anthropic acting against a read-only usage monitor.**
- **No user asking for a containerised Claude usage monitor.**
- **Reddit could not be fetched** through this environment; every Reddit-sourced
  quote reaching me was secondhand via journalism or search snippets and I have
  excluded all of it from this document. X/Twitter likewise. The evidence above
  is GitHub issues, HN comments and Anthropic's own documentation, all directly
  fetched. Treat the Reddit-shaped gap in this review as unexamined, not as
  absent.
