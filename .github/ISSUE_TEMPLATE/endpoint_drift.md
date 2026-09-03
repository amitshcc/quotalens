---
name: Endpoint drift
about: QuotaLens says the response shape changed, or a number looks wrong
labels: drift
---

## Before you paste anything

`quotalens probe` masks UUID-shaped values by default. Use it without
`--no-redact`, and do not paste raw sample exports. See the bug report template
for the longer version; the short version is that the payload is your account's
data and the safe form is `quotalens probe` with no flags.

## What the dashboard said

<!-- "shape drifted", "could not be parsed", a window missing, a percentage that
     disagrees with claude.ai's own Settings page, and so on. -->

## The parsed section of `quotalens probe`

<!-- The `== parsed ==` block alone is often enough, and it contains no
     identifiers at all. Start with that. -->

```
```

## The raw payload, if the parsed section is not enough

<!-- `quotalens probe` (masked). Delete anything you would rather not share;
     the top-level key names are the part that matters for drift. -->

```
```

## What claude.ai's own Settings → Usage page showed at the same moment

<!-- A number to compare against. A screenshot with the account details cropped
     out is fine. -->
