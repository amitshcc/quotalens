# Fix the type in the weekly budget table

The panel answers the right question now. Its typography does not hold up.

## What is wrong

`.bignum` is `font-size: var(--fs-sub)` (18px) against a table set in
`--fs-sm` (12px), and `_budget()` applies it to whatever string is in the
"Full sessions left" cell — number or not.

1. **The same words render at two sizes in one row.** The Fable row reads
   `none left` at 18px mono in "Full sessions left", and `none left` at 12px in
   "At your typical session", side by side. That is the visible glitch.
2. **A phrase is wearing a readout's clothes.** The column is sized large because
   it carries *the answer*, and the answer is a quantity. `none left` is prose; at
   18px it outweighs `0.3` beside it and becomes the loudest thing in the panel,
   which inverts the emphasis — the row with nothing left shouts, the row with a
   real number does not.
3. **`--fs-sub` is not a table size.** DESIGN.md §6 assigns it to "unit next to
   the readout, state reference values", and the scale is deliberately
   non-modular — 68 / 28 / 18, then 15 / 13 / 12 / 11 — so that a cell never sits
   at readout scale. An 18px line also crowds `--row-h` at 28px, and the affected
   rows stand taller than the rest of the table.
4. **The Fable row has an unexplained empty cell.** "Each full session costs" is
   blank with no em dash and no reason, which the rest of this panel would not
   accept anywhere else.
5. **The cost column's parenthetical** — `(10–15, from 6 sessions)` — is long and
   right-aligned to the table's far edge, opening a wide gap between it and the
   column beside it.

## What to change

**Make the answer column uniformly numeric, and let the words be words.**

- Fable's answer is a number: it is **0**. Render `0` in the answer column at the
  column's size, and put `none left` beside it as a `.far` note — exactly the
  pairing `typical` and `cost` already use. Every row then has the same shape, the
  column right-aligns on real digits, and the emphasis lands where the quantity
  is.
- Decide number-versus-phrase in the **view**, not the renderer. `budget.py`
  already knows: `full_windows == 0.0` is the exact zero case, `None` is the
  unknown case. Carry that distinction into `BudgetRowView` as a field rather than
  inferring it from whether a string parses.
- If a cell must ever hold a phrase in that column, it takes body size and
  `--txt`, never `.bignum`. Add the modifier rather than letting `.bignum` cover
  both.
- **Drop `.bignum` from `--fs-sub` to `--fs-lg` (15px).** It still reads as the
  answer against a 12px table, it stops colliding with `--row-h`, and it stops
  borrowing a size the scale reserves for readouts. Check the row heights are
  uniform afterwards.
- **Fill the empty cost cell.** Fable's cost is not unknown — it is unmeasurable
  while the limit is saturated, which `budget.py` already reasons about
  (`SATURATED_PCT`). Say that in the cell, briefly, or give it the em dash plus
  the reason the way the no-number row does.
- **Give the cost parenthetical room.** Either put the note on a second line
  within the cell, or shorten it to `12 pts · 10–15 · n=6`. Do not let it set the
  column width.

## Constraints

- Do not change any value. This is typography and cell content only;
  `/api/budget` stays byte-identical.
- Keep `font-variant-numeric: tabular-nums` on the numeric columns so the digits
  stay in their columns as they change.
- Stay on the documented scale. No new font sizes; use tokens that exist.
- Keep the no-number row's `reason` text exactly as it is — that fix is working.

## Verification

- Render the panel in all four row states — a real number, exact zero, unknown
  with a reason, and a saturated limit — and confirm every row has the same
  height and the same shape, and that no string appears at two sizes in one row.
- Screenshot at desktop width in both themes and check the answer column reads as
  the answer without dominating the panel.
- `ruff check`, `pytest`, and the `/api/budget` snapshot all clean.
