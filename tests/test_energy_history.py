"""Adapter tests for the energy-history recorder and read_usage_history.

Uses the `env` fixture of test_battery_planner (fakes for state/log/service,
paths redirected to tmp_path, mutable clock).
"""
import ast
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_battery_planner import (  # noqa: F401  (env is a fixture)
    FORECAST_ENTITY, SRC, STEP, T0, env, fields_of)

UTC = timezone.utc
MODULES = SRC.parent / "modules"
IMP = ["sensor.slimmelezer_energy_consumed_tariff_1",
       "sensor.slimmelezer_energy_consumed_tariff_2"]
EXP = ["sensor.slimmelezer_energy_produced_tariff_1",
       "sensor.slimmelezer_energy_produced_tariff_2"]
SOLAR = "sensor.pv_energy_total"
CHG = "sensor.battery_charge_total"
DIS = "sensor.battery_discharge_total"
QUARTER = timedelta(minutes=15)
B = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)     # boundary at/before T0


def counters(env, **values):
    """Set cumulative counters: name -> kWh ('unavailable' allowed)."""
    names = {"i1": IMP[0], "i2": IMP[1], "e1": EXP[0], "e2": EXP[1],
             "solar": SOLAR, "chg": CHG, "dis": DIS, "load": "sensor.load_total"}
    for key, v in values.items():
        env.state.set(names[key], v, {"unit_of_measurement": "kWh"})


def hist_dir(env):
    return env.tmp / "history"


def records(env):
    out = []
    for f in sorted(hist_dir(env).glob("blocks-*.jsonl")):
        out += [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()]
    return out


def warnings(env):
    return [m for m in env.log.by_level["warning"] if "energy history" in m]


FULL = ("history:\n  sensors:\n    solar: [%s]\n    battery_charge: [%s]\n"
        "    battery_discharge: [%s]\n" % (SOLAR, CHG, DIS))


def set_all(env, step):
    """Counters grow by `step` units each call: import 1, export .1, solar 2..."""
    counters(env, i1=10 + step, i2=5 + step, e1=1 + step * 0.1, e2=0.5,
             solar=100 + step * 2, chg=20 + step * 0.5, dis=15 + step * 0.25)


def cycles(env, n, start=T0):
    """Run n cycles one per block, counters stepping each time."""
    for k in range(n):
        set_all(env, k)
        env.run(start + k * QUARTER)


# ---- recording ---------------------------------------------------------------

def test_boundary_crossing_writes_one_record_with_all_fields(env):
    env.write_config(FULL)
    set_all(env, 0)
    env.run(T0)                                   # first snapshot, no record
    assert records(env) == []
    set_all(env, 1)
    env.run(T0 + QUARTER + timedelta(minutes=0))  # 12:50 -> boundary 12:45
    (r,) = records(env)
    assert r["block_start"] == B.isoformat() and r["local_date"] == "2026-09-30"
    assert r["import_kwh"] == pytest.approx(2.0)          # two tariffs, +1 each
    assert r["export_kwh"] == pytest.approx(0.1)
    assert r["solar_kwh"] == pytest.approx(2.0)
    assert r["battery_charge_kwh"] == pytest.approx(0.5)
    assert r["battery_discharge_kwh"] == pytest.approx(0.25)
    assert r["load_kwh"] == pytest.approx(2.0 - 0.1 + 2.0 + 0.25 - 0.5)
    assert r["load_source"] == "derived" and r["complete"] is True
    assert r["from_net_kwh"] == r["import_kwh"]
    assert r["consumption_price"] is not None and r["injection_price"] is not None
    assert isinstance(r["forecast_solar_kwh"], float)
    assert r["soc_percent"] is None                       # SOC is the stub
    assert r["start_read_at"] == T0.isoformat()
    assert r["snapshot_read_at"] == (T0 + QUARTER).isoformat()
    assert env.decisions()                                # decisions unaffected


SOC = "sensor.alphaess_soc_battery"


def soc_config(env):
    env.write_config(FULL)
    nl = chr(10)
    text = env.config_path.read_text(encoding="utf-8").replace(
        "battery:" + nl, "battery:" + nl + "  soc_sensor: %s" % SOC + nl, 1)
    env.config_path.write_text(text, encoding="utf-8")


