# The boost label, and the boost that vanishes on a custom range

Two faults in the same feature. Fix both in one change.

---

## Part A — the label is one long line. Shorten it; move the detail to hover.

`render.py::_boost_marks` currently draws two text lines beside the rocket:

```
limits boosted
08:09 · Weekly Fable 100% → 1%, Weekly all 98% → 0%
```

The second line is a paragraph inside a chart. When two windows move it runs
past the plot edge and collides with the traces. It is reference material, not
a label.

**What it should be.** One line only:

```
Limits Boosted
```

`Limits Boosted`, capitalised as written — this is the owner's wording, use it
verbatim. Keep it in `.bx` (`fill: var(--txt)`), keep the rocket where it is,
keep the rocket vertically centred on that one line.

**Where the detail goes.** Into a native `<title>` on a `<g>` that wraps *both*
the rocket and the heading text, so hovering either one shows it:

```html
<g class="boost"><title>08:09 · Weekly Fable 100% → 1%, Weekly all 98% → 0%</title>
  …rocket…<text class="ax bx">Limits Boosted</text></g>
```

`BoostMark.detail` already holds exactly that string — do not rebuild it, do not
change `_boost_detail`. Only its destination changes: it moves out of the second
`<text>` element and into the `<title>`. Delete the second `<text>` line.

**Make sure the tooltip actually fires.** `.trace` in `app.css` sets `fill:none`
on the group; a `<text>` with no fill and no pointer target will not receive
hover. `.bx` sets `fill:var(--txt)` so the heading is hittable, but the rocket's
own shapes must be too. Give the wrapping `<g class="boost">` an explicit
`pointer-events: all` rule in `app.css` and drop the `aria-hidden="true"` that
`_rocket()` puts on its group when it is inside this wrapper — an element the
mouse must find should not be hidden from the accessibility tree either. Put the
accessible name on the wrapper instead: `role="img"` plus
`aria-label="Limits boosted. {detail}"`.

**Check it against the chart's own hover handler.** `chart.js` binds `mousemove`
on `#chart` and shows the readout box; a native `<title>` tooltip still appears
under it. Verify by hovering — with a real pointer, in a real browser — that the
tooltip shows and the crosshair readout does not suppress it. If it does, say so
and fall back to a small `<g>`-scoped HTML tooltip rather than leaving the detail
unreachable.

---

## Part B — the boost does not render when the range is custom

### What the owner sees

Selecting `24h` (or any dropdown preset): crimson drop, rocket, label — all
present at 08:09. Drag-selecting a custom range that **contains** 08:09
(`02:09 to 18:40`, and again `05:33 to 21:12`): **no crimson drop, no rocket, no
label.** The trace is drawn, the range is right, the boost is simply gone.

### What is already ruled out — do not re-investigate these

I ran `_chart_view` directly against synthetic rows with a boost inside the
range, at four different spans including custom-shaped ones (end in the future,
end at now, 16h and 18h spans). **Every one produced `marks=1 drops=1`.**

```
24h        rows= 241 span= 24.0h marks=1 drops=1
12h        rows= 145 span= 12.0h marks=1 drops=1
custom     rows= 193 span= 18.0h marks=1 drops=1   (end 2h in the future)
cust2      rows= 193 span= 16.0h marks=1 drops=1
```

So `_chart_view` is **not** range-sensitive. The bug is upstream of it, or
downstream in rendering. Do not spend time re-reading the bucketing or
`_boost_between`; they behave identically for every range.

### How to find it — reproduce against the live database, not a copy

This feature has already burned two rounds on tests that passed while the
dashboard showed nothing. Run this against the real store at the real path,
with the app's own settings:

1. Open the dashboard, drag-select a range that contains a known boost, and copy
   the resulting `?range=<from>-<to>` **exactly** out of the address bar.
2. Fetch the fragment for that exact URL and for a preset that works:

   ```
   curl -s 'http://localhost:8787/api/dashboard/fragment?range=<from>-<to>' > /tmp/custom.html
   curl -s 'http://localhost:8787/api/dashboard/fragment?range=24h'          > /tmp/preset.html
   grep -c 'Limits Boosted\|limits boosted' /tmp/custom.html /tmp/preset.html
   grep -c '#E13A54'                        /tmp/custom.html /tmp/preset.html
   ```

   This settles in one command whether the server is failing to emit the mark or
   the client is failing to keep it.
