"""Tests for pyscript/peak_guard.py.

Documented extension beyond the WP text: peak_guard.py is a pyscript SCRIPT
that leans on names pyscript provides at runtime. Each test injects stand-ins
into builtins (identity pyscript_executor, recording trigger decorators, a
dict-backed `state`, a collecting `log`, `task`), loads the file with
importlib, and monkeypatch restores everything afterwards. Module-level paths
point at tmp_path. The inverter boundary is replaced by a recorder; the real
decision.format_record is still run over every record it receives.

Nothing here runs inside Home Assistant; see the WP report for what that
leaves unverified.
"""
import ast
import builtins
import importlib.util
import re
import subprocess
import sys
import types
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import capacity
import config
import decision

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "peak_guard.py"
MODULES = SRC.parent / "modules"
_CORE_NAMES = ("config", "capacity", "battery", "rules", "decision")
TZ = ZoneInfo("Europe/Brussels")
SHAVING = "pyscript.peak_guard_shaving"
OFFTAKE = "sensor.slimmelezer_power_consumed"
AVG = "sensor.slimmelezer_huidig_kwartiervermogen"
PEAK = "sensor.slimmelezer_maandpiek"


class _StateVal(str):
    """Like pyscript's StateVal: a str with timestamp attributes (only set
    when exposed, so a missing one is an AttributeError like an old HA)."""

    def __new__(cls, value, updated=None, reported=None):
        obj = super().__new__(cls, value)
        if updated is not None:
            obj.last_updated = updated
        if reported is not None:
            obj.last_reported = reported
        return obj


class FakeState:
    def __init__(self):
        self.values = {}
        self.attrs = {}
        self.gets = []
        self.sets = []
        self.units = {}      # entity -> unit; absent = kW, None = no attribute
        self.updated = {}    # entity -> last_updated (absent = not exposed)
        self.reported = {}   # entity -> last_reported (absent = not exposed)

    def get(self, name):
        self.gets.append(name)
        if name not in self.values:
            raise NameError("name '%s' is not defined" % name)
        return _StateVal(self.values[name], self.updated.get(name),
                         self.reported.get(name))

    def getattr(self, name):
        out = {}
        unit = self.units.get(name, "kW")
        if unit is not None:
            out["unit_of_measurement"] = unit
        return out      # real pyscript strips the timestamps from this dict

    def set(self, name, value=None, new_attributes=None, **kwargs):
        self.sets.append((name, value))
        self.values[name] = value
        self.attrs[name] = new_attributes


class FakeLog:
    def __init__(self):
        self.messages = []
        self.leveled = []     # (level, message)

    def _add(self, msg, *args, level="?"):
        text = msg % args if args else msg
        self.messages.append(text)
        self.leveled.append((level, text))

    def warning(self, msg, *args):
        self._add(msg, *args, level="warning")

    def info(self, msg, *args):
        self._add(msg, *args, level="info")

    def error(self, msg, *args):
        self._add(msg, *args, level="error")

    def debug(self, msg, *args):
        self._add(msg, *args, level="debug")


class FakeInverter:
    def __init__(self):
        self.calls = []
        self.reads = []
        self.charge_percent = 50.0
        self.marker = None
        self.stub = True
        self.last_action = None            # what last_command.json would say

    def last_sent_action(self, *a, **k):
        return self.last_action

    def apply(self, action, target_power_kw, record, *a, **k):
        decision.format_record(record)      # must always format
        self.kwargs = k
        self.calls.append((action, target_power_kw, record))
        return True

    def read_charge(self, inverter_type="logging", driver_dir=None):
        self.reads.append((inverter_type, driver_dir))
        return self.charge_percent, self.stub, self.marker


def _factory(registry, name):
    def make(*args, **kwargs):
        registry.append((name, args))

        def deco(fn):
            return fn
        return deco
    return make


class Guard:
    """Loaded module plus the fakes and a small driver."""

    def __init__(self, mod, st, log, inv, clock, triggers):
        self.mod, self.st, self.log, self.inv = mod, st, log, inv
        self.clock, self.triggers = clock, triggers
        self.now = None

    def at(self, h, m, s=0, day=30):
        self.now = datetime(2026, 9, day, h, m, s, tzinfo=TZ)
        return self

    def tick(self, offtake=None, avg=None, peak=None, trigger_type="time",
             advance=60.0, **raw):
        for name, value in ((OFFTAKE, offtake), (AVG, avg), (PEAK, peak)):
            if value is not None:
                self.st.values[name] = value
        self.st.values.update(raw)
        self.clock[0] += advance
        self.mod.peak_guard(trigger_type=trigger_type)

    @property
    def discharges(self):
        return [c for c in self.inv.calls if c[0] == "discharge"]


def _write_config(path, mode="running", extra=""):
    path.write_text(
        "battery:\n  capacity_kwh: 10.0\n"
        "alerts:\n  address: owner@example.com\n"
        "capacity_tariff:\n  quarter_hour_average_mode: %s\n"
        "  stay_under_percent: 100\n%s" % (mode, extra),
        encoding="utf-8")


@pytest.fixture
def make_guard(monkeypatch, tmp_path):
    saved_bare = {n: sys.modules.get(n) for n in _CORE_NAMES}

    builds = []

    def build(mode="running", extra="", state=None):
        # Each guard gets its own state dir (as a separate install would);
        # pass the same `state` name to simulate a restart of one guard.
        builds.append(1)
        state = state or ("state%d" % len(builds))
        cfg_path = tmp_path / "user_config.yaml"
        _write_config(cfg_path, mode, extra)
        triggers = []
        st, log, inv = FakeState(), FakeLog(), FakeInverter()
        for name, value in (
                ("pyscript_executor", lambda fn: fn),
                ("state_trigger", _factory(triggers, "state_trigger")),
                ("time_trigger", _factory(triggers, "time_trigger")),
                ("task_unique", _factory(triggers, "task_unique")),
                ("state", st), ("log", log), ("task", object())):
            monkeypatch.setattr(builtins, name, value, raising=False)
        spec = importlib.util.spec_from_file_location("peak_guard_under_test",
                                                      SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "CORE_DIR", str(MODULES))
        mod._ensure_core()
        clock = [1000.0]
        guard = Guard(mod, st, log, inv, clock, triggers)
        monkeypatch.setattr(mod, "CONFIG_PATH", str(cfg_path))
        state_dir = tmp_path / state
        monkeypatch.setattr(mod, "STATE_DIR", str(state_dir) + "/")
        monkeypatch.setattr(mod, "WARN_STATE_PATH",
                            str(state_dir / "peak_warning.json"))
        monkeypatch.setattr(mod, "inverter", inv)
        monkeypatch.setattr(mod, "_monotonic", lambda: clock[0])
        monkeypatch.setattr(mod, "_now", lambda cfg: guard.now)
        return guard
    yield build
    for n, previous in saved_bare.items():    # loader aliases are permanent
        if previous is None:
            sys.modules.pop(n, None)
        else:
            sys.modules[n] = previous


def _cfg(mode="running"):
    return config.from_dict({
        "battery": {"capacity_kwh": 10.0},
        "alerts": {"address": "owner@example.com"},
        "capacity_tariff": {"quarter_hour_average_mode": mode,
                            "stay_under_percent": 100}})


# a forming peak: 12:07:30, average 4.0 kW so far, drawing 5.0 kW, ceiling 2.5
PEAK_ARGS = dict(offtake="5.0", avg="4.0", peak="2.5")


