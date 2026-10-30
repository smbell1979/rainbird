"""Runs plans: cycle-and-soak sequencing, the rain-delay fallback, and watching.

Design (agreed with Scott, 2026-10-28):
- The controller keeps its own programs as a backup. Each day just after
  midnight the service sets a 1-day rain delay so they don't fire, and waters
  on its own schedule with manual runs (which the controller still accepts
  during a rain delay; tested). If this service dies, the delay runs out and
  the controller's programs take over within about a day.
- A rain delay the service didn't set is never lowered. A delay of 2+ days
  means someone set it on purpose (after a storm, say), so plans pause too.
- Each zone command carries its own run time, so the controller ends every
  segment itself. The service confirms each start and each stop by reading
  the zone state back.
- A failed read during a segment proves nothing (the module often misses one).
  Only a successful read showing the wrong state counts as an interruption,
  and an interruption ends the run rather than fighting whoever caused it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from pyrainbird.const import ProgramFrequency

from .clock import Clock
from .link import Link, LinkLockedOut, describe
from .planner import Segment, Sequencer, Wait, ZoneSpec, adjust_zones, preview
from .store import Store

log = logging.getLogger(__name__)

FALLBACK_AT = dt.time(0, 10)
# Controller program starts this early would fire before the daily renewal.
FALLBACK_RISK_BEFORE = dt.time(0, 15)
TICK_S = 20
IDLE_POLL_S = 60
SEGMENT_POLL_S = 30
END_GRACE_S = 90
# A zone found off this close to its end finished on the controller's timer.
EARLY_STOP_TOLERANCE_S = 30
IDLE_WAIT_LIMIT_S = 30 * 60
SUPPRESSION_CHECK_AFTER = dt.timedelta(minutes=5)
FALLBACK_RETRY = dt.timedelta(minutes=5)
# Time between one segment ending and the next being started and confirmed,
# used only for the preview's estimated finish.
SEGMENT_GAP_MIN = 0.25


def _day(day: dt.date | str) -> str:
    """"Sat Nov 1". Not %-d, which Windows' strftime rejects."""
    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    return f"{d:%a %b} {d.day}"


def watered_minutes(segment: dict[str, Any]) -> float | None:
    """How long a cycle's valve was open. None when history can't say: cycles
    cut short before watered_min was recorded (added 2026-10-29)."""
    if "watered_min" in segment:
        return float(segment["watered_min"])
    if segment.get("result") == "ok":
        return float(segment["minutes"])  # the controller's own timer ran it out
    return None


def water_usage(runs: list[dict[str, Any]], gpm: dict[int, float]) -> dict[str, Any]:
    """Minutes and gallons per day from run history.

    A day's gallons cover only zones with a GPM set; those without are listed
    in no_gpm instead of being counted as zero. Cycles whose time is unknown
    are listed in unknown rather than guessed. Water from the app, the panel
    or the controller's own programs isn't in run history and isn't counted.
    """
    days: dict[str, dict[str, Any]] = {}
    unknown, no_gpm = [], set()
    for run in runs:
        day = (run["started_at"] or run["queued_at"])[:10]
        for seg in run["detail"].get("segments", []):
            minutes = watered_minutes(seg)
            if minutes is None:
                unknown.append({"run_id": run["id"], "day": day, "zone": seg["zone"], "result": seg.get("result")})
                continue
            if minutes <= 0:
                continue
            d = days.setdefault(day, {"minutes": 0.0, "gallons": 0.0, "zones": {}})
            z = d["zones"].setdefault(str(seg["zone"]), {"minutes": 0.0, "gallons": None})
            z["minutes"] += minutes
            d["minutes"] += minutes
            if seg["zone"] in gpm:
                gallons = minutes * gpm[seg["zone"]]
                z["gallons"] = (z["gallons"] or 0.0) + gallons
                d["gallons"] += gallons
            else:
                no_gpm.add(seg["zone"])
    for d in days.values():
        d["minutes"] = round(d["minutes"], 1)
        d["gallons"] = round(d["gallons"], 1)
        for z in d["zones"].values():
            z["minutes"] = round(z["minutes"], 1)
            if z["gallons"] is not None:
                z["gallons"] = round(z["gallons"], 1)
    return {"days": days, "no_gpm": sorted(no_gpm), "unknown": unknown, "gpm": {str(k): v for k, v in gpm.items()}}


@dataclass
class RunRequest:
    run_id: int
    kind: str  # scheduled | plan-now | zone
    name: str
    zones: list[ZoneSpec]
    plan_adjust_pct: int = 100
    apply_global_adjust: bool = True


