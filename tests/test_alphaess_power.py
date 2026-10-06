"""battery.power_sensor: household draw measured from the battery power.

household draw (what the house takes from the grid WITHOUT the battery
helping) = max(0, offtake + battery_discharge), discharge positive.
"""
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

import capacity as cap
import config
from alphaess_support import battery_config
from config import ConfigError, from_dict
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    OFFTAKE_ENTITY, STEP, T0, _charge_at_budget, _real_driver, env, fields_of)
from test_config import base
from test_peak_guard import (  # noqa: F401  (make_guard is a fixture)
    PEAK_ARGS, _cfg, make_guard)

PWR = "sensor.alphaess_power_battery"
RUNNING = ("capacity_tariff:\n  quarter_hour_average_mode: running\n"
           "  stay_under_percent: 80\n")
TOTAL = 2.0 * 0.25 / (7 / 60.0)          # 4.2857 kW: 2.0 kW ceiling, 7 min left


def at(h, m, s=0):
    return datetime(2026, 9, 29, h, m, s)


@pytest.fixture
def c80(site_config):
    return replace(site_config, stay_under_percent=80.0)


def gstate(offtake, discharge=None, own=0.0, energy=0.0, elapsed=8.0):
    return cap.GridState(offtake, at(14, 0), energy, elapsed,
                         energy / (elapsed / 60.0), 0.0, False,
                         own_grid_charge_kw=own, battery_discharge_kw=discharge)


# ---- capacity -------------------------------------------------------------------

def test_battery_covering_the_house_counts_as_household_draw(c80):
    # house 3 kW, covered by the battery: the meter reads 0
    s = gstate(0.0, discharge=3.0)
    assert cap.household_draw_kw(s) == pytest.approx(3.0)
    assert cap.budget_kw(s, c80) == pytest.approx(TOTAL - 3.0)


def test_grid_charging_does_not_change_the_household_draw(c80):
    # the planner charges at 2.14 kW from the grid: meter 5.14, battery -2.14
    charging = gstate(5.14, discharge=-2.14)
    covered = gstate(0.0, discharge=3.0)
    assert cap.household_draw_kw(charging) == pytest.approx(3.0)
    assert cap.budget_kw(charging, c80) == pytest.approx(
        cap.budget_kw(covered, c80))


def test_own_grid_charge_is_not_subtracted_again_with_a_sensor(c80):
    s = gstate(5.14, discharge=-2.14, own=2.14)
    assert cap.household_draw_kw(s) == pytest.approx(3.0)


def test_result_is_clamped_at_zero():
    assert cap.household_draw_kw(gstate(1.0, discharge=-3.0)) == 0.0


def test_pv_surplus_is_not_household_draw():
    # PV exports 2 kW (offtake 0 here), battery idle: nothing drawn
    assert cap.household_draw_kw(gstate(0.0, discharge=0.0)) == 0.0


def test_without_a_sensor_the_old_formula_applies():
    s = gstate(3.0, discharge=None, own=2.0)
    assert cap.household_draw_kw(s) == pytest.approx(1.0)
    assert gstate(1.0).battery_discharge_kw is None


def test_build_state_carries_the_battery_power(c80):
    s = cap.build_state(1.0, 0.0, at(14, 8), 0.0, c80, battery_discharge_kw=2.5)
    assert s.battery_discharge_kw == 2.5
    assert cap.build_state(1.0, 0.0, at(14, 8), 0.0, c80
                           ).battery_discharge_kw is None


@pytest.mark.parametrize("positive, reading, expected", [
    ("discharge", 3.0, 3.0), ("discharge", -2.0, -2.0),
    ("charge", 3.0, -3.0), ("charge", -2.0, 2.0)])
def test_sign_convention(positive, reading, expected):
    assert cap.battery_discharge_from_power(reading, positive) == expected


# ---- config ---------------------------------------------------------------------

def test_defaults_and_values():
    c = from_dict(base())
    assert c.power_sensor is None and c.power_positive == "discharge"
    c = from_dict(base(battery__power_sensor="sensor.alphaess_power_battery",
                       battery__power_positive="charge"))
    assert c.power_sensor == "sensor.alphaess_power_battery"
    assert c.power_positive == "charge"


@pytest.mark.parametrize("key, bad", [
    ("power_sensor", "power"), ("power_sensor", 3), ("power_positive", "up"),
    ("power_positive", "Charge"), ("power_positive", 1)])
