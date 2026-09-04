# Fix: the live favicon renders as an empty image

`/favicon.svg` is drawing nothing. Not a wrong colour — nothing. Chrome shows no
icon in the tab because the SVG it receives has zero visible pixels.

## The cause

`ring()` in `src/quotalens/render.py` emits colours as bare custom properties:

```python
body = (f'{circle} stroke="var(--txt-far)"/>'
        f'{circle} stroke="{ARC_COLOUR.get(state, "var(--s1)")}" ...')
```

That is correct for `header_mark()`, which is inlined into a page that loads
`tokens.css`, so `--txt-far` and `--s1` resolve.

It is wrong for `favicon_svg()`. The favicon is fetched as a standalone
`image/svg+xml` document. Nothing loads `tokens.css` into it, so those custom
properties are undefined, and `var(--txt-far)` with **no fallback** is an invalid
substitution: the `stroke` property falls back to its initial value, which for
SVG is `none`. Both circles get `stroke: none` and already have `fill="none"`, so
the image is fully transparent.

Confirmed by rendering the exact string the code produces today. Chromium reports
`getComputedStyle(circle).stroke === "none"` on both circles, and the rasterised
16×16 has 0 non-transparent pixels.

Two things follow from this that are worth noticing:

- The checked-in `src/quotalens/web/favicon.svg` is **not** affected — it carries
  its own `<style>` with literal fallbacks. So the icon appears before the first
  successful poll and disappears after it. That asymmetry is the tell.
- No test caught it because asserting on the emitted string cannot see a colour
  that fails to resolve. See the verification section.

The docstring on `ring()` says an inline `<style>` "would fail a strict CSP".
That is true of the website repo and it is not true here: this app serves no CSP
header, and the favicon is fetched as an image resource rather than parsed as a
document under one. It also is not the fix being asked for — the fix below keeps
the header path attribute-only.

## The fix

1. **Give every colour a fallback in `ring()`.** `var(--txt-far, #626A6B)`,
   `var(--s1, #F2B33D)`, `var(--st-elevated, #F2B33D)`,
   `var(--st-critical, #F0575C)`. Dark values, because `tokens.css` declares
   `color-scheme: dark` as the base and light is the override. In the inlined
   header mark the token still wins, so nothing changes there; in the standalone
   favicon the fallback applies and the mark draws.

2. **Add the light-scheme overrides to the favicon path only.** In
   `favicon_svg()`, and nowhere else, prepend:

   ```html
   <style>@media (prefers-color-scheme:light){svg{
     --txt-far:#808887;--s1:#A66A00;--st-elevated:#8A5B00;--st-critical:#B3242B}}</style>
   ```

   Take those four values from `tokens.css` rather than from this file, and fail
   loudly if any of them has changed since — do not let two sources of truth for
   a colour drift silently. `header_mark()` must keep emitting no `<style>` at
   all.

3. **Change `Cache-Control: no-store` to `no-cache`** on the favicon response.
   `no-cache` still revalidates on every fetch, so the reading stays live, but it
   does not tell the browser it may not write the resource to disk — and Chrome's
   favicon store is a disk cache. This is not the diagnosed cause; it is a free
   variable to remove while you are here.

## Verification — the part that actually matters

A string assertion cannot catch this class of bug. Add a test that **rasterises**
the favicon and asserts it is not blank:

- Render `favicon_svg(dash)` in a headless browser and assert
  `getComputedStyle` on each `<circle>` returns a real `rgb(...)`, never `none`.
- Screenshot it and assert the count of non-transparent pixels is greater than
  zero, for a normal reading, for elevated, for critical, and for the no-reading
  case.
- Do the same in both `prefers-color-scheme` settings, and assert the two
  schemes produce *different* colours — otherwise the media query is dead and
  nobody would know.
- Keep an assertion that the inline `header_mark()` output contains no `<style>`.

If Playwright is not already a dev dependency, a rasterising check is worth one.
If you decide it is not, say so explicitly and put the reason in the test file,
rather than leaving a string assertion that looks like coverage and is not.

Finally: after the fix, Chrome will still show the old empty icon for a while —
its favicon cache is per-origin and aggressive. Verify in a fresh profile or a
hard reload before concluding anything about whether the fix worked.
