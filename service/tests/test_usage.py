"""Water totals: minutes per cycle, gallons from each zone's GPM.

The whole-run test checks the totals against the simulator's own record of
the run times it was sent, not against the service's run history that the
totals are computed from. The rest use numbers worked out by hand.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time

from rbservice.scheduler import water_usage, watered_minutes
from rbservice.store import Store

from test_service import Rig, plan, until


def run(run_id, started, segments):
    return {"id": run_id, "started_at": started, "queued_at": started, "detail": {"segments": segments}}


def seg(zone, minutes, result="ok", **extra):
    return {"zone": zone, "minutes": minutes, "result": result, **extra}


def test_gallons_follow_each_zones_gpm_and_unset_zones_are_named_not_zeroed():
    runs = [
        run(1, "2026-11-02T05:00:00-07:00", [seg(1, 5, watered_min=5.0), seg(2, 4, watered_min=4.0),
                                             seg(1, 5, watered_min=5.0)]),
        run(2, "2026-11-02T18:00:00-07:00", [seg(3, 10, watered_min=10.0)]),
        run(3, "2026-11-03T05:00:00-07:00", [seg(1, 6, watered_min=6.0)]),
    ]
    u = water_usage(runs, {1: 2.5, 2: 1.5})
    nov2 = u["days"]["2026-11-02"]
    # zone 1: 10 min x 2.5 = 25 gal; zone 2: 4 x 1.5 = 6; zone 3: no GPM
    assert nov2["minutes"] == 24.0
    assert nov2["gallons"] == 31.0
    assert nov2["zones"]["1"] == {"minutes": 10.0, "gallons": 25.0}
    assert nov2["zones"]["3"] == {"minutes": 10.0, "gallons": None}
    assert u["days"]["2026-11-03"] == {"minutes": 6.0, "gallons": 15.0, "zones": {"1": {"minutes": 6.0, "gallons": 15.0}}}
    assert u["no_gpm"] == [3]
    assert u["unknown"] == []


def test_cut_short_cycles_count_what_ran_and_old_records_are_flagged_not_guessed():
    assert watered_minutes(seg(1, 10, "interrupted", watered_min=3.5)) == 3.5
    assert watered_minutes(seg(1, 10, "failed", watered_min=0.0)) == 0.0
    # history from before watered_min existed
    assert watered_minutes(seg(1, 10, "ok")) == 10.0
    assert watered_minutes(seg(1, 10, "interrupted")) is None
    u = water_usage([run(7, "2026-10-29T07:02:00-07:00", [seg(7, 10, "interrupted")])], {7: 2.0})
    assert u["days"] == {}
    assert u["unknown"] == [{"run_id": 7, "day": "2026-10-29", "zone": 7, "result": "interrupted"}]


def test_naming_a_zone_keeps_its_gpm_and_the_reverse(tmp_path):
    s = Store(tmp_path / "z.db")
    s.set_zone_gpm(4, 2.25)
    s.set_zone_name(4, "Front strip")
    s.set_zone_gpm(5, 1.0)
    s.set_zone_name(4, "Front strip (east)")
    assert s.zone_gpm() == {4: 2.25, 5: 1.0}
    assert s.zone_names()[4] == "Front strip (east)"
    s.set_zone_gpm(4, None)
    assert s.zone_gpm() == {5: 1.0}
    assert s.zone_names()[4] == "Front strip (east)"


def test_totals_of_a_whole_run_match_what_the_controller_was_sent(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            rig.store.set_zone_gpm(1, 2.5)
            rig.store.set_zone_gpm(2, 1.2)
            # zone 1 in two cycles with a soak, then zone 2
            p = rig.store.save_plan(plan([(1, 4, 2, 1), (2, 3, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.run(run_id)["status"] not in ("queued", "running"), 30)
            assert rig.run(run_id)["status"] == "done"
            sent = {}
            for zone, minutes in rig.starts():
                sent[zone] = sent.get(zone, 0) + minutes
            assert sent == {1: 4, 2: 3}
            day = rig.clock.now().date()
            usage = rig.service.calendar(day, 1)["usage"]["days"][day.isoformat()]
            assert {int(z): v["minutes"] for z, v in usage["zones"].items()} == sent
            assert usage["gallons"] == round(4 * 2.5 + 3 * 1.2, 1)

    asyncio.run(go())


def test_stop_mid_cycle_counts_the_minutes_it_ran(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 10, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.service.state.running == [1], 10)
            sent = time.monotonic()
            await asyncio.sleep(1.5)  # ~3 plan minutes at 120x
            assert await rig.service.stop_all()
            ran_min = (time.monotonic() - sent) * rig.clock.speed / 60
            segs = rig.run(run_id)["detail"]["segments"]
            assert [s["result"] for s in segs] == ["stopped"]
            # Timed from the start being sent, which came a little before the
            # confirmation this test waited for: never less than measured here,
            # and never more than the cycle.
            assert ran_min - 0.2 <= segs[0]["watered_min"] <= 10
            assert segs[0]["watered_min"] < ran_min + 2

    asyncio.run(go())


def test_outside_start_records_the_part_that_ran(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            p = rig.store.save_plan(plan([(1, 10, 0, 0)], enabled=False))
            run_id = rig.service.enqueue_plan(p, "plan-now")
            await until(lambda: rig.service.state.running == [1], 10)
            await asyncio.sleep(1.0)  # ~2 plan minutes
            rig.sim.start_from_outside(5, 2)
            await until(lambda: rig.run(run_id)["status"] != "running", 20)
            s = rig.run(run_id)["detail"]["segments"][0]
            assert s["result"] == "interrupted"
            assert 1.5 <= s["watered_min"] < 10

    asyncio.run(go())