def test_triggers_registered(make_guard):
    g = make_guard()
    assert ("state_trigger", (OFFTAKE,)) in g.triggers
    assert ("time_trigger", ("period(now, 30sec)",)) in g.triggers
    assert ("task_unique", ("peak_guard",)) in g.triggers


def test_forming_peak_discharges_once_with_guard_record(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert len(g.inv.calls) == 1
    action, power, rec = g.inv.calls[0]
    grid = capacity.build_state(5.0, 0.0, g.now, 2.5, _cfg(),
                                reported_average_kw=4.0)
    expected = capacity.shave_kw(grid, _cfg())
    assert expected > 0
    assert action == "discharge" and power == pytest.approx(expected)
    assert rec.source == "guard" and rec.selector == "S0"
    assert rec.target_power_kw == pytest.approx(expected)
    # a forming peak leaves no charge budget after the household draw (V3
    # forbids grid charging only; the guard never charges)
    assert rec.vetoes_applied == ["V3"]
    assert "soc_stubbed" in rec.degraded_inputs
    line = decision.format_record(rec)
    assert "source=guard" in line and "selector=S0" in line
    assert "average mode running;" in line
    # documented renderings of trajectory-dependent fields
    assert rec.forecast_remaining_kwh is None
    assert rec.usage_remaining_kwh is None and rec.spill_kwh is None
    assert rec.saturation_block is None and rec.reserve_breach_block is None
    assert rec.projected_end_charge_kwh == rec.charge_kwh
    assert "cons=n/a" in line and "inj=n/a" in line
    assert "solar_rem=n/a" in line and "usage_rem=n/a" in line
    assert "spill=n/a" in line and "0.00kWh" not in line



def test_netted_sensor_used_and_per_phase_ignored(make_guard):
    g = make_guard().at(12, 7, 30)
    per_phase = {
        "sensor.slimmelezer_power_consumed_phase_1": "5.0",
        "sensor.slimmelezer_power_consumed_phase_2": "5.0",
        "sensor.slimmelezer_power_consumed_phase_3": "5.0",
    }
    g.tick(offtake="0.003", avg="0.5", peak="2.5", **per_phase)
    assert g.inv.calls == []
    assert OFFTAKE in g.st.gets
    assert not [n for n in g.st.gets if "phase" in n]
    # and the netted value alone is what drives a shave
    g.tick(**PEAK_ARGS, **per_phase)
    assert len(g.discharges) == 1
    assert not [n for n in g.st.gets if "phase" in n]


def test_quiet_window_no_log_no_inverter_call(make_guard):
    g = make_guard().at(12, 7, 30)
    for i in range(5):
        g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert g.inv.calls == [] and g.log.messages == []
    assert g.st.values[SHAVING] == "off"


def test_battery_at_reserve_still_shaves(make_guard):
    # the reserve limits exporting only: peak shaving may use charge below it
    g = make_guard().at(12, 7, 30)
    g.inv.charge_percent = 10.0
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1
    assert g.discharges[0][2].vetoes_applied == ["V1", "V3"]
    g.inv.charge_percent = 3.0
    g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"


def test_empty_battery_is_vetoed_by_v5(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.charge_percent = 0.0
    g.tick(**PEAK_ARGS)
    assert g.discharges == []
    assert len(g.inv.calls) == 1
    action, power, rec = g.inv.calls[0]
    assert action == "idle" and power == 0.0
    assert rec.source == "guard" and rec.selector == "S0"
    assert "V5(suppressed S0 discharge)" in rec.vetoes_applied
    assert "V1" in rec.vetoes_applied
    assert "peak is allowed to form" in rec.reasoning
    assert "empty" in rec.reasoning
    assert g.st.values[SHAVING] == "off"
    # same window again: recorded once, not per tick
    g.tick(**PEAK_ARGS)
    assert len(g.inv.calls) == 1


def test_below_floor_does_nothing(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(offtake="2.4", avg="2.4", peak="2.5")
    assert g.inv.calls == [] and g.log.messages == []


def test_unavailable_sensor_is_logged_noop(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(offtake="unavailable", avg="4.0", peak="2.5")
    g.tick(offtake="unknown")
    assert g.inv.calls == []
    warnings = [m for m in g.log.messages if "unreadable" in m]
    assert len(warnings) == 1                       # rate limited
    g.tick(offtake="5.0")                           # recovers, still works
    assert len(g.discharges) == 1
    assert "meter_restored" in g.discharges[0][2].degraded_inputs


def test_missing_entity_is_noop(make_guard):
    g = make_guard().at(12, 7, 30)
    g.st.values[OFFTAKE] = "5.0"            # avg and peak entities absent
    g.mod.peak_guard(trigger_type="time")
    assert g.inv.calls == []


def test_flag_clears_after_grace_when_meter_stays_dead(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"
    g.tick(offtake="unavailable", advance=30.0)
    assert g.st.values[SHAVING] == "on"             # inside grace
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert g.st.values[SHAVING] == "off"


def test_shaving_flag_transitions_and_only_transitions_logged(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert g.st.values[SHAVING] == "off" and g.inv.calls == []
    g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"
    assert g.st.attrs[SHAVING]["shave_kw"] > 0
    assert g.st.attrs[SHAVING]["window_start"].startswith("2026-09-30T12:00")
    assert len(g.inv.calls) == 1
    g.tick(**PEAK_ARGS)                             # steady shave: silent
    g.tick(**PEAK_ARGS)
    assert len(g.inv.calls) == 1
    g.tick(offtake="0.3", avg="0.4", peak="2.5")    # load gone: stop
    assert g.st.values[SHAVING] == "off"
    assert [c[0] for c in g.inv.calls] == ["discharge", "idle"]
    assert "shaving stopped" in g.inv.calls[1][2].reasoning
    g.tick(offtake="0.3", avg="0.4", peak="2.5")    # quiet again: silent
    assert len(g.inv.calls) == 2


def test_shave_power_change_is_recorded(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    g.tick(offtake="4.0", avg="4.0", peak="2.5")    # smaller shave
    assert len(g.discharges) == 2
    assert g.discharges[1][1] < g.discharges[0][1]


def test_exception_is_swallowed_and_next_tick_runs(make_guard, monkeypatch):
    g = make_guard().at(12, 7, 30)
    real = g.mod.capacity.shave_kw

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(g.mod.capacity, "shave_kw", boom)
    g.tick(**PEAK_ARGS)                             # must not raise
    assert any("tick failed" in m and "kaput" in m for m in g.log.messages)
    monkeypatch.setattr(g.mod.capacity, "shave_kw", real)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_bad_config_file_is_swallowed(make_guard):
    g = make_guard().at(12, 7, 30)
    Path(g.mod.CONFIG_PATH).write_text("not: [valid", encoding="utf-8")
    g.mod._cfg["mtime"] = None
    g.mod._cfg["config"] = None
    g.tick(**PEAK_ARGS)
    assert g.inv.calls == []
    assert any("tick failed" in m for m in g.log.messages)


def test_config_cached_by_mtime(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    first = g.mod._cfg["config"]
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert g.mod._cfg["config"] is first


def test_time_tick_respects_configured_interval(make_guard):
    g = make_guard(extra="  guard_interval_seconds: 120\n").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    n = len(g.st.sets)
    g.tick(advance=30.0, trigger_type="time", **PEAK_ARGS)
    assert len(g.st.sets) == n                       # skipped
    g.tick(advance=30.0, trigger_type="state", **PEAK_ARGS)   # state runs
    g.tick(offtake="0.3", avg="0.4", advance=30.0, trigger_type="state")
    assert g.st.values[SHAVING] == "off"


def test_record_states_the_configured_mode(make_guard):
    g = make_guard(mode="accumulating").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    # 12:07:30 with "accumulating": 4.0*15/7.5 = 8.0 kW average
    rec = g.discharges[0][2]
    assert "average mode accumulating;" in rec.reasoning
    assert "avg_mode_assumed" not in rec.degraded_inputs
    assert rec.running_average_kw == pytest.approx(8.0)


# ---- review cycle 1 regressions ---------------------------------------------

def _stamp(h, m, s=0):
    return datetime(2026, 9, 30, h, m, s, tzinfo=TZ)


def test_stale_window_boundary_average_never_discharges(make_guard):
    # reviewer probe: accumulating, peak 4.0, offtake 3.5, stale avg 3.9
    g = make_guard(mode="accumulating").at(12, 15, 2)
    g.st.updated[AVG] = _stamp(12, 14, 58)              # previous window
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []
    assert g.st.values[SHAVING] == "off"
    stale = [m for m in g.log.messages if "not refreshed" in m]
    assert len(stale) == 1
    g.at(12, 15, 3).tick(trigger_type="state")          # still stale
    assert g.inv.calls == [] and len(
        [m for m in g.log.messages if "not refreshed" in m]) == 1
    # the meter publishes the new window's average: acts normally, no phantom
    g.at(12, 15, 4)
    g.st.updated[AVG] = _stamp(12, 15, 4)
    g.tick(offtake="3.5", avg="0.0", peak="4.0", trigger_type="state")
    assert g.inv.calls == []
    # later in that window a real peak is shaved off a fresh average
    g.at(12, 22, 30)
    g.st.updated[AVG] = _stamp(12, 22, 29)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_stale_last_reported_no_action_fresh_one_acts(make_guard):
    # reviewer probe with real-style timestamps; last_updated is old too
    g = make_guard(mode="accumulating").at(12, 15, 16)
    g.st.updated[AVG] = _stamp(12, 14, 40)
    g.st.reported[AVG] = _stamp(12, 14, 58)
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []
    # identical value re-reported after the boundary: last_updated unchanged
    g.at(12, 22, 30)
    g.st.reported[AVG] = _stamp(12, 22, 29)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_boundary_margin_constant_is_two_seconds(make_guard):
    assert make_guard().mod.GUARD_BOUNDARY_MARGIN_S == 2


@pytest.mark.parametrize("attr", ["reported", "updated"])
def test_stamp_within_margin_of_window_start_is_stale(make_guard, attr):
    # meter clock a few seconds behind HA: a stamp 1 s after the boundary may
    # still be the previous window's average
    g = make_guard(mode="accumulating").at(12, 22, 30)
    getattr(g.st, attr)[AVG] = _stamp(12, 15, 1)
    g.tick(**PEAK_ARGS)
    assert g.inv.calls == []
    assert len([m for m in g.log.messages if "not refreshed" in m]) == 1
    getattr(g.st, attr)[AVG] = _stamp(12, 15, 3)        # start + 3 s: fresh
    g.at(12, 22, 31).tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_stamp_exactly_at_margin_is_fresh(make_guard):
    g = make_guard(mode="accumulating").at(12, 22, 30)
    g.st.reported[AVG] = _stamp(12, 15, 2)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_margin_does_not_change_no_timestamp_fallback(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 14)   # no timestamps at all
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []                              # inside the 15 s
    assert not [m for m in g.log.messages if "not refreshed" in m]
    g.at(12, 22, 30).tick(**PEAK_ARGS)                    # past it: acts
    assert len(g.discharges) == 1


def test_quiet_house_repeated_value_with_moving_last_reported_not_stale(
        make_guard):
    g = make_guard(mode="accumulating")
    g.st.updated[AVG] = _stamp(11, 40)                  # value never changes
    for m in (16, 20, 25):
        g.at(12, m)
        g.st.reported[AVG] = _stamp(12, m)
        g.tick(offtake="0.3", avg="0.0", peak="4.0")
    assert not [x for x in g.log.messages if "not refreshed" in x]
    g.at(12, 27, 30)
    g.st.reported[AVG] = _stamp(12, 27, 29)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_only_last_updated_present_is_used(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 30)
    g.st.updated[AVG] = _stamp(12, 14, 50)
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []
    assert len([m for m in g.log.messages if "not refreshed" in m]) == 1
    g.at(12, 22, 30)
    g.st.updated[AVG] = _stamp(12, 22, 29)
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1


def test_iso_string_and_naive_timestamps_are_accepted(make_guard):
    g = make_guard(mode="accumulating").at(12, 22, 30)
    g.st.reported[AVG] = "2026-09-30T10:22:29+00:00"    # 12:22:29 local
    g.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1
    g2 = make_guard(mode="accumulating").at(12, 22, 30)
    g2.st.reported[AVG] = datetime(2026, 9, 30, 10, 22, 29)   # naive = UTC
    g2.tick(**PEAK_ARGS)
    assert len(g2.discharges) == 1


def test_getattr_only_timestamps_are_ignored(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 2)
    real_getattr = g.st.getattr

    def leaky(name):
        out = real_getattr(name)
        out["last_updated"] = _stamp(12, 15, 1)         # never in real pyscript
        return out
    g.st.getattr = leaky
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []                            # fallback still holds


def test_stale_average_without_timestamp_ignored_for_first_seconds(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 2)   # no timestamps
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == []
    g.at(12, 15, 14).tick(offtake="3.5", avg="3.9", peak="4.0",
                          trigger_type="state")
    assert g.inv.calls == []
    assert not [m for m in g.log.messages if "not refreshed" in m]
    g.at(12, 22, 30).tick(**PEAK_ARGS)                  # past the fallback
    assert len(g.discharges) == 1


@pytest.mark.parametrize("scale", [(1000.0, "W"), (1.0, "kW")])
def test_watt_values_give_the_kw_decision(make_guard, scale):
    factor, unit = scale
    g = make_guard().at(12, 7, 30)
    for name in (OFFTAKE, AVG, PEAK):
        g.st.units[name] = unit
    g.tick(offtake=str(5.0 * factor), avg=str(4.0 * factor),
           peak=str(2.5 * factor))
    ref = make_guard().at(12, 7, 30)
    ref.tick(**PEAK_ARGS)
    assert len(g.discharges) == 1
    assert g.discharges[0][1] == pytest.approx(ref.discharges[0][1])


def test_mixed_units_are_normalised_per_entity(make_guard):
    g = make_guard().at(12, 7, 30)
    g.st.units[OFFTAKE] = "W"
    g.tick(offtake="5000", avg="4.0", peak="2.5")
    ref = make_guard().at(12, 7, 30)
    ref.tick(**PEAK_ARGS)
    assert g.discharges[0][1] == pytest.approx(ref.discharges[0][1])


@pytest.mark.parametrize("entity", [OFFTAKE, AVG, PEAK])
@pytest.mark.parametrize("unit", ["MW", "kWh", "", None])
def test_unknown_or_missing_unit_is_logged_noop(make_guard, entity, unit):
    g = make_guard().at(12, 7, 30)
    g.st.units[entity] = unit
    g.tick(**PEAK_ARGS)
    g.tick(**PEAK_ARGS)
    assert g.inv.calls == []
    assert g.st.values[SHAVING] == "off"
    warnings = [m for m in g.log.messages if "unreadable" in m]
    assert len(warnings) == 1 and "unit" in warnings[0]      # rate limited


def test_cancelled_start_leaves_flags_updated(make_guard):
    import asyncio
    g = make_guard().at(12, 7, 30)

    def killed(*a, **k):
        raise asyncio.CancelledError()
    g.inv.apply = killed
    with pytest.raises(asyncio.CancelledError):
        g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"
    assert g.mod._flags["shaving"] is True and g.mod._flags["shave_kw"] > 0
    # the next run sees a shave in progress: no second "started" record
    ok = FakeInverter()
    g.mod.inverter = ok
    g.tick(**PEAK_ARGS)
    assert ok.calls == []


def test_cancelled_stop_leaves_flags_updated(make_guard):
    import asyncio
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"

    def killed(*a, **k):
        raise asyncio.CancelledError()
    g.inv.apply = killed
    with pytest.raises(asyncio.CancelledError):
        g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert g.st.values[SHAVING] == "off"
    assert g.mod._flags["shaving"] is False
    ok = FakeInverter()
    g.mod.inverter = ok
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert ok.calls == []                                # no duplicate stop


def test_flags_updated_before_apply_is_called(make_guard):
    g = make_guard().at(12, 7, 30)
    seen = []
    real = g.inv.apply

    def spy(*a, **k):
        seen.append((g.st.values.get(SHAVING), g.mod._flags["shaving"]))
        return real(*a, **k)
    g.inv.apply = spy
    g.tick(**PEAK_ARGS)
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    assert seen == [("on", True), ("off", False)]


def test_first_run_after_reload_forces_entity_off(make_guard):
    g = make_guard().at(12, 7, 30)
    g.st.values[SHAVING] = "on"                    # survived a pyscript reload
    g.tick(offtake="unavailable", avg="4.0", peak="2.5")
    assert g.st.sets[0] == (SHAVING, "off")
    assert g.st.values[SHAVING] == "off"
    assert g.mod._flags["primed"] is True


def test_reload_while_on_unknown_state_clears_after_grace(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)                            # primed, shaving
    g.mod._flags["shaving"] = None                 # module state lost
    g.st.values[SHAVING] = "on"
    g.tick(offtake="unavailable", advance=30.0)
    assert g.st.values[SHAVING] == "on"            # inside grace
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert g.st.values[SHAVING] == "off"


def test_error_while_shaving_clears_flag_after_grace(make_guard, monkeypatch):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.st.values[SHAVING] == "on"

    def boom():
        raise ValueError("config broke")
    monkeypatch.setattr(g.mod, "_get_config", boom)
    g.tick(advance=30.0)
    assert g.st.values[SHAVING] == "on"            # inside grace
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert g.st.values[SHAVING] == "off"
    assert g.mod._flags["shaving"] is False


def test_shave_record_renders_fired_v3_bare(make_guard):
    # budget <= 0 late in the window: V3 fires (forbids grid charge only)
    g = make_guard().at(12, 13, 30)
    g.tick(offtake="9.0", avg="8.0", peak="2.5")
    action, _, rec = g.inv.calls[0]
    assert action == "discharge"                    # V3 blocks nothing
    assert rec.vetoes_applied == ["V3"]
    assert "vetoes=V3" in decision.format_record(rec)
    # quiet start of a fresh window: nothing fired, nothing rendered
    g.at(12, 20).tick(offtake="0.3", avg="0.4", peak="2.5")
    stop = g.inv.calls[1][2]
    assert g.inv.calls[1][0] == "idle" and stop.vetoes_applied == []


# ---- static checks ---------------------------------------------------------

def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _decorator_names(fn):
    out = set()
    for d in fn.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        out.add(node.id if isinstance(node, ast.Name)
                else getattr(node, "attr", ""))
    return out


def test_every_open_lives_in_an_executor_helper():
    tree = _tree()
    opens = 0
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            if isinstance(call.func, ast.Name) and call.func.id == "open":
                opens += 1
                assert "pyscript_executor" in _decorator_names(fn), fn.name
    assert opens >= 1
    top = [n for n in tree.body if isinstance(n, ast.Expr)
           or isinstance(n, ast.With)]
    assert all(not isinstance(n, ast.With) for n in top)


def test_no_blocking_stat_or_yaml_outside_executor():
    tree = _tree()
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if "pyscript_executor" in _decorator_names(fn):
            continue
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            f = call.func
            if isinstance(f, ast.Attribute):
                assert f.attr not in {"stat", "safe_load", "load",
                                      "read_text", "write_text"}, fn.name


def test_guard_stays_narrow():
    tree = _tree()
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert not imported & {"trajectory", "prices", "series", "cache",
                           "battery_planner"}
    assert "inverter" in imported
    # the core arrives through the native loader, never through pyscript imports
    top = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            top.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            top.add((n.module or "").split(".")[0])
    assert not top & set(_CORE_NAMES) and "yaml" not in top


def test_core_module_list_is_narrow():
    ns = {}
    for node in _tree().body:
        if isinstance(node, ast.Assign) and node.targets[0].id == "CORE_MODULES":
            ns["v"] = ast.literal_eval(node.value)
    assert set(ns["v"]) == set(_CORE_NAMES)
    assert not set(ns["v"]) & {"trajectory", "prices", "series", "cache",
                               "inverter"}


def test_no_per_phase_sensor_names_or_credentials():
    tree = _tree()
    doc_nodes = {id(n.body[0].value) for n in ast.walk(tree)
                 if isinstance(n, (ast.Module, ast.FunctionDef))
                 and n.body and isinstance(n.body[0], ast.Expr)
                 and isinstance(n.body[0].value, ast.Constant)}
    code_strings = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in doc_nodes]
    assert not [s for s in code_strings if "phase" in s.lower()]
    text = SRC.read_text(encoding="utf-8")
    assert not re.search(r"password|api[_-]?key|secret|bearer|token", text,
                         re.IGNORECASE)


def test_file_is_lf_only():
    assert b"\r" not in SRC.read_bytes()


# ---- native core loading (the interpreter cannot run the core) -----------------

def test_core_modules_are_bound_natively(make_guard):
    mod = make_guard().mod
    for name in mod.CORE_MODULES:
        assert Path(sys.modules[name].__file__) == MODULES / (name + ".py")
    assert mod.rules.capacity is mod.capacity
    assert "inverter" not in mod.CORE_MODULES


def _bind_fresh(mod, monkeypatch):
    """Simulate a fresh HA process: no bare or private core entries."""
    for n in _CORE_NAMES:
        monkeypatch.delitem(sys.modules, n, raising=False)
        monkeypatch.delitem(sys.modules, "peak_guard_core_" + n, raising=False)


def test_loader_uses_private_names_aliases_bare_names_and_keeps_sys_path(
        make_guard, monkeypatch):
    mod = make_guard().mod
    _bind_fresh(mod, monkeypatch)
    path_before = list(sys.path)
    loaded = mod._load_core(str(MODULES), mod.CORE_MODULES)
    for n in mod.CORE_MODULES:
        assert loaded[n].__name__ == "peak_guard_core_" + n
        assert sys.modules[n] is loaded[n] is sys.modules["peak_guard_core_" + n]
    assert sys.path == path_before
    assert loaded["rules"].capacity is loaded["capacity"]
    again = mod._load_core(str(MODULES), mod.CORE_MODULES)      # idempotent
    assert all(again[n] is loaded[n] for n in mod.CORE_MODULES)


def test_loader_reuses_modules_the_planner_already_registered(
        make_guard, monkeypatch):
    """Planner loaded first: bare names already point at the same files."""
    mod = make_guard().mod
    _bind_fresh(mod, monkeypatch)
    planner = {}
    for n in ("config", "capacity", "battery", "rules", "decision"):
        spec = importlib.util.spec_from_file_location(
            "battery_planner_core_" + n, MODULES / (n + ".py"))
        m = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, n, m)
        monkeypatch.setitem(sys.modules, "battery_planner_core_" + n, m)
        spec.loader.exec_module(m)
        planner[n] = m
    loaded = mod._load_core(str(MODULES), mod.CORE_MODULES)
    assert all(loaded[n] is planner[n] for n in planner)
    assert "peak_guard_core_config" not in sys.modules


def test_loader_replaces_foreign_bare_names_when_planner_loads_second(
        make_guard, monkeypatch, tmp_path):
    """Bare names pointing at a different file are not reused."""
    mod = make_guard().mod
    _bind_fresh(mod, monkeypatch)
    import types
    foreign = types.ModuleType("config")
    foreign.__file__ = str(tmp_path / "config.py")
    monkeypatch.setitem(sys.modules, "config", foreign)
    loaded = mod._load_core(str(MODULES), mod.CORE_MODULES)
    assert loaded["config"] is not foreign
    assert sys.modules["config"] is loaded["config"]
    # a later loader (e.g. the planner) re-pointing the bare names must not
    # break the guard's own bindings
    other = importlib.util.spec_from_file_location(
        "battery_planner_core_capacity", MODULES / "capacity.py")
    m = importlib.util.module_from_spec(other)
    other.loader.exec_module(m)
    monkeypatch.setitem(sys.modules, "capacity", m)
    assert loaded["rules"].capacity is loaded["capacity"] is not m


def test_loader_failure_leaves_no_half_loaded_module(make_guard, monkeypatch,
                                                     tmp_path):
    mod = make_guard().mod
    _bind_fresh(mod, monkeypatch)
    (tmp_path / "config.py").write_text("raise RuntimeError('broken')\n")
    with pytest.raises(RuntimeError):
        mod._load_core(str(tmp_path), ("config",))
    assert "peak_guard_core_config" not in sys.modules
    assert "config" not in sys.modules


def test_loader_partial_failure_undoes_aliases_of_good_modules(
        make_guard, monkeypatch, tmp_path):
    """config loads fine, capacity raises: config's bare alias must be undone
    (a pre-existing bare entry restored) and no private name may remain."""
    mod = make_guard().mod
    _bind_fresh(mod, monkeypatch)
    import types
    previous = types.ModuleType("config")
    previous.__file__ = str(tmp_path / "elsewhere.py")
    monkeypatch.setitem(sys.modules, "config", previous)
    (tmp_path / "config.py").write_text("VALUE = 1\n")
    (tmp_path / "capacity.py").write_text("raise RuntimeError('broken')\n")
    with pytest.raises(RuntimeError, match="broken"):
        mod._load_core(str(tmp_path), ("config", "capacity"))
    assert sys.modules["config"] is previous            # alias undone
    assert "capacity" not in sys.modules
    # the good first module keeps its private name only if it stays consistent
    assert "peak_guard_core_capacity" not in sys.modules
    # with no pre-existing bare entry the alias disappears altogether
    monkeypatch.delitem(sys.modules, "config")
    with pytest.raises(RuntimeError, match="broken"):
        mod._load_core(str(tmp_path), ("config", "capacity"))
    assert "config" not in sys.modules and "capacity" not in sys.modules


def test_ensure_core_loads_only_once(make_guard, monkeypatch):
    mod = make_guard().mod
    monkeypatch.setattr(mod, "_core_ready", False)
    seen = []

    def counting(core_dir, names):
        seen.append((core_dir, names))
        return {n: sys.modules[n] for n in names}
    monkeypatch.setattr(mod, "_load_core", counting)
    mod._ensure_core()
    mod._ensure_core()
    mod._ensure_core()
    assert seen == [(mod.CORE_DIR, mod.CORE_MODULES)]


_SUBPROCESS_SCRIPT = r"""
import builtins, importlib.util, sys, types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

src, modules, tmp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
assert not any(Path(p).resolve() == modules.resolve() for p in sys.path if p)
assert not any(n in sys.modules for n in (
    "config", "capacity", "battery", "rules", "decision", "inverter",
    "prices", "series", "trajectory", "cache"))


class State:
    def __init__(self):
        self.d, self.sets = {}, []

    def get(self, e):
        if e not in self.d:
            raise NameError(e)
        return self.d[e]

    def getattr(self, e):
        return {"unit_of_measurement": "kW"}

    def set(self, name, value=None, new_attributes=None, **kw):
        self.sets.append((name, value))


class Log:
    msgs = []

    def info(self, m, *a): pass
    def warning(self, m, *a): self.msgs.append(m % a if a else m)
    error = warning


calls = []
stub = types.ModuleType("inverter")             # pyscript's own interpreted import
stub.read_charge = lambda *a, **k: (50.0, True, None)
stub.apply = lambda action, power, record, *a, **k: calls.append((action, power)) or True
sys.modules["inverter"] = stub
builtins.pyscript_executor = lambda fn: fn
for n in ("state_trigger", "time_trigger", "task_unique"):
    setattr(builtins, n, lambda *a, **k: (lambda fn: fn))
builtins.state, builtins.log, builtins.task = State(), Log(), object()

spec = importlib.util.spec_from_file_location("pg", src)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert not any(n in sys.modules for n in ("config", "capacity", "decision"))
(tmp / "user_config.yaml").write_text(
    "battery:\n  capacity_kwh: 10.0\nalerts:\n  address: a@b.c\n"
    "capacity_tariff:\n  quarter_hour_average_mode: running\n")
mod.CONFIG_PATH = str(tmp / "user_config.yaml")
mod.STATE_DIR = str(tmp / "state") + "/"
mod.WARN_STATE_PATH = str(tmp / "state" / "peak_warning.json")
mod.CORE_DIR = str(modules)
mod._now = lambda cfg: datetime(2026, 9, 30, 12, 7, 30, tzinfo=ZoneInfo("Europe/Brussels"))
builtins.state.d.update({
    "sensor.slimmelezer_power_consumed": "5.0",
    "sensor.slimmelezer_huidig_kwartiervermogen": "4.0",
    "sensor.slimmelezer_maandpiek": "2.5"})
mod.peak_guard(trigger_type="time")
assert not Log.msgs, Log.msgs
assert [c[0] for c in calls] == ["discharge"], calls
assert calls[0][1] > 0
assert ("pyscript.peak_guard_shaving", "on") in builtins.state.sets
loaded = {n for n in sys.modules if n.startswith("peak_guard_core_")}
assert loaded == {"peak_guard_core_" + n for n in mod.CORE_MODULES}, loaded
assert not any(n in sys.modules for n in ("prices", "series", "trajectory", "cache"))
print("OK")
"""


def test_shaving_tick_runs_without_modules_dir_on_sys_path(tmp_path):
    """Call-time core imports (rules -> capacity) must resolve in HA, where
    pyscript/modules is not on sys.path."""
    script = tmp_path / "run.py"
    script.write_text(_SUBPROCESS_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(SRC), str(MODULES), str(tmp_path)],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"


# ---- interpreter-safety lint (pyscript AST interpreter, see battery_planner) ----

_DECORATORS = {"property", "staticmethod", "classmethod"}


def interpreter_violations(source):
    """Constructs pyscript's AST interpreter cannot run."""
    tree = ast.parse(source)
    module_funcs = {n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.GeneratorExp):
            found.append((line, "generator expression"))
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            found.append((line, "yield"))
        elif isinstance(node, ast.Match):
            found.append((line, "match"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("__") and node.name.endswith("__"):
                found.append((line, "dunder method " + node.name))
            for name in _decorator_names(node):
                if name in _DECORATORS:
                    found.append((line, "@" + name))
        elif isinstance(node, ast.Call):
            fn = node.func
            callbacks = [kw.value for kw in node.keywords if kw.arg == "key"]
            if isinstance(fn, ast.Name) and fn.id in ("map", "filter"):
                callbacks += node.args[:1]
            for cb in callbacks:
                if isinstance(cb, ast.Lambda) or (
                        isinstance(cb, ast.Name) and cb.id in module_funcs):
                    found.append((line, "pyscript function as native callback"))
    return found


def test_guard_uses_only_interpreter_safe_constructs():
    assert interpreter_violations(SRC.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("snippet", [
    "x = sum(i for i in range(3))",
    "class A:\n    @property\n    def p(self): return 1",
    "class A:\n    @staticmethod\n    def p(): return 1",
    "class A:\n    @classmethod\n    def p(cls): return 1",
    "class A:\n    def __post_init__(self): pass",
    "def g():\n    yield 1",
    "match 1:\n    case 1: pass",
    "def f(x): return x\nsorted([1], key=f)",
    "def f(x): return x\nlist(map(f, [1]))",
    "def f(x): return x\nlist(filter(f, [1]))",
    "sorted([1], key=lambda x: x)",
])
def test_lint_detects_each_forbidden_construct(snippet):
    assert interpreter_violations(snippet)


def test_lint_allows_comprehensions_and_native_key_callables():
    assert interpreter_violations(
        "a = [i for i in range(3)]\nb = {i for i in a}\nc = sorted(a, key=abs)"
    ) == []


# ---- inverter.type: the guard selects the driver from the same config ----------

def test_guard_passes_configured_type_and_driver_dir_to_the_boundary(make_guard):
    g = make_guard(extra="inverter:\n  type: alphaess\n").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.inv.reads and g.inv.reads[0] == ("alphaess", str(MODULES))
    assert g.inv.kwargs == {"inverter_type": "alphaess",
                            "driver_dir": str(MODULES),
                            "resend_minutes": 15,
                            "state_dir": g.mod.STATE_DIR,
                            "dry_run": False}


def test_guard_defaults_to_the_logging_driver(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.inv.reads[0][0] == "logging"
    assert g.inv.kwargs["inverter_type"] == "logging"


def test_guard_driver_marker_lands_in_the_record_and_is_not_sticky(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.marker = "inverter_driver_unavailable"
    g.tick(**PEAK_ARGS)
    assert "inverter_driver_unavailable" in g.discharges[0][2].degraded_inputs
    g.inv.marker = None
    g.tick(offtake="0.3", avg="0.4", peak="2.5")          # stop record
    stop = g.inv.calls[-1][2]
    assert stop.action == "idle"
    assert "inverter_driver_unavailable" not in stop.degraded_inputs


def test_guard_stub_flag_comes_from_the_driver(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.stub = False
    g.tick(**PEAK_ARGS)
    assert "soc_stubbed" not in g.discharges[0][2].degraded_inputs


# ---- daily rotation: the guard writes the same dated file as the planner ----------

def test_guard_record_lands_in_the_dated_file_shared_with_the_planner(
        make_guard, monkeypatch, tmp_path):
    g = make_guard().at(23, 59, 30, day=30)
    spec = importlib.util.spec_from_file_location(
        "inverter_real_for_guard", MODULES / "inverter.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    logs = tmp_path / "logs"
    seen = []

    def apply(action, power, record, **k):
        seen.append((action, power, record))
        # The guard passes no log location, so apply() derives the dated file
        # from the record timestamp; only the directory is redirected here.
        return real.apply(action, power, record, log_dir=str(logs), **k)

    monkeypatch.setattr(g.mod, "inverter", types.SimpleNamespace(
        apply=apply, read_charge=g.inv.read_charge))
    g.tick(**PEAK_ARGS)
    day_file = logs / "decisions-2026-09-30.log"
    assert [f.name for f in logs.iterdir()] == [day_file.name]
    lines = day_file.read_text().splitlines()
    assert len(lines) == 1 and "source=guard" in lines[0]
    assert lines[0].startswith("2026-09-30T23:59:30")
    action, power, rec = seen[0]
    planner_rec = replace(rec, source="planner", selector="S3")
    assert real.apply(action, power, planner_rec, log_dir=str(logs))
    assert len(day_file.read_text().splitlines()) == 2


# --- configurable SlimmeLezer entities -----------------------------------------

def test_guard_reads_configured_sensors(make_guard):
    g = make_guard(extra=(
        "  quarter_hour_average_sensor: sensor.my_quarter_avg\n"
        "  month_peak_sensor: sensor.my_month_peak\n")).at(12, 7, 30)
    g.tick(offtake="5.0", **{"sensor.my_quarter_avg": "4.0",
                            "sensor.my_month_peak": "2.5"})
    assert len(g.discharges) == 1
    assert "sensor.my_quarter_avg" in g.st.gets
    assert "sensor.my_month_peak" in g.st.gets
    assert AVG not in g.st.gets and PEAK not in g.st.gets


def test_guard_default_sensors_ignored_when_reconfigured(make_guard):
    g = make_guard(extra="  month_peak_sensor: sensor.my_month_peak\n"
                   ).at(12, 7, 30)
    g.tick(**PEAK_ARGS)                  # default names have data, custom absent
    assert g.inv.calls == []
    assert any("sensor.my_month_peak" in m for m in g.log.messages)


def test_guard_trigger_stays_literal_default_offtake(make_guard):
    g = make_guard(extra="  offtake_sensor: sensor.other_meter\n")
    assert ("state_trigger", (OFFTAKE,)) in g.triggers
    assert "LITERALLY" in g.mod.__doc__ and "KNOWN LIMITATION" in g.mod.__doc__


# ---- stale-average logging levels (behaviour unchanged, logging only) -------

def _lv(g, level, needle):
    return [m for lv, m in g.log.leveled if lv == level and needle in m]


def test_guard_stale_warn_fraction_is_half_the_floor(make_guard):
    assert make_guard().mod.GUARD_STALE_WARN_FRACTION == 0.5


def test_low_stale_value_logs_info_only_and_never_acts(make_guard):
    g = make_guard(mode="accumulating").at(17, 6, 0)
    g.st.reported[AVG] = _stamp(16, 15, 2)             # 51 min old, quiet house
    g.tick(offtake="0.3", avg="0.0", peak="4.0")
    assert g.inv.calls == [] and g.st.values[SHAVING] == "off"
    assert not _lv(g, "warning", "not refreshed")
    (msg,) = _lv(g, "info", "not refreshed")
    assert "16:15:02" in msg and "age 3058s" in msg and "0.000 kW" in msg
    g.at(17, 6, 30).tick()                              # same window: silent
    assert len(_lv(g, "info", "not refreshed")) == 1


def test_low_stale_watt_value_is_normalised_before_threshold(make_guard):
    g = make_guard(mode="accumulating", ).at(12, 20)
    g.st.units[AVG] = "W"
    g.st.reported[AVG] = _stamp(12, 1)
    g.tick(offtake="0.3", avg="900", peak="4.0")        # 0.9 kW: low
    assert _lv(g, "info", "not refreshed")
    assert not _lv(g, "warning", "not refreshed")
    g.at(12, 35).tick(avg="3000")                       # 3 kW: high
    (msg,) = _lv(g, "warning", "not refreshed")
    assert "3.000 kW" in msg


def test_threshold_boundary_value_at_half_floor_warns(make_guard):
    g = make_guard(mode="accumulating").at(12, 20)
    g.st.reported[AVG] = _stamp(12, 1)
    g.tick(offtake="0.3", avg="1.249", peak="4.0")
    assert _lv(g, "info", "not refreshed") and not _lv(g, "warning", "")
    g.at(12, 35).tick(avg="1.25")
    assert _lv(g, "warning", "not refreshed")


def test_high_stale_value_warns_with_stamp_and_age_and_never_acts(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 2)
    g.st.updated[AVG] = _stamp(12, 14, 58)
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert g.inv.calls == [] and g.st.values[SHAVING] == "off"
    assert not _lv(g, "info", "not refreshed")
    (msg,) = _lv(g, "warning", "not refreshed")
    assert "2026-09-30T12:14:58+02:00" in msg
    assert "age 4s" in msg and "3.900 kW" in msg
    assert "no action until it is" in msg


def test_one_warning_per_window_and_again_next_window(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 2)
    g.st.updated[AVG] = _stamp(12, 14, 58)
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    g.at(12, 15, 5).tick(trigger_type="state")
    g.at(12, 20).tick(trigger_type="state")
    assert len(_lv(g, "warning", "not refreshed")) == 1
    g.at(12, 30, 1).tick(trigger_type="state")          # next window, stale
    assert len(_lv(g, "warning", "not refreshed")) == 2
    assert g.inv.calls == []


def test_refresh_line_once_per_stale_episode(make_guard):
    g = make_guard(mode="accumulating").at(12, 15, 2)
    g.st.updated[AVG] = _stamp(12, 14, 58)
    g.tick(offtake="3.5", avg="3.9", peak="4.0", trigger_type="state")
    assert not _lv(g, "info", "refreshed (")
    g.at(12, 15, 5)
    g.st.updated[AVG] = _stamp(12, 15, 5)
    g.tick(offtake="3.5", avg="0.0", peak="4.0", trigger_type="state")
    (msg,) = _lv(g, "info", "refreshed (")
    assert "2026-09-30T12:15:05+02:00" in msg and AVG in msg
    for sec in (10, 40):                                # fresh ticks: silent
        g.at(12, 15, sec)
        g.st.updated[AVG] = _stamp(12, 15, sec)
        g.tick(offtake="3.5", avg="0.0", peak="4.0", trigger_type="state")
    assert len(_lv(g, "info", "refreshed (")) == 1
    g.at(12, 30, 1).tick(trigger_type="state")          # new episode
    g.at(12, 30, 9)
    g.st.updated[AVG] = _stamp(12, 30, 9)
    g.tick(trigger_type="state")
    assert len(_lv(g, "info", "refreshed (")) == 2


def test_refresh_in_a_later_window_is_logged(make_guard):
    g = make_guard(mode="accumulating").at(12, 20)
    g.st.reported[AVG] = _stamp(12, 1)
    g.tick(offtake="0.3", avg="0.0", peak="4.0")
    g.at(12, 50)
    g.st.reported[AVG] = _stamp(12, 49, 59)
    g.tick(offtake="0.3", avg="0.0", peak="4.0")
    assert len(_lv(g, "info", "refreshed (")) == 1


def test_never_stale_logs_nothing_about_refresh(make_guard):
    g = make_guard(mode="accumulating")
    for m in (16, 20):
        g.at(12, m)
        g.st.reported[AVG] = _stamp(12, m)
        g.tick(offtake="0.3", avg="0.0", peak="4.0")
    assert not [m for m in g.log.messages if "refreshed" in m]


# ---- add-back of the commanded discharge, and the heartbeat -----------------
#
# Scenario (ceiling 2.5 kW = billing floor, window 12:00-12:15): the household
# draws 5.0 kW unshaved. At 12:05 the window energy is 0.100 kWh (1.2 kW
# average) and 10 minutes remain, so the allowed rate is
# (2.5 * 0.25 - 0.100) * 6 = 3.15 kW and the shave is 5.0 - 3.15 = 1.85 kW.
# Once the battery shaves, the METER shows 3.15 kW.

REAL = "inverter:\n  type: fake\n"
UNSHAVED_KW = 5.0
ALLOWED_KW = 3.15


def _shave_tick(g, minute, second, shaving, advance=30.0):
    """One guard tick at 12:<minute>:<second>. Window energy follows the
    scenario: 0.100 kWh at 12:05:00, then the meter rate (the allowed rate
    while the battery shaves, the unshaved load otherwise)."""
    elapsed_h = (minute * 60 + second) / 3600.0
    started = elapsed_h <= 5 / 60.0
    rate = ALLOWED_KW if (shaving and not started) else UNSHAVED_KW
    energy = 0.1 if started else 0.1 + rate * (elapsed_h - 5 / 60.0)
    g.at(12, minute, second)
    g.tick(offtake=str(rate), avg=str(energy / elapsed_h), peak="2.5",
           advance=advance)


def _quiet_tick(g, offtake, minute=5, second=30, advance=30.0):
    """A tick whose meter reading is below the allowed rate (no peak in sight
    unless something is added back); avg is the matching 12:05:30 value."""
    g.at(12, minute, second)
    energy = 0.1 + float(offtake) * (30 / 3600.0)
    g.tick(offtake=offtake, avg=str(energy / (5.5 / 60.0)), peak="2.5",
           advance=advance)


def test_real_driver_keeps_one_stable_shave_across_ticks(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    assert len(g.discharges) == 1
    first = g.discharges[0][1]
    assert first == pytest.approx(1.85)
    for second in (30, 60, 90, 120, 150):
        _shave_tick(g, 5 + second // 60, second % 60, shaving=True)
        assert g.st.values[SHAVING] == "on"
    # no stop, no restart, no second command: the add-back holds it at 1.85
    assert [c[0] for c in g.inv.calls] == ["discharge"]
    assert g.mod._flags["shave_kw"] == pytest.approx(first)


def test_logging_driver_projects_from_the_metered_offtake(make_guard):
    # nothing is commanded, so the meter keeps showing the unshaved load and
    # the shave is unchanged; add-back is 0
    g = make_guard()
    _shave_tick(g, 5, 0, shaving=False)
    for second in (30, 60):
        _shave_tick(g, 5 + second // 60, second % 60, shaving=False)
    assert [c[0] for c in g.inv.calls] == ["discharge"]
    assert g.mod._commanded_discharge_kw(g.mod._get_config(),
                                         g.mod._monotonic()) == 0.0
    # a net reading that already dropped has nothing added back: it stops
    # (the unchanged behaviour with this driver)
    _quiet_tick(g, "3.0", 6, 30)
    assert g.inv.calls[-1][0] == "idle"
    assert g.st.values[SHAVING] == "off"


def test_without_addback_a_real_driver_would_flip(make_guard, monkeypatch):
    # the flip the add-back prevents: with the commanded power ignored the
    # second tick (meter below the allowed rate) sees no peak and stops
    g = make_guard(extra=REAL)
    monkeypatch.setattr(g.mod, "_commanded_discharge_kw", lambda cfg, at: 0.0)
    _shave_tick(g, 5, 0, shaving=False)
    _quiet_tick(g, "3.0")
    assert [c[0] for c in g.inv.calls] == ["discharge", "idle"]


def test_addback_keeps_the_shave_when_the_meter_dropped(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    _quiet_tick(g, "3.0")
    assert [c[0] for c in g.inv.calls] == ["discharge"]
    assert g.st.values[SHAVING] == "on"


def test_addback_expires_when_the_shave_is_not_reaffirmed(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    cfg = g.mod._get_config()
    at = g.mod._monotonic()
    assert g.mod._commanded_discharge_kw(cfg, at + 60) == pytest.approx(1.85)
    assert g.mod._commanded_discharge_kw(cfg, at + 60.5) == 0.0
    assert g.mod._commanded_discharge_kw(cfg, at - 1) == 0.0     # clock odd
    # a tick that arrives after the expiry projects from the meter alone
    _quiet_tick(g, "3.0", advance=70.0)
    assert g.inv.calls[-1][0] == "idle"


def test_addback_is_clamped_and_needs_a_shave(make_guard):
    g = make_guard(extra=REAL)
    cfg = g.mod._get_config()
    flags = g.mod._flags
    at = g.mod._monotonic()
    assert g.mod._commanded_discharge_kw(cfg, at) == 0.0         # not shaving
    flags.update(shaving=True, command_at=at, shave_kw=99.0)
    assert g.mod._commanded_discharge_kw(cfg, at) == cfg.max_discharge_kw
    flags["shave_kw"] = -3.0
    assert g.mod._commanded_discharge_kw(cfg, at) == 0.0
    flags.update(shave_kw=1.0, shaving=None)
    assert g.mod._commanded_discharge_kw(cfg, at) == 0.0         # unknown
    flags.update(shaving=True, command_at=None)
    assert g.mod._commanded_discharge_kw(cfg, at) == 0.0


def test_shave_stops_when_the_unshaved_load_goes_away(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    # the load drops to 2.0 kW unshaved: the meter then shows 2.0 - 1.85 =
    # 0.15 kW and the add-back gives back 2.0 kW, under the 2.5 kW floor
    _quiet_tick(g, "0.15")
    assert g.inv.calls[-1][0] == "idle"
    assert g.st.values[SHAVING] == "off"
    assert g.mod._flags["command_at"] is None
    assert "shaving stopped" in g.inv.calls[-1][2].reasoning


def test_shave_follows_a_changing_unshaved_load(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)              # 1.85 kW commanded
    # the load grows to 6.0 kW unshaved: the meter shows 6.0 - 1.85 = 4.15 kW
    g.at(12, 5, 30)
    g.tick(offtake="4.15", avg="1.3", peak="2.5", advance=30.0)
    last = g.discharges[-1]
    assert len(g.discharges) == 2 and last[1] > 1.85 + 0.25
    assert "offtake 6.00 kW (metered 4.15 kW + 1.85 kW commanded discharge)" \
        in last[2].reasoning


def test_heartbeat_is_written_with_the_flag_and_refreshed(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    assert g.st.attrs[SHAVING]["last_beat"] == g.now.isoformat()
    sets = len(g.st.sets)
    g.at(12, 5, 2)
    g.tick(offtake="3.15", avg="1.3", peak="2.5", advance=2.0,
           trigger_type="state")                     # too soon: no rewrite
    assert len(g.st.sets) == sets
    _shave_tick(g, 5, 30, shaving=True)              # one interval later
    assert len(g.st.sets) == sets + 1
    assert g.st.attrs[SHAVING]["last_beat"] == g.now.isoformat()
    assert g.st.values[SHAVING] == "on"


def test_heartbeat_is_refreshed_with_the_logging_driver_too(make_guard):
    g = make_guard()
    _shave_tick(g, 5, 0, shaving=False)
    first = g.st.attrs[SHAVING]["last_beat"]
    _shave_tick(g, 5, 30, shaving=False)
    assert g.st.attrs[SHAVING]["last_beat"] == g.now.isoformat() != first


def test_beat_is_cleared_when_the_flag_goes_off(make_guard):
    g = make_guard(extra=REAL)
    _shave_tick(g, 5, 0, shaving=False)
    _quiet_tick(g, "0.15")
    assert g.st.values[SHAVING] == "off"
    assert g.st.attrs[SHAVING]["last_beat"] is None
    n = len(g.st.sets)
    _quiet_tick(g, "0.15", 6, 0)
    assert len(g.st.sets) == n              # no beat while nothing is shaving


def test_dry_run_adds_nothing_back_because_nothing_is_commanded(make_guard):
    g = make_guard(extra=REAL + "  dry_run: true\n")
    _shave_tick(g, 5, 0, shaving=False)
    assert g.mod._commanded_discharge_kw(g.mod._get_config(),
                                         g.mod._monotonic()) == 0.0


# ---- L5: a discharge left over from before a reload is released -------------------

QUIET = dict(offtake="0.3", avg="0.4", peak="2.5")


def test_reload_with_a_discharge_on_record_sends_idle_once(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.last_action = "discharge"
    g.tick(**QUIET)
    assert [c[0] for c in g.inv.calls] == ["idle"]
    rec = g.inv.calls[0][2]
    assert rec.source == "guard" and "released" in rec.reasoning
    g.tick(advance=30.0, **QUIET)                  # nothing more afterwards
    assert len(g.inv.calls) == 1


@pytest.mark.parametrize("last", [None, "idle", "charge", "export"])
def test_reload_without_a_discharge_on_record_sends_nothing(make_guard, last):
    g = make_guard().at(12, 7, 30)
    g.inv.last_action = last
    g.tick(**QUIET)
    assert g.inv.calls == []


def test_reload_that_needs_shaving_sends_the_discharge_not_idle(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.last_action = "discharge"
    g.tick(**PEAK_ARGS)
    assert [c[0] for c in g.inv.calls] == ["discharge"]


# ---- sensor failure policy: a dead meter releases the discharge ----------------------

def test_meter_lost_while_shaving_releases_the_discharge_once(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert [c[0] for c in g.inv.calls] == ["discharge"]
    g.tick(offtake="unavailable", advance=30.0)
    assert [c[0] for c in g.inv.calls] == ["discharge"]       # inside the grace
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert [c[0] for c in g.inv.calls] == ["discharge", "idle"]
    rec = g.inv.calls[1][2]
    assert rec.source == "guard" and "released" in rec.reasoning
    assert "unreadable" in rec.reasoning
    g.tick(advance=30.0)                                      # still dead
    assert len(g.inv.calls) == 2


def test_meter_lost_while_not_shaving_sends_nothing(make_guard):
    g = make_guard().at(12, 7, 30)
    g.tick(offtake="0.3", avg="0.4", peak="2.5")
    g.tick(offtake="unavailable", advance=30.0)
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert g.inv.calls == []


def test_failing_ticks_while_shaving_release_the_discharge(make_guard, monkeypatch):
    g = make_guard().at(12, 7, 30)
    g.tick(**PEAK_ARGS)

    def boom():
        raise ValueError("config broke")
    monkeypatch.setattr(g.mod, "_get_config", boom)
    g.tick(advance=30.0)
    g.tick(advance=g.mod.GRACE_SECONDS + 1)
    assert [c[0] for c in g.inv.calls] == ["discharge", "idle"]
    assert "failing" in g.inv.calls[1][2].reasoning
