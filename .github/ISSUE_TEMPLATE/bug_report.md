---
name: Bug report
about: Something QuotaLens did that it should not have
labels: bug
---

## Before you paste anything

**`quotalens probe` output is your account's data.** It never contains your
session cookie, and UUID-shaped values are masked by default — but only by
default. Do not paste output from `probe --no-redact` or from
`/api/export.json?table=samples&raw=1`; both are unmasked on purpose.

If you need to show a payload, `quotalens probe` (no flags) is the safe form.
If you are unsure whether something identifies you, leave it out and say so; a
missing field is easier to ask for than a leaked one is to take back.

Never paste your cookie. Nobody debugging this will ever need it. If you think
you have pasted one anywhere, sign out of claude.ai on all devices, which
invalidates it.

## What happened

<!-- What you saw, and what you expected instead. -->

## How to reproduce

<!-- The exact commands, and the URL if it was the dashboard. Every view is a
     URL, so pasting the address bar captures the range, the lookback and which
     series were hidden. -->

## Environment

- QuotaLens version: <!-- quotalens --version -->
- OS and version:
- Installed with: <!-- pipx / uvx / from source -->
- Python: <!-- python3 --version -->

## Collector state

<!-- `quotalens status` output. It contains your window percentages and reset
     times but no identifiers. Trim it if you would rather not share those. -->

```
```

## Anything in the log

<!-- `quotalens logs -n 50`. The cookie is redacted from logs by design; if you
     ever find one in there, that is itself the bug and it is a serious one —
     please report it privately rather than in a public issue. -->

```
```
