"""Sensor failure policy: freshness, bounds, a short debounce, a real driver
that only has the placeholder charge.
"""
from datetime import timedelta

import pytest

from alphaess_support import battery_config
from config import ConfigError, from_dict
from test_alphaess_limits import MAXC, MAXD, limits_config, set_sensor, spy_config
from test_alphaess_power import PWR, RUNNING
from test_alphaess_soc import SOC
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    STEP, T0, _real_driver, env, fields_of)
from test_config import base

LATE = T0 + timedelta(minutes=1)


def stamped(env, entity, value, unit, minutes_old):
    env.state.set(entity, value, {"unit_of_measurement": unit} if unit else {},
                  last_reported=T0 - timedelta(minutes=minutes_old))


# ---- config -------------------------------------------------------------------------

def test_sensor_stale_minutes_default_and_validation(site_config):
    assert site_config.sensor_stale_minutes == 15
    assert from_dict(base(timing__sensor_stale_minutes=5)).sensor_stale_minutes == 5
    for bad in (0, -1, 2.5):
        with pytest.raises(ConfigError) as e:
            from_dict(base(timing__sensor_stale_minutes=bad))
        assert "sensor_stale_minutes" in str(e.value)


# ---- freshness ----------------------------------------------------------------------

def test_a_frozen_charge_reading_counts_as_unavailable(env):
    battery_config(env.config_path, ["soc_sensor: %s" % SOC,
                                     "power_sensor: %s" % PWR], extra=RUNNING)
    set_sensor(env, PWR, "0", "W")
    stamped(env, SOC, "50", "%", 20)             # older than the 15 minute limit
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "soc_unavailable" in keys["degraded"] and "V7" in keys["vetoes"]


def test_a_fresh_charge_reading_is_used(env):
    battery_config(env.config_path, ["soc_sensor: %s" % SOC,
                                     "power_sensor: %s" % PWR], extra=RUNNING)
    set_sensor(env, PWR, "0", "W")
    stamped(env, SOC, "50", "%", 5)
    env.run(T0)
    assert "soc_unavailable" not in fields_of(env.decisions()[0])["degraded"]


def test_a_reading_without_a_timestamp_is_never_stale(env):
    battery_config(env.config_path, ["soc_sensor: %s" % SOC,
                                     "power_sensor: %s" % PWR], extra=RUNNING)
    set_sensor(env, PWR, "0", "W")
    env.state.set(SOC, "50", {})
    env.run(T0)
    assert "soc_unavailable" not in fields_of(env.decisions()[0])["degraded"]


def test_a_frozen_power_reading_holds_the_planner(env):
    battery_config(env.config_path, ["power_sensor: %s" % PWR], extra=RUNNING)
    stamped(env, PWR, "0", "W", 30)
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "battery_power_unavailable" in keys["degraded"] and "V8" in keys["vetoes"]


def test_a_frozen_limit_falls_back_to_the_configured_number(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["max_charge_kw: 2.0", "max_charge_sensor: %s" % MAXC])
    stamped(env, MAXC, "4000", "W", 40)
    env.run(T0)
    assert seen[0].max_charge_kw == pytest.approx(2.0)
    assert "battery_limits_fallback" in fields_of(env.decisions()[0])["degraded"]


# ---- bounds -------------------------------------------------------------------------

def test_an_implausible_limit_is_not_used(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["max_discharge_kw: 5.0", "max_discharge_sensor: %s" % MAXD])
    set_sensor(env, MAXD, "65000", "W")          # 65 kW on a 5 kW inverter
    env.run(T0)
    assert seen[0].max_discharge_kw == pytest.approx(5.0)
    assert "battery_limits_fallback" in fields_of(env.decisions()[0])["degraded"]


def test_a_limit_up_to_four_times_the_fallback_is_used(env, monkeypatch):
    seen = spy_config(env, monkeypatch)
    limits_config(env, ["max_discharge_kw: 5.0", "max_discharge_sensor: %s" % MAXD])
    set_sensor(env, MAXD, "10000", "W")
    env.run(T0)
    assert seen[0].max_discharge_kw == pytest.approx(10.0)


def test_an_implausible_battery_power_holds_the_planner(env):
    battery_config(env.config_path, ["power_sensor: %s" % PWR], extra=RUNNING)
    set_sensor(env, PWR, "900000", "W")          # 900 kW
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "battery_power_unavailable" in keys["degraded"] and "V8" in keys["vetoes"]


# ---- debounce -----------------------------------------------------------------------

def test_a_power_dropout_of_one_cycle_does_not_hold(env):
    battery_config(env.config_path, ["power_sensor: %s" % PWR], extra=RUNNING)
    set_sensor(env, PWR, "0", "W")
    env.run(T0)
    env.state.set(PWR, "unavailable", {"unit_of_measurement": "W"})
    env.run(T0 + STEP)
    keys = fields_of(env.decisions()[1])
    assert "battery_power_last_good" in keys["degraded"]
    assert "V8" not in keys["vetoes"]
    env.run(T0 + 2 * STEP)
    env.run(T0 + 3 * STEP)                       # beyond 2 intervals: unavailable
    assert "V8" in fields_of(env.decisions()[3])["vetoes"]


