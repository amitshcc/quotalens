# v0.1.0 release checklist

Two kinds of item: things CI proves, and things a person has to do on a machine
CI does not have. The second kind is the point of this file.

## What CI proves

`.github/workflows/ci.yml` on `ubuntu-latest`, `macos-latest` and
`windows-latest`, Python 3.11 and 3.13:

All seven jobs green as of `e979b1b` (run 33795392935, 4 Sep 2026):

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
