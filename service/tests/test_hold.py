"""The weather hold: no watering for N days, then every plan on the day after.

Expected days are worked out by hand from the calendar, not from plan_day.
NOW is Thu Oct 29 09:00; a 3-day hold set then covers Oct 29, 30 and 31 and
plans start again on Sun Nov 1.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from rbservice.clock import ScaledClock
from rbservice.scheduler import Service
from rbservice.store import Store

TZ = "America/Phoenix"
PHX = ZoneInfo(TZ)
NOW = dt.datetime(2026, 10, 29, 9, 0, tzinfo=PHX)  # a Thursday


def make(tmp) -> Service:
    return Service(Store(tmp / "h.db"), None, ScaledClock(TZ, 1.0, NOW))  # type: ignore[arg-type]


def at(svc: Service, *when: int) -> dt.datetime:
    """Move the service's clock (speed 1, so it stays put during the test)."""
    t = dt.datetime(*when, tzinfo=PHX)
    svc.clock = ScaledClock(TZ, 1.0, t)
    return t


def plan(name, start_time, days, enabled=True) -> dict:
    return {"name": name, "enabled": enabled, "start_time": start_time, "days": days, "adjust_pct": 100,
            "zones": [{"zone": 1, "total_min": 5, "cycle_min": 0, "soak_min": 0}]}


def calendar_days(svc: Service, name: str) -> dict[str, str]:
    """day -> 'held' / 'resume' / 'due' for one plan, Oct 29 .. Nov 8."""
    out = {}
    for e in svc.calendar(dt.date(2026, 10, 29), 11)["expected"]:
        if e["name"] == name:
            out[e["at"][:10]] = "held" if e["held"] else "resume" if e["resume"] else "due"
    return out


EVERY3 = {"every": 3, "anchor": "2026-10-30"}  # Oct 30, Nov 2, 5, 8 without a hold


def test_hold_holds_its_days_then_every_plan_starts_on_the_day_after(tmp_path):
    svc = make(tmp_path)
    svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    svc.store.save_plan(plan("MonThu", "18:00", {"weekdays": [0, 3]}))
    svc.store.save_plan(plan("Off3", "05:00", EVERY3, enabled=False))

    hold = svc.set_hold(3)
    assert (hold["set_on"], hold["resume_on"]) == ("2026-10-29", "2026-11-01")

    # resumes Sun Nov 1 (not one of its days), then every 3 days from there
    assert calendar_days(svc, "Every3") == {"2026-10-30": "held", "2026-11-01": "resume",
                                            "2026-11-04": "due", "2026-11-07": "due"}
    # today's 18:00 is held; Sun Nov 1 is extra; then its own Mondays and Thursdays
    assert calendar_days(svc, "MonThu") == {"2026-10-29": "held", "2026-11-01": "resume",
                                            "2026-11-02": "due", "2026-11-05": "due"}
    # a plan that is off is held like the rest but keeps its own cycle
    assert calendar_days(svc, "Off3") == {"2026-10-30": "held", "2026-11-02": "due",
                                          "2026-11-05": "due", "2026-11-08": "due"}


def test_coming_up_agrees_with_the_calendar(tmp_path):
    svc = make(tmp_path)
    svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    svc.store.save_plan(plan("MonThu", "18:00", {"weekdays": [0, 3]}))
    svc.set_hold(3)
    assert [(r["name"], r["at"]) for r in svc.next_runs()] == [
        ("Every3", "2026-11-01T05:00:00-07:00"), ("MonThu", "2026-11-01T18:00:00-07:00")]


def test_held_day_records_a_skip_and_resume_day_waters(tmp_path):
    svc = make(tmp_path)
    p = svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    svc.set_hold(3)

    now = at(svc, 2026, 10, 30, 5, 1)
    svc._maybe_start_plan(svc.store.plan(p["id"]), now, 30)
    run = svc.store.runs()[0]
    assert run["status"] == "skipped" and "Sun Nov 1" in run["detail"]["reason"]
    assert svc.state.queued == []

    now = at(svc, 2026, 11, 1, 5, 1)
    svc._end_hold(now.date())
    svc._maybe_start_plan(svc.store.plan(p["id"]), now, 30)
    assert [q["name"] for q in svc.state.queued] == ["Every3"]
    assert svc.store.runs()[0]["status"] == "queued"