@dataclass
class ControllerProgram:
    name: str
    frequency: str
    weekdays: list[int]  # Mon=0, matching the plans
    period: int | None
    first_due: dt.date | None  # for cyclic programs, estimated from synchro
    starts: list[str]
    zones: list[int]

    def due_on(self, day: dt.date) -> bool | None:
        """Whether the program fires that day; None when it can't be worked out."""
        if self.frequency == "CUSTOM":
            return day.weekday() in self.weekdays
        if self.frequency == "ODD":
            return day.day % 2 == 1
        if self.frequency == "EVEN":
            return day.day % 2 == 0
        if self.frequency == "CYCLIC" and self.period and self.first_due:
            return (day - self.first_due).days % self.period == 0
        return None


@dataclass
class ServiceState:
    started_at: dt.datetime | None = None
    running: list[int] = field(default_factory=list)
    running_read_at: dt.datetime | None = None
    rain_delay: int | None = None
    paused_by_rain_delay: bool = False
    sensor_tripped: bool | None = None
    available_zones: list[int] = field(default_factory=list)
    controller_programs: list[ControllerProgram] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback_done_for: dt.date | None = None
    fallback_next_try: dt.datetime | None = None
    programs_read_for: dt.date | None = None
    programs_next_try: dt.datetime | None = None
    current: dict[str, Any] | None = None
    queued: list[dict[str, Any]] = field(default_factory=list)


