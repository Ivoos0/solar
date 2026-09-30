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

CONTRACT_FIELDS = [
    "action", "power", "soc", "cons", "inj", "solar_rem", "usage_rem",
    "saturation", "spill", "breach", "end_soc", "took", "avg", "ceiling",
    "budget", "vetoes", "selector", "why", "degraded", "source",
]

_CORE_NAMES = ("config", "prices", "series", "battery", "trajectory",
               "capacity", "rules", "decision", "cache")
_INJECTED = ("pyscript_executor", "time_trigger", "task_unique", "state",
             "service", "log", "task")


class FakeState:
    def __init__(self):
        self.data = {}

    def set(self, entity, value=None, attrs=None):
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
        self.log_path = tmp / "decisions.log"
        self.cache_dir = tmp / "cache"

    def run(self, now=None):
        if now is not None:
            self.clock = now
        self.mod.run_cycle(self.clock)

    def lines(self):
        if not self.log_path.exists():
            return []
        return self.log_path.read_text(encoding="utf-8").splitlines()

    def decisions(self):
        return [l for l in self.lines() if " | action=" in l]

    def write_config(self, extra=""):
        self.config_path.write_text(
            "battery:\n  capacity_kwh: 10.0\nalerts:\n"
            "  address: owner@example.com\n"
            "  notify_service: test_notifier\n" + extra, encoding="utf-8")
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
    try:
        spec = importlib.util.spec_from_file_location(
            "battery_planner_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        e = Env(mod, tmp_path)
        mod.CORE_DIR = str(MODULES)
        mod._ensure_core()
        mod.CONFIG_PATH = str(e.config_path)
        mod.CACHE_DIR = str(e.cache_dir) + "/"
        mod.DECISIONS_LOG_PATH = str(e.log_path)
        mod._now = lambda: e.clock
        e.write_config()
        st = e.state
        st.set(mod.PRICE_ENTITY, "0.10", {mod.PRICE_ATTRIBUTE: price_entries()})
        st.set(mod.FORECAST_ENTITY, "12.3",
               {mod.FORECAST_ATTRIBUTE: forecast_payload()})
        st.set("sensor.slimmelezer_power_consumed", "0.8",
               {"unit_of_measurement": "kW"})
        st.set(mod.QUARTER_AVG_ENTITY, "0.7", {"unit_of_measurement": "kW"})
        st.set(mod.MONTH_PEAK_ENTITY, "3.0", {"unit_of_measurement": "kW"})
        yield e
    finally:
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
    st.set(mod.PRICE_ENTITY, "unavailable", {})
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
    st.set(mod.PRICE_ENTITY, "0.10", {mod.PRICE_ATTRIBUTE: price_entries()})
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
    env.state.set(env.mod.PRICE_ENTITY, "0.10", attrs)
    env.run()
    assert env.decisions() == []
    assert " | HALT | " in env.lines()[0]


def test_halt_line_carries_alert_time_and_no_decision_logic(env):
    env.state.set(env.mod.PRICE_ENTITY, "unknown", {})
    env.run()
    parts = env.lines()[0].split(" | ")
    assert parts[1] == "HALT" and parts[2].startswith("cause=")
    assert parts[4] != "alerted=none"


# ---- forecast outage: degrade, not halt --------------------------------------------

def test_forecast_outage_degrades_to_zero_solar_without_halt(env):
    env.state.set(env.mod.FORECAST_ENTITY, "unavailable", {})
    env.run()
    lines = env.decisions()
    assert len(lines) == 1
    assert "solar_zero_fallback" in fields_of(lines[0])["degraded"]
    assert not any("HALT" in l for l in env.lines())
    assert env.service.of("notify", NOTIFY) == []
    assert not (env.cache_dir / "solar.json").exists()    # fallback not cached


def test_forecast_retry_is_spaced_and_counted(env):
    mod = env.mod
    env.state.set(mod.FORECAST_ENTITY, "unavailable", {})
    env.run(T0)
    assert env.service.of("homeassistant", "update_entity") == []   # threshold
    env.run(T0 + STEP)
    assert len(env.service.of("homeassistant", "update_entity")) == 1
    assert env.service.of("homeassistant", "update_entity")[0][2] == {
        "entity_id": mod.FORECAST_ENTITY}
    env.run(T0 + 2 * STEP)                                # only 5 min later
    assert len(env.service.of("homeassistant", "update_entity")) == 1
    env.run(T0 + 3 * STEP)                                # 10 min later
    assert len(env.service.of("homeassistant", "update_entity")) == 2


def test_forecast_retries_stay_within_hourly_budget(env):
    env.write_config("timing:\n  evaluation_interval_minutes: 1\n"
                     "  forecast_retry_minutes: 1\n")
    env.state.set(env.mod.FORECAST_ENTITY, "unavailable", {})
    for i in range(60):
        env.run(T0 + timedelta(minutes=i))
    assert len(env.service.of("homeassistant", "update_entity")) <= 11


def test_forecast_down_with_fresh_solar_cache_states_its_age(env):
    env.run(T0)
    env.state.set(env.mod.FORECAST_ENTITY, "unavailable", {})
    env.run(T0 + STEP)
    degraded = fields_of(env.decisions()[-1])["degraded"]
    assert "cache_age_solar=5m" in degraded               # FR-027 / SC-014
    assert "solar_zero_fallback" not in degraded


def test_fresh_cache_hit_adds_age_marker_rebuilt_series_does_not(env):
    env.run(T0)                                           # miss: rebuilt
    assert "cache_age" not in fields_of(env.decisions()[-1])["degraded"]
    env.run(T0 + STEP)                                    # fresh hit
    assert "cache_age_solar=5m" in fields_of(env.decisions()[-1])["degraded"]
    payload = forecast_payload()
    payload[next(iter(payload))] = 900                    # new forecast: rebuilt
    env.state.set(env.mod.FORECAST_ENTITY, "12.3",
                  {env.mod.FORECAST_ATTRIBUTE: payload})
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
    env.state.set(env.mod.FORECAST_ENTITY, "unavailable", {})
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
        assert before[key] == after[key]                   # SC-013


def test_new_forecast_rebuilds_solar(env, monkeypatch):
    calls = []
    real = env.mod.series.solar_series
    monkeypatch.setattr(env.mod.series, "solar_series",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    env.run(T0)
    payload = forecast_payload()
    first = next(iter(payload))
    payload[first] = 900
    env.state.set(env.mod.FORECAST_ENTITY, "12.3",
                  {env.mod.FORECAST_ATTRIBUTE: payload})
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
    env.state.set(env.mod.PRICE_ENTITY, "0.1", {
        env.mod.PRICE_ATTRIBUTE: price_entries(days=3)})
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
            "charge", 3.0, "S1", "cheapest block", [], [], "grid",
            d.block_start)

    monkeypatch.setattr(mod.rules, "decide", decide)


def test_grid_charge_passes_when_guard_is_off(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(env.mod.GUARD_FLAG_ENTITY, "off", {})
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "charge"


def test_grid_charge_downgraded_while_guard_shaving(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(env.mod.GUARD_FLAG_ENTITY, "on", {})
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "idle" and keys["power"] == "0.00kW"
    assert keys["selector"] == "S6"
    assert "GUARD(suppressed S1 charge)" in keys["vetoes"]
    assert "peak guard is shaving" in keys["why"]


def test_solar_charge_is_not_downgraded_by_guard(env, monkeypatch):
    mod = env.mod
    real = mod.rules.decide
    monkeypatch.setattr(
        mod.rules, "decide",
        lambda *a, **kw: mod.rules.Decision(
            "charge", 2.0, "S2", "solar surplus", [], [], "solar",
            real(*a, **kw).block_start))
    env.state.set(mod.GUARD_FLAG_ENTITY, "on", {})
    env.run()
    assert fields_of(env.decisions()[0])["action"] == "charge"


def test_grid_charge_downgraded_when_grid_sensors_unreadable(env, monkeypatch):
    _force_grid_charge(env, monkeypatch)
    env.state.set(env.mod.QUARTER_AVG_ENTITY, "unavailable", {})
    env.run()
    keys = fields_of(env.decisions()[0])
    assert keys["action"] == "idle"
    assert "NOGRID" in keys["vetoes"]
    assert "grid_sensors_unavailable" in keys["degraded"]


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
stub.DEFAULT_LOG_PATH = str(tmp / "decisions.log")
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
mod.DECISIONS_LOG_PATH = str(tmp / "decisions.log")
mod._now = lambda: T0
start = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)
mod_state = builtins.state
mod_state.d[mod.PRICE_ENTITY] = ("0.10", {mod.PRICE_ATTRIBUTE: [
    {"time": (start + timedelta(hours=h)).isoformat(), "price": 0.08 + 0.02 * (h % 6)}
    for h in range(48)]})
mod_state.d[mod.FORECAST_ENTITY] = ("1", {mod.FORECAST_ATTRIBUTE: {
    (start + timedelta(hours=h)).isoformat(): 600 for h in range(8, 20)}})
for ent in ("sensor.slimmelezer_power_consumed", mod.QUARTER_AVG_ENTITY,
            mod.MONTH_PEAK_ENTITY):
    mod_state.d[ent] = ("0.8", {"unit_of_measurement": "kW"})
mod.run_cycle(T0)
assert not Log.errors, Log.errors
lines = (tmp / "decisions.log").read_text().splitlines()
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

_INTERPRETED = [SRC, MODULES / "inverter.py"]
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
    env.state.set(mod.PRICE_ENTITY, "unavailable", {})
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
    env.state.set(mod.PRICE_ENTITY, "0.10",
                  {mod.PRICE_ATTRIBUTE: price_entries(days=2, start=start)})
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
    env.state.set(mod.PRICE_ENTITY, "-0.20", {mod.PRICE_ATTRIBUTE: entries})


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
    # S1 grid charge is vetoed; the planner falls through to S2, which
    # charges from SOLAR (unaffected by V4).
    assert keys["selector"] == "S2" and keys["action"] == "charge"
    assert "V4(suppressed S1 charge)" in keys["vetoes"]
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
        "  address: owner@example.com\n", encoding="utf-8")
    bump = env.config_path.stat().st_mtime + 20
    os.utime(env.config_path, (bump, bump))
    env.state.set(mod.PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    assert len(env.service.of("notify", "gmail_alert")) == 1
    assert env.service.of("notify", NOTIFY) == []


def test_alert_goes_to_the_configured_service_name(env):
    mod = env.mod
    env.write_config()
    env.state.set(mod.PRICE_ENTITY, "unavailable", {})
    env.run(T0)
    calls = env.service.of("notify", NOTIFY)
    assert len(calls) == 1 and calls[0][2]["target"] == ["owner@example.com"]
