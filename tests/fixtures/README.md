# Usage payload fixtures

Both are written by `qa/fake_claude.py:build_usage` at a fixed clock (Thu
2026-10-01 12:00Z, weekly reset Mon 2026-10-05 01:00Z); `tests/test_plan_shapes.py`
checks they still match it.

- `usage_max.json` — the Max shape as observed on a real Max account
  (`limits[]`: `session`, `weekly_all`, Fable-scoped `weekly_scoped`; $250 credit).
- `usage_pro_assumed.json` — **assumed from the help centre, not observed
  (2026-10-01)**: Pro has no Fable-scoped weekly limit (Fable runs on usage
  credits there) and a $100 credit. If you are on Pro, a redacted
  `quotalens probe` in an issue would let us replace it with a real one.
