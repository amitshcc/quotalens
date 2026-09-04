# Make the weekly budget answerable, retire the empty attribution table, tighten the layout

Three changes on the dashboard. The first is the important one, and it is almost
entirely a naming problem — **do not change the maths in `budget.py`.** It
already computes the right things under the wrong labels.

---

## 1. The weekly budget panel says nothing a person can act on

The question this panel exists to answer, in the owner's words:

> At 95% weekly used, how many full 5-hour sessions can I still run? If I only
> use half a session each time, how many then? I want to plan ahead.

`compute_budget()` already answers exactly that. `full_windows` **is** "sessions
left if you run each one to 100%". `typical_windows` **is** the same at the
owner's own median session peak. The panel is unreadable because of what it calls
them and what it hides.

### What is wrong, precisely

- **"Windows of budget"** is jargon. Nobody reads that as "sessions you can still
  run". Call the unit a *session*, not a *window*, everywhere in this panel.
- **"Windows of clock"** is worse: it reads as a second budget, and it is not. It
  is how many five-hour slots of wall clock remain before the limit resets — the
  other constraint. 13.4 next to an em dash invites exactly the wrong conclusion.
- **The em dash is silent.** `Weekly — all models` shows "—" because
  `MIN_COMPARE_WINDOWS` is 5 and there are fewer usable complete windows than
  that: partial windows are excluded by `MIN_COVERAGE_PCT`, and windows moving
  the limit less than `MIN_SESSION_DELTA_PCT` are excluded as noise.
  `Budget.reason` **already holds that sentence** and the panel throws it away.
  That single omission is most of the confusion: the owner cannot tell "we don't
  know yet" from "the answer is nothing".

### What to build

Keep a table. Replace the columns with these, left to right:

| Limit | Left | **Full sessions left** | At your typical session | Each full session costs |
|---|---|---|---|---|

- **Left** — `headroom_pct`, as a percentage.
- **Full sessions left** — `full_windows`. This is the hero column: set it in the
  same weight as a meter value, not as body text. It is the answer.
- **At your typical session** — `typical_windows`, with `typical_peak` named in
  the column header or a footnote so "typical" is not a mystery: *"at your
  typical session (61% used)"*. This is the owner's "50% column", already
  computed; do not add a hardcoded 50%.
- **Each full session costs** — `cost_per_full`, in points of that limit, with
  `cost_low`–`cost_high` beside it as the observed spread, and `usable_windows`
  as the sample it rests on. Something like `11 pts (9–14, from 6 windows)`.

**When a number is unknown, print `reason` in the cell instead of an em dash.**
Not a tooltip — the visible cell. "Needs 5 complete session windows to estimate
the cost of one; 3 so far." is a useful thing to read; "—" is not. If the row is
too narrow for the sentence, put it on a continuation line under the row.

**Move the clock out of the table** and into the note beneath it, phrased as time
and stated against the budget, because which one binds is the actual finding:

> There is time for 13.4 more sessions before this resets Mon 06:30, and budget
> for 0.4 — the budget is what runs out.

Compute which one binds by comparing `full_windows` to `clock_windows` and word
it accordingly; when the clock is the smaller of the two, say that instead. Keep
the existing `constraint` note (the Fable sub-cap sentence) — it is doing real
work that neither meter can do alone.

Finally, give the panel a caption that states the question rather than the
method. The current one describes the derivation. Something closer to *"What
your remaining weekly headroom will buy, in 5-hour sessions"*.

### Constraints

- `budget.py` is a pure module with a deliberate refusal threshold. Do not lower
  `MIN_COMPARE_WINDOWS`, `MIN_COVERAGE_PCT` or `MIN_SESSION_DELTA_PCT` to make a
  number appear. The fix for "no number" is to show the reason, not to
  manufacture confidence from three observations.
- Do not change `/api/budget`'s JSON keys. This is presentation only.
- Keep the epistemic rule: withheld or stale readings show the reason, never a
  guess.

---

## 2. Attribution — replace the empty table with the pointers

`docs/MVP-SCOPE.md` records M3 (per-project attribution) as **out, indefinitely**,
with the reasoning that `/usage` now attributes to skills, subagents, plugins,
MCP servers and scheduled tasks; that `ccusage` owns per-project; and that pooled
quota means local logs can only show correlation. Its own recommendation is
"link to both instead". The current panel instead renders an empty table
promising a milestone that is not coming, which is the worst of both.

Replace `_attribution()` in `render.py` with a short block in the same slot that
says where attribution actually lives:

- **`claude /usage`** — attributes recent usage to skills, subagents, plugins,
  MCP servers and scheduled tasks. Last 24 hours or 7 days, computed from local
  session history on this machine, so it excludes usage from other devices and
  from claude.ai, and it is gone when the terminal closes.
- **`ccusage`** — per-project token attribution from Claude Code's local
  transcript logs.
- **One line on why QuotaLens does not duplicate this**: quota is pooled across
  every surface, so local logs can show correlation with the climb but cannot
  attribute pooled quota to a project.

Keep it to a few lines of prose and a rule, not a table with no rows. Remove the
now-dead column headers and any CSS only that table used. Update `PLAN.md` and
the README if either still implies the scanner is coming, so the code and the
scope document stop disagreeing.

---

## 3. Layout — the sidebar starts too low and sprawls

Today `.cols` is `grid-template-columns: 1fr 288px` and it wraps only the
attribution table and `.side`. The chart sits full-width *above* that grid, so
the sidebar cannot begin until the chart has ended. The result is a tall,
half-empty right column and a lot of dead space beside Diagnostics and Recent
events.

Extend the two-column grid upward so it starts at the chart: the chart goes in
the left column, `.side` in the right, and the sidebar begins level with the top
of the chart. The chart gets narrower, which is fine and is the point.

- The hero, the window meters and the weekly budget panel stay **full width**.
  Only the chart and what follows it are inside the two columns.
- The chart is inline SVG with a fixed `viewBox`; check that the narrower column
  does not crowd the right-hand series labels or the axis ticks. Adjust the
  `viewBox` or the right padding rather than letting labels collide.
- Give `.side` a deliberate order, top to bottom: the counters that describe the
  current view (Range, Not collected, No session, Poll interval), then the store
  (Samples, Database, Oldest sample, Last/Next poll), then Extra usage, then
  Diagnostics, then Recent events last. Recent events is the only unbounded
  section and belongs at the bottom.
- Cap Recent events at a sensible number of entries with the rest behind
  `/api/events`, so the sidebar cannot grow without limit.
- Keep the existing responsive collapse: below the current breakpoint the two
  columns become one, chart first.

---

## While you are in here (low priority, only if it is clean)

The hero reads **"Session 67% left"** while the meter immediately below reads
**"Session 33%"**. Both are correct and they are the same window stated two ways,
which is a trap of the same kind as the one above. Consider labelling the meter
"used" explicitly so the pair reads as one fact rather than two numbers that look
like a contradiction. If that turns out to touch more than the label, leave it
and say so.

---

## Verification

- Unit tests for the panel's wording: a limit with no usable windows renders its
  `reason` text and no em dash; a limit with headroom 0 renders "none left"; a
  known limit renders the session counts and the cost with its spread and sample
  size.
- A test that the binding-constraint sentence names the clock when
  `clock_windows < full_windows`, and the budget when it is the other way round.
- Render the page against a copy of the real database at a desktop width and
  confirm by eye: the sidebar top is level with the chart top, no series label is
  clipped, and Recent events is last.
- `ruff check` and `pytest` clean.
- Confirm `/api/budget` output is byte-identical before and after, since nothing
  in the derivation should have changed.
