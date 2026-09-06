# v1.0 release checklist

Two kinds of item: things CI proves, and things a person has to do on a machine
CI does not have. The second kind is the point of this file.

**Retitled from v0.1.0 on 2026-09-06.** That version was never tagged; the first
public tag is `v1.0`. This is a live checklist for a release about to happen, so
it is corrected rather than annotated — see the historical planning documents for
the note explaining the rename.

## What CI proves

`.github/workflows/ci.yml` on `ubuntu-latest`, `macos-latest` and
`windows-latest`, Python 3.11 and 3.13.

> **Ticks cleared 2026-09-06.** They read "all seven jobs green as of `e979b1b`
> (run 33795392935, 4 Sep 2026)". Since that run: the boost work, the `Provider`
> seam, the footer and version change, the CSS ceiling, the config store,
> retention, notifications, the vendor status row, the settings panel and the
> boost tooltip. The job count is now **eight**. A checklist citing a two-week-old
> run is decoration, so these are unchecked until CI has actually run on the
> commit being tagged.
>
> Filled in on the day: commit `969eb6d`, run id `34056217434`
> (https://github.com/amitshcc/quotalens/actions/runs/34056217434), date
> `2026-09-06T19:52:04Z`, jobs green `8 of 8`.

- [x] the wheel builds, installs, and carries the stylesheet, the scripts and
      the favicon (`quotalens.web`)
- [x] `ruff check` and `ruff format --check` clean
- [x] the unit tests pass against the **installed wheel**, not the source tree
- [x] `qa/smoke.py`: a real server polls a fake claude.ai, writes rows and reads
      them back through the API, `/metrics` and an export
- [x] `qa/smoke.py`: `start`, a refused double start, `logs`, `status`, `stop`
      and a stale pid file
- [x] on Windows, `service install` registers the logon task, `service status`
      reads it back, and `service uninstall` removes it

**What CI does not prove: the OS keyring.** Storing a cookie in CI would need
either an environment-variable credential path (a security regression) or an
extra package. The smoke test substitutes the in-memory secret store the unit
tests already use, so everything except the keyring backend is exercised on all
three platforms. So the README claims the wheel installs and the app runs on all
three, and says in the same breath that the credential path is not covered
anywhere. Item 3 below is what would close that on Windows.

## What a person has to do

### 1. `service install` on a clean macOS account

The failure this guards against is the worst bug report this project can
generate: a background agent that silently reads no data.

- [ ] On a fresh macOS user account, `pipx install quotalens`
- [ ] `quotalens auth`, paste a cookie, confirm it verifies
- [ ] `quotalens service install`
- [ ] Note every path and command it printed
- [ ] Log out and back in
- [ ] **Wait an hour**, then `quotalens status`
- [ ] Confirm: `collector: ok`, `polls_ok` above 50, and **no keychain prompt
      appeared at any point**
- [ ] `curl localhost:8787/api/quota/series?hours=1 | grep -c ts` shows roughly
      60 readings
- [ ] `quotalens service uninstall` removes what it wrote

### 2. The systemd user unit on a clean Linux user

- [ ] Same, with `quotalens service install` writing
      `~/.config/systemd/user/quotalens.service`
- [ ] Confirm the printed `loginctl enable-linger` guidance is accurate for the
      distribution
- [ ] `systemctl --user status quotalens` after an hour
- [ ] Confirm the keyring works headless, or note which backend was needed
      (`gnome-keyring`, `kwallet`, `keyctl`) — this is the most likely place for
      it to fail

### If either fails

**Cut `service install` to printing.** Have it write nothing and instead print
the unit file and the exact command to install it, and let the user run that.
`start` / `stop` / `status` / `logs` on the pid file are one code path and stay
either way. A background service that silently produces no data is worse than no
service command at all.

### 3. Windows, once

CI now builds, installs, tests and smoke-runs on Windows, and registers and
removes the logon task there. What no machine has done is read a real cookie out
of the Windows Credential Manager.

- [ ] `pipx install quotalens`, `quotalens auth` — does the Windows Credential
      Manager backend work through `keyring`?
- [ ] `quotalens start`, `status`, `logs`, `stop`
- [ ] The dashboard renders in a browser
- [ ] `quotalens service install`, log out and back in, and confirm after an
      hour that it collected without a credential prompt
- [ ] If all of that holds, drop the credential caveat from the README's
      platform section. Not before.

## Before tagging

- [ ] `docs/QA.md` run end to end by the QA agent, with a report
- [ ] Version bumped in `pyproject.toml` and `src/quotalens/__init__.py`
- [ ] The README's measured storage figures still match a real database
- [ ] `VISION.md`, the README and the Terms section each say something
      defensible line by line to a reader who goes and checks the source

## Verified locally on 2026-09-06, which is not the same as CI

Run against the **installed wheel** in a clean venv, on macOS only, at commit
`e947590` with ten commits unpushed. This is evidence, not a substitute for the
matrix above: it covers one OS and one Python, and CI covers six combinations.

- [x] `uv build` produces `quotalens-1.0-py3-none-any.whl`; the wheel carries
      `app.css`, `tokens.css`, `app.js`, `chart.js`, `favicon.svg` and
      `boost-rocket.svg` under `quotalens/web/`
- [x] the full suite passes against the installed wheel, not the source tree
- [x] `ruff check` and `ruff format --check` clean
- [x] `qa/smoke.py` green against the wheel with **no `config.json` present** —
      the clean-machine path the config store introduced
- [x] a fresh empty database is written `retention: 3months`; a database with
      history is written `1year` and prunes nothing on first run
- [x] `quotalens --version` reports `1.0`, agreeing with `pyproject.toml`

Not covered here and still open: every non-macOS platform, and all three manual
gates below.

## Publishing to PyPI, when the time comes

Not done, and deliberately the owner's action. What it involves:

1. **Name availability.** `quotalens` must be free on PyPI. Check
   `https://pypi.org/project/quotalens/` before anything else; a taken name is
   cheaper to discover now than after a tag.
2. **Trusted publishing, not an API token.** On PyPI, add a trusted publisher
   for the GitHub repository, workflow filename and environment name. The
   workflow then mints a short-lived credential per run through OIDC and no
   long-lived secret is ever stored in the repository. A token in
   `secrets.PYPI_API_TOKEN` also works and is worse: it does not expire, and it
   is one leaked log line from being someone else's.
3. **A release workflow** triggered on a tag, which builds the wheel and sdist
   and uploads with `pypa/gh-action-pypi-publish`. Test it against TestPyPI
   first: the upload is irreversible, and a filename can never be reused even
   after deletion.
4. **What a stranger's `pipx install quotalens` then does:** resolves the wheel,
   creates an isolated venv, installs four runtime dependencies (`fastapi`,
   `uvicorn`, `curl_cffi`, `keyring`) and puts `quotalens` on PATH. First run
   writes nothing until `quotalens auth`; the database and `config.json` appear
   in the per-OS data directory on the first `serve`.
