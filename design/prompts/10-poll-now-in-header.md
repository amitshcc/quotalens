# Move "poll now" into the header

It sits in the chart toolbar, which since the layout change is below the fold on
a short viewport and no longer where the eye goes. The owner hunts for it every
time.

## The change

Move the control into `_header()`, immediately beside the `#polled` label.

That is the right home for it on the merits, not just for reach: `#polled` says
*how stale this is* and the button says *make it fresh*. They are the same
concern, and putting them together means the reason to click is next to the
click.

- **Move it, do not duplicate it.** Two controls that do the same thing in one
  view is worse than one in a slightly awkward place.
- **Leave the cadence controls where they are** — `auto / off / 10s / 30s / 1m /
  5m` is a setting about the poller, and it belongs with the range and lookback
  controls. Only the action moves.
- Keep the existing markup pattern: a real `<form>` with a submit button, so it
  works with script disabled, enhanced by `app.js` as it is now. Do not turn it
  into a JS-only click handler while moving it.
- Match the theme button's treatment so the header reads as one group of two
  controls, not one button and one stray.

## The thing that will break if you are not looking for it

`render_app()` re-renders the header on every refresh, so the button's DOM node
is replaced underneath any handler bound to it at load. `app.js` currently binds
`#poll` in the toolbar, which is inside the same refreshed region — check how it
survives today and keep that mechanism, or move to event delegation on a stable
ancestor. A "poll now" button that silently stops working after the first
auto-refresh is a worse outcome than the one being fixed.

Also keep the in-flight behaviour that already exists: the label changes while a
poll is running and returns to "poll now" after (`app.js` line 117). Carry that
over, and make sure a second click while a poll is in flight does nothing rather
than queuing a second request.

## While you are there

If it is one line, give it a keyboard shortcut and put the letter in the button's
`title`, the way `theme` could take one too. If it is more than one line, skip it
and say so.

## Verification

- The button polls from its new position, and still polls after several
  auto-refresh cycles have replaced the header.
- With JavaScript disabled the form still submits and the page still refreshes.
- Header layout holds at narrow widths without wrapping the chips onto their own
  line; check the existing responsive breakpoint.
- Keyboard focus order through the header is sensible and the focus ring is
  visible on both buttons.
- The toolbar no longer has a gap where the button was — close it up rather than
  leaving the row visibly missing an element.
- `ruff check` and `pytest` clean.
