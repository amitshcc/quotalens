# Launch readiness — 2026-09-06

**Verdict: do not tag tonight. Ship after six fixes** — two are one middleware,
three are a few lines each in `metrics.py`/`dashboard.py`, one is site wording —
**plus one CI gate that will physically stop the tag from publishing.**
Reason, in one sentence: any website the user visits can rewrite or read the
dashboard, three surfaces present a number as certain when the product knows it
is not, the site's headline privacy claim is false, and the release workflow's
own `pytest` step fails on the pinned commit.

**Pinned commits.** `quotalens` `3b7d2ec84de5feaa2d7e86c889b23a06673787f4` ·
`quotalens-web` `ad9e605e00cf7fb9909ad3cfda4b912f608141b2`. Every finding below
is a claim about those two commits. **The published repo (`origin/master`
`c1891b9`, v0.1.0) is 21 commits behind the audited tree**; nothing found here
is visible to the public until they are pushed.

Phase reports, each read back in full and spot-checked before this was written:

- **A — security:** [`SECURITY-AUDIT-2026-09-06.md`](SECURITY-AUDIT-2026-09-06.md)
- **B — correctness:** [`AUDIT-2026-09-06.md`](AUDIT-2026-09-06.md)
- **C — the site:** [`../../quotalens-web/REVIEW-2026-09-06.md`](../../quotalens-web/REVIEW-2026-09-06.md)

Independent re-verification by the controller, on a fresh instance after the
phases finished (fake upstream on 38791, `serve --port 38790`): cross-origin
`POST /settings` with `Origin: https://evil.example` → `303`, `webhook_url`
persisted to `config.json`; `curl -H 'Host: attacker.example' /api/quota/series`
→ `200` with readings; `spend.used.exponent: 60` → `/`, `/api/health`,
`/api/quota/current` all `500`, `/metrics` `200`; lapsed session → `/metrics`
`quota_percent{five_hour} NaN` beside `session_headroom_percent 60`. All four
reproduce exactly as reported. `metrics.py` contains the word `boost` zero times
(F3 below); `dashboard.py:527-543` passes only the collector-level `withheld`
into `_burn_view` (F1 below).

---

## 0. Preflight