def set_soc(env, value):
    env.state.set(SOC, value, {"unit_of_measurement": "%"})


def test_end_charge_is_the_reading_of_the_closing_snapshot(env):
    soc_config(env)
    set_all(env, 0)
    set_soc(env, "40.0")
    env.run(T0)
    set_all(env, 1)
    set_soc(env, "55.0")
    env.run(T0 + QUARTER)
    set_all(env, 2)
    set_soc(env, "61.0")
    env.run(T0 + 2 * QUARTER)
    r1, r2 = records(env)
    assert (r1["soc_percent"], r1["soc_end_percent"]) == (40.0, 55.0)
    assert (r2["soc_percent"], r2["soc_end_percent"]) == (55.0, 61.0)


def test_end_charge_null_when_the_sensor_is_unavailable_at_the_end(env):
    soc_config(env)
    set_all(env, 0)
    set_soc(env, "40.0")
    env.run(T0)
    set_all(env, 1)
    set_soc(env, "unavailable")
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["soc_percent"] == 40.0 and r["soc_end_percent"] is None


def test_end_charge_null_without_a_charge_sensor(env):
    env.write_config(FULL)
    cycles(env, 2)
    (r,) = records(env)
    assert r["soc_percent"] is None and r["soc_end_percent"] is None


def test_end_charge_survives_a_restart(env):
    soc_config(env)
    set_all(env, 0)
    set_soc(env, "40.0")
    env.run(T0)
    assert json.loads((hist_dir(env) / "last_snapshot.json").read_text()
                      )["soc_percent"] == 40.0
    restart(env)
    set_all(env, 1)
    set_soc(env, "52.5")
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["soc_percent"] == 40.0 and r["soc_end_percent"] == 52.5


def test_restart_gap_gives_null_charges(env):
    soc_config(env)
    set_all(env, 0)
    set_soc(env, "40.0")
    env.run(T0)
    restart(env)
    set_all(env, 5)
    set_soc(env, "70.0")
    env.run(T0 + 3 * QUARTER)
    out = records(env)
    assert len(out) == 3
    assert all(r["soc_percent"] is None and r["soc_end_percent"] is None
               for r in out)


def test_no_duplicate_record_on_repeated_cycles_in_a_block(env):
    env.write_config(FULL)
    set_all(env, 0)
    env.run(T0)
    set_all(env, 1)
    for k in range(3, 6):                         # 12:50, 12:55, 13:00 ... 3 per
        env.run(T0 + k * STEP)                    # block at 5 min interval
    assert len(records(env)) == 2                 # 12:30 and 12:45 blocks
    first_read = (T0 + 3 * STEP).isoformat()      # later cycles do not re-read
    assert records(env)[1]["start_read_at"] == first_read
    env.run(T0 + 5 * STEP)                        # same minute again: gated
    assert len(records(env)) == 2


def test_default_import_export_recorded_without_load(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    counters(env, i1=1.5, i2=1.25, e1=0.0, e2=0.25)
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] == pytest.approx(0.75)
    assert r["export_kwh"] == pytest.approx(0.25)
    assert r["load_kwh"] is None and r["solar_kwh"] is None
    assert r["complete"] is True


def test_wh_and_mwh_units_are_converted(env):
    env.write_config("history:\n  sensors:\n    import: [sensor.a, sensor.b]\n"
                     "    export: []\n")
    env.state.set("sensor.a", "1000", {"unit_of_measurement": "Wh"})
    env.state.set("sensor.b", "0.001", {"unit_of_measurement": "MWh"})
    env.run(T0)
    env.state.set("sensor.a", "3000", {"unit_of_measurement": "Wh"})
    env.state.set("sensor.b", "0.002", {"unit_of_measurement": "MWh"})
    env.run(T0 + QUARTER)
    assert records(env)[0]["import_kwh"] == pytest.approx(3.0)


