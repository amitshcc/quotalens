# Vendor marks

**These are third-party trademarks. They are not part of QuotaLens's icon set
and are never restyled to match the theme** — see `design/DESIGN.md` §8, which
records this as a deliberate exception alongside the boost rocket.

Each file is a vendor's own brand asset, taken from that vendor's brand page and
shipped **unmodified**. Do not hand-redraw one, do not trace one from a
screenshot, and do not recolour one: a redrawn wordmark looks worse and is a
worse legal position than the real file used nominatively to identify the
service it names.

Expected filenames come from `status.StatusVendor.logo`:

| File | Vendor | Where it comes from |
|---|---|---|
| `claude.svg` | Anthropic / Claude | <https://www.anthropic.com/brand> |
| `openai.svg` | OpenAI | <https://openai.com/brand/> |

**A missing file is a supported state.** The row renders with the vendor's name
alone, exactly as it looked before logos existed, and `/static/vendor/<name>`
returns 404. Nothing breaks; the mark is simply absent.

Prefer the monochrome variant where the vendor offers one, so this row does not
become the only place in the product carrying two brand palettes. Follow each
vendor's published guidance on minimum size and clear space — both marks render
at 16px, which is at or near most brand minimums, so check before assuming.
