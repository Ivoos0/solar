"""Tests for pyscript/battery_planner.py (documented extension beyond the WP text).

The adapter is the riskiest untested part, so it is loaded here with stand-ins
for the names pyscript provides at runtime (pyscript_executor, time_trigger,
task_unique, state, service, log, task) injected into builtins and removed
again after each test. Paths are redirected to tmp_path via the module-level
constants; the clock is a mutable test value patched over `_now`.
"""
import ast
import builtins
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "battery_planner.py"
MODULES = SRC.parent / "modules"
UTC = timezone.utc
T0 = datetime(2026, 9, 30, 12, 35, tzinfo=UTC)          # 14:35 in Brussels
NOTIFY = "test_notifier"  # set in Env.write_config; the adapter reads alerts.notify_service
STEP = timedelta(minutes=5)
# Config defaults for the Home Assistant entities (config.py is the source).
PRICE_ENTITY = "sensor.entso_prices_average_electricity_price"
PRICE_ATTRIBUTE = "prices"
FORECAST_ENTITY = "sensor.forecast_solar_estimate"
FORECAST_ATTRIBUTE = "watt_hours_period"
QUARTER_AVG_ENTITY = "sensor.slimmelezer_huidig_kwartiervermogen"
MONTH_PEAK_ENTITY = "sensor.slimmelezer_maandpiek"

CONTRACT_FIELDS = [
    "action", "power", "soc", "cons", "inj", "solar_rem", "usage_rem",
    "saturation", "spill", "breach", "end_soc", "took", "avg", "ceiling",
    "budget", "vetoes", "selector", "why", "degraded", "source",
]

_CORE_NAMES = ("config", "prices", "series", "battery", "trajectory",
               "capacity", "rules", "decision", "cache", "history")
_INJECTED = ("pyscript_executor", "time_trigger", "task_unique", "state",
             "service", "log", "task")


class StateVal(str):
    """What pyscript's state.get() returns: the state string carrying the
    virtual last_reported / last_updated attributes (getattr() strips them)."""


class FakeState:
    def __init__(self):
        self.data = {}
        self.published = []             # (entity, value, attrs) of planner state.set calls
        self.fail_publish = False       # make planner state.set calls raise

    def set(self, entity, value=None, attrs=None, new_attributes=None, **stamps):
        if new_attributes is not None:  # the planner publishing, like pyscript's state.set
            if self.fail_publish:
                raise RuntimeError("state.set down")
            self.published.append((entity, value, dict(new_attributes)))
            attrs = new_attributes
        if stamps:
            value = StateVal(value)
            for k, v in stamps.items():
                setattr(value, k, v)
        self.data[entity] = (value, dict(attrs or {}))

    def drop(self, entity):
        self.data.pop(entity, None)

    def get(self, entity):
        if entity not in self.data:
            raise NameError("name '%s' is not defined" % entity)
        return self.data[entity][0]

    def getattr(self, entity):
        return dict(self.data[entity][1])


class FakeService:
    def __init__(self):
        self.calls = []
        self.fail = set()               # (domain, name) pairs that raise

    def call(self, domain, name, **kwargs):
        self.calls.append((domain, name, kwargs))
        if (domain, name) in self.fail:
            raise RuntimeError("service down")

    def of(self, domain, name):
        return [c for c in self.calls if c[:2] == (domain, name)]


class FakeLog:
    def __init__(self):
        self.by_level = {"info": [], "warning": [], "error": []}

    def info(self, msg, *a):
        self.by_level["info"].append(msg % a if a else msg)

    def warning(self, msg, *a):
        self.by_level["warning"].append(msg % a if a else msg)

    def error(self, msg, *a):
        self.by_level["error"].append(msg % a if a else msg)


class Env:
    """Everything a test needs: the module, fakes, paths and a clock."""

    def __init__(self, mod, tmp):
        self.mod = mod
        self.tmp = tmp
        self.state = builtins.state
        self.service = builtins.service
        self.log = builtins.log
        self.clock = T0
        self.config_path = tmp / "user_config.yaml"
        self.log_dir = tmp / "logs"
        self.cache_dir = tmp / "cache"
        self.state_dir = tmp / "state"
        self.peak_path = self.state_dir / "peak_alert.json"
        self.halt_path = self.state_dir / "halt.json"
        self.mode_path = self.state_dir / "average_mode_planner.json"
        self.command_path = self.state_dir / "last_command.json"
        # Off by default: the fixture's month peak (3.0) is above the floor
        # and would mail in every unrelated test. Peak-alert tests turn it on.
        self.peak_alerts = False

    def run(self, now=None):
        if now is not None:
            self.clock = now
        self.mod.run_cycle(self.clock)

    def lines(self):
        out = []
        for f in self.log_files():
            out += f.read_text(encoding="utf-8").splitlines()
        return out

    def log_files(self):
        if not self.log_dir.exists():
            return []
        return sorted(self.log_dir.glob("decisions-*.log"))

    def day_lines(self, day):
        f = self.log_dir / ("decisions-%s.log" % day)
        return f.read_text(encoding="utf-8").splitlines() if f.exists() else []

    def decisions(self):
        return [l for l in self.lines() if " | action=" in l]

    def write_config(self, extra=""):
        self.config_path.write_text(
            "battery:\n  capacity_kwh: 10.0\nalerts:\n"
            "  address: owner@example.com\n"
            "  notify_service: test_notifier\n"
            "  peak_enabled: %s\n" % ("true" if self.peak_alerts else "false")
            + extra, encoding="utf-8")
        bump = self.config_path.stat().st_mtime + 10
        os.utime(self.config_path, (bump, bump))


def price_entries(days=2, start=None):
    """Hourly {time, price} entries from local midnight, `days` days long."""
    start = start or datetime(2026, 9, 30, 0, 0, tzinfo=UTC) - timedelta(hours=2)
    return [{"time": (start + timedelta(hours=h)).isoformat(),
             "price": 0.08 + 0.02 * (h % 6)} for h in range(24 * days)]


def forecast_payload():
    out = {}
    for day in (30, 31):
        for hour in range(8, 20):
            base = datetime(2026, 9, 30, 0, 0, tzinfo=UTC) + timedelta(
                days=day - 30, hours=hour - 2)
            out[base.isoformat()] = 600
    return out


def fields_of(line):
    parts = line.split(" | ")
    out = {"_ts": parts[0]}
    for p in parts[1:]:
        k, v = p.split("=", 1)
        out[k] = v
    return out


