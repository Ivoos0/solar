"""Battery state derived from capacity and charge percent. Pure - no I/O."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BatteryState:
    charge_percent: float
    stored_kwh: float
    headroom_kwh: float    # room left before the battery is full


def from_percent(charge_percent, config):
    """Build a BatteryState from a state-of-charge percentage.

    stored   = capacity * percent / 100
    headroom = capacity - stored
    The reserve itself only forbids exporting (veto V1) and the trajectory
    projects down to it; peak shaving may still draw on the charge under it
    (it stops at empty, V5).
    """
    if not 0 <= charge_percent <= 100:
        raise ValueError(
            "charge_percent=%r: must be within 0..100" % (charge_percent,))
    capacity = config.capacity_kwh
    stored = capacity * charge_percent / 100.0
    return BatteryState(
        charge_percent=charge_percent,
        stored_kwh=stored,
        headroom_kwh=capacity - stored,
    )


def unknown(config):
    """Placeholder BatteryState for a missing charge reading.

    The charge is set to the reserve floor (nothing to export, nothing to
    plan on). It exists only so the hold decision can be built and logged:
    the caller knows there is no reading (soc_known False) and publishes and
    records nothing from it. rules.decide(soc_known=False) holds on top of it.
    """
    return from_percent(config.reserve_percent, config)
