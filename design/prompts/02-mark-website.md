# Adopt the ring mark on the website

The QuotaLens mark has changed from the ray-diagram lens to a **ring**: a faint
full circle (the whole session window) with a solid arc over it, clockwise from
twelve o'clock, showing the part used. This is settled — do not redesign it or
adjust the geometry.

The specification is `../quotalens/design/DESIGN.md` §9. The drawings are
`../quotalens/design/mark.svg`, `favicon.svg` and `logo.svg`. **Those are
inputs; do not edit them here.** The site copies brand assets from the product's
design folder the same way it copies `tokens.css` — verbatim, never edited
downstream.

Geometry, so you do not have to derive it:

```
grid 24 · centre 12,12 · r 9 · stroke 2.6 · circumference 56.55
track   stroke var(--txt-far), full circle
arc     stroke var(--s1), stroke-dasharray "38.45 56.55"   (68%, the canonical
        static value) transform="rotate(-90 12 12)"
```

## What to change

1. **`public/index.html`** — replace the inlined lens SVG in the header (the
   `<svg>` inside `a.brand`, currently four `<path>` elements) with the two
   circles. Keep the existing pattern exactly: inlined, internal `<style>`
   stripped, colours referenced as `var(--txt-far)` and `var(--s1)` so they
   resolve from `tokens.css` and follow the theme toggle. Update the comment
   above it, which currently describes the lens.

2. **`public/favicon.svg`** — replace with `../quotalens/design/favicon.svg`
   verbatim (the 16-grid redraw, not the 24 scaled down).

3. **`brand/mark.svg` and `brand/logo.svg`** — replace with the versions from
   `../quotalens/design/`, verbatim.

4. **`public/og-image.png` is now stale** — it still shows the lens. There is no
   source file for it in the repo. Do not fake one. Add a line to `TODO.md`
   recording that the OG image needs re-rendering with the ring, and say so in
   your summary. Leave the PNG in place until it is re-rendered.

5. The site's mark stays **static at 68%**. The live version is a product
   feature; the website has no reading to show and must not imply it does.

## Constraints — these are the ones this repo exists under

- **CSP.** `public/_headers` is `default-src 'none'` with `'self'` and no
  `'unsafe-inline'`. No inline `<style>`, no `style=""`, no inline `<script>`.
  Fix any violation by moving code into a file, never by loosening the policy.
- **No third-party requests at all.** No webfonts, no icon library, no CDN, no
  analytics.
- **Do not edit `public/tokens.css`.** It is copied verbatim from the product.
  Site-specific values go in `site.css`.
- **The amber rule.** Amber is the session window and nothing else. No link,
  border or hover on this site uses it. After this change the only amber on the
  page is still exactly two things: the arc inside the inlined mark, and the
  dashboard screenshot. Do not let the ring become a decorative accent anywhere
  else — not a bullet, not a rule, not a hover.
- No build step, no framework, no package manager. Hand-written HTML and CSS.
- Asset paths in `index.html` stay relative, so the page still opens from disk.

## Verification

- Render `public/` headlessly and assert **zero off-origin requests**.
- Check the mark in both colour schemes and through the theme toggle: the track
  must be visible against the page in light and dark, and the arc must be the
  same amber as `--s1`.
- Check the mark at the size it actually renders in the header, and check the
  favicon in a real tab at 16px — if the track disappears against the tab strip,
  say so rather than adjusting the geometry yourself.
- Confirm the page still passes its CSP with no console violations.

Report anything else on the site that still refers to the lens — copy in
`COPY.md`, alt text, comments — and fix it in the same change.
