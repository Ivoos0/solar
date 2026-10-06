"""E-mail when configured sensors stay unavailable.

The tracker and the wording are pure (decision.py); the adapter runs through
the `env` fixture of test_battery_planner. Expected figures are hand-derived.
"""
from datetime import datetime, timedelta, timezone

import pytest

import decision
from alphaess_support import battery_config
from config import ConfigError, from_dict
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    NOTIFY, PRICE_ENTITY, T0, env)
from test_config import base

SOC = "sensor.test_soc"
UTC = timezone.utc
N0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def at(minutes):
    return N0 + timedelta(minutes=minutes)


# ---- pure tracker -------------------------------------------------------------

def step(first, last, down, minutes, after=15, realert=60):
    return decision.outage_step(first, last, down, at(minutes), after, realert)


def test_nothing_down_nothing_due():
    assert step({}, None, [], 0) == ({}, None, [])


def test_first_sight_starts_the_clock_but_does_not_alert():
    first, last, due = step({}, None, ["sensor.a"], 0)
    assert first == {"sensor.a": at(0)} and last is None and due == []


def test_alert_is_due_after_the_threshold():
    first = {"sensor.a": at(0)}
    assert step(first, None, ["sensor.a"], 14)[2] == []
    assert step(first, None, ["sensor.a"], 15)[2] == ["sensor.a"]


def test_no_repeat_before_realert_then_repeats():
    first = {"sensor.a": at(0)}
    assert step(first, at(15), ["sensor.a"], 74)[2] == []
    assert step(first, at(15), ["sensor.a"], 75)[2] == ["sensor.a"]


def test_a_recovered_sensor_is_forgotten_and_the_timer_resets():
    first = {"sensor.a": at(0)}
    first, last, due = step(first, at(15), [], 30)
    assert first == {} and last is None and due == []
    first, last, due = step(first, last, ["sensor.a"], 31)
    assert first == {"sensor.a": at(31)} and due == []


def test_only_sensors_past_the_threshold_are_reported():
    first = {"sensor.a": at(0)}
    _, _, due = step(first, None, ["sensor.a", "sensor.b"], 20)
    assert due == ["sensor.a"]


def test_message_names_each_sensor_its_downtime_and_effect():
    first = {"sensor.a": at(0), "sensor.b": at(5)}
    title, message = decision.outage_message(
        {"sensor.a": "soc", "sensor.b": "power"}, first, at(20), 60)
    assert title == "Battery planner: 2 sensors unavailable"
    assert "sensor.a: unavailable for 20 min" in message
    assert "sensor.b: unavailable for 15 min" in message
    assert "planner holds" in message and "grid offtake" in message
    assert "Re-alert every 60 min" in message


def test_message_singular_title():
    title, _ = decision.outage_message({"sensor.a": "soc"},
                                       {"sensor.a": at(0)}, at(15), 60)
    assert title == "Battery planner: 1 sensor unavailable"


# ---- which sensors are required -----------------------------------------------

def test_required_sensors_follow_the_configuration(site_config):
    kinds = dict(decision.required_sensors(site_config))
    assert kinds[site_config.forecast_entity] == "forecast"
    assert kinds[site_config.offtake_sensor] == "offtake"
    assert site_config.price_entity not in kinds       # has its own halt alert
    assert "soc" not in kinds.values()                 # not configured


def test_optional_sensors_appear_once_configured():
    cfg = from_dict(base(battery__soc_sensor=SOC,
                         battery__power_sensor="sensor.p",
                         battery__reserve_sensor="sensor.r"))
    kinds = dict(decision.required_sensors(cfg))
    assert kinds[SOC] == "soc" and kinds["sensor.p"] == "power"
    assert kinds["sensor.r"] == "reserve"


def test_capacity_sensors_drop_out_when_the_tariff_is_off():
    cfg = from_dict(base(capacity_tariff__enabled=False))
    assert "offtake" not in dict(decision.required_sensors(cfg)).values()


# ---- config ------------------------------------------------------------------------

