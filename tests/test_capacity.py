"""Capacity model tests. Expected values are hand-derived, never computed by
the code under test."""
from dataclasses import replace
from datetime import datetime

import pytest

import capacity as cap


def at(h, m, s=0):
    return datetime(2026, 9, 29, h, m, s)


def state(offtake=0.0, energy=0.0, elapsed=7.5, peak=0.0):
    return cap.GridState(offtake, at(14, 0), energy, elapsed,
                         energy / (elapsed / 60.0), peak, False)


def cfg(site_config, **kw):
    return replace(site_config, **kw)


# ---- T053 window alignment / build_state --------------------------------

@pytest.mark.parametrize("now,start,elapsed", [
    (at(14, 0, 0), at(14, 0), 0.0),
    (at(14, 7, 30), at(14, 0), 7.5),
    (at(14, 15, 0), at(14, 15), 0.0),
    (at(14, 30, 0), at(14, 30), 0.0),
    (at(14, 44, 59), at(14, 30), 14 + 59 / 60),
    (at(14, 45, 0), at(14, 45), 0.0),
    (at(14, 59, 59), at(14, 45), 14 + 59 / 60),
])
def test_window_alignment(site_config, now, start, elapsed):
    s = cap.build_state(1.0, 0.1, now, 0.0, site_config)
    assert s.window_start == start
    assert s.elapsed_minutes == pytest.approx(elapsed)


def test_running_mode_energy(site_config):
    c = cfg(site_config, quarter_hour_average_mode="running")
    s = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, c,
                        reported_average_kw=4.0)
    # 4.0 kW * 7.5 min / 60 = 0.5 kWh
    assert s.running_average_kw == pytest.approx(4.0)
    assert s.window_energy_kwh == pytest.approx(0.5)
    assert (s.average_mode, s.mode_confidence) == ("running", "configured")


def test_accumulating_mode_normalised(site_config):
    c = cfg(site_config, quarter_hour_average_mode="accumulating")
    # meter shows energy/15min: 0.5 kWh / 0.25 h = 2.0 kW at 7.5 min.
    # true running = 2.0 * 15 / 7.5 = 4.0 kW ; energy = 4.0*7.5/60 = 0.5
    s = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, c,
                        reported_average_kw=2.0)
    assert s.running_average_kw == pytest.approx(4.0)
    assert s.window_energy_kwh == pytest.approx(0.5)


def test_auto_defaults_to_assumed_accumulating(site_config):
    s = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, site_config,
                        reported_average_kw=2.0)
    assert (s.average_mode, s.mode_confidence) == ("accumulating", "assumed")
    assert s.running_average_kw == pytest.approx(4.0)
    d = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, site_config,
                        reported_average_kw=4.0, average_mode="running",
                        mode_confidence="detected")
    assert d.running_average_kw == pytest.approx(4.0)
    assert d.mode_confidence == "detected"


def test_opening_seconds_guard(site_config):
    # 3 s in, 0.001 kWh. Elapsed floored to 1 min: 0.001 / (1/60) = 0.06 kW.
    # Unguarded it would be 0.001 / (0.05/60) = 1.2 kW.
    s = cap.build_state(1.0, 0.001, at(14, 0, 3), 0.0, site_config)
    assert s.running_average_kw == pytest.approx(0.06)
    # accumulating meter figure 0.004 kW: 0.004*15/1 = 0.06, energy 0.001
    a = cap.build_state(1.0, 0.0, at(14, 0, 3), 0.0, site_config,
                        reported_average_kw=0.004)
    assert a.running_average_kw == pytest.approx(0.06)
    assert a.window_energy_kwh == pytest.approx(0.001)


# ---- T054 ceiling --------------------------------------------------------

@pytest.mark.parametrize("peak,expected", [
    (0.0, 2.5), (1.8, 2.5), (2.5, 2.5), (6.2, 6.2)])
def test_ceiling(site_config, peak, expected):
    assert cap.ceiling_kw(state(peak=peak), site_config) == expected


# ---- T055 budget ---------------------------------------------------------

def test_budget_clamped_high(site_config):
    # ceiling 4 -> allowance 1.0 ; remaining 7.5 min = 0.125 h
    # (1.0 - 0.2)/0.125 = 6.4 -> clamp to max_charge 5.0
    assert cap.budget_kw(state(energy=0.2, peak=4.0), site_config) == 5.0
    c = cfg(site_config, max_charge_kw=10.0)
    assert cap.budget_kw(state(energy=0.2, peak=4.0), c) == pytest.approx(6.4)


