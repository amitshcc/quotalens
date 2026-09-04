# QuotaLens QA checklist

What the unit tests cannot see: the app running, in a browser, driven by a
person or the QA agent. Run all of it before calling a change done. Add a line
whenever a new class of bug appears; never remove one.

## Setup

```sh
mkdir -p /tmp/qa
# a copy of the live database, WAL included (a plain cp loses recent rows)
python -c "import sqlite3, pathlib; src = pathlib.Path.home() / 'Library/Application Support/quotalens/quotalens.db'; s = sqlite3.connect(src); d = sqlite3.connect('/tmp/qa/copy.db'); s.backup(d)"
# a stand-in upstream you can switch between states
python qa/fake_claude.py 8799 &
# an instance that never touches your real data: --data-dir also moves the
# database there unless QUOTALENS_DB or --db says otherwise
QUOTALENS_DB=/tmp/qa/copy.db QUOTALENS_BASE_URL=http://127.0.0.1:8799 \
  quotalens --data-dir /tmp/qa start --port 8790 --interval 30
```

Open `http://127.0.0.1:8790/`. Every item below is an observation, not an
inference from source: write down what was on screen or in the DOM.

## Controls

- [ ] With JavaScript disabled (DevTools > Settings > Debugger > Disable
      JavaScript, or `curl` the URLs): range select plus its go button, lookback
      links, auto-refresh links, the poll now form, series label links, history
      sort links and row links all change the URL and the page.
- [ ] With JavaScript enabled: the same controls swap the page in place, the URL
      updates, back and forward work.
- [ ] Hover over the chart: a crosshair and a readout with every visible series
      and a timestamp; the readout does not jitter.
- [ ] Drag on the chart: a selection rectangle, then the URL carries
      `range=<from>-<to>` and the select reads "custom: HH:MM to HH:MM".
- [ ] Double-click the chart: back to auto.
- [ ] Click a series end label: the series hides, the label is struck through,
      `hide=` appears in the URL; click again to restore.
- [ ] Poll now: the button reads "poll in Ns", counts down, re-enables; "polled
      Ns ago" resets to a small number.
- [ ] Auto-refresh set to 10s: the polled-ago counter resets on schedule; set to
      off: it does not.

## Looks

- [ ] Both themes via the theme button; the choice survives a reload.
- [ ] Viewport 900px tall: header, hero, all meters and the chart visible
      without scrolling.
- [ ] Amber appears only on the session window: hero figure, session meter,
      `--s1` trace, elevated chip. Nowhere in the chrome.

## Service lifecycle (use a scratch `--data-dir`)

- [ ] `start` returns at once and prints pid, pid file, log, dashboard URL;
      `status` exits 0 and shows the session line; `logs -n 5` shows real lines,
      none duplicated.
- [ ] `start` again: refused, exit 1, names the pid.
- [ ] Write a dead pid into the pid file: `status` cleans it; `start` proceeds.
- [ ] Hold the port with a foreground `serve`, then `start`: refused, naming
      the pid (serve owns the pid file). Hold it with any other process:
      refused with the bind error in the log tail.
- [ ] `stop`: exits 0; `status` exits 1 and says not running. `restart` comes
      back on the same port and interval without flags.
- [ ] `status` with no `--port` reports the instance in that data directory,
      never the default port's.

## Epistemic states, each rendered and distinguishable at a glance

- [ ] Stale: `POST /mode/down` on the fake upstream, wait three poll intervals:
      em dashes, hatched bars, dashed "stale" chip, a message naming the last
      good sample.
- [ ] Auth failed: `POST /mode/401`: purple left rule, key glyph, "Cookie expired
      or rejected", em dashes.
- [ ] Unverified: `POST /mode/drift`: stale treatment with the "could not be
      parsed" wording, never the cookie wording.
- [ ] Link lost: stop the server with the page open; after two refresh failures
      the header says "dashboard unreachable since", values are em dashes.
- [ ] Recovery: `POST /mode/ok` (or start the server): numbers return.

## The three kinds of nothing on the chart

- [ ] Collection gap: hatched span, counted as "Not collected".
- [ ] No session active: flat `--grid` span labelled "no session", counted as
      "No session". Must not look like the gap.
- [ ] Future region (auto range): no trace and no hatching, but the horizontal
      rules and the hourly `--grid` separators both continue to the right edge of
      the plot, plus a "now" marker and the projection line. Must not look like
      either of the above. (Until 4 Sep 2026 the rules stopped at "now"; the chart
      read as cropped in a region the pointer still reads values out of.)

## The weekly budget

- [ ] Under the meters, never in the hero. The hero stays the session window.
- [ ] "Windows of budget" beside "windows of clock", both in five-hour units.
- [ ] Cost per full window shows a median and a range, not one number.
- [ ] A history below five usable windows shows an em dash, and the row's title
      says how many it has. It never shows a number computed from two windows.
- [ ] A spent sub-cap (Fable at 100%) reads "none left" without needing a cost
      estimate, and the footnote says the parent's headroom cannot be spent on it.
- [ ] `/api/budget` and the page agree; `/metrics` carries NaN, never 0, where the
      page shows an em dash.

## Data shapes

- [ ] Cold database: under 5 minutes of data the hero says collecting and no
      alert fires; under 15 minutes the chart says "Collecting: Nm of data"
      instead of a grid (the hero may already show a rate).
- [ ] A real reset boundary inside the selected range (24h on live data): the
      session trace breaks cleanly, the meter foot says "+N pts since the reset", a
      session-start rule appears on the chart.
- [ ] `POST /mode/reset` on the fake upstream: within a poll the hero shows a
      new window, the history gains a row, the old row closes.