def test_counter_without_unit_is_taken_as_kwh(env):
    env.write_config("history:\n  sensors:\n    import: [sensor.a]\n"
                     "    export: []\n")
    env.state.set("sensor.a", "1")
    env.run(T0)
    env.state.set("sensor.a", "1.5")
    env.run(T0 + QUARTER)
    assert records(env)[0]["import_kwh"] == 0.5


def test_unknown_unit_is_unreadable(env):
    env.write_config("history:\n  sensors:\n    import: [sensor.a]\n"
                     "    export: []\n")
    env.state.set("sensor.a", "5", {"unit_of_measurement": "W"})
    env.run(T0)
    env.run(T0 + QUARTER)
    assert records(env)[0]["import_kwh"] is None


def test_sensor_unavailable_gives_nulls_incomplete_and_one_warning(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    counters(env, i1="unavailable", i2=1.5, e1=0.5, e2=0.5)
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] is None and r["from_net_kwh"] is None
    assert r["export_kwh"] == pytest.approx(1.0)
    assert r["complete"] is False
    assert len(warnings(env)) >= 1
    n = len(warnings(env))
    counters(env, i1="unavailable", i2=1.5, e1=0.5, e2=0.5)
    env.run(T0 + 2 * QUARTER)
    assert len(warnings(env)) == n                        # rate-limited


def test_a_counter_bouncing_to_zero_gives_nulls_not_a_huge_delta(env):
    counters(env, i1=500, i2=500, e1=5, e2=5)
    env.run(T0)
    counters(env, i1=0, i2=500, e1=5, e2=5)          # one tariff counter drops to 0
    env.run(T0 + QUARTER)
    counters(env, i1=500, i2=500, e1=5, e2=5)        # and recovers
    env.run(T0 + 2 * QUARTER)
    counters(env, i1=500.5, i2=500, e1=5, e2=5)
    env.run(T0 + 3 * QUARTER)
    first, second, third = records(env)
    assert first["import_kwh"] is None and second["import_kwh"] is None
    assert third["import_kwh"] == pytest.approx(0.5)
    assert max(r["import_kwh"] or 0 for r in records(env)) < 1.0
    assert len(warnings(env)) >= 1


def test_the_zero_check_survives_a_restart(env):
    counters(env, i1=500, i2=500, e1=5, e2=5)
    env.run(T0)
    restart(env)
    counters(env, i1=0, i2=0, e1=5, e2=5)
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] is None


def test_missing_entity_is_unreadable_not_an_error(env):
    env.run(T0)                                           # no counters at all
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] is None and r["complete"] is False
    assert env.log.by_level["error"] == []


def test_forecast_null_when_forecast_missing(env):
    env.state.set(FORECAST_ENTITY, "unavailable", {})
    env.write_config("")
    env.run(T0)
    env.run(T0 + QUARTER)
    assert records(env)[0]["forecast_solar_kwh"] is None


def test_disabled_writes_nothing(env):
    env.write_config("history:\n  enabled: false\n")
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    env.run(T0 + QUARTER)
    assert not hist_dir(env).exists()
    assert env.decisions()


def test_all_sensor_lists_empty_writes_nothing(env):
    env.write_config("history:\n  sensors:\n    import: []\n    export: []\n")
    env.run(T0)
    env.run(T0 + QUARTER)
    assert not hist_dir(env).exists()


def test_records_continue_during_price_halt(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    env.state.set("sensor.entso_prices_average_electricity_price",
                  "unavailable", {})
    counters(env, i1=2, i2=1, e1=0, e2=0)
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] == pytest.approx(1.0)
    assert isinstance(r["consumption_price"], float)   # known at block start


# ---- persistence and restart ---------------------------------------------------

def restart(env):
    env.mod._hist_last = None
    env.mod._hist_loaded = False


