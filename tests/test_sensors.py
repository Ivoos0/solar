"""The planner publishes its state as Home Assistant sensors (state.set).

Uses the `env` fixture of test_battery_planner; its FakeState records every
state.set(..., new_attributes=...) call in `state.published`.
"""
import json
from datetime import timedelta

import pytest

import config
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    MONTH_PEAK_ENTITY, PRICE_ATTRIBUTE, PRICE_ENTITY, QUARTER_AVG_ENTITY, STEP,
    T0, env, fields_of, price_entries)

ACTION = "sensor.battery_planner_action"
BUDGET = "sensor.battery_planner_budget"
HALTED = "binary_sensor.battery_planner_halted"
OFFTAKE = "sensor.slimmelezer_power_consumed"


def last(env, entity):
    """(value, attrs) of the newest publish of `entity`, or None."""
    for e, value, attrs in reversed(env.state.published):
        if e == entity:
            return value, attrs
    return None


def kw(text):
    return float(text.replace("kW", ""))


def test_normal_cycle_publishes_the_three_sensors(env):
    env.run()
    log = fields_of(env.decisions()[0])
    assert sorted(set(e for e, _, _ in env.state.published)) == sorted(
        [ACTION, BUDGET, HALTED])
    value, a = last(env, ACTION)
    assert value == log["action"]
    assert a["friendly_name"] == "Battery planner action"
    assert a["icon"].startswith("mdi:")
    assert a["power_kw"] == pytest.approx(kw(log["power"]), abs=0.005)
    assert a["selector"] == log["selector"]
    assert a["vetoes"] == log["vetoes"].replace(",", ", ")
    assert a["degraded"] == log["degraded"].replace(",", ", ")
    assert "soc_stubbed" in a["degraded"] and isinstance(a["degraded"], str)
    assert a["why"] == log["why"].strip('"')            # same words as the log
    assert isinstance(a["why"], str) and "\n" not in a["why"]
    assert a["soc_percent"] is None                     # charge is a stub: not a measurement
    assert a["decided_at"] == log["_ts"] and a["decided_at"].endswith("+02:00")
    assert a["source"] == "planner"

    value, a = last(env, BUDGET)
    assert value == pytest.approx(kw(log["budget"]), abs=0.005)
    assert a["unit_of_measurement"] == "kW" and a["device_class"] == "power"
    assert a["ceiling_kw"] == pytest.approx(kw(log["ceiling"]), abs=0.005)
    assert a["average_kw"] == pytest.approx(kw(log["avg"]), abs=0.005)
    assert a["month_peak_kw"] == pytest.approx(3.0)

    value, a = last(env, HALTED)
    assert value == "off"
    assert a["cause"] is None and a["since"] is None
    assert a["device_class"] == "problem"


def test_published_values_are_ui_friendly(env):
    env.run()
    for entity, value, attrs in env.state.published:
        assert len(str(value)) <= 255                    # state strings are limited
        json.dumps(attrs)                                # plain JSON types only
        assert all(isinstance(k, str) for k in attrs)
        assert attrs["friendly_name"]


def test_published_once_per_cycle(env):
    env.run()
    n = len(env.state.published)
    assert n == 3
    env.run(T0 + STEP)
    assert len(env.state.published) == 2 * n


def test_soc_is_published_when_the_charge_is_a_measurement(env):
    env.run()
    # (the fixture driver reports a stub, so the attribute is null there)
    fake = env.mod
    cfg = fake._config
    record = type("R", (), dict(
        action="idle", target_power_kw=0.0, selector="S6", vetoes_applied=[],
        reasoning="x", degraded_inputs=[], charge_percent=55.5,
        timestamp=T0, source="planner", budget_kw=None, ceiling_kw=None,
        running_average_kw=None))()
    env.state.published.clear()
    fake._publish_sensors(cfg, T0, record, None, False)
    assert last(env, ACTION)[1]["soc_percent"] == 55.5


def test_halted_on_during_outage_and_off_on_recovery(env):
    st = env.state
    env.run()
    assert last(env, HALTED)[0] == "off"
    st.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0 + STEP)
    value, a = last(env, HALTED)
    assert value == "on"
    assert "price entity" in a["cause"] and isinstance(a["cause"], str)
    since = a["since"]
    assert since.startswith("2026-09-30T14:40")
    env.run(T0 + 2 * STEP)                               # still halted: same start
    value, a = last(env, HALTED)
    assert value == "on" and a["since"] == since
    st.set(PRICE_ENTITY, "0.10", {PRICE_ATTRIBUTE: price_entries()})
    env.run(T0 + 3 * STEP)
    value, a = last(env, HALTED)
    assert value == "off" and a["cause"] is None and a["since"] is None