def test_resume_day_rewrites_the_anchor_once(tmp_path):
    svc = make(tmp_path)
    p = svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    off = svc.store.save_plan(plan("Off3", "05:00", EVERY3, enabled=False))
    svc.set_hold(3)

    svc._end_hold(dt.date(2026, 10, 31))  # still held: nothing changes
    assert svc.store.plan(p["id"])["days"]["anchor"] == "2026-10-30"

    at(svc, 2026, 11, 1, 0, 1)
    svc._end_hold(dt.date(2026, 11, 1))
    assert svc.store.plan(p["id"])["days"] == {"every": 3, "anchor": "2026-11-01"}
    assert svc.store.plan(off["id"])["days"]["anchor"] == "2026-10-30"  # off: left alone
    assert svc.store.hold()["reanchored"] is True

    # once past, the plan's own (rewritten) rule gives the same days, and a
    # later edit to it is not overridden by the old hold
    at(svc, 2026, 11, 2, 9, 0)
    assert svc.active_hold() is None
    assert [r["at"][:10] for r in svc.next_runs()] == ["2026-11-04"]
    svc.store.save_plan({**svc.store.plan(p["id"]), "days": {"every": 3, "anchor": "2026-11-03"}}, p["id"])
    svc._end_hold(dt.date(2026, 11, 3))
    assert svc.store.plan(p["id"])["days"]["anchor"] == "2026-11-03"


def test_anchor_is_rewritten_after_an_outage_over_the_resume_day(tmp_path):
    svc = make(tmp_path)
    p = svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    svc.set_hold(3)
    at(svc, 2026, 11, 3, 8, 0)  # service was down Nov 1 and 2
    svc._end_hold(dt.date(2026, 11, 3))
    assert svc.store.plan(p["id"])["days"]["anchor"] == "2026-11-01"  # Nov 4, 7: the same cycle


def test_cancel_puts_plans_back_on_their_own_days(tmp_path):
    svc = make(tmp_path)
    svc.store.save_plan(plan("Every3", "05:00", EVERY3))
    svc.set_hold(3)
    assert svc.cancel_hold() is True
    assert svc.store.hold() is None
    assert calendar_days(svc, "Every3") == {"2026-10-30": "due", "2026-11-02": "due",
                                            "2026-11-05": "due", "2026-11-08": "due"}
    assert svc.cancel_hold() is False  # nothing left to cancel


def test_cannot_cancel_on_the_resume_day(tmp_path):
    svc = make(tmp_path)
    svc.set_hold(3)
    at(svc, 2026, 11, 1, 6, 0)
    assert svc.active_hold() is not None  # still deciding today's runs
    assert svc.cancel_hold() is False


def test_setting_again_changes_the_length_but_not_the_start(tmp_path):
    svc = make(tmp_path)
    svc.set_hold(3)
    at(svc, 2026, 10, 30, 12, 0)
    hold = svc.set_hold(5)  # Oct 30 + 5: resumes Wed Nov 4
    assert (hold["set_on"], hold["resume_on"]) == ("2026-10-29", "2026-11-04")

    # after it has ended, a new hold starts the day it is set
    at(svc, 2026, 11, 10, 12, 0)
    hold = svc.set_hold(1)
    assert (hold["set_on"], hold["resume_on"]) == ("2026-11-10", "2026-11-11")


def test_settings_page_cannot_write_or_see_the_hold(tmp_path):
    svc = make(tmp_path)
    svc.set_hold(2)
    assert "weather_hold" not in svc.store.settings()
    with pytest.raises(ValueError):
        svc.store.update_settings({"weather_hold": None})
    assert svc.store.hold() is not None


def test_plan_saved_during_a_hold_after_its_time_is_not_reported_missed(tmp_path):
    svc = make(tmp_path)
    svc.set_hold(3)
    p = svc.store.save_plan(plan("Daily", "06:00", {"weekdays": list(range(7))}))
    svc.plan_saved(p)  # 06:00 today has passed, and today is held
    svc._maybe_start_plan(svc.store.plan(p["id"]), svc.clock.now(), 30)
    assert svc.store.runs() == []
    assert not [e for e in svc.store.events() if "missed" in e["message"]]