def test_config_defaults_and_overrides(site_config):
    assert site_config.sensor_alert_enabled is True
    assert site_config.sensor_outage_minutes == 15
    c = from_dict(base(alerts__sensor_enabled=False,
                       alerts__sensor_outage_minutes=5))
    assert c.sensor_alert_enabled is False and c.sensor_outage_minutes == 5


@pytest.mark.parametrize("key,bad", [("sensor_outage_minutes", 0),
                                     ("sensor_outage_minutes", 2.5),
                                     ("sensor_outage_minutes", -1),
                                     ("sensor_enabled", "yes")])
def test_config_rejects(key, bad):
    with pytest.raises(ConfigError) as e:
        from_dict(base(**{"alerts__" + key: bad}))
    assert "alerts.sensor_" in str(e.value)


# ---- adapter -------------------------------------------------------------------------

def arm(env, extra="", lines=(), alert_lines=("sensor_enabled: true",)):
    """soc_sensor configured but never created in the fake state = down.
    The capacity tariff and the history are off so those sensors do not count."""
    battery_config(env.config_path, ["soc_sensor: %s" % SOC, *lines],
                   extra=("capacity_tariff:\n  enabled: false\n"
                          "history:\n  enabled: false\n" + extra),
                   alert_lines=alert_lines)


def mails(env):
    return [m for m in env.service.of("notify", NOTIFY)
            if "sensor" in m[2]["title"]]


def test_no_mail_before_the_threshold(env):
    arm(env)
    env.run(T0)
    env.run(T0 + timedelta(minutes=10))
    assert mails(env) == []


def test_one_mail_after_the_threshold_naming_the_sensor(env):
    arm(env)
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    sent = mails(env)
    assert len(sent) == 1
    assert sent[0][2]["title"] == "Battery planner: 1 sensor unavailable"
    assert SOC in sent[0][2]["message"]
    assert sent[0][2]["target"] == ["owner@example.com"]


def test_repeats_per_realert_minutes_only(env):
    arm(env)
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    env.run(T0 + timedelta(minutes=40))
    assert len(mails(env)) == 1
    env.run(T0 + timedelta(minutes=77))
    assert len(mails(env)) == 2


def test_a_recovered_sensor_stops_the_mails(env):
    arm(env)
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    env.state.set(SOC, "55", {"unit_of_measurement": "%"})
    env.run(T0 + timedelta(minutes=140))
    assert len(mails(env)) == 1


def test_disabled_sends_nothing(env):
    arm(env, alert_lines=("sensor_enabled: false",))
    env.run(T0)
    env.run(T0 + timedelta(minutes=30))
    assert mails(env) == []


def test_threshold_is_configurable(env):
    arm(env, alert_lines=("sensor_enabled: true", "sensor_outage_minutes: 3"))
    env.run(T0)
    env.run(T0 + timedelta(minutes=6))
    assert len(mails(env)) == 1


def test_the_price_sensor_alone_does_not_send_this_mail(env):
    arm(env)
    env.state.set(SOC, "55", {"unit_of_measurement": "%"})
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    env.run(T0 + timedelta(minutes=30))
    assert mails(env) == []                    # only the halt alert goes out


def test_also_works_during_a_price_halt(env):
    arm(env)
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    assert len(mails(env)) == 1


def test_failed_send_is_retried_next_cycle(env, monkeypatch):
    arm(env)
    calls = []
    real = env.mod._notify

    def flaky(cfg, title, message):
        calls.append(title)
        return False if len(calls) == 1 else real(cfg, title, message)

    monkeypatch.setattr(env.mod, "_notify", flaky)
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    assert mails(env) == []
    env.run(T0 + timedelta(minutes=22))
    assert len(mails(env)) == 1


def test_a_failing_alert_never_touches_the_decision(env, monkeypatch):
    arm(env)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.mod.decision, "outage_step", boom)
    env.run(T0)
    env.run(T0 + timedelta(minutes=16))
    assert env.decisions() != []
