"""Tests for pyscript/housekeeping.py (daily report job).

Loaded with stand-ins for the names pyscript provides at runtime, like
test_battery_planner does. Paths are redirected to tmp_path.
"""
import ast
import builtins
import importlib.util
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import decision
import history

SRC = Path(__file__).resolve().parent.parent / "pyscript" / "housekeeping.py"
MODULES = SRC.parent / "modules"
BRU = ZoneInfo("Europe/Brussels")
UTC = timezone.utc
# 00:10 local on 2026-09-30; "yesterday" is 2026-09-29
NOW = datetime(2026, 9, 30, 0, 10, tzinfo=BRU)
YESTERDAY = date(2026, 9, 29)
_CORE_NAMES = ("config", "history", "report", "retention")
_INJECTED = ("pyscript_executor", "time_trigger", "log")


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
    def __init__(self, mod, tmp):
        self.mod = mod
        self.tmp = tmp
        self.base = tmp / "battery_planner"
        self.hist = self.base / "history"
        self.log = builtins.log
        self.config_path = self.base / "user_config.yaml"

    def write_config(self, extra=""):
        self.base.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            "battery:\n  capacity_kwh: 10.0\nalerts:\n"
            "  address: owner@example.com\n" + extra, encoding="utf-8")
        bump = self.config_path.stat().st_mtime + 10
        os.utime(self.config_path, (bump, bump))

    def decisions(self, day, text="x"):
        self.base.mkdir(parents=True, exist_ok=True)
        path = self.base / ("decisions-%s.log" % day.isoformat())
        path.write_text(text, encoding="utf-8")
        return path

    def blocks(self, day, text="x"):
        self.hist.mkdir(parents=True, exist_ok=True)
        path = self.hist / ("blocks-%s.jsonl" % day.isoformat())
        path.write_text(text, encoding="utf-8")
        return path

    def report(self, day):
        return self.hist / ("report-%s.md" % day.isoformat())


def decision_line(when):
    return decision.format_record(decision.DecisionRecord(
        timestamp=when, action="idle", target_power_kw=0.0,
        charge_percent=50.0, charge_kwh=5.0, consumption_price=0.2,
        injection_price=0.1, forecast_remaining_kwh=3.0,
        usage_remaining_kwh=4.0, saturation_block=None, spill_kwh=0.0,
        reserve_breach_block=None, projected_end_charge_kwh=2.0,
        duration_ms=80, running_average_kw=1.2, ceiling_kw=2.5,
        budget_kw=1.0, vetoes_applied=[], selector="S6", reasoning="hold",
        degraded_inputs=[], source="planner"))


def block_line(start_local, imp=0.5):
    rec = dict.fromkeys(history.RECORD_FIELDS)
    rec.update(schema=1, block_start=start_local.astimezone(UTC).isoformat(),
               local_date=start_local.date().isoformat(), block_minutes=15,
               import_kwh=imp, complete=True)
    return history.to_line(rec)


def day_files(env, day, imp=0.5):
    at = datetime(day.year, day.month, day.day, 12, tzinfo=BRU)
    env.decisions(day, decision_line(at) + "\n")
    env.blocks(day, block_line(at, imp) + "\n")


@pytest.fixture
def env(tmp_path):
    saved = {n: getattr(builtins, n) for n in _INJECTED if hasattr(builtins, n)}
    saved_bare = {n: sys.modules.get(n) for n in _CORE_NAMES}
    builtins.pyscript_executor = lambda fn: fn
    builtins.time_trigger = lambda *a, **k: (lambda fn: fn)
    builtins.log = FakeLog()
    try:
        spec = importlib.util.spec_from_file_location(
            "housekeeping_under_test", SRC)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        e = Env(mod, tmp_path)
        mod.CORE_DIR = str(MODULES)
        mod.CONFIG_PATH = str(e.config_path)
        mod.LOG_DIR = str(e.base) + "/"
        mod.HISTORY_DIR = str(e.hist) + "/"
        e.write_config()
        yield e
    finally:
        for n in _INJECTED:
            if n in saved:
                setattr(builtins, n, saved[n])
            elif hasattr(builtins, n):
                delattr(builtins, n)
        for n in list(sys.modules):
            if n.startswith("housekeeping_core_"):
                del sys.modules[n]
        for n, previous in saved_bare.items():      # loader aliases are permanent
            if previous is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = previous