def test_budget_positive_and_negative(site_config):
    # (1.0 - 0.9)/0.125 = 0.8   (WP text calls 0.9 negative; it is not)
    assert cap.budget_kw(state(energy=0.9, peak=4.0),
                         site_config) == pytest.approx(0.8)
    # (1.0 - 1.1)/0.125 = -0.8 : negative, NOT clamped to zero
    assert cap.budget_kw(state(energy=1.1, peak=4.0),
                         site_config) == pytest.approx(-0.8)


def test_budget_final_minute_zero(site_config):
    assert cap.budget_kw(state(energy=0.5, elapsed=14.5, peak=4.0),
                         site_config) == 0.0
    # 30 s remaining, even when already over the ceiling
    assert cap.budget_kw(state(energy=2.0, elapsed=14.5, peak=4.0),
                         site_config) == 0.0


def test_budget_last_allowed_minute(site_config):
    # elapsed 13 -> remaining 2 min = 1/30 h ; (1.0-0.9)*30 = 3.0
    assert cap.budget_kw(state(energy=0.9, elapsed=13.0, peak=4.0),
                         site_config) == pytest.approx(3.0)


def test_budget_subtracts_household_draw_once(site_config):
    # Household draws 4 kW steadily: 7.5 min -> 0.5 kWh already IN
    # window_energy (metered at the connection point).
    # Total allowed rate (1.0 - 0.5)/0.125 = 4.0 kW; the house keeps drawing
    # 4.0 kW, so nothing is left for charging.
    s = state(offtake=4.0, energy=0.5, peak=4.0)
    assert cap.allowed_offtake_kw(s, site_config) == pytest.approx(4.0)
    assert cap.budget_kw(s, site_config) == pytest.approx(0.0)


# ---- T056 shave ----------------------------------------------------------

def test_shave_worked_scenario(site_config):
    # 6 kW draw, ceiling 4.0, 5 min in.
    s = state(offtake=6.0, energy=0.5, elapsed=5.0, peak=4.0)
    # energy: 6 kW * 5/60 h = 0.5 ; running avg 0.5/(5/60) = 6.0
    assert s.running_average_kw == pytest.approx(6.0)
    assert cap.ceiling_kw(s, site_config) == 4.0
    # allowance 1.0 ; remaining 10 min = 1/6 h
    # allowed offtake (1.0-0.5)/(1/6) = 3.0 ; household 6.0 -> budget -3.0
    assert cap.allowed_offtake_kw(s, site_config) == pytest.approx(3.0)
    assert cap.budget_kw(s, site_config) == pytest.approx(-3.0)
    # projected 0.5 + 6*(1/6) = 1.5 kWh -> avg 6.0 > 4.0
    # shave (1.5-1.0)/(1/6) = 3.0 ; offtake becomes 3.0 (>= floor 2.5)
    # check: 0.5 + 3.0/6 = 1.0 kWh -> avg 4.0
    assert cap.shave_kw(s, site_config) == pytest.approx(3.0)


def test_shave_clamped_to_max_discharge(site_config):
    c = cfg(site_config, max_discharge_kw=2.0)
    s = state(offtake=6.0, energy=0.5, elapsed=5.0, peak=4.0)
    assert cap.shave_kw(s, c) == 2.0


def test_shave_zero_below_floor(site_config):
    # offtake 2.0 <= floor 2.5, even with the window over the ceiling
    assert cap.shave_kw(state(offtake=2.0, energy=1.2, elapsed=10.0),
                        site_config) == 0.0
    assert cap.shave_kw(state(offtake=2.5, energy=1.2, elapsed=10.0),
                        site_config) == 0.0


def test_shave_zero_when_under_ceiling(site_config):
    # ceiling 6.2 ; offtake 5, 5 min in, 0.4 kWh: projected 0.4+5/6=1.2333
    # avg 4.93 < 6.2 -> 0
    assert cap.shave_kw(state(offtake=5.0, energy=0.4, elapsed=5.0, peak=6.2),
                        site_config) == 0.0


