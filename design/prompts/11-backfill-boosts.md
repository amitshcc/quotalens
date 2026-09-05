# The boost is detected but never recorded for history already in the database

## Why nothing appears on the dashboard

The wiring is correct and there is no data behind it.

- `detect_boosts()` is called in exactly one place: `poller.py:384`, on ingest,
  comparing the previous stored reading to the incoming one. It writes a
  `quota_boost` event.
- Every display reads boosts back out of the events table —
  `dashboard.py:416`, `store.recent_events(kind=BOOST_KIND)` — and feeds
  `boost_ts` to the chart marker, the meter note, the History badge and
  `budget.window_costs()`.
- The owner's boost happened **before that code shipped**. No event row exists
  for it, so `boost_ts` is empty and all four surfaces render nothing.

The replay in the last change ran the detector over historical rows in a test
harness. That demonstrated the detector; it did not write anything to the live
store. Nothing in `cli.py` rescans history.

## What to build

### 1. Backfill

A one-time scan of the stored samples that runs the **same** `detect_boosts`
over consecutive readings per window and records the events it finds.

- **Idempotent.** Running it twice must not produce two events for one boost.
  Key on (window, timestamp) or check for an existing event at that instant
  before writing. Say in your summary how you guaranteed this.
- **One definition of a boost.** Reuse `detect_boosts` exactly; do not write a
  second, subtly different scanner for historical rows. If the shape of the
  stored data forces an adapter, keep the adapter thin and the rule shared.
- **Where it runs** — pick one and justify it in a sentence:
  a `quotalens rescan` subcommand the owner runs once, or an automatic pass on
  startup guarded so it happens once per database. A subcommand is the safer
  default: it is explicit, and this is a one-off correction rather than
  ongoing behaviour.
- **Trust.** Historical readings recovered by the parse fallback are unverified
  and must not become boosts, the same rule the live path already applies.
- Report how many events it wrote, per window, with timestamps.

### 2. A boost must not fall off the events list

Even once recorded, the sidebar shows the most recent `EVENT_ROWS` events, and
the owner's list is dominated by repeats — `usage payload has blocks without
resets_at, not charted: nimbus_quill` appeared four times in one screenshot,
alongside timeouts and burn-rate crossings. A boost happens rarely and would be
pushed off within the hour.

- **Collapse consecutive identical events** into one row carrying a count and
  the time range: `13:10 usage payload has blocks without resets_at ×4`. This is
  worth doing on its own merits — four identical lines is not four facts — and it
  makes room for the events that matter.
- Confirm after that whether a boost still survives in a normal hour. If it does
  not, raise `EVENT_ROWS` or keep the newest structural event pinned. Do not add
  a colour or a chip for boosts; the existing list treatment is enough.

### 3. The chart marker

The owner asked for this by name. `boost_x` already exists and will populate once
the backfill runs. Check, with the real data in place:

- The marker renders inside the default range and is legible — a rule plus a
  short label, drawn like the existing climb marker.
- The label says what happened in the user's terms. **"limits boosted"** or
  "limit raised", not "boost" alone — the reader should not have to work out
  whether their usage dropped or their ceiling rose.
- It does not collide with the `now` rule, the climb marker, or the series end
  labels. The previous change already found two markers stacking at one x when
  both weekly windows boosted together; verify that fix holds against the live
  data rather than only the fixture.

## Expect the budget to change, and let it

Once the boosted window is in `boost_ts`, `window_costs()` will exclude it, and
`/api/budget` output **will** change. That is the intended behaviour from the
previous change finally taking effect on real data. Do not preserve byte-identity
here. Report the before and after — cost per full session, the spread, the sample
count, and the resulting sessions-left figure — so the owner can see what the
exclusion did.

## Verification — against the live database, not a fixture

The last round's replay passed while the dashboard showed nothing. Close that
gap:

- Run the backfill against a **copy of the owner's real database**, start the
  app, and confirm by screenshot: the event in the sidebar list, the marker on
  the chart, the note on both weekly meters, and the badge in History.
- Run the backfill a second time and confirm no duplicate events.
- Confirm a fresh database with no boosts is unaffected and the command exits
  cleanly.
- Unit tests for the collapse rule: consecutive identical events merge with a
  count; non-consecutive ones do not; a boost between two repeats is not
  swallowed.
- `ruff check` and `pytest` clean.

Then say plainly which of the four surfaces you saw with your own eyes on the
running app, and which you are inferring from tests.
