"""battery.reserve_sensor: the inverter's own minimum charge replaces
battery.reserve_percent when readable; otherwise the numeric key is the
fallback and records carry battery_reserve_fallback.
"""
import pytest

import config
from alphaess_support import battery_config
from config import ConfigError, from_dict
from test_alphaess_limits import limits_config, set_sensor, spy_config
from test_battery_planner import T0, env, fields_of  # noqa: F401
from test_config import base
from test_peak_guard import PEAK_ARGS, make_guard  # noqa: F401
from pathlib import Path

RES = "sensor.alphaess_discharging_cutoff_soc"
LINES = ["reserve_sensor: %s" % RES]


# ---- config -----------------------------------------------------------------

def test_with_reserve_returns_a_new_config_and_never_mutates(site_config):
    new = config.with_reserve(site_config, 12.0)
    assert new.reserve_percent == 12.0 and site_config.reserve_percent == 10.0
    assert new is not site_config


@pytest.mark.parametrize("bad", [None, -1, 100, 100.5, "10", True, float("nan")])
def test_with_reserve_ignores_values_outside_zero_to_below_100(site_config, bad):
    assert config.with_reserve(site_config, bad) is site_config


def test_with_reserve_accepts_zero(site_config):
    assert config.with_reserve(site_config, 0).reserve_percent == 0.0


def test_reserve_sensor_parsed_and_validated():
    assert from_dict(base()).reserve_sensor is None
    assert from_dict(base(battery__reserve_sensor=RES)).reserve_sensor == RES
    with pytest.raises(ConfigError):
        from_dict(base(battery__reserve_sensor="not an entity"))


# ---- planner ------------------------------------------------------------------

def test_reading_replaces_the_reserve(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["reserve_percent: 15"] + LINES)
    set_sensor(env, RES, "10", "%")
    env.run(T0)
    assert seen[0].reserve_percent == pytest.approx(10.0)
    assert "battery_reserve_fallback" not in fields_of(
        env.decisions()[0])["degraded"]


@pytest.mark.parametrize("value", ["unavailable", "unknown", "x", "-3", "100", "150"])
def test_unreadable_reading_falls_back_and_is_marked(env, monkeypatch, value):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["reserve_percent: 15"] + LINES)
    set_sensor(env, RES, value, "%")
    env.run(T0)
    assert seen[0].reserve_percent == pytest.approx(15.0)
    assert "battery_reserve_fallback" in fields_of(
        env.decisions()[0])["degraded"]


def test_missing_entity_falls_back_and_is_marked(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["reserve_percent: 15"] + LINES)
    env.run(T0)
    assert seen[0].reserve_percent == pytest.approx(15.0)
    assert "battery_reserve_fallback" in fields_of(
        env.decisions()[0])["degraded"]


def test_without_a_sensor_nothing_changes(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["reserve_percent: 15"])
    set_sensor(env, RES, "10", "%")            # present but not configured
    env.run(T0)
    assert seen[0].reserve_percent == pytest.approx(15.0)
    assert "battery_reserve_fallback" not in fields_of(
        env.decisions()[0])["degraded"]


def test_the_loaded_config_is_not_mutated(env):
    limits_config(env, ["reserve_percent: 15"] + LINES)
    set_sensor(env, RES, "10", "%")
    env.run(T0)
    assert env.mod._config.reserve_percent == 15.0


# ---- peak guard -----------------------------------------------------------------

def guard_with_reserve(make_guard):
    g = make_guard().at(12, 7, 30)
    battery_config(Path(g.mod.CONFIG_PATH), ["reserve_percent: 15"] + LINES,
                   mode="running")
    return g


def test_guard_uses_the_reading(make_guard):
    g = guard_with_reserve(make_guard)
    g.tick(**PEAK_ARGS, **{RES: "10"})
    assert "battery_reserve_fallback" not in g.discharges[0][2].degraded_inputs


def test_guard_falls_back_and_marks_it(make_guard):
    g = guard_with_reserve(make_guard)
    g.tick(**PEAK_ARGS, **{RES: "unavailable"})
    assert "battery_reserve_fallback" in g.discharges[0][2].degraded_inputs
