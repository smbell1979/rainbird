import pytest

from rbservice.planner import (
    Segment,
    Sequencer,
    Wait,
    ZoneSpec,
    adjust_zones,
    adjusted_minutes,
    preview,
)


def totals(segments):
    out: dict[int, int] = {}
    for s in segments:
        out[s.zone] = out.get(s.zone, 0) + s.minutes
    return out


def check_invariants(zones, segments):
    """Properties that must hold for any plan, checked independently of the sequencer."""
    spec = {z.zone: z for z in zones}
    # Every zone gets exactly its total.
    assert totals(segments) == {z.zone: z.total_min for z in zones if z.total_min}
    # No cycle longer than allowed.
    for s in segments:
        if spec[s.zone].cycle_min:
            assert s.minutes <= spec[s.zone].cycle_min
    # One zone at a time: no overlap.
    ordered = sorted(segments, key=lambda s: s.start_min)
    for a, b in zip(ordered, ordered[1:]):
        assert b.start_min >= a.start_min + a.minutes - 1e-9
    # Soak respected between a zone's own cycles.
    for zone in spec:
        own = [s for s in ordered if s.zone == zone]
        for a, b in zip(own, own[1:]):
            assert b.start_min >= a.start_min + a.minutes + spec[zone].soak_min - 1e-9


def test_rounding_half_up():
    assert adjusted_minutes(12, 50) == 6
    assert adjusted_minutes(13, 85) == 11  # 11.05
    assert adjusted_minutes(10, 85) == 9  # 8.5 -> 9
    assert adjusted_minutes(17, 85, 100) == 14  # 14.45
    assert adjusted_minutes(0, 150) == 0


def test_adjust_reports_zone_rounding_to_zero():
    result = adjust_zones([ZoneSpec(1, 10), ZoneSpec(2, 1)], global_pct=30)
    assert [s.zone for s in result.specs] == [1]
    assert result.specs[0].total_min == 3
    assert len(result.warnings) == 1 and "zone 2" in result.warnings[0]


def test_no_cycling_runs_each_zone_once_in_order():
    zones = [ZoneSpec(1, 13), ZoneSpec(2, 13), ZoneSpec(3, 9)]
    segs = preview(zones)
    assert [(s.zone, s.minutes) for s in segs] == [(1, 13), (2, 13), (3, 9)]
    check_invariants(zones, segs)


def test_unequal_zones_without_cycling_keep_plan_order():
    # PGM A as written: the controller runs 1 to 6 in order, whatever the lengths.
    zones = [ZoneSpec(z, t) for z, t in [(1, 11), (2, 11), (3, 8), (4, 14), (5, 14), (6, 14)]]
    segs = preview(zones)
    assert [s.zone for s in segs] == [1, 2, 3, 4, 5, 6]
    check_invariants(zones, segs)


def test_single_zone_cycle_and_soak_waits_out_the_soak():
    zones = [ZoneSpec(4, 17, cycle_min=6, soak_min=30)]
    segs = preview(zones)
    assert segs == [Segment(4, 0, 6), Segment(4, 36, 6), Segment(4, 72, 5)]
    check_invariants(zones, segs)


def test_zones_water_while_others_soak():
    zones = [ZoneSpec(1, 10, 5, 10), ZoneSpec(2, 10, 5, 10)]
    segs = preview(zones)
    assert [(s.zone, s.start_min, s.minutes) for s in segs] == [
        (1, 0, 5), (2, 5, 5), (1, 15, 5), (2, 20, 5),
    ]
    check_invariants(zones, segs)


def test_scotts_current_program_with_cycles():
    # PGM A as read from the controller, given cycling.
    zones = [ZoneSpec(z, t, 5, 20) for z, t in [(1, 13), (2, 13), (3, 9), (4, 17), (5, 17), (6, 17)]]
    segs = preview(zones, gap_min=0.25)
    check_invariants(zones, segs)
    end = max(s.start_min + s.minutes for s in segs)
    # 86 minutes of water; soaks overlap other zones, so it ends well under 2x.
    # Plan order (Scott's choice, so zones run 1, 2, 3... like the controller)
    # leaves the last zone a tail of cycles; ~132 min here.
    assert 86 <= end < 140


def test_sequencer_follows_real_elapsed_time():
    seq = Sequencer([ZoneSpec(1, 10, 5, 10)])
    first = seq.next(0)
    assert first == Segment(1, 0, 5)
    seq.done(first, 7.5)  # took longer than planned
    assert seq.next(8) == Wait(17.5)  # soak counts from the real end
    assert seq.next(17.5) == Segment(1, 17.5, 5)


def test_duplicate_zone_rejected():
    with pytest.raises(ValueError):
        Sequencer([ZoneSpec(1, 5), ZoneSpec(1, 5)])


@pytest.mark.parametrize("seed", range(200))
def test_random_plans_hold_invariants(seed):
    import random

    rng = random.Random(seed)
    zones = [
        ZoneSpec(z, rng.randint(1, 40), rng.choice([0, 0, rng.randint(1, 15)]), rng.randint(0, 45))
        for z in rng.sample(range(1, 13), rng.randint(1, 8))
    ]
    check_invariants(zones, preview(zones, gap_min=rng.choice([0, 0.3])))
