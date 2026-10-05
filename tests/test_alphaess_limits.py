"""battery.max_charge_sensor / max_discharge_sensor: the inverter's own limits.

A readable sensor (W or kW, above 0) replaces battery.max_charge_kw /
max_discharge_kw for that cycle; otherwise the numeric key is the fallback
and records carry battery_limits_fallback.
"""
from pathlib import Path

import pytest

import capacity as cap
import config
from alphaess_support import battery_config
from config import ConfigError, from_dict
from test_alphaess_soc import cheap_power_with_history
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    STEP, T0, env, fields_of)
from test_config import base
from test_peak_guard import (  # noqa: F401  (make_guard is a fixture)
    PEAK_ARGS, _cfg, make_guard)
from test_rules import FLAT, NEG, SHAVE, go

MAXC = "sensor.alphaess_battery_max_charge_power"
MAXD = "sensor.alphaess_battery_max_discharge_power"
SENSORS = ["max_charge_sensor: %s" % MAXC, "max_discharge_sensor: %s" % MAXD]


# ---- config.with_limits ------------------------------------------------------------

def test_with_limits_returns_a_new_config_and_never_mutates(site_config):
    new = config.with_limits(site_config, 3.5, 2.0)
    assert (new.max_charge_kw, new.max_discharge_kw) == (3.5, 2.0)
    assert (site_config.max_charge_kw, site_config.max_discharge_kw) == (5.0, 5.0)
    assert new is not site_config


def test_with_limits_replaces_each_direction_on_its_own(site_config):
    only_c = config.with_limits(site_config, 3.0, None)
    assert (only_c.max_charge_kw, only_c.max_discharge_kw) == (3.0, 5.0)
    only_d = config.with_limits(site_config, None, 2.0)
    assert (only_d.max_charge_kw, only_d.max_discharge_kw) == (5.0, 2.0)
    assert config.with_limits(site_config) is site_config


@pytest.mark.parametrize("bad", [0, 0.0, -1.0, "3", True, None, float("nan")])
def test_with_limits_ignores_values_that_are_not_above_zero(site_config, bad):
    new = config.with_limits(site_config, bad, bad)
    assert (new.max_charge_kw, new.max_discharge_kw) == (5.0, 5.0)


def test_limits_actually_change_the_proposed_power(site_config):
    limited = config.with_limits(site_config, 3.0, 2.0)
    assert go(site_config, [NEG] * 3).target_power_kw == pytest.approx(5.0)
    d = go(limited, [NEG] * 3)
    assert (d.selector, d.action) == ("S1", "charge")
    assert d.target_power_kw == pytest.approx(3.0)


def test_discharge_limit_caps_the_shave(site_config):
    limited = config.with_limits(site_config, None, 1.0)
    assert go(site_config, [FLAT] * 3, g=SHAVE).target_power_kw > 1.0
    d = go(limited, [FLAT] * 3, g=SHAVE)
    assert (d.selector, d.action) == ("S0", "discharge")
    assert d.target_power_kw == pytest.approx(1.0)


# ---- config keys -----------------------------------------------------------------

def test_defaults_are_the_numeric_fallback():
    c = from_dict(base())
    assert c.max_charge_sensor is None and c.max_discharge_sensor is None
    assert (c.max_charge_kw, c.max_discharge_kw) == (5.0, 5.0)
    c = from_dict(base(battery__max_charge_sensor=MAXC,
                       battery__max_discharge_sensor=MAXD))
    assert (c.max_charge_sensor, c.max_discharge_sensor) == (MAXC, MAXD)


@pytest.mark.parametrize("key", ["max_charge_sensor", "max_discharge_sensor"])
@pytest.mark.parametrize("bad", ["power", "Sensor.x", 4, ""])
def test_sensor_keys_must_be_entity_ids(key, bad):
    with pytest.raises(ConfigError, match="battery." + key):
        from_dict(base(**{"battery__" + key: bad}))


def test_sensor_keys_are_not_in_the_fingerprint():
    a = from_dict(base())
    b = from_dict(base(battery__max_charge_sensor=MAXC,
                       battery__max_discharge_sensor=MAXD))
    assert a.fingerprint() == b.fingerprint()


# ---- planner adapter ---------------------------------------------------------------

def limits_config(env, lines=SENSORS, extra=""):
    battery_config(env.config_path, lines, extra=extra)


def set_sensor(env, entity, value, unit="W"):
    attrs = {} if unit is None else {"unit_of_measurement": unit}
    env.state.set(entity, value, attrs)


def spy_config(env, monkeypatch):
    """The config object each decide() call receives."""
    seen = []
    real = env.mod.rules.decide

    def spy(traj, price_map, bat, grid, cfg, now, **kw):
        seen.append(cfg)
        return real(traj, price_map, bat, grid, cfg, now, **kw)

    monkeypatch.setattr(env.mod.rules, "decide", spy)
    return seen


@pytest.mark.parametrize("c_val, c_unit, d_val, d_unit", [
    ("4000", "W", "3000", "W"), ("4.0", "kW", "3.0", "kW"),
    ("4000", "W", "3.0", "kW")])