def test_invalid_values_are_rejected(key, bad):
    with pytest.raises(ConfigError, match="battery." + key):
        from_dict(base(**{"battery__" + key: bad}))


def test_not_in_the_fingerprint():
    a = from_dict(base())
    b = from_dict(base(battery__power_sensor="sensor.alphaess_power_battery",
                       battery__power_positive="charge"))
    assert a.fingerprint() == b.fingerprint()


# ---- planner adapter --------------------------------------------------------------

def planner_cfg(env, positive=None, extra=RUNNING):
    lines = ["power_sensor: %s" % PWR]
    if positive:
        lines.append("power_positive: %s" % positive)
    battery_config(env.config_path, lines, extra=extra)


def set_pwr(env, value, unit="W"):
    attrs = {} if unit is None else {"unit_of_measurement": unit}
    env.state.set(PWR, value, attrs)


def run_two(env, seen):
    env.state.set(OFFTAKE_ENTITY, "0.0", {"unit_of_measurement": "kW"})
    env.run(T0)
    return seen


@pytest.mark.parametrize("value, unit", [("3000", "W"), ("3.0", "kW")])
def test_unit_is_read_and_converted(env, value, unit):
    seen = _charge_at_budget(env)
    planner_cfg(env)
    set_pwr(env, value, unit)
    env.state.set(OFFTAKE_ENTITY, "0.0", {"unit_of_measurement": "kW"})
    env.run(T0)
    assert seen[0].battery_discharge_kw == pytest.approx(3.0)
    assert cap.household_draw_kw(seen[0]) == pytest.approx(3.0)


def test_power_positive_charge_inverts_the_sign(env):
    seen = _charge_at_budget(env)
    planner_cfg(env, positive="charge")
    set_pwr(env, "-3000")                        # negative = discharging here
    env.state.set(OFFTAKE_ENTITY, "0.0", {"unit_of_measurement": "kW"})
    env.run(T0)
    assert seen[0].battery_discharge_kw == pytest.approx(3.0)


def test_review_scenario_budget_is_the_allowed_rate_minus_the_house(env):
    seen = _charge_at_budget(env)
    planner_cfg(env)
    set_pwr(env, "3000")
    env.state.set(OFFTAKE_ENTITY, "0.0", {"unit_of_measurement": "kW"})
    env.run(T0)
    grid = seen[0]
    cfg = env.mod._config
    assert cap.household_draw_kw(grid) == pytest.approx(3.0)
    assert cap.budget_kw(grid, cfg) == pytest.approx(
        min(cap.allowed_offtake_kw(grid, cfg) - 3.0, cfg.max_charge_kw))
    assert "battery_power_unavailable" not in fields_of(
        env.decisions()[0])["degraded"]


@pytest.mark.parametrize("value, unit", [
    ("unavailable", "W"), ("unknown", "W"), ("abc", "W"), ("3000", None),
    ("3000", "A"), ("nan", "W")])
def test_unreadable_sensor_falls_back_and_is_marked(env, value, unit):
    seen = _charge_at_budget(env)
    planner_cfg(env)
    set_pwr(env, value, unit)
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    assert seen[0].battery_discharge_kw is None
    assert cap.household_draw_kw(seen[0]) == pytest.approx(1.5)   # old formula
    assert "battery_power_unavailable" in fields_of(
        env.decisions()[0])["degraded"]


def test_missing_entity_falls_back_and_is_marked(env):
    seen = _charge_at_budget(env)
    planner_cfg(env)                             # entity never created
    env.run(T0)
    assert seen[0].battery_discharge_kw is None
    assert "battery_power_unavailable" in fields_of(
        env.decisions()[0])["degraded"]


def test_no_sensor_configured_means_no_marker_and_no_reading(env):
    seen = _charge_at_budget(env)
    env.state.set(PWR, "3000", {"unit_of_measurement": "W"})
    env.run(T0)
    assert seen[0].battery_discharge_kw is None
    assert "battery_power_unavailable" not in fields_of(
        env.decisions()[0])["degraded"]


