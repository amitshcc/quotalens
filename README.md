# quotawatch

A local, self-hosted monitor for Claude Pro/Max subscription usage. It records
your quota over time and leads with the **burn rate** (percentage points per
hour), so you can tell not just where you are but whether something is running.

Status: **pre-alpha**. Milestones M0 and M1 (credential handling, poller,
storage, read APIs) are in progress. No dashboard yet.

## Quick start

```sh
pipx install quotawatch      # or: uv tool install quotawatch
quotawatch auth              # paste your claude.ai session cookie once (stored in the OS keychain)
quotawatch probe             # one fetch, prints raw + parsed output for debugging
quotawatch serve             # binds 127.0.0.1:8787, polls every 60s
curl 'http://127.0.0.1:8787/api/quota/current'
```

## Security note

The session cookie is equivalent to your claude.ai password. quotawatch stores
it only in the OS keychain (via `keyring`), never in a file, the database, or a
log line, and redacts it from error output. The server binds loopback only.
Treat `quotawatch probe` output as sensitive and redact it before sharing.

## Disclaimer

Unofficial. Uses undocumented claude.ai endpoints that may change without
notice. Not affiliated with or endorsed by Anthropic. It only observes usage;
it cannot raise or bypass a limit. For a macOS menu bar view of the same data,
see [ClaudeUsageBar](https://github.com/Artzainnn/ClaudeUsageBar), which this
project complements rather than replaces.

## License

MIT.
