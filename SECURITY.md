# Security policy

QuotaLens handles the one credential that is equivalent to your claude.ai
password — your session cookie. Reports about how it is stored, redacted, or
transmitted are the ones that matter most, and I would rather hear them
privately first.

## Reporting a vulnerability

Please **do not open a public issue** for anything involving the session
cookie, credential storage, redaction, the local web server's exposure to
other software on the machine, or a way for a website to reach the dashboard.

Report privately via GitHub's private advisory form:
  https://github.com/amitshcc/quotalens/security/advisories/new

If that is unavailable, email work@amit.cloud with "QuotaLens security" in the
subject.

## What to expect

This is maintained by one person in evenings, so the timelines are honest
rather than corporate:

- Acknowledgement within **5 working days**.
- An initial assessment (in scope / not / need more info) within **14 days**.
- I will keep you updated at least every **14 days** until it is resolved, and
  credit you in the release notes unless you ask me not to.

## In scope

- The dashboard's exposure to other websites the user visits (CSRF, DNS
  rebinding, anything that reaches `127.0.0.1:<port>` from off-machine).
- Leakage of the session cookie via logs, errors, exports, the database, or the
  config file.
- Handling of a hostile or spoofed upstream response.
- File permissions on the database, config, and logs on a shared machine.
- The release/publish supply chain.

## Out of scope

- The dashboard having no authentication of its own. This is documented design:
  it binds loopback only and there is no `--host`. Reaching it from *another
  machine* is out of scope; reaching it from a *website on the same machine* is
  in scope.
- Anything requiring local root or an already-compromised machine.
- Denial of service against a server you deliberately exposed off-loopback via
  your own proxy.

## Supported versions

Only the latest released version is supported.
