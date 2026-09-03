# Claude Code prompt — repo split, the four decisions, and the tag

Paste everything below the line into Claude Code in the QuotaLens repo.

One name to confirm before you paste: the prompt assumes the public repo is
**`amitshcc/quotalens`**. Change it in two places below if you want a different
one — it goes in the README, the PyPI metadata and every link on the site, so
it is expensive to change later.

---

v0.1.0 is built and the tree is clean. This session does four things: split the
repo, get CI actually green on GitHub, settle the four decisions from
`docs/V0.1.0-PLAN.md`, and tag. Read `docs/V0.1.0-PLAN.md` and
`docs/RELEASE-CHECKLIST.md` first.

Same ground rules as before: no new runtime dependencies, loopback only, the
cookie never leaves the keyring, small commits, `ruff` clean, tests alongside
the code. Batch your questions at stage boundaries. **If I am not at the
keyboard when you need a decision, do what you did last time — write it down,
pick the reversible option, keep it to one small commit, and tell me at the
end.** That was the right call.

## Stage 1 — restructure the folder

Right now the connected folder *is* the repo. I want it to be a container with
two repos inside it:

```
QuotaLens/                 # local folder, not a repo
  quotalens/               # this repo, public, github.com/amitshcc/quotalens
  quotalens-web/           # the marketing site, private, Cloudflare Pages
```

Move everything — including `.git` — into `quotalens/`, leaving the parent
folder empty of tracked files. Do not initialise `quotalens-web/`; create the
empty directory and leave it alone, it gets its own session.

Get this right, in this order, and verify each step before the next:

1. Confirm the tree is clean and note the current `HEAD` hash so we can prove
   nothing was lost.
2. Move the contents, `.git` included, with a single move rather than a copy —
   a copy plus delete will lose file modes and confuse the running server.
3. Stop the running instance on 8787 first. Its pid file, log and database live
   in the OS data directory rather than the repo, so the move should not
   disturb them, but confirm that rather than assuming it — if any of the three
   resolve to a path inside the repo, say so and stop, because that is a bug
   worth fixing before release.
4. Verify: `git log` shows the same `HEAD`, `git status` is clean, the venv
   still resolves (recreate it if the absolute paths broke), `pytest` passes,
   and `quotalens start` comes back healthy on 8787.
5. Move `docs/prompts/02-web-site.md` out to the parent folder for now — it is
   the brief for the other repo and does not belong in this one's history.

## Stage 2 — the four decisions

Two of them change.

**Decision 2, profile ports: keep the derivation, fix the failure mode.**
`8788 + crc32(name) % 100` is fine as a default but it is unguessable and it can
collide. So: on bind failure, exit with a message that names the port, the
profile and `--port` as the fix — not a traceback. And have `start`, `status`
and `serve` print the port they landed on, every time, so nobody has to compute
crc32 to find their own dashboard. A deterministic port the user cannot predict
is only acceptable if the tool tells them the answer.

**Decision 4, webhook body: add the profile name.** A local label I chose is not
an account identifier, and without it a receiver watching two profiles can only
tell them apart by port number. Add `profile` to the JSON body and document it.

**Decisions 1 and 3 stand.** No `keyrings.alt` in CI — it would test a fake
backend on three runners and tell us nothing about Keychain, Credential Manager
or SecretService, which is worse than an honest gap. And 20,000 samples stays,
because the README already draws the line correctly: readings are permanent,
raw payloads are debugging material and bounded.

But decision 1 leaves a real hole, so close it separately: **headless Linux has
no SecretService without a D-Bus session**, and `systemd --user` is one of the
two ways we tell people to run this. Find out what actually happens on a
headless Linux box when `keyring` has no backend — does `quotalens auth` fail
with something a user can act on, or does it fail obscurely at the first poll,
six hours after they walked away? Whatever it does, the error must name the
problem and the fix, and the README must say plainly that a Linux server
without a session keyring is not currently supported. If that turns out to need
a credential path we do not have, do not invent one in this session — write it
up as the first post-1.0 issue.

## Stage 3 — CI, for real

There is no git remote. Everything about "cross-platform" is still a claim.

1. Create `amitshcc/quotalens` as a **public** repo and push. Check the history
   for anything that should not be public before the first push — a cookie
   value, an org id, a real database, a `TEMP/` scratch file, the `.venv`. Once
   it is pushed it is public forever, so this check happens before, not after.
2. Watch the three runners. Fix what fails. Expect Windows to break on paths,
   the pid file and signals; that is the whole point of running it.
3. When all three are green on both Python versions, **then** change the README
   from "Windows untested" to what the evidence supports — and be precise about
   what CI proves: it proves the wheel installs and the app runs, not that the
   Windows Credential Manager path works, because the smoke test uses the
   in-memory store.
4. Add the badge. A badge that has never been red is decoration; this one will
   have been earned.

## Stage 4 — the last small things

- **The Fable label.** My Weekly — Fable meter reads 100% and shows critical.
  Per `FEATURE-REVIEW.md` §2.6 a Fable limit at 100% means half the weekly pool
  is used, not an exhausted account, so the dashboard is currently telling me
  something false. The review said label rather than code and that is still
  right: label the meter so 100% reads as "the Fable half of the weekly pool",
  and make sure it does not drive the critical state for the account as a whole.
- **The contaminated windows.** Give me the removal SQL again as a one-liner I
  can run, and a `quotalens` subcommand or documented recipe for the general
  case, because the bug that produced them shipped and other people's databases
  will have the same rows.
- **`docs/prompts/`** — keep this folder, it is how this project has been built.

## Stage 5 — tag

Only when three runners are green and I have run the `service install` gates in
`docs/RELEASE-CHECKLIST.md` on a clean macOS account and a clean Linux user.
Ask me for those results; do not tag on my behalf without them.

Then: `v0.1.0` tag, a GitHub release with notes written for a stranger rather
than for me, and tell me exactly what the PyPI publish would involve — name
availability, the trusted-publisher setup, and what the first `pipx install
quotalens` from a clean machine actually does. Do not publish. That is my
button to press.

Start by reading the plan and the checklist, then give me the restructure steps
you intend to run before you run them.
