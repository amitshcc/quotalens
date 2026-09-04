# The budget table's answer column is still a different size — make it uniform

Previous instructions asked for that column to carry "a readout's weight", then
to drop from `--fs-sub` to `--fs-lg`. Both were wrong. The owner wants the row to
read as one row.

Today in `app.css`:

```css
.bignum{font-family:var(--font-num);font-size:var(--fs-lg);color:var(--txt)}
.bignum .far{font-size:var(--fs-sm)}
```

`--fs-lg` is 15px. The table is `--fs-sm`, 12px. So `0.2` is set 25% larger than
`Weekly — all models` and `3%` in the same row, and the gloss inside the same
cell is 12px against its own 15px parent. That is the complaint, and it is
visible.

## The change

**Remove the font-size override.** The answer column renders at the table's own
size, identical to every other cell in the row:

```css
.bignum{font-family:var(--font-num);color:var(--txt);font-weight:var(--w-med)}
```

- Drop `.bignum .far{font-size:...}` too — it exists only to climb back down from
  a size that will no longer be there. The gloss keeps its `--txt-far` colour,
  which is what distinguishes it.
- **Emphasis comes from colour and weight, never size.** The other cells are
  `--txt-dim`; the answer stays `--txt` and may take `--w-med`. That is enough to
  make it the thing your eye lands on inside a 12px row, and it costs no vertical
  rhythm.
- Keep `--font-num` on the numeric cells. The digits have to align down the
  column, and DESIGN.md §6 is explicit that mono is there for tabular figures.
- Update the comment on line 159 — "the answer column carries a readout's weight"
  is no longer what this does.

## One thing to check by eye, not by token

At equal pixel size, a monospace face can still read slightly larger than the
sans beside it, because of cap-height and set width. Look at the rendered row
before calling this done. If `0.2` still reads heavier than `3%`, the answer is
**not** to shrink it below the table's size — that reintroduces the same class of
bug from the other direction. Drop `--w-med` first, and if it is still off, say
so and leave it rather than inventing a size.

## Verification

- Assert with `getComputedStyle` that every `td` and `th` in a budget row returns
  the same `font-size`, including the gloss span inside the answer cell.
- Confirm the same in all three row states — a number, exact zero with its gloss,
  and the unknown row with its reason sentence.
- Row heights stay at 28px and match the history table.
- `/api/budget` unchanged; `ruff check` and `pytest` clean.
