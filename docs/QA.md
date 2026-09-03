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
# an instance that never touches your real data directory
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
- [ ] Future region (auto range): blank, no horizontal gridlines (the hourly
      `--grid` separators of the window do continue), a "now" marker, the
      projection line. Must not look like either of the above.

## Data shapes

- [ ] Cold database (under 15 minutes of data): "Collecting: Nm of data" instead
      of a grid; the hero says collecting; no burn alert fires.
- [ ] A real reset boundary inside the selected range (24h on live data): the
      session trace breaks cleanly, the meter foot says "N resets in range", a
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
