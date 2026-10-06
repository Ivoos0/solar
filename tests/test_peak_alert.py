"""Month-peak notice: e-mail when the meter's month peak exceeds the billing floor.

Adapter behaviour runs through the `env` fixture of test_battery_planner (fake
state/service/log); the pure decision and wording are tested on capacity.py.
Expected figures are hand-derived.
"""
import json
from dataclasses import replace
from datetime import timedelta

import pytest

import capacity as cap
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    MONTH_PEAK_ENTITY, NOTIFY, PRICE_ENTITY, STEP, T0, env)

NEXT_MONTH = T0 + timedelta(hours=10)      # 00:35 on 1 Oct, Brussels time


def arm(env, extra=""):
    env.peak_alerts = True
    env.write_config(extra)


def set_peak(env, value):
    env.state.set(MONTH_PEAK_ENTITY, str(value), {"unit_of_measurement": "kW"})


def mails(env):
    return env.service.of("notify", NOTIFY)


def saved(env):
    return json.loads(env.peak_path.read_text(encoding="utf-8"))


# ---- pure decision and wording ------------------------------------------------

@pytest.mark.parametrize("peak,month,last_month,last,due", [
    (3.1, "2026-09", None, None, True),            # first crossing
    (2.5, "2026-09", None, None, False),           # equal to the floor
    (2.4, "2026-09", None, None, False),           # below
    (None, "2026-09", None, None, False),          # unreadable
    (3.1, "2026-09", "2026-09", 3.1, False),       # unchanged
    (3.0, "2026-09", "2026-09", 3.1, False),       # lower (cannot happen in-month)
    (3.14, "2026-09", "2026-09", 3.1, False),      # below the step
    (3.15, "2026-09", "2026-09", 3.1, True),       # exactly the step
    (3.6, "2026-09", "2026-09", 3.1, True),
    (2.6, "2026-10", "2026-09", 3.9, True),        # new month, lower peak
    (2.4, "2026-10", "2026-09", 3.9, False),       # new month, under the floor
])
def test_peak_alert_due(site_config, peak, month, last_month, last, due):
    assert cap.peak_alert_due(peak, month, last_month, last,
                              site_config) is due


def test_step_constant_is_documented_value():
    assert cap.PEAK_ALERT_MIN_STEP_KW == 0.05


def test_due_uses_the_configured_floor(site_config):
    c = replace(site_config, billing_floor_kw=3.0)
    assert cap.peak_alert_due(3.0, "m", None, None, c) is False
    assert cap.peak_alert_due(3.1, "m", None, None, c) is True


def test_message_carries_the_numbers_and_no_cost_estimate(site_config):
    title, message = cap.peak_alert_message(
        3.1, "2026-09-30T14:35:00+02:00", None, site_config)
    assert "3.10 kW" in title and "2.50 kW" in title
    assert "3.10 kW" in message and "2.50 kW" in message
    assert "0.60 kW over" in message
    assert "2026-09-30T14:35:00+02:00" in message
    assert "EUR" not in message and "ESTIMATED" not in message
    assert "invoice" not in message and "months" not in message
    assert "Previous notice" not in message


def test_message_mentions_the_previous_notice(site_config):
    _, message = cap.peak_alert_message(3.4, "t", 3.1, site_config)
    assert "Previous notice this month: 3.10 kW" in message


# ---- config ---------------------------------------------------------------------

def test_config_default_and_override():
    from config import ConfigError, from_dict
    raw = {"battery": {"capacity_kwh": 10.0, "soc_sensor": "sensor.test_battery_soc"}, "alerts": {"address": "a@b.c"}}
    assert from_dict(raw).peak_alert_enabled is True
    raw["alerts"]["peak_enabled"] = False
    assert from_dict(raw).peak_alert_enabled is False
    for bad in ("yes", 1, "false"):
        raw["alerts"]["peak_enabled"] = bad
        with pytest.raises(ConfigError) as e:
            from_dict(raw)
        assert "alerts.peak_enabled" in str(e.value)


# ---- adapter ----------------------------------------------------------------------

def test_crossing_sends_exactly_one_alert_with_the_numbers(env):
    arm(env)
    set_peak(env, 3.1)
    env.run(T0)
    sent = mails(env)
    assert len(sent) == 1
    kw = sent[0][2]
    assert kw["target"] == ["owner@example.com"]
    assert kw["title"] == "Capacity peak 3.10 kW is above the 2.50 kW billing floor"
    assert "now 3.10 kW" in kw["message"] and "floor of 2.50 kW" in kw["message"]
    assert "2026-09-30T14:35:00+02:00" in kw["message"]
    assert "EUR" not in kw["message"] and "ESTIMATED" not in kw["message"]
    assert saved(env) == {"month": "2026-09", "peak_kw": 3.1}
    assert len(env.decisions()) == 1                 # the decision still ran


@pytest.mark.parametrize("value", ["2.4", "2.5", "0.0"])
def test_at_or_below_the_floor_sends_nothing(env, value):
    arm(env)
    set_peak(env, value)
    env.run(T0)
    assert mails(env) == [] and not env.peak_path.exists()


def test_unchanged_peak_never_repeats(env):
    arm(env)
    set_peak(env, 3.1)
    for i in range(4):
        env.run(T0 + i * STEP)
    assert len(mails(env)) == 1


def test_higher_peak_alerts_again_only_from_the_step(env):
    arm(env)
    set_peak(env, 3.1)
    env.run(T0)
    set_peak(env, 3.14)                              # +0.04: below the step
    env.run(T0 + STEP)
    assert len(mails(env)) == 1
    set_peak(env, 3.2)                               # +0.10 over the notified 3.1
    env.run(T0 + 2 * STEP)
    assert len(mails(env)) == 2
    assert "Previous notice this month: 3.10 kW" in mails(env)[1][2]["message"]
    assert saved(env)["peak_kw"] == 3.2
    set_peak(env, 3.25)                              # exactly the step
    env.run(T0 + 3 * STEP)
    assert len(mails(env)) == 3


