# QuotaLens QA checklist

> **Note added 2026-09-05.** This document names third-party tools
> (`ccusage`, ClaudeUsageBar and others) because that is what the landscape and
> the decisions looked like when it was written. Those pointers have since been
> removed from everything a reader treats as current — the dashboard, `README.md`,
> `VISION.md`, `PLAN.md`, `docs/MVP-SCOPE.md` and the site copy — because
> QuotaLens should not send its owner to a project it does not control, and
> because it is being made vendor-agnostic. The text below is left unedited: it
> is a record of a decision made at a point in time, and rewriting it would
> falsify that record.

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
- 2026-09-04: the weekly budget panel showed "—" for a limit with no usable sessions
      and threw away `Budget.reason`, which already held the explanation. A reader
      could not tell "we do not know yet" from "the answer is nothing", which are
      opposite facts to plan against. It also called the unit a "window" and put
      "windows of clock" in a column beside it, where it read as a second budget.
- 2026-09-04: the attribution panel rendered an empty table promising milestone M3,
      which `docs/MVP-SCOPE.md` puts out indefinitely. An empty table occupies the
      slot and delivers nothing; the slot now names `claude /usage` and `ccusage`.
- 2026-09-04: `.cols` began below the chart, so the sidebar could not start until the
      full-width chart had ended, leaving a tall half-empty right column beside
      Diagnostics and Recent events.
- 2026-09-04: the hero read "Session 79% left" while the meter under it read "Session
      33%" — the same window stated two ways, which reads as two numbers disagreeing.
      The meter now says "33% used", so the pair sums to 100 on sight.
- 2026-09-04: a session window that ended at 14:00 still read "Session 40% used,
      resets 14:00, +35 pts since the last reset" at 14:22, twelve pixels below a hero
      saying "no window". `compute_runway` detected the lapse, refused to project, and
      passed the stored percentage through anyway, so the meter, the ring and the
      favicon all rendered a window that no longer existed.
- 2026-09-04: the cause underneath it. `parse.py` dropped any block without a
      `resets_at`, and the drop fed Diagnostics only. The server sent
      `five_hour: {utilization: 0.0, resets_at: null}` for twenty-five consecutive
      polls — verified in the stored payloads — while `seven_day` and `limit:fable`
      recorded every minute. Nothing marked the window as no longer updating, so the
      13:59 row stayed newest and kept rendering.
- 2026-09-04: staleness was per collector only. `last_success_ts` was current
      throughout, `collector: ok`, and one block inside that healthy payload had gone
      dark. A healthy collector is not evidence that every meter is current.
- 2026-09-04: the weekly budget panel rendered "none left" at 18px mono in "Full
      sessions left" and "none left" at 12px in "At your typical session", side by side
      in the same row. `.bignum` was applied to whatever string the answer cell held,
      so a phrase wore a readout's clothes: the row with nothing left was the loudest
      thing in the panel while the row with a real number was not. `--fs-sub` is
      reserved for readouts by DESIGN.md §6 and also outgrew `--row-h`, so those rows
      stood taller than the rest of the table.
- 2026-09-04: the same panel left "Each full session costs" blank for a saturated
      limit, with no em dash and no reason — the one cell in it that explained nothing.
- 2026-09-04: the budget table's answer column was set at 15px inside a 12px table,
      so "0.2" was a quarter larger than "Weekly — all models" and "3%" in the same
      row, and the gloss inside that cell dropped back to 12px against its own parent.
      One row read as two. Emphasis by size is the wrong lever in a table row; colour
      and weight cost no vertical rhythm. (The same cell had been 18px before that.)
- 2026-09-04: the hero's headroom readout was `--lit` unconditionally, with one override
      to `--st-critical`. "89% left" — the healthiest state the tool can report — was a
      68px amber number, for the same reason "9% left" was: the element was always
      amber. The colour carried nothing, and it taught the eye to stop reading the one
      time it did change.
- 2026-09-04: that override came from `Runway.critical`, a projection (will this rate
      exhaust the window before it resets), while the meter twelve pixels below was
      coloured by `magnitude_state`, a level. Two adjacent elements, one palette, two
      meanings: at 20% used with a steep climb the hero went red while the meter stayed
      quiet, and both were behaving as designed.
