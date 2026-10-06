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


# ---- window alignment / build_state --------------------------------

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
    assert s.average_mode == "running"


def test_accumulating_mode_normalised(site_config):
    c = cfg(site_config, quarter_hour_average_mode="accumulating")
    # meter shows energy/15min: 0.5 kWh / 0.25 h = 2.0 kW at 7.5 min.
    # true running = 2.0 * 15 / 7.5 = 4.0 kW ; energy = 4.0*7.5/60 = 0.5
    s = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, c,
                        reported_average_kw=2.0)
    assert s.running_average_kw == pytest.approx(4.0)
    assert s.window_energy_kwh == pytest.approx(0.5)


def test_the_default_mode_is_accumulating(site_config):
    s = cap.build_state(4.0, 0.0, at(14, 7, 30), 0.0, site_config,
                        reported_average_kw=2.0)
    assert s.average_mode == "accumulating"
    assert s.running_average_kw == pytest.approx(4.0)


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


# ---- ceiling --------------------------------------------------------

@pytest.mark.parametrize("peak,expected", [
    (0.0, 2.5), (1.8, 2.5), (2.5, 2.5), (6.2, 6.2)])
def test_ceiling(site_config, peak, expected):
    assert cap.ceiling_kw(state(peak=peak), site_config) == expected


# ---- budget ---------------------------------------------------------

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


def test_budget_last_allowed_minute_with_a_one_minute_interval(site_config):
    # elapsed 13 -> remaining 2 min = 1/30 h ; (1.0-0.9)*30 = 3.0
    c = cfg(site_config, evaluation_interval_minutes=1)
    assert cap.budget_kw(state(energy=0.9, elapsed=13.0, peak=4.0),
                         c) == pytest.approx(3.0)
    assert cap.budget_kw(state(energy=0.9, elapsed=13.99, peak=4.0), c) > 0
    assert cap.budget_kw(state(energy=0.9, elapsed=14.01, peak=4.0), c) == 0.0


def test_default_interval_is_five_minutes(site_config):
    assert site_config.evaluation_interval_minutes == 5


def test_budget_is_zero_in_the_last_evaluation_interval(site_config):
    # interval 5: with 5 min left the budget exists, with less it is 0, so a
    # charge never runs past the quarter-hour boundary until the next decision.
    last_ok = state(energy=0.5, elapsed=10.0, peak=4.0)      # remaining 5.0
    assert cap.budget_kw(last_ok, site_config) > 0
    for elapsed in (10.01, 11.0, 13.0, 14.0, 14.5):
        s = state(offtake=1.5, energy=0.5, elapsed=elapsed, peak=4.0)
        assert cap.budget_kw(s, site_config) == 0.0, elapsed
        assert cap.allowed_offtake_kw(s, site_config) == 0.0, elapsed


@pytest.mark.parametrize("interval,elapsed,zero", [
    (1, 13.9, False), (1, 14.0, False), (1, 14.01, True),
    (5, 9.9, False), (5, 10.0, False), (5, 10.01, True),
    (10, 5.0, False), (10, 5.01, True),
    (15, 0.01, True),
])
def test_cutoff_is_the_evaluation_interval(site_config, interval, elapsed, zero):
    c = cfg(site_config, evaluation_interval_minutes=interval)
    s = state(energy=0.1, elapsed=elapsed, peak=4.0)
    assert (cap.budget_kw(s, c) == 0.0) is zero


def test_no_budget_minutes_never_below_the_fixed_minimum(site_config):
    assert cap.no_budget_minutes(cfg(site_config, evaluation_interval_minutes=1)) == 1.0
    assert cap.no_budget_minutes(site_config) == 5.0
    assert cap.no_budget_minutes(cfg(site_config, evaluation_interval_minutes=0)) == 1.0
    assert cap.NO_BUDGET_MINUTES == 1.0


def test_peak_shaving_is_not_cut_by_the_interval(site_config):
    s = cap.GridState(4.0, at(14, 0), 0.9, 12.0, 4.5, 2.5, False)
    five = cap.shave_kw(s, site_config)
    one = cap.shave_kw(s, cfg(site_config, evaluation_interval_minutes=1))
    assert five > 0 and five == one


def test_peak_warning_keeps_the_one_minute_margin(site_config):
    # 3 minutes left, interval 5: the guard still warns (it re-evaluates every
    # 30 s and sizes no charge), unlike the grid-charge budget.
    s = cap.GridState(6.0, at(14, 0), 0.9, 12.0, 4.5, 2.5, False)
    mem = {}
    assert cap.peak_warning_due(mem, s, site_config, 0.0) is False
    assert cap.peak_warning_due(mem, s, site_config, 30.0) is True


def test_budget_subtracts_household_draw_once(site_config):
    # Household draws 4 kW steadily: 7.5 min -> 0.5 kWh already IN
    # window_energy (metered at the connection point).
    # Total allowed rate (1.0 - 0.5)/0.125 = 4.0 kW; the house keeps drawing
    # 4.0 kW, so nothing is left for charging.
    s = state(offtake=4.0, energy=0.5, peak=4.0)
    assert cap.allowed_offtake_kw(s, site_config) == pytest.approx(4.0)
    assert cap.budget_kw(s, site_config) == pytest.approx(0.0)


# ---- shave ----------------------------------------------------------

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


# ---- euros ---------------------------------------------------------------

def test_arbitrage_value():
    assert cap.arbitrage_value_eur(2.0, 0.15) == pytest.approx(0.30)


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
