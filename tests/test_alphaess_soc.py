"""battery.soc_sensor: the planner and the peak guard read the real charge.

Planner tests use the `env` fixture of test_battery_planner, guard tests the
`make_guard` fixture of test_peak_guard.
"""
from pathlib import Path

import pytest

from alphaess_support import battery_config
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    STEP, T0, _negative_prices, _history, env, fields_of)
from test_peak_guard import (  # noqa: F401  (make_guard is a fixture)
    PEAK_ARGS, SHAVING, make_guard)

SOC = "sensor.alphaess_soc_battery"
SOC_LINES = ("soc_sensor: %s" % SOC,)


def planner_with_soc(env, reading, attrs=None):
    battery_config(env.config_path, SOC_LINES)
    if reading is not None:
        env.state.set(SOC, reading, attrs if attrs is not None
                      else {"unit_of_measurement": "%"})


def cheap_power_with_history(env, monkeypatch):
    monkeypatch.setattr(env.mod, "read_usage_history",
                        lambda cfg, local: _history(3))
    _negative_prices(env)                      # S1 would grid-charge


def action_sensor(env):
    return [p for p in env.state.published if p[0].endswith("_action")][-1]


# ---- planner ------------------------------------------------------------------

def test_reading_is_used_and_not_stubbed(env):
    planner_with_soc(env, "57.6")
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert keys["soc"].startswith("57.6")
    assert "soc_stubbed" not in keys["degraded"]
    assert "soc_unavailable" not in keys["degraded"]
    assert action_sensor(env)[2]["soc_percent"] == 57.6


def test_reading_fills_the_history_record(env):
    planner_with_soc(env, "57.6")
    seen = []
    real = env.mod._record_history

    def spy(*a, **kw):
        seen.append(a[-1])
        return real(*a, **kw)

    env.mod._record_history = spy
    env.run(T0)
    assert seen == [57.6]


def test_sensor_bypasses_the_driver_read(env, monkeypatch):
    planner_with_soc(env, "40")

    def boom(*a, **k):
        raise AssertionError("driver read must not run")

    monkeypatch.setattr(env.mod.inverter, "read_charge", boom)
    env.run(T0)
    assert len(env.decisions()) == 1


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "abc", "101", "-1",
                                 "nan"])
def test_unreadable_charge_marks_holds_and_shows_v7(env, monkeypatch, raw):
    cheap_power_with_history(env, monkeypatch)
    planner_with_soc(env, raw)
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "soc_unavailable" in keys["degraded"]
    assert "soc_stubbed" not in keys["degraded"]
    assert keys["action"] == "idle" and keys["selector"] == "S6"
    assert "V7(suppressed S1 charge)" in keys["vetoes"]
    assert "no battery reading" in keys["why"]
    assert action_sensor(env)[2]["soc_percent"] is None


def test_missing_entity_is_unreadable(env):
    planner_with_soc(env, None)                # entity never created
    env.run(T0)
    assert "soc_unavailable" in fields_of(env.decisions()[0])["degraded"]


def test_unreadable_charge_is_not_recorded_in_history(env):
    planner_with_soc(env, "unavailable")
    seen = []
    real = env.mod._record_history

    def spy(*a, **kw):
        seen.append(a[-1])
        return real(*a, **kw)

    env.mod._record_history = spy
    env.run(T0)
    assert seen == [None]


def test_readable_charge_lets_the_same_prices_charge(env, monkeypatch):
    cheap_power_with_history(env, monkeypatch)
    planner_with_soc(env, "50")
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "charge" and "V7" not in keys["vetoes"]


def test_soc_known_follows_the_sensor_cycle_by_cycle(env, monkeypatch):
    seen = []
    real = env.mod.rules.decide

    def spy(*a, **kw):
        seen.append(kw.get("soc_known"))
        return real(*a, **kw)

    monkeypatch.setattr(env.mod.rules, "decide", spy)
    planner_with_soc(env, "50")
    env.run(T0)
    env.state.set(SOC, "unavailable", {})
    env.run(T0 + STEP)
    env.state.set(SOC, "50", {})
    env.run(T0 + 2 * STEP)
    assert seen == [True, False, True]


def test_without_the_sensor_nothing_changes(env):
    env.run(T0)
    keys = fields_of(env.decisions()[0])
    assert "soc_stubbed" in keys["degraded"]
    assert "soc_unavailable" not in keys["degraded"]
    assert action_sensor(env)[2]["soc_percent"] is None


# ---- peak guard ---------------------------------------------------------------

def guard_with_soc(make_guard):
    g = make_guard().at(12, 7, 30)
    battery_config(Path(g.mod.CONFIG_PATH), SOC_LINES, mode="running")
    return g


def test_guard_reads_the_sensor_not_the_driver(make_guard):
    g = guard_with_soc(make_guard)
    g.inv.charge_percent = 99.0
    g.tick(**PEAK_ARGS, **{SOC: "42.5"})
    assert g.inv.reads == []
    rec = g.discharges[0][2]
    assert rec.charge_percent == 42.5
    assert "soc_stubbed" not in rec.degraded_inputs


def test_guard_empty_battery_is_still_vetoed_by_v5(make_guard):
    g = guard_with_soc(make_guard)
    g.tick(**PEAK_ARGS, **{SOC: "0"})
    assert g.discharges == []
    assert "V5(suppressed S0 discharge)" in g.inv.calls[0][2].vetoes_applied


@pytest.mark.parametrize("raw", ["unavailable", "unknown", "x", "150"])
def test_guard_still_shaves_when_the_charge_is_unreadable(make_guard, raw):
    g = guard_with_soc(make_guard)
    g.tick(**PEAK_ARGS, **{SOC: raw})
    assert len(g.discharges) == 1
    rec = g.discharges[0][2]
    assert "soc_unavailable" in rec.degraded_inputs
    assert "soc_stubbed" not in rec.degraded_inputs
    assert "V7" in rec.vetoes_applied           # bare: it blocks nothing
    assert not any(v.startswith("V5") for v in rec.vetoes_applied)
    assert g.st.values[SHAVING] == "on"