@pytest.fixture
def env(tmp_path):
    saved = {n: getattr(builtins, n) for n in _INJECTED if hasattr(builtins, n)}
    saved_inverter = sys.modules.pop("inverter", None)
    saved_bare = {n: sys.modules.get(n) for n in _CORE_NAMES}
    builtins.pyscript_executor = lambda fn: fn
    builtins.time_trigger = lambda *a, **k: (lambda fn: fn)
    builtins.task_unique = lambda *a, **k: (lambda fn: fn)
    builtins.state = FakeState()
    builtins.service = FakeService()
    builtins.log = FakeLog()
    builtins.task = object()
    restore_decide = None
    try:
        spec = importlib.util.spec_from_file_location(
            "battery_planner_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        e = Env(mod, tmp_path)
        mod.CORE_DIR = str(MODULES)
        mod._ensure_core()
        restore_decide = (mod.rules, mod.rules.decide)  # a test may wrap it
        mod.CONFIG_PATH = str(e.config_path)
        mod.CACHE_DIR = str(e.cache_dir) + "/"
        mod.DECISIONS_LOG_DIR = str(e.log_dir)
        mod.HISTORY_DIR = str(e.tmp / "history") + "/"
        mod.PEAK_ALERT_PATH = str(e.peak_path)
        mod.STATE_DIR = str(e.state_dir) + "/"
        mod.HALT_STATE_PATH = str(e.halt_path)
        mod.MODE_STATE_PATH = str(e.mode_path)
        mod._now = lambda: e.clock
        e.write_config()
        st = e.state
        st.set(PRICE_ENTITY, "0.10", {PRICE_ATTRIBUTE: price_entries()})
        st.set(FORECAST_ENTITY, "12.3",
               {FORECAST_ATTRIBUTE: forecast_payload()})
        st.set("sensor.slimmelezer_power_consumed", "0.8",
               {"unit_of_measurement": "kW"})
        st.set(QUARTER_AVG_ENTITY, "0.7", {"unit_of_measurement": "kW"})
        st.set(MONTH_PEAK_ENTITY, "3.0", {"unit_of_measurement": "kW"})
        yield e
    finally:
        if restore_decide is not None:      # the core module outlives the test
            restore_decide[0].decide = restore_decide[1]
        for n in _INJECTED:
            if n in saved:
                setattr(builtins, n, saved[n])
            elif hasattr(builtins, n):
                delattr(builtins, n)
        sys.modules.pop("inverter", None)
        if saved_inverter is not None:
            sys.modules["inverter"] = saved_inverter
        for n, previous in saved_bare.items():      # loader aliases are permanent
            if previous is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = previous


# ---- normal cycle -----------------------------------------------------------

def test_normal_cycle_appends_one_contract_line(env):
    env.run()
    lines = env.decisions()
    assert len(lines) == 1 and len(env.lines()) == 1
    keys = fields_of(lines[0])
    assert list(keys)[1:] == CONTRACT_FIELDS
    assert all(v != "" for v in keys.values())
    assert re.fullmatch(r"\d+ms", keys["took"])
    assert keys["source"] == "planner"
    assert "soc_stubbed" in keys["degraded"]
    assert "usage_history_unavailable" in keys["degraded"]
    assert keys["_ts"].endswith("+02:00")
    assert env.service.calls == []
    assert env.log.by_level["error"] == []


def test_trajectory_is_never_persisted(env):
    env.run()
    names = sorted(p.name for p in env.cache_dir.iterdir())
    assert names == ["solar.json"]          # usage: empty history is not cached


# ---- interval gating and config reload ---------------------------------------

def test_interval_gating_and_reload_without_restart(env):
    env.run(T0)
    env.run(T0 + timedelta(minutes=2))
    assert len(env.decisions()) == 1                      # gated
    env.run(T0 + STEP)
    assert len(env.decisions()) == 2                      # 5 min elapsed
    env.write_config("timing:\n  evaluation_interval_minutes: 10\n")
    env.run(T0 + 2 * STEP)                                # reloads, runs
    assert len(env.decisions()) == 3
    env.run(T0 + 3 * STEP)                                # now 10 min: gated
    assert len(env.decisions()) == 3
    env.run(T0 + 4 * STEP)
    assert len(env.decisions()) == 4


def test_cron_jitter_does_not_skip_a_slot(env):
    env.run(T0)
    env.run(T0 + timedelta(minutes=5) - timedelta(seconds=1))
    assert len(env.decisions()) == 2


# ---- config failures -----------------------------------------------------------

@pytest.mark.parametrize("body", [
    "battery:\n  capacity_kwh: -3\nalerts:\n  address: a@b.c\n",
    ": : not yaml [",
    "alerts:\n  address: a@b.c\n",
])
def test_invalid_config_no_decision_and_logged(env, body):
    env.config_path.write_text(body, encoding="utf-8")
    env.run()
    assert env.decisions() == []
    assert any("STARTUP FAILURE" in m for m in env.log.by_level["error"])


def test_invalid_value_error_names_the_field(env):
    env.config_path.write_text(
        "battery:\n  capacity_kwh: -3\nalerts:\n  address: a@b.c\n")
    env.run()
    assert any("capacity_kwh" in m for m in env.log.by_level["error"])


def test_missing_config_no_decision(env):
    env.config_path.unlink()
    env.run()
    assert env.decisions() == []
    assert any("cannot read" in m for m in env.log.by_level["error"])


# ---- price outage: halt, alert, recovery ----------------------------------------

def test_price_outage_halts_alerts_once_and_recovers(env):
    st, mod = env.state, env.mod
    st.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    assert env.decisions() == []
    assert len(env.lines()) == 1 and " | HALT | " in env.lines()[0]
    alerts = env.service.of("notify", NOTIFY)
    assert len(alerts) == 1
    assert alerts[0][2]["target"] == ["owner@example.com"]
    env.run(T0 + STEP)                                    # within realert (60)
    assert len(env.service.of("notify", NOTIFY)) == 1
    assert sum(" | HALT | " in l for l in env.lines()) == 2
    env.run(T0 + timedelta(minutes=65))                   # past realert
    assert len(env.service.of("notify", NOTIFY)) == 2
    st.set(PRICE_ENTITY, "0.10", {PRICE_ATTRIBUTE: price_entries()})
    env.run(T0 + timedelta(minutes=70))
    assert any(" | RECOVERED | " in l for l in env.lines())
    assert len(env.decisions()) == 1
    env.run(T0 + timedelta(minutes=75))
    assert sum(" | RECOVERED | " in l for l in env.lines()) == 1
    assert len(env.service.of("notify", NOTIFY)) == 2


@pytest.mark.parametrize("attrs", [
    {},                                                    # attribute missing
    {"prices": []},
    {"prices": price_entries(days=1,
                             start=datetime(2026, 9, 28, 0, 0, tzinfo=UTC))},
], ids=["no-attribute", "empty", "series-already-elapsed"])
def test_missing_or_stale_prices_halt(env, attrs):
    env.state.set(PRICE_ENTITY, "0.10", attrs)
    env.run()
    assert env.decisions() == []
    assert " | HALT | " in env.lines()[0]


def test_halt_line_carries_alert_time_and_no_decision_logic(env):
    env.state.set(PRICE_ENTITY, "unknown", {})
    env.run()
    parts = env.lines()[0].split(" | ")
    assert parts[1] == "HALT" and parts[2].startswith("cause=")
    assert parts[4] != "alerted=none"


# ---- forecast outage: degrade, not halt --------------------------------------------

def test_forecast_outage_degrades_to_zero_solar_without_halt(env):
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    env.run()
    lines = env.decisions()
    assert len(lines) == 1
    assert "solar_zero_fallback" in fields_of(lines[0])["degraded"]
    assert not any("HALT" in l for l in env.lines())
    assert env.service.of("notify", NOTIFY) == []
    assert not (env.cache_dir / "solar.json").exists()    # fallback not cached


def test_forecast_retry_is_spaced_and_counted(env):
    mod = env.mod
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    env.run(T0)
    assert env.service.of("homeassistant", "update_entity") == []   # threshold
    env.run(T0 + STEP)
    assert len(env.service.of("homeassistant", "update_entity")) == 1
    assert env.service.of("homeassistant", "update_entity")[0][2] == {
        "entity_id": FORECAST_ENTITY}
    env.run(T0 + 2 * STEP)                                # only 5 min later
    assert len(env.service.of("homeassistant", "update_entity")) == 1
    env.run(T0 + 3 * STEP)                                # 10 min later
    assert len(env.service.of("homeassistant", "update_entity")) == 2


def test_forecast_retries_stay_within_hourly_budget(env):
    env.write_config("timing:\n  evaluation_interval_minutes: 1\n"
                     "  forecast_retry_minutes: 1\n")
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    for i in range(60):
        env.run(T0 + timedelta(minutes=i))
    assert len(env.service.of("homeassistant", "update_entity")) <= 11


def test_forecast_down_with_fresh_solar_cache_states_its_age(env):
    env.run(T0)
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    env.run(T0 + STEP)
    degraded = fields_of(env.decisions()[-1])["degraded"]
    assert "cache_age_solar=5m" in degraded            
    assert "solar_zero_fallback" not in degraded


def test_fresh_cache_hit_adds_age_marker_rebuilt_series_does_not(env):
    env.run(T0)                                           # miss: rebuilt
    assert "cache_age" not in fields_of(env.decisions()[-1])["degraded"]
    env.run(T0 + STEP)                                    # fresh hit
    assert "cache_age_solar=5m" in fields_of(env.decisions()[-1])["degraded"]
    payload = forecast_payload()
    payload[next(iter(payload))] = 900                    # new forecast: rebuilt
    env.state.set(FORECAST_ENTITY, "12.3",
                  {FORECAST_ATTRIBUTE: payload})
    env.run(T0 + 2 * STEP)
    assert "cache_age_solar" not in fields_of(env.decisions()[-1])["degraded"]


def test_fresh_usage_cache_hit_adds_age_marker(env, monkeypatch):
    monkeypatch.setattr(env.mod, "read_usage_history",
                        lambda cfg, local: _history(3))
    env.run(T0)
    assert "cache_age_usage" not in fields_of(env.decisions()[-1])["degraded"]
    env.run(T0 + STEP)
    assert "cache_age_usage=5m" in fields_of(env.decisions()[-1])["degraded"]


# ---- cache -----------------------------------------------------------------------------

def test_stale_cache_used_with_age_marker_when_refresh_impossible(env):
    env.write_config("timing:\n  solar_cache_stale_minutes: 10\n")
    env.run(T0)
    assert (env.cache_dir / "solar.json").exists()
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    env.run(T0 + timedelta(minutes=30))
    degraded = fields_of(env.decisions()[-1])["degraded"]
    assert "cache_age_solar=30m" in degraded
    assert "solar_zero_fallback" not in degraded


def test_solar_cache_miss_rebuilds_then_hits_and_survives_deletion(env, monkeypatch):
    calls = []
    real = env.mod.series.solar_series

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(env.mod.series, "solar_series", counting)
    env.run(T0)
    env.run(T0 + STEP)
    assert len(calls) == 1                                 # second was a hit
    before = fields_of(env.decisions()[-1])
    import shutil
    shutil.rmtree(env.cache_dir)
    env.mod._last_run = None                               # same instant again
    env.run(T0 + STEP)
    assert len(calls) == 2                                 # miss rebuilt
    after = fields_of(env.decisions()[-1])
    for key in ("action", "power", "selector", "solar_rem"):
        assert before[key] == after[key]                 


def test_new_forecast_rebuilds_solar(env, monkeypatch):
    calls = []
    real = env.mod.series.solar_series
    monkeypatch.setattr(env.mod.series, "solar_series",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    env.run(T0)
    payload = forecast_payload()
    first = next(iter(payload))
    payload[first] = 900
    env.state.set(FORECAST_ENTITY, "12.3",
                  {FORECAST_ATTRIBUTE: payload})
    env.run(T0 + STEP)
    assert len(calls) == 2


def _history(days):
    out = []
    end = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)   # local midnight
    for d in range(1, days + 1):
        for q in range(96):
            out.append((end - timedelta(days=d) + timedelta(minutes=15 * q), 0.1))
    return out


def test_usage_rebuilt_daily_not_per_cycle_and_coverage_reported(env, monkeypatch):
    calls = []

    def history(cfg, local):
        calls.append(local)
        return _history(3)

    monkeypatch.setattr(env.mod, "read_usage_history", history)
    env.run(T0)
    env.run(T0 + STEP)
    env.run(T0 + 2 * STEP)
    assert len(calls) == 1
    degraded = fields_of(env.decisions()[-1])["degraded"]
    assert "usage_samples=3" in degraded                   # 3 of 28 days
    assert "usage_history_unavailable" not in degraded
    env.state.set(PRICE_ENTITY, "0.1", {
        PRICE_ATTRIBUTE: price_entries(days=3)})
    env.run(T0 + timedelta(hours=12))                      # next local day
    assert len(calls) == 2


def test_full_history_coverage_reports_no_marker(env, monkeypatch):
    monkeypatch.setattr(env.mod, "read_usage_history",
                        lambda cfg, local: _history(28))
    env.run()
    assert "usage_samples" not in fields_of(env.decisions()[0])["degraded"]


def test_atomic_write_leaves_no_temp_file(env):
    env.run()
    assert not list(env.cache_dir.glob("*.tmp"))
    assert (env.cache_dir / "solar.json").read_text().startswith("{")


# ---- resilience --------------------------------------------------------------------------

def test_exception_is_swallowed_logged_and_next_cycle_runs(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    real = env.mod.trajectory.project
    monkeypatch.setattr(env.mod.trajectory, "project", boom)
    env.run(T0)                                            # must not raise
    assert env.decisions() == []
    assert any("cycle failed" in m and "kaboom" in m
               for m in env.log.by_level["error"])
    monkeypatch.setattr(env.mod.trajectory, "project", real)
    env.run(T0 + STEP)
    assert len(env.decisions()) == 1


# ---- peak guard coordination ----------------------------------------------------------------

def _force_grid_charge(env, monkeypatch):
    mod = env.mod
    real = mod.rules.decide

    def decide(traj, price_map, bat, grid, cfg, now, **kw):
        d = real(traj, price_map, bat, grid, cfg, now, **kw)
        return mod.rules.Decision(
            "charge", 3.0, "S1", "cheapest block", [], [],
            d.block_start)

    monkeypatch.setattr(mod.rules, "decide", decide)


def test_grid_charge_passes_when_guard_is_off(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(env.mod.GUARD_FLAG_ENTITY, "off", {})
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "charge"


def test_grid_charge_downgraded_while_guard_shaving(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    _flag(env, 10)
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "idle" and keys["power"] == "0.00kW"
    assert keys["selector"] == "S6"
    assert "GUARD(suppressed S1 charge)" in keys["vetoes"]
    assert "peak guard is shaving" in keys["why"]


def test_grid_charge_downgraded_when_grid_sensors_unreadable(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(QUARTER_AVG_ENTITY, "unavailable", {})
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "idle"
    assert "NOGRID" in keys["vetoes"]
    assert "grid_sensors_unavailable" in keys["degraded"]


# ---- guard heartbeat: a stuck "on" flag must not block charging forever ------

def _flag(env, beat_age_s, value="on"):
    attrs = {}
    if beat_age_s is not None:
        attrs["last_beat"] = (env.clock - timedelta(seconds=beat_age_s)
                              ).isoformat()
    env.state.set(env.mod.GUARD_FLAG_ENTITY, value, attrs)


def test_guard_flag_with_a_fresh_beat_suppresses_grid_charge(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    _flag(env, 30)
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "idle" and "GUARD" in keys["vetoes"]


def test_guard_flag_beat_at_the_limit_still_counts(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    _flag(env, 90)                  # 3 x the 30 s default guard interval
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "idle"


def test_stuck_on_flag_with_an_old_beat_is_ignored(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    _flag(env, 91)
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "charge" and "GUARD" not in keys["vetoes"]
    assert any("heartbeat is 91 s old" in m for m in env.log.by_level["warning"])


def test_on_flag_without_a_beat_is_ignored(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    _flag(env, None)
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "charge"
    assert any("heartbeat is missing" in m for m in env.log.by_level["warning"])


def test_unparseable_beat_is_ignored(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(env.mod.GUARD_FLAG_ENTITY, "on", {"last_beat": "soon"})
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "charge"


def test_beat_window_follows_the_guard_interval(env):
    env.write_config(
        extra="capacity_tariff:\n  guard_interval_seconds: 60\n")
    cfg = env.mod._load_config()
    _flag(env, 180)
    assert env.mod._guard_is_shaving(cfg, env.clock) is True
    _flag(env, 181)
    assert env.mod._guard_is_shaving(cfg, env.clock) is False


def test_off_flag_is_off_whatever_the_beat(env):
    cfg = env.mod._load_config()
    _flag(env, 1, value="off")
    assert env.mod._guard_is_shaving(cfg, env.clock) is False


# ---- source hygiene ---------------------------------------------------------------------------

def test_no_credentials_or_smtp_in_source():
    text = SRC.read_text(encoding="utf-8")
    assert not re.search(r"(?i)password|passwd|secret|api[_-]?key|smtp|token",
                         text)
    assert "\r" not in text


def _functions(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _decorator_names(fn):
    names = []
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        names.append(getattr(target, "id", getattr(target, "attr", "")))
    return names


def test_every_open_lives_in_a_pyscript_executor_helper():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    opens = 0
    for fn in _functions(tree):
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "open"):
                opens += 1
                assert "pyscript_executor" in _decorator_names(fn), fn.name
    assert opens >= 4
    module_level = [n for n in tree.body if isinstance(n, ast.Expr)
                    or (isinstance(n, ast.With))]
    assert not module_level[1:]        # only the module docstring at top level


def test_no_bare_pyscript_compile_and_trigger_is_declared():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    every = [n for fn in _functions(tree) for n in _decorator_names(fn)]
    assert "pyscript_compile" not in every
    trigger = [fn for fn in _functions(tree)
               if "time_trigger" in _decorator_names(fn)]
    assert len(trigger) == 1
    assert "task_unique" not in _decorator_names(trigger[0])  # kill_me is silent
    call = next(d for d in trigger[0].decorator_list
                if getattr(d.func, "id", "") == "time_trigger")
    assert call.args[0].value == "cron(* * * * *)"


# ---- native core loading (interpreter cannot run the core) ---------------------

def test_core_modules_are_bound_natively_under_private_names(env):
    mod = env.mod
    for name in mod.CORE_MODULES:
        bound = getattr(mod, name)
        assert bound.__name__ == "battery_planner_core_" + name
        assert Path(bound.__file__) == MODULES / (name + ".py")
    assert sys.modules["battery_planner_core_rules"] is mod.rules


def test_loader_aliases_bare_names_for_lifetime_and_leaves_sys_path(env, monkeypatch):
    mod = env.mod
    for name in mod.CORE_MODULES:               # force a real (re)load
        monkeypatch.delitem(sys.modules, "battery_planner_core_" + name)
    path_before = list(sys.path)
    loaded = mod._load_core(str(MODULES), mod.CORE_MODULES)
    for name in mod.CORE_MODULES:               # function-local imports need these
        assert sys.modules[name] is sys.modules["battery_planner_core_" + name]
    assert "inverter" not in mod.CORE_MODULES
    assert sys.path == path_before
    assert loaded["rules"].capacity is loaded["capacity"]  # rules got the native one


_SUBPROCESS_SCRIPT = r'''
import builtins, importlib.util, sys, types
from datetime import datetime, timedelta, timezone
from pathlib import Path

src, modules, tmp = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
assert not any(Path(p).resolve() == modules.resolve() for p in sys.path if p)
assert not any(n in sys.modules for n in (
    "config", "prices", "series", "battery", "trajectory", "capacity",
    "rules", "decision", "cache", "inverter"))
UTC = timezone.utc
T0 = datetime(2026, 9, 30, 12, 35, tzinfo=UTC)


class State:
    def __init__(self):
        self.d = {}

    def get(self, e):
        if e not in self.d:
            raise NameError(e)
        return self.d[e][0]

    def getattr(self, e):
        return dict(self.d[e][1])


class Log:
    errors = []

    def info(self, *a): pass
    def warning(self, *a): pass
    def error(self, m, *a): self.errors.append(m)


class Service:
    def call(self, *a, **k): pass


builtins.pyscript_executor = lambda fn: fn
builtins.time_trigger = lambda *a, **k: (lambda fn: fn)
builtins.state, builtins.service, builtins.log = State(), Service(), Log()

# Stand-in for pyscript's own import of the interpreted inverter module.
stub = types.ModuleType("inverter")
stub.DEFAULT_LOG_DIR = str(tmp / "logs")
sys.modules["inverter"] = stub
spec = importlib.util.spec_from_file_location("bp", src)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.CORE_DIR = str(modules)
mod._ensure_core()                     # core loaded; bare aliases now exist
ispec = importlib.util.spec_from_file_location("inverter", modules / "inverter.py")
real = importlib.util.module_from_spec(ispec)
sys.modules["inverter"] = real
ispec.loader.exec_module(real)         # its `import decision` resolves via alias
mod.inverter = real

(tmp / "user_config.yaml").write_text(
    "battery:\n  capacity_kwh: 10.0\nalerts:\n  address: a@b.c\n")
mod.CONFIG_PATH = str(tmp / "user_config.yaml")
mod.CACHE_DIR = str(tmp / "cache") + "/"
mod.DECISIONS_LOG_DIR = str(tmp / "logs")
mod.STATE_DIR = str(tmp / "state") + "/"
mod.HALT_STATE_PATH = str(tmp / "state" / "halt.json")
mod.MODE_STATE_PATH = str(tmp / "state" / "average_mode_planner.json")
mod._now = lambda: T0
start = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
mod_state = builtins.state
mod_state.d["sensor.entso_prices_average_electricity_price"] = ("0.10", {"prices": [
    {"time": (start + timedelta(hours=h)).isoformat(), "price": 0.08 + 0.02 * (h % 6)}
    for h in range(48)]})
mod_state.d["sensor.forecast_solar_estimate"] = ("1", {"watt_hours_period": {
    (start + timedelta(hours=h)).isoformat(): 600 for h in range(8, 20)}})
for ent in ("sensor.slimmelezer_power_consumed", "sensor.slimmelezer_huidig_kwartiervermogen",
            "sensor.slimmelezer_maandpiek"):
    mod_state.d[ent] = ("0.8", {"unit_of_measurement": "kW"})
mod.run_cycle(T0)
assert not Log.errors, Log.errors
files = sorted((tmp / "logs").glob("decisions-2026-09-30.log"))
assert len(files) == 1, list((tmp / "logs").iterdir())
lines = files[0].read_text().splitlines()
assert len(lines) == 1 and " | action=" in lines[0], lines
print("OK")
'''


def test_full_cycle_writes_a_record_without_modules_dir_on_sys_path(tmp_path):
    """Function-local core imports (decision.build -> import capacity) must
    resolve in HA, where pyscript/modules is not on sys.path."""
    import subprocess
    script = tmp_path / "run.py"
    script.write_text(_SUBPROCESS_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(script), str(SRC), str(MODULES), str(tmp_path)],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "OK"


def test_loader_failure_leaves_no_half_loaded_module(env, tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("raise RuntimeError('broken')\n")
    monkeypatch.delitem(sys.modules, "battery_planner_core_config")
    with pytest.raises(RuntimeError):
        env.mod._load_core(str(tmp_path), ("config",))
    assert "battery_planner_core_config" not in sys.modules


# ---- interpreter-safety lint ----------------------------------------------------------------

_INTERPRETED = [SRC, MODULES / "inverter.py", SRC.parent / "housekeeping.py"]
_DECORATORS = {"property", "staticmethod", "classmethod"}


def interpreter_violations(source):
    """Constructs pyscript's AST interpreter cannot run (see the adapter docstring)."""
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


@pytest.mark.parametrize("path", _INTERPRETED, ids=lambda p: p.name)
def test_interpreted_files_use_only_interpreter_safe_constructs(path):
    assert interpreter_violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("snippet, what", [
    ("x = sum(i for i in range(3))", "generator"),
    ("class A:\n    @property\n    def p(self): return 1", "@property"),
    ("class A:\n    @staticmethod\n    def p(): return 1", "@staticmethod"),
    ("class A:\n    @classmethod\n    def p(cls): return 1", "@classmethod"),
    ("class A:\n    def __post_init__(self): pass", "dunder"),
    ("def g():\n    yield 1", "yield"),
    ("match 1:\n    case 1: pass", "match"),
    ("def f(x): return x\nsorted([1], key=f)", "callback"),
    ("def f(x): return x\nlist(map(f, [1]))", "callback"),
    ("def f(x): return x\nlist(filter(f, [1]))", "callback"),
    ("sorted([1], key=lambda x: x)", "callback"),
])
def test_lint_detects_each_forbidden_construct(snippet, what):
    assert interpreter_violations(snippet), what


def test_lint_allows_comprehensions_and_native_key_callables():
    assert interpreter_violations(
        "a = [i for i in range(3)]\nb = {i for i in a}\nc = sorted(a, key=abs)") == []


# ---- overlap: busy marker, SKIP line, hung-cycle error -----------------------------------------

def test_due_cycle_while_busy_is_skipped_recorded_and_warned(env):
    env.run(T0)
    env.mod._cycle_started_at = T0 + STEP - timedelta(minutes=3)   # running 3 min
    env.run(T0 + STEP)
    assert len(env.decisions()) == 1                       # not run, not queued
    skips = [l for l in env.lines() if " | SKIP | " in l]
    assert len(skips) == 1 and "busy_for=180s" in skips[0]
    assert any("cycle skipped" in m for m in env.log.by_level["warning"])
    assert env.log.by_level["error"] == []
    assert env.mod._cycle_started_at is not None           # not stolen or cleared


def test_marker_older_than_two_intervals_is_an_error(env):
    env.run(T0)
    env.mod._cycle_started_at = T0 - timedelta(minutes=6)  # 11 min at T0+5 > 10
    env.run(T0 + STEP)
    assert any("cycle skipped" in m and "hung" in m
               for m in env.log.by_level["error"])
    assert sum(" | SKIP | " in l for l in env.lines()) == 1


def test_gated_not_due_invocation_is_silent_even_when_busy(env):
    env.run(T0)
    env.mod._cycle_started_at = T0
    env.run(T0 + timedelta(minutes=1))
    assert not any(" | SKIP | " in l for l in env.lines())


def test_busy_marker_cleared_after_success_and_after_exception(env, monkeypatch):
    env.run(T0)
    assert env.mod._cycle_started_at is None
    monkeypatch.setattr(env.mod.trajectory, "project",
                        lambda *a, **k: 1 / 0)
    env.run(T0 + STEP)
    assert env.mod._cycle_started_at is None
    assert any("cycle failed" in m for m in env.log.by_level["error"])


def test_cycle_sets_marker_while_running(env, monkeypatch):
    seen = []
    real = env.mod.trajectory.project
    monkeypatch.setattr(env.mod.trajectory, "project",
                        lambda *a, **k: seen.append(env.mod._cycle_started_at)
                        or real(*a, **k))
    env.run(T0)
    assert seen == [T0]


# ---- alert failure, unit conversion, DST fall-back --------------------------------------------

def _alerted(line):
    return next(p for p in line.split(" | ") if p.startswith("alerted="))[8:]


def test_failed_alert_send_is_retried_and_recorded_as_not_alerted(env):
    mod = env.mod
    env.service.fail.add(("notify", NOTIFY))
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    assert _alerted(env.lines()[0]) == "none"
    assert any("alert send failed" in m for m in env.log.by_level["error"])
    env.run(T0 + STEP)                                     # retried, not rate-limited
    assert len(env.service.of("notify", NOTIFY)) == 2
    assert _alerted(env.lines()[1]) == "none"
    env.service.fail.clear()
    env.run(T0 + 2 * STEP)
    assert len(env.service.of("notify", NOTIFY)) == 3
    assert _alerted(env.lines()[2]) != "none"
    env.run(T0 + 3 * STEP)                                 # now rate-limited
    assert len(env.service.of("notify", NOTIFY)) == 3


def test_sensor_kw_converts_watts_and_leaves_kw_alone(env):
    st, mod = env.state, env.mod
    st.set("sensor.w", "700", {"unit_of_measurement": "W"})
    st.set("sensor.kw", "0.7", {"unit_of_measurement": "kW"})
    st.set("sensor.bare", "0.7", {})
    st.set("sensor.bad", "n/a", {"unit_of_measurement": "W"})
    assert mod._sensor_kw("sensor.w") == pytest.approx(0.7)
    assert mod._sensor_kw("sensor.kw") == pytest.approx(0.7)
    assert mod._sensor_kw("sensor.bare") == pytest.approx(0.7)
    assert mod._sensor_kw("sensor.bad") is None
    assert mod._sensor_kw("sensor.missing") is None


def test_dst_fall_back_price_map_is_not_rekeyed_in_local_time(env, monkeypatch):
    mod = env.mod
    start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)      # local midnight, CEST
    env.state.set(PRICE_ENTITY, "0.10",
                  {"prices": price_entries(days=2, start=start)})
    seen = []
    real = mod.rules.decide

    def spy(traj, price_map, *a, **kw):
        seen.append(dict(price_map))
        return real(traj, price_map, *a, **kw)

    monkeypatch.setattr(mod.rules, "decide", spy)
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)       # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)      # 02:30 CET
    env.run(first)
    env.run(second)
    assert len(env.decisions()) == 2 and len(seen) == 2
    step = timedelta(minutes=15)
    for price_map, now in zip(seen, (first, second)):
        starts = sorted(k.astimezone(UTC) for k in price_map)
        assert len(set(starts)) == len(starts)
        assert all(b - a == step for a, b in zip(starts, starts[1:])), \
            "gap or overlap: prices were re-keyed in local time"
        assert starts[0] <= now < starts[0] + step
        hour_2 = [t for t in starts
                  if datetime(2026, 10, 25, 0, 0, tzinfo=UTC) <= t
                  < datetime(2026, 10, 25, 2, 0, tzinfo=UTC)]
        if now == first:
            assert len(hour_2) == 6                          # 00:30Z..02:00Z blocks


# ---- V4: no usage history never grid-charges ------------------------------------------------

def _negative_prices(env):
    mod = env.mod
    entries = [dict(e, price=-0.20) for e in price_entries()]
    env.state.set(PRICE_ENTITY, "-0.20", {"prices": entries})


def test_empty_history_passes_false_and_history_passes_true(env, monkeypatch):
    mod = env.mod
    seen = []
    real = mod.rules.decide

    def spy(*a, **kw):
        seen.append(kw.get("usage_history_available"))
        return real(*a, **kw)

    monkeypatch.setattr(mod.rules, "decide", spy)
    env.run()
    monkeypatch.setattr(mod, "read_usage_history",
                        lambda cfg, local: _history(3))
    env.run(T0 + timedelta(hours=12))                      # next local day
    assert seen == [False, True]


def test_empty_history_vetoes_grid_charge_in_the_record(env):
    _negative_prices(env)
    env.run()
    keys = fields_of(env.decisions()[0])
    # Without usage history the planner holds: S1 is vetoed and the
    # record is an idle one that says why.
    assert keys["selector"] == "S6" and keys["action"] == "idle"
    assert "V4(suppressed S1 charge)" in keys["vetoes"]
    assert "no usage history: planner holds" in keys["why"]
    assert "usage_history_unavailable" in keys["degraded"]


def test_populated_history_lets_the_same_prices_grid_charge(env, monkeypatch):
    monkeypatch.setattr(env.mod, "read_usage_history",
                        lambda cfg, local: _history(3))
    _negative_prices(env)
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "charge" and keys["selector"] == "S1"
    assert "V4" not in keys["vetoes"]


def test_alert_uses_default_service_when_config_omits_it(env):
    mod = env.mod
    env.config_path.write_text(
        "battery:\n  capacity_kwh: 10.0\nalerts:\n"
        "  address: owner@example.com\n  peak_enabled: false\n",
        encoding="utf-8")
    bump = env.config_path.stat().st_mtime + 20
    os.utime(env.config_path, (bump, bump))
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    assert len(env.service.of("notify", "battery_alert")) == 1
    assert env.service.of("notify", NOTIFY) == []


def test_alert_goes_to_the_configured_service_name(env):
    mod = env.mod
    env.write_config()
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    calls = env.service.of("notify", NOTIFY)
    assert len(calls) == 1 and calls[0][2]["target"] == ["owner@example.com"]


# ---- inverter.type: the planner selects the driver from config ---------------------

_REAL_DRIVER = '''
SOC_IS_STUB = False
SENT = []
def send(action, target_power_kw):
    SENT.append((action, target_power_kw))
    return True
def read_charge_percent():
    return 61.0
'''


def _use_driver(env, tmp_path, kind, source=_REAL_DRIVER):
    d = tmp_path / "drv"
    d.mkdir(exist_ok=True)
    if source is not None:
        (d / ("inverter_%s.py" % kind)).write_text(source, encoding="utf-8")
    env.mod.CORE_DIR = str(d)             # core is already bound; drivers load here
    env.write_config("inverter:\n  type: %s\n" % kind)


@pytest.fixture(autouse=False)
def _clean_drivers():
    yield
    for name in list(sys.modules):
        if name.startswith("inverter_driver_"):
            del sys.modules[name]


def test_default_config_uses_logging_driver_and_stays_stubbed(env, _clean_drivers):
    env.mod.CORE_DIR = str(MODULES)
    env.run()
    keys = fields_of(env.decisions()[0])
    assert "soc_stubbed" in keys["degraded"]
    assert "inverter_driver_unavailable" not in keys["degraded"]
    assert env.log.by_level["error"] == []


def test_configured_driver_gets_the_decision_and_clears_the_stub_marker(
        env, tmp_path, _clean_drivers):
    _use_driver(env, tmp_path, "realdrv")
    env.run()
    lines = env.decisions()
    assert len(lines) == 1
    keys = fields_of(lines[0])
    assert "soc_stubbed" not in keys["degraded"]
    assert keys["soc"].startswith("61")
    sent = sys.modules["inverter_driver_realdrv"].SENT
    assert sent == [(keys["action"], float(keys["power"].replace("kW", "")))]
    assert env.log.by_level["error"] == []


def test_raising_driver_still_logs_and_the_cycle_survives(
        env, tmp_path, _clean_drivers):
    _use_driver(env, tmp_path, "boom", _REAL_DRIVER.replace(
        "    return True", "    raise OSError('bus down')"))
    env.run()
    assert len(env.decisions()) == 1
    assert any("bus down" in m for m in env.log.by_level["error"])
    assert not any("cycle failed" in m for m in env.log.by_level["error"])
    env.run(T0 + STEP)                                    # next cycle runs too
    assert len(env.decisions()) == 2


def test_unknown_driver_falls_back_to_logging_with_marker_and_error(
        env, tmp_path, _clean_drivers):
    _use_driver(env, tmp_path, "nonexistent", source=None)
    env.run()
    lines = env.decisions()
    assert len(lines) == 1
    keys = fields_of(lines[0])
    assert "inverter_driver_unavailable" in keys["degraded"]
    assert "soc_stubbed" in keys["degraded"]
    first_errors = [m for m in env.log.by_level["error"] if "NOT transmitting" in m]
    assert len(first_errors) == 1
    env.run(T0 + STEP)
    assert len([m for m in env.log.by_level["error"]
                if "NOT transmitting" in m]) == 2         # loud every cycle
    assert not any("cycle failed" in m for m in env.log.by_level["error"])


# ---- daily rotation: one file per local day ---------------------------------------

def test_cycle_writes_to_the_dated_file_of_its_local_day(env):
    env.run(T0)
    assert [f.name for f in env.log_files()] == ["decisions-2026-09-30.log"]
    assert len(env.day_lines("2026-09-30")) == 1


def test_local_date_not_utc_date_names_the_file(env):
    # 22:30 UTC on 30 Sep is 00:30 on 1 Oct in Brussels (CEST, +02:00).
    env.run(datetime(2026, 9, 30, 22, 30, tzinfo=UTC))
    assert [f.name for f in env.log_files()] == ["decisions-2026-10-01.log"]


def test_midnight_boundary_splits_files(env):
    env.run(datetime(2026, 9, 30, 21, 59, 59, tzinfo=UTC))   # 23:59:59 local
    env.mod._last_run = None                  # one second later is gated
    env.run(datetime(2026, 9, 30, 22, 0, 0, tzinfo=UTC))     # 00:00:00 local
    assert len(env.day_lines("2026-09-30")) == 1
    assert len(env.day_lines("2026-10-01")) == 1
    assert env.day_lines("2026-09-30")[0].startswith("2026-09-30T23:59:59")
    assert env.day_lines("2026-10-01")[0].startswith("2026-10-01T00:00:00")


def test_two_consecutive_days_make_two_files(env):
    env.run(T0)
    env.run(T0 + timedelta(days=1))
    assert [f.name for f in env.log_files()] == [
        "decisions-2026-09-30.log", "decisions-2026-10-01.log"]


def test_halt_recovered_and_decision_share_the_days_file(env):
    st, mod = env.state, env.mod
    st.set(PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    st.set(PRICE_ENTITY, "0.10", {"prices": price_entries()})
    env.run(T0 + STEP)
    env.run(T0 + 2 * STEP)
    assert [f.name for f in env.log_files()] == ["decisions-2026-09-30.log"]
    text = "\n".join(env.day_lines("2026-09-30"))
    assert " | HALT | " in text and " | RECOVERED | " in text
    assert "source=planner" in text


def test_halt_line_after_midnight_goes_to_the_next_days_file(env):
    env.run(T0)
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run(datetime(2026, 9, 30, 22, 5, tzinfo=UTC))        # 00:05 local, 1 Oct
    assert any(" | HALT | " in l for l in env.day_lines("2026-10-01"))
    assert not any(" | HALT | " in l for l in env.day_lines("2026-09-30"))


def test_skip_line_uses_the_local_day_of_the_cycle_time(env):
    env.run(T0)
    late = datetime(2026, 9, 30, 22, 5, tzinfo=UTC)          # 00:05 local, 1 Oct
    env.mod._cycle_started_at = late - timedelta(minutes=3)
    env.run(late)
    assert any(" | SKIP | " in l for l in env.day_lines("2026-10-01"))
    assert not any(" | SKIP | " in l for l in env.day_lines("2026-09-30"))


def test_recovered_line_goes_to_the_day_it_is_written_not_the_halt_day(env):
    st, mod = env.state, env.mod
    st.set(PRICE_ENTITY, "unavailable", {})
    env.run(datetime(2026, 9, 30, 21, 55, tzinfo=UTC))       # 23:55 local, 30 Sep
    st.set(PRICE_ENTITY, "0.10", {"prices": price_entries()})
    env.run(datetime(2026, 9, 30, 22, 5, tzinfo=UTC))        # 00:05 local, 1 Oct
    assert any(" | HALT | " in l for l in env.day_lines("2026-09-30"))
    assert any(" | RECOVERED | " in l for l in env.day_lines("2026-10-01"))
    assert not any(" | RECOVERED | " in l for l in env.day_lines("2026-09-30"))


# --- configurable entities ----------------------------------------------------

CUSTOM_PRICE = "sensor.my_prices"
CUSTOM_FORECAST = "sensor.my_forecast"
CUSTOM_AVG = "sensor.my_quarter_avg"
CUSTOM_PEAK = "sensor.my_month_peak"
CUSTOM_CONFIG = (
    "prices:\n  entity: sensor.my_prices\n  attribute: price_list\n"
    "solar:\n  forecast_entity: sensor.my_forecast\n"
    "  forecast_attribute: wh_period\n"
    "capacity_tariff:\n  quarter_hour_average_sensor: sensor.my_quarter_avg\n"
    "  month_peak_sensor: sensor.my_month_peak\n")


def _live_shaped_entries():
    """Exactly the live entsoe shape: 'YYYY-MM-DD HH:MM:SS+02:00' strings."""
    start = datetime(2026, 9, 30, 0, 0, tzinfo=UTC) - timedelta(hours=2)
    out = []
    for i in range(24 * 4 * 2):
        t = (start + timedelta(minutes=15 * i)).astimezone(
            timezone(timedelta(hours=2)))
        out.append({"time": t.strftime("%Y-%m-%d %H:%M:%S") + "+02:00",
                    "price": round(0.08 + 0.02 * ((i // 4) % 6), 5)})
    return out


def test_live_shaped_prices_on_default_entity_produce_decision(env):
    entries = _live_shaped_entries()
    assert " " in entries[0]["time"] and entries[0]["time"].endswith("+02:00")
    env.state.set(PRICE_ENTITY, "0.1", {PRICE_ATTRIBUTE: entries})
    env.run()
    assert len(env.decisions()) == 1
    assert not any(" | HALT | " in l for l in env.lines())


def test_custom_entities_are_read_and_defaults_ignored(env):
    st = env.state
    for default in (PRICE_ENTITY, FORECAST_ENTITY, QUARTER_AVG_ENTITY,
                    MONTH_PEAK_ENTITY):
        st.drop(default)                       # default names absent entirely
    st.set(CUSTOM_PRICE, "0.10", {"price_list": price_entries()})
    st.set(CUSTOM_FORECAST, "12.3", {"wh_period": forecast_payload()})
    st.set(CUSTOM_AVG, "0.7", {"unit_of_measurement": "kW"})
    st.set(CUSTOM_PEAK, "3.0", {"unit_of_measurement": "kW"})
    env.write_config(CUSTOM_CONFIG)
    env.run()
    assert len(env.decisions()) == 1
    import json
    solar = json.loads((env.cache_dir / "solar.json").read_text())
    assert solar["source"] == CUSTOM_FORECAST


def test_custom_price_entity_ignores_default_data(env):
    # default entity still has good data, the configured one is absent: halt
    env.write_config(CUSTOM_CONFIG)
    env.run()
    assert env.decisions() == []
    assert " | HALT | " in env.lines()[0]
    assert CUSTOM_PRICE in env.lines()[0]


def test_custom_sensors_feed_grid_state(env):
    env.write_config(CUSTOM_CONFIG)
    env.state.set(CUSTOM_PRICE, "0.10", {"price_list": price_entries()})
    env.state.set(CUSTOM_FORECAST, "1", {"wh_period": forecast_payload()})
    # custom capacity sensors absent although the defaults have data: the
    # grid state must be unreadable (defaults are NOT consulted)
    env.run()
    assert len(env.decisions()) == 1
    before = env.decisions()[0]
    env.state.set(CUSTOM_AVG, "0.7", {"unit_of_measurement": "kW"})
    env.run(T0 + STEP)                       # month peak still absent
    assert fields_of(env.decisions()[-1])["avg"] == fields_of(before)["avg"]
    env.state.set(CUSTOM_PEAK, "3.0", {"unit_of_measurement": "kW"})
    env.run(T0 + 2 * STEP)
    after = env.decisions()[-1]
    assert fields_of(before)["avg"] != fields_of(after)["avg"]


def test_halt_cause_names_configured_entity_and_attribute(env):
    env.write_config(CUSTOM_CONFIG)
    env.state.set(CUSTOM_PRICE, "0.10", {"prices": price_entries()})  # wrong attr
    env.run()
    line = env.lines()[0]
    assert " | HALT | " in line
    assert "attribute price_list missing or empty on sensor.my_prices" in line
    alert = env.service.of("notify", NOTIFY)[0][2]["message"]
    assert "price_list" in alert and CUSTOM_PRICE in alert


def test_halt_cause_names_default_entity_when_attribute_missing(env):
    env.state.set(PRICE_ENTITY, "0.10", {"other": []})
    env.run()
    assert ("attribute prices missing or empty on %s" % PRICE_ENTITY
            in env.lines()[0])


def test_halt_cause_names_entity_when_unavailable(env):
    env.state.set(PRICE_ENTITY, "unavailable", {})
    env.run()
    assert "price entity %s unavailable" % PRICE_ENTITY in env.lines()[0]


# ---- own_grid_charge_kw (household draw in the grid-charge budget) ----------

OFFTAKE_ENTITY = "sensor.slimmelezer_power_consumed"


def _real_driver(env, name="fakeinv"):
    """A non-logging driver in its own dir; the adapter reads CORE_DIR."""
    d = env.tmp / "drivers"
    d.mkdir(exist_ok=True)
    (d / ("inverter_%s.py" % name)).write_text(
        "SOC_IS_STUB = False\n"
        "def send(action, target_power_kw):\n    return True\n"
        "def read_charge_percent():\n    return 40.0\n", encoding="utf-8")
    env.mod.CORE_DIR = str(d)
    env.write_config("inverter:\n  type: %s\n"
                     "capacity_tariff:\n  quarter_hour_average_mode: running\n"
                     "  stay_under_percent: 80\n" % name)


def _charge_at_budget(env):
    """Wrap rules.decide: remember the grid it saw, always charge at budget."""
    seen = []
    cap = env.mod.capacity
    rules = env.mod.rules
    real = rules.decide

    def fake(traj, price_map, bat, grid, cfg, now, **kw):
        d = real(traj, price_map, bat, grid, cfg, now, **kw)
        seen.append(grid)
        kwh = min(cap.budget_kw(grid, cfg), cfg.max_charge_kw)
        if kwh <= 0:
            return d
        from dataclasses import replace
        return replace(d, action="charge", target_power_kw=kwh,
                       selector="S1", reasoning="forced")
    rules.decide = fake
    return seen


def test_own_grid_charge_is_zero_with_logging_driver(env):
    seen = _charge_at_budget(env)
    env.write_config("capacity_tariff:\n  quarter_hour_average_mode: running\n")
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    env.run(T0 + STEP)
    assert len(seen) == 2
    assert seen[0].own_grid_charge_kw == 0.0
    assert seen[1].own_grid_charge_kw == 0.0


def test_own_grid_charge_equals_last_grid_charge_with_real_driver(env):
    _real_driver(env)
    seen = _charge_at_budget(env)
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    assert seen[0].own_grid_charge_kw == 0.0          # nothing commanded yet
    first_kw = env.mod._last_grid_charge[1]
    assert first_kw > 0
    env.run(T0 + STEP)
    assert seen[1].own_grid_charge_kw == pytest.approx(first_kw)


def test_own_grid_charge_expires_after_two_intervals(env):
    _real_driver(env)
    seen = _charge_at_budget(env)
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    env.run(T0 + timedelta(minutes=11))                 # > 2 x 5 min
    assert seen[1].own_grid_charge_kw == 0.0


def test_non_grid_decision_clears_own_grid_charge(env):
    _real_driver(env)
    _charge_at_budget(env)
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    assert env.mod._last_grid_charge is not None
    env.mod.rules.decide = env.mod.rules.decide.__closure__[0].cell_contents \
        if False else env.mod.rules.decide
    env.mod._remember_grid_charge(
        type("D", (), {"action": "idle", "target_power_kw": 0.0})(), T0)
    assert env.mod._last_grid_charge is None


def test_real_driver_budget_is_stable_across_cycles(env):
    # Constant household 1.5 kW. With a real driver the metered offtake is
    # household + the charge commanded last cycle; the budget must come out
    # exactly as if only the household were drawing (no every-other-cycle
    # oscillation), and the planner keeps charging every cycle.
    _real_driver(env)
    seen = _charge_at_budget(env)
    cap = env.mod.capacity
    house = 1.5
    powers = []
    for i in range(3):
        own = env.mod._last_grid_charge[1] if env.mod._last_grid_charge else 0.0
        env.state.set(OFFTAKE_ENTITY, str(house + own),
                      {"unit_of_measurement": "kW"})
        env.run(T0 + i * STEP)
        g = seen[-1]
        reference = cap.GridState(
            house, g.window_start, g.window_energy_kwh, g.elapsed_minutes,
            g.running_average_kw, g.month_peak_kw, g.is_restored)
        cfg = env.mod._config
        assert cap.budget_kw(g, cfg) == pytest.approx(
            cap.budget_kw(reference, cfg))
        powers.append(env.mod._last_grid_charge[1])
    assert all(p > 0 for p in powers)


def test_real_driver_without_own_correction_would_oscillate(env):
    # Documents the failure the correction prevents: household 1.5 kW only,
    # budget with the raw (charge-inclusive) offtake is strictly smaller.
    _real_driver(env)
    seen = _charge_at_budget(env)
    cap = env.mod.capacity
    env.state.set(OFFTAKE_ENTITY, "1.5", {"unit_of_measurement": "kW"})
    env.run(T0)
    first_kw = env.mod._last_grid_charge[1]
    env.state.set(OFFTAKE_ENTITY, str(1.5 + first_kw),
                  {"unit_of_measurement": "kW"})
    env.run(T0 + STEP)
    g = seen[-1]
    naive = cap.GridState(
        g.offtake_kw, g.window_start, g.window_energy_kwh, g.elapsed_minutes,
        g.running_average_kw, g.month_peak_kw, g.is_restored)
    cfg = env.mod._config
    assert cap.budget_kw(naive, cfg) < cap.budget_kw(g, cfg)


# ---- forecast sensor age ------------------------------------------------------------

def _forecast_stamped(env, **stamps):
    env.state.set(FORECAST_ENTITY, "12.3",
                  {FORECAST_ATTRIBUTE: forecast_payload()}, **stamps)


def _age_warnings(env):
    return [w for w in env.log.by_level["warning"] if "forecast sensor" in w]


def _degraded(env):
    return fields_of(env.decisions()[-1])["degraded"]


def test_fresh_forecast_stamp_adds_no_marker(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(minutes=20))
    env.run(T0)
    assert "forecast_age" not in _degraded(env)
    assert "solar_zero_fallback" not in _degraded(env)


def test_forecast_marker_threshold_is_inclusive_at_75(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(minutes=74))
    env.run(T0)
    assert "forecast_age" not in _degraded(env)
    _forecast_stamped(env, last_reported=T0 + STEP - timedelta(minutes=75))
    env.run(T0 + STEP)
    assert "forecast_age=1h15m" in _degraded(env)


def test_forecast_80_minutes_old_is_used_and_marked(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(minutes=80))
    env.run(T0)
    assert "forecast_age=1h20m" in _degraded(env)
    assert "solar_zero_fallback" not in _degraded(env)
    assert (env.cache_dir / "solar.json").exists()        # the payload was used
    assert env.service.of("homeassistant", "update_entity") == []
    assert _age_warnings(env) == []


def test_forecast_older_than_limit_counts_as_failed_zero_fallback(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(hours=3))
    env.run(T0)
    degraded = _degraded(env)
    assert "solar_zero_fallback" in degraded
    assert "forecast_age" not in degraded
    assert not (env.cache_dir / "solar.json").exists()    # old payload unused
    warnings = _age_warnings(env)
    assert len(warnings) == 1
    assert FORECAST_ENTITY in warnings[0] and "3h0m" in warnings[0]
    env.run(T0 + STEP)                                    # 2nd failure: nudge
    assert len(env.service.of("homeassistant", "update_entity")) == 1
    assert len(_age_warnings(env)) == 1          # rate limited
    env.run(T0 + 2 * STEP)                                # bounded spacing
    assert len(env.service.of("homeassistant", "update_entity")) == 1


def test_limit_is_exclusive_and_follows_config(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(minutes=120))
    env.run(T0)
    assert "forecast_age=2h0m" in _degraded(env)          # == limit: still used
    env.write_config("timing:\n  solar_cache_stale_minutes: 90\n")
    _forecast_stamped(env, last_reported=T0 + STEP - timedelta(minutes=91))
    env.run(T0 + STEP)
    assert "forecast_age" not in _degraded(env)


def test_stale_forecast_uses_cached_series_with_its_age(env):
    env.run(T0)                                           # builds the cache
    _forecast_stamped(env, last_reported=T0 + STEP - timedelta(hours=3))
    env.run(T0 + STEP)
    degraded = _degraded(env)
    assert "cache_age_solar=5m" in degraded
    assert "solar_zero_fallback" not in degraded
    assert "forecast_age" not in degraded


def test_old_payload_is_not_used_even_if_it_differs(env):
    env.run(T0)
    changed = forecast_payload()
    changed[next(iter(changed))] = 9000
    env.state.set(FORECAST_ENTITY, "12.3", {FORECAST_ATTRIBUTE: changed},
                  last_reported=T0 + STEP - timedelta(hours=3))
    env.run(T0 + STEP)
    data = (env.cache_dir / "solar.json").read_text()
    assert "9000" not in data and "9.0" not in data
    assert "cache_age_solar=5m" in _degraded(env)         # not rebuilt


def test_no_stamp_keeps_old_behaviour(env):
    env.run(T0)                                           # fixture: plain str
    assert "forecast_age" not in _degraded(env)
    assert _age_warnings(env) == []
    _forecast_stamped(env, last_reported=None, last_updated="garbage")
    env.run(T0 + STEP)
    assert "forecast_age" not in _degraded(env)


def test_last_reported_preferred_over_last_updated(env):
    _forecast_stamped(env, last_reported=T0 - timedelta(minutes=10),
                      last_updated=T0 - timedelta(hours=5))
    env.run(T0)
    assert "solar_zero_fallback" not in _degraded(env)
    assert "forecast_age" not in _degraded(env)


def test_last_updated_is_the_fallback(env):
    _forecast_stamped(env, last_updated=T0 - timedelta(minutes=90))
    env.run(T0)
    assert "forecast_age=1h30m" in _degraded(env)


def test_iso_and_naive_stamps(env):
    iso = (T0 - timedelta(minutes=80)).isoformat()
    _forecast_stamped(env, last_reported=iso)
    env.run(T0)
    assert "forecast_age=1h20m" in _degraded(env)
    naive = (T0 - timedelta(minutes=100)).replace(tzinfo=None)   # taken as UTC
    _forecast_stamped(env, last_reported=naive)
    env.run(T0 + STEP)
    assert "forecast_age=1h45m" in _degraded(env)


def test_future_stamp_counts_as_age_zero(env):
    _forecast_stamped(env, last_reported=T0 + timedelta(hours=1))
    env.run(T0)
    assert "forecast_age" not in _degraded(env)


def test_unavailable_sensor_path_unchanged_with_old_stamp(env):
    env.state.set(FORECAST_ENTITY, "unavailable", {},
                  last_reported=T0 - timedelta(hours=5))
    env.run(T0)
    assert "solar_zero_fallback" in _degraded(env)
    assert _age_warnings(env) == []              # plain failure, no age log
