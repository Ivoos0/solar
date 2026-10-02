"""Grid-charge budget accounts for the household's own draw.

budget_kw = allowed_offtake_kw - household_draw_kw, capped at max_charge_kw.
household_draw_kw = max(0, offtake_kw - own_grid_charge_kw).
"""
from dataclasses import replace
from datetime import datetime

import pytest

from pyscript.modules import capacity as cap
from pyscript.modules import rules

TOTAL = 2.0 * 0.25 / (7 / 60.0)          # 4.2857 kW: 2.0 kW ceiling, 7 min left


def at(h, m, s=0):
    return datetime(2026, 9, 29, h, m, s)


@pytest.fixture
def c80(site_config):
    return replace(site_config, stay_under_percent=80.0)


def hstate(offtake, own=0.0, energy=0.0, elapsed=8.0, peak=0.0):
    """GridState with 7 min left by default."""
    return cap.GridState(offtake, at(14, 0), energy,
                         elapsed, energy / (elapsed / 60.0), peak, False,
                         own_grid_charge_kw=own)


def test_worked_example(c80):
    s = hstate(offtake=1.5)
    assert cap.charging_ceiling_kw(s, c80) == pytest.approx(2.0)
    assert cap.allowed_offtake_kw(s, c80) == pytest.approx(TOTAL)
    assert cap.household_draw_kw(s) == pytest.approx(1.5)
    assert cap.budget_kw(s, c80) == pytest.approx(2.7857, abs=1e-4)


def test_household_above_allowed_rate_is_negative(c80):
    b = cap.budget_kw(hstate(offtake=5.0), c80)
    assert b == pytest.approx(TOTAL - 5.0) and b < 0


def test_own_charge_is_subtracted_from_offtake(c80):
    s = hstate(offtake=3.0, own=2.0)
    assert cap.household_draw_kw(s) == pytest.approx(1.0)
    assert cap.budget_kw(s, c80) == pytest.approx(TOTAL - 1.0)


def test_own_charge_larger_than_offtake_clamps_household_to_zero(c80):
    s = hstate(offtake=1.0, own=2.5)
    assert cap.household_draw_kw(s) == 0.0
    assert cap.budget_kw(s, c80) == pytest.approx(TOTAL)


def test_own_grid_charge_defaults_to_zero(c80):
    assert hstate(1.0).own_grid_charge_kw == 0.0
    s = cap.build_state(1.0, 0.0, at(14, 8), 0.0, c80)
    assert s.own_grid_charge_kw == 0.0
    s = cap.build_state(1.0, 0.0, at(14, 8), 0.0, c80, own_grid_charge_kw=0.7)
    assert s.own_grid_charge_kw == 0.7


def test_last_minute_is_still_zero(c80):
    s = hstate(offtake=1.5, elapsed=14.5)
    assert cap.budget_kw(s, c80) == 0.0
    assert cap.allowed_offtake_kw(s, c80) == 0.0


def test_cap_at_max_charge_still_applies(c80):
    # peak 8 -> charging ceiling 6.4 -> allowed 13.7 kW, minus 0.5 household
    assert cap.budget_kw(hstate(0.5, peak=8.0), c80) == c80.max_charge_kw


def test_shave_ignores_own_grid_charge_and_household_budget(c80):
    a = cap.shave_kw(hstate(3.0, energy=0.3, peak=0.0), c80)
    b = cap.shave_kw(hstate(3.0, own=0.0, energy=0.3, peak=0.0), c80)
    assert a == b
    # same offtake, own charge differs: shave is driven by metered offtake only
    assert cap.shave_kw(hstate(3.0, own=1.0, energy=0.3), c80) == a


# ---- rules: S1 / S4 / S5 clamp and V3 use the post-household budget --------

def test_grid_power_clamps_to_post_household_budget(c80):
    class Ctx:
        grid = hstate(offtake=3.0)
        config = c80
    kw, note = rules._grid_power(Ctx, c80.max_charge_kw)
    assert kw == pytest.approx(TOTAL - 3.0)
    assert "capped by grid budget %.2f kW" % (TOTAL - 3.0) in note


def test_v3_fires_when_household_eats_the_whole_allowance(c80):
    batt = type("B", (), {"charge_percent": 50.0})()
    _, fired = rules.establish_vetoes(batt, None, c80,
                                      hstate(offtake=TOTAL + 0.5), True)
    assert "V3" in fired
    _, fired = rules.establish_vetoes(batt, None, c80,
                                      hstate(offtake=1.5), True)
    assert "V3" not in fired
