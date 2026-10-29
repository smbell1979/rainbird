# Rain Bird ESP-TM2 local control

Talks to the ESP-TM2's LNK2 WiFi module directly on the LAN (192.168.0.105,
HTTPS only) using [pyrainbird](https://github.com/allenporter/pyrainbird), the
library behind Home Assistant's Rain Bird integration. Goal: add features the
controller lacks, starting with cycle and soak.

## The runner

- `service/`: Python (FastAPI, port 8710, localhost only). Owns the controller
  connection, the cycle-and-soak planner, the scheduler and SQLite. Run it
  against a simulated controller with
  `python -m rbservice --sim --speed 10 --tz America/Phoenix`. Tests:
  `cd service && ../.venv313/Scripts/python -m pytest -q tests/` (planner
  invariants and whole runs against the simulator, time at 120x).
- `web/`: Next.js 16 page (port 3004 on the NUC, http://amethyst:3004) with status, plans (editor
  with timeline preview), a calendar, history and settings. It talks only to
  the service through `/api/svc/*`. The calendar (`/api/calendar?start=&days=`)
  shows past runs from history and expected runs from the plans at today's
  seasonal adjust, plus the controller's own program days (struck through while
  the rain-delay fallback holds them back).
- The page's look (colour tokens, Geist, header, pill nav, cards, buttons,
  inputs) is copied from the pool app (`F:/Claude/PoolDataVisualizer`) so the
  home-screen apps match; tokens live in `web/src/app/globals.css`, shared
  pieces (status marks, banners) in `web/src/lib/ui.tsx`. Change colours in
  the pool app first.
- The web build is copied over the NUC's tree, which never deletes anything:
  remove a deleted source file on amethyst by hand before rebuilding.
- `deploy/deploy.sh`: dry run by default; `--apply --tz America/Phoenix`
  copies, builds and tests on amethyst without starting anything; `--start`
  enables both systemd user services.

How it waters:
- **Fallback:** the controller keeps its own programs as a backup. Each night
  at 00:10 the service sets a 1-day rain delay (only when the delay is 0, never
  lowering one) and waters with manual runs, which work during a rain delay. If
  the service stops, the delay runs out and the controller's programs resume.
- **Read-back:** every start and stop is confirmed by reading the zone state. A
  start that got no answer is never re-sent blindly. A zone started from the app
  or the panel ends the run instead of being fought.
- **Seasonal adjust:** a global seasonal adjust plus an optional per-plan
  adjust. A zone that rounds to 0 min is reported, not dropped.

## Setup

pyrainbird 6.4+ requires Python 3.13. The environment uses a standalone 3.13
fetched by [uv](https://docs.astral.sh/uv/) so the system `py` default is
untouched:

    pip install uv
    uv venv --python 3.13 .venv313
    uv pip install --python .venv313\Scripts\python.exe -r requirements.txt

## probe.py: read-only first contact

    .venv313\Scripts\python probe.py

Reads model, firmware, zones, current state, seasonal adjust and the stored
programs. It sends no command that changes anything.

Before running:

1. Put the controller password (the one the Rain Bird app asks for) alone in
   `controller-password.txt` here. It is git-ignored and never printed.
2. Close the Rain Bird app on every phone. The module serves one client at a
   time.

Each run writes `probe-logs/<time>.json` (results) and `probe-logs/<time>.log`
(pyrainbird's debug log with every raw reply; any record mentioning a password
is blanked).

### Lockout guard

The LNK2 locks out local access for 15 minutes after 3 bad passwords. The probe
records each failed first contact that may have reached the password check in
`.probe-password-failures.json`, and refuses to run once there are 2 in the
last 15 minutes. A connection that was refused, or a "busy" reply, is not
counted. A successful run clears the record.

## Known module behaviour

- LNK2 firmware 4.x is reported to answer the first request and then time out
  or reset connections (home-assistant/core#169765, unresolved). Measured here
  (LNK2 on ESP-TM2 fw 2.29.0, 20 reads each): a fresh HTTPS connection per
  request took ~4 s per reply and 20% went unanswered; one kept-alive connection
  took ~0.25 s and all answered. The probe therefore keeps the connection alive
  (`--fresh-connections` to re-test), spaces requests 1 s apart, retries an
  unanswered read once, and stops after 3 unanswered reads in a row. Some reads
  still need that retry.
- After an unanswered request the module often refuses the next connection
  outright for a few seconds. It seems to stay stuck on the abandoned one.
- `getServerMode` shows the module checks in with an AWS IoT endpoint every 60 s.
  The combined-state seasonal adjust reads 65535 (0xFFFF, presumably "per
  program"). The real values are per program.
- pyrainbird can read ESP-TM2 programs but not write them (`set_zone_schedule`
  is for ESP-RZXe/ST8 only).