- 2026-09-05: "poll now" sat in the chart toolbar, which since the two-column layout
      can fall below the fold on a short viewport. It is in the header now, beside
      `#polled`: the label says how stale this is and the button makes it fresh, so
      the reason to click is next to the click. Check after a few auto-refresh cycles —
      the header is inside `#app` and is replaced wholesale, so the button only keeps
      working because `app.js` delegates submit on `document` rather than binding it.
- 2026-09-05: the weekly limit was raised mid-window — `seven_day` fell 98% -> 0% and
      Fable 100% -> 1%, with `resets_at` moving 0.83s, i.e. not at all — and QuotaLens
      showed nothing. Worse than nothing: a boost inside a session window makes that
      window's weekly delta consumption *minus* the raise, and that delta feeds the
      cost estimate behind "full sessions left", so a boost silently makes the weekly
      budget optimistic. The payload carries no ceiling anywhere, so the size of a
      raise cannot be recovered and the window has to be excluded rather than adjusted.
- 2026-09-05: a boost on the session window collapses the burn rate, which would have
      fired a "fell back below threshold" recovery that nobody earned.
- 2026-09-05: boost detection shipped and the dashboard still showed nothing, because
      detection runs on ingest and the boost had already happened: no event row, so all
      four surfaces rendered empty. The previous round's "replay" wrote events into a
      throwaway copy inside a test harness and proved only the detector. `quotalens
      rescan` backfills from stored readings; check a *running app* on a copy of the
      real database, not a harness.
- 2026-09-05: even once recorded, the boost was not in the sidebar — six slots taken by
      burn-rate crossings. Collapsing consecutive identical events was not enough here
      because those six were all distinct, so the newest boost is pinned into the list.
- 2026-09-05: the boost's fall rendered as a twelve-hour diagonal, because no samples
      exist across the outage and the renderer joins the last point before a gap to the
      first point after. It read as a gradual decline the owner caused; it was an
      instantaneous step he was given. The series now breaks at a boost and draws the
      fall as a vertical. General gaps are still interpolated — same class of invention,
      logged below as its own issue.
- 2026-09-05: the crimson step first landed on every series with a sample straddling
      the boost instant, including the Session window, which had *risen* 0% -> 4% there.
      Boosts are per window and the mark has to be matched to the window that moved.

## Open: gaps elsewhere are still interpolated

Outside a boost, a gap in collection is drawn as a straight line between the last
sample before it and the first after. That invents a shape for hours nobody
observed, which is the same fault the boost step just fixed. The hatched "not
collected" band marks where it happens, so the information is on screen, but the
trace still asserts a path through it. Deciding what a gap should look like — a
break, a dotted bridge, something else — is a design question this change did not
carry.
- 2026-09-05: the boost vanished on a drag-selected custom range that plainly contained
      it. Not `parse_view` and not a stale client: `rng.key` was `custom` with the boost
      inside, and the *server's* fragment had neither the mark nor the crimson. The
      range's left edge landed inside the six-hour outage before the boost, so the boost
      row was the first row in range with no preceding sample to step from. Dragging a
      range around a boost to look at it is exactly the gesture that hid it.
- 2026-09-05: the `now` rule was gated on `end > now`, so a custom range ending at or
      before now had no marker at all and the chart gave no cue where the present was.
- 2026-09-05: the hero read "Exhausted at 01:34" — past tense for a projection about the
      future. Now "Will run out at 01:34, 3h 17m before the reset."
- 2026-09-05: `quotalens status` crashed with `TypeError: unsupported format string
      passed to NoneType.__format__` whenever any window's percentage was unknown —
      `service.py` formatted `reading['pct']` as `%6.1f` with no None branch. Present
      since the service command shipped (7806017); it only surfaced once a lapsed
      session window started reporting a null percentage, which is the same shape
      prompt 05 fixed on the dashboard. The CLI had not been given the epistemic rule
      the page already had: unknown is removed and explained, never formatted. Now an
      em dash.
- 2026-09-05: `QUOTALENS_POLL_ENABLED=0` was silently ignored. `Settings.poll_enabled`
      existed and `api.py` honoured it, but no loader ever read the environment
      variable, so a scratch QA instance polled the live vendor on startup — twice in
      this session's own verification runs. A declared setting that no layer can set
      reads as supported, which is worse than absent. `tests/test_config_store.py`
      now parametrises over every config key and fails if any one of them cannot be
      set from the environment or the file.

## Open: downsampling beats deleting, and is not what retention does