def test_shave_never_below_floor(site_config):
    # ceiling 6.2 allowance 1.55 ; 10 min in, 1.5 kWh, offtake 3.0
    # projected 1.5 + 3*(1/12) = 1.75 -> avg 7.0 > 6.2
    # uncapped shave (1.75-1.55)*12 = 2.4 ; cap offtake-floor = 0.5
    s = state(offtake=3.0, energy=1.5, elapsed=10.0, peak=6.2)
    assert cap.shave_kw(s, site_config) == pytest.approx(0.5)


# ---- T062 euros ----------------------------------------------------------

def test_peak_increase_cost(site_config):
    # avg rises 1/13 kW ; monthly fee 40/12 per kW ; stays 13 months:
    # (1/13) * (40/12) * 13 = 3.3333 EUR
    assert cap.peak_increase_cost_eur(1.0, site_config) == pytest.approx(
        40.0 / 12.0)
    c = cfg(site_config, capacity_rate_eur_per_kw_year=60.0)
    assert cap.peak_increase_cost_eur(2.0, c) == pytest.approx(10.0)


def test_peak_increase_cost_ignores_the_billing_floor(site_config):
    # Pins the documented behaviour: the cost is linear in delta_kw and blind
    # to the 2.5 kW floor. The caller must pass the floored difference.
    assert site_config.billing_floor_kw == 2.5
    floor = site_config.billing_floor_kw
    # 1.0 -> 2.0 kW is entirely below the floor: billed rise is 0, but the raw
    # delta of 1.0 kW is priced in full
    assert cap.peak_increase_cost_eur(2.0 - 1.0, site_config) == pytest.approx(
        40.0 / 12.0)
    billed = max(floor, 2.0) - max(floor, 1.0)
    assert billed == 0.0
    assert cap.peak_increase_cost_eur(billed, site_config) == 0.0
    # 2.0 -> 3.5 kW: only the 1.0 kW above the floor is billed
    billed = max(floor, 3.5) - max(floor, 2.0)
    assert cap.peak_increase_cost_eur(billed, site_config) == pytest.approx(
        40.0 / 12.0)


def test_averaging_window_from_config(site_config):
    assert cap.billed_average_increase_kw(1.3, site_config) == pytest.approx(
        0.1)
    c = cfg(site_config, peak_averaging_months=6)
    assert cap.billed_average_increase_kw(1.2, c) == pytest.approx(0.2)
    # the months cancel in the total cost
    assert cap.peak_increase_cost_eur(1.0, c) == pytest.approx(40.0 / 12.0)


def test_arbitrage_value():
    assert cap.arbitrage_value_eur(2.0, 0.15) == pytest.approx(0.30)


# ---- T067 detector -------------------------------------------------------

def win(i):
    return datetime(2026, 9, 29, 8 + i, 0)


def pair(i, kind, e=3.0, late=12.0, load_e=3.0, load_l=3.0):
    def rep(minutes, load):
        return load * minutes / 15.0 if kind == "acc" else load
    return [cap.Sample(win(i), e, rep(e, load_e), load_e),
            cap.Sample(win(i), late, rep(late, load_l), load_l)]


def samples(n, kind, **kw):
    out = []
    for i in range(n):
        out += pair(i, kind, **kw)
    return out


def test_detects_accumulating(site_config):
    # load 3 kW: 3 min -> 0.6, 12 min -> 2.4 ; ratio 0.25 = 3/12
    v = cap.detect_average_mode(samples(3, "acc"), site_config)
    assert (v.mode, v.confidence) == ("accumulating", "detected")


def test_detects_running(site_config):
    # 3.0 and 3.0 : ratio 1
    v = cap.detect_average_mode(samples(3, "run"), site_config)
    assert (v.mode, v.confidence) == ("running", "detected")
    assert v.running_votes == 3


def test_safe_default_before_conclusion(site_config):
    v = cap.detect_average_mode(samples(2, "run"), site_config)
    assert (v.mode, v.confidence) == ("accumulating", "assumed")
    v = cap.detect_average_mode([], site_config)
    assert (v.mode, v.confidence) == ("accumulating", "assumed")


def test_samples_from_different_windows_are_never_mixed(site_config):
    # Each window holds ONE usable sample (3 min in one, 12 min in the next).
    # Paired across windows they would look like a clean "running" pair
    # (same reported value, steady load) and vote; kept apart they are lone
    # samples and cast no vote at all.
    out = []
    for i in range(6):
        minutes = 3.0 if i % 2 == 0 else 12.0
        out.append(cap.Sample(win(i), minutes, 3.0, 3.0))
    v = cap.detect_average_mode(out, site_config)
    assert (v.mode, v.confidence) == ("accumulating", "assumed")
    assert v.running_votes == 0 and v.accumulating_votes == 0