def test_snapshot_persisted_and_restart_continues_the_block(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    snap_file = hist_dir(env) / "last_snapshot.json"
    assert snap_file.exists()
    assert json.loads(snap_file.read_text())["boundary"] == B.isoformat()
    restart(env)                                          # HA restart
    counters(env, i1=2, i2=1, e1=0, e2=0)
    env.run(T0 + QUARTER)
    (r,) = records(env)
    assert r["import_kwh"] == pytest.approx(1.0) and r["complete"] is True


def test_restart_with_a_gap_yields_null_blocks_not_a_spread_delta(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    restart(env)
    counters(env, i1=9, i2=1, e1=0, e2=0)
    env.run(T0 + 4 * QUARTER)
    out = records(env)
    assert len(out) == 4
    assert all(r["import_kwh"] is None and r["complete"] is False for r in out)


def test_corrupt_snapshot_file_means_no_record_first_time(env):
    hist_dir(env).mkdir()
    (hist_dir(env) / "last_snapshot.json").write_text("{nope")
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    assert records(env) == []
    assert env.decisions()


# ---- failures never reach decisions --------------------------------------------

def test_write_failure_does_not_affect_decision_and_is_retried(env, monkeypatch):
    env.write_config("timing:\n  evaluation_interval_minutes: 1\n")
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    real = env.mod._append_text
    monkeypatch.setattr(env.mod, "_append_text", lambda p, t: "OSError('disk')")
    counters(env, i1=2, i2=1, e1=0, e2=0)
    env.run(T0 + QUARTER)
    assert len(env.decisions()) == 2                      # decision still made
    assert records(env) == []
    assert len(warnings(env)) == 1 and "disk" in warnings(env)[0]
    env.run(T0 + QUARTER + timedelta(minutes=1))
    assert len(warnings(env)) == 1                        # rate-limited
    monkeypatch.setattr(env.mod, "_append_text", real)
    env.run(T0 + QUARTER + timedelta(minutes=2))          # retry, same boundary
    (r,) = records(env)
    assert r["import_kwh"] == pytest.approx(1.0)


def test_recorder_exception_is_contained(env, monkeypatch):
    def boom(entity):
        raise RuntimeError("kaput")
    monkeypatch.setattr(env.mod, "_counter_kwh", boom)
    env.run(T0)
    assert len(env.decisions()) == 1
    assert env.log.by_level["error"] == []
    assert any("recorder failed" in m for m in warnings(env))


def test_snapshot_save_failure_is_contained(env, monkeypatch):
    real = env.mod._write_json_atomic
    monkeypatch.setattr(env.mod, "_write_json_atomic",
                        lambda p, x: "EIO" if "last_snapshot" in p else real(p, x))
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    assert len(env.decisions()) == 1
    assert any("last_snapshot" in m for m in warnings(env))


# ---- file rotation ---------------------------------------------------------------

def test_files_rotate_by_local_date_of_block_start(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    t = datetime(2026, 9, 30, 21, 40, tzinfo=UTC)         # 23:40 local
    for k in range(4):                                    # 21:40 .. 22:25 UTC
        counters(env, i1=1 + k, i2=1, e1=0, e2=0)
        env.run(t + k * QUARTER)
    names = sorted(p.name for p in hist_dir(env).glob("blocks-*.jsonl"))
    assert names == ["blocks-2026-09-30.jsonl", "blocks-2026-10-01.jsonl"]
    by_file = {p.name: [json.loads(l) for l in p.read_text().splitlines()]
               for p in hist_dir(env).glob("blocks-*.jsonl")}
    assert [r["block_start"] for r in by_file["blocks-2026-09-30.jsonl"]] == [
        "2026-09-30T21:30:00+00:00", "2026-09-30T21:45:00+00:00"]
    assert [r["local_date"] for r in by_file["blocks-2026-10-01.jsonl"]] == [
        "2026-10-01"]
    assert by_file["blocks-2026-10-01.jsonl"][0]["block_start"] \
        == "2026-09-30T22:00:00+00:00"


# ---- read_usage_history -------------------------------------------------------------

def test_read_usage_history_default_config_is_empty(env):
    counters(env, i1=1, i2=1, e1=0, e2=0)
    env.run(T0)
    counters(env, i1=2, i2=1, e1=0, e2=0)
    env.run(T0 + QUARTER)
    assert records(env)                                   # import/export recorded
    cfg = env.mod._load_config()
    assert env.mod.read_usage_history(cfg, T0 + QUARTER) == []
    env.run(T0 + 2 * QUARTER)
    assert "usage_history_unavailable" in fields_of(env.decisions()[-1])["degraded"]


def test_read_usage_history_returns_derived_load_for_configured_solar(env):
    env.write_config(FULL)
    cycles(env, 4)
    cfg = env.mod._load_config()
    out = env.mod.read_usage_history(cfg, T0 + 4 * QUARTER)
    assert len(out) == 3
    assert out[0][0] == B and out[0][0].tzinfo is not None
    assert [t for t, _ in out] == sorted(t for t, _ in out)
    assert out[0][1] == pytest.approx(2.0 - 0.1 + 2.0 + 0.25 - 0.5)


def test_solar_without_battery_counters_does_not_unlock_usage(env):
    env.write_config("history:\n  sensors:\n    solar: [%s]\n" % SOLAR)
    counters(env, i1=1, i2=1, e1=0, e2=0, solar=0)
    env.run(T0)
    counters(env, i1=2, i2=1, e1=0, e2=0, solar=1)
    env.run(T0 + QUARTER)
    assert records(env)[0]["solar_kwh"] == 1.0
    assert env.mod.read_usage_history(env.mod._load_config(), T0 + QUARTER) == []


def test_measured_load_counter_gives_usage_history(env):
    env.write_config("history:\n  sensors:\n    load: [sensor.load_total]\n")
    counters(env, load=10)
    env.run(T0)
    counters(env, load=10.4)
    env.run(T0 + QUARTER)
    out = env.mod.read_usage_history(env.mod._load_config(), T0 + QUARTER)
    assert out == [(B, pytest.approx(0.4))]


def test_usage_history_feeds_the_profile_and_clears_the_marker(env):
    env.write_config("history:\n  sensors:\n    load: [sensor.load_total]\n")
    counters(env, load=10)
    env.run(T0)
    counters(env, load=10.4)
    env.run(T0 + QUARTER)
    env.run(T0 + QUARTER + STEP)                  # profile is built next cycle
    degraded = fields_of(env.decisions()[-1])["degraded"]
    assert "usage_history_unavailable" not in degraded
    assert (env.cache_dir / "usage.json").exists()


def test_read_usage_history_skips_corrupt_lines_and_warns(env):
    env.write_config("history:\n  sensors:\n    load: [sensor.load_total]\n")
    counters(env, load=10)
    env.run(T0)
    counters(env, load=10.4)
    env.run(T0 + QUARTER)
    day = hist_dir(env) / "blocks-2026-09-30.jsonl"
    day.write_text("{broken\n" + day.read_text() + "[1]\n", encoding="utf-8")
    out = env.mod.read_usage_history(env.mod._load_config(), T0 + QUARTER)
    assert len(out) == 1
    assert any("2 corrupt" in m for m in warnings(env))


def test_read_usage_history_only_reads_the_configured_window(env):
    env.write_config("usage:\n  history_weeks: 1\n")
    hist_dir(env).mkdir()
    old = {"block_start": "2026-09-01T10:00:00+00:00", "load_kwh": 5.0}
    new = {"block_start": "2026-09-29T10:00:00+00:00", "load_kwh": 0.5}
    (hist_dir(env) / "blocks-2026-09-01.jsonl").write_text(json.dumps(old) + "\n")
    (hist_dir(env) / "blocks-2026-09-29.jsonl").write_text(json.dumps(new) + "\n")
    out = env.mod.read_usage_history(env.mod._load_config(), T0)
    assert [kwh for _, kwh in out] == [0.5]


# ---- hygiene ------------------------------------------------------------------------

def test_history_module_is_pure_stdlib_no_io():
    tree = ast.parse((MODULES / "history.py").read_text(encoding="utf-8"))
    allowed = {"json", "math", "dataclasses", "datetime"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name.split(".")[0] for a in node.names} <= allowed
        if isinstance(node, ast.ImportFrom):
            assert node.module.split(".")[0] in allowed
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("open", "print", "input")
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("now", "utcnow", "today")


def test_history_is_a_registered_core_module(env):
    assert "history" in env.mod.CORE_MODULES
    assert env.mod.history.__name__ == "battery_planner_core_history"