## Add here when something new bites

- 2026-09-03: hover never appeared (SVG elements have no `.hidden` property).
- 2026-09-03: `start` spawned the child without `--data-dir`.
- 2026-09-03: log lines written twice when a log file is set.
- 2026-09-03: zombie children counted as alive by `pid_alive`.
- 2026-09-03: an hour starting at exactly now rendered as no-data, not future.
- 2026-09-03: `status` without `--port` printed the scratch pid, then port 8787's
      health and session (the port comes from the flag default, not the pid file).
- 2026-09-03: hero figure said 79.5% left while the verdict sentence said 80%
      (the same number rounded twice, differently).
- 2026-09-03: the critical headroom figure rendered inside a chip box because a
      state modifier shared the chip class name `crit`.
- 2026-09-03: `--data-dir` does not move the database. A scratch instance started
      without `QUOTALENS_DB` opened the real `~/Library/Application Support/quotalens/quotalens.db`
      and wrote fake-upstream samples into it; always export `QUOTALENS_DB` in QA.
- 2026-09-03: samples whose `resets_at` alternates between values (two upstreams
      interleaved, or a flapping API) made `sessions.rebuild` raise
      `IntegrityError: UNIQUE constraint failed: session_window.started_at`; every
      `start` then died at lifespan and the poller of the live instance failed too.
- 2026-09-03: `status` with no `--port` and no `quotalens.runtime.json` (instance
      never started) fell through to port 8787 and printed "running (pid unknown,
      no pid file)" with that instance's session, exit 0.
- 2026-09-03: the "partial, N% observed" badge divides by the viewing instance's
      `--interval`, not the cadence the samples were collected at: a 60s database
      viewed at `--interval 30` showed every window as ~50% observed.
- 2026-09-03: a collection gap that runs from the start of the selected range to
      the first sample *inside* it is neither hatched nor counted: `find_gaps`
      only pairs timestamps already inside `[start, end]`, so trailing gaps count
      and leading ones vanish. Repro: an instance resumed after hours off, `?range=6h`
      said "Not collected 0 min in range" while `?range=24h` on the same data said
      548, and the hero hour strip hatched the same hours the chart drew as ordinary
      background.
- 2026-09-03: `prune --dry-run` reported "would remove 2068 raw samples; 2270 kept",
      i.e. the pre-prune count, not the count that would remain (202). Its size line
      also sums an uncheckpointed WAL, so a dry run printed "9.4 MB -> 9.4 MB" for a
      4.9 MB file while the real run of the same command printed "4.9 MB -> 0.8 MB".
- 2026-09-03: restarting the service re-fired `burn_alert` and re-POSTed the webhook
      while the rate was still above the threshold; three restarts gave three events
      and three POSTs. `alerts.py` says "a restart cannot re-fire an alert that
      already fired", but the detector is in-memory and starts with `firing=False`.
- 2026-09-03: the alert path and the display path disagree on how much data a burn
      rate needs. On a cold database with the default 20 pts/hr threshold, a
      `burn_alert` event and a webhook POST went out 61 seconds after start ("Burn
      rate 180.0 pts/hr") while the hero read "Collecting: 1m of samples", the burn
      figure was an em dash and the header showed no chip: `MIN_SPAN_S` is 60s for
      the alert, `DISPLAY_MIN_BURN_SPAN_S` is 300s for the screen.
- 2026-09-04: the Weekly — Fable meter read 100% with a critical chip, and drove the
      header's account-level chip to critical too. Fable models may use up to 50% of
      the weekly limits, so 100% of that meter is half the weekly pool spent, not an
      exhausted account. The account was at 12% session and 40% weekly at the time.
- 2026-09-04: on a range extending into the future (the default session range), the
      horizontal gridlines stopped at the vertical "now" line, so the chart looked
      cropped roughly two thirds across while the pointer still read values out of
      the empty region to its right. Repro: open the dashboard at 12:50 with a window
      resetting at 01:20 and hover right of "now".
- 2026-09-04: `--db` was read only by `serve` and `prune`. Any other command took the
      default database however the flag was set, which for a command that deletes
      rows means deleting them from the wrong file.
- 2026-09-04: four `%-d` date formats crashed the dashboard on Windows with
      `ValueError: Invalid format string`. `%-d` is a glibc and BSD extension, `%#d`
      is the Windows spelling, and the wrong one raises at the moment a date is
      rendered rather than degrading. Found by the CI matrix, not by a person.
- 2026-09-04: `start` slept a fixed 1.5s and then checked the pid was alive, so on a
      slow machine it reported "started pid N" before the server had written a line,
      and `logs` immediately after came back empty. A server that died at 1.6s was
      reported as started.
- 2026-09-04: `service install` never put `--data-dir` in the unit it wrote, on any
      platform. Installing from a shell with a custom data directory produced a
      service that collected into the default one.
- 2026-09-04: `pid_alive` on Windows treated "OpenProcess succeeded" as "still
      running". A process that has exited still opens while any handle to it is held,
      so a dead child read as alive.
- 2026-09-04: "6 resets in range" on a weekly meter, on two days of history, where a
      weekly window cannot reset even once. Not a bug in `is_reset`: every real weekly
      row carried `resets_at` within 1s of `2026-09-07T01:00:00`, far inside the 60s
      tolerance, and the drop rule never runs where an expiry is present. All six were
      crossings into or out of the eight contaminated samples, whose weekly reset time
      was 17 hours away. `split_at_resets` counted 6 with them and 0 without; the
      session series went from 17 to 6. It also flagged the real 13:00–18:00 window
      `(reset)`, which would have excluded the best data point from the weekly budget.
