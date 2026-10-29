"""The whole service against the simulated module, with time running fast.

SPEED 120: one plan minute is half a real second. Checks are made against the
simulator's own record of what it was told and what its timers did, not
against the service's view of itself.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path

import pytest

from rbservice.clock import ScaledClock
from rbservice.link import Link
from rbservice.scheduler import Service
from rbservice.sim import SimController
from rbservice.store import Store

SPEED = 120.0
TZ = "America/Phoenix"
PIN = "123456"


class Rig:
    def __init__(self, tmp: Path, start: dt.datetime | None = None, no_answer_rate: float = 0.0,
                 pin_in_file: str = PIN, delay: int = 0, speed: float = SPEED,
                 drop_writes: bool = True) -> None:
        self.sim = SimController(PIN, speed=speed, no_answer_rate=no_answer_rate, seed=7,
                                 drop_writes=drop_writes).start()
        self.sim.delay = delay
        (tmp / "pin.txt").write_text(pin_in_file + "\n")
        self.clock = ScaledClock(TZ, speed, start)
        self.link = Link(self.sim.host, tmp / "pin.txt", self.clock, spacing_s=0.01, retry_pause_s=0.05,
                         confirm_s=3.0, speed=speed, poll_s=0.01)
        self.store = Store(tmp / "test.db")
        self.service = Service(self.store, self.link, self.clock)

    async def __aenter__(self) -> "Rig":
        await self.service.start()
        await until(lambda: self.link.health.connected or self.link.health.auth_failed, 10)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.service.shutdown()
        self.sim.close()

    def starts(self) -> list[tuple[int, int]]:
        return [(c[1], c[2]) for c in self.sim.commands if c[0] == "start"]

    def run(self, run_id: int) -> dict:
        return next(r for r in self.store.runs(500) if r["id"] == run_id)


async def until(condition, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.02)


def plan(zones, start_time="06:00", **extra) -> dict:
    return {"name": "Test", "enabled": True, "start_time": start_time,
            "days": {"weekdays": list(range(7))}, "adjust_pct": 100,
            "zones": [{"zone": z, "total_min": t, "cycle_min": c, "soak_min": s} for z, t, c, s in zones],
            **extra}


def test_cycle_and_soak_run_alternates_zones(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 2, 1, 1), (2, 2, 1, 1)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 30)
            run = rig.run(run_id)
            assert run["status"] == "done", run
            assert rig.starts() == [(1, 1), (2, 1), (1, 1), (2, 1)]
            # Every segment ended on the controller's own timer, not a stop.
            assert [c for c in rig.sim.commands if c[0] == "timer-end"] == [
                ("timer-end", 1), ("timer-end", 2), ("timer-end", 1), ("timer-end", 2)]
            assert rig.sim.running() == set()
            # Zone 1's second cycle began at least its soak after its first ended.
            segs = run["detail"]["segments"]
            assert segs[2]["at_min"] >= segs[0]["at_min"] + 1 + 1 - 0.05

    asyncio.run(go())


def test_seasonal_adjust_scales_runs_but_not_manual_zone_runs(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            rig.store.update_settings({"global_adjust_pct": 50})
            p = rig.store.save_plan(plan([(3, 4, 0, 0)], enabled=False, adjust_pct=150))
            run_id = rig.service.enqueue_plan(p, "plan-now")  # 4 x 50% x 150% = 3
            zone_run = rig.service.enqueue_zone(4, 1)
            await until(lambda: rig.run(zone_run)["status"] == "done", 30)
            assert rig.run(run_id)["status"] == "done"
            assert rig.starts() == [(3, 3), (4, 1)]

    asyncio.run(go())


def test_preview_shows_as_written_and_adjusted(tmp_path):
    store = Store(tmp_path / "p.db")
    store.update_settings({"global_adjust_pct": 60})
    service = Service(store, None, ScaledClock(TZ, SPEED))  # type: ignore[arg-type]
    pv = service.preview_plan(plan([(1, 13, 0, 0), (3, 9, 5, 10)], adjust_pct=85))
    # 13 x 60% x 85% = 6.63 -> 7; 9 x 0.51 = 4.59 -> 5 (one 5-min cycle)
    assert [(s["zone"], s["minutes"]) for s in pv["segments"]] == [(1, 7), (3, 5)]
    assert pv["water_min"] == 12
    written = pv["as_written"]
    assert [(s["zone"], s["minutes"]) for s in written["segments"]] == [(1, 13), (3, 5), (3, 4)]
    assert written["water_min"] == 22
    assert written["duration_min"] > pv["duration_min"]


def test_stop_button_ends_run_and_closes_valve(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 10, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            queued = rig.service.enqueue_zone(2, 1)
            await until(lambda: rig.sim.running() == {1}, 10)
            assert await rig.service.stop_all()
            assert rig.sim.running() == set()
            assert rig.run(run_id)["status"] == "stopped"
            assert rig.run(queued)["status"] == "stopped"
            assert rig.starts() == [(1, 10)]

    asyncio.run(go())


def test_outside_start_interrupts_without_touching_their_zone(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 3, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            # Wait for the service's own confirmation, not just the sim's state.
            await until(lambda: rig.service.state.running == [1], 10)
            rig.sim.start_from_outside(5, 2)
            await until(lambda: rig.run(run_id)["status"] != "running", 10)
            run = rig.run(run_id)
            assert run["status"] == "interrupted"
            assert "zone(s) [5]" in run["detail"]["reason"]
            assert rig.sim.running() == {5}  # left alone
            assert not [c for c in rig.sim.commands if c[0] == "stop"]

    asyncio.run(go())


def test_outside_start_during_confirmation_is_not_overridden(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 3, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.sim.running() == {1}, 10)
            rig.sim.start_from_outside(5, 2)  # before the service's first read-back
            await until(lambda: rig.run(run_id)["status"] != "running", 40)
            assert rig.run(run_id)["status"] == "interrupted"
            # Zone 5's own timer may have ended during the confirmation wait;
            # what matters is the service neither stopped it nor re-sent zone 1.
            assert not [c for c in rig.sim.commands if c[0] == "stop"]
            assert rig.starts() == [(1, 3)]

    asyncio.run(go())


def test_controller_programs_reread_after_a_failed_read(tmp_path):
    async def go():
        rig = Rig(tmp_path)
        real_schedule = rig.link.schedule
        calls = []

        async def flaky_schedule():
            calls.append(1)
            if len(calls) == 1:
                raise TimeoutError("first read lost")
            return await real_schedule()

        rig.link.schedule = flaky_schedule  # type: ignore[method-assign]
        async with rig:
            # Retry is 5 plan-minutes: 2.5 s real at 120x.
            await until(lambda: rig.service.state.programs_read_for is not None, 15)
            assert len(calls) == 2
            assert any("retrying in 5 min" in e["message"] for e in rig.store.events())

    asyncio.run(go())


def test_slow_start_confirmation_is_not_mistaken_for_an_early_stop(tmp_path):
    """Reproduces 2026-10-29: zone 7's start took ~20 s to confirm, so its
    on-time stop by the controller's timer was reported as an interruption."""
    async def go():
        async with Rig(tmp_path) as rig:
            real_start = rig.link.start_zone

            async def slow_start(zone, minutes):
                result = await real_start(zone, minutes)
                await asyncio.sleep(25 / SPEED)  # 25 plan-seconds of slow read-back
                return result

            rig.link.start_zone = slow_start  # type: ignore[method-assign]
            p = rig.store.save_plan(plan([(7, 1, 0, 0), (1, 1, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 30)
            run = rig.run(run_id)
            assert run["status"] == "done", run
            assert rig.starts() == [(7, 1), (1, 1)]

    asyncio.run(go())


def test_lost_start_is_resent_once_and_waters_once(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            rig.sim.drop_next_starts = 1
            p = rig.store.save_plan(plan([(3, 1, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 30)
            assert rig.run(run_id)["status"] == "done"
            assert rig.starts() == [(3, 1)]  # the dropped one never reached the controller

    asyncio.run(go())


def test_late_first_read_never_resends_a_start(tmp_path):
    """If the first read-back that works comes after the zone could already
    have finished, 'nothing running' is ambiguous: the start must not be re-sent
    (it would water twice). The run fails loudly instead."""
    async def go():
        async with Rig(tmp_path) as rig:
            real_running = rig.link.running
            started_at: list[float] = []

            async def blind_running():
                # Zone-state reads fail until the 1-minute run (0.5 s real) is over.
                if any(c[0] == "start" for c in rig.sim.commands):
                    if not started_at:
                        started_at.append(time.monotonic())
                    if time.monotonic() - started_at[0] < 0.7:
                        raise TimeoutError("read lost")
                return await real_running()

            rig.link.running = blind_running  # type: ignore[method-assign]
            p = rig.store.save_plan(plan([(2, 1, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 30)
            assert rig.run(run_id)["status"] == "failed"
            assert rig.starts() == [(2, 1)]  # watered once, not twice

    asyncio.run(go())


def test_fallback_sets_one_day_only_from_zero(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            await until(lambda: rig.service.state.fallback_done_for is not None, 10)
            assert rig.sim.delay == 1
            assert ("delay", 1) in rig.sim.commands

    asyncio.run(go())


def test_fallback_never_lowers_a_longer_delay_and_pauses_plans(tmp_path):
    async def go():
        start = dt.datetime(2026, 10, 29, 5, 59, 30, tzinfo=dt.timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo(TZ)).replace(hour=5, minute=59, second=30)
        async with Rig(tmp_path, start=start, delay=3) as rig:
            rig.store.save_plan(plan([(1, 1, 0, 0)], start_time="06:00"))
            await until(lambda: any(r["status"] == "skipped" for r in rig.store.runs()), 20)
            assert rig.sim.delay == 3
            assert not [c for c in rig.sim.commands if c[0] in ("delay", "start")]
            assert rig.service.state.paused_by_rain_delay

    asyncio.run(go())


def test_scheduled_plan_fires_at_its_start_time_once(tmp_path):
    async def go():
        tz = __import__("zoneinfo").ZoneInfo(TZ)
        start = dt.datetime(2026, 10, 29, 5, 59, 30, tzinfo=tz)
        async with Rig(tmp_path, start=start) as rig:
            rig.store.save_plan(plan([(6, 1, 0, 0)], start_time="06:00"))
            await until(lambda: any(r["status"] == "done" for r in rig.store.runs()), 30)
            first_start = rig.run(rig.store.runs()[0]["id"])["started_at"]
            assert first_start >= "2026-10-29T06:00"
            await asyncio.sleep(1.0)  # two more minutes of plan time: no second run
            assert rig.starts() == [(6, 1)]

    asyncio.run(go())


def test_plan_saved_after_its_time_waits_for_next_day_without_missed_warning(tmp_path):
    async def go():
        tz = __import__("zoneinfo").ZoneInfo(TZ)
        async with Rig(tmp_path, start=dt.datetime(2026, 10, 29, 9, 0, tzinfo=tz)) as rig:
            saved = rig.store.save_plan(plan([(1, 1, 0, 0)], start_time="06:00"))
            rig.service.plan_saved(saved)
            await asyncio.sleep(0.5)  # a minute of plan time: several ticks
            assert not rig.store.runs()
            assert not [e for e in rig.store.events() if "missed" in e["message"]]
            assert rig.service.next_runs()[0]["at"].startswith("2026-10-30T06:00")

    asyncio.run(go())


def test_run_completes_when_a_quarter_of_reads_go_unanswered(tmp_path):
    # Reads only: a start lost twice in a row ends the run safely (failed, never
    # watered twice), which is covered by the next two tests, not this one.
    # At 30x, so a few dropped reads in a row don't outlast half a 1-minute run.
    async def go():
        async with Rig(tmp_path, no_answer_rate=0.25, speed=30, drop_writes=False) as rig:
            p = rig.store.save_plan(plan([(1, 2, 1, 1), (2, 1, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 60)
            run = rig.run(run_id)
            assert run["status"] == "done", run
            # No zone was ever started more times than it has cycles.
            assert sorted(rig.starts()) == [(1, 1), (1, 1), (2, 1)]

    asyncio.run(go())


def test_wrong_pin_stops_all_requests(tmp_path):
    async def go():
        async with Rig(tmp_path, pin_in_file="999999") as rig:
            assert rig.link.health.auth_failed
            sent = rig.sim.requests
            rig.service.enqueue_zone(1, 1)
            await asyncio.sleep(1.0)
            assert rig.sim.requests == sent == 1
            assert not rig.starts()

    asyncio.run(go())
