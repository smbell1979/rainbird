# Sprinklers

A local runner for a Rain Bird ESP-TM2 controller. It talks to the
controller's LNK2 WiFi module directly on the LAN, with no cloud, using
[pyrainbird](https://github.com/allenporter/pyrainbird) (the library behind
Home Assistant's Rain Bird integration), and adds what the controller lacks.

- **Cycle and soak:** split a zone's watering into short cycles with soaks
  between, so it soaks in instead of running off.
- **Plans** on days of the week or every N days, with a seasonal adjust and a
  per-plan adjust, previewed on a timeline before they run.
- **Weather hold:** skip watering for a few days, then water every plan on
  the day after.
- **Calendar** of past and upcoming runs, with watering time and gallons per
  day, week and month.
- **History** of every run, cycle by cycle.
- **Safe fallback:** the controller keeps its own programs as a backup, held
  back while this runs, and back in charge within a day if it stops.

It runs on a home server (the NUC, `amethyst`) as two Docker containers, with
the page at http://amethyst:3004.

## Docs

- [How it works](docs/how-it-works.md): the fallback, confirming every
  command, adjusts, the weather hold, and what is recorded.
- [Running and deploying](docs/running-and-deploying.md): setting up, the
  simulator, tests, deploying to the NUC, rollback, and backups.
- [The controller](docs/controller.md): the read-only probe, the password
  lockout guard, and how the LNK2 module behaves.

## Layout

    service/   Python service (FastAPI): controller link, planner, scheduler, SQLite
    web/       Next.js page: Status, Plans, Calendar, History, Settings
    deploy/    ship, deploy and roll back on the NUC; weekly database backup
    probe.py   read-only first contact with the controller