# ---- a real driver with only the placeholder charge -----------------------------------

def test_a_real_driver_without_a_charge_reading_holds(env):
    env.soc_sensor = False
    _real_driver(env)
    (env.tmp / "drivers" / "inverter_fakeinv.py").write_text(
        "def send(action, target_power_kw):\n    return True\n", encoding="utf-8")
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "V7" in keys["vetoes"] and keys["action"] == "idle"
    assert "soc_unavailable" in keys["degraded"]


# ---- the peak guard applies the same rules ------------------------------------------------

from datetime import datetime  # noqa: E402

from test_alphaess_power import guard_with_power, spy_build_state  # noqa: E402,F401
from test_peak_guard import PEAK_ARGS, TZ, make_guard  # noqa: E402,F401


def test_guard_ignores_a_frozen_battery_power(make_guard, monkeypatch):
    g = guard_with_power(make_guard)
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "W"
    g.st.updated[PWR] = datetime(2026, 9, 30, 11, 0, tzinfo=TZ)     # over an hour old
    g.tick(**PEAK_ARGS, **{PWR: "3000"})
    assert built[-1].battery_discharge_kw is None


def test_guard_uses_a_fresh_battery_power(make_guard, monkeypatch):
    g = guard_with_power(make_guard)
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "W"
    g.st.updated[PWR] = datetime(2026, 9, 30, 12, 7, 0, tzinfo=TZ)
    g.tick(**PEAK_ARGS, **{PWR: "3000"})
    assert built[-1].battery_discharge_kw == pytest.approx(3.0)


def test_guard_ignores_an_implausible_battery_power(make_guard, monkeypatch):
    g = guard_with_power(make_guard)
    built = spy_build_state(g, monkeypatch)
    g.st.units[PWR] = "W"
    g.tick(**PEAK_ARGS, **{PWR: "900000"})
    assert built[-1].battery_discharge_kw is None


# ---- the inverter refuses commands ------------------------------------------------------

from alphaess_support import bump_mtime  # noqa: E402
from test_battery_planner import NOTIFY, PRICE_ENTITY  # noqa: E402,F401


def _failing_driver(env, ok=False):
    _real_driver(env)
    (env.tmp / "drivers" / "inverter_fakeinv.py").write_text(
        "def send(action, target_power_kw):\n    return %s\n"
        "def read_charge_percent():\n    return 40.0\n" % ok, encoding="utf-8")


def _negative_prices(env):
    from test_battery_planner import price_entries
    entries = [dict(e, price=-0.20) for e in price_entries()]
    env.state.set(PRICE_ENTITY, "-0.20", {"prices": entries})


def test_three_refused_commands_send_one_mail_and_mark_the_record(env, monkeypatch):
    _failing_driver(env)
    from test_battery_planner import _history
    monkeypatch.setattr(env.mod, "read_usage_history", lambda cfg, local: _history(3))
    _negative_prices(env)
    for k in range(6):
        env.run(T0 + k * STEP)
    mails = [m for m in env.service.of("notify", NOTIFY)
             if "does not accept commands" in m[2]["title"]]
    assert len(mails) == 1
    assert "inverter_send_failed" in fields_of(env.decisions()[-1])["degraded"]


def test_a_working_driver_has_no_failure_streak(env, monkeypatch):
    _failing_driver(env, ok=True)
    from test_battery_planner import _history
    monkeypatch.setattr(env.mod, "read_usage_history", lambda cfg, local: _history(3))
    _negative_prices(env)
    for k in range(4):
        env.run(T0 + k * STEP)
    assert not [m for m in env.service.of("notify", NOTIFY)
                if "does not accept commands" in m[2]["title"]]
    assert "inverter_send_failed" not in fields_of(env.decisions()[-1])["degraded"]


def test_a_failed_planner_mail_is_also_a_persistent_notification(env):
    env.write_config()
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.service.fail = {("notify", "test_notifier")}
    env.run(T0)
    pn = env.service.of("persistent_notification", "create")
    assert pn and pn[0][2]["notification_id"].startswith("battery_planner_")


# ---- health attributes -------------------------------------------------------------------

def test_the_action_sensor_lists_the_inputs_that_are_down(env):
    battery_config(env.config_path, ["soc_sensor: %s" % SOC],
                   extra="capacity_tariff:\n  enabled: false\nhistory:\n  enabled: false\n",
                   alert_lines=("sensor_enabled: true",))
    env.run(T0)                                  # the charge sensor does not exist
    attrs = [a for e, _, a in env.state.published if e == "sensor.battery_planner_action"]
    assert attrs and SOC in attrs[-1]["inputs_down"]


def test_the_action_sensor_says_none_when_all_inputs_are_up(env):
    env.run(T0)
    attrs = [a for e, _, a in env.state.published if e == "sensor.battery_planner_action"]
    assert attrs and "inputs_down" in attrs[-1]
