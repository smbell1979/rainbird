r"""Run the service.

    python -m rbservice                 # the real controller (settings from env or flags)
    python -m rbservice --sim           # a simulated controller, for working on the page
    python -m rbservice --sim --speed 30   # ... with time running 30x fast

Settings (flag, or environment variable):
    --host           RAINBIRD_HOST           controller address (default 192.168.0.105)
    --password-file  RAINBIRD_PASSWORD_FILE  file holding the controller PIN
    --db             RAINBIRD_DB             SQLite database path
    --tz             RAINBIRD_TZ             time zone for plan start times (required)
    --port           RAINBIRD_PORT           API port on 127.0.0.1 (default 8710)
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path

import uvicorn

from .api import create_app
from .clock import Clock, ScaledClock
from .link import Link
from .scheduler import Service
from .store import Store


def main() -> None:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Rain Bird cycle-and-soak service")
    parser.add_argument("--host", default=env("RAINBIRD_HOST", "192.168.0.105"))
    parser.add_argument("--password-file", type=Path, default=env("RAINBIRD_PASSWORD_FILE"))
    parser.add_argument("--db", type=Path, default=env("RAINBIRD_DB"))
    # No default: the NUC's own zone may not be where the sprinklers are, and a
    # wrong zone shifts every start time by an hour or more without any error.
    parser.add_argument("--tz", default=env("RAINBIRD_TZ"))
    parser.add_argument("--port", type=int, default=int(env("RAINBIRD_PORT", "8710")))
    parser.add_argument("--sim", action="store_true", help="use a simulated controller")
    parser.add_argument("--speed", type=float, default=1.0, help="with --sim: time speed-up")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.tz:
        parser.error("set --tz or RAINBIRD_TZ (e.g. America/Phoenix)")

    if args.sim:
        from .sim import SimController

        scratch = Path(tempfile.mkdtemp(prefix="rainbird-sim-"))
        password_file = scratch / "pin.txt"
        password_file.write_text("123456\n")
        sim = SimController("123456", speed=args.speed).start()
        host = sim.host
        db = args.db or scratch / "sim.db"
        clock: Clock = ScaledClock(args.tz, args.speed) if args.speed != 1 else Clock(args.tz)
        logging.getLogger(__name__).info("Simulated controller on %s, database %s", host, db)
        link = Link(host, password_file, clock, spacing_s=0.2, retry_pause_s=1.0, speed=args.speed)
    else:
        if not args.password_file or not args.db:
            parser.error("set --password-file and --db (or RAINBIRD_PASSWORD_FILE / RAINBIRD_DB)")
        db = args.db
        clock = Clock(args.tz)
        link = Link(args.host, args.password_file, clock)

    service = Service(Store(db), link, clock)
    uvicorn.run(create_app(service), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