Retention deletes old `quota` rows outright, so the period before the cutoff is
gone — not coarser, gone. Keeping one row per 15 minutes beyond 30 days would
hold a year of chart shape in roughly a twentieth of the rows: at a 60-second
poll that is 96 rows a day instead of 1,440, and the chart at a month's zoom
cannot resolve the difference anyway. Deletion loses the period entirely; the
downsample loses only detail nobody can see at that scale.

That is the better feature, and it is deliberately not this one. Half-building
it inside the retention job — "delete, but keep every fifteenth row" — would
make the row counts, the size estimates and the burn-rate maths all disagree
about what a row means. It wants its own design: which columns survive an
aggregation, what `is_active` means for a bucket, and whether the boost detector
can still find a step in downsampled data (it looks at consecutive rows, so
probably not without help).

- 2026-09-06: retention nearly shipped destroying the history it was designed to
      protect. `session_window` is kept on a two-year floor because
      `compute_budgets` needs it — but `api.py` rebuilds that table from every
      `quota` row on each start, so pruning `quota` to a week and restarting
      silently deleted every older window. Observed on a copy of the real
      database: 11 windows became 7 across one restart. The rebuild was treating
      "I cannot derive this" as "this did not happen". `rebuild` now takes
      `keep_underivable`, true at startup and false for `forget`/`rescan`, where
      deleting the derived row is the point. Two tests pin both directions.
- 2026-09-06: the settings panel labelled every field "takes effect on the next poll"
      and that was false for all of them. `Settings` is frozen and was captured in
      `create_app`'s closure, so saving a poll interval wrote `config.json` and changed
      nothing until a restart — verified: /api/health still said 60 after saving 120.
      Rather than relabel the fields, routes now read `state.settings` and the save
      handler replaces it and calls `poller.adopt()`, so the claim is true. The port
      and database path are deliberately excluded: neither can move under a running
      server, which is why they are the read-only block.
- 2026-09-06: `create_app` starts the vendor status watcher and its first check is due
      immediately, so the whole test suite was making real outbound requests to three
      third parties. Nothing failed — the watcher swallows errors by design — which is
      exactly why it went unnoticed. conftest now refuses `status.fetch` for every
      test, the same rule that already forbade reaching the provider API.
- 2026-09-06: the vendor status feeds failed with CERTIFICATE_VERIFY_FAILED through
      stdlib `urllib`, which uses the interpreter's trust store; this Python has none.
      Every row would have read "unreachable" forever, blaming three vendors for a
      local misconfiguration. Now fetched through `curl_cffi`, already a dependency
      for the provider client and shipping its own CA bundle.
- 2026-09-06: hovering the boost mark produced two tooltips stating two different
      times — the crosshair readout at the cursor's timestamp and the native `<title>`
      at the boost's — and the readout box covered the `Limits Boosted` label, which
      rendered as `Li`. Two interaction modes wearing the same clothes: the readout is
      a continuous scrub, the mark is a discrete annotation. Only one is on screen now,
      in both directions.
- 2026-09-06: the boost group's `mouseenter` never fired under injected pointer moves,
      while `mousemove` on the SVG did — the browser's hover chain does not reliably
      update for grouped SVG children. The tooltip is driven from `onMove` instead, so
      one handler decides what the pointer is over.
- 2026-09-06: the tooltip was first positioned against `state.svg`'s rect, but it is
      `position:absolute` inside the chart `<section>` — its offsetParent. Measuring
      against the wrong origin under-counted by the section's padding and put the box
      on top of the label it hangs below. Now anchored to the mark's own rendered box
      relative to `tip.offsetParent`.
- 2026-09-06: `_boost_marks` always drew the rocket and label to the right of the step,
      so a boost near the right edge pushed `Limits Boosted` past the plot and clipped
      it. The group now mirrors to the left, and the tooltip's alignment mirrors with
      it.
- 2026-09-06: every `.cap` heading sat one indent step right of the content it headed —
      "Where the quota went", "Vendor status", the chart headings, the budget and
      settings sections. A rule setting `.cap`'s horizontal padding to zero was written
      to fix exactly this and never took effect: it sat *above* `caption,.cap{...padding
      ...}` at the same specificity, so the later rule won. Dead code that looked like a
      fix. The captions were never misaligned by an empty slot waiting for an icon.