class Service:
    def __init__(self, store: Store, link: Link, clock: Clock) -> None:
        self.store = store
        self.link = link
        self.clock = clock
        self.state = ServiceState()
        self._queue: asyncio.Queue[RunRequest] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._current_task: asyncio.Task | None = None
        self._external_seen: list[tuple[dt.datetime, list[int]]] = []
        self._suppression_checked: set[tuple[str, str, dt.date]] = set()
        self._segment_begun: float | None = None  # clock.elapsed() when the current cycle's start was sent

    # --- lifecycle -----------------------------------------------------------

    def event(self, level: str, message: str, **data: Any) -> None:
        getattr(log, "warning" if level == "warn" else level, log.info)(message)
        self.store.add_event(self.clock.now(), level, message, **data)

    async def start(self) -> None:
        self.state.started_at = self.clock.now()
        if n := self.store.interrupted_runs_on_startup(self.clock.now()):
            self.event("warn", f"{n} run(s) were cut short by a restart and are marked failed")
        await self.link.open()
        self._tasks = [
            asyncio.create_task(self._connect_then_watch(), name="watch"),
            asyncio.create_task(self._worker(), name="worker"),
            asyncio.create_task(self._ticker(), name="ticker"),
        ]

    async def shutdown(self) -> None:
        # The current run's cancel handler sends stop, so give it time to finish.
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            await asyncio.wait({self._current_task}, timeout=60)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)
        await self.link.close()

    async def _connect_then_watch(self) -> None:
        while True:
            try:
                await self.link.identify()
                zones = await self.link.available_zones()
                self.state.available_zones = sorted(zones)
                break
            except LinkLockedOut as err:
                self.event("error", f"Controller rejected the password; nothing more will be sent. {err}")
                return
            except Exception as err:
                self.event("warn", f"Could not reach the controller, retrying in 1 min: {describe(err)}")
                await self.clock.sleep(60)
        self.event("info", f"Connected to {self.link.health.model}, firmware {self.link.health.firmware}",
                   zones=self.state.available_zones)
        await self._read_controller_programs()
        while True:
            if self._current_task is None or self._current_task.done():
                await self._poll_idle()
            await self.clock.sleep(IDLE_POLL_S)

    # --- controller's own programs (the fallback) ----------------------------

    async def _read_controller_programs(self) -> None:
        """Read the controller's own programs. Re-read daily: the suppression
        check depends on them, and a cyclic program's next day (from synchro)
        is only right as of the day it was read."""
        now = self.clock.now()
        try:
            schedule = await self.link.schedule()
        except LinkLockedOut:
            self.state.programs_read_for = now.date()
            return
        except Exception as err:
            self.state.programs_next_try = now + FALLBACK_RETRY
            self.event("warn", f"Could not read the controller's own programs, retrying in 5 min: {describe(err)}")
            return
        self.state.programs_read_for = now.date()
        today = now.date()
        programs = []
        for p in schedule.programs:
            if not p.starts or not p.durations:
                continue
            first_due = None
            if p.frequency == ProgramFrequency.CYCLIC and p.synchro is not None:
                first_due = today + dt.timedelta(days=p.synchro)
            programs.append(ControllerProgram(
                name=p.name,
                frequency=p.frequency.name,
                # pyrainbird counts Sunday as 0; plans use Monday = 0.
                weekdays=sorted((d.value - 1) % 7 for d in p.days_of_week),
                period=p.period,
                first_due=first_due,
                starts=[s.strftime("%H:%M") for s in p.starts],
                zones=[d.zone for d in p.durations],
            ))
        self.state.controller_programs = programs
        self._refresh_warnings()

    def _refresh_warnings(self) -> None:
        warnings = []
        if self.store.settings()["fallback_enabled"]:
            for p in self.state.controller_programs:
                early = [s for s in p.starts if dt.time.fromisoformat(s) < FALLBACK_RISK_BEFORE]
                if early:
                    warnings.append(
                        f"Controller {p.name} starts at {', '.join(early)}, before the daily rain delay "
                        f"is renewed at {FALLBACK_AT:%H:%M}, so it may water anyway.")
        self.state.warnings = warnings

    async def ensure_fallback(self) -> None:
        """Make sure a rain delay is holding the controller's programs back. Never lowers one."""
        now = self.clock.now()
        try:
            before = await self.link.rain_delay()
            if before == 0:
                ok = await self.link.set_rain_delay(1)
                self.state.rain_delay = 1 if ok else None
                self.event("info" if ok else "error",
                           "Set a 1-day rain delay to hold the controller's programs back"
                           if ok else "Tried to set a 1-day rain delay but it did not read back",
                           read_before=before)
                self.state.paused_by_rain_delay = False
            else:
                self.state.rain_delay = before
                self.state.paused_by_rain_delay = before >= 2
                if before >= 2:
                    self.event("warn", f"A {before}-day rain delay is set (not by this service); "
                                       "leaving it, and pausing plans until it ends", read_before=before)
                else:
                    # Logged daily so the countdown behaviour can be learned:
                    # still 1 just after midnight means it doesn't count down then.
                    self.event("info", "Rain delay already 1 day; left as is", read_before=before)
            self.state.fallback_done_for = now.date()
        except LinkLockedOut:
            self.state.fallback_done_for = now.date()
        except Exception as err:
            self.state.fallback_next_try = now + FALLBACK_RETRY
            self.event("warn", f"Could not check the rain delay, retrying in 5 min: {describe(err)}")

    def _check_suppression(self, now: dt.datetime) -> None:
        """After each controller program start time, say whether it watered.

        This is the real test of the fallback: the poller would have seen the
        controller's own program running despite the rain delay.
        """
        today = now.date()
        for p in self.state.controller_programs:
            for start in p.starts:
                key = (p.name, start, today)
                at = dt.datetime.combine(today, dt.time.fromisoformat(start), tzinfo=now.tzinfo)
                if key in self._suppression_checked or now < at + SUPPRESSION_CHECK_AFTER:
                    continue
                self._suppression_checked.add(key)
                if now > at + dt.timedelta(hours=1):
                    continue  # service wasn't up around then
                seen = [z for t, zones in self._external_seen if t >= at - dt.timedelta(minutes=1)
                        for z in zones if z in p.zones]
                due = p.due_on(today)
                if seen:
                    self.event("error", f"Controller {p.name} watered at {start} despite the rain delay: "
                                        "the fallback is NOT holding it back", zones=sorted(set(seen)))
                elif due:
                    self.event("info", f"Controller {p.name} was due at {start} (estimated) and did not "
                                       "water: the rain-delay fallback is holding it back")

    # --- watching ------------------------------------------------------------

    async def _poll_idle(self) -> None:
        try:
            zones = await self.link.running()
        except Exception:
            return
        self._record_running(zones, ours=False)

    def _record_running(self, zones: set[int], ours: bool) -> None:
        now = self.clock.now()
        previous = set(self.state.running)
        self.state.running = sorted(zones)
        self.state.running_read_at = now
        if not ours and zones:
            self._external_seen.append((now, sorted(zones)))
            self._external_seen = [(t, z) for t, z in self._external_seen if now - t < dt.timedelta(hours=2)]
            if zones != previous:
                self.event("warn", f"Zone(s) {sorted(zones)} running, not started by this service")
        elif not ours and previous and not zones:
            self.event("info", f"Zone(s) {sorted(previous)} stopped")

    # --- scheduling ----------------------------------------------------------

    async def _ticker(self) -> None:
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("tick failed")
            await self.clock.sleep(TICK_S)

    async def _tick(self) -> None:
        now = self.clock.now()
        self._end_hold(now.date())  # database only, so not waiting on the link
        if not self.link.health.connected:
            return
        settings = self.store.settings()
        if settings["fallback_enabled"]:
            due_today = now.time() >= FALLBACK_AT or self.state.fallback_done_for is None
            retry_ok = self.state.fallback_next_try is None or now >= self.state.fallback_next_try
            if self.state.fallback_done_for != now.date() and due_today and retry_ok:
                await self.ensure_fallback()
        programs_retry_ok = self.state.programs_next_try is None or now >= self.state.programs_next_try
        if (self.state.programs_read_for != now.date() and now.time() >= FALLBACK_AT
                and programs_retry_ok and (self._current_task is None or self._current_task.done())):
            await self._read_controller_programs()
        self._check_suppression(now)
        for plan in self.store.plans():
            self._maybe_start_plan(plan, now, settings["catch_up_minutes"])

    @staticmethod
    def plan_due_on(plan: dict[str, Any], day: dt.date) -> bool:
        days = plan["days"]
        if "weekdays" in days:
            return day.weekday() in days["weekdays"]
        anchor = dt.date.fromisoformat(days["anchor"])
        return (day - anchor).days >= 0 and (day - anchor).days % days["every"] == 0

    @staticmethod
    def plan_day(plan: dict[str, Any], day: dt.date, hold: dict[str, Any] | None) -> str | None:
        """What a plan does on a day: "due", "held" (due, but inside a weather
        hold), "resume" (the hold's first day back) or None.

        Every plan that is on waters on the resume day, whether or not its own
        rule says so, and every-N-day plans count from that day afterwards:
        otherwise an every-3 plan could water on the resume day and again the
        next day, when its old cycle comes round. The anchor is rewritten to
        match on the resume day (_end_hold); until then this works it out, so
        the calendar shows the new cycle as soon as a hold is set.
        """
        if hold is not None:
            set_on, resume_on = dt.date.fromisoformat(hold["set_on"]), dt.date.fromisoformat(hold["resume_on"])
            if set_on <= day < resume_on:
                return "held" if Service.plan_due_on(plan, day) else None
            if plan["enabled"] and day == resume_on:
                return "resume"
            if plan["enabled"] and day > resume_on and "every" in plan["days"]:
                return "due" if (day - resume_on).days % plan["days"]["every"] == 0 else None
        return "due" if Service.plan_due_on(plan, day) else None

    def _maybe_start_plan(self, plan: dict[str, Any], now: dt.datetime, catch_up_min: int) -> None:
        today = now.date()
        if not plan["enabled"] or plan["last_started_date"] == today.isoformat():
            return
        hold = self.active_hold()
        kind = self.plan_day(plan, today, hold)
        if kind is None:
            return
        start = dt.datetime.combine(today, dt.time.fromisoformat(plan["start_time"]), tzinfo=now.tzinfo)
        if now < start:
            return
        self.store.mark_plan_started(plan["id"], today)
        if kind == "held":
            # Recorded even if the service was down at the start time: the
            # hold, not the outage, is why it didn't water.
            reason = f"weather hold; plans start again {_day(hold['resume_on'])}"  # type: ignore[index]
            run_id = self.store.add_run("scheduled", plan["name"], now, plan["id"])
            self.store.update_run(run_id, status="skipped", finished_at=now, detail={"reason": reason})
            self.event("info", f"Skipped plan '{plan['name']}': {reason}")
            return
        if now > start + dt.timedelta(minutes=catch_up_min):
            self.event("warn", f"Plan '{plan['name']}' at {plan['start_time']} was missed "
                               f"(service was down or busy for over {catch_up_min} min)")
            return
        if self.state.paused_by_rain_delay:
            run_id = self.store.add_run("scheduled", plan["name"], now, plan["id"])
            self.store.update_run(run_id, status="skipped", finished_at=now,
                                  detail={"reason": f"{self.state.rain_delay}-day rain delay"})
            self.event("info", f"Skipped plan '{plan['name']}': {self.state.rain_delay}-day rain delay")
            return
        self.enqueue_plan(plan, "scheduled")

    def plan_saved(self, plan: dict[str, Any]) -> None:
        """A plan created or edited after today's start time begins at its next
        occurrence, rather than being reported as missed or started late."""
        now = self.clock.now()
        start = dt.datetime.combine(now.date(), dt.time.fromisoformat(plan["start_time"]), tzinfo=now.tzinfo)
        if self.plan_day(plan, now.date(), self.active_hold()) and now >= start:
            self.store.mark_plan_started(plan["id"], now.date())

    # --- weather hold --------------------------------------------------------
    # Held in this service, not with the controller's rain delay: the
    # fallback renews that nightly and reads a delay of 2+ as someone else's,
    # and the controller can't be told to water every plan on the day after.

    def active_hold(self) -> dict[str, Any] | None:
        """The hold while it still decides anything: up to and including its resume day."""
        hold = self.store.hold()
        if hold is None or self.clock.now().date() > dt.date.fromisoformat(hold["resume_on"]):
            return None
        return hold

    def set_hold(self, days: int) -> dict[str, Any]:
        """Hold today and the next days-1 days; plans start again on the day after.
        Setting it again while it holds changes the length; it still began when first set."""
        today = self.clock.now().date()
        current = self.active_hold()
        holding = current is not None and today < dt.date.fromisoformat(current["resume_on"])
        resume_on = today + dt.timedelta(days=days)
        hold = {"set_on": current["set_on"] if holding else today.isoformat(),  # type: ignore[index]
                "resume_on": resume_on.isoformat(), "reanchored": False}
        self.store.set_hold(hold)
        self.event("info", f"Weather hold: no watering through {_day(resume_on - dt.timedelta(days=1))}; "
                           f"plans start again {_day(resume_on)}", **hold)
        return hold

    def cancel_hold(self) -> bool:
        """Only before the resume day. Plans go back to their own days, as if
        there had been no hold; nothing makes up for the days already held."""
        hold = self.active_hold()
        if hold is None or self.clock.now().date() >= dt.date.fromisoformat(hold["resume_on"]):
            return False
        self.store.set_hold(None)
        self.event("info", "Weather hold cancelled: plans are back on their usual days")
        return True

    def _end_hold(self, today: dt.date) -> None:
        """On (or, after an outage, any day after) the resume day, restart each
        every-N-day plan's count from it, once."""
        hold = self.store.hold()
        if hold is None or hold.get("reanchored") or today < dt.date.fromisoformat(hold["resume_on"]):
            return
        resume_on = hold["resume_on"]
        for plan in self.store.plans():
            days = plan["days"]
            if plan["enabled"] and "every" in days and days["anchor"] != resume_on:
                self.store.save_plan({**plan, "days": {**days, "anchor": resume_on}}, plan["id"])
                self.event("info", f"Plan '{plan['name']}' now counts its every {days['every']} days from "
                                   f"{_day(resume_on)}, the first day after the weather hold",
                           old_anchor=days["anchor"])
        self.store.set_hold({**hold, "reanchored": True})

    def enqueue_plan(self, plan: dict[str, Any], kind: str) -> int:
        zones = [ZoneSpec(z["zone"], z["total_min"], z.get("cycle_min", 0), z.get("soak_min", 0))
                 for z in plan["zones"]]
        run_id = self.store.add_run(kind, plan["name"], self.clock.now(), plan["id"])
        self._enqueue(RunRequest(run_id, kind, plan["name"], zones, plan["adjust_pct"]))
        return run_id

    def enqueue_zone(self, zone: int, minutes: int) -> int:
        name = f"Zone {zone} for {minutes} min"
        run_id = self.store.add_run("zone", name, self.clock.now())
        # A manual run is exactly what was asked for: no seasonal adjust.
        self._enqueue(RunRequest(run_id, "zone", name, [ZoneSpec(zone, minutes)],
                                 apply_global_adjust=False))
        return run_id

    def _enqueue(self, req: RunRequest) -> None:
        self.state.queued.append({"run_id": req.run_id, "name": req.name, "kind": req.kind})
        self._queue.put_nowait(req)

    async def stop_all(self) -> bool:
        """Stop button: drop the queue, end the current run, stop the controller."""
        while not self._queue.empty():
            req = self._queue.get_nowait()
            self.store.update_run(req.run_id, status="stopped", finished_at=self.clock.now(),
                                  detail={"reason": "stopped before it started"})
        self.state.queued.clear()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()  # its own cleanup sends stop
            await asyncio.wait({self._current_task}, timeout=90)
            return not self.state.running
        stopped = await self.link.stop()
        self.event("info" if stopped else "error",
                   "Stop sent; nothing running" if stopped else "Stop sent but could not confirm")
        return stopped

    # --- running -------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            req = await self._queue.get()
            self.state.queued = [q for q in self.state.queued if q["run_id"] != req.run_id]
            self._current_task = asyncio.create_task(self._execute(req))
            # asyncio.wait, not await: the stop button cancels the run, and that
            # must not look like the worker itself being cancelled. Suppressing
            # CancelledError here used to swallow shutdown's cancel and hang.
            await asyncio.wait({self._current_task})
            self.state.current = None

    async def _execute(self, req: RunRequest) -> None:
        settings = self.store.settings()
        detail: dict[str, Any] = {"segments": [], "warnings": []}
        in_progress: Segment | None = None
        started = self.clock.now()
        self.store.update_run(req.run_id, status="running", started_at=started)
        self.state.current = {"run_id": req.run_id, "name": req.name, "kind": req.kind,
                              "phase": "checking", "started_at": started.isoformat()}

        def finish(status: str, message: str | None = None, level: str = "info") -> None:
            if message:
                detail["reason"] = message
                self.event(level, f"{req.name}: {message}")
            self.store.update_run(req.run_id, status=status, finished_at=self.clock.now(), detail=detail)

        def record_cut_cycle(result: str) -> None:
            """A cycle ended by a stop or an error never reaches the segment
            list on its own; record how long its valve was open, or the water
            totals quietly miss it."""
            if in_progress is None or self._segment_begun is None:
                return  # between cycles, or its start was never confirmed
            ran_s = min(self.clock.elapsed() - self._segment_begun, in_progress.minutes * 60)
            detail["segments"].append({
                "zone": in_progress.zone, "minutes": in_progress.minutes,
                "at_min": round(in_progress.start_min, 2), "result": result,
                "note": f"{result} {ran_s / 60:.1f} min into the cycle", "watered_min": round(ran_s / 60, 1)})

        try:
            global_pct = settings["global_adjust_pct"] if req.apply_global_adjust else 100
            adjusted = adjust_zones(req.zones, global_pct, req.plan_adjust_pct)
            detail["warnings"] = adjusted.warnings
            detail["adjust"] = {"global_pct": global_pct, "plan_pct": req.plan_adjust_pct}
            for w in adjusted.warnings:
                self.event("warn", f"{req.name}: {w}")
            if not adjusted.specs:
                return finish("skipped", "nothing to water after adjustment")

            if req.kind == "scheduled" and settings["skip_when_sensor_tripped"]:
                try:
                    tripped = await self.link.rain_sensor_tripped()
                    self.state.sensor_tripped = tripped
                    if tripped:
                        return finish("skipped", "rain sensor is tripped")
                except LinkLockedOut:
                    raise
                except Exception as err:
                    detail["warnings"].append(f"rain sensor not read: {describe(err)}")

            if not await self._wait_until_idle(req):
                return finish("failed", "another zone kept running for 30 min", "error")

            self.event("info", f"Started {req.name}")
            seq = Sequencer(adjusted.specs)
            t0 = self.clock.elapsed()

            def elapsed_min() -> float:
                return (self.clock.elapsed() - t0) / 60

            while (step := seq.next(elapsed_min())) is not None:
                if isinstance(step, Wait):
                    self._set_phase("soaking", seq, until_min=step.until_min, t0=t0)
                    await self.clock.sleep(max(0.0, (step.until_min - elapsed_min()) * 60))
                    continue
                self._set_phase("watering", seq, segment=step, t0=t0)
                in_progress = step
                self._segment_begun = None
                outcome = await self._run_segment(step)
                in_progress = None
                detail["segments"].append({"zone": step.zone, "minutes": step.minutes,
                                           "at_min": round(step.start_min, 2), **outcome})
                if outcome["result"] != "ok":
                    return finish("interrupted" if outcome["result"] == "interrupted" else "failed",
                                  outcome["note"], "warn")
                seq.done(step, elapsed_min())
            finish("done")
            self.event("info", f"Finished {req.name}", minutes=sum(s.total_min for s in adjusted.specs))
        except asyncio.CancelledError:
            # Stop button or shutdown. Shielded so the stop is sent even while cancelling.
            record_cut_cycle("stopped")
            stopped = await asyncio.shield(self._stop_quietly())
            finish("stopped", "stopped" + ("" if stopped else " (could not confirm the valve closed)"),
                   "info" if stopped else "error")
            raise
        except LinkLockedOut as err:
            record_cut_cycle("failed")
            finish("failed", f"controller rejected the password: {err}", "error")
        except Exception as err:
            log.exception("run failed")
            record_cut_cycle("failed")
            await self._stop_quietly()
            finish("failed", describe(err), "error")

    async def _stop_quietly(self) -> bool:
        try:
            stopped = await self.link.stop()
        except Exception:
            return False
        if stopped:
            self._record_running(set(), ours=True)
        return stopped

    def _set_phase(self, phase: str, seq: Sequencer, t0: float, segment: Segment | None = None,
                   until_min: float | None = None) -> None:
        cur = self.state.current or {}
        now_wall = self.clock.now()
        elapsed_s = self.clock.elapsed() - t0
        cur.update(phase=phase, remaining=seq.remaining)
        if segment is not None:
            cur.update(zone=segment.zone, minutes=segment.minutes,
                       ends_at=(now_wall + dt.timedelta(minutes=segment.minutes)).isoformat())
        else:
            cur.update(zone=None, minutes=None)
        if until_min is not None:
            cur["soak_until"] = (now_wall + dt.timedelta(seconds=until_min * 60 - elapsed_s)).isoformat()
        self.state.current = cur

    async def _wait_until_idle(self, req: RunRequest) -> bool:
        waited = 0.0
        announced = False
        while True:
            try:
                zones = await self.link.running()
                self._record_running(zones, ours=False)
                if not zones:
                    return True
                if not announced:
                    self.event("info", f"{req.name}: waiting for zone(s) {sorted(zones)} to finish first")
                    announced = True
            except LinkLockedOut:
                raise
            except Exception:
                pass
            if waited >= IDLE_WAIT_LIMIT_S:
                return False
            if self.state.current:
                self.state.current["phase"] = "waiting for the controller to be idle"
            await self.clock.sleep(IDLE_POLL_S)
            waited += IDLE_POLL_S

    async def _run_segment(self, seg: Segment) -> dict[str, Any]:
        # Timed from sending the start, not from its confirmation: the valve
        # opens on the command, and a slow read-back (20 s seen on 2026-10-29)
        # once made the controller's on-time stop look like an early one.
        begun = self.clock.elapsed()
        started = await self.link.start_zone(seg.zone, seg.minutes)
        if not started.running:
            if started.seen:
                self._external_seen.append((self.clock.now(), sorted(started.seen)))
                return {"result": "interrupted", "watered_min": 0.0,
                        "note": f"zone(s) {sorted(started.seen)} took over as zone {seg.zone} was "
                                "starting (from the app, the panel, or a controller program)"}
            # Unconfirmed: it may be running, so close the valve to be sure.
            await self._stop_quietly()
            return {"result": "failed", "watered_min": 0.0,
                    "note": f"zone {seg.zone} did not start (no confirmation)"}
        self._record_running({seg.zone}, ours=True)
        self._segment_begun = begun
        expected = seg.minutes * 60

        def into() -> float:
            return self.clock.elapsed() - begun

        def cut_short() -> float:
            # Found at a poll, so the valve closed (or was taken over) up to one
            # poll earlier: an upper bound, never more than the cycle asked for.
            return round(min(into(), expected) / 60, 1)

        # During the segment: only a successful read showing the wrong state counts.
        while into() < expected - 15:
            await self.clock.sleep(min(SEGMENT_POLL_S, expected - 15 - into()))
            try:
                zones = await self.link.running()
            except LinkLockedOut:
                raise
            except Exception:
                continue
            self._record_running(zones, ours=True)
            others = sorted(zones - {seg.zone})
            if others:
                self._external_seen.append((self.clock.now(), others))
                return {"result": "interrupted", "watered_min": cut_short(),
                        "note": f"zone(s) {others} started outside this service while zone {seg.zone} "
                                "was watering (from the app, the panel, or a controller program)"}
            if seg.zone not in zones:
                if into() >= expected - EARLY_STOP_TOLERANCE_S:
                    # Close enough to the end that it's the controller's own timer.
                    return {"result": "ok", "note": "", "took_s": round(into()), "watered_min": float(seg.minutes)}
                return {"result": "interrupted", "watered_min": cut_short(),
                        "note": f"zone {seg.zone} stopped {into() / 60:.1f} min into a {seg.minutes} min "
                                "cycle (stopped from the app or panel?)"}

        # The controller ends the segment itself; confirm, and stop it if it doesn't.
        await self.clock.sleep(max(0.0, expected - into()))
        while into() < expected + END_GRACE_S:
            try:
                zones = await self.link.running()
            except LinkLockedOut:
                raise
            except Exception:
                await self.clock.sleep(5)
                continue
            self._record_running(zones, ours=True)
            if seg.zone not in zones:
                # The controller's own timer closed it: exactly the minutes sent.
                return {"result": "ok", "note": "", "took_s": round(into()), "watered_min": float(seg.minutes)}
            await self.clock.sleep(5)
        stopped = await self._stop_quietly()
        self.event("warn", f"Zone {seg.zone} was still running {END_GRACE_S} s after its "
                           f"{seg.minutes} min; sent stop" + ("" if stopped else " (not confirmed)"))
        # Ran over: count the time it was really open (at least that, if the stop failed).
        return {"result": "ok" if stopped else "failed", "watered_min": round(into() / 60, 1),
                "note": "" if stopped else f"zone {seg.zone} did not stop", "took_s": round(into())}

    # --- status --------------------------------------------------------------

    def next_runs(self, days: int = 14) -> list[dict[str, Any]]:
        now = self.clock.now()
        hold = self.active_hold()
        out = []
        for plan in self.store.plans():
            if not plan["enabled"]:
                continue
            for offset in range(days):
                day = now.date() + dt.timedelta(days=offset)
                if self.plan_day(plan, day, hold) not in ("due", "resume"):
                    continue
                at = dt.datetime.combine(day, dt.time.fromisoformat(plan["start_time"]), tzinfo=now.tzinfo)
                if at <= now and plan["last_started_date"] == day.isoformat():
                    continue
                if at < now - dt.timedelta(minutes=self.store.settings()["catch_up_minutes"]):
                    continue
                out.append({"plan_id": plan["id"], "name": plan["name"], "at": at.isoformat()})
                break
        return sorted(out, key=lambda r: r["at"])

    def calendar(self, first: dt.date, days: int) -> dict[str, Any]:
        """What ran on past days and what is expected on the coming ones.

        Expected runs use today's seasonal adjust; a later change to it moves
        them. Plans that are off are included (flagged) so the page can show
        them faded. The controller's own programs are listed too, flagged as
        held back while the rain-delay fallback is on: those are the days the
        suppression check gets a real test.
        """
        now = self.clock.now()
        today = now.date()
        last = first + dt.timedelta(days=days - 1)
        settings = self.store.settings()
        delay = self.state.rain_delay or 0
        hold = self.active_hold()

        expected = []
        for plan in self.store.plans():
            if not plan["show_on_calendar"]:
                continue  # e.g. a test plan; its past runs still show from history
            pv = self.preview_plan(plan)
            zones = [{"zone": z["zone"], "minutes": z["total_min"]} for z in pv["zones"] if z["total_min"] > 0]
            start_time = dt.time.fromisoformat(plan["start_time"])
            for offset in range(days):
                day = first + dt.timedelta(days=offset)
                kind = self.plan_day(plan, day, hold)
                if day < today or kind is None:
                    continue
                at = dt.datetime.combine(day, start_time, tzinfo=now.tzinfo)
                if at <= now:
                    # Started already (its run is in `runs`), or past and no
                    # longer catchable, or off: either way not expected any more.
                    late = now - at <= dt.timedelta(minutes=settings["catch_up_minutes"])
                    if not (plan["enabled"] and late and plan["last_started_date"] != day.isoformat()):
                        continue
                # A delay of 2+ days pauses plans; it is re-read each night, so
                # this only holds while the delay stays as it is now.
                held = self.state.paused_by_rain_delay and (day - today).days < delay
                expected.append({
                    "plan_id": plan["id"], "name": plan["name"], "enabled": plan["enabled"],
                    "at": at.isoformat(),
                    "ends_at": (at + dt.timedelta(minutes=pv["duration_min"])).isoformat(),
                    "water_min": pv["water_min"], "zones": zones,
                    "held": "weather hold" if kind == "held" else f"{delay}-day rain delay" if held else None,
                    "resume": kind == "resume",
                })

        controller = []
        for p in self.state.controller_programs:
            for offset in range(days):
                day = first + dt.timedelta(days=offset)
                if day < today or not p.due_on(day):
                    continue  # due_on is None when the controller's rule can't be worked out
                for start in p.starts:
                    at = dt.datetime.combine(day, dt.time.fromisoformat(start), tzinfo=now.tzinfo)
                    if at > now:
                        controller.append({"name": p.name, "at": at.isoformat(), "zones": p.zones,
                                           "held": bool(settings["fallback_enabled"])})

        history = self.store.runs_between(first, last)
        runs = [{
            "id": r["id"], "plan_id": r["plan_id"], "name": r["name"], "kind": r["kind"],
            "status": r["status"], "queued_at": r["queued_at"], "started_at": r["started_at"],
            "finished_at": r["finished_at"], "detail": r["detail"],
        } for r in history]

        return {
            "now": now.isoformat(), "first": first.isoformat(), "last": last.isoformat(),
            "expected": sorted(expected, key=lambda e: e["at"]),
            "controller": sorted(controller, key=lambda e: e["at"]),
            "runs": runs,
            # The latest hold even once it has ended, so its days stay marked
            # looking back (until the next hold replaces it).
            "hold": self.store.hold(),
            # GPM is applied at read time, so changing a zone's GPM restates its past gallons.
            "usage": water_usage(history, self.store.zone_gpm()),
        }

    def preview_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        settings = self.store.settings()
        zones = [ZoneSpec(z["zone"], z["total_min"], z.get("cycle_min", 0), z.get("soak_min", 0))
                 for z in plan["zones"]]
        adjusted = adjust_zones(zones, settings["global_adjust_pct"], plan.get("adjust_pct", 100))
        segments = preview(adjusted.specs, gap_min=SEGMENT_GAP_MIN)
        end = max((s.start_min + s.minutes for s in segments), default=0)
        # The plan as written (no adjust), so the page can show both side by side.
        as_written = preview([z for z in zones if z.total_min > 0], gap_min=SEGMENT_GAP_MIN)
        return {
            "as_written": {
                "segments": [{"zone": s.zone, "start_min": round(s.start_min, 2), "minutes": s.minutes}
                             for s in as_written],
                "water_min": sum(z.total_min for z in zones),
                "duration_min": round(max((s.start_min + s.minutes for s in as_written), default=0), 1),
            },
            "segments": [{"zone": s.zone, "start_min": round(s.start_min, 2), "minutes": s.minutes}
                         for s in segments],
            "zones": [{"zone": s.zone, "total_min": s.total_min, "cycle_min": s.cycle_min,
                       "soak_min": s.soak_min} for s in adjusted.specs],
            "warnings": adjusted.warnings,
            "water_min": sum(s.total_min for s in adjusted.specs),
            "duration_min": round(end, 1),
            "adjust": {"global_pct": settings["global_adjust_pct"], "plan_pct": plan.get("adjust_pct", 100)},
        }

    def status(self) -> dict[str, Any]:
        h = self.link.health
        s = self.state
        return {
            "now": self.clock.now().isoformat(),
            "link": {
                "connected": h.connected, "model": h.model, "firmware": h.firmware,
                "last_ok": h.last_ok, "last_error": h.last_error, "last_error_at": h.last_error_at,
                "consecutive_failures": h.consecutive_failures, "auth_failed": h.auth_failed,
            },
            "running": s.running,
            "running_read_at": s.running_read_at.isoformat() if s.running_read_at else None,
            "rain_delay": s.rain_delay,
            "paused_by_rain_delay": s.paused_by_rain_delay,
            "sensor_tripped": s.sensor_tripped,
            "hold": self.active_hold(),
            "available_zones": s.available_zones,
            "controller_programs": [p.__dict__ | {"first_due": p.first_due.isoformat() if p.first_due else None}
                                    for p in s.controller_programs],
            "warnings": s.warnings,
            "current": s.current,
            "queued": s.queued,
            "next_runs": self.next_runs(),
            "settings": self.store.settings(),
        }
