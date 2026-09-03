# QuotaLens — design system

The direction is **Bench**: the page is a piece of test equipment. Chrome is the
matte housing; anything that plots data is an inset display. That distinction —
not a card with a shadow — is what creates hierarchy here. There is exactly one
lit element on screen, the session window, and everything else is disciplined and
quiet.

Files: `tokens.css` is the contract. `preview.html` is the system applied to a
real dashboard and is the reference for spacing, density and state treatment.
`mark.svg` / `logo.svg` / `favicon.svg` / `og-image.png` are the brand assets.

---

## 1. The one rule

**Amber means the session window, and nothing else.**

Chrome may never use amber. Not for a hover, not for a border, not for a link.
The headroom readout is amber because it is what is left of the session window;
the `--s1` trace is amber because it is that window plotted over time; the
elevated state is amber because it means "the session window is being consumed
fast." Those three uses agree with each other. A fourth use would break all of
them.

Everything else in the chrome is achromatic. If a chrome element seems to need
colour, it is either data (use a series colour) or a state (use a state
treatment). It is never a brand accent.

## 2. Three tiers of emphasis

| Tier | What it is | Treatment |
|---|---|---|
| **Lit** | The session headroom. One per screen. | `--lit`, `--fs-readout`, mono, tabular |
| **Legible** | The data: traces, current values, table figures | `--txt`, chroma only from the series palette |
| **Quiet** | Everything else: labels, axes, rules, nav, units, timestamps | `--txt-dim` / `--txt-far`, never chroma |

When you are unsure which tier something belongs to, ask whether the number
would change if the poller stopped. If yes it is data. If no it is chrome.

## 3. Surfaces

Three greys, and a rule that decides which one you use.

**The screen is the extreme of the value range** — the darkest surface in dark,
the lightest in light. It is the maximum-contrast field, because traces are
drawn on it. **The chrome sits one step toward mid-grey.** The case is between
them.

```
dark    screen #0C0F10   case #14181A   chrome #191E20
light   screen #FAFBFA   case #E4E6E5   chrome #DBDEDD
```

Apply `.screen` only to a surface that plots or tabulates data. In the preview
that is five surfaces: the hero, the window-meter strip, the 24-hour chart, the
attribution table, and nothing else. The side panel is a spec list, so it gets a
hairline and no box. The state reference is documentation, so it gets nothing.

A recess is one hairline border plus the background step. There are **no
shadows in this system** and no radius above 3px. If you find yourself adding a
drop shadow to separate two things, you have too many boxes — merge them and
divide with a hairline, the way the three window meters are one surface with two
internal rules rather than three cards.

## 4. Palette

Chrome carries no chroma at all. All the chroma in `tokens.css` is either a
series colour or a state colour.

Why not the obvious alternatives: warm cream with a terracotta accent is both
everywhere right now and specifically close to Anthropic's own accent, which
this project should not appear to borrow. Near-black with one acid accent reads
as a landing page, not an instrument. The grey-green case here (`#14181A` has a
slight green cast; `#E4E6E5` is its light counterpart) is the colour of anodised
equipment housing — enough character to not be `#111`, quiet enough to disappear
behind the data.

Amber (`#F2B33D` dark, `#8A5B00` light) is the sodium-lamp colour of a bench
readout. It is not near `#D97757`: more yellow, more saturated, and used as the
one lit element, the session window, rather than as a brand wash.

### Series

Six simultaneous series, derived from the Okabe–Ito set, which is constructed to
survive dichromacy.

| Slot | Meaning | Dark | Light | Dash | Weight |
|---|---|---|---|---|---|
| `--s1` | session window | `#F2B33D` | `#A66A00` | solid | `--trace-hero` |
| `--s2` | 7-day window | `#7FCDEF` | `#0E6E9C` | solid | `--trace` |
| `--s3` | 7-day Sonnet | `#4FC08A` | `#00704E` | `7 3` | `--trace` |
| `--s4` | per-model limit | `#6E9BE8` | `#2A4FA0` | `2 3` | `--trace` |
| `--s5` | per-model limit | `#DE79AC` | `#A32C6A` | `10 3 2 3` | `--trace` |
| `--s6` | per-project / other | `#B9A0E8` | `#5F42A8` | `4 4` | `--trace-dim` |

Slot 1 is always the session window, so the amber in the chart is the same amber
as the headroom readout and means the same thing. Assign the rest in order; do not pick
by taste.

**Colour is never the only channel.** Every series carries three: hue, a dash
pattern, and a label drawn at the end of its own line. Under simulated
deuteranopia the pairs that converge are s3/s5 (khaki vs pale grey) and s4/s6
(two blues); in both cases the dash patterns are maximally different — a long
dash against a dash-dot, a fine dot against an even dash — and the end labels
settle it outright.

