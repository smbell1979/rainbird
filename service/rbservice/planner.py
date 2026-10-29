"""Cycle-and-soak sequencing.

Pure logic with no I/O and no clock, so it can be tested exhaustively and used
both to run a plan and to preview one on the page.

The controller runs exactly one zone at a time, and a new start replaces
whatever is running (measured on this ESP-TM2), so a plan is a sequence of
single-zone segments. Whenever the valve is free, the first zone in the plan's
order whose soak has finished and that still has watering left goes next, so
the others water while each one soaks. Plan order (not "most left first") so a
plan without cycling runs exactly like the controller: zone 1, then 2, ...
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ZoneSpec:
    zone: int
    total_min: int
    """Minutes to water in this run, after seasonal adjust."""
    cycle_min: int = 0
    """Most minutes in one go. 0 means no cycling: the whole total at once."""
    soak_min: int = 0
    """Least minutes between the end of one cycle and the start of the next."""


@dataclass(frozen=True)
class Segment:
    zone: int
    start_min: float
    """Offset from the start of the run."""
    minutes: int


@dataclass(frozen=True)
class Wait:
    until_min: float


def adjusted_minutes(base_min: int, *percents: int) -> int:
    """Apply percentages to a run time, rounding half up to whole minutes.

    The controller only accepts whole minutes. Integer arithmetic so 12 min at
    50% is exactly 6, not 5.999...
    """
    numerator = base_min
    denominator = 1
    for pct in percents:
        numerator *= pct
        denominator *= 100
    return (2 * numerator + denominator) // (2 * denominator)


@dataclass
class AdjustedZones:
    specs: list[ZoneSpec]
    warnings: list[str] = field(default_factory=list)


def adjust_zones(
    zones: list[ZoneSpec], global_pct: int, plan_pct: int = 100
) -> AdjustedZones:
    """Scale each zone's total, reporting (never silently dropping) zones that round to 0."""
    result = AdjustedZones(specs=[])
    for z in zones:
        total = adjusted_minutes(z.total_min, global_pct, plan_pct)
        if z.total_min > 0 and total == 0:
            result.warnings.append(
                f"zone {z.zone}: {z.total_min} min at {global_pct}% x {plan_pct}% rounds to "
                "0 min, so it will not water"
            )
            continue
        if total > 0:
            result.specs.append(ZoneSpec(z.zone, total, z.cycle_min, z.soak_min))
    return result


class Sequencer:
    """Decides the next segment given how far into the run we actually are.

    The executor feeds back real elapsed time after each segment, so slow
    commands or a late start shift the schedule rather than shortening a soak.
    """

    def __init__(self, zones: list[ZoneSpec]) -> None:
        seen: set[int] = set()
        for z in zones:
            if z.zone in seen:
                raise ValueError(f"zone {z.zone} appears twice in one plan")
            seen.add(z.zone)
            if z.total_min < 0 or z.cycle_min < 0 or z.soak_min < 0:
                raise ValueError(f"zone {z.zone}: minutes cannot be negative")
        self._order = [z.zone for z in zones]
        self._spec = {z.zone: z for z in zones}
        self._remaining = {z.zone: z.total_min for z in zones}
        self._ready_at = {z.zone: 0.0 for z in zones}

    @property
    def remaining(self) -> dict[int, int]:
        return dict(self._remaining)

    def next(self, now_min: float) -> Segment | Wait | None:
        pending = [z for z in self._order if self._remaining[z] > 0]
        if not pending:
            return None
        ready = [z for z in pending if self._ready_at[z] <= now_min]
        if not ready:
            return Wait(min(self._ready_at[z] for z in pending))
        zone = ready[0]  # plan order
        cycle = self._spec[zone].cycle_min or self._remaining[zone]
        return Segment(zone, now_min, min(cycle, self._remaining[zone]))

    def done(self, segment: Segment, end_min: float) -> None:
        self._remaining[segment.zone] -= segment.minutes
        self._ready_at[segment.zone] = end_min + self._spec[segment.zone].soak_min


def preview(zones: list[ZoneSpec], gap_min: float = 0.0) -> list[Segment]:
    """The run as planned, assuming each segment takes exactly its minutes.

    gap_min adds the time between one zone stopping and the next being started
    and confirmed, to make the estimated finish honest.
    """
    seq = Sequencer(zones)
    now = 0.0
    segments: list[Segment] = []
    while (step := seq.next(now)) is not None:
        if isinstance(step, Wait):
            now = step.until_min
            continue
        segments.append(step)
        now += step.minutes
        seq.done(step, now)
        now += gap_min
    return segments
