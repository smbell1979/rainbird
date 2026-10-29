r"""Read-only first contact with the Rain Bird controller.

Asks the LNK2 module for the controller's model, firmware, zones, current state
and stored programs. It changes nothing: every call below is a read, and the
pyrainbird calls that write (irrigate, stop, rain delay, seasonal adjust,
firmware update) are never referenced.

    .venv313\Scripts\python probe.py

First put the controller password (the one the Rain Bird app asks for) alone in
controller-password.txt next to this file, and close the Rain Bird app on every
phone: the module serves one client at a time.

Each run writes probe-logs/<time>.json (the results) and probe-logs/<time>.log
(pyrainbird's debug log, including every raw reply from the module).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp
from pyrainbird.async_client import AsyncRainbirdClient, AsyncRainbirdController
from pyrainbird.const import ProgramFrequency
from pyrainbird.data import AvailableStations, Schedule, States
from pyrainbird.exceptions import (
    RainbirdAuthException,
    RainbirdConnectionError,
    RainbirdDeviceBusyException,
    RainbirdDeviceNackError,
)

HERE = Path(__file__).resolve().parent
DEFAULT_HOST = "192.168.0.105"
DEFAULT_PASSWORD_FILE = HERE / "controller-password.txt"
LOG_DIR = HERE / "probe-logs"
FAILURE_FILE = HERE / ".probe-password-failures.json"

# The LNK2 locks /stick for 15 minutes after 3 bad passwords. Refusing to make
# what could be the third attempt keeps a typo from turning into a lockout.
LOCKOUT_WINDOW = dt.timedelta(minutes=15)
MAX_RECENT_FAILURES = 2

# pyrainbird spaces requests this far apart, and setting it also gives each
# request a 10 s timeout. Healthy reads answer in well under a second.
REQUEST_SPACING_S = 1.0

# Reads going unanswered back to back means the module has stopped responding
# (reported for LNK2 firmware 4.x). More requests won't help and may prolong it.
# Only silence counts: a reply of any kind, even an error, shows the link works.
MAX_CONSECUTIVE_FAILURES = 3
RETRY_PAUSE_S = 2.0

# pyrainbird logs this at debug level when it discards a zone's run times while
# parsing the schedule. Watching for it is the only way to know the printed
# schedule is missing something.
DROPPED_DURATIONS_MARKER = "Mismatched number of program durations"

DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


class ModuleStalled(Exception):
    """Too many reads failed in a row."""


# --- password and lockout guard ---------------------------------------------


def read_password(path: Path) -> str:
    if not path.exists():
        sys.exit(
            f"No password file at {path}\n"
            "Create it containing only the controller password (the one the "
            "Rain Bird app asks for), then run again."
        )
    # utf-8-sig because Notepad may save a byte-order mark, which would
    # otherwise become the first character of the password.
    text = path.read_text(encoding="utf-8-sig")
    # Only the line ending is removed. Trimming anything else would quietly
    # change the password and spend one of the three attempts before lockout.
    password = text.rstrip("\r\n")
    if not password:
        sys.exit(f"{path} is empty.")
    if "\n" in password or "\r" in password:
        sys.exit(f"{path} has more than one line; it should hold only the password.")
    if password != password.strip():
        sys.exit(
            f"{path} has spaces or tabs at the start or end of the password. "
            "Remove them and run again. Nothing was sent to the controller."
        )
    return password


def local_now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def recent_failures(now: dt.datetime) -> list[dt.datetime]:
    if not FAILURE_FILE.exists():
        return []
    stamps = [dt.datetime.fromisoformat(s) for s in json.loads(FAILURE_FILE.read_text())]
    return [t for t in stamps if now - t < LOCKOUT_WINDOW]


def record_failure(now: dt.datetime) -> int:
    failures = recent_failures(now) + [now]
    FAILURE_FILE.write_text(json.dumps([t.isoformat() for t in failures]))
    return len(failures)


def counts_as_password_attempt(err: BaseException) -> bool:
    """Whether a failed first request may have reached the module's password check.

    Refused, unreachable, a failed TLS handshake or a connect timeout means the
    request never got as far as decryption, so it can't count toward the
    lockout. Neither does "busy", which the module says while serving another
    client. Anything else (a 403, a lockout reply, a reply that won't decrypt, a
    timeout after the request was sent) might have, so it is counted.
    """
    if isinstance(err, RainbirdDeviceBusyException):
        return False
    if isinstance(err, RainbirdConnectionError):
        cause = err.__cause__
        return not isinstance(
            cause, (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError)
        )
    return True


def got_no_answer(err: BaseException) -> bool:
    """Whether a failed read was the transport failing rather than the module replying.

    pyrainbird wraps transport trouble (refused, reset, timeout) with the aiohttp
    or timeout error as the cause. An HTTP error status is still a reply, and an
    error with no transport cause came from a reply that decoded to an error or
    didn't parse.
    """
    for candidate in (err, err.__cause__):
        if isinstance(candidate, aiohttp.ClientResponseError):
            return False
        if isinstance(candidate, (RainbirdConnectionError, aiohttp.ClientError, TimeoutError)):
            return True
    return False


# --- logging -------------------------------------------------------------------


class GuardLog(logging.Filter):
    """Blanks password-bearing records and notices dropped schedule data."""

    def __init__(self) -> None:
        super().__init__()
        self.dropped_durations = False

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if DROPPED_DURATIONS_MARKER in message:
            self.dropped_durations = True
        # pyrainbird writes every decrypted reply to the debug log verbatim, and
        # getWifiParams replies with the home WiFi password. That call is not
        # made here, but rather than trust that no other reply carries one, any
        # record mentioning a password is blanked.
        if "password" in message.lower():
            record.msg = "[record removed: it mentioned a password]"
            record.args = None
        return True


def setup_logging(log_path: Path) -> GuardLog:
    guard = GuardLog()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    # On the handler, not the logger: logger filters skip records from child
    # loggers such as pyrainbird.encryption.
    handler.addFilter(guard)
    rainbird_log = logging.getLogger("pyrainbird")
    rainbird_log.setLevel(logging.DEBUG)
    rainbird_log.addHandler(handler)
    return guard


# --- formatting ----------------------------------------------------------------


def describe_error(err: BaseException) -> str:
    parts = [f"{type(err).__name__}: {err}"]
    cause = err.__cause__
    while cause is not None:
        parts.append(f"{type(cause).__name__}: {cause}")
        cause = cause.__cause__
    return "  <-  ".join(parts)


def zone_list(zones: set[int]) -> str:
    return ", ".join(str(z) for z in sorted(zones)) or "none"


def program_days(program: Any) -> str:
    if program.frequency == ProgramFrequency.CUSTOM:
        days = sorted(day.value for day in program.days_of_week)
        return " ".join(DAY_NAMES[d] for d in days) or "no days set"
    if program.frequency == ProgramFrequency.CYCLIC:
        return f"every {program.period} days (synchro {program.synchro})"
    return f"{program.frequency.name.lower()} days"


def schedule_lines(schedule: Schedule) -> list[str]:
    lines = []
    if info := schedule.controller_info:
        lines.append(
            f"controller: rain delay {info.rain_delay} d, station delay "
            f"{info.station_delay}, rain sensor {'on' if info.rain_sensor else 'off'}"
        )
    for program in schedule.programs:
        starts = ", ".join(s.strftime("%H:%M") for s in program.starts) or "none"
        lines.append(f"{program.name}: {program_days(program)}")
        lines.append(f"    start times: {starts}")
        if not program.durations:
            lines.append("    run times: none")
        for zone in program.durations:
            minutes = int(zone.duration.total_seconds() // 60)
            lines.append(f"    zone {zone.zone:>2}: {minutes} min")
    if schedule.zone_schedules:
        lines.append(f"(plus {len(schedule.zone_schedules)} per-zone schedules)")
    return lines


def to_jsonable(value: Any) -> Any:
    if isinstance(value, AvailableStations):
        value = value.stations
    if isinstance(value, States):
        return {"active": sorted(value.active_set), "count": value.count, "mask": f"{value.mask:X}"}
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            # Some pyrainbird classes can't serialise themselves (ControllerState's
            # device-time strategy raises "serialize not implemented").
            pass
    if dataclasses.is_dataclass(value):
        try:
            return dataclasses.asdict(value)
        except Exception as err:  # keep the run's record even if one value won't convert
            return {"unconvertible": repr(value), "error": describe_error(err)}
    return value


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "***" if "password" in str(k).lower() else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


# --- the probe -----------------------------------------------------------------


@dataclasses.dataclass
class Step:
    name: str
    status: str  # ok | unsupported | error reply | no answer
    seconds: float
    shown: str = ""
    value: Any = None
    error: str | None = None


class Probe:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.consecutive_failures = 0

    def _print(self, step: Step) -> None:
        detail = step.shown if step.status == "ok" else step.error
        print(f"  {step.status:<12} {step.seconds:5.1f}s  {step.name}: {detail}", flush=True)

    def add(self, step: Step) -> None:
        self.steps.append(step)
        self._print(step)

    async def read(
        self,
        name: str,
        fetch: Callable[[], Awaitable[Any]],
        show: Callable[[Any], str] = str,
    ) -> Any:
        start = time.monotonic()
        try:
            try:
                value = await fetch()
            except Exception as err:
                if not got_no_answer(err):
                    raise
                # One retry: on this module about a third of requests go
                # unanswered at random, and the next one usually gets through.
                await asyncio.sleep(RETRY_PAUSE_S)
                value = await fetch()
        except RainbirdDeviceNackError:
            # A NACK is the controller saying it doesn't support the request.
            # The link is working, so it doesn't count toward the stall limit.
            self.consecutive_failures = 0
            self.add(Step(name, "unsupported", time.monotonic() - start,
                          error="controller does not support this (NACK)"))
            return None
        except RainbirdAuthException as err:
            # The password worked moments ago, so this is a lockout or the
            # module misbehaving. Either way, stop sending it.
            self.add(Step(name, "error reply", time.monotonic() - start, error=describe_error(err)))
            raise
        except Exception as err:
            if not got_no_answer(err):
                self.consecutive_failures = 0
                self.add(Step(name, "error reply", time.monotonic() - start,
                              error=describe_error(err)))
                return None
            self.consecutive_failures += 1
            self.add(Step(name, "no answer", time.monotonic() - start, error=describe_error(err)))
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise ModuleStalled from err
            return None
        self.consecutive_failures = 0
        try:
            shown = show(value)
        except Exception as err:  # a formatting bug shouldn't lose the reading
            shown = f"(could not format: {describe_error(err)})"
        self.add(Step(name, "ok", time.monotonic() - start, shown, value))
        return value


def show_model(model: Any) -> str:
    text = f"{model.model_name} ({model.model_code}, id {model.model:04X}), protocol {model.major}.{model.minor}"
    if model.model_code == "UNKNOWN":
        text += "  ** pyrainbird does not recognise this model and will guess its limits **"
    return text


def show_firmware(fw: Any) -> str:
    return f"{fw.major}.{fw.minor}.{fw.patch}"


def show_combined_state(state: Any) -> str:
    # Units of remaining_runtime aren't documented by pyrainbird, so it's shown raw.
    return (f"controller clock {state.device_time:%Y-%m-%d %H:%M:%S}, seasonal adjust "
            f"{state.seasonal_adjust}%, active station {state.active_station}, "
            f"remaining runtime {state.remaining_runtime}")


async def run_reads(probe: Probe, controller: AsyncRainbirdController, model: Any) -> None:
    info = model.model_info
    await probe.read("controller firmware", controller.get_controller_firmware_version, show_firmware)
    await probe.read("serial number", controller.get_serial_number)
    await probe.read("available zones", controller.get_available_stations,
                     lambda v: zone_list(v.active_set))
    await probe.read("zones running now", controller.get_zone_states,
                     lambda v: zone_list(v.active_set))
    await probe.read("irrigation state", controller.get_current_irrigation,
                     lambda v: "on" if v else "off")
    await probe.read("rain delay", controller.get_rain_delay, lambda v: f"{v} days")
    await probe.read("rain sensor", controller.get_rain_sensor_state,
                     lambda v: "tripped" if v else "not tripped")
    await probe.read("combined state", controller.get_combined_controller_state,
                     show_combined_state)
    for program in range(info.max_programs):
        letter = chr(ord("A") + program)
        await probe.read(f"seasonal adjust PGM {letter}",
                         lambda program=program: controller.water_budget(program),
                         lambda v: f"{v.adjust}%")
    await probe.read("network status", controller.get_network_status,
                     lambda v: f"network {'up' if v.network_up else 'down'}, "
                               f"internet {'up' if v.internet_up else 'down'}")
    # Raw, because this firmware's reply lacks fields pyrainbird's ServerMode
    # requires (missedCheckins), so the parsed call always errors.
    await probe.read("cloud server mode", lambda: controller.test_rpc_support("getServerMode"),
                     lambda v: json.dumps(redact(v), default=str))
    await probe.read("settings", controller.get_settings,
                     lambda v: json.dumps(redact(to_jsonable(v)), default=str))
    schedule = await probe.read("schedule", controller.get_schedule,
                                lambda v: f"{len(v.programs)} programs (below)")
    if schedule is not None:
        for line in schedule_lines(schedule):
            print(f"      {line}")


async def run_bench(controller: AsyncRainbirdController, count: int) -> dict[str, Any]:
    """Repeat one cheap read to measure the link itself, without retries."""
    timings: list[float] = []
    outcomes: list[str] = []
    for i in range(count):
        start = time.monotonic()
        try:
            await controller.get_rain_delay()
            outcome = "ok"
        except Exception as err:
            outcome = "no answer" if got_no_answer(err) else "error reply"
        elapsed = time.monotonic() - start
        timings.append(elapsed)
        outcomes.append(outcome)
        print(f"  {i + 1:>3}  {outcome:<12} {elapsed:5.1f}s", flush=True)
    ok_times = sorted(t for t, o in zip(timings, outcomes) if o == "ok")
    summary = {
        "reads": count,
        "ok": len(ok_times),
        "no_answer": outcomes.count("no answer"),
        "error_reply": outcomes.count("error reply"),
        "median_ok_s": round(ok_times[len(ok_times) // 2], 2) if ok_times else None,
        "max_ok_s": round(ok_times[-1], 2) if ok_times else None,
        "outcomes": outcomes,
        "seconds": [round(t, 2) for t in timings],
    }
    print(f"\nbench: {summary['ok']}/{count} ok, {summary['no_answer']} unanswered, "
          f"median {summary['median_ok_s']}s, max {summary['max_ok_s']}s "
          "(times include the 1 s request spacing)")
    return summary


async def run(args: argparse.Namespace) -> int:
    password = read_password(args.password_file)
    now = local_now()
    recent = recent_failures(now)
    if len(recent) >= MAX_RECENT_FAILURES:
        retry_at = max(recent) + LOCKOUT_WINDOW
        print(
            f"Refusing to run: {len(recent)} failed first contacts in the last 15 minutes, "
            "and a third may lock the module for 15 minutes.\n"
            f"Check the password file, then run again after {retry_at:%H:%M}. "
            f"(Failures are recorded in {FAILURE_FILE.name}.)"
        )
        return 2

    LOG_DIR.mkdir(exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"{stamp}.log"
    json_path = LOG_DIR / f"{stamp}.json"
    guard = setup_logging(log_path)
    url = f"https://{args.host}/stick"
    print(f"Rain Bird read-only probe -> {url}")
    print("(The Rain Bird app must be closed on every phone; the module serves one client at a time.)\n")

    probe = Probe()
    outcome = "complete"
    bench: dict[str, Any] | None = None
    # Keep-alive by default. Measured 2026-10-28 on this module over 20 reads:
    # a fresh connection per request took ~4 s each (the TLS handshake) and 4
    # went unanswered; one kept-alive connection took ~0.25 s each and all 20
    # answered. That contradicts the HA report's guess that keep-alive is the
    # problem, so --fresh-connections stays for re-testing after firmware updates.
    keep_alive = not args.fresh_connections
    print(f"connection: {'keep-alive' if keep_alive else 'fresh per request'}\n")
    connector = aiohttp.TCPConnector(force_close=not keep_alive)
    async with aiohttp.ClientSession(connector=connector) as session:
        # The module only listens on 443 with a self-signed certificate, so the
        # scheme is fixed here instead of using pyrainbird's create_controller.
        # Its fallback to HTTP would replace an HTTPS timeout with a port-80
        # "refused", hiding whether the password was ever tried.
        client = AsyncRainbirdClient(
            session, url, password, ssl_context=False, min_delay=REQUEST_SPACING_S
        )
        controller = AsyncRainbirdController(client)

        start = time.monotonic()
        try:
            model = await controller.get_model_and_version()
        except Exception as err:
            status = "no answer" if got_no_answer(err) else "error reply"
            probe.add(Step("connect + model", status, time.monotonic() - start,
                           error=describe_error(err)))
            outcome = "first contact failed"
            print()
            if counts_as_password_attempt(err):
                count = record_failure(local_now())
                lockout = "lockout" in str(err).lower()
                if lockout:
                    print("The module says a lockout is active. Wait 15 minutes before trying again.")
                elif isinstance(err, RainbirdAuthException):
                    print("The module rejected the password.")
                else:
                    print("The request reached the module but no usable reply came back; "
                          "a wrong password can look like this.")
                print(f"Counted as a password attempt ({count} in the last 15 minutes; "
                      f"the module locks after 3). Check {args.password_file.name} before rerunning.")
            elif isinstance(err, RainbirdDeviceBusyException):
                print("The module is busy serving another client, so the password was not tried. "
                      "Close the Rain Bird app (and anything else polling it) and run again.")
            else:
                print("Could not connect to the module at all, so the password was not tried. "
                      "Is it powered and on the network?")
        else:
            FAILURE_FILE.unlink(missing_ok=True)
            probe.add(Step("connect + model", "ok", time.monotonic() - start, show_model(model), model))
            try:
                if args.bench:
                    bench = await run_bench(controller, args.bench)
                else:
                    await run_reads(probe, controller, model)
            except ModuleStalled:
                outcome = "module stopped answering"
                print(f"\nStopped after {MAX_CONSECUTIVE_FAILURES} reads failed in a row: the module "
                      "stopped answering. This matches the reported LNK2 4.x behaviour; "
                      "power-cycling the controller is the reported way to clear it.")
            except RainbirdAuthException:
                outcome = "auth error after a good connection"
                print("\nThe module refused a request after accepting the password. Stopped.")

    unanswered = [s for s in probe.steps if s.status == "no answer"]
    errors = [s for s in probe.steps if s.status == "error reply"]
    if any(s.name == "schedule" for s in unanswered + errors):
        print(f"\nThe schedule read failed partway. The replies that did arrive are in {log_path.name}.")
    if guard.dropped_durations:
        print(f"\n** pyrainbird discarded some zone run times while parsing the schedule, so the "
              f"schedule above is incomplete. The raw replies are in {log_path.name}. **")

    record = {
        "time": now.isoformat(),
        "url": url,
        "keep_alive": keep_alive,
        "outcome": outcome,
        "bench": bench,
        "dropped_schedule_durations": guard.dropped_durations,
        "steps": [
            {"name": s.name, "status": s.status, "seconds": round(s.seconds, 2),
             "shown": s.shown, "error": s.error, "value": redact(to_jsonable(s.value))}
            for s in probe.steps
        ],
    }
    json_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")

    ok = sum(s.status == "ok" for s in probe.steps)
    unsupported = sum(s.status == "unsupported" for s in probe.steps)
    print(f"\n{outcome}: {ok} ok, {unsupported} unsupported, {len(errors)} error replies, "
          f"{len(unanswered)} unanswered")
    print(f"results: {json_path}\nlog:     {log_path}")
    return 0 if outcome == "complete" and not unanswered else 1


def main() -> None:
    # Git Bash and redirected output default to cp1252 on Windows; replace
    # rather than crash on an unexpected character in a reply.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--password-file", type=Path, default=DEFAULT_PASSWORD_FILE)
    parser.add_argument("--fresh-connections", action="store_true",
                        help="open a new connection per request instead of reusing one")
    parser.add_argument("--bench", type=int, metavar="N",
                        help="instead of the full probe, repeat one read N times and time it")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
