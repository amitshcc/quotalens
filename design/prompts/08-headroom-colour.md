# The headroom readout is amber at every healthy value

## What is actually happening

Nothing is inverted. `magnitude_state()` thresholds correctly — higher *used*
percentage is worse, and the API's own severity wins when it gives one. The
meters are right.

The hero is a different mechanism:

```css
.readout{ ... color:var(--lit); ... }
.readout.off{color:var(--txt-far)}
.readout.is-crit{color:var(--st-critical)}
```

The headroom readout is **unconditionally amber**. There is no threshold between
`--lit` and `--st-critical`; there is no third state. So "89% left" is amber for
the same reason "9% left" is amber — the element is always amber. The colour
carries no information at all.

That is what makes it wrong, and it is worse than a plain inversion. Amber reads
as caution to everyone. A 68px amber number saying *89% left* is the healthiest
state the tool can report, dressed as a warning. The one time the colour does
change, to red, it is easy to miss because the eye has learnt the number is
always coloured.

There is a second problem sitting next to the first. `.is-crit` comes from
`Runway.critical`, which is `exhaust_ts is not None` — a **projection**: will the
burn rate exhaust the window before it resets. The meters below are coloured by
`magnitude_state`, a **level**. Two adjacent elements use the same palette to mean
two different things, so at 89% left with a steep climb the hero goes red while
the session meter stays quiet, and both are behaving as designed.

## The design-system conflict to resolve first

`DESIGN.md` disagrees with itself, and this is the root of it.

- §2 assigns the tier "Lit" to the session headroom and treats `--lit` as its
  identity: one lit element per screen, always.
- §5 says magnitude states colour the value, and that **normal has no colour at
  all**: "Absence is the signal, and this is why the system has no green: red
  against nothing cannot fail for a red-green dichromat, where red against green
  can."

The readout is the only place amber is used as identity rather than as severity,
and §5's rule is the one that makes the page readable. Resolve it in §5's favour
and update §2 and §9 to match, in the same change. Do not leave the document
saying both.

## The change

**The readout is achromatic when there is nothing to say.** Three tiers, and
colour appears only above the first:

| Condition | Colour |
|---|---|
| normal | `--txt` — no chroma, like every other normal value on the page |
| elevated | `--st-elevated` |
| critical | `--st-critical` |
| withheld / no reading | `--txt-far`, unchanged |

- **Drive it from the same source the session meter uses.** The hero and the
  meter directly beneath it describe the same window; they must not be able to
  disagree. Take `magnitude_state` for the session window and colour the readout
  from that, rather than from the runway projection.
- **Keep the projection, express it in words.** "Exhausted at 03:02, 3h 17m
  before reset" already says it, in the verdict, where it belongs. A rate finding
  should not be wearing a level's colour. If the projection deserves its own
  visual signal, give it the burn-alert chip that already exists — do not
  overload the readout's colour with a second meaning.
- **No green.** The owner asked for green and §5's reasoning still holds: red
  against nothing survives red-green dichromacy, red against green does not.
  Achromatic-when-normal delivers what he actually wants — a healthy 89% that
  does not look like a warning — without a hue that fails for some readers. If
  the instruction later changes to green anyway, it needs a second channel
  alongside the hue, not the hue alone.
- Amber then means what §1 says it means: the session window, elevated. Its use
  in the ring mark is unaffected — the arc is a series colour, not a state.

## Verification

- A test per tier: normal renders the readout with no colour override; elevated
  and critical render theirs; withheld renders `--txt-far`.
- A test that the readout's tier and the session meter's tier are always equal —
  they describe the same window and must not disagree on screen.
- A test that a steep projection no longer turns the readout red on its own.
- Render at a healthy value and confirm by eye that the page has no chroma on it
  except the series colours in the chart: at 89% left with nothing wrong, the
  dashboard should be quiet.
- Update `DESIGN.md` §2 and §5 so the tier table and the one-rule section agree,
  and say in your summary which sections you changed.
- `ruff check` and `pytest` clean.
