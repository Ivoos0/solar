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

    def _add(self, msg, *args):
        self.messages.append(msg % args if args else msg)

    warning = info = error = debug = _add


class FakeInverter:
    def __init__(self):
        self.calls = []
        self.charge_percent = 50.0

    def apply(self, action, target_power_kw, record, *a, **k):
        decision.format_record(record)      # must always format
        self.calls.append((action, target_power_kw, record))
        return True

    def read_charge_percent(self):
        return self.charge_percent


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

    def build(mode="running", extra=""):
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
                                reported_average_kw=4.0,
                                average_mode="running",
                                mode_confidence="configured")
    expected = capacity.shave_kw(grid, _cfg())
    assert expected > 0
    assert action == "discharge" and power == pytest.approx(expected)
    assert rec.source == "guard" and rec.selector == "S0"
    assert rec.target_power_kw == pytest.approx(expected)
    assert rec.vetoes_applied == []
    assert "soc_stubbed" in rec.degraded_inputs
    line = decision.format_record(rec)
    assert "source=guard" in line and "selector=S0" in line
    assert "average mode running (configured)" in line
    # documented renderings of trajectory-dependent fields
    assert rec.forecast_remaining_kwh == 0.0 and rec.usage_remaining_kwh == 0.0
    assert rec.saturation_block is None and rec.reserve_breach_block is None
    assert rec.spill_kwh == 0.0
    assert rec.projected_end_charge_kwh == rec.charge_kwh
    assert "cons=n/a" in line and "inj=n/a" in line


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


def test_battery_at_reserve_is_vetoed_by_v1(make_guard):
    g = make_guard().at(12, 7, 30)
    g.inv.charge_percent = 10.0
    g.tick(**PEAK_ARGS)
    assert g.discharges == []
    assert len(g.inv.calls) == 1
    action, power, rec = g.inv.calls[0]
    assert action == "idle" and power == 0.0
    assert rec.source == "guard" and rec.selector == "S0"
    assert rec.vetoes_applied == ["V1(suppressed S0 discharge)"]
    assert "peak is allowed to form" in rec.reasoning
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


def test_window_boundary_resets_buffer_and_feeds_detector(make_guard,
                                                         monkeypatch):
    g = make_guard(mode="auto")
    seen = []
    real = capacity.detect_average_mode

    def spy(samples, cfg):
        seen.append(list(samples))
        return real(samples, cfg)
    monkeypatch.setattr(g.mod.capacity, "detect_average_mode", spy)
    quiet = dict(offtake="3.0", avg="3.0", peak="6.0")
    g.at(12, 5).tick(**quiet)
    g.at(12, 10).tick(**quiet)
    assert len(g.mod._samples) == 2
    assert all(isinstance(s, capacity.Sample) for s in seen[-1])
    g.at(12, 15, 5).tick(**quiet)                    # new window, elapsed<2
    assert g.mod._samples == []
    assert len(g.mod._history) == 2
    g.at(12, 20).tick(**quiet)
    assert len(g.mod._samples) == 1                  # only this window's
    assert {s.window_start.minute for s in g.mod._samples} == {15}


def test_auto_mode_detection_reaches_running_verdict(make_guard, monkeypatch):
    g = make_guard(mode="auto")
    verdicts = []
    real = capacity.detect_average_mode

    def spy(samples, cfg):
        v = real(samples, cfg)
        verdicts.append(v)
        return v
    monkeypatch.setattr(g.mod.capacity, "detect_average_mode", spy)
    steady = dict(offtake="3.0", avg="3.0", peak="6.0")   # running semantics
    for start in (0, 15, 30, 45):
        g.at(13, start, 0)
        g.at(13, start + 4).tick(**steady)
        g.at(13, start + 12).tick(**steady)
    g.at(14, 0, 5).tick(**steady)
    assert verdicts[-1].mode == "running"
    assert verdicts[-1].confidence == "detected"


def test_auto_mode_record_states_mode_and_confidence(make_guard):
    g = make_guard(mode="auto").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    # 12:07:30 with "accumulating" assumed: 4.0*15/7.5 = 8.0 kW average
    rec = g.discharges[0][2]
    assert "average mode accumulating (assumed)" in rec.reasoning
    assert "avg_mode_assumed" in rec.degraded_inputs
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


def test_stale_average_still_rolls_the_window_buffer(make_guard):
    g = make_guard(mode="auto")
    quiet = dict(offtake="3.0", avg="3.0", peak="6.0")
    g.at(12, 5).tick(**quiet)
    g.at(12, 10).tick(**quiet)
    g.at(12, 15, 2)
    g.st.updated[AVG] = _stamp(12, 14, 59)
    g.tick(**quiet)
    assert g.mod._samples == [] and len(g.mod._history) == 2


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
stub.read_charge_percent = lambda: 50.0
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