- 2026-09-06: the notification banner reading "Session at 91%, resets 13:00" was **not
      sent by QuotaLens**. Proof: zero `notify_crossed` rows exist in the database;
      `notify` is off by default and was never enabled on the live instance; the
      window's stored `resets_at` at that moment was `2026-09-05T20:39:59Z` = 02:10
      local, and `_reset_clock` renders it as 02:10 correctly. The banner was a retained
      one from this session's own prompt-17 verification, where `send()` was called with
      a hand-written `Crossing(pct=91.0, resets_at_text='13:00')` — the string matches
      character for character, and the real window was at 100%, not 91%. No detection
      change made. The reset text now carries a date when the reset is not today, which
      is the ambiguity that made it unreadable either way.
- 2026-09-06: `osascript -e 'display notification'` can never show the QuotaLens icon —
      macOS attaches the icon of the posting process, and that is Script Editor. Not a
      message bug and not fixable in the message. `terminal-notifier` is preferred when
      present and passed `-appIcon`; `notify-send` gets `-i`. When it is absent the
      settings toggle says so rather than shipping the wrong icon silently.
- 2026-09-06: over-cap spend rendered as *normal* whenever credits were disabled, so the
      one real over-cap reading anyone has seen — $3.16 against a $2.00 cap, 158% — was
      drawn in the quiet tier. Being over the cap is *why* it had been disabled. Now
      critical on the readout either way, while the page-level chip still only rises
      when credits are on and more can actually be spent.

## Open: the credits panel disappears when the collector is quiet

`dash.spend` comes from the live poller status, not from the store, so a dashboard
whose collector has stopped shows no `Usage credits` block at all — even though
the whole `overage` series is on disk and the new `On credits` / `Spent in range`
rows beside it render fine from it. Consistent with the epistemic rules (no
current reading, no current figure) but inconsistent with its own neighbours, and
the range-scoped figures are the ones a reader wants when the collector is down.
Reading the last stored overage row and marking it stale would fix it; deciding
what "stale money" should look like is a design question this change did not carry.
- 2026-09-06: the retention radios started 40% across and `≈ 43 MB` broke into three
      lines reading `≈`, `43`, `MB`. Not a design problem: the retention form is its own
      `.fform`, and while `.ropt` and `.rexp` were pinned to `grid-column:2`, the two
      explanatory paragraphs were not. They auto-placed, one landing in column 1, which
      made that column 394px wide against column 2's 135px. Measured before the fix.
      Three declarations put it right — span every non-control child, `nowrap` the size,
      cap the prose at 76ch — with no layout work at all. The pinning was done element
      by element, which is exactly how two paragraphs slipped through; it is a rule now.
- 2026-09-06: the settings control was an `<a>` sitting beside `theme`'s `<button>`, so
      it took the link colour and rendered as a blue smudge. Two controls doing the same
      kind of job were two colours because of their tag names. It is a button now, with
      the `<a>` kept for the no-JavaScript path and CSS showing exactly one — the same
      `.go` idiom the range form already uses. Verified by computed style: both now
      report rgb(139,148,149).
- 2026-09-06: saving from the settings panel reloaded settings wholesale from file and
      environment, dropping every CLI flag the instance was started with. An instance on
      `--port 8830` reported 8787 in the panel's own read-only block — the one place that
      has to be right — as soon as anything was saved. `db_path` had been patched back by
      hand, which was the tell that the patch was the wrong shape. The save now adopts
      only the keys the panel owns.
- 2026-09-06: OpenAI's mark vanished on the dark theme. Not a colour choice: the file
      is drawn in `currentColor`, and an SVG loaded through `<img>` is an isolated
      document with no access to the embedding page's CSS, so its `currentColor`
      resolved to the initial black on a near-black ground. A `currentColor` mark is
      the variant a vendor ships *for* dark grounds, so honouring it is using the file
      as supplied rather than restyling it. Painted through a CSS mask now: the mark
      takes `--txt-dim` like the row it sits in, the stored file is unchanged, and a
      mask cannot execute anything — which inlining a third-party SVG could. Marks
      carrying their own brand colour, like Claude's clay, stay in `<img>` and are
      never recoloured.
- 2026-09-06: the settings dialog's header and footer scrolled away with the content,
      because `overflow:auto` sat on the `<dialog>` itself alongside `max-height`.
      Measured before the fix: the header went from top 90 to top -310 on a 400px
      scroll — above the dialog's own top edge. The scroll belongs to the body alone,
      and `min-height:0` on that body is load-bearing: without it a flex item will not
      shrink below its content, the body never becomes scrollable, and the header
      leaves anyway.
