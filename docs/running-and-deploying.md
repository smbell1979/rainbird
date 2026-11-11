# Running and deploying

## Setting up

pyrainbird 6.4+ needs Python 3.13 (on 3.12 pip quietly installs 6.0.2,
which can't reach an LNK2 that serves HTTPS only). The environment uses a
standalone 3.13 fetched by [uv](https://docs.astral.sh/uv/), so the system
Python is untouched:

    pip install uv
    uv venv --python 3.13 .venv313
    uv pip install --python .venv313\Scripts\python.exe -r service\requirements.txt

The page: `cd web && npm install`.

## Running locally against a simulator

    cd service
    ..\.venv313\Scripts\python -m rbservice --sim --speed 10 --tz America/Phoenix

`--speed 10` runs the simulated controller's time ten times fast; leave it out
for real time. Then `cd web && npm run dev`. The page reaches the service on
127.0.0.1:8710 (`RAINBIRD_API` to change it).

## Tests

    cd service
    ..\.venv313\Scripts\python -m pytest -q tests/

Planner rules, the calendar, the weather hold, water use, and whole runs
against the simulator at 120x. The page: `cd web && npx tsc --noEmit && npx eslint .`

## Deploying to the NUC

The service and the page run as two containers on `amethyst`,
`rainbird-service` and `rainbird-web` (`docker-compose.yml`, in `~/rainbird`
there), the same shape as the Pool and Weather apps, so they show in Beszel
next to them. Both use host networking: the API stays on 127.0.0.1:8710, the
page is on port 3004, and the controller is reached directly on the LAN.

    bash deploy/ship.sh            # committed work
    bash deploy/ship.sh --dirty    # a throwaway build of uncommitted changes

`ship.sh` copies the tree to the NUC (replacing the source directories) and
the PIN separately, then runs `deploy/remote-deploy.sh` there. That builds
both images while the running version carries on (the service image runs the
whole test suite against the simulator, so a failing test stops the deploy),
then switches. It refuses to switch while a zone is watering or a run is
queued; `FORCE=1` overrides. The service gets 90 seconds to stop, so a stop
mid-run closes the valve and confirms it.

Images are tagged with their commit. On the NUC:

    bash deploy/rollback.sh          # what's running, what exists
    bash deploy/rollback.sh <tag>    # switch back, no rebuild

On the NUC the database is the bind mount `~/rainbird/data`, and the
controller PIN is `~/rainbird/controller-pin.txt`, mounted read-only. The
time zone is set in `docker-compose.yml`. Until 2026-11-03 these ran as
systemd user services; the first container deploy retired them.

## Backups

Weekly, Sundays at 03:15 NUC time (`rainbird-backup.timer`, a systemd user
timer), as a plain-text SQL dump to the private repo
`smbell1979/rainbird-backup`, via `~/rainbird-backup` on the NUC, pushed with
its own deploy key `~/.ssh/id_ed25519_rainbird_backup`.

`deploy/backup.sh` is a dry run unless given `--apply`. It refuses a corrupt
snapshot, an empty one, or one that lost more than half the run history. The
first backup was restore-tested from GitHub's copy into a fresh database:
every table matched the live one.

To restore: `docker compose stop service` in `~/rainbird`, then
`sqlite3 rainbird.db < rainbird.sql`.