def test_action_and_budget_are_unknown_not_stale_during_a_halt(env):
    env.run()
    assert last(env, ACTION)[0] != "unknown"
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0 + STEP)
    value, a = last(env, ACTION)
    assert value == "unknown"
    assert a["why"].startswith("halted: ") and a["power_kw"] is None
    assert a["selector"] is None
    value, a = last(env, BUDGET)
    assert value == "unknown" and a["ceiling_kw"] is None


def test_budget_is_unknown_when_the_grid_sensors_are_unreadable(env):
    env.run()
    assert last(env, BUDGET)[0] != "unknown"
    env.state.set(OFFTAKE, "unavailable", {})
    env.run(T0 + STEP)
    value, a = last(env, BUDGET)
    assert value == "unknown"
    assert a["ceiling_kw"] is None and a["average_kw"] is None
    assert a["month_peak_kw"] is None


def test_budget_is_unknown_with_capacity_tariff_off(env):
    env.write_config("capacity_tariff:\n  enabled: false\n")
    env.run()
    value, a = last(env, BUDGET)
    assert value == "unknown" and a["ceiling_kw"] is None


def test_disabled_publishes_nothing(env):
    env.write_config("sensors:\n  enabled: false\n")
    env.run()
    assert env.decisions() and env.state.published == []
    env.state.set(PRICE_ENTITY, "unavailable", {})       # also not during a halt
    env.run(T0 + STEP)
    assert env.state.published == []


def test_failing_state_set_does_not_break_the_cycle(env):
    env.state.fail_publish = True
    env.run()
    assert len(env.decisions()) == 1                     # the decision is logged
    assert env.log.by_level["error"] == []
    warns = [m for m in env.log.by_level["warning"] if "cannot publish" in m]
    assert len(warns) == 1                               # 3 failures, one warning
    env.run(T0 + STEP)
    assert len(env.decisions()) == 2
    assert len([m for m in env.log.by_level["warning"]
                if "cannot publish" in m]) == 1          # rate-limited (hourly)
    env.run(T0 + timedelta(minutes=65))
    assert len([m for m in env.log.by_level["warning"]
                if "cannot publish" in m]) == 2
    env.state.fail_publish = False                       # and it recovers
    env.run(T0 + timedelta(minutes=70))
    assert last(env, ACTION) is not None


def test_failing_state_set_during_a_halt_still_halts_and_alerts(env):
    env.state.fail_publish = True
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run()
    assert len(env.service.of("notify", "test_notifier")) == 1
    assert any(" | HALT | " in l for l in env.lines())
    assert env.log.by_level["error"] == [] or all(
        "cycle failed" not in m for m in env.log.by_level["error"])


def test_publish_error_in_building_the_payload_is_contained(env):
    env.run()
    env.state.published.clear()
    env.mod._publish_sensors(env.mod._config, T0, object(), None, False)
    assert env.state.published == []
    assert any("publishing failed" in m for m in env.log.by_level["warning"])


def test_decision_is_identical_with_and_without_sensors(env):
    env.write_config("sensors:\n  enabled: false\n")
    env.run()
    off = fields_of(env.decisions()[0])
    env.write_config("sensors:\n  enabled: true\n")
    env.run(T0 + STEP)
    on = fields_of(env.decisions()[1])
    for key in ("action", "selector", "vetoes"):
        assert on[key] == off[key]


# ---- config -----------------------------------------------------------------------

def _cfg(**extra):
    raw = {"battery": {"capacity_kwh": 10.0}, "alerts": {"address": "a@b.c"}}
    raw.update(extra)
    return config.from_dict(raw)


def test_sensors_enabled_defaults_to_true():
    assert _cfg().sensors_enabled is True


def test_sensors_enabled_can_be_turned_off():
    assert _cfg(sensors={"enabled": False}).sensors_enabled is False


@pytest.mark.parametrize("bad", ["yes", 1, 0, "false"])
def test_sensors_enabled_must_be_a_boolean(bad):
    with pytest.raises(config.ConfigError, match=r"sensors\.enabled"):
        _cfg(sensors={"enabled": bad})


def test_sensors_enabled_is_not_in_the_cache_fingerprint():
    assert _cfg().fingerprint() == _cfg(sensors={"enabled": False}).fingerprint()