def test_household_draw_does_not_flip_while_the_planner_charges(env):
    # real driver, so the planner's own charge is in force from cycle 2 on
    _real_driver(env)
    battery_config(env.config_path, ["power_sensor: %s" % PWR],
                   extra="inverter:\n  type: fakeinv\n" + RUNNING)
    seen = _charge_at_budget(env)
    # cycle 1: the battery covers a 3 kW house, meter 0
    set_pwr(env, "3000")
    env.state.set(OFFTAKE_ENTITY, "0.0", {"unit_of_measurement": "kW"})
    env.run(T0)
    charge_kw = env.mod._last_grid_charge[1]
    assert charge_kw > 0
    # cycle 2: the planner now charges: battery takes charge_kw, the meter
    # shows house + charge_kw (the battery no longer covers the house)
    set_pwr(env, str(-1000 * charge_kw))
    env.state.set(OFFTAKE_ENTITY, "%.4f" % (3.0 + charge_kw),
                  {"unit_of_measurement": "kW"})
    env.run(T0 + STEP)
    first, second = (cap.household_draw_kw(g) for g in seen[:2])
    assert first == pytest.approx(3.0)
    assert second == pytest.approx(3.0)
    assert seen[1].own_grid_charge_kw == pytest.approx(charge_kw)   # not used


# ---- peak guard --------------------------------------------------------------------

def guard_with_power(make_guard, positive=None):
    g = make_guard().at(12, 7, 30)
    lines = ["power_sensor: %s" % PWR]
    if positive:
        lines.append("power_positive: %s" % positive)
    battery_config(Path(g.mod.CONFIG_PATH), lines, mode="running")
    return g


def spy_build_state(g, monkeypatch):
    built = []
    real = g.mod.capacity.build_state

    def spy(*a, **kw):
        state = real(*a, **kw)
        built.append(state)
        return state

    monkeypatch.setattr(g.mod.capacity, "build_state", spy)
    return built


def test_guard_passes_the_battery_power_to_its_state(make_guard, monkeypatch):
    g = guard_with_power(make_guard)
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "W"
    g.tick(**PEAK_ARGS, **{PWR: "3000"})
    assert built[-1].battery_discharge_kw == pytest.approx(3.0)
    rec = g.discharges[0][2]
    grid = cap.build_state(5.0, 0.0, g.now, 2.5, _cfg(),
                           reported_average_kw=4.0,
                           battery_discharge_kw=3.0)
    assert rec.budget_kw == pytest.approx(cap.budget_kw(grid, _cfg()))


def test_guard_honours_power_positive(make_guard, monkeypatch):
    g = guard_with_power(make_guard, positive="charge")
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "kW"
    g.tick(**PEAK_ARGS, **{PWR: "-2.0"})
    assert built[-1].battery_discharge_kw == pytest.approx(2.0)


@pytest.mark.parametrize("raw, unit", [("unavailable", "W"), ("x", "W"),
                                        ("3000", "A")])
def test_guard_falls_back_when_unreadable(make_guard, monkeypatch, raw, unit):
    g = guard_with_power(make_guard)
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = unit
    g.tick(**PEAK_ARGS, **{PWR: raw})
    assert built[-1].battery_discharge_kw is None
    assert len(g.discharges) == 1                # still shaves


def test_guard_does_not_count_its_own_discharge_twice(make_guard, monkeypatch):
    g = make_guard(extra="inverter:\n  type: fakeinv\n").at(12, 7, 30)
    battery_config(Path(g.mod.CONFIG_PATH), ["power_sensor: %s" % PWR],
                   extra="inverter:\n  type: fakeinv\n", mode="running")
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "W"
    g.tick(**PEAK_ARGS, **{PWR: "0"})            # shave starts, commanded
    shave = g.discharges[0][1]
    assert shave > 0
    del built[:]
    # next tick: the battery now delivers the commanded shave plus 1 kW for the house
    g.tick(**PEAK_ARGS, **{PWR: str(int((shave + 1.0) * 1000))})
    metered, unshaved = built[-2], built[-1]
    assert unshaved.offtake_kw == pytest.approx(5.0 + shave)
    for state in (metered, unshaved):
        assert cap.household_draw_kw(state) == pytest.approx(6.0 + shave)


def test_guard_unreadable_with_inverted_sign_still_falls_back(
        make_guard, monkeypatch):
    g = guard_with_power(make_guard, positive="charge")
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "A"
    g.tick(**PEAK_ARGS, **{PWR: "3000"})
    assert built[-1].battery_discharge_kw is None