# ---- report job --------------------------------------------------------------

def test_writes_yesterdays_report(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert text.startswith("# Battery planner report for 2026-09-29")
    assert "- Records: 1" in text
    assert "| Imported from the grid | 0.50 | 1 |" in text
    assert env.log.by_level["error"] == [] and env.log.by_level["warning"] == []
    assert env.log.by_level["info"] == [
        "housekeeping: wrote daily report for 2026-09-29"]


def test_report_lives_next_to_the_history_and_leaves_no_temp_file(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert sorted(p.name for p in env.hist.iterdir()) == [
        "blocks-2026-09-29.jsonl", "report-2026-09-29.md"]


def test_only_decisions_file_is_enough(env):
    env.decisions(YESTERDAY, decision_line(NOW - timedelta(hours=9)) + "\n")
    env.mod.run_report(NOW)
    text = env.report(YESTERDAY).read_text(encoding="utf-8")
    assert "- No energy history for this day." in text


def test_nothing_is_written_when_neither_file_exists(env):
    env.mod.run_report(NOW)
    assert not env.hist.exists()
    assert env.log.by_level["error"] == []


def test_blank_files_write_nothing(env):
    env.decisions(YESTERDAY, "\n")
    env.blocks(YESTERDAY, "")
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()


def test_day_is_decided_in_the_configured_timezone(env):
    # 00:10 on the 30th in Brussels is 15:10 on the 29th in Los Angeles: there
    # the 29th is still today, so only the 28th is complete.
    env.write_config("timezone: America/Los_Angeles\n")
    day_files(env, date(2026, 9, 29))
    day_files(env, date(2026, 9, 28))
    env.mod.run_report(NOW)
    assert env.report(date(2026, 9, 28)).exists()
    assert not env.report(date(2026, 9, 29)).exists()


def test_today_is_never_reported(env):
    today = date(2026, 9, 30)
    day_files(env, today)
    env.mod.run_report(NOW)
    assert not env.report(today).exists()


def test_backfills_missing_days_of_the_last_week_and_only_those(env):
    for back in (1, 3, 5, 7, 8):
        day_files(env, YESTERDAY - timedelta(days=back - 1))
    done = YESTERDAY - timedelta(days=2)             # 3 days back: already done
    env.hist.mkdir(parents=True, exist_ok=True)
    env.report(done).write_text("keep me", encoding="utf-8")
    env.mod.run_report(NOW)
    written = sorted(p.name for p in env.hist.glob("report-*.md"))
    assert written == ["report-2026-09-23.md", "report-2026-09-25.md",
                       "report-2026-09-27.md", "report-2026-09-29.md"]
    # day 8 back (09-22) is outside the window; existing report untouched
    assert env.report(done).read_text(encoding="utf-8") == "keep me"
    assert not env.report(date(2026, 9, 22)).exists()


def test_never_overwrites_an_existing_report(env):
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    first = env.report(YESTERDAY).read_text(encoding="utf-8")
    env.blocks(YESTERDAY, block_line(datetime(2026, 9, 29, 12, tzinfo=BRU), 9.0))
    env.mod.run_report(NOW + timedelta(days=1, minutes=1))
    assert env.report(YESTERDAY).read_text(encoding="utf-8") == first


def test_write_helper_refuses_to_overwrite(env):
    target = env.hist / "report-2026-01-01.md"
    env.hist.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    assert env.mod._write_text_new(str(target), "new") == "exists"
    assert target.read_text(encoding="utf-8") == "old"


def test_write_is_atomic_a_failed_replace_leaves_no_report(env, monkeypatch):
    day_files(env, YESTERDAY)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()       # never a partial file
    assert any("not written" in m and "disk full" in m
               for m in env.log.by_level["warning"])
    assert env.log.by_level["error"] == []


def test_report_disabled_writes_nothing(env):
    env.write_config("report:\n  enabled: false\n")
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()


def test_broken_config_logs_and_does_not_raise(env):
    env.config_path.write_text("battery: [", encoding="utf-8")
    bump = env.config_path.stat().st_mtime + 20
    os.utime(env.config_path, (bump, bump))
    day_files(env, YESTERDAY)
    env.mod.run_report(NOW)
    assert not env.report(YESTERDAY).exists()
    assert any("config unusable" in m for m in env.log.by_level["error"])


def test_missing_config_logs_and_does_not_raise(env):
    env.config_path.unlink()
    env.mod.run_report(NOW)
    assert any("config unusable" in m for m in env.log.by_level["error"])


def test_a_failing_day_does_not_stop_the_other_days(env, monkeypatch):
    day_files(env, YESTERDAY)
    day_files(env, YESTERDAY - timedelta(days=1))
    real = env.mod._read_text

    def flaky(path):
        if "2026-09-28" in path:
            raise RuntimeError("nas hiccup")
        return real(path)
    monkeypatch.setattr(env.mod, "_read_text", flaky)
    env.mod.run_report(NOW)
    assert env.report(YESTERDAY).exists()
    assert not env.report(YESTERDAY - timedelta(days=1)).exists()
    assert any("2026-09-28" in m and "failed" in m
               for m in env.log.by_level["warning"])


def test_unexpected_failure_is_logged_never_raised(env, monkeypatch):
    monkeypatch.setattr(env.mod, "_list_names",
                        lambda d: (_ for _ in ()).throw(RuntimeError("x")))
    env.mod.run_report(NOW)
    assert any("daily report failed" in m for m in env.log.by_level["error"])


def test_symlinked_report_dir_entries_are_not_listed(env, tmp_path):
    env.hist.mkdir(parents=True)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("x", encoding="utf-8")
    (env.hist / "blocks-2026-09-29.jsonl").symlink_to(target)
    assert env.mod._list_names(str(env.hist)) == []


def test_trigger_function_is_registered_at_00_10(env):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    crons = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call)
                        and getattr(dec.func, "id", "") == "time_trigger"):
                    crons[node.name] = dec.args[0].value
    assert crons["housekeeping_report"] == "cron(10 0 * * *)"


# ---- native loader ------------------------------------------------------------

def test_ensure_core_binds_modules_and_aliases_bare_names(env):
    mod = env.mod
    for n in mod.CORE_MODULES:
        sys.modules.pop(n, None)
    mod._ensure_core()
    assert mod.CORE_MODULES == ("config", "history", "report")
    assert mod.report.__name__ == "housekeeping_core_report"
    assert sys.modules["report"] is mod.report
    assert sys.modules["history"] is mod.history
    assert "inverter" not in mod.CORE_MODULES


def test_loader_reuses_a_module_already_loaded_from_the_same_file(env):
    mod = env.mod
    first = mod._load_core(str(MODULES), ("config",))
    again = mod._load_core(str(MODULES), ("config",))
    assert again["config"] is first["config"]


def test_failed_load_undoes_the_bare_aliases(env, tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("raise RuntimeError('broken')\n")
    (tmp_path / "history.py").write_text("X = 1\n")
    monkeypatch.delitem(sys.modules, "housekeeping_core_config", raising=False)
    before = sys.modules.get("history")
    with pytest.raises(RuntimeError):
        env.mod._load_core(str(tmp_path), ("history", "config"))
    assert sys.modules.get("history") is before
    assert "housekeeping_core_config" not in sys.modules
