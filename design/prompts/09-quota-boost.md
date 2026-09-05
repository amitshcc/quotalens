# Detect and show a quota boost

## What happened

The owner's weekly limit was raised mid-window: the used percentage fell without
the window resetting. QuotaLens showed nothing. It is exactly the kind of event
this project exists to remember — the level moved for a reason that is not
consumption, and by tomorrow there will be no trace of it.

## Probe before designing

Boosts are rare and you cannot reproduce one on demand, so work from evidence,
not from a guess about the payload shape.

- Look in the existing database for the transition. The samples are still there:
  find the point where a weekly window's utilization fell while `resets_at` stayed
  put, and print the rows either side.
- Run `quotalens probe` and record which fields a limit block actually carries —
  in particular whether it exposes a ceiling or allowance alongside the
  percentage, or only the percentage.

Report both before writing the detection rule. The rule differs depending on
whether a boost shows as *the number falling* or as *the denominator changing*,
and only the payload can say.

## What a boost is, and what it must not be confused with

A boost is: **utilization falls, while the window has not reset.**

Everything below can also make a number fall, and none of them is a boost:

- **A genuine reset** — `resets_at` moved forward. Already handled; leave it.
- **A stale or withheld reading** — the collector is not reporting. There is no
  new value, so there is no fall.
- **A block that stopped being dated** — the `no resets_at` path. That is the
  freshness bug, not a boost.
- **A parse fallback** — `parse.py` recovering a number by the generic path,
  which the design already treats as unverified. An unverified reading must never
  become a boost event; it is not trusted enough to make a claim from.
- **Sampling noise** — a fall smaller than the rounding the API reports at.
  Require a minimum magnitude, and say in the code what it is and why.

Detect it in the parse/store layer where readings arrive, once, so every consumer
sees the same conclusion. Do not re-derive it per view.

## The part that matters more than the display

`sessions.py` groups windows by `resets_at`, so a boost will **not** split a
session window — that much is safe. But `_delta()` takes the difference across a
window, and a boost inside that window makes the weekly delta smaller than what
was actually consumed. That delta is the input to `budget.window_costs()`, which
is the input to the median cost of a full session, which is the number the owner
spent three rounds getting to read correctly.

So a boost silently makes the budget optimistic, and nothing on the page would
say so. Fix that in the same change:

- Exclude a boosted window from `window_costs`, the way partial and low-delta
  windows are already excluded, and give it its own reason string.
- Or, if you can attribute the boost's magnitude exactly, subtract it and keep the
  window. Only do this if the payload gives you the size of the boost directly —
  do not infer it from the fall, because a fall is consumption plus boost and you
  cannot separate them after the fact.

Prefer excluding. A window you cannot cost is honest; a window you costed wrong
is not, and this system's whole posture is that a refused answer beats a
confident wrong one.

## What to show

Smallest thing that is genuinely useful, in this order:

1. **An event.** `14:12 — Weekly, all models fell 96% → 70% with no reset. Limit
   raised.` This is the record, it is what "QuotaLens remembers" means, and it
   costs almost nothing because the events machinery already exists. Give it its
   own kind so `/api/events` consumers can filter it.
2. **A marker on the chart** at that timestamp, drawn like the existing climb
   marker — a rule and a short label. The shape of the series already shows the
   drop; the marker says why it dropped.
3. **A note on the affected meter** while the boosted window is current, using the
   `note` / `note_title` mechanism `WindowView` already has. Two or three words —
   "boosted 14:12" — not a chip. This is not a state; the window is not in
   trouble, something good happened to it.
4. **In History**, mark the window the boost fell in, so the row's weekly delta is
   not read as ordinary consumption. If you excluded it from the cost estimate,
   this is where the reader finds out why.

Do not add a fifth thing. No banner, no celebration, no colour of its own — a
boost is information, not a state, and the state vocabulary in §5 is full.

## Alerts

Check `alerts.py`. A boost will look like the burn rate collapsing and the
headroom jumping, which can fire a "fell back below threshold" recovery event
that is not a recovery. Suppress or relabel it: the threshold was not crossed by
anything the user did.

## Verification

- Unit tests over synthetic rows for each case: a real boost, a reset, a stale
  gap, an undated block, an unverified reading, and a fall smaller than the noise
  floor. Only the first produces a boost.
- A test that a window containing a boost is excluded from `window_costs` with
  its reason, and that the median cost is unchanged by the boost's presence.
- A test that no alert fires on the boost transition.
- Replay the real transition from the owner's database and show the event, the
  marker and the History annotation in your summary.
- Say in your summary what a boost looks like in the payload, since that is the
  finding this change rests on and nobody has written it down yet.
- `ruff check` and `pytest` clean. Note any `/api` shape changes explicitly.