def test_readings_replace_the_limits_in_kw(env, monkeypatch, c_val, c_unit,
                                           d_val, d_unit):
    seen = spy_config(env, monkeypatch)
    limits_config(env)
    set_sensor(env, MAXC, c_val, c_unit)
    set_sensor(env, MAXD, d_val, d_unit)
    env.run(T0)
    assert seen[0].max_charge_kw == pytest.approx(4.0)
    assert seen[0].max_discharge_kw == pytest.approx(3.0)
    assert "battery_limits_fallback" not in fields_of(
        env.decisions()[0])["degraded"]


def test_the_charge_limit_caps_the_proposal_power(env, monkeypatch):
    cheap_power_with_history(env, monkeypatch)
    off = "capacity_tariff:\n  enabled: false\n"     # no budget in the way
    limits_config(env, [], extra=off)
    env.run(T0)
    assert fields_of(env.decisions()[0])["power"] == "5.00kW"
    limits_config(env, SENSORS[:1], extra=off)
    set_sensor(env, MAXC, "4000")
    env.run(T0 + STEP)
    keys = fields_of(env.decisions()[1])
    assert keys["action"] == "charge" and keys["power"] == "4.00kW"


def test_the_limit_follows_the_sensor_cycle_by_cycle(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, SENSORS[:1])
    set_sensor(env, MAXC, "4000")
    env.run(T0)
    set_sensor(env, MAXC, "2500")
    env.run(T0 + STEP)
    assert [c.max_charge_kw for c in seen] == [pytest.approx(4.0),
                                               pytest.approx(2.5)]


@pytest.mark.parametrize("value, unit", [
    ("0", "W"), ("-500", "W"), ("abc", "W"), ("unavailable", "W"),
    ("unknown", "W"), ("4000", None), ("4000", "A"), ("nan", "W")])
def test_unreadable_sensor_falls_back_to_the_numeric_key(env, monkeypatch,
                                                         value, unit):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["max_charge_kw: 2.0", "max_discharge_kw: 1.5"]
                  + SENSORS)
    set_sensor(env, MAXC, value, unit)
    set_sensor(env, MAXD, "3000")                 # this one is fine
    env.run(T0)
    assert seen[0].max_charge_kw == pytest.approx(2.0)      # fallback
    assert seen[0].max_discharge_kw == pytest.approx(3.0)   # reading
    assert "battery_limits_fallback" in fields_of(
        env.decisions()[0])["degraded"]


def test_missing_entity_falls_back_and_is_marked(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["max_discharge_kw: 1.5"] + SENSORS[1:])
    env.run(T0)                                   # sensor never created
    assert seen[0].max_discharge_kw == pytest.approx(1.5)
    assert "battery_limits_fallback" in fields_of(
        env.decisions()[0])["degraded"]


def test_without_sensors_nothing_changes(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    set_sensor(env, MAXC, "4000")                 # present but not configured
    env.run(T0)
    assert (seen[0].max_charge_kw, seen[0].max_discharge_kw) == (5.0, 5.0)
    assert "battery_limits_fallback" not in fields_of(
        env.decisions()[0])["degraded"]


def test_the_loaded_config_is_not_mutated(env):
    limits_config(env, SENSORS)
    set_sensor(env, MAXC, "4000")
    set_sensor(env, MAXD, "3000")
    env.run(T0)
    assert env.mod._config.max_charge_kw == 5.0
    assert env.mod._config.max_discharge_kw == 5.0


# ---- peak guard --------------------------------------------------------------------

def guard_with_limits(make_guard, lines=SENSORS):
    g = make_guard().at(12, 7, 30)
    battery_config(Path(g.mod.CONFIG_PATH), lines, mode="running")
    g.st.units[MAXC] = "W"
    g.st.units[MAXD] = "W"
    return g


def test_guard_caps_the_shave_at_the_inverter_limit(make_guard):
    plain = make_guard().at(12, 7, 30)
    plain.tick(**PEAK_ARGS)
    uncapped = plain.discharges[0][1]
    assert uncapped > 1.0
    g = guard_with_limits(make_guard)
    g.tick(**PEAK_ARGS, **{MAXC: "4000", MAXD: "1000"})
    assert g.discharges[0][1] == pytest.approx(1.0)
    assert "battery_limits_fallback" not in g.discharges[0][2].degraded_inputs


def test_guard_kw_unit_is_read(make_guard):
    g = guard_with_limits(make_guard)
    g.st.units[MAXD] = "kW"
    g.tick(**PEAK_ARGS, **{MAXC: "4000", MAXD: "1.5"})
    assert g.discharges[0][1] == pytest.approx(1.5)


@pytest.mark.parametrize("value, unit", [
    ("unavailable", "W"), ("0", "W"), ("-5", "W"), ("x", "W"),
    ("1000", "A")])
def test_guard_falls_back_and_marks_it(make_guard, value, unit):
    g = guard_with_limits(make_guard, ["max_discharge_kw: 1.2"] + SENSORS)
    g.st.units[MAXD] = unit
    g.tick(**PEAK_ARGS, **{MAXC: "4000", MAXD: value})
    assert g.discharges[0][1] == pytest.approx(1.2)
    assert "battery_limits_fallback" in g.discharges[0][2].degraded_inputs


def test_guard_without_sensors_has_no_marker(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert "battery_limits_fallback" not in g.discharges[0][2].degraded_inputs
