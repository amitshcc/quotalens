# QuotaLens

QuotaLens is a local, self-hosted monitor for Claude Pro/Max subscription usage. It records
your quota over time and leads with the **burn rate** (percentage points per
hour), so you can tell not just where you are but whether something is running.

Status: **pre-alpha**. Milestones M0 to M2 are done: credential handling, the
poller, storage, read APIs, and the dashboard. Per-project attribution from
local Claude Code logs (M3), alerts and export (M4) are next.

## Quick start

```sh
pipx install quotalens      # or: uv tool install quotalens
quotalens auth              # paste your claude.ai session cookie once (stored in the OS keychain)
quotalens probe             # one fetch, prints raw + parsed output for debugging
quotalens serve             # binds 127.0.0.1:8787, polls every 60s
open http://127.0.0.1:8787   # the dashboard
curl 'http://127.0.0.1:8787/api/quota/current'
```

## The dashboard

The number it leads with is the burn rate in percentage points per hour over
the 5-hour window. Below it: one meter per quota window with the API's own
severity, a 24-hour chart of every window with resets drawn as gaps, and the
extra-usage spend computed from minor units with the payload's exponent, never
clamped at 100%.

States are honest by construction. If the collector has not succeeded in three
poll intervals, the cookie was rejected, or the response could not be parsed,
every value is replaced by an em dash and the frame changes, so a stale page
never looks like a healthy one showing low usage. If the browser loses the
server, the same treatment applies from CSS alone.

Renamed from `quotawatch`: on first start an existing database and keyring
entry under the old name are moved across automatically.

## How it reaches claude.ai

claude.ai sits behind Cloudflare bot protection that fingerprints the TLS
handshake. A plain Python HTTP client is served a challenge page even with a
valid session cookie, so QuotaLens talks to claude.ai through
[`curl_cffi`](https://github.com/lexiforest/curl_cffi), which impersonates a
browser's TLS and HTTP/2 fingerprint. If you ever see a `blocked` state on
`/api/health`, try `QUOTALENS_IMPERSONATE=safari` (default `chrome`).

## Security note

The session cookie is equivalent to your claude.ai password. QuotaLens stores
it only in the OS keychain (via `keyring`), never in a file, the database, or a
log line, and redacts it from error output. The server binds loopback only.
Treat `quotalens probe` output as sensitive and redact it before sharing.

## Disclaimer

Unofficial. Uses undocumented claude.ai endpoints that may change without
notice. Not affiliated with or endorsed by Anthropic. It only observes usage;
it cannot raise or bypass a limit. For a macOS menu bar view of the same data,
see [ClaudeUsageBar](https://github.com/Artzainnn/ClaudeUsageBar), which this
project complements rather than replaces.

## License

MIT.
