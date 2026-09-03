# QuotaLens — vision

## The problem

Claude Pro and Max subscribers get a level, not a rate. Settings → Usage shows
you a percentage. It does not show you how fast that percentage is moving, and
when you close the tab it does not remember that it moved.

Claude Code's `/usage` goes further than that, and further than this project
once planned to. On Pro and Max it attributes recent usage to skills, subagents,
plugins and individual MCP servers, flags behaviours like cache misses, and
lists the heaviest scheduled tasks with how often each fired and what it cost.
That is better attribution than reading local transcript logs could ever give,
and it needs no cookie. It also, in Anthropic's own words, covers the last 24
hours or 7 days, is "computed from local session history on this machine", so
usage from other devices or claude.ai is not included, and it is gone when you
close the terminal.

So the gap is not attribution. It is memory, and the shape of the climb:

> **Claude tells you what is consuming your quota right now, on this machine,
> and then forgets. QuotaLens remembers.**

That matters because of a specific failure mode. A scheduled task or a
background agent session runs on Anthropic's infrastructure, not on your
machine, so nothing appears in `ps` or in your task manager. It consumes quota
silently. You find out at 100%, hours after the fact, and by then there is
nothing left to look at: no record of when the climb started, how steep it was,
or which five-hour session it happened in. If extra usage billing is enabled,
you find out on your card.

The other tools each hold one half and let go of it. Menu bar apps
(ClaudeUsageBar, Usagebar, Usage4Claude) poll the account endpoint and show the
current percentage — no history, and macOS only. `ccusage` reads Claude Code's
local transcript logs and attributes tokens to projects — but knows nothing
about your subscription quota, and nothing about usage from the web app, Cowork
or scheduled tasks, because quota is pooled across all of them and local logs
are not.

Nobody keeps the series.

## What this is

A local, self-hosted web application that records your Claude usage over time
and tells you what is consuming it.

It runs on your machine, stores everything in a local SQLite file, and serves a
dashboard on a loopback port. You paste your session cookie once. It polls your
account quota on an interval, reads Claude Code's local session logs if they
exist, and puts both on one timeline.

The number it leads with is the **burn rate**: points of quota per hour. A level
tells you where you are. A rate tells you whether something is running.

## Who it's for

Heavy Pro and Max users who run more than one thing against their account —
scheduled tasks, background agents, Claude Code across several repos, the web
app, an IDE extension. People who have been surprised by a limit and want the
receipts. Disproportionately developers and infra people, who will also want the
data somewhere they can query it.

## Principles

**Free, MIT, no account, no telemetry.** Same posture as ClaudeUsageBar. There
is no server component, no signup, and nothing phones home. The only outbound
requests are to claude.ai.

**Local by default.** Binds `127.0.0.1`. The session cookie lives in the OS
keychain, never in a config file committed by accident, never in a log line.

**Cross-platform.** macOS, Linux, and Windows, plus a container image. This is
the clearest gap in the existing tools and the cheapest one to fill.

**History is the product.** Anyone can render a percentage, and Claude Code can
attribute today's. The value here is the time series: burn rate, the shape of
the climb, whether this five-hour window will run out before it resets, and
being able to look back at last Tuesday and see the exact minute it started.

**Your data leaves easily.** CSV and JSON export, plus a Prometheus `/metrics`
endpoint so it drops into an existing Grafana stack without a plugin.

**Honest about the API.** The quota endpoints are internal and undocumented.
The tool says so plainly in the README and in the UI when a call fails, degrades
gracefully when the shape changes, and never pretends to be an official
integration.

## What this is not

- Not a way to increase, bypass, or extend a limit. It only observes.
- Not a hosted service. There is no SaaS tier and no plan to add one.
- Not a Claude Code wrapper or a proxy. It never sits in the request path.
- Not an API cost tracker for Console users. That is a different product with a
  documented API and existing tools; subscription quota is the underserved case.
- Not a menu bar app. ClaudeUsageBar does that well. This is complementary, and
  the README should say so and link to it.

## What success looks like

Someone installs it, leaves it running for a week, and can answer: what
consumed my quota, when, and is anything running right now that shouldn't be.
