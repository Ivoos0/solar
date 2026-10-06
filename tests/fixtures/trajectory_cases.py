"""Hand-computable trajectory fixtures. Expected values are constants derived
by hand (arithmetic in comments); nothing here calls the code under test
except to BUILD inputs (config, series slots).

All fixtures use 15-minute blocks starting 2026-06-02 10:00 Europe/Brussels.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import series

TZ = ZoneInfo("Europe/Brussels")
START = datetime(2026, 6, 2, 10, 0, tzinfo=TZ)
STEP = timedelta(minutes=15)


def site(capacity, max_charge_kw, max_discharge_kw, reserve_percent=10.0):
    return config.from_dict({
        "battery": {
            "capacity_kwh": capacity,
            "soc_sensor": "sensor.test_battery_soc",
            "reserve_percent": reserve_percent,
            "max_charge_kw": max_charge_kw,
            "max_discharge_kw": max_discharge_kw,
        },
        "alerts": {"address": "owner@example.com"},
    })


def starts(n):
    return [START + i * STEP for i in range(n)]


def solar_slots(values):
    return [series.ForecastSlot(t, v, False, 60)
            for t, v in zip(starts(len(values)), values)]


def usage_slots(values):
    return [series.UsageSlot(t, v, 7)
            for t, v in zip(starts(len(values)), values)]


# 15-min block = 0.25 h, so kW * 0.25 = kWh per block.
# Each case: cfg, start_percent, solar, usage, and the hand-derived answers.

# --- sunny_saturates ---------------------------------------------------
# capacity 4.0, reserve 10% = 0.4, charge 8 kW -> 2.0/block (never binds),
# discharge 4 kW -> 1.0/block. Start 50% = 2.0 kWh. Usage 0.5 every block.
# net = solar - usage = [0, 1, 2, 2, 2, 1, 0, -0.5]
# b0 charge 2.0 | b1 absorb 1.0 -> 3.0 | b2 headroom 1.0: absorb 1.0, spill 1.0
# -> 4.0 (FULL, saturation = block 2) | b3 spill 2.0 | b4 spill 2.0 | b5 spill 1.0
# | b6 4.0 | b7 draw 0.5 -> 3.5.
# total spill = 1+2+2+1 = 6.0 ; leftover = 3.5 - 0.4 = 3.1 ; no breach.
SUNNY_SATURATES = dict(
    cfg=site(4.0, 8.0, 4.0), start_percent=50.0,
    solar=[0.5, 1.5, 2.5, 2.5, 2.5, 1.5, 0.5, 0.0],
    usage=[0.5] * 8,
    saturation_index=2, total_spill=6.0, breach_index=None, leftover=3.1,
    end_charge=3.5,
)

# --- power_limited -----------------------------------------------------
# capacity 20, reserve 2.0, charge 4 kW -> 1.0/block, start 25% = 5.0. Usage 0.5.
# net = [0, 0, 3, 3, 0, 0, 0, 0]. b2: min(3, 1.0, headroom 15) = 1.0 absorbed,
# spill 2.0 -> 6.0. b3: same -> 7.0, spill 2.0. Spill 4.0 with 13 kWh headroom
# left: NEVER saturates. leftover = 7.0 - 2.0 = 5.0.
POWER_LIMITED = dict(
    cfg=site(20.0, 4.0, 4.0), start_percent=25.0,
    solar=[0.5, 0.5, 3.5, 3.5, 0.5, 0.5, 0.5, 0.5],
    usage=[0.5] * 8,
    saturation_index=None, total_spill=4.0, breach_index=None, leftover=5.0,
    end_charge=7.0,
)

# --- winter_breach -----------------------------------------------------
# capacity 10, reserve 1.0, discharge 4 kW -> 1.0/block, start 30% = 3.0. No solar.
# usage = [1.5, 0.5 x7].
# b0: need 1.5, draw min(1.5, 1.0, 2.0) = 1.0 -> 2.0, grid shortfall 0.5
# b1: draw 0.5 -> 1.5 | b2: draw 0.5 -> 1.0 = reserve (BREACH = block 2)
# b3..b7: nothing available, shortfall 0.5 each = 2.5.
# grid shortfall total = 0.5 + 2.5 = 3.0 ; discharged total = 1.0+0.5+0.5 = 2.0
# (3.0 start - 1.0 floor); usage total 5.0 = 2.0 + 3.0. leftover 0. spill 0.
WINTER_BREACH = dict(
    cfg=site(10.0, 5.0, 4.0), start_percent=30.0,
    solar=[0.0] * 8,
    usage=[1.5] + [0.5] * 7,
    saturation_index=None, total_spill=0.0, breach_index=2, leftover=0.0,
    end_charge=1.0, total_shortfall=3.0, total_discharged=2.0,
)

# --- balanced ----------------------------------------------------------
# solar == usage every block -> net 0, charge stays at start 50% of 10 = 5.0.
# leftover = 5.0 - 1.0 = 4.0. No saturation, breach or spill.
_BAL = [0.3, 0.6, 0.9, 0.9, 0.6, 0.3, 0.2, 0.2]
BALANCED = dict(
    cfg=site(10.0, 5.0, 5.0), start_percent=50.0,
    solar=list(_BAL), usage=list(_BAL),
    saturation_index=None, total_spill=0.0, breach_index=None, leftover=4.0,
    end_charge=5.0,
)

# --- saturate_then_drain ----------------------------------------------
# capacity 4.0, reserve 0.4, charge 2.0/block, discharge 1.0/block, start 50% = 2.0.
# usage 0.5. solar [2,2,0,0,2,0,0,0,0,0] -> net [1.5,1.5,-.5,-.5,1.5,-.5 x5]
# b0 absorb 1.5 -> 3.5 | b1 headroom 0.5: absorb 0.5, spill 1.0 -> 4.0 (FIRST
# saturation = block 1) | b2 -> 3.5 | b3 -> 3.0 | b4 headroom 1.0: absorb 1.0,
# spill 0.5 -> 4.0 (second crossing, must NOT be reported) | b5..b9: 5 x 0.5
# -> 1.5. spill = 1.0+0.5 = 1.5 ; leftover = 1.5 - 0.4 = 1.1.
SATURATE_THEN_DRAIN = dict(
    cfg=site(4.0, 8.0, 4.0), start_percent=50.0,
    solar=[2.0, 2.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    usage=[0.5] * 10,
    saturation_index=1, total_spill=1.5, breach_index=None, leftover=1.1,
    end_charge=1.5,
)

ALL = {
    "sunny_saturates": SUNNY_SATURATES,
    "power_limited": POWER_LIMITED,
    "winter_breach": WINTER_BREACH,
    "balanced": BALANCED,
    "saturate_then_drain": SATURATE_THEN_DRAIN,
}

# --- price_gap ---------------------------------------------------------
# 16 blocks (10:00-14:00). Hourly market prices for 10:00, 11:00, 13:00; the
# 12:00 hour is MISSING. Blocks 8..11 (12:00-12:45) therefore have no price.
# Expected market price per block index:
PRICE_GAP_RAW = [
    {"time": "2026-06-02T10:00:00+02:00", "price": 0.10},
    {"time": "2026-06-02T11:00:00+02:00", "price": 0.20},
    {"time": "2026-06-02T13:00:00+02:00", "price": 0.40},
]
PRICE_GAP_EXPECTED_MARKET = (
    [0.10] * 4 + [0.20] * 4 + [None] * 4 + [0.40] * 4)
# Solar/usage differ per block (0.1*(i+1) and 0.05*(i+1)) so a positional
# shift would be detectable. Big battery + fast charge: nothing clamps.
PRICE_GAP_CFG = site(50.0, 40.0, 40.0)
PRICE_GAP_SOLAR = [round(0.1 * (i + 1), 10) for i in range(16)]
PRICE_GAP_USAGE = [round(0.05 * (i + 1), 10) for i in range(16)]
