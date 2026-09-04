# Claude Code prompt — the weekly limit, in units I can act on

Paste below the line into Claude Code in the `quotalens` repo.

---

Two changes to the weekly meters. One is a label that currently confuses me,
and one is the number I actually open the dashboard to find and cannot get.

## 1. "6 resets in range" — say what it means, and check whether it is true

`dashboard._change_over_range` wants to report the change of a window over the
selected range. When `split_at_resets` finds a boundary inside the range it
cannot state a single delta, so it falls back to counting boundaries and prints
"6 resets in range". That is defensible for the Session meter — 17 five-hour
boundaries across all of my history is a fact. It is close to meaningless on a
weekly meter, for two reasons.

**It reads as a statistic when it is an apology.** The intent is "I can't give
you a number because the series was cut." Say that instead. Something like
"+11 pts since the last reset" — the change over the most recent segment, which
is the number a person actually wants — with the reset count as secondary text
if it earns its place at all. Decide the wording yourself, but the meter must
answer "how much has this moved" rather than explaining why it won't.

**And I don't believe the number.** My history starts 2 Sep 18:27, which is
about two days. A weekly window cannot reset six times in two days. So those
six are either false positives from `is_reset` on the weekly series, or they
are real server-side events that are not resets. Find out which. Places to
look: whether the weekly rows always carry a `resets_at` (the diagnostics panel
says `nimbus_quill` arrives without a reset time, so the drop rule may be
running on windows I assume are protected); whether `resets_at_changed` is
firing on jitter larger than `RESET_TOLERANCE_S`; and the two history rows
tagged `(reset)` on 3 Sep 13:00–18:00, where both weekly columns claim a reset
mid-afternoon while the meter says the weekly limit resets Monday 06:29. If
these are false positives, that is a bug of the same family as the one we fixed
in `is_reset`, and it is corrupting the weekly deltas in the history table.
Report what you find before changing behaviour.

## 2. Weekly headroom, expressed in session windows

This is the feature. Weekly says 93%. What I want to know, and currently have
to work out on paper, is: **how many more five-hour windows can I burn before
the weekly limit stops me?**

I have the data to answer that, and it is already on the page. Every completed
session window in the history table carries both its own consumption and what
it cost the weekly limits — `3 Sep 20:20–01:20: Session 100%, Weekly all +11`.
So a session window run to 100% costs about 11 weekly points, and 7 points of
weekly headroom is a bit over half a window. That is the number. Derive it from
my own history rather than from an assumed constant, because the cost per
window depends on which models I used.

**The metric.** For each weekly window (all-models and Fable separately),
compute the weekly points consumed per point of session consumption across
complete session windows, take the median, and report:

- **Full windows remaining** — weekly headroom divided by the cost of a session
  window run to 100%.
- **Typical windows remaining** — the same against my median window
  consumption, which `runway.median_peak` already computes. If my typical
  window is 61%, "0.6 full windows" and "1.0 typical windows" are both true and
  the second is the one that matches how I actually work.
- **Time versus budget.** The weekly limit resets at a known time. Say how many
  five-hour windows of wall clock remain before that reset, next to how many
  windows of budget remain. "0.6 windows of budget, 12 windows of clock" is the
  whole story in one line: I am rationing, not racing.

**Which windows count.** Exclude any session window that would poison the
ratio, and be strict about it:

- partial coverage — the rows badged "partial, 3% observed" are not measurements
- any window where the weekly limit reset mid-window, where the delta is not a
  consumption at all (`Delta.reset` already marks these)
- windows with a session delta small enough that the ratio is mostly noise

**Say nothing rather than something wrong.** Follow the precedent already in
`runway.py`: below `MIN_COMPARE_WINDOWS` usable windows, print an em dash and a
plain sentence saying it needs more history, exactly as the six-state system
does elsewhere. A confident "3.2 windows left" computed from two observations
is worse than no number, because I will plan my week around it.

**Show the spread, not just the median.** Cost per window varies with the model
mix — in my own history the same 100% window has cost between 8 and 15 weekly
points. A single number hides that. Give me the median and the range, in
whatever form fits the meter without turning it into a paragraph.

**Fable.** My Fable meter reads 100%, and per `FEATURE-REVIEW.md` §2.6 that
means the Fable half of the weekly pool is spent, not that my account is
exhausted. So it needs its own version of the same answer, and the honest one
is "zero Fable windows left until Monday". More importantly, the two limits
interact: the 7% of all-models headroom I have left **cannot be spent on
Fable**, so if I plan around the all-models number alone I will plan wrong. The
dashboard should make that constraint visible rather than leaving me to infer
it from two meters that each look fine on their own.

## How to build it

- The derivation is pure and belongs next to `runway.py` — same shape as what
  is there, functions over rows returning a result object, no I/O.
- Expose it on the API (`/api/runway` or a sibling; your call, but say which and
  why) before it appears on the page, so it is testable without scraping HTML.
- On the page it belongs with the weekly meters, not in the hero. The hero is
  the session window and it stays that way.
- Add it to `/metrics` too — this is exactly the kind of number people alert on.
- Tests: a synthetic history with a known cost ratio; a history with partial
  and reset-contaminated windows that must be excluded and prove the answer
  changes when they are; a history below the floor that must produce the em
  dash; and a Fable series at 100%.

Ground rules unchanged: no new dependencies, no chart library, the design
tokens as they are, `ruff` clean, small commits. And as before, if I am not at
the keyboard when you hit a decision, take the reversible option, write it down,
and tell me at the end.

Start with the investigation in part 1 and tell me what those six resets are
before you build part 2 on top of the same delta data.
