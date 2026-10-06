"""Vetoes, selectors and the resolution loop. Every expectation is a
hand-derived constant (arithmetic in comments); nothing is computed by the
code under test.

Default site (conftest): capacity 10 kWh, reserve 10 % (1.0 kWh), max charge
and discharge 5 kW, round-trip efficiency 0.90, billing floor 2.5 kW, 15-minute
blocks (0.25 h). Trajectories are built by hand so each test controls exactly
the facts a selector reads; one test at the end uses the real project().
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dataclasses

import pytest

import battery
import capacity
import prices
import rules
import trajectory
from fixtures import trajectory_cases as fx
import rules as _rules



def establish_vetoes(*a, usage_history_available=True, **kw):
    """The existing V1-V3 expectations assume a usable history (V4 quiet)."""
    return _rules.establish_vetoes(
        *a, usage_history_available=usage_history_available, **kw)


def decide(*a, usage_history_available=True, **kw):
    return _rules.decide(
        *a, usage_history_available=usage_history_available, **kw)


TZ = ZoneInfo("Europe/Brussels")
T0 = datetime(2026, 6, 2, 10, 0, tzinfo=TZ)
STEP = timedelta(minutes=15)

# Grid states (5 min elapsed -> 10 min = 1/6 h remain; budget =
# (ceiling*0.25 - energy) * 6, capped at max_charge_kw 5.0; ceiling =
# max(floor 2.5, month_peak)).
#   CALM      peak 5.0 -> allowance 1.25; energy 0.1: (1.25-0.1)*6 = 6.9 ->
#             capped 5.0. offtake 1.0 <= floor -> shave 0.
#   TIGHT     peak 2.5 -> allowance 0.625; energy 0.425: 0.2*6 = 1.2 kW.
#   EXHAUSTED energy 0.7: (0.625-0.7)*6 = -0.45 (V3). Energy 0.625 -> 0.0.
#   Household draw is subtracted from the budget (budget = allowed offtake
#   rate - offtake), so the legacy budget figures below hold for offtake 0.0
#   (grid() default). A peak-shaving state can never have charge budget left.
#   SHAVE     offtake 4.0, energy 0.3: projected = 0.3 + 4.0/6 = 0.96667 kWh
#             -> 3.867 kW avg > 2.5. needed = (0.96667-0.625)*6 = 2.05 kW,
#             clamped to offtake-floor = 1.5 kW (<= max_discharge 5) -> 1.5.
#             budget = (0.625-0.3)*6 - 4.0 = -2.05 kW (V3 fires).


def grid(offtake=0.0, energy=0.1, peak=5.0, avg=1.2):
    return capacity.GridState(
        offtake_kw=offtake, window_start=T0, window_energy_kwh=energy,
        elapsed_minutes=5.0, running_average_kw=avg, month_peak_kw=peak,
        is_restored=False)


CALM = grid()
TIGHT = grid(energy=0.425, peak=2.5)
EXHAUSTED = grid(energy=0.7, peak=2.5)
SHAVE = grid(offtake=4.0, energy=0.3, peak=2.5, avg=3.6)

FLAT = (0.20, 0.02)     # consumption, injection: nothing attractive anywhere


def blk(i, solar=0.0, usage=0.0, priced=True, shortfall=0.0, charge=5.0):
    return trajectory.TrajectoryBlock(
        block_start=T0 + i * STEP, solar_kwh=solar, usage_kwh=usage,
        usage_covered_kwh=min(solar, usage), absorbed_kwh=0.0,
        spilled_kwh=0.0, discharged_kwh=0.0, grid_shortfall_kwh=shortfall,
        has_price=priced, projected_charge_kwh=charge,
        projected_percent=charge * 10.0)


def price(i, cons, inj):
    return prices.PricePoint(T0 + i * STEP, 0.0, cons, inj, 15)


def go(cfg, plist, now_i=0, pct=50.0, g=CALM, sat=None, breach=None,
       spill=0.0, leftover=0.0, solar=None, usage=None, shortfall=None,
       hide=(), history=True, forecast=True, soc_known=True, charges=None):
    # history: True/False -> usage_history_available; None -> omit the kwarg
    """Build trajectory + price_map by hand and decide.

    plist: (consumption, injection) per block, or None = no price published.
    hide: blocks whose map entry exists but whose trajectory has_price=False.
    """
    n = len(plist)
    solar = solar or {}
    usage = usage or {}
    shortfall = shortfall or {}
    charges = charges or {}
    blocks = [blk(i, solar.get(i, 0.0), usage.get(i, 0.0),
                  priced=(plist[i] is not None and i not in hide),
                  shortfall=shortfall.get(i, 0.0),
                  charge=charges.get(i, 5.0)) for i in range(n)]
    traj = trajectory.Trajectory(
        blocks=blocks,
        saturation_block=None if sat is None else blocks[sat].block_start,
        total_spill_kwh=spill,
        reserve_breach_block=None if breach is None
        else blocks[breach].block_start,
        leftover_kwh=leftover, horizon_end=T0 + n * STEP)
    pmap = {T0 + i * STEP: price(i, *p) for i, p in enumerate(plist)
            if p is not None}
    kw = {} if history is None else {"usage_history_available": history}
    if soc_known is not True:
        kw["soc_known"] = soc_known
    if forecast is not True:
        kw["forecast_available"] = forecast
    return _rules.decide(traj, pmap, battery.from_percent(pct, cfg), g, cfg,
                         T0 + now_i * STEP + timedelta(minutes=1), **kw)


# ---- vetoes ---------------------------------------------------------------

@pytest.mark.parametrize("pct,fires", [
    (10.0, True),     # exactly the 10 % reserve floor: <= not <
    (9.99, True),     # below
    (0.0, True),
    (10.01, False),   # just above
    (50.0, False),
])
def test_v1_boundary(site_config, pct, fires):
    forbidden, fired = establish_vetoes(
        battery.from_percent(pct, site_config), price(0, 0.2, 0.05),
        site_config)
    assert ("V1" in fired) is fires
    # the reserve limits EXPORT only; V5 (empty battery) is the sole discharge
    # lower bound
    assert forbidden == (({"export"} | ({"discharge"} if pct <= 0 else set()))
                         if fires else set())
    assert ("V5" in fired) is (pct <= 0)


@pytest.mark.parametrize("inj,fires", [
    (-0.001, True), (0.0, False), (0.05, False)])
def test_v2_boundary_zero_does_not_fire(site_config, inj, fires):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, inj),
        site_config)
    assert ("V2" in fired) is fires
    # V2 forbids export ONLY: discharge-to-house stays available.
    assert forbidden == ({"export"} if fires else set())
    assert "discharge" not in forbidden


def test_v1_and_v2_together(site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(10, site_config), price(0, 0.2, -0.01),
        site_config)
    assert fired == ["V1", "V2"]
    assert forbidden == {"export"}


def test_v5_battery_empty_forbids_discharge_only(site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(0.0, site_config), price(0, 0.2, 0.05),
        site_config)
    assert fired == ["V1", "V5"]
    assert forbidden == {"discharge", "export"}
    # just above empty: only V1 fires, discharge stays allowed
    forbidden, fired = establish_vetoes(
        battery.from_percent(0.1, site_config), price(0, 0.2, 0.05),
        site_config)
    assert fired == ["V1"] and forbidden == {"export"}


def test_v5_fires_on_zero_stored_kwh_alone(site_config):
    class Empty:
        charge_percent = 5.0
        stored_kwh = 0.0
    forbidden, fired = establish_vetoes(Empty(), None, site_config)
    assert "V5" in fired and "discharge" in forbidden


def test_s0_peak_shaves_below_the_reserve(site_config):
    # 5 % is under the 10 % reserve: export is forbidden, shaving is not
    d = go(site_config, [FLAT] * 3, g=SHAVE, pct=5.0)
    assert (d.selector, d.action, d.target_power_kw) == (
        "S0", "discharge", pytest.approx(1.5))
    assert d.vetoes_fired == ["V1", "V3"] and d.suppressed == []


def test_s0_stops_when_the_battery_is_empty(site_config):
    d = go(site_config, [FLAT] * 3, g=SHAVE, pct=0.0)
    assert d.selector == "S6" and d.action == "idle"
    assert d.vetoes_fired == ["V1", "V3", "V5"]
    assert d.suppressed == [("S0", "discharge", "V5")]


def test_export_is_still_forbidden_below_the_reserve(site_config):
    # spill ahead, now is the best injection block, but 5 % <= reserve: V1.
    # There is no charge above the reserve to export, so S3 proposes nothing.
    d = go(site_config, [(0.2, 0.05), (0.2, 0.01)], pct=5.0, spill=1.0)
    assert "V1" in d.vetoes_fired
    assert d.action == "idle"


@pytest.mark.parametrize("g,fires", [
    (EXHAUSTED, True),    # budget (0.625-0.7)*6 = -0.45
    (grid(energy=0.625, peak=2.5), True),    # budget exactly 0.0
    (TIGHT, False),       # budget 1.2
    (CALM, False),        # budget 5.0
    (None, False),        # no grid state: capacity logic inactive
])
def test_v3_budget_boundary(site_config, g, fires):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config, g)
    assert ("V3" in fired) is fires
    assert forbidden == ({"grid_charge"} if fires else set())


def test_v3_inactive_when_capacity_disabled(site_config):
    cfg = replace(site_config, capacity_enabled=False)
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, cfg), price(0, 0.2, 0.05), cfg, EXHAUSTED)
    assert fired == [] and forbidden == frozenset()


def test_v2_leaves_discharge_to_house_allowed(site_config):
    # Injection negative but a peak is forming: S0 discharges to the house.
    d = go(site_config, [(0.20, -0.05)] * 3, g=SHAVE)
    assert d.vetoes_fired == ["V2", "V3"]     # V3: household eats the allowance
    assert (d.selector, d.action, d.target_power_kw) == ("S0", "discharge", 1.5)
    assert d.suppressed == []


# ---- S0 -------------------------------------------------------------------

def test_s0_fires_with_exact_shave_power(site_config):
    d = go(site_config, [FLAT] * 3, g=SHAVE)
    assert (d.selector, d.action) == ("S0", "discharge")
    assert d.target_power_kw == pytest.approx(1.5)   # see SHAVE arithmetic
    assert "1.50" in d.reasoning and "ceiling" in d.reasoning


def test_s0_does_not_fire_below_billing_floor(site_config):
    # offtake 2.0 <= floor 2.5 -> shave_kw 0.0 -> S1 gets it.
    g = grid(offtake=2.0, energy=0.1, peak=2.5)    # budget 3.15 - 2.0 = 1.15
    d = go(site_config, [(-0.05, 0.02)] * 3, g=g)
    assert d.selector == "S1" and d.suppressed == []


def test_s0_tried_before_s1(site_config):
    # Both would fire: shave 1.5 kW and consumption price negative.
    d = go(site_config, [(-0.05, 0.02)] * 3, g=SHAVE)
    assert (d.selector, d.action, d.target_power_kw) == (
        "S0", "discharge", pytest.approx(1.5))


def test_s0_vetoed_by_v1_and_v3_leaves_idle(site_config):
    d = go(site_config, [(-0.05, 0.02)] * 3, pct=10.0, g=SHAVE)
    # below the reserve S0 still shaves (V1 forbids export only); S1 would
    # be vetoed by V3 but S0 comes first
    assert d.vetoes_fired == ["V1", "V3"]
    assert (d.selector, d.action) == ("S0", "discharge")
    assert d.suppressed == []
    # empty battery: V5 stops the shave, V3 leaves S1 no budget either
    d = go(site_config, [(-0.05, 0.02)] * 3, pct=0.0, g=SHAVE)
    assert d.vetoes_fired == ["V1", "V3", "V5"]
    assert d.suppressed[0] == ("S0", "discharge", "V5")
    assert ("S1", "charge", "V3") in d.suppressed
    assert d.selector == "S6" and d.action == "idle"
    assert d.target_power_kw == 0.0


# ---- S1 -------------------------------------------------------------------

def test_s1_fires_at_minus_one_milli_not_at_zero(site_config):
    d = go(site_config, [(-0.001, 0.02), FLAT], g=CALM)
    assert (d.selector, d.action) == ("S1", "charge")
    assert d.target_power_kw == 5.0
    assert "-0.0010" in d.reasoning
    d0 = go(site_config, [(0.0, 0.02), FLAT], g=CALM)
    assert d0.selector != "S1"


def test_s1_capped_by_budget_and_says_so(site_config):
    d = go(site_config, [(-0.05, 0.02)] * 2, g=TIGHT)   # budget 1.2 kW
    assert d.selector == "S1"
    assert d.target_power_kw == pytest.approx(1.2)
    assert "capped" in d.reasoning and "1.20" in d.reasoning


def test_s1_generous_budget_full_power(site_config):
    d = go(site_config, [(-0.05, 0.02)] * 2, g=CALM)
    assert d.target_power_kw == 5.0 and "capped" not in d.reasoning


def test_s1_uncapped_when_no_grid_state(site_config):
    d = go(site_config, [(-0.05, 0.02)] * 2, g=None)
    assert d.target_power_kw == 5.0


# ---- V3 and the inverter's default behaviour ----------------------------

def test_v3_blocks_grid_charge_and_leaves_solar_to_the_default(site_config):
    # Budget exhausted. Grid charge (S1, cons -0.05) is suppressed by V3 ...
    d = go(site_config, [(-0.05, 0.02)] * 2, g=EXHAUSTED)
    assert d.vetoes_fired == ["V3"]
    # S5 also proposes a grid charge (later 0.02*0.9 = 0.018 > -0.05): vetoed.
    assert d.suppressed == [("S1", "charge", "V3"), ("S4", "charge", "V3")]
    assert d.selector == "S6" and d.action == "idle"
    # ... and surplus solar asks for nothing: the inverter stores it by default.
    d2 = go(site_config, [(0.20, 0.02), (0.20, 0.10)], g=EXHAUSTED,
            solar={0: 1.0}, usage={0: 0.4})
    assert d2.vetoes_fired == ["V3"]
    assert (d2.selector, d2.action, d2.target_power_kw) == ("S6", "idle", 0.0)


# ---- surplus solar is the inverter's default, not a rule ------------------

def test_sunny_with_room_and_positive_price_is_idle_not_a_forced_charge(
        site_config):
    # Surplus 1.0 - 0.4 = 0.6 kWh, battery 50 % (headroom), positive prices,
    # a better injection price later: the old S2 forced a charge here. The
    # inverter charges from surplus on its own, so the planner sends idle.
    d = go(site_config, [(0.20, 0.05), (0.20, 0.20)],
           solar={0: 1.0}, usage={0: 0.4})
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)
    assert d.vetoes_fired == [] and d.suppressed == []
    assert "S2" not in d.reasoning
    assert "default behaviour" in d.reasoning


def test_no_selector_is_named_s2(site_config):
    assert not hasattr(_rules, "_s2")
    assert "S2" not in [n for n, _ in _rules._SELECTORS]
    assert not hasattr(_rules, "FORBID_SOLAR_CHARGE")
    assert not hasattr(_rules.Proposal("idle", 0.0, "r"), "charge_source")


@pytest.mark.parametrize("plist", [
    [(0.15, -0.02), (0.15, -0.02)],      # flat negative
    [(0.15, -0.02), (0.15, -0.019)],     # later slightly better
    [(0.15, -0.02), (0.15, -0.03)],      # later worse
    [(0.15, -0.02)],                     # last horizon block
])
def test_negative_injection_neither_exports_nor_forces_a_charge(
        site_config, plist):
    # Block 0 solar 1.0, usage 0.4, spill ahead 2.0: V2 forbids export, and
    # storing the surplus is the inverter's own default -> idle.
    d = go(site_config, plist, solar={0: 1.0}, usage={0: 0.4}, spill=2.0)
    assert d.vetoes_fired == ["V2"]
    assert (d.selector, d.action) == ("S6", "idle")


def test_nonnegative_injection_surplus_is_also_idle(site_config):
    d = go(site_config, [(0.15, 0.0), (0.15, 0.0)], solar={0: 1.0},
           usage={0: 0.4})
    assert (d.selector, d.action) == ("S6", "idle")


# ---- REGRESSION scenarios -------------------------------------------------

def test_regression_negative_injection_does_not_end_the_chain(site_config):
    """V2 forbids export and evaluation CONTINUES (it does not end the chain).

    Hand numbers: battery 50 % (headroom 5.0). Block 0: solar 1.0, usage 0.4.
    Injection now -0.02 (V2 fires); later 0.10. S3 sees a later block with a
    better price and stands aside; S5 needs 0.10 * 0.90 = 0.09 > consumption
    0.15, which fails. Nothing applies: idle, and the inverter's default
    stores the solar surplus. Nothing is exported at the negative price.
    """
    d = go(site_config, [(0.15, -0.02), (0.15, 0.05), (0.15, 0.10)],
           solar={0: 1.0}, usage={0: 0.4}, spill=2.0)
    assert d.vetoes_fired == ["V2"]
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.action != "export"


def test_regression_reserve_floor_does_not_stop_negative_price_charging(
        site_config):
    """V1 forbids discharge, not the whole chain: at the reserve floor with a
    negative consumption price the battery must still take free energy.

    Hand numbers: 10 % = the 10 % reserve floor -> V1. Consumption -0.05 ->
    S1 charges at min(max_charge 5.0, budget 5.0) = 5.0 kW.
    """
    d = go(site_config, [(-0.05, 0.05), FLAT], pct=10.0, g=CALM)
    assert d.vetoes_fired == ["V1"]
    assert (d.selector, d.action) == ("S1", "charge")
    assert d.target_power_kw == 5.0


# ---- S3 -------------------------------------------------------------------

# 8 blocks; injection: b0 .10 b1 .15 b2 .12 b3 .11 b4 .10 b5 .09 b6 .30 b7 .08
S3_INJ = [0.10, 0.15, 0.12, 0.11, 0.10, 0.09, 0.30, 0.08]
S3_PRICES = [(0.40, i) for i in S3_INJ]     # consumption high: no S5/S1


def test_s3_window_bounded_by_saturation(site_config):
    # saturation at block 3 -> window blocks 0..2, best .15 at block 1.
    # A global search would pick block 6 (.30) and NOT export at block 1.
    d = go(site_config, S3_PRICES, now_i=1, sat=3, spill=2.0)
    assert (d.selector, d.action, d.target_power_kw) == ("S3", "export", 5.0)
    assert "0.1500" in d.reasoning and "spill ahead 2.00" in d.reasoning
    # ... and block 0 (.10) is not the best of its window.
    d0 = go(site_config, S3_PRICES, now_i=0, sat=3, spill=2.0)
    assert d0.selector == "S6"
    # The best price after saturation (block 6, .30) is ignored: from block 6
    # itself the window collapses to just block 6 (saturation is behind now).
    d6 = go(site_config, S3_PRICES, now_i=6, sat=3, spill=2.0)
    assert d6.selector == "S3"


def test_s3_needs_a_saturation_ahead(site_config):
    # a spill without the battery filling up is a rate limit: exporting
    # stored energy would not save any of it
    d = go(site_config, S3_PRICES, now_i=6, sat=None, spill=2.0)
    assert d.selector == "S6"


def test_s3_never_sells_leftover_charge(site_config):
    # charge left at the horizon end, no spill: kept, whatever the price
    d = go(site_config, S3_PRICES, now_i=6, spill=0.0, leftover=4.1)
    assert d.selector == "S6"
    d = go(site_config, S3_PRICES, now_i=6, sat=None, spill=0.0, leftover=4.1)
    assert d.selector == "S6"


def test_s3_not_without_spill_or_leftover(site_config):
    d = go(site_config, S3_PRICES, now_i=6)
    assert d.selector == "S6"


def test_s3_tie_with_window_best_fires(site_config):
    d = go(site_config, [(0.4, 0.15), (0.4, 0.15), (0.4, 0.10)], now_i=1,
           sat=2, spill=1.0)
    assert d.selector == "S3"


def test_s3_imminent_saturation_uses_current_block(site_config):
    # saturation IS the current block: window is just now -> export.
    d = go(site_config, S3_PRICES, now_i=1, sat=1, spill=1.0)
    assert d.selector == "S3"


def test_s3_skips_unpriced_blocks(site_config):
    # Block 2 (would be .50, the window's best) has no price: skipped, so
    # block 1 (.15) is the best PRICED block of the window 0..2.
    plist = [(0.60, i) for i in S3_INJ]     # 0.50*0.9 = 0.45 < 0.60: no S5
    plist[2] = (0.60, 0.50)
    d = go(site_config, plist, now_i=1, sat=3, spill=2.0, hide={2})
    assert d.selector == "S3"
    # Same when the map has no entry at all.
    plist[2] = None
    d2 = go(site_config, plist, now_i=1, sat=3, spill=2.0)
    assert d2.selector == "S3"
    # Control: if block 2 counted, block 1 would not be the best.
    plist[2] = (0.60, 0.50)
    d3 = go(site_config, plist, now_i=1, sat=3, spill=2.0)
    assert d3.selector == "S6"


def test_s3_no_price_for_current_block(site_config):
    plist = list(S3_PRICES)
    plist[1] = None
    d = go(site_config, plist, now_i=1, sat=3, spill=2.0)
    assert d.selector == "S6" and "no price published" in d.reasoning


def test_s3_does_not_export_into_a_coming_shortage(site_config):
    # C4: the projection falls to the reserve at block 3 (1.0 kWh) before the
    # saturation at block 5, and the house buys at 0.40 later. Nothing is
    # spare, so S3 stays quiet however good the injection price is.
    charges = {0: 6.0, 1: 4.0, 2: 2.0, 3: 1.0, 4: 3.0, 5: 10.0}
    d = go(site_config, S3_PRICES, now_i=1, sat=5, spill=2.0, charges=charges)
    assert d.selector == "S6" and d.action == "idle"


def test_s3_exports_only_what_is_not_needed(site_config):
    # lowest projected charge before saturation is 1.5 kWh: 0.5 kWh is spare,
    # a 0.25 h block at 2.0 kW. Not the 5 kW the inverter could do.
    charges = {0: 6.0, 1: 4.0, 2: 1.5, 3: 10.0}
    d = go(site_config, S3_PRICES, now_i=1, sat=3, spill=2.0, charges=charges)
    assert (d.selector, d.action) == ("S3", "export")
    assert d.target_power_kw == pytest.approx(2.0)
    assert "0.50 kWh is not needed" in d.reasoning


# ---- S4 -------------------------------------------------------------------

# consumption b0 .30 b1 .20 b2 .25 b3 .35 | b4 .60 b5 .20 | b6, b7 .05
# (b4..b7 are after the breach at b4). injection tiny (0.02) so arbitrage (S5)
# never applies: 0.018 < any cons.
S4_PRICES = [(0.30, 0.02), (0.20, 0.02), (0.25, 0.02), (0.35, 0.02),
             (0.60, 0.02), (0.20, 0.02), (0.05, 0.02), (0.05, 0.02)]
# shortfall total 2.0 kWh; per block 5 kW * 0.25 = 1.25 -> N = ceil(1.6) = 2.
# The shortfall blocks b4 (.60) and b5 (.20), 1 kWh each, are what importing
# at the breach costs: (0.60 + 0.20) / 2 = 0.40 EUR/kWh. Charging now costs
# price / 0.9 (the default efficiency): .20 -> .222, .25 -> .278, .30 -> .333,
# .35 -> .389: all below .40, so the cheapest-N rule decides, as before.
S4_SHORT = {4: 1.0, 5: 1.0}


@pytest.mark.parametrize("now_i,fires,frag", [
    # window is [now, breach): N = 2 (shortfall 2.0 / 1.25 per block)
    (0, False, None),   # window .30 .20 .25 .35 -> cutoff .25; .30 too dear
    (1, True, ("2 of 3", "0.2500")),   # window .20 .25 .35 -> cutoff .25
    (2, True, ("2 of 2", "0.3500")),   # window .25 .35 -> both qualify
    (3, True, ("1 of 1", "0.3500")),   # last chance: window is just now
])
def test_s4_cheapest_n_before_breach(site_config, now_i, fires, frag):
    d = go(site_config, S4_PRICES, now_i=now_i, pct=30.0, breach=4,
           shortfall=S4_SHORT)
    assert (d.selector == "S4") is fires
    if fires:
        assert d.action == "charge"
        assert d.target_power_kw == 5.0
        assert "cheapest " + frag[0] in d.reasoning
        assert "cutoff " + frag[1] in d.reasoning


def test_s4_ignores_cheaper_prices_after_breach(site_config):
    # b4..b7 at .05 are cheaper but arrive after the breach at block 4.
    d = go(site_config, S4_PRICES, now_i=0, pct=30.0, breach=4,
           shortfall=S4_SHORT)
    assert d.selector != "S4"
    d5 = go(site_config, S4_PRICES, now_i=5, pct=30.0, breach=4,
            shortfall=S4_SHORT)
    # breach is behind now: window is the current block -> acts now.
    assert d5.selector == "S4"


def test_s4_counts_only_the_shortage_until_the_refill(site_config):
    # M2: breach at b4 (1 kWh short), full again at b5, a second, much bigger
    # shortage at b7 that charging now cannot cover. N is 1, not 9: only the
    # cheapest block before the breach (b1, .20) charges.
    kw = dict(pct=30.0, breach=4, shortfall={4: 1.0, 7: 10.0},
              charges={5: 10.0})
    assert go(site_config, S4_PRICES, now_i=0, **kw).selector != "S4"
    d = go(site_config, S4_PRICES, now_i=1, **kw)
    assert d.selector == "S4" and "cheapest 1 of" in d.reasoning


def test_s4_import_price_ignores_a_shortage_after_the_refill(site_config):
    # The later shortage is bought at .05 (b7) but is a different shortage:
    # the import S4 avoids is the one at b4 (.60), so .30 / 0.9 qualifies.
    kw = dict(pct=30.0, breach=4, shortfall={4: 1.0, 7: 10.0},
              charges={5: 10.0})
    d = go(site_config, S4_PRICES, now_i=1, **kw)
    assert d.selector == "S4" and "vs 0.6000 importing" in d.reasoning


def test_s4_not_without_breach(site_config):
    d = go(site_config, S4_PRICES, now_i=1, pct=30.0, breach=None)
    assert d.selector == "S6" and "no reserve breach" in d.reasoning


def test_s4_n_capped_at_window_size(site_config):
    # shortfall 100 kWh -> N huge, capped to 4 blocks: every window block ok.
    d = go(site_config, S4_PRICES, now_i=3, pct=30.0, breach=4,
           shortfall={4: 100.0})
    assert d.selector == "S4"


def test_s4_skips_unpriced_blocks(site_config):
    # Block 1 (.01, would be cheapest) is unpriced. Priced window: .30 .25
    # .35 -> N=2 cutoff .30 -> block 0 (.30) fires. If block 1 counted the
    # cheapest two would be .01 and .25, and block 0 would not fire.
    plist = list(S4_PRICES)
    plist[1] = (0.01, 0.02)
    d = go(site_config, plist, now_i=0, pct=30.0, breach=4,
           shortfall=S4_SHORT, hide={1})
    assert d.selector == "S4"
    d2 = go(site_config, plist, now_i=0, pct=30.0, breach=4,
            shortfall=S4_SHORT)
    assert d2.selector != "S4"


def test_s4_grid_charge_capped_by_budget(site_config):
    d = go(site_config, S4_PRICES, now_i=1, pct=30.0, breach=4,
           shortfall=S4_SHORT, g=TIGHT)
    assert d.selector == "S4" and d.target_power_kw == pytest.approx(1.2)
    assert "capped" in d.reasoning


def test_s4_vetoed_by_v3_when_no_budget(site_config):
    d = go(site_config, S4_PRICES, now_i=1, pct=30.0, breach=4,
           shortfall=S4_SHORT, g=EXHAUSTED)
    assert d.suppressed == [("S4", "charge", "V3")]
    assert d.selector == "S6"


# ---- S4, sale use (was S5) -------------------------------------------------

@pytest.mark.parametrize("later,fires", [
    (0.25, True),      # 0.25*0.9 = 0.225 > 0.20, spread 0.025
    (0.2223, True),    # 0.2223*0.9 = 0.20007 > 0.20
    (0.22, False),     # profitable BEFORE losses (0.22 > 0.20), 0.198 after
    (0.201, False),    # 1 % spread is nowhere near enough
])
def test_s4_sale_efficiency_threshold(site_config, later, fires):
    d = go(site_config, [(0.20, 0.02), (0.30, later)])
    assert (d.selector == "S4") is fires
    if fires:
        assert d.action == "charge"
        assert d.target_power_kw == 5.0
        assert "10:15" in d.reasoning        # names the target block


def test_s4_sale_reasoning_names_spread(site_config):
    d = go(site_config, [(0.20, 0.02), (0.30, 0.25)])
    assert "0.2250" in d.reasoning and "0.0250" in d.reasoning


def test_s4_sale_needs_headroom(site_config):
    d = go(site_config, [(0.20, 0.02), (0.30, 0.25)], pct=100.0)
    assert d.selector == "S6"


def test_s4_sale_never_proposes_loss_making_cycle_on_negative_prices(site_config):
    # negative later injection can never beat a positive consumption price
    d = go(site_config, [(0.20, -0.05), (0.30, -0.01)])
    assert d.selector == "S6"


def test_s4_sale_grid_charge_capped(site_config):
    d = go(site_config, [(0.20, 0.02), (0.30, 0.25)], g=TIGHT)
    assert d.selector == "S4" and d.target_power_kw == pytest.approx(1.2)


# ---- S6 -------------------------------------------------------------------

def test_s6_reasoning_names_salient_facts(site_config):
    d = go(site_config, [(0.20, 0.02), (0.20, 0.03)])
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)
    for fact in ("no spill ahead", "no reserve breach", "spread too narrow",
                 "leftover 0.00", "grid budget"):
        assert fact in d.reasoning
    assert d.vetoes_fired == [] and d.suppressed == []


# ---- fall-through ---------------------------------------------------------

def test_negative_injection_positive_consumption_band(site_config):
    # injection -0.01 while consumption +0.20, battery 50 %, spill ahead:
    # S3 proposes export (now -0.01 beats later -0.05) but V2 forbids it;
    # evaluation advances (S4, S5 do not apply) and lands on S6.
    d = go(site_config, [(0.20, -0.01), (0.20, -0.05)], spill=1.0, sat=1)
    assert d.vetoes_fired == ["V2"]
    assert d.suppressed == [("S3", "export", "V2")]
    assert (d.selector, d.action) == ("S6", "idle")
    assert "V2" in d.reasoning


def test_vetoed_s3_advances_to_next_selector_not_first(site_config):
    # S3 vetoed by V2, then S4 (NOT S1, NOT out of the loop) fires:
    # breach at block 3 (importing there costs .60), consumption now .20 is
    # cheapest of window 0..2 and .20 / 0.9 = .22 is below .60.
    plist = [(0.20, -0.01), (0.30, -0.05), (0.40, -0.05), (0.60, -0.05)]
    d = go(site_config, plist, pct=30.0, breach=3, spill=1.0, sat=1,
           shortfall={3: 1.0}, charges={0: 3.0, 1: 3.0, 2: 3.0})
    assert d.suppressed == [("S3", "export", "V2")]
    assert d.selector == "S4" and d.action == "charge"


def test_consecutive_vetoed_proposals_no_loop(site_config):
    # Battery at floor (V1), injection negative (V2), budget exhausted (V3).
    # S0 shave discharge: V1. S1 (cons -0.05) grid charge: V3.
    # S3 proposes nothing: no charge above the reserve to export.
    # S5 grid-charge: later -0.05 * 0.9 = -0.045 > consumption now -0.05, so
    # under the plain-multiplication loss rule S5 also proposes (the old
    # price/eff division gave -0.0556 < -0.05 and hid it); V3 blocks it.
    # Then S6.
    g = grid(offtake=4.0, energy=0.9, peak=2.5, avg=9.0)
    # shave: projected 0.9+4/6 = 1.5667; needed (1.5667-0.625)*6 = 5.65 ->
    # clamped to offtake-floor 1.5. budget (0.625-0.9)*6 = -1.65 <= 0.
    d = go(site_config, [(-0.05, -0.01), (0.20, -0.05)], pct=0.0, g=g,
           spill=1.0)
    assert d.vetoes_fired == ["V1", "V2", "V3", "V5"]
    assert d.suppressed == [
        ("S0", "discharge", "V5"),
        ("S1", "charge", "V3"),
        ("S4", "charge", "V3"),
    ]
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)


def test_non_firing_selector_is_not_suppressed(site_config):
    d = go(site_config, [FLAT, FLAT], pct=10.0)   # V1 only; nothing proposed
    assert d.vetoes_fired == ["V1"] and d.suppressed == []


# ---- current block / errors ----------------------------------------------

def test_now_mid_block_selects_that_block(site_config):
    cfg = site_config
    plist = [FLAT, (-0.05, 0.02)]
    blocks = [blk(0), blk(1)]
    traj = trajectory.Trajectory(blocks, None, 0.0, None, 0.0, T0 + 2 * STEP)
    pmap = {T0 + i * STEP: price(i, *p) for i, p in enumerate(plist)}
    st = battery.from_percent(50, cfg)
    d = decide(traj, pmap, st, CALM, cfg, T0 + timedelta(minutes=29, seconds=59))
    assert d.selector == "S1" and d.block_start == T0 + STEP
    d0 = decide(traj, pmap, st, CALM, cfg, T0 + timedelta(minutes=14, seconds=59))
    assert d0.selector == "S6"


def test_no_current_block_raises(site_config):
    traj = trajectory.Trajectory([blk(0)], None, 0.0, None, 0.0, T0 + STEP)
    st = battery.from_percent(50, site_config)
    for when in (T0 - timedelta(minutes=1), T0 + STEP):
        with pytest.raises(rules.NoCurrentBlockError):
            decide(traj, {T0: price(0, *FLAT)}, st, CALM, site_config, when)
    with pytest.raises(ValueError):
        decide(traj, {}, st, CALM, site_config, datetime(2026, 6, 2, 10, 0))


# ---- fall-back day (UTC joins) -------------------------------------------

def test_fall_back_day_prices_joined_by_utc_instant(site_config):
    # 2026-10-25 Brussels clocks go 03:00 CEST -> 02:00 CET, so 02:00 occurs
    # twice: 00:00Z (CEST, fold 0) and 01:00Z (CET, fold 1). As aware
    # ZoneInfo datetimes the two compare EQUAL (PEP 495), so a join on
    # datetime equality would give both blocks the same price.
    first = datetime(2026, 10, 25, 2, 0, tzinfo=TZ)
    second = datetime(2026, 10, 25, 2, 0, fold=1, tzinfo=TZ)
    assert first == second and first.astimezone(timezone.utc) \
        != second.astimezone(timezone.utc)

    def mk(start):
        return trajectory.TrajectoryBlock(
            start, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, True, 5.0, 50.0)

    traj = trajectory.Trajectory([mk(first), mk(second)], None, 0.0, None,
                                 0.0, second + STEP)
    k1 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone(timedelta(hours=2)))
    k2 = datetime(2026, 10, 25, 2, 0, tzinfo=timezone(timedelta(hours=1)))
    pmap = {
        k1: prices.PricePoint(k1, 0.0, 0.30, 0.02, 15),    # first 02:00
        k2: prices.PricePoint(k2, 0.0, -0.05, 0.02, 15),   # second 02:00
    }
    st = battery.from_percent(50, site_config)
    # now = 02:05 in the SECOND occurrence (01:05Z) -> price -0.05 -> S1
    d2 = decide(traj, pmap, st, CALM, site_config,
                datetime(2026, 10, 25, 2, 5, fold=1, tzinfo=TZ))
    assert d2.selector == "S1" and d2.block_start.utcoffset() \
        == timedelta(hours=1)
    # now = 02:05 in the FIRST occurrence (00:05Z) -> price 0.30 -> no S1
    d1 = decide(traj, pmap, st, CALM, site_config,
                datetime(2026, 10, 25, 2, 5, tzinfo=TZ))
    assert d1.selector == "S6" and d1.block_start.utcoffset() \
        == timedelta(hours=2)


# ---- integration with the real trajectory ---------------------------------

def test_regression_with_real_projection():
    """Same protection as the hand-built regression, through project().

    Site: 4 kWh, reserve 10 %, 8 kW in / 4 kW out, start 50 % = 2.0 kWh.
    solar [1.5, 2.5, 2.5, 0.5], usage 0.5 each. b0 surplus 1.0 (headroom 2.0)
    -> absorbed, charge 3.0; b1 surplus 2.0, headroom 1.0 -> absorb 1.0,
    spill 1.0, FULL (saturation block 1). Injection: b0 -0.02, then 0.10.
    V2 fires; S3 (spill ahead, now the best injection in its window) wants to
    export at the negative price and is suppressed by V2; nothing else
    applies, so the planner sends idle and the inverter's default stores it.
    """
    cfg = fx.site(4.0, 8.0, 4.0)
    st = battery.from_percent(50.0, cfg)
    inj = [-0.02, 0.10, 0.10, 0.10]
    pmap = {t: prices.PricePoint(t, 0.0, 0.20, i, 15)
            for t, i in zip(fx.starts(4), inj)}
    traj = trajectory.project(st, fx.solar_slots([1.5, 2.5, 2.5, 0.5]),
                              fx.usage_slots([0.5] * 4), pmap, cfg)
    assert traj.total_spill_kwh > 0 and traj.saturation_block is not None
    d = decide(traj, pmap, st, None, cfg, fx.START + timedelta(minutes=2))
    assert d.vetoes_fired == ["V2"]
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.suppressed == [("S3", "export", "V2")]


# ---- V4 no usage profile ----------------------------------------------------

NEG = (-0.05, 0.02)          # negative consumption price: S1 grid-charges
ARB = [(0.10, 0.02), (0.10, 0.50), (0.10, 0.50)]
# ARB: 0.50 * 0.9 = 0.45 > 0.10 -> S5 arbitrage grid charge when history is ok.


V4_FORBIDS = {"grid_charge", "export"}


def test_v4_default_is_the_safe_value_and_holds_price_selectors(site_config):
    forbidden, fired = _rules.establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config)                                  # kwarg omitted
    assert fired == ["V4"]
    assert forbidden == V4_FORBIDS
    assert "discharge" not in forbidden       # S0 peak shaving must still act


def test_v4_fires_without_capacity_logic(site_config):
    cfg = replace(site_config, capacity_enabled=False)
    forbidden, fired = _rules.establish_vetoes(
        battery.from_percent(50, cfg), price(0, 0.2, 0.05), cfg, None,
        usage_history_available=False)
    assert fired == ["V4"] and forbidden == V4_FORBIDS


def test_v4_quiet_with_history(site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config)
    assert fired == [] and forbidden == frozenset()


def test_v4_omitted_kwarg_in_decide_vetoes_grid_charge(site_config):
    d = go(site_config, [NEG] * 3, history=None)
    assert d.vetoes_fired == ["V4"]
    assert d.suppressed == [("S1", "charge", "V4"),
                            ("S4", "charge", "V4")]
    assert (d.selector, d.action) == ("S6", "idle")


def test_v4_blocks_s1_grid_charge_and_falls_through_to_idle(site_config):
    d = go(site_config, [NEG] * 3, history=False)
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)
    assert d.vetoes_fired == ["V4"]
    assert d.suppressed == [("S1", "charge", "V4"),
                            ("S4", "charge", "V4")]
    assert "V4" in d.reasoning


def test_v4_blocks_s4_sale_arbitrage_grid_charge(site_config):
    d = go(site_config, ARB, history=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.suppressed == [("S4", "charge", "V4")]


def test_history_present_lets_s1_and_s4_grid_charge(site_config):
    d = go(site_config, [NEG] * 3, history=True)
    assert (d.selector, d.action) == ("S1", "charge")
    assert d.vetoes_fired == [] and d.suppressed == []
    d = go(site_config, ARB, history=True)
    assert (d.selector, d.action) == ("S4", "charge")
    assert d.vetoes_fired == []


def test_v4_surplus_solar_is_idle_with_or_without_history(site_config):
    # Surplus solar asks for nothing (the inverter stores it by default), so
    # history makes no difference; V4 only reports itself.
    d = go(site_config, [(0.20, -0.05)] * 3, solar={0: 1.0}, history=False)
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)
    assert d.vetoes_fired == ["V2", "V4"]
    assert d.suppressed == []
    assert "no usage history: planner holds" in d.reasoning
    d = go(site_config, [(0.20, -0.05)] * 3, solar={0: 1.0}, history=True)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.vetoes_fired == ["V2"] and d.suppressed == []


def test_v4_blocks_s3_export(site_config):
    # spill ahead and now is the best injection price: S3 exports with history.
    plist = [(0.20, 0.30), (0.20, 0.10), (0.20, 0.10)]
    d = go(site_config, plist, spill=1.0, sat=2, history=True)
    assert (d.selector, d.action) == ("S3", "export")
    d = go(site_config, plist, spill=1.0, sat=2, history=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.suppressed == [("S3", "export", "V4")]
    assert "no usage history: planner holds" in d.reasoning


def test_v4_blocks_s4_grid_charge(site_config):
    plist = [(0.10, 0.02), (0.10, 0.02), (0.30, 0.02)]
    d = go(site_config, plist, breach=2, shortfall={2: 0.5}, history=True)
    assert (d.selector, d.action) == ("S4", "charge")
    d = go(site_config, plist, breach=2, shortfall={2: 0.5}, history=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert ("S4", "charge", "V4") in d.suppressed


def test_idle_reasoning_without_history_says_so_plainly(site_config):
    d = go(site_config, [FLAT] * 3, history=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.reasoning.startswith("hold: no usage history: planner holds")


def test_idle_reasoning_with_history_does_not_claim_it(site_config):
    d = go(site_config, [FLAT] * 3, history=True)
    assert "no usage history" not in d.reasoning


def test_v4_leaves_peak_shaving_alone(site_config):
    d = go(site_config, [FLAT] * 3, g=SHAVE, history=False)
    assert (d.selector, d.action, d.target_power_kw) == ("S0", "discharge", 1.5)
    assert d.vetoes_fired == ["V3", "V4"]
    assert d.suppressed == []


def test_v4_does_not_suppress_s0_but_v1_still_does(site_config):
    # an empty battery (V5) forbids the S0 discharge; V4 alone never does,
    # and neither does being under the reserve (V1 is export-only).
    d = go(site_config, [FLAT] * 3, g=SHAVE, pct=0.0, history=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.suppressed == [("S0", "discharge", "V5")]
    d = go(site_config, [FLAT] * 3, g=SHAVE, pct=5.0, history=False)
    assert (d.selector, d.action) == ("S0", "discharge")


def test_v4_renders_in_the_vetoes_field(site_config):
    import decision
    d = go(site_config, [NEG] * 3, history=False)
    assert decision.render_vetoes(d) == ["V4(suppressed S1 charge)",
                                        "V4(suppressed S4 charge)"]


# ---- stay_under_percent -------------------------------------------------------
# Grid state elapsed 5 min (1/6 h left), peak 2.5 -> ceiling 2.5.
#   100 %: allowance 0.625; energy 0.45 -> (0.625-0.45)*6 = 1.05 kW
#    80 %: allowance 0.500; energy 0.45 -> (0.500-0.45)*6 = 0.30 kW
#    80 %: energy 0.50 -> budget 0.0 -> V3 ; 100 %: (0.625-0.5)*6 = 0.75
PCT_GRID = grid(energy=0.45, peak=2.5)
PCT_FULL = grid(energy=0.50, peak=2.5)


def test_percent_100_charges_against_the_full_ceiling(site_config):
    d = go(site_config, [NEG] * 3, g=PCT_GRID)
    assert d.selector == "S1"
    assert d.target_power_kw == pytest.approx(1.05)


def test_percent_80_caps_grid_charge_at_the_reduced_ceiling(site_config):
    cfg = replace(site_config, stay_under_percent=80.0)
    d = go(cfg, [NEG] * 3, g=PCT_GRID)
    assert d.selector == "S1"
    assert d.target_power_kw == pytest.approx(0.30)
    assert "capped by grid budget 0.30" in d.reasoning


def test_percent_80_fires_v3_where_100_does_not(site_config):
    cfg = replace(site_config, stay_under_percent=80.0)
    d = go(cfg, [NEG] * 3, g=PCT_FULL)
    assert d.vetoes_fired == ["V3"]
    assert d.suppressed == [("S1", "charge", "V3"),
                            ("S4", "charge", "V3")]
    d = go(site_config, [NEG] * 3, g=PCT_FULL)
    assert d.vetoes_fired == [] and d.target_power_kw == pytest.approx(0.75)


def test_percent_80_caps_s4_sale_too(site_config):
    cfg = replace(site_config, stay_under_percent=80.0)
    d = go(cfg, ARB, g=PCT_GRID)
    assert d.selector == "S4" and d.target_power_kw == pytest.approx(0.30)


def test_percent_does_not_reduce_peak_shaving(site_config):
    cfg = replace(site_config, stay_under_percent=80.0)
    d = go(cfg, [FLAT] * 3, g=SHAVE)
    assert (d.selector, d.action) == ("S0", "discharge")
    assert d.target_power_kw == pytest.approx(1.5)   # same as 100 %
    assert "2.50 kW ceiling" in d.reasoning           # real ceiling, not 2.0


# ---- household draw reduces the clamp of every grid-charging selector -------
HOUSEHOLD = grid(offtake=1.0, energy=0.425, peak=2.5)   # 1.2 allowed - 1.0 = 0.2


def test_s1_clamps_to_budget_after_household_draw(site_config):
    d = go(site_config, [(-0.05, 0.02)] * 2, g=HOUSEHOLD)
    assert d.selector == "S1"
    assert d.target_power_kw == pytest.approx(0.2)
    assert "capped by grid budget 0.20 kW" in d.reasoning


def test_s4_sale_clamps_to_budget_after_household_draw(site_config):
    d = go(site_config, ARB, g=HOUSEHOLD)
    assert d.selector == "S4"
    assert d.target_power_kw == pytest.approx(0.2)
    assert "capped by grid budget 0.20 kW" in d.reasoning


def test_household_draw_beyond_allowance_fires_v3_and_blocks_charge(site_config):
    g = grid(offtake=1.5, energy=0.425, peak=2.5)         # 1.2 - 1.5 < 0
    d = go(site_config, [(-0.05, 0.02)] * 2, g=g)
    assert "V3" in d.vetoes_fired and d.action != "charge"


def test_s4_clamps_to_budget_after_household_draw(site_config):
    d = go(site_config, S4_PRICES, now_i=1, pct=30.0, breach=4,
           shortfall=S4_SHORT, g=HOUSEHOLD)
    assert d.selector == "S4"
    assert d.target_power_kw == pytest.approx(0.2)
    assert "capped by grid budget 0.20 kW" in d.reasoning


# ---- S4 only when cheaper than importing at the breach ---------------------
#
# The reserve is a floor, not a target: when the battery reaches it the house
# imports at that time. S4 charges ahead only if price now / efficiency (0.9)
# is below the energy-weighted consumption price of the shortfall blocks.

def _s4_case(site_config, now_cons, breach_cons, short=1.0, **kw):
    plist = [(now_cons, 0.02), (0.50, 0.02), (breach_cons, 0.02)]
    return go(site_config, plist, pct=30.0, breach=2,
              shortfall=kw.pop("shortfall", {2: short}), **kw)


def test_s4_cheaper_now_charges_and_states_the_comparison(site_config):
    d = _s4_case(site_config, 0.20, 0.30)          # .20 / .9 = .2222 < .30
    assert (d.selector, d.action) == ("S4", "charge")
    assert "charging now costs 0.2222 EUR/kWh after losses vs 0.3000 "         "importing at the breach" in d.reasoning


def test_s4_dearer_now_does_nothing_and_falls_through(site_config):
    # 0.30 now vs 0.22 at the breach: 0.30 / 0.9 = 0.333 > 0.22 -> no charge
    d = _s4_case(site_config, 0.30, 0.22)
    assert d.selector == "S6" and d.action == "idle"
    assert d.suppressed == []        # not proposed, so not a suppression


def test_s4_dearer_now_lets_a_later_selector_act(site_config):
    # S4 steps aside; S5 (arbitrage against a later injection price) can act.
    plist = [(0.30, 0.02), (0.50, 0.02), (0.22, 0.60)]
    d = go(site_config, plist, pct=30.0, breach=2, shortfall={2: 1.0})
    assert d.selector == "S4"


def test_s4_efficiency_decides(site_config):
    # 0.20 now vs 0.21 at the breach: charges at 0.9 (.222?) no - 0.2222 > 0.21
    d = _s4_case(site_config, 0.20, 0.21)
    assert d.selector == "S6"
    # a better battery (0.99) makes the same prices worth it: .2020 < .21
    better = dataclasses.replace(site_config, round_trip_efficiency=0.99)
    d = _s4_case(better, 0.20, 0.21)
    assert d.selector == "S4"


def test_s4_equal_cost_prefers_importing(site_config):
    cfg = dataclasses.replace(site_config, round_trip_efficiency=1.0)
    d = _s4_case(cfg, 0.25, 0.25)
    assert d.selector == "S6"


def test_s4_unpriced_shortfall_blocks_are_ignored(site_config):
    # block 3 has the big shortfall but no price: only block 2 (.30) counts
    plist = [(0.20, 0.02), (0.50, 0.02), (0.30, 0.02), (0.01, 0.02)]
    d = go(site_config, plist, pct=30.0, breach=2, hide={3},
           shortfall={2: 1.0, 3: 50.0})
    assert d.selector == "S4"
    assert "0.3000 importing at the breach" in d.reasoning


def test_s4_does_nothing_when_no_shortfall_block_is_priced(site_config):
    plist = [(0.20, 0.02), (0.20, 0.02), None]
    d = go(site_config, plist, pct=30.0, breach=2, shortfall={2: 1.0})
    assert d.selector == "S6"


def test_s4_shortfall_prices_are_weighted_by_energy(site_config):
    # 3 kWh at .50 and 1 kWh at .10 -> (1.5 + 0.1) / 4 = 0.40, not the plain
    # mean .30. Now .30 / .9 = .333: below .40 (charges), above .30 (would not).
    plist = [(0.30, 0.02), (0.60, 0.02), (0.50, 0.02), (0.10, 0.02)]
    d = go(site_config, plist, pct=30.0, breach=2,
           shortfall={2: 3.0, 3: 1.0})
    assert d.selector == "S4"
    assert "0.4000 importing at the breach" in d.reasoning
    # and the other way round: the cheap block carries the weight -> .20
    d = go(site_config, plist, pct=30.0, breach=2,
           shortfall={2: 1.0, 3: 3.0})
    assert d.selector == "S6"


def test_s4_every_candidate_must_beat_the_import_price(site_config):
    # importing at the breach (block 3) costs .30; shortfall 2.5 kWh asks for
    # N = 2 blocks. Blocks 0..2 cost .20 / .28 / .29, i.e. .222 / .311 / .322
    # after losses: only block 0 beats .30. Without the check the 2nd cheapest
    # (.28) would also charge.
    plist = [(0.20, 0.02), (0.28, 0.02), (0.29, 0.02), (0.30, 0.02)]
    d = go(site_config, plist, pct=30.0, breach=3, shortfall={3: 2.5})
    assert d.selector == "S4" and "cheapest 1 of 1 qualifying" in d.reasoning
    d = go(site_config, plist, now_i=1, pct=30.0, breach=3,
           shortfall={3: 2.5})
    assert d.selector == "S6"


# ---- V6 no solar forecast ---------------------------------------------------------

V6_FORBIDS = {"grid_charge", "export"}


def test_v6_quiet_by_default_and_when_the_forecast_is_available(site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config)
    assert "V6" not in fired
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config, forecast_available=True)
    assert fired == [] and forbidden == frozenset()


def test_v6_fires_without_a_forecast_and_forbids_grid_charge_and_export(
        site_config):
    forbidden, fired = establish_vetoes(
        battery.from_percent(50, site_config), price(0, 0.2, 0.05),
        site_config, forecast_available=False)
    assert fired == ["V6"] and forbidden == V6_FORBIDS
    assert "discharge" not in forbidden           # peak shaving must still act


def test_v6_review_scenario_s4_does_not_over_buy_on_a_zero_forecast(
        site_config):
    # 03:00-style case: battery 30 %, the forecast is lost so the projection
    # shows a breach. With a forecast S4 charges; without one it holds.
    kw = dict(pct=30.0, breach=4, shortfall=S4_SHORT)
    assert go(site_config, S4_PRICES, now_i=1, **kw).selector == "S4"
    d = go(site_config, S4_PRICES, now_i=1, forecast=False, **kw)
    assert (d.selector, d.action, d.target_power_kw) == ("S6", "idle", 0.0)
    assert d.vetoes_fired == ["V6"]
    assert d.suppressed == [("S4", "charge", "V6")]
    assert d.reasoning.startswith("hold: no solar forecast")


def test_v6_blocks_negative_price_grid_charge(site_config):
    d = go(site_config, [NEG] * 3, forecast=False)
    assert d.suppressed == [("S1", "charge", "V6"), ("S4", "charge", "V6")]
    assert (d.selector, d.action) == ("S6", "idle")


def test_v6_blocks_export(site_config):
    plist = [(0.20, 0.30), (0.20, 0.10), (0.20, 0.10)]
    d = go(site_config, plist, spill=1.0, sat=2, forecast=False)
    assert (d.selector, d.action) == ("S6", "idle")
    assert d.suppressed == [("S3", "export", "V6")]


def test_v6_does_not_stop_peak_shaving(site_config):
    d = go(site_config, [FLAT] * 3, g=SHAVE, forecast=False)
    assert (d.selector, d.action) == ("S0", "discharge")
    assert d.vetoes_fired == ["V3", "V6"]


def test_v6_and_v4_together_name_both(site_config):
    d = go(site_config, [NEG] * 3, history=False, forecast=False)
    assert d.vetoes_fired == ["V4", "V6"]
    assert d.suppressed == [("S1", "charge", "V4+V6"),
                            ("S4", "charge", "V4+V6")]
    assert "no solar forecast" in d.reasoning


def test_v6_joins_the_block_list_in_the_record_text(site_config):
    import decision
    d = go(site_config, [NEG] * 3, forecast=False)
    assert decision.render_vetoes(d) == [
        "V6(suppressed S1 charge)", "V6(suppressed S4 charge)"]


# ---- L1: never charge more than the room left in the battery ---------------

NEG_NOW = [(-0.05, 0.02), (0.20, 0.02)]


def test_s1_does_not_charge_a_full_battery(site_config):
    d = go(site_config, NEG_NOW, pct=100.0)
    assert d.selector == "S6" and d.action == "idle"


def test_s1_charge_limited_to_the_room_left(site_config):
    # 97.5 % of 10 kWh: 0.25 kWh of room in a 0.25 h block = 1.0 kW, not 5 kW
    d = go(site_config, NEG_NOW, pct=97.5)
    assert (d.selector, d.action) == ("S1", "charge")
    assert d.target_power_kw == pytest.approx(1.0)
    assert "room left in the battery" in d.reasoning


def test_s1_full_power_when_there_is_room(site_config):
    d = go(site_config, NEG_NOW, pct=50.0)
    assert d.target_power_kw == 5.0 and "room left" not in d.reasoning


def test_s4_sale_charge_limited_to_the_room_left(site_config):
    d = go(site_config, ARB, pct=97.5)
    assert (d.selector, d.action) == ("S4", "charge")
    assert d.target_power_kw == pytest.approx(1.0)


# ---- M4: now must beat the other blocks by the round-trip loss ---------------

def test_s3_equal_prices_do_not_export(site_config):
    flat = [(0.40, 0.15)] * 4
    d = go(site_config, flat, now_i=1, sat=3, spill=2.0)
    assert d.selector == "S6"


def test_s3_needs_the_round_trip_margin(site_config):
    # efficiency 0.9: 0.15 * 0.9 = 0.135 must beat the best other block
    just_short = [(0.40, 0.15), (0.40, 0.15), (0.40, 0.14), (0.40, 0.10)]
    d = go(site_config, just_short, now_i=1, sat=3, spill=2.0)
    assert d.selector == "S6"                       # 0.135 < 0.14
    enough = [(0.40, 0.15), (0.40, 0.15), (0.40, 0.13), (0.40, 0.10)]
    d = go(site_config, enough, now_i=1, sat=3, spill=2.0)
    assert d.selector == "S3"                       # 0.135 > 0.13


# ---- S4 sale use: only the cheapest blocks that fill the room charge ----------

# consumption b0 .30 b1 .20 b2 .25 b3 .22 | sell block b4 (injection .60,
# value .54 after losses) | b5 .01 is after the sell block and does not count.
SALE = [(0.30, 0.02), (0.20, 0.02), (0.25, 0.02), (0.22, 0.02),
        (0.50, 0.60), (0.01, 0.02)]


@pytest.mark.parametrize("pct,now_i,fires", [
    (90.0, 0, False),   # room 1.0 kWh = 1 block: only the cheapest (b1) charges
    (90.0, 1, True),
    (90.0, 2, False),
    (50.0, 0, True),    # room 5.0 kWh = 4 blocks: all of b0..b3 qualify
    (50.0, 2, True),
])
def test_s4_sale_charges_only_the_cheapest_blocks_that_fill_the_room(
        site_config, pct, now_i, fires):
    d = go(site_config, SALE, now_i=now_i, pct=pct)
    assert (d.selector == "S4") is fires


def test_s4_sale_ignores_cheaper_blocks_after_the_sell_block(site_config):
    # b5 (.01) is cheaper but arrives after the sell block: not a candidate
    d = go(site_config, SALE, now_i=1, pct=90.0)
    assert d.selector == "S4" and "cheapest 1 of 3" in d.reasoning
