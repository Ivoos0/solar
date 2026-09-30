"""Battery state derived from capacity and charge percent. Pure - no I/O."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BatteryState:
    charge_percent: float
    stored_kwh: float
    usable_kwh: float      # stored above the reserve floor, never negative
    headroom_kwh: float    # room left before the battery is full
    is_stubbed: bool = True  # C-003: the charge reading is a stub this mission


def from_percent(charge_percent, config, is_stubbed=True):
    """Build a BatteryState from a state-of-charge percentage (FR-006).

    stored   = capacity * percent / 100
    usable   = max(0, stored - capacity * reserve_percent / 100)
    headroom = capacity - stored
    Below the reserve floor there is no usable energy (0.0, not negative).
    """
    if not 0 <= charge_percent <= 100:
        raise ValueError(
            "charge_percent=%r: must be within 0..100" % (charge_percent,))
    capacity = config.capacity_kwh
    stored = capacity * charge_percent / 100.0
    reserve = capacity * config.reserve_percent / 100.0
    return BatteryState(
        charge_percent=charge_percent,
        stored_kwh=stored,
        usable_kwh=max(0.0, stored - reserve),
        headroom_kwh=capacity - stored,
        is_stubbed=is_stubbed,
    )
