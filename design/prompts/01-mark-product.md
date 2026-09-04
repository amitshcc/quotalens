# Adopt the ring mark in the product

The QuotaLens mark has changed. It was a ray diagram — a beam through a
biconvex lens, leaving as two diverging rays. It is now a **ring**: a faint full
circle (the whole session window) with a solid arc drawn over it clockwise from
twelve o'clock (the part of that window you have used).

This is settled. Do not redesign it, do not propose alternatives, do not adjust
the geometry. Read `design/DESIGN.md` §9 first — it is already rewritten and is
the specification. `design/mark.svg`, `design/favicon.svg` and `design/logo.svg`
are already the new drawings, and `design/preview.html` already shows the mark in
the header. **Those five files are inputs. Do not edit them.**

The geometry, so you do not have to derive it:

```
grid 24 · centre 12,12 · r 9 · stroke 2.6 · circumference 2πr = 56.55
track   stroke var(--txt-far), full circle
arc     stroke var(--s1), stroke-dasharray "<56.55 × fraction> 56.55",
        transform="rotate(-90 12 12)"
favicon redrawn on a true 16 grid: centre 8,8 · r 5.5 · stroke 3 · circ 34.56
```

## What to change

1. **`src/quotalens/render.py` — the `MARK` constant.** Replace the four lens
   paths with the two circles. Strip any `<style>`; reference `var(--txt-far)`
   and `var(--s1)` directly so the mark resolves from `tokens.css` and follows
   the theme toggle. Keep it at 22×22 in the header, as now.

2. **Make the header mark live.** `MARK` becomes a function of the dashboard,
   not a constant: the arc's dash length is the current *session window*
   percentage, not a fixed 68%. It is the same reading as the 5-hour meter on
   the page — take it from the same place, do not recompute it from a different
   field.

3. **Make `/favicon.svg` live too.** `api.py` currently serves the static bytes
   of `web/favicon.svg`. Render it instead from the current reading, on the 16
   grid, so the tab strip shows the session window from a background tab. Serve
   it with `Cache-Control: no-store` — a cached favicon defeats the whole point.
   Fall back to the static file when there is no reading yet.

4. **States follow the meters, not the brand.**
   - normal → arc in `var(--s1)`
   - elevated → arc in `var(--st-elevated)`
   - critical → arc in `var(--st-critical)`
   - stale, auth failed, unverified → **dashed empty track, no arc at all**
     (`stroke-dasharray="3 3"` on the track circle). Never freeze the arc at the
     last good value. A stale mark showing a confident reading is a lie in the
     tab strip, the same way a stale meter showing a red number is one.

5. **`src/quotalens/web/favicon.svg`** — replace with the contents of
   `design/favicon.svg` verbatim. It stays as the no-reading fallback.

6. **Accessibility.** The header mark stays `aria-hidden="true"`: the word
   "QuotaLens" is already beside it as text, and the percentage is already on
   the page as a meter, so announcing it a third time is noise. The standalone
   `favicon.svg` keeps its `role="img"` and an `aria-label` carrying the
   reading.

## Constraints

- No new dependencies, no build step, no chart or icon library. Hand-rolled, as
  the rest of this codebase is.
- Do not touch `tokens.css`, and do not add or change a colour token. The arc
  uses tokens that already exist.
- The one rule in `DESIGN.md` §1 still holds: amber means the session window.
  The mark may carry amber because its arc *is* the session window. That is not
  licence to put amber anywhere else in the chrome.
- Build the SVG from attributes. No inline `<style>` blocks and no `style=""`.

## Verification

- Add tests under `tests/`: the dash length equals `56.55 × fraction` to two
  decimals for a handful of percentages; a stale dashboard renders a mark with
  no arc element; the favicon route reflects the current reading and returns
  `no-store`; the no-reading case falls back to the static file.
- `ruff check` and `pytest` clean.
- Start the app against real data and confirm by eye: the header mark matches
  the 5-hour meter, the favicon in the tab matches both, and the mark goes to a
  dashed empty ring when you stop the collector.

Finally, report anything in the repo that still refers to the lens, the ray
diagram or refraction — comments, docstrings, README, `docs/` — and fix those
references in the same change.
