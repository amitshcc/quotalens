# Bug: a lapsed session window keeps showing its last percentage

## What is on screen

At 14:22, with no session running:

```
hero    Session  60 % left      resets in: no window
        "No session running. The next message starts a fresh session window."
        0.00 pts/hr over the last 36m

meter   Session  40 % used      resets 14:00      +35 pts since the last reset
```

The window ran 08:59–13:59 and ended 22 minutes ago. The page says so in the
verdict and then contradicts itself three times: a headroom figure that reads as
a live budget, a "resets" time in the past presented as if it were in the future,
and a delta "since the last reset" for a window that has closed. The header ring
and the favicon show the same phantom 40%, because they read the same view.

## The cause

`compute_runway()` in `runway.py`:

```python
if reset_ts is None or remaining <= 0:
    verdict = "No session running. The next message starts a fresh session window."
    return Runway(reset_ts, 0, pct, headroom, rate, None, None, None, verdict, "")
```

The branch correctly detects that the window has lapsed, sets `remaining` to 0,
and refuses to project — and then passes `pct` and `headroom` straight through
from the last stored reading. So every consumer downstream renders a number that
describes a window which no longer exists.

## Before changing anything: settle what the payload says

Run `quotalens probe` in this state and read the raw output. The answer decides
the fix, and I do not want it guessed:

- **If the `five_hour` block is absent, or present with utilization 0** — the
  window has genuinely reset upstream and the stored 40% is simply stale. The
  live reading is 0% used / 100% left.
- **If the block is present, still reporting ~40%, and carries no `resets_at`** —
  which is what Diagnostics and the 14:00 event suggest is happening — then the
  server is reporting a figure it will not date. That is not a current reading
  and must not be shown as one.

Report which of the two it is in your summary, with the relevant fragment of the
payload, before you show me the diff.

## The fix

**The invariant: never present a session percentage as current when its window's
reset time has passed.** Fix it once, in `compute_runway`, so every consumer
inherits it rather than each one learning the rule separately.

When `remaining <= 0`:

- If probe shows the window has reset upstream, the session reading is
  **0% used / 100% left**, and the verdict stays as it is — the next message
  starts a fresh window with the whole allowance. This is the answer I expect and
  the one the owner expects.
- If probe shows a dated-less leftover figure, the reading is **unknown**: em
  dash, the existing stale treatment, and a line saying the window ended at 13:59
  and the server has not opened a new one. Do not display the leftover number
  under any label.

Then follow it through everywhere the old value leaked:

- **The meter footer** must not print `resets 14:00` for a time in the past. When
  the window has lapsed it says so — "ended 13:59", or "no window open" — and the
  `when()` helper should refuse to format a past timestamp as a pending reset
  anywhere it is used, not just here.
- **"+35 pts since the last reset"** refers to a closed window. Either scope it
  to the window it describes ("+35 pts in the window that ended 13:59") or drop
  it while no window is open.
- **The hero and the meter must agree.** "resets in: no window" and
  "resets 14:00" are twelve pixels apart and say opposite things. After the fix,
  assert in a test that they cannot disagree.
- **The chart** should show the window ending. `Dashboard.idle` already exists
  for exactly this ("spans with no window running as flat shading") — check it is
  populated for 14:00→now and rendered, and that the session trace does not run
  flat to the right edge as though it were still live.
- **The ring and the favicon** read `mark_reading()`, which reads the same
  `WindowView`. Confirm they follow the fix rather than needing their own branch.
  On the unknown path they take the dashed empty ring, which is already correct
  behaviour for "no trusted reading".

## The deeper bug underneath it, which is probably the real one

`parse.py` drops any block with no `resets_at` — `IgnoredBlock(key, "no resets_at")`
— and that drop currently feeds **Diagnostics only**. Nothing marks the affected
window's reading as no longer updating. So when the server stops dating the
`five_hour` block, the last stored row simply stays put and the meter keeps
rendering it, full confidence, indefinitely. The Recent events list shows this
happening from 12:08 onward.

That is the same failure the stale treatment exists to prevent, and the existing
mechanism does not catch it because staleness is tracked per *collector*
(`last_success_ts`) and not per *window*. The collector is healthy; one block
inside a healthy payload went dark.

Add per-window freshness: record when each window's reading was last actually
present in a payload, and withhold that window's value once its own reading has
gone unrefreshed for longer than a small multiple of the poll interval, using the
stale treatment that already exists. A healthy collector is not evidence that
every meter on the page is current.

If that turns out to be a larger change than it looks, do the `compute_runway`
fix and the display consequences first, and open the per-window freshness work as
a separate, clearly-described issue rather than half-doing it.

## Constraints

- The epistemic rules hold: unknown values are removed and explained, never
  guessed, never frozen at the last good reading.
- `/api/quota/current` and the other endpoints must express the lapsed state
  honestly too. If a consumer reads the JSON it should not be able to reach the
  wrong conclusion the page just stopped reaching. State the key changes in your
  summary; this one is allowed to change shape, unlike the budget work.
- No new dependencies.

## Verification

- A unit test that a lapsed window (`reset_ts` in the past) yields no live
  percentage from `compute_runway` — whichever of the two answers probe selects.
- A test that the hero's window text and the meter's footer are consistent for:
  live window, lapsed window, no reading yet, stale collector.
- A test that a window whose block stops appearing in the payload is withheld
  after the freshness threshold, rather than rendering its last value.
- Reproduce the reported state end to end: seed a database whose newest
  `five_hour` row has a `resets_at` in the past, render, and confirm no 40% and no
  "resets 14:00" appears anywhere in the HTML — including the ring, the favicon
  and the chart.
- `ruff check` and `pytest` clean.