- 2026-09-06: `notify_credits` was in `PANEL_KEYS` but had no field in the form, so
      every save silently turned credit notifications off — an unchecked box sends
      nothing, and `apply_form` reads a missing boolean as false. Observed: True on
      disk before a save, False after one that never mentioned it. A panel key with no
      field is not "left alone", it is switched off. A test now asserts every
      `PANEL_KEYS` entry is rendered.
- 2026-09-06: the narrow one-column collapse was written *above* the `.fform` rules it
      overrides, at equal specificity, so it lost the cascade and did nothing. Measured
      at a real 390px viewport through Playwright: the grid still reported two columns.
      This is the same silent no-op as the `.cap` padding rule two prompts ago —
      a rule written to fix something, placed where it cannot win. Position in the
      file was the whole fix, and a test now asserts the order.
- 2026-09-06: the settings dialog vanished a refresh interval after opening and turned
      up as ordinary content below the footer. Two faults, both required. **(A)** the
      `<dialog>` was emitted by `_main`, so it was inside `render_app` — which is exactly
      what `/api/dashboard/fragment` returns and what `app.js` writes into
      `#app.innerHTML`. Every refresh destroyed the open dialog and inserted a fresh
      closed one; a native dialog removed from the document leaves the top layer for
      good, and the replacement had never had `showModal()` called on it. **(B)**
      `dialog{display:flex}` carried no `[open]` guard, so it overrode the UA's
      `display:none` for that closed replacement and it laid out in normal flow after
      the footer. The shell now renders once as a sibling of `#app`, the flex rules sit
      on `dialog[open]`, and `app.js` re-queries the node after the fetch instead of
      caching it across the await. Verified: open for 60s across ~6 refreshes plus a
      forced poll, still one `#sd`, still `:modal`, still holding a typed value.
- 2026-09-06: switching every notification threshold off handed 50/75/90 straight back.
      Two layers. `parse_thresholds` answered both `None` and `""` with
      `DEFAULT_THRESHOLDS`, so an empty configured list was indistinguishable from an
      absent key. And underneath it `Settings.with_overrides` drops `None` by design —
      so a CLI flag that was not passed clears nothing — which made the panel writing
      `None` a silent no-op: the stored value simply stayed. The panel sets every key it
      owns, so it uses `dataclasses.replace` now; the merge helper is for flags.
      Observed: 303 accepted, `config.json` unchanged at `30,60,80`.
- 2026-09-06: a stored threshold outside the offered select values — `10`, from a
      hand-written or older config — had no `<option>`, so the select fell back to
      Disabled and the next save would have thrown the value away silently. The current
      value is now always added to the choices.
- 2026-09-06: a failed notification was recorded as if it had been sent. `_check_notify`
      wrote the `notify_crossed` event *before* calling `send()` and discarded the
      boolean, so a delivery that never happened still suppressed that level for the
      rest of the window. The event stays — it is what stops a failure becoming a retry
      on every poll — but the outcome is recorded with it, and no surface reads
      "notified" for an attempt that returned False.
- 2026-09-06: `detect_capability()` ran once, at Poller construction, so installing
      `terminal-notifier` afterwards went unnoticed until a restart and any status line
      would have been reporting startup rather than now. Re-detected when the settings
      view renders and when the test action runs; the poller adopts the fresh value.
- 2026-09-06: `selected_vendors` began `if not keys: return VENDORS`, so deselecting
      every source silently re-checked all of them — the same shape as the
      `notify_thresholds` fallback fixed the day before. Empty now means none. The
      documented default for an *absent* key lives on the ConfigKey, not here: deciding
      it in both places is what made "the user turned everything off" unrepresentable.
- 2026-09-06: **the `with_overrides` drops-None trap, third occurrence.** After the
      panel wrote `status_vendors: null`, the API's adopt block used `with_overrides` to
      copy the reloaded values onto the running instance — and that helper drops None by
      design, so an unpassed CLI flag clears nothing. The value reached `config.json`
      and never reached the running app: the rows stayed up. Observed as 2 rows on the
      dashboard with `null` on disk. The panel sets every key it owns, so the adopt
      block uses `dataclasses.replace` now, as `apply_form` already did. Any future
      "clearing a setting does nothing" bug should start here.
- 2026-09-06: a heading inside `.dep` is not a direct child of `.fform`, so it never
      matched the span rule and auto-placed into column 1 — landing *beside* its own
      group of controls instead of above it. Seen at 1440px.