def test_month_rollover_resets_even_for_a_lower_peak(env):
    arm(env)
    set_peak(env, 3.1)
    env.run(T0)
    set_peak(env, 2.8)                               # meter register reset
    env.run(NEXT_MONTH)
    sent = mails(env)
    assert len(sent) == 2
    assert "now 2.80 kW" in sent[1][2]["message"]
    assert "Previous notice" not in sent[1][2]["message"]
    assert saved(env) == {"month": "2026-10", "peak_kw": 2.8}


def test_month_boundary_uses_local_time_not_utc(env):
    arm(env)
    set_peak(env, 3.1)
    env.run(T0 + timedelta(hours=9))                 # 21:35 UTC = 23:35 local, 30 Sep
    env.run(T0 + timedelta(hours=9, minutes=30))     # 22:05 UTC = 00:05 local, 1 Oct
    assert len(mails(env)) == 2
    assert saved(env)["month"] == "2026-10"


def test_failed_send_is_retried_and_not_persisted(env):
    arm(env)
    set_peak(env, 3.1)
    env.service.fail.add(("notify", NOTIFY))
    env.run(T0)
    assert len(mails(env)) == 1                      # attempted
    assert not env.peak_path.exists()
    assert any("alert send failed" in m for m in env.log.by_level["error"])
    assert len(env.decisions()) == 1
    env.service.fail.clear()
    env.run(T0 + STEP)                               # retried
    assert len(mails(env)) == 2
    assert saved(env)["peak_kw"] == 3.1
    env.run(T0 + 2 * STEP)
    assert len(mails(env)) == 2                      # and now done


def test_state_survives_a_restart(env):
    arm(env)
    set_peak(env, 3.1)
    env.run(T0)
    assert len(mails(env)) == 1
    env.mod._peak_alert_loaded = False               # simulate a fresh process
    env.mod._peak_alert_last = None
    env.run(T0 + STEP)
    assert len(mails(env)) == 1                      # file said: already told
    env.mod._peak_alert_loaded = False
    env.mod._peak_alert_last = None
    set_peak(env, 3.3)
    env.run(T0 + 2 * STEP)
    assert len(mails(env)) == 2                      # a higher peak still alerts


def test_unreadable_state_file_alerts_once_per_start(env):
    arm(env)
    env.peak_path.parent.mkdir(parents=True)
    env.peak_path.write_text("{not json", encoding="utf-8")
    set_peak(env, 3.1)
    env.run(T0)
    env.run(T0 + STEP)
    assert len(mails(env)) == 1
    assert saved(env) == {"month": "2026-09", "peak_kw": 3.1}   # repaired


def test_unwritable_state_still_does_not_repeat_in_process(env):
    arm(env)
    env.peak_path.parent.mkdir(parents=True)
    env.peak_path.mkdir()                            # a directory: replace() fails
    set_peak(env, 3.1)
    env.run(T0)
    env.run(T0 + STEP)
    assert len(mails(env)) == 1
    assert any("cannot save peak alert state" in m
               for m in env.log.by_level["error"])


def test_disabled_config_sends_nothing(env):
    env.write_config()                               # peak_enabled: false
    set_peak(env, 3.1)
    env.run(T0)
    assert mails(env) == [] and not env.peak_path.exists()


def test_capacity_disabled_sends_nothing(env):
    arm(env, "capacity_tariff:\n  enabled: false\n")
    set_peak(env, 3.1)
    env.run(T0)
    assert mails(env) == []


@pytest.mark.parametrize("how", ["unavailable", "unknown", "garbage", "drop"])
def test_unreadable_sensor_no_alert_and_no_crash(env, how):
    arm(env)
    if how == "drop":
        env.state.drop(MONTH_PEAK_ENTITY)
    else:
        set_peak(env, how)
    env.run(T0)
    assert mails(env) == []
    assert not any("peak alert" in m for m in env.log.by_level["error"])


def test_message_uses_the_configured_floor(env):
    arm(env, "capacity_tariff:\n  billing_floor_kw: 3.0\n")
    set_peak(env, 3.0)
    env.run(T0)
    assert mails(env) == []                          # equal to the floor
    set_peak(env, 3.4)
    env.run(T0 + STEP)
    kw = mails(env)[0][2]
    assert "floor of 3.00 kW" in kw["message"] and "3.00 kW billing floor" in kw["title"]
    assert "2.50" not in kw["message"] and "2.50" not in kw["title"]
    assert "EUR" not in kw["message"]


def test_alert_also_works_during_a_price_halt(env):
    arm(env)
    set_peak(env, 3.1)
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    titles = [c[2]["title"] for c in mails(env)]
    assert len(titles) == 2
    assert any(t.startswith("Battery planner halted") for t in titles)
    assert any(t.startswith("Capacity peak") for t in titles)
    env.run(T0 + STEP)
    assert len(mails(env)) == 2                      # neither repeats


def test_a_failing_alert_never_touches_the_decision(env):
    arm(env)
    set_peak(env, 3.1)

    def boom(*a, **k):
        raise RuntimeError("wording broke")
    env.mod.capacity.peak_alert_message = boom
    env.run(T0)
    assert len(env.decisions()) == 1
    assert any("peak alert failed" in m for m in env.log.by_level["error"])
    assert "cycle failed" not in " ".join(env.log.by_level["error"])