| Check | Result |
|---|---|
| `quotalens` HEAD | `3b7d2ec84de5feaa2d7e86c889b23a06673787f4` — `fix: the de-dup event could not be read back, and pre-tag polish`, 2026-09-06 11:01 IST, branch `master` |
| `quotalens-web` HEAD | `ad9e605e00cf7fb9909ad3cfda4b912f608141b2` — `copy: vendor-agnostic wording, Claude-only note`, 2026-09-06 10:55 IST, branch `main`, 0 ahead of `origin/main` |
| `quotalens` tree | **clean** (`git status --porcelain` empty) |
| `quotalens-web` tree | **dirty — two untracked files**: `FEEDBACK-01.md`, `REVIEW-PROMPT.md`. Neither is site content (`public/` untouched). You chose to proceed; every report records this. |
| Gap to the public repo | **21 commits ahead** of `origin/master` (`c1891b9`), not 13 as the controller prompt says. `origin/master` reads `version = "0.1.0"` in both `pyproject.toml` and `__init__.py`; the local tree reads `1.0`. **No tag exists locally or on GitHub.** |
| Sibling repo | reachable at `../quotalens-web`; phase C ran. |
| CI on the pinned commit | **Has not run.** Latest run is `33981687447` on `c1891b9`, `success`, 2026-09-05T17:40:21Z, **8 of 8 jobs green** (lint, render, ubuntu/macos/windows × 3.11/3.13 — read from the GitHub API via the Mac, since the cloud workspace's GitHub egress is gated). The 21 local commits have never been through CI. |
| PyPI | `quotalens` → 404: the name is free. Whether a pending trusted publisher is configured is **not checkable here** (PyPI account settings). |
| `amitsharma/quotalens` | **404** — the security-advisory link in `.github/ISSUE_TEMPLATE/config.yml` leads nowhere (A F7). Repo `amitshcc/quotalens` is public, default branch `master`. |

**Baseline environment.** The connected-folder shell on the Mac runs inside a
Linux aarch64 VM with Python 3.10 (the project requires ≥3.11), no Playwright,
and no route to the running app or to `~/Library/Application Support/quotalens`
(a protected location; the folder-access request was refused by the desktop
app). So both repos were copied, with full git history, into the cloud
workspace: Linux x86_64, Python 3.11.15 (+ clean 3.12 for the wheel), `uv 0.12`,
`ruff 0.15.11` (CI pins `0.16.*`), Playwright 1.56.0 with Chromium 141 (pinned
down from 1.62 so the bundled browser matched). Both copies verify to the pinned
commits with identical `git status`. Root user (read-only-directory cases are
void). No keyring backend.

**No real `quotalens.db` is reachable.** Storage figures are measured against a
harness-filled database; §5 says what that downgrades.

**Baseline test run, before any phase started** (`python -m pytest`, source
tree, headless container):

```
1 failed, 533 passed in 17.97s
FAILED tests/test_status_and_settings.py::test_every_panel_key_is_actually_rendered_as_a_field
AssertionError: {'notify'}
```

`ruff check` / `ruff format --check` clean; the two browser test files pass
(24 passed). The failure is real and it is the CI gate in §2.

---

## 1. Blockers — deduplicated across all three phases, ranked

A blocker is only: something an ordinary website can do to a user running this;
a security or privacy claim in the README or on the site that is false; a
number the product presents as certain that is not; or a documented command
that fails on a clean install. Applied strictly. Each names its owning phase and
links to the detail rather than repeating it.

| # | Blocker | Owner | Detail | Effort |
|---|---|---|---|---|
| **1** | **Any website can rewrite the dashboard's settings.** No `Origin`/`Sec-Fetch-Site`/CSRF check on `POST /settings`, `/settings/retention`, `/poll`, `/api/poll`, `/api/notify/test`. Verified from a different origin in headless Chromium: sets `webhook_url` (exfil of burn-rate/working-hours telemetry from the next crossing, indefinitely — no cookie, no org id), sets retention to `1week` and prunes months of history, clears every panel key it omits. | A | [A F1–F3](SECURITY-AUDIT-2026-09-06.md#f1--csrf-sets-the-webhook-the-dashboard-becomes-an-exfil-beacon) | S–M: one middleware rejecting state-changing requests whose `Sec-Fetch-Site` is `cross-site` or whose `Origin` is not the loopback origin. Survives the no-JS rule (plain `<form>` POSTs carry both headers). |
| **2** | **Any website can read the dashboard via DNS rebinding.** No `Host` allow-list; the server answers `Host: attacker.example` with real readings. After a rebind the attacker's page is same-origin with `127.0.0.1:8787` and reads `/api/quota/series`, `/api/export.json`, `/api/budget`, `/api/health`. Live rebind not completed here (needs a controlled domain); the precondition is proven. | A | [A F4](SECURITY-AUDIT-2026-09-06.md#f4--dns-rebinding-a-web-page-can-read-the-dashboard) | S: `Host` allow-list (`127.0.0.1[:port]`, `localhost[:port]`) → 421/400. Same middleware as #1. |
| **3** | **The site's headline privacy claim is false.** `index.html:228` "**0 bytes leave your machine**" and the footer's "**no third-party requests**" on all four pages. Established outbound inventory (A, observed at the egress proxy by B): the cookie goes to claude.ai every poll; by default one GET every 5 min to `status.claude.com` **and** `status.openai.com`; opt-in webhook. The README states this correctly (`README.md:122`); the site does not. | **C** (A establishes what leaves; B establishes the README wording) | [C C1–C2](../../quotalens-web/REVIEW-2026-09-06.md#1-launch-blockers) · [A F9](SECURITY-AUDIT-2026-09-06.md#f9--site-copy-states-outbound-traffic-claims-that-are-false-by-default) · [B §3](AUDIT-2026-09-06.md#3-security-claims-merely-wrong-sense) | S: 6 lines across 5 files, wording drafted in C1/C2 (figure `0` **telemetry**; footer "This site: no analytics, no third-party requests"). |
| **4** | **`/metrics` prints a headroom and burn rate for a session window it has already declared unknown.** Lapsed and per-window-stale cases both observed: `quota_percent{five_hour} NaN` beside `session_headroom_percent 60` / `burn_pts_per_hour 908.571`. The spend gauges have no freshness gate at all. Contradicts README "`/metrics` reports `NaN` … never a stale number somebody would alert on." | B | [B B2](AUDIT-2026-09-06.md#1-launch-blockers) | S: `metrics.py:121-133` — compute `live` once for the session row and gate headroom, burn and spend on it. |
| **5** | **The hero shows "74% left, resets in 2h 50m" from a reading the meter beneath it withheld as stale.** Per-window staleness (the `five_hour` block stops refreshing while neighbours are healthy): meter → withheld, `/api/quota/current` → `pct null, stale true`, hero → a number. The comment at `dashboard.py:524` says the hero reads the meter's view; it reads only the meter's *state*. | B | [B F1](AUDIT-2026-09-06.md#f1--the-hero-shows-74-left-resets-in-2h-50m-from-a-session-reading-the-meter-beneath-it-has-withheld-as-stale--high--s) | S: pass `session_view.withheld` into `_burn_view` as `withheld`. |
| **6** | **`/metrics` computes the weekly budget without the boost exclusion**, so it prints `weekly_windows_remaining 2.5` where the page and `/api/budget` say "Needs 5; 4 so far." `metrics.py:155` calls `compute_budgets` with no `boost_ts`; the word `boost` does not occur in `metrics.py`. This is the "threshold counted before exclusions" failure the prompt named, confined to `/metrics`. | B | [B F3](AUDIT-2026-09-06.md#f3--metrics-computes-the-weekly-budget-without-the-boost-exclusion-so-it-prints-a-number-the-page-refuses--high--s) | S: read boost events in `metrics.collect` and pass `boost_ts`, as `dashboard.py:595` does. |

**The CI gate, which is not a blocker by definition but stops the tag.**
`tests/test_status_and_settings.py::test_every_panel_key_is_actually_rendered_as_a_field`
(added in `4cf923b`, never seen by CI) fails on any Linux host without
`notify-send` and a session bus, because `render.py:1282-1307` omits the
`notify` checkbox when no notifier is available. The `ubuntu-24.04` runner image
has no `libnotify-bin` (B checked the runner-images package list; confidence
high). So both `ubuntu-latest` CI jobs go red **and — this is the part that
matters — `release.yml`'s `build` job runs `python -m pytest -q` against the
wheel on `ubuntu-latest` (`release.yml:74-78`) before uploading the artifact, so
a `v1.0` tag push produces a red release run and no publish.** Owner B,
[B F4](AUDIT-2026-09-06.md#2-findings-ranked-by-how-badly-a-strangers-first-hour-goes).
Effort S — and note it is the shadow of a real bug, [B F5](AUDIT-2026-09-06.md#f5--on-any-host-without-a-notifier-saving-the-settings-panel-silently-turns-notify-off--medium--s):
on a host without a notifier, saving the panel silently writes `notify: false`.
Rendering the checkbox `disabled` with its current value, and having
`apply_form` leave a key alone when the form did not carry it, fixes both.

**The publish sequence, which is a blocker by definition and resolves itself.**
`pipx install quotalens` — the README's first command — cannot work today
(PyPI 404). It is true the minute the tag publishes, which needs the pending
trusted publisher configured on PyPI first (see §2). Owner B,
[B B3](AUDIT-2026-09-06.md#b3--pipx-install-quotalens--the-first-documented-command--cannot-work-until-the-package-is-on-pypi--blocker-by-definition-resolved-by-the-publish-step--effort-).

**Phase-level blockers the controller's definition demotes.** Each phase's own
prompt called these blockers; under the definition above they are not, and
each is a few minutes' work, so they are all *before launch* in §3 anyway:
B B1 (a hostile `spend.exponent` takes every page to 500 — high),
C C3 (`og-image.png` promises per-project attribution — high),
C C4 (install page scrolls sideways on a phone — high),
C C5 (Terms sentence dropped the word "prohibit" — high; C's prompt is right
that this is the one honesty edit that matters most),
C C6 (Windows line understates CI — medium), C C7 (`robots.txt` → missing
sitemap — low).

---

## 2. The gap against `docs/RELEASE-CHECKLIST.md`

**"What CI proves" — fill in on the day.** Commit `c1891b9` (**not the audited
commit** — the 21 local commits have never been through CI), run id
`33981687447`, date `2026-09-05T17:40:21Z`, jobs green **8 of 8**. On the
audited commit `3b7d2ec`, locally against the installed wheel in a clean venv:
wheel builds and carries all nine assets ✓; ruff clean ✓ (0.15.11 vs CI's
0.16.\*); unit tests against the wheel **✗ 1 failed / 533 passed** (the gate
above); `qa/smoke.py` server half ✓; lifecycle half ✓; Windows `service
install` — not checkable here. The checklist's own rule applies: **these ticks
are decoration until CI has run on the commit being tagged.**

**The manual gates, all still unrun, and no agent can close them:**

- **1. `service install` on a clean macOS account** — not run. Needs a fresh
  macOS user, a real cookie, a real Keychain, and an hour.
- **2. The systemd user unit on a clean Linux user** — not run. Needs a desktop
  Linux with a keyring backend; the checklist itself names this as the most
  likely place to fail. (Related and new: B F5 — a settings save from a
  headless instance turns `notify` off.)
- **3. Windows Credential Manager, once** — not run. CI covers the wheel, the
  suite, the smoke test and the logon task on Windows; no machine has read a
  real cookie out of Credential Manager. The README's Windows caveat is
  therefore **still accurate** and must stay (B F8); the *site* understates it
  the other way (C C6).

**"Before tagging":** `docs/QA.md` run end to end — done by phase B with an
observation per box (B §5), five boxes not run and named. Version bumped — yes,
`1.0` in both files (`Development Status :: 3 - Alpha` still in `pyproject.toml`,
B F8). Storage figures against a real database — **not done**, no database
reachable. VISION/README/Terms defensible line by line — **no**: eleven
contradictions in B F8's table, of which VISION.md's "the only outbound requests
are to claude.ai" and the README's "Desktop notifications" under *What this
doesn't do* are the two a checking reader hits first.

**"Publishing to PyPI"** — from the earlier release-path review, still open and
re-verified here: (a) `release.yml:103` lets a manual dispatch with
`target: pypi` publish from any ref while the tag/version agreement step at
`:59-60` is gated on `refs/tags/v*` — so dispatch-to-PyPI has no version check;
(b) `environment: pypi` protects nothing until a required reviewer is added in
GitHub settings; (c) the pending trusted publisher on PyPI must exist before the
first tag or the publish step fails. (a) is a one-line `if:` change; (b) and (c)
are settings, not checkable from here.

**Disclosure surface:** no `SECURITY.md`, no `CONTRIBUTING.md`, advisory link
404s (A F7). A draft `SECURITY.md` is in A §4; it needs a real email address.

---

## 3. Everything else, in one table

Severity: high / medium / low / info. Effort: S / M / L. Timing: **before** /
**after** launch / **won't fix** (with the reason).

| Finding | Phase | Sev | Effort | When |
|---|---|---|---|---|
| A hostile `spend.used.exponent` (e.g. 60) is accepted, stored, and takes `/`, `/api/health`, `/api/quota/current`, `/api/dashboard` to 500 until pruned; guard lives only in `format_money` at render time ([B B1](AUDIT-2026-09-06.md#1-launch-blockers)) | B | high | S | before — one `if` in `_spend_from_spend_block`; hostile-upstream reach, and "honest by construction" is false while it lasts |
| `og-image.png` says "which project is spending it" (first item under *What this doesn't do*) under the retired lens logo; every link preview shows it ([C C3](../../quotalens-web/REVIEW-2026-09-06.md#1-launch-blockers)) | C | high | S | before — regenerate from `brand/mark.svg`; PoC in C's scratch is 40 lines + one screenshot |
| `install.html` scrolls horizontally at 320/390 px; Install and As-a-service rows clipped on a phone ([C C4](../../quotalens-web/REVIEW-2026-09-06.md#1-launch-blockers)) | C | high | S | before — `site.css:315` `minmax(0, 1fr)` |
| Terms row: "under … section 3(7)" drops the README's "prohibit" and "risk" ([C C5](../../quotalens-web/REVIEW-2026-09-06.md#1-launch-blockers)) | C | high | S | before — one sentence; the phase-C rule makes this a blocker and I would treat it as one |
| The hero is a blank box occupying 48% of the first screen; `TODO.md` names a `dashboard.png` that is not in the tree ([C H2](../../quotalens-web/REVIEW-2026-09-06.md#h2--y-275705-the-hero-is-an-empty-box--verified--high--s-now-m-for-the-capture)) | C | high | S now / M capture | before — delete the `<figure>` until the capture lands |
| No response size cap in `client.py`; a hostile upstream fills the disk through the `sample` table ([A F5](SECURITY-AUDIT-2026-09-06.md#f5--no-response-size-cap-on-the-upstream-payload--disk-fill)) | A | medium | S | before — refuse bodies over a few MB |
| `quotalens.db`, `.log`, `.pid`, `.runtime.json` created 0644; `config.json` is 0600 (overturns the prompt) ([A F6](SECURITY-AUDIT-2026-09-06.md#f6--database-log-pid-and-runtime-files-are-created-world-readable)) | A | medium (shared/synced host) | M | after — `0700` dir + `0600` files on POSIX; say Windows relies on the profile ACL |
| Security-advisory link → `amitsharma/quotalens` (404); no `SECURITY.md`; issue template links to `ccusage` ([A F7](SECURITY-AUDIT-2026-09-06.md#f7--private-security-report-link-points-at-the-wrong-repository), B F8) | A | medium | S | before — fix the slug, add A's draft `SECURITY.md`, enable private vulnerability reporting on the repo |
| Manual dispatch to PyPI bypasses the tag/version check (`release.yml:103` vs `:60`); no required reviewer on `environment: pypi` | ctrl (earlier release review, re-verified) | medium | S | before — gate `pypi` on the tag only, or drop `pypi` from the dispatch options |
| Actions pinned by moving tag (`checkout@v4`, `gh-action-pypi-publish@release/v1` in the job holding `id-token: write`) ([A §5](SECURITY-AUDIT-2026-09-06.md#5-residual-risk-accepted-knowingly)) | A | medium | S | after — pin to SHAs |
| No lockfile; four deps with floors only; no dependabot ([A §5](SECURITY-AUDIT-2026-09-06.md#5-residual-risk-accepted-knowingly)) | A | medium | S | after — a lockfile or upper bounds; dependabot is optional if a lockfile exists |
| starlette advisories (PYSEC-2026-161/248/249/2280/2281) on the request path, reachability not adjudicated per advisory ([A §5](SECURITY-AUDIT-2026-09-06.md#5-residual-risk-accepted-knowingly)) | A | medium | M | after — resolve against a lockfile and adjudicate; `python-multipart` ones are verified unreachable |
| Saving the settings panel on a host without a notifier silently writes `notify: false` ([B F5](AUDIT-2026-09-06.md#f5--on-any-host-without-a-notifier-saving-the-settings-panel-silently-turns-notify-off--medium--s)) | B | medium | S | before — same fix as the CI gate |
| `quotalens stop` SIGTERM/SIGKILLs whatever pid the pid file names, no identity check; pid reuse after reboot is the realistic path ([B F6](AUDIT-2026-09-06.md#f6--quotalens-stop-will-sigterm-then-sigkill-whatever-process-the-pid-file-names--medium--s)) | B | medium | S | after — record start time or require `/api/health` before signalling |
| Negative `utilization` served as `-5%` / headroom `105` ([B F7](AUDIT-2026-09-06.md#f7--a-negative-utilization-is-presented-as-a-current-percentage--medium--s)) | B | medium | S | after — reject `< 0` in `_pct_of` (values > 100 are the vendor's number; leave them) |
| README "lapse rule lives in `compute_runway`" is false — it is duplicated in `window_has_lapsed`, and the webhook's `headroom_pct` bypasses both ([B F2](AUDIT-2026-09-06.md#2-findings-ranked-by-how-badly-a-strangers-first-hour-goes)) | B | medium | S | before (README sentence) / after (gate the webhook headroom) |
| Eleven docs-vs-code contradictions: README "Desktop notifications" under *What this doesn't do*; README "only outbound … vendor status" omits the webhook; README "six jobs" (eight); VISION.md "only outbound requests are to claude.ai", "container image", "reads Claude Code's local logs", "leads with burn rate"; `3 - Alpha`; MVP-SCOPE (declared history) ([B F8](AUDIT-2026-09-06.md#f8--readme-what-this-doesnt-do-and-other-documents-contradict-shipped-features--medium-docs--s-each)) | B | medium | S each | before — README and `pyproject.toml` lines; VISION.md can carry a dated "superseded by" note instead of a rewrite |
| Site: Windows "written for but untested" understates CI ([C C6](../../quotalens-web/REVIEW-2026-09-06.md#1-launch-blockers)) | C (fact from B F8) | medium | S | before — "each in CI on every push; the Windows keychain path is not yet tested by hand" |
| Site: cost and author are inferred, not read; "free" appears nowhere ([C H3](../../quotalens-web/REVIEW-2026-09-06.md#h3--y-265322-cost-and-author-are-inferred-not-read--verified--medium--s)) | C | medium | S | before — one `<small>` line |
| Site: `--txt-far` used as text fails AA (2.89–3.5:1) on the only above-the-fold "Claude" and on the cookie ask itself ([C H4, H5, H8](../../quotalens-web/REVIEW-2026-09-06.md#h4--y-322-the-only-above-the-fold-claude-is-the-lowest-contrast-text-on-the-page--verified--medium--s)) | C | medium | S | before — three CSS tokens → `--txt-dim` |
| Site: "how do I verify that" never arrives beside the cookie claim ([C H5](../../quotalens-web/REVIEW-2026-09-06.md#h5--a2-the-cookie-the-home-pages-silence-is-right-how-to-use-answers-in-time--verified--info-with-two-s-fixes)) | C | medium | S | before — link to README#security-note + `quotalens config list` |
| Site: "Your account data stays yours" reads as a security promise that A F1–F4 currently break ([C H6](../../quotalens-web/REVIEW-2026-09-06.md#h6--y-2290-loopback-only-is-offered-as-a-promise-not-a-reachability-fact--conditional--medium--s)) | C (cites A) | medium | S | conditional — no change if blockers 1–2 ship first; else "stays on your disk" |
| Site: "Claude" removed from `<title>`/H1/description by `ad9e605`; the one word every query contains ([C S3](../../quotalens-web/REVIEW-2026-09-06.md#fix-before-launch)) | C | medium | S | before — the pre-`ad9e605` title |
| Site: `robots.txt` → missing `sitemap.xml` ([C C7/S1](../../quotalens-web/REVIEW-2026-09-06.md#fix-before-launch)) | C | low | S | before — 7-line file, text supplied |
| Site: JSON-LD ships inline under `script-src 'self'` — the CSP "trap" in the prompt is not there (verified in Chromium; Firefox/Safari on deploy day) ([C S2](../../quotalens-web/REVIEW-2026-09-06.md#fix-before-launch)) | C | medium | S | after — block drafted |
| Site: canonical extensionless, links `.html` → one 308 per click on Pages ([C S4](../../quotalens-web/REVIEW-2026-09-06.md#fix-before-launch)) | C | medium | S–M | after — pick one; C recommends extensionless |
| Site: `pages.dev` host indexable ([C S5](../../quotalens-web/REVIEW-2026-09-06.md#fix-before-launch)) | C | medium | S | after — `X-Robots-Tag: noindex` in `_headers` |
| Site: no question-shaped heading anywhere; FAQ row drafted for *How to use* with README anchors ([C S9](../../quotalens-web/REVIEW-2026-09-06.md#fix-after-launch)) | C | medium | M | after |
| Site: GEO — GitHub is the citable surface; every site claim should link to where it is checkable ([C S11](../../quotalens-web/REVIEW-2026-09-06.md#fix-after-launch)) | C | medium | M | after |
| Site: print in dark theme → 3.4:1 body text, `.cmd` clipped ([C H8](../../quotalens-web/REVIEW-2026-09-06.md#h8--mechanics-a5--each-a-yesno-with-evidence)) | C | low | S | after — 3-line `@media print` |
| Site: `COPY.md` drift (supporting row 8→5 items; screenshot caption line); `TODO.md` names a file not in the tree ([C H8, H10](../../quotalens-web/REVIEW-2026-09-06.md#h10--stale-notes-that-will-confuse-the-next-editor--verified--low--s)) | C | low | S | after |
| Site: "sign out … which invalidates it" is ahead of the README and unverifiable here ([C H9](../../quotalens-web/REVIEW-2026-09-06.md#h9--feedback-y-530-the-site-is-ahead-of-the-readme-once--not-checkable-here--low)) | C | low | S | before — drop "which invalidates it" until confirmed |
| Site: two-sentence rule broken in three blocks; feedback list runs to 165 ch at 1440; SVG labels 7.2 px at 320; H1→H3 hierarchy; Safari favicon fallback ([C H8, S7, S12](../../quotalens-web/REVIEW-2026-09-06.md#h8--mechanics-a5--each-a-yesno-with-evidence)) | C | low | S | after |
| Maintainer's real name and home paths in `design/prompts/*.md` at the tip ([A F8](SECURITY-AUDIT-2026-09-06.md#f8--maintainers-real-name-and-home-directory-paths-committed-in-designprompts)) | A | low | S | before, if `design/prompts/` ships publicly; otherwise won't fix — the name is on the Contact page anyway |
| A newer-schema database is opened and written without a check ([B F9](AUDIT-2026-09-06.md#f9--a-database-from-a-newer-schema-is-opened-and-written-without-a-check--low--s)) | B | low | S | after |
| `--db` / `QUOTALENS_DB` pointing two profiles at one file: interleaves silently, survives the rebuild ([B F10](AUDIT-2026-09-06.md#f10--two-collectors-on-one-database-remain-one-flag-away--low-documented--)) | B | low | — | won't fix — documented behaviour, and the rebuild now survives it; a startup advisory is optional |
| Generic-fallback parse leaves `unknown:*` gauges for three intervals ([B F12](AUDIT-2026-09-06.md#f12--the-generic-fallback-parse-leaves-eight-unknown-windows-in-apiquotacurrent-and-metrics-until-they-age-out--info)) | B | info | — | won't fix — consistent with "degrades gracefully" and the dashboard flags them |
| Fractional CLI thresholds collide in the de-dup key (`{threshold:.0f}`); event rows from the pre-`3b7d2ec` build parse to `set()` (earlier review, still true) | ctrl | info | S | won't fix — unreachable from the UI; zero notify events in any real database seen |
| Storage README figures 2.0 KB / 2.8 MB/day / 39 MB are arithmetic on one measured datum, contrary to "measured, not arithmetic" ([B §1](AUDIT-2026-09-06.md#1-storage-figures-harness-database-downgraded-not-a-real-payload)) | B | low | S | before — either re-measure against your real DB (one SQL line, §5) or change the README's verb |
| Notifications arm from the next poll; levels already passed in this window never fire, and nothing says so (earlier review) | ctrl | low | S | after — one line under `Delivery` |

Won't-fix items explicitly **not** carried: "the dashboard has no
authentication" (documented design; A §5), "uses SQLite / uses subprocess", any
dependency CVE without a call path (A dropped `pypdf`, `soupsieve`,
`setuptools`, `wheel`, `urllib3`, `python-multipart` on that rule).

---

## 4. Verified safe — what you can cite

From A §3, each with the evidence in the report: `base_url` is environment-only
on every path (`load_settings`, `with_overrides`, `replace`, the panel) — a
`POST /settings` carrying `base_url=http://evil.example` changes nothing, so
**the cookie cannot be redirected from the browser**; the cookie lives only in
the OS keyring, never in the DB, `config.json` or a log, and the redactor
rewrites `exc.args` so `repr(exc)` is scrubbed too; TLS verification is on in
all three outbound paths; every `subprocess` is a fixed argv; the AppleScript
and PowerShell escapes hold against newlines, backticks, `$()`, unicode quotes
and are pinned by tests; `/favicon.svg` interpolates only a number; `/static`
is allow-listed; `release.yml` is not fork-reachable to publish; **git history
holds no credential, org id, database, log or probe output — ever**. From B:
loopback bind has no override path; `status_row false` stops the requests
(observed at the proxy, not inferred); the wheel is complete and every
quick-start command explains itself on a clean install with no stack trace;
the session-start inference detects, records and says so on the page; the
boost exclusion and the five-window threshold are correct on the page and
`/api/budget`; `auth`/`probe`/`start`/`status` all say "keyring" in the three
keyring failure modes.

---

## 5. What was not checked, and what would settle each

This is what you accept by tagging.

| Not checked | Why | What settles it |
|---|---|---|
| The README's 2.0 KB payload, 2.8 MB/day, 39 MB figures | no real database reachable | your `quotalens.db`: `SELECT AVG(LENGTH(payload)), COUNT(*) FROM sample WHERE source='usage'` plus the file size |
| CI on `3b7d2ec` — and the ubuntu prediction | never run; reasoned from the runner-image package list | push, or `workflow_dispatch`, and read the two ubuntu jobs |
| Live DNS rebind to completion | needs a controlled authoritative domain with a short TTL | any rebinding test domain; the missing `Host` check is already proven |
| Keyring cross-application read and OS prompt behaviour, per OS | headless Linux, no backend | one desktop each: macOS Keychain, Windows Credential Manager, Linux Secret Service |
| Per-advisory starlette reachability | no lockfile to resolve against | lockfile + `pip-audit`, then read each advisory against the request path |
| 100 MB payload to disk exhaustion | destructive; code-read only | a bounded tmpfs and a fake upstream |
| Read-only data dir; disk full mid-`VACUUM`; clock stepping backwards | root user; no bounded FS; cannot step the container clock | unprivileged user + `chmod 555`; 8 MB tmpfs; VM snapshot restore or `faketime` |
| Chart boost mark / rocket, "future region" box, `restart`, port-held-by-`serve` message, Fable "none left", retention panel sizes on a > 2-day DB, live webhook body | budget / instance too young | a `qa_browser.py` rerun against an instance older than 15 min; a real crossing for the webhook |
| The three manual gates (macOS `service install`, Linux user unit, Windows Credential Manager) | no such machine | RELEASE-CHECKLIST 1–3, unchanged; **no agent can close them** |
| `pipx install git+https://github.com/amitshcc/quotalens` from a clean machine | GitHub gated from the workspace | one command from any machine with network |
| PyPI pending publisher; `environment: pypi` required reviewer | account settings | the PyPI and GitHub settings pages |
| The live site: DNS, `_headers` on Pages, `.html` → 308, sitemap 200, `pages.dev` noindex, every external link, JSON-LD in Firefox/Safari, link-preview caches, PageSpeed, print in three browsers, Safari favicon, a real screen-reader listen | site not live from here | C §4's 15-item deploy-day list |
| Whether claude.ai's sign-out-everywhere invalidates a pasted cookie | needs a real account | one test with a throwaway session |
| `light-dark()` fallback on browsers older than Chrome 123 / Safari 17.5 / Firefox 120 | no old browser here | BrowserStack or an old machine; the fallback is readable, unstyled |

---

## 6. Phase reports, and where the ownership rule moved a finding

- [Phase A — security](SECURITY-AUDIT-2026-09-06.md): 5 phase-blockers, 3 medium, 1 low; draft `SECURITY.md`; residual risk.
- [Phase B — correctness](AUDIT-2026-09-06.md): 3 phase-blockers, 3 high, 4 medium, 2 low, 2 info; `QA.md` observations per box; checklist ticks.
- [Phase C — the site](../../quotalens-web/REVIEW-2026-09-06.md): 7 phase-blockers, 2 high, 9 medium, 8 low, 4 info; 23-item patch list; 15-item deploy-day list.

Where §3 of the controller moved a finding: **"0 bytes leave your machine"** —
A established the outbound inventory (F9), B established the README's wording
and observed the requests at the proxy (§3, F8), **C owns the finding** (C1/C2)
and cites both. **The Windows line** — B established the fact (F8), C owns the
site wording (C6). **The cookie** — A owns whether it can leak (verified safe),
C owns whether the site tells a stranger in time (H5: it does, at 57 words).
**"Loopback only" as a promise** — C H6 cites A F1–F4 rather than re-deriving.
**The `amitsharma/quotalens` link** — A owns it as disclosure surface (F7), B
lists it in the contradictions table and cites A; the 404 was settled by the
controller from the Mac. **`QUOTALENS_BASE_URL`** — B pointed at A; A verified
it environment-only and unreachable from the browser. **The exponent 500** — B
owns it as a false "honest by construction" claim even though the adversary is
A's hostile upstream; A's hostile-upstream section found the escaping safe and
did not reach this, so it stays with B. **The CI gate** — B owns it; the
controller added the `release.yml` consequence.

---

## Appendix — the shortest path to green (also given in chat)

Ordered; **(tonight)** marks what needs no machine you do not have.

1. **(tonight)** Fix the CI gate and B F5 together: render the `notify`
   checkbox `disabled` with its current value when no notifier is available,
   and make `apply_form` leave a key alone when the form did not carry it.
   Run the suite with `DISPLAY` unset to prove it.
2. **(tonight)** One middleware in `api.py`: reject any non-GET whose
   `Sec-Fetch-Site` is `cross-site`/`same-site`-from-another-origin or whose
   `Origin`, when present, is not `http://127.0.0.1:<port>` /
   `http://localhost:<port>`; and 421 any request whose `Host` is not one of
   those. Closes blockers 1 and 2. Re-run A's two `curl` lines from §0 of this
   report to prove it.
3. **(tonight)** `metrics.py`: gate `session_headroom_percent`,
   `burn_pts_per_hour` and the two spend gauges on the same `live` test as
   `quota_percent`; pass `boost_ts` into `compute_budgets`. Closes 4 and 6.
4. **(tonight)** `dashboard.py:527-543`: pass `session_view.withheld` into
   `_burn_view` as the withheld flag. Closes 5.
5. **(tonight)** `parse.py`: validate the exponent in `_spend_from_spend_block`
   (B B1) — one `if`, while you are in the file.
6. **(tonight, words only)** README: move Desktop notifications out of *What
   this doesn't do*; "the only outbound traffic … the vendor status row" →
   add "and a webhook if you set one"; "six jobs" → eight; "lives in
   `compute_runway`" → "in `window_has_lapsed` and `compute_runway`";
   `pyproject.toml` `3 - Alpha` → `5 - Production/Stable` or `4 - Beta`;
   `.github/ISSUE_TEMPLATE/config.yml` `amitsharma` → `amitshcc` and drop the
   `ccusage` link; add A's `SECURITY.md` with a real address; either
   re-measure the three storage figures against your DB or change "measured"
   to "estimated from one measured payload".
7. **(tonight, words only)** Site: C's patches 1–5 — `minmax(0, 1fr)`; "0
   **telemetry**" + the two-sentence body + diagram label + aria-label; "This
   site: no analytics…"; the Terms sentence with "prohibit" and "risk"; the
   Windows line; `sitemap.xml`. Then patches 6–9 if you have twenty more
   minutes (contrast tokens, "Claude" back in the title, the `<small>` with
   "free", delete the empty hero figure).
8. **(tonight)** `release.yml:103`: drop `|| (workflow_dispatch && target ==
   'pypi')` or make the version-agreement step run on dispatch too.
9. **(settings, ten minutes)** PyPI: add the pending trusted publisher
   (`amitshcc/quotalens`, `release.yml`, environment `pypi`). GitHub: required
   reviewer on `environment: pypi`; enable private vulnerability reporting so
   the advisory link resolves.
10. **Push. Watch all 8 jobs on the pushed commit.** Fill the checklist blanks
    from that run, not from `33981687447`.
11. Regenerate `og-image.png` (C's PoC) and capture the hero from a running
    instance — needs your Mac, not an agent.
12. **Tag.** Then the deploy-day list (C §4) and, when you have the machines,
    the three manual gates.

Everything in 1–8 is one evening. Nothing in 1–8 needs a machine you do not
have. What you accept by tagging after 1–10 is exactly §5.
