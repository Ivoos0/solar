"""Block-by-block battery charge projection. Pure - no I/O, no clock.

Projects what happens if the planner does NOTHING (the baseline the selectors
reason against). Recomputed every cycle from live charge; never cached or
stored (FR-034).

Definitions (per block, all kWh):
  usage_covered_kwh   solar consumed directly by the household:
                      min(solar, usage). Never battery or grid energy.
  absorbed_kwh        surplus stored in the battery.
  spilled_kwh         surplus that could not be stored -> reaches the grid.
  discharged_kwh      energy the battery supplies to the household.
  grid_shortfall_kwh  usage neither solar nor battery could supply (battery
                      at the reserve floor or discharge-power limited) ->
                      imported from the grid. Recorded so nothing vanishes.

Two identities hold in every block (see tests):
  solar == usage_covered + absorbed + spilled
  usage == usage_covered + discharged + grid_shortfall

Absorption is bound by TWO independent ceilings plus the surplus itself:
  absorbed = min(surplus, max_charge_kw * block_hours, headroom)
so spill can occur while headroom remains (charge-power limit).

If the battery starts below the reserve floor it is NOT lifted to the floor
(that would create energy): it simply cannot discharge until it climbs back.
"""
from dataclasses import dataclass
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_TOL_KWH = 1e-6


class TrajectoryError(Exception):
    """Input series are misaligned or invalid (a data error, never zero-filled)."""


@dataclass(frozen=True)
class TrajectoryBlock:
    block_start: datetime
    solar_kwh: float
    usage_kwh: float
    usage_covered_kwh: float
    absorbed_kwh: float
    spilled_kwh: float
    discharged_kwh: float
    grid_shortfall_kwh: float
    has_price: bool
    projected_charge_kwh: float
    projected_percent: float


@dataclass(frozen=True)
class Trajectory:
    blocks: list
    saturation_block: object   # datetime | None - FIRST block reaching capacity
    total_spill_kwh: float     # summed over ALL blocks
    reserve_breach_block: object  # datetime | None - FIRST block at the floor
    leftover_kwh: float        # charge above reserve at the final block, >= 0
    horizon_end: object        # datetime | None


def _utc(dt):
    return dt.astimezone(timezone.utc)


def _index(entries, kind):
    """{utc instant: kwh}. Duplicates and non-finite/negative energy raise.

    Keys are UTC instants: PEP 495 makes two ZoneInfo datetimes in the repeated
    fall-back hour compare and hash equal, so aware datetimes must never be
    used as join keys.
    """
    out = {}
    for e in entries:
        t = _utc(e.block_start)
        if t in out:
            raise TrajectoryError("duplicate %s block %s" % (kind, e.block_start))
        kwh = e.expected_kwh
        if not math.isfinite(kwh) or kwh < 0:
            raise TrajectoryError(
                "%s energy at %s must be finite and >= 0, got %r"
                % (kind, e.block_start, kwh))
        out[t] = kwh
    return out


def project(battery_state, solar_series, usage_series, price_map, config,
            start_time=None):
    """Project charge over the dense price-horizon grid.

    The grid steps config.block_minutes apart in absolute (UTC) time from
    start_time (default: the earliest solar/usage block) up to, excluding, the
    price horizon_end (end of the last priced block). With no prices it ends
    at the end of the solar series (else usage). Nothing at or beyond
    horizon_end is projected; solar/usage entries past it are ignored.

    Solar, usage and price are all joined by UTC instant, never by aware
    datetime equality. A grid block with no solar entry (beyond the forecast
    coverage, or a hole) has 0.0 solar. A grid block with no usage entry -
    inside the usage span or beyond its end - raises TrajectoryError (usage is
    contractually dense to the horizon). A duplicate instant in solar or
    usage, or negative / NaN / inf energy, raises, as does an empty window
    (nothing to derive a start or horizon from). A solar entry inside the
    projection window whose start is not on the block grid (misaligned, e.g. a
    :07 start) raises rather than being silently dropped; entries outside the
    window (before start_time, at or beyond horizon_end) are ignored. A block absent from price_map
    is still projected with has_price False (FR-040).
    TrajectoryBlock.block_start is an aware local datetime derived from the
    UTC instant.
    """
    tz = ZoneInfo(config.timezone)
    step = timedelta(minutes=config.block_minutes)
    solar_by = _index(solar_series, "solar")
    usage_by = _index(usage_series, "usage")
    price_utc = {_utc(k) for k in price_map}

    if start_time is not None:
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=tz)
        first = _utc(start_time)
    else:
        known = list(solar_by) + list(usage_by)
        if not known:
            raise TrajectoryError("no solar or usage data and no start_time")
        first = min(known)

    if price_utc:
        end = max(price_utc) + step
    elif solar_by:
        end = max(solar_by) + step
    elif usage_by:
        end = max(usage_by) + step
    else:
        raise TrajectoryError("cannot determine the horizon")

    grid = []
    t = first
    while t < end:
        grid.append(t)
        t += step
    if not grid:
        raise TrajectoryError(
            "empty projection window (start %s, end %s)" % (first, end))
    on_grid = set(grid)
    off_grid = sorted(t for t in solar_by
                      if first <= t < end and t not in on_grid)
    if off_grid:
        raise TrajectoryError(
            "solar block %s is off the %d-minute grid (start %s); it would be "
            "silently dropped" % (off_grid[0].astimezone(tz),
                                  config.block_minutes, first.astimezone(tz)))

    capacity = config.capacity_kwh
    reserve = capacity * config.reserve_percent / 100.0
    hours = config.block_minutes / 60.0
    max_in = config.max_charge_kw * hours
    max_out = config.max_discharge_kw * hours

    charge = battery_state.stored_kwh
    blocks = []
    saturation = None
    breach = None
    spill_total = 0.0

    for tu in grid:
        t = tu.astimezone(tz)
        if tu not in usage_by:
            raise TrajectoryError("no usage entry for block %s" % t)
        solar = solar_by.get(tu, 0.0)
        usage = usage_by[tu]

        covered = min(solar, usage)
        surplus = solar - covered
        deficit = usage - covered
        absorbed = spilled = discharged = shortfall = 0.0

        if surplus > 0:
            headroom = max(0.0, capacity - charge)
            absorbed = min(surplus, max_in, headroom)
            spilled = surplus - absorbed
            charge += absorbed
        elif deficit > 0:
            available = max(0.0, charge - reserve)
            discharged = min(deficit, max_out, available)
            shortfall = deficit - discharged
            charge -= discharged

        spill_total += spilled
        if saturation is None and charge >= capacity - _TOL_KWH:
            saturation = t
        if breach is None and charge <= reserve + _TOL_KWH:
            breach = t

        blocks.append(TrajectoryBlock(
            block_start=t,
            solar_kwh=solar,
            usage_kwh=usage,
            usage_covered_kwh=covered,
            absorbed_kwh=absorbed,
            spilled_kwh=spilled,
            discharged_kwh=discharged,
            grid_shortfall_kwh=shortfall,
            has_price=tu in price_utc,
            projected_charge_kwh=charge,
            projected_percent=charge / capacity * 100.0,
        ))

    leftover = max(0.0, charge - reserve)
    return Trajectory(
        blocks=blocks,
        saturation_block=saturation,
        total_spill_kwh=spill_total,
        reserve_breach_block=breach,
        leftover_kwh=leftover,
        horizon_end=end.astimezone(tz),
    )
