# How it works

Two parts: a Python service that owns the controller connection, the
planner, the scheduler and the database, and a web page that talks only to
the service (through `/api/svc/*`). Nothing but the service reaches the
controller.

## The fallback

The controller keeps its own programs as a backup. Each night at 00:10 the
service sets a 1-day rain delay (only when the delay is 0; it never lowers
one) and waters with manual runs, which the controller accepts during a rain
delay. If the service stops, the delay runs out and the controller's own
programs take over within about a day.

A rain delay of 2 days or more that the service didn't set is left alone and
pauses plans too: someone set it on purpose. After each of the controller's
own program start times, the service checks whether it watered, so the
fallback is tested every time one comes round.

## Every command is confirmed

Every start and stop is confirmed by reading the zone state back. A start
that got no answer is never re-sent blindly. A zone started from the Rain
Bird app or the panel ends the run rather than being fought.

## Adjusts

A global seasonal adjust (Settings) and an optional per-plan adjust multiply
each zone's minutes, rounded to whole minutes once from the product. A zone
that rounds to 0 minutes is reported, never quietly dropped. Manual zone runs
are never adjusted.

## Weather hold

Set from the Status page (1 to 14 days, today counting as day 1), or
`PUT /api/hold {days}` and `DELETE /api/hold` to cancel. No plan
waters until the hold ends. On the day after, every plan that is on waters at
its usual time, and every-N-day plans count their days from then (their start
date is rewritten that day, once). Held days get a skipped run in History and
are hatched on the calendar.

It lives in the service rather than in the controller's rain delay, which the
fallback renews nightly and which can't water every plan on the resume day.
Cancelling before the resume day puts plans back on their own days, with no
make-up run.

## Calendar

Past days show runs from history. Coming days show each plan's expected runs
at today's seasonal adjust (a later change to it moves them), and the
controller's own program days, struck through while the fallback holds them
back. Plans can be kept off the calendar (a test plan, say); their past runs
still show. Served by `/api/calendar?start=&days=`.

## What is recorded

Every run is kept: when it was queued, started and finished, how it ended
and why, and each cycle's zone, start, minutes asked for and minutes the
valve was actually open. Nothing deletes runs; deleting a plan keeps its runs,
which carry the plan's name at the time. An event log records everything the
service did or noticed.

Gallons are minutes times each zone's GPM (Settings), worked out when shown,
so changing a GPM restates past runs. Zones without a GPM are left out and
the total is marked with a "+". Watering started from the app, the panel or
the controller's own programs is not in the history.

## Look

The page's colours, type, header, pill navigation, cards, buttons and inputs
are copied from the pool app (`F:/Claude/PoolDataVisualizer`) so the
home-screen apps match. Tokens are in
`web/src/app/globals.css`, shared pieces (status marks, banners) in
`web/src/lib/ui.tsx`. Change colours in the pool app first.
