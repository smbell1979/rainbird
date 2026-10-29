"""The calendar: past runs from history, expected runs from the plans.

The expected days are checked against dates worked out by hand from each
plan's rule, not against plan_due_on, which the calendar itself uses.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from rbservice.clock import ScaledClock
from rbservice.scheduler import ControllerProgram, Service
from rbservice.store import Store

TZ = "America/Phoenix"
PHX = ZoneInfo(TZ)
NOW = dt.datetime(2026, 10, 29, 9, 0, tzinfo=PHX)  # a Thursday


def make(tmp, **settings) -> Service:
    store = Store(tmp / "c.db")
    if settings:
        store.update_settings(settings)
    # speed 1: "now" must not drift during the test
    return Service(store, None, ScaledClock(TZ, 1.0, NOW))  # type: ignore[arg-type]


def plan(name, start_time, days, zones, enabled=True, adjust_pct=100) -> dict:
    return {"name": name, "enabled": enabled, "start_time": start_time, "days": days,
            "adjust_pct": adjust_pct,
            "zones": [{"zone": z, "total_min": t, "cycle_min": 0, "soak_min": 0} for z, t in zones]}


def days_of(entries, name) -> list[str]:
    return [e["at"][:10] for e in entries if e["name"] == name]


def test_expected_days_follow_each_rule(tmp_path):
    svc = make(tmp_path)
    # every 3 days from Oct 30: Oct 30, Nov 2, 5, 8, ...
    svc.store.save_plan(plan("Every3", "05:00", {"every": 3, "anchor": "2026-10-30"}, [(1, 10)]))
    # Mon and Thu: today (Thu) at 06:00 is already past, 18:00 is still ahead
    svc.store.save_plan(plan("MonThuEarly", "06:00", {"weekdays": [0, 3]}, [(2, 5)]))
    svc.store.save_plan(plan("MonThuLate", "18:00", {"weekdays": [0, 3]}, [(2, 5)]))
    cal = svc.calendar(dt.date(2026, 10, 26), 14)  # Mon Oct 26 .. Sun Nov 8

    assert days_of(cal["expected"], "Every3") == ["2026-10-30", "2026-11-02", "2026-11-05", "2026-11-08"]
    # Mon Oct 26 is in the past; today's 06:00 has gone (3 h ago, past catch-up)
    assert days_of(cal["expected"], "MonThuEarly") == ["2026-11-02", "2026-11-05"]
    assert days_of(cal["expected"], "MonThuLate") == ["2026-10-29", "2026-11-02", "2026-11-05"]
    assert cal["first"] == "2026-10-26" and cal["last"] == "2026-11-08"


def test_expected_runs_use_the_adjust_and_flag_plans_that_are_off(tmp_path):
    svc = make(tmp_path, global_adjust_pct=85)
    svc.store.save_plan(plan("Off", "05:00", {"weekdays": list(range(7))}, [(1, 13), (2, 9)], enabled=False))
    first = svc.calendar(dt.date(2026, 10, 30), 1)["expected"][0]
    assert first["enabled"] is False
    # 13 x 85% = 11.05 -> 11; 9 x 85% = 7.65 -> 8
    assert first["zones"] == [{"zone": 1, "minutes": 11}, {"zone": 2, "minutes": 8}]
    assert first["water_min"] == 19
    assert first["at"] == "2026-10-30T05:00:00-07:00"
    assert first["ends_at"] > first["at"]


def test_a_long_rain_delay_holds_only_the_days_it_covers(tmp_path):
    svc = make(tmp_path)
    svc.store.save_plan(plan("Daily", "05:00", {"weekdays": list(range(7))}, [(1, 5)]))
    svc.state.rain_delay, svc.state.paused_by_rain_delay = 3, True
    held = {e["at"][:10]: e["held"] for e in svc.calendar(dt.date(2026, 10, 30), 4)["expected"]}
    # today is Oct 29: a 3-day delay covers Oct 29, 30, 31
    assert held == {"2026-10-30": "3-day rain delay", "2026-10-31": "3-day rain delay",
                    "2026-11-01": None, "2026-11-02": None}


def test_our_own_one_day_delay_holds_nothing(tmp_path):
    svc = make(tmp_path)
    svc.store.save_plan(plan("Daily", "05:00", {"weekdays": list(range(7))}, [(1, 5)]))
    svc.state.rain_delay, svc.state.paused_by_rain_delay = 1, False
    assert all(e["held"] is None for e in svc.calendar(dt.date(2026, 10, 30), 3)["expected"])


def test_past_runs_come_from_history_within_the_range(tmp_path):
    svc = make(tmp_path)
    s = svc.store
    for day, status in ((25, "done"), (27, "failed"), (29, "skipped"), (31, "done")):
        at = dt.datetime(2026, 10, day, 5, 0, tzinfo=PHX)
        run = s.add_run("scheduled", f"Oct {day}", at, None)
        s.update_run(run, status=status, started_at=at)
    runs = svc.calendar(dt.date(2026, 10, 26), 5)["runs"]  # Oct 26..30
    assert [(r["name"], r["status"]) for r in runs] == [("Oct 27", "failed"), ("Oct 29", "skipped")]


def test_controller_programs_show_as_held_while_the_fallback_is_on(tmp_path):
    svc = make(tmp_path)
    svc.state.controller_programs = [ControllerProgram(
        "PGM A", "CYCLIC", [], 3, dt.date(2026, 11, 1), ["05:00"], [1, 2, 3, 4, 5, 6])]
    cal = svc.calendar(dt.date(2026, 10, 29), 7)
    assert [(c["at"][:10], c["held"]) for c in cal["controller"]] == [("2026-11-01", True), ("2026-11-04", True)]

    svc.store.update_settings({"fallback_enabled": False})
    assert all(c["held"] is False for c in svc.calendar(dt.date(2026, 10, 29), 7)["controller"])


def test_a_plan_kept_off_the_calendar_hides_upcoming_runs_but_not_past_ones(tmp_path):
    svc = make(tmp_path)
    shown = svc.store.save_plan(plan("Main", "05:00", {"weekdays": list(range(7))}, [(1, 5)]))
    hidden = svc.store.save_plan({**plan("Zone test", "12:00", {"weekdays": list(range(7))}, [(1, 1)]),
                                  "show_on_calendar": False})
    assert hidden["show_on_calendar"] is False and shown["show_on_calendar"] is True
    at = dt.datetime(2026, 10, 28, 12, 0, tzinfo=PHX)
    svc.store.update_run(svc.store.add_run("plan-now", "Zone test", at, hidden["id"]), status="done", started_at=at)

    cal = svc.calendar(dt.date(2026, 10, 26), 7)
    assert {e["name"] for e in cal["expected"]} == {"Main"}
    assert [r["name"] for r in cal["runs"]] == ["Zone test"]

    # turning it back on brings the upcoming runs back
    svc.store.save_plan({**plan("Zone test", "12:00", {"weekdays": list(range(7))}, [(1, 1)])}, hidden["id"])
    assert "Zone test" in {e["name"] for e in svc.calendar(dt.date(2026, 10, 26), 7)["expected"]}


def test_a_database_from_before_the_flag_keeps_its_plans_on_the_calendar(tmp_path):
    import json
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    # the plans table as first deployed, without show_on_calendar
    db.execute("""CREATE TABLE plans (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, start_time TEXT NOT NULL, days TEXT NOT NULL,
        adjust_pct INTEGER NOT NULL DEFAULT 100, zones TEXT NOT NULL, last_started_date TEXT)""")
    db.execute("INSERT INTO plans (name, enabled, start_time, days, zones) VALUES (?, 0, '05:00', ?, ?)",
               ("Old", json.dumps({"weekdays": [0]}), json.dumps([{"zone": 1, "total_min": 5}])))
    db.commit()
    db.close()
    from rbservice.store import Store as S
    plans = S(path).plans()
    assert [(p["name"], p["show_on_calendar"]) for p in plans] == [("Old", True)]
    S(path)  # opening again must not try to add the column twice
