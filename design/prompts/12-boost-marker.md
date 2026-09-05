# Draw the boost on the chart: a crimson drop and a rocket

The owner has picked. This is the specification, not a set of options. Three
elements, at the boost's timestamp:

1. **The drop segment is crimson.** The vertical fall from 98% to 7% — and the
   Fable fall from 100% to 2% — is drawn in `#E13A54` at 2.6px, not in the
   window's own series colour. The rest of each series is unchanged either side.
   This is the marker: the eye lands on the fall itself.
2. **An upright, full-colour rocket** beside it, at 18px.
   `design/marks/boost-rocket.svg` is the drawing. **Inline it; do not redraw it,
   do not tilt it, do not substitute an icon font or a Unicode character.**
3. **The label, inline with the rocket** — not stacked under it and not on the
   line. `limits boosted` on the first line in `--txt`, `01:30 · 98% → 7%` on the
   second in `--txt-far`, both starting to the right of the rocket, the rocket
   vertically centred on the first line.

There is **no separate pointer rule**. An earlier draft had a grey vertical
standing on the axis; the crimson drop marks the position on its own and the grey
line only crowded it. Do not reintroduce it.

## The prerequisite: draw the drop as a step

None of this works until the fall is a step. The chart currently renders that
transition as a twelve-hour diagonal, because no samples exist across the gap and
the renderer joins the last point before it to the first point after. It reads as
a gradual decline the owner caused; it was an instantaneous step he was given.
A crimson diagonal would be a lie in a louder colour.

Fix it in the same change: at a boost timestamp the series steps vertically, and
the crimson segment is that vertical. Elsewhere, decide deliberately what a gap
should look like — interpolating across one is the same class of invention, and
this system does not draw what it does not know. If the general gap treatment is
more than this change should carry, do the boost step now and open the general
case as its own issue, described.

## Sizes, and the honest limit

Four fills inside sixteen pixels is roughly four pixels each. This mark does not
degrade gracefully.

| Where | Size |
|---|---|
| chart, beside the crimson drop | **18px** |
| events list, History row | 20–24px |
| anywhere below 16px | **do not use it** |

If a surface cannot give it 16px it gets the text alone. Do not scale the rocket
down and hope, and do not add a simplified variant in this change without saying
so.

## The colour rule this creates, and where to write it down

The crimson is off-palette on purpose. DESIGN.md §1 gives every hue in the
product a job, and this one is not among them — it sits next to `--st-critical`,
which means something is wrong. The boost mark is the one place on the page where
red does not mean trouble, and that is a deliberate exception the owner has
chosen. Record it rather than letting it leak:

- `#E13A54` is the **boost colour**, and it appears in exactly two places: the
  rocket, and the drop segment of a boosted window. Nothing else. Whether it
  becomes a token or stays a literal, the constraint is the same and it belongs
  in the comment as well as the doc.
- Add a paragraph to **DESIGN.md §1**, and to **§8** — the icon section notes one
  slot deliberately unspent, and this fills it. State that the boost mark is an
  illustration and an event, not a state; that §5's state vocabulary is unchanged
  and still closed; and that no other element may borrow these colours.
- The mark must never sit adjacent to a `critical` chip in the same row, where
  two different reds would do two different jobs an inch apart. If the layout puts
  it there, move the mark, not the chip.

## One marker per moment

The owner's weekly and Fable windows boosted in the same poll, so **both** drops
are crimson and there is **one** rocket and **one** label for the moment — not one
per window. A previous change already found two glyphs stacking at the same x.
When both boosted, the label says so in one line rather than repeating itself.

## This will not appear until two other things are true

Both were found while diagnosing why the last change showed nothing:

- `quotalens rescan` must have been run against the database. Detection happens
  at ingest only, so a boost predating the detector has no `quota_boost` row, and
  the chart reads `boost_ts` from that table. Missing row, empty chart, correctly.
- `EVENT_ROWS = 6`. A boost from 01:30 sits far below the cut in a normal day's
  events. The chart marker does not fix the sidebar — say plainly whether you
  changed that too or left it.

## Verification

- Render against a copy of the owner's real database and **screenshot the chart**.
  Not a fixture. The last two rounds both passed their tests while the dashboard
  showed nothing.
- Confirm the fall renders as a vertical step in `#E13A54`, that both weekly
  series show it, and that the colour stops at the step — the flat runs either
  side keep `--s2` and `--s3`.
- Confirm the rocket is upright, unclipped, inline with the first label line, and
  identical to `design/marks/boost-rocket.svg` — diff the emitted path data
  against the file.
- Confirm nothing overlaps: the label against the `now` rule, the climb marker,
  the series end labels, and the session trace when the session is low. Check at
  the default range and at 24h.
- A test that two windows boosting in one poll produce one rocket and one label.
- A test that the mark is omitted, not scaled, below 16px.
- `ruff check` and `pytest` clean. Note any `/api` shape change.