A swatch legend is not an acceptable substitute. It still makes colour the only
channel; it just moves the failure to the corner of the chart.

## 5. States

Six, and they are two different kinds of thing.

**Magnitude states colour the value. Epistemic states colour the frame and take
the value away.**

| State | Colour | Second channel | Value shown |
|---|---|---|---|
| normal | none | — | yes |
| elevated | `--st-elevated` (amber) | filled chip | yes, in amber |
| critical | `--st-critical` | outlined chip + alert triangle | yes, in red |
| stale | `--st-stale` | dashed border + hatched fill | **no — em dash** |
| auth failed | `--st-auth` | 3px solid left rule + key glyph | **no — em dash** |
| unverified | `--st-stale` | dashed border + hatched fill, own message | **no — em dash** |

Normal has no chip and no colour. Absence is the signal, and this is why the
system has no green: red against nothing cannot fail for a red-green dichromat,
where red against green can.

Unverified is the collector's third epistemic failure: claude.ai answered but
the response could not be parsed, or was recovered by the generic fallback and
cannot be trusted. It borrows the stale treatment because the consequence is the
same — the number on screen is not known — but carries its own wording, because
the action is different: run `quotalens probe` and report the shape, rather than
restart the collector or refresh a cookie.

Stale, auth-failed and unverified must never be styled anywhere on the amber→red ramp. They
are not high usage. A stale meter showing a large red number is a lie: the
correct display is an em dash, a hatched bar, and a timestamp for the last good
sample. Quota during an auth failure is unknown, not zero, and local scanning
keeps working — say so in the panel rather than blanking the page.

## 6. Type

Two stacks, no webfonts, no downloads.

```
--font-ui   system-ui, -apple-system, "Segoe UI Variable Text", "Segoe UI",
            Roboto, "Helvetica Neue", Arial, sans-serif
--font-num  ui-monospace, SFMono-Regular, "SF Mono", "Cascadia Mono",
            "Segoe UI Mono", "Roboto Mono", "Liberation Mono", Menlo,
            Consolas, monospace
```

`system-ui` resolves to SF on macOS and iOS, Segoe UI Variable on Windows 11,
Roboto on Android, and whatever fontconfig maps on Linux. The explicit entries
after it are the fallback chain for older Windows and for Linux systems where
`system-ui` resolves to something unpleasant.

`ui-monospace` gets SF Mono on Apple platforms without a licence question;
Cascadia Mono ships with Windows 11; Liberation Mono is the near-universal Linux
fallback. Every stop on that chain is a real monospace, which matters because
**mono is here for tabular figures, not for flavour**. A burn rate that reflows
its digits every poll is unreadable; `font-variant-numeric: tabular-nums
slashed-zero` is set on `body` and inherits everywhere.

Use mono for numbers, timestamps, paths, model identifiers and axis ticks. Do
not use it for prose, headings, or small labels as decoration.

### Scale

| Token | px | Use |
|---|---|---|
| `--fs-readout` | 48–68 fluid | the session headroom, once |
| `--fs-metric` | 28 | window percentages, the reset countdown |
| `--fs-sub` | 18 | unit next to the readout, state reference values |
| `--fs-lg` | 15 | product name |
| `--fs-md` | 13 | body, table cells |
| `--fs-sm` | 12 | labels, captions |
| `--fs-xs` | 11 | axis ticks, units, meta |

This is deliberately not a modular scale. The readouts jump hard (68 → 28 → 18)
and the text sizes are compressed into 15/13/12/11, because the whole design
depends on one number dominating a quiet field. A smooth 1.25 ratio would give
you six sizes that all look similar and no hierarchy at all.

13px base is the monitoring-tool convention and is what makes the density
target reachable. Line height is 1 for numbers (no descenders to clear), 1.45
for prose, 1.3 for table rows.

Labels are sentence case. No tracked-out all-caps eyebrows, no middle-dot
separators, no arrows appended to text.

## 7. Space and density

4px grid: `--sp-0` 2, `--sp-1` 4, `--sp-2` 8, `--sp-3` 12, `--sp-4` 16,
`--sp-5` 24, `--sp-6` 32, `--sp-7` 48. Nothing off-grid.

Table rows are 28px comfortable, 24px compact (`--row-h`, `--row-h-tight`).
Section gaps are `--sp-3`; the only `--sp-5` gaps are above the hero and above
the state reference, because those are the two real breaks on the page.