3. If the server output differs, call `build_dashboard` in a script against the
   live DB with `parse_view({"range": "<from>-<to>"}, now)` and with
   `parse_view({"range": "24h"}, now)`, and print, for each:
   `rng.start/end/key`, `len(boost_ts)`, `boost_windows`, the per-window row
   count inside the range, `len(chart.boost_marks)`, and
   `[len(s.drops) for s in chart.series]`. Whichever of those first differs is
   the bug. **Report the actual numbers you saw.**

### The two live hypotheses, in order

1. **`parse_view` silently drops the custom range.** It requires
   `end - start >= MIN_CUSTOM_SPAN_S` and clamps `end = min(end, now)`. If the
   range fails that test, `range_key` falls back to `AUTO` and the page renders
   the *session* window instead — which is only five hours and may not contain
   08:09. The heading would still read as a range the owner didn't choose. Check
   what `rng.key` and `rng.label` actually are for his URL. (The screenshots show
   the select reading `custom: 02:09 to 18:40`, which argues against this — but
   confirm rather than assume; `render.py:506` builds that option from
   `view.range_param()`, which can be set even where resolution differs.)
2. **The client keeps a stale chart.** `submitRange`/`navigate` in `app.js`
   replaces `#app` innerHTML with the fragment, then dispatches
   `quotalens:rendered`. The drag path in `chart.js::onUp` calls
   `window.quotalens.navigate(...)` — verify the fragment that comes back on
   *that* path is the one rendered, and that nothing (the `#sel` rect, a stale
   `chart-data`) is left over from the pre-drag chart.

Fix the cause you find. Do not add a workaround that re-derives boosts in the
browser: the boost conclusion is written once by the poller and read by the
chart, the meters, the history rows and the budget — keep it that way.

### Then add the regression test that would have caught it

An end-to-end test at the level the bug lives: build the dashboard from a store
containing a boost, once with `range=24h` and once with
`range=<start>-<end>` spanning the same boost, and assert **both** render the
crimson drop and the `Limits Boosted` mark. A unit test on `_chart_view` would
have passed, as mine did — put this one above that line.

---

## Part C — audit what else the custom range suppresses

The owner asked directly: *"see if custom range is hiding other info as well."*
It is. These are real, and each needs a decision — fix, or state that it is
correct and why:

| What | Where | Behaviour on a custom range |
|---|---|---|
| The runway projection and the `exhausted HH:MM` crossing | `_mark_runway` | Returns early when `target_ts > end + 1`. Any custom range ending before the projected reset silently loses both. |
| The `now` line and label | `_mark_runway` | `chart.future = end > now`. A custom range ending at or before now has no `now` marker at all — so the chart gives no cue where the present is. |
| Hourly separators in the current window | `_mark_runway` | Only drawn where `start < t < end`; a custom range that clips the session start loses them. |
| Session start rules | `_mark_sessions` | `start < w.started_at < end` — strict, so a session starting exactly at the range edge is dropped. |
| The "Collecting: …" note | `resolve_range` | Hard-coded `collecting=False` for custom, so a custom range over thin data claims a chart it does not have. |
| Preset highlighting | `dashboard.py:599` | `rng.key == k and not rng.auto` — no preset is current, which is correct; confirm the custom option in the select is the one marked selected. |

At minimum: the `now` line should be drawn whenever `now` falls inside the range
(not only when the range extends past it), and the projection's disappearance
should be explained rather than silent — a range that ends before the reset
should say so in the diagnostics, in the house style, rather than just omitting
the line. Unknown is removed and explained; this is the same rule applied to a
value that is outside the frame rather than unmeasured.

---

## Verification — the standard for this feature

Do not report success from tests alone. With the app running against the live
database:

- Screenshot the chart on a dropdown preset containing the boost.
- Screenshot the chart on a drag-selected custom range containing the same
  boost. Both must show the crimson drop, the rocket, and `Limits Boosted`.
- Screenshot the hover tooltip showing the full detail line.
- State explicitly, for each claim you make, whether you **saw** it in the
  running app or **inferred** it from code or tests.