def test_rejects_boundary_straddle(site_config):
    # early 0.5 and late 14 min are outside 2..13 -> no vote
    v = cap.detect_average_mode(samples(5, "run", e=0.5, late=14.0),
                                site_config)
    assert v.confidence == "assumed" and v.running_votes == 0


def test_rejects_unstable_load(site_config):
    # 3.0 -> 5.0 : change 2/5 = 0.4 > 0.25
    v = cap.detect_average_mode(samples(5, "run", load_l=5.0), site_config)
    assert v.confidence == "assumed" and v.running_votes == 0


def test_rejects_near_zero_draw(site_config):
    v = cap.detect_average_mode(samples(5, "run", load_e=0.2, load_l=0.2),
                                site_config)
    assert v.confidence == "assumed" and v.running_votes == 0


def test_configured_mode_bypasses_detection(site_config):
    c = cfg(site_config, quarter_hour_average_mode="running")
    v = cap.detect_average_mode(samples(5, "acc"), c)
    assert (v.mode, v.confidence) == ("running", "configured")


# ---- stay_under_percent -----------------------------------------------------
# state(): elapsed 7.5 min -> 0.125 h left.

@pytest.mark.parametrize("pct,peak,expected", [
    (80.0, 0.0, 2.0),      # floor 2.5 * 0.8
    (80.0, 3.0, 2.4),      # month peak 3.0 * 0.8
    (100.0, 0.0, 2.5),
    (100.0, 3.0, 3.0),
    (50.0, 2.0, 1.25),     # ceiling is the floor 2.5
])
def test_charging_ceiling(site_config, pct, peak, expected):
    c = cfg(site_config, stay_under_percent=pct)
    assert cap.charging_ceiling_kw(state(peak=peak), c) == pytest.approx(expected)
    assert cap.ceiling_kw(state(peak=peak), c) == max(2.5, peak)   # unchanged


@pytest.mark.parametrize("pct,peak,expected", [
    (80.0, 0.0, 2.4),      # (2.0*0.25 - 0.2)/0.125
    (80.0, 3.0, 3.2),      # (2.4*0.25 - 0.2)/0.125
    (100.0, 0.0, 3.4),     # (2.5*0.25 - 0.2)/0.125 : unchanged behaviour
    (100.0, 3.0, 4.4),     # (3.0*0.25 - 0.2)/0.125
])
def test_budget_uses_the_charging_ceiling(site_config, pct, peak, expected):
    c = cfg(site_config, stay_under_percent=pct)
    assert cap.budget_kw(state(energy=0.2, peak=peak), c) == pytest.approx(expected)


def test_budget_can_go_negative_under_the_reduced_ceiling(site_config):
    c = cfg(site_config, stay_under_percent=80.0)
    # (2.0*0.25 - 0.6)/0.125 = -0.8 ; the full ceiling would give +0.2
    assert cap.budget_kw(state(energy=0.6), c) == pytest.approx(-0.8)
    full = cfg(site_config, stay_under_percent=100.0)
    assert cap.budget_kw(state(energy=0.6), full) == pytest.approx(0.2)


def test_shave_ignores_stay_under_percent(site_config):
    # ceiling = floor 2.5, allowance 0.625; projected 0.3 + 3.0*0.125 = 0.675
    # -> needed (0.675-0.625)/0.125 = 0.4 kW, below every clamp
    s = state(offtake=3.0, energy=0.3, peak=0.0)
    a = cap.shave_kw(s, cfg(site_config, stay_under_percent=100.0))
    b = cap.shave_kw(s, cfg(site_config, stay_under_percent=50.0))
    assert a == pytest.approx(0.4) and b == pytest.approx(0.4)


def test_no_shave_when_between_charging_and_real_ceiling(site_config):
    # projected 0.175 + 3.0*0.125 = 0.55 kWh -> 2.2 kW average: above the
    # 80 % charging ceiling (2.0) but under the real ceiling (2.5): no shave.
    s = state(offtake=3.0, energy=0.175, peak=0.0)
    assert cap.shave_kw(s, cfg(site_config, stay_under_percent=80.0)) == 0.0