The target: header, burn rate, all three windows and the 24-hour chart visible
without scrolling on a 900px viewport. In the preview that block ends around
730px. The attribution table starts above the fold and continues below it,
which is correct — the current state is what must be readable at a glance; the
attribution is what you scroll to when the rate surprises you.

## 8. Icons

Five, hand-drawn, inline, defined once as `<symbol>` in a hidden sprite:
`i-alert` (critical), `i-stale`, `i-auth`, `i-rate`, `i-theme`. They are
16×16, 1.6px stroke, `currentColor`, round caps and joins. One slot is
deliberately unspent.

The lens mark is not one of the five; it is the brand.

## 9. The mark

A ray diagram. A flat line comes in from the left — that is a level, which is
what the Claude usage page gives you. It passes through a biconvex lens and
leaves as two rising, diverging rays — a rate, and the separation of what is
producing it. The lens is the thing that converts one into the other, which is
the product in one drawing.

It is not an aperture. Refraction stayed in the mark; dispersion moved to the
chart palette, where it does actual work.

- `mark.svg` — 24×24, holds down to about 24px.
- `favicon.svg` — the mark simplified, not scaled: the incoming beam is dropped,
  the lens becomes a solid fill, two rays remain, strokes thicken to 2.2–2.6.
- `logo.svg` — mark plus wordmark, 186×40.

Each file carries an internal `<style>` defining fallbacks for both colour
schemes, so a standalone `<img>` renders correctly on either background. **Inline
the SVG wherever the app can**, because only then do `--txt-dim`, `--s1` and
`--s2` resolve from `tokens.css` and follow a manual theme toggle; an `<img>`
can only follow the OS.

The wordmark is set in `--font-ui` as live `<text>`, not outlines. That means it
substitutes per platform — which is the honest consequence of a no-webfonts
system, and keeps the wordmark identical to the type in the product. If you need
a fixed-letterform version for print or a favicon service, outline it then.

## 10. Theming

`tokens.css` declares the palette once using `light-dark(light, dark)`. Dark is
the base; light applies when the OS asks for it or when the toggle sets
`[data-theme="light"]`.

```css
:root { color-scheme: dark; }
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) { color-scheme: light; }
}
[data-theme="light"] { color-scheme: light; }
[data-theme="dark"]  { color-scheme: dark; }
```

The toggle sets one attribute on `<html>`; no class juggling, no second
stylesheet, no flash. Cost: `light-dark()` needs Chrome 123 / Safari 17.5 /
Firefox 120 (mid-2024). Declaring both palettes explicitly instead would work
everywhere and add roughly 700 bytes.

## 11. Runtime cost

No webfonts, no framework, no icon library, no chart library, no CDN, no
external request of any kind. Charts are inline SVG. The theme toggle is the
only script on the page and it is four lines.

Measured, minified, uncompressed:

| | bytes |
|---|---|
| `tokens.css` | 2,769 |
| component CSS (app) | 5,673 |
| **app total** | **8,442** (2,744 gzipped) |
| preview-only rules, not shipped | 441 |

**This is over the 4KB budget, and I did not cut to reach it.** Here is what
that would have cost, in the order I would remove things:

1. Derive `--s*-soft` and `--st-*-bg` with `color-mix()` instead of listing them
   — saves ~800B, but the token file stops being a complete colour contract and
   the app starts computing its own fills. Cheapest cut, real loss.
2. Drop the per-row sparkline column — ~150B. Loses the only per-project shape
   signal in the table.
3. Drop the responsive collapse below 1000px — ~120B. The dashboard stops
   working on a phone.
4. Drop the hatch fill and dashed frame on stale — ~150B. Stale loses a channel
   and starts looking like a magnitude state. I would not do this.

Those four together get to roughly 7.2KB, not 4KB. Reaching 4KB means dropping
either the attribution table or the state system, and neither is optional. My
recommendation is to ship 8.4KB (2.7KB over the wire) and spend the budget
somewhere it is felt: the CSS is parsed once at load and never again, whereas
the poller runs every 60 seconds forever.

If you want it under 4KB anyway, cut the attribution table into a second route
and load its CSS there.

## 12. Things this system does not do

- No shadows. No radius above 3px. No gradient fills except a hatch pattern.
- No green for "good". Normal is the absence of a chip.
- No swatch legends. Series are labelled at the end of their own line.
- No all-caps tracked eyebrow labels, no middle-dot meta strings, no arrows in
  link text.
- No animation beyond a 120ms colour transition, disabled under
  `prefers-reduced-motion`.
- Token counts and quota points are never summed or plotted on one axis. They
  share a timeline, not a unit. If a local-session overlay is added, use
  `--trace-ghost` on a second axis and label both.
