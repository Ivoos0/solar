"""Solar calibration in the planner cycle: the ratio is applied on top of the RAW
forecast series (cache and history keep the raw numbers), shows up in the
record and the sensor, and the history files are read at most once an hour.

Uses the `env` fixture of test_battery_planner (clock T0 = 14:35 Brussels).
"""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from test_battery_planner import (  # noqa: F401  (env is a fixture)
    FORECAST_ATTRIBUTE, FORECAST_ENTITY, STEP, T0, env, fields_of,
    forecast_payload)

UTC = timezone.utc
BRU = ZoneInfo("Europe/Brussels")
RATIO = "sensor.battery_planner_solar_ratio"
CAL = "solar:\n  calibration_default: %s\n  calibration_weeks: %s\n"
EVERY_MINUTE = "timing:\n  evaluation_interval_minutes: 1\n"
BLOCK = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)         # block of T0 (14:30 local)


def configure(env, default=0.9, weeks=4, extra=""):
    env.write_config(CAL % (default, weeks) + extra)


def write_history(env, weeks, ratio=0.8, with_field=False):
    """`weeks` weeks (+1 day) of 15-minute blocks before T0: forecast 0.15 kWh
    from 08:00 to 20:00 local, 0 otherwise; solar = ratio x forecast."""
    folder = env.tmp / "history"
    folder.mkdir(exist_ok=True)
    first = T0.astimezone(BRU).date() - timedelta(days=7 * weeks + 1)
    last = T0.astimezone(BRU).date()
    day = first
    while day <= last:
        lines = []
        for slot in range(96):
            local = datetime(day.year, day.month, day.day, tzinfo=BRU) \
                + timedelta(minutes=15 * slot)
            if local.astimezone(UTC) >= BLOCK:
                break
            forecast = 0.15 if 8 <= local.hour < 20 else 0.0
            rec = {"schema": 1, "block_start": local.astimezone(UTC).isoformat(),
                   "local_date": day.isoformat(), "block_minutes": 15,
                   "solar_kwh": round(forecast * ratio, 6),
                   "forecast_solar_kwh": forecast, "complete": False}
            if with_field and forecast >= 0.05:
                rec["solar_ratio"] = ratio
            lines.append(json.dumps(rec))
        (folder / ("blocks-%s.jsonl" % day.isoformat())).write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
        day += timedelta(days=1)


def cached_blocks(env):
    data = json.loads((env.cache_dir / "solar.json").read_text(encoding="utf-8"))
    return data["blocks"]


def raw_remaining(env, since=BLOCK):
    """Sum of the cached (raw) expected kWh from `since` on."""
    return sum(b["expected_kwh"] for b in cached_blocks(env)
               if datetime.fromisoformat(b["block_start"]) >= since)


def solar_rem(env, index=-1):
    return float(fields_of(env.decisions()[index])["solar_rem"].replace("kWh", ""))


def degraded(env, index=-1):
    return fields_of(env.decisions()[index])["degraded"].split(",")


def sensor(env):
    for e, value, attrs in reversed(env.state.published):
        if e == RATIO:
            return value, attrs
    return None


def calibration_calls(env, monkeypatch):
    calls = []
    real = env.mod.history.solar_calibration

    def counting(*args, **kwargs):
        calls.append(args[1])
        return real(*args, **kwargs)
    monkeypatch.setattr(env.mod.history, "solar_calibration", counting)
    return calls


# ---- measured and configured ------------------------------------------------------

def test_five_weeks_of_history_scale_the_forecast_by_the_measured_ratio(env):
    write_history(env, 5, ratio=0.8)
    configure(env, default=0.9)
    env.run()
    assert "solar_ratio=0.80" in degraded(env)
    assert not [m for m in degraded(env) if m.startswith("solar_ratio_configured")]
    assert solar_rem(env) == pytest.approx(0.8 * raw_remaining(env), abs=0.011)
    assert raw_remaining(env) > 1.0                         # the check means something


def test_two_weeks_of_history_use_the_configured_ratio(env):
    write_history(env, 2, ratio=0.8)
    configure(env, default=0.9)
    env.run()
    assert "solar_ratio_configured=0.90" in degraded(env)
    assert not [m for m in degraded(env) if m.startswith("solar_ratio=")]
    assert solar_rem(env) == pytest.approx(0.9 * raw_remaining(env), abs=0.011)


def test_one_week_less_than_calibration_weeks_is_not_enough(env):
    write_history(env, 3, ratio=0.8)
    configure(env, default=0.9, weeks=4)
    env.run()
    assert "solar_ratio_configured=0.90" in degraded(env)


def test_calibration_weeks_is_configurable(env):
    write_history(env, 3, ratio=0.8)
    configure(env, default=0.9, weeks=3)
    env.run()
    assert "solar_ratio=0.80" in degraded(env)


def test_defaults_change_nothing(env):
    env.run()
    assert not [m for m in degraded(env) if m.startswith("solar_ratio")]
    assert solar_rem(env) == pytest.approx(raw_remaining(env), abs=0.011)


def test_no_marker_when_the_measured_ratio_is_one(env):
    write_history(env, 5, ratio=1.0)
    configure(env, default=0.9)
    env.run()
    assert not [m for m in degraded(env) if m.startswith("solar_ratio")]
    assert solar_rem(env) == pytest.approx(raw_remaining(env), abs=0.011)


def test_no_marker_at_night_when_there_is_no_forecast_to_scale(env):
    configure(env, default=0.5)
    env.run(datetime(2026, 9, 30, 22, 35, tzinfo=UTC))      # 00:35 local
    assert not [m for m in degraded(env) if m.startswith("solar_ratio")]


def test_records_without_the_ratio_field_and_with_it_agree(env):
    write_history(env, 5, ratio=0.7, with_field=True)
    configure(env, default=0.9)
    env.run()
    assert "solar_ratio=0.70" in degraded(env)


def test_corrupt_lines_and_missing_days_are_tolerated(env):
    write_history(env, 5, ratio=0.8)
    folder = env.tmp / "history"
    files = sorted(folder.glob("blocks-*.jsonl"))
    files[10].unlink()
    with open(files[12], "a", encoding="utf-8") as handle:
        handle.write("{not json\n\n")
    configure(env, default=0.9)
    env.run()
    assert "solar_ratio=0.80" in degraded(env)
    assert env.log.by_level["error"] == []


def test_the_ratio_is_applied_to_the_trajectory_not_only_logged(env):
    write_history(env, 5, ratio=0.5)
    configure(env, default=1.0)
    env.run()
    assert solar_rem(env) == pytest.approx(0.5 * raw_remaining(env), abs=0.011)


# ---- the cache keeps the raw series -----------------------------------------------

def raw_series(env):
    cfg = env.mod._config
    midnight = T0.astimezone(BRU).replace(hour=0, minute=0, second=0, microsecond=0)
    end = midnight.astimezone(UTC) + timedelta(hours=env.mod.SERIES_SPAN_HOURS)
    return env.mod.series.solar_series(forecast_payload(), cfg, midnight, end)


def test_the_cache_holds_the_raw_forecast(env):
    write_history(env, 5, ratio=0.8)
    configure(env, default=0.9)
    env.run()
    cached = [b["expected_kwh"] for b in cached_blocks(env)]
    assert cached == pytest.approx([s.expected_kwh for s in raw_series(env)])
    assert max(cached) == pytest.approx(0.15)               # not 0.12


def test_ratio_change_is_applied_without_a_new_forecast_payload(env):
    configure(env, default=0.9, extra=EVERY_MINUTE)
    env.run(T0)
    before = (env.cache_dir / "solar.json").read_bytes()
    assert solar_rem(env) == pytest.approx(0.9 * raw_remaining(env), abs=0.011)
    configure(env, default=0.5, extra=EVERY_MINUTE)
    env.run(T0 + timedelta(minutes=1))                      # same block, same payload
    assert (env.cache_dir / "solar.json").read_bytes() == before      # cache untouched
    assert "solar_ratio_configured=0.50" in degraded(env)
    assert solar_rem(env) == pytest.approx(0.5 * raw_remaining(env), abs=0.011)


def test_a_cached_series_is_not_scaled_twice(env):
    configure(env, default=0.5, extra=EVERY_MINUTE)
    env.run(T0)
    first = solar_rem(env)
    for minute in (1, 2, 3):                                # served from the cache
        env.run(T0 + timedelta(minutes=minute))
        assert solar_rem(env) == pytest.approx(first, abs=0.011)
    assert first == pytest.approx(0.5 * raw_remaining(env), abs=0.011)
    # cache stayed raw through all of it
    assert max(b["expected_kwh"] for b in cached_blocks(env)) == pytest.approx(0.15)


def test_a_new_forecast_payload_is_rebuilt_raw_and_scaled_once(env):
    configure(env, default=0.5, extra=EVERY_MINUTE)
    env.run(T0)
    doubled = {k: v * 2 for k, v in forecast_payload().items()}
    env.state.set(FORECAST_ENTITY, "12.3", {FORECAST_ATTRIBUTE: doubled})
    env.run(T0 + timedelta(minutes=1))
    assert max(b["expected_kwh"] for b in cached_blocks(env)) == pytest.approx(0.30)
    assert solar_rem(env) == pytest.approx(0.5 * raw_remaining(env), abs=0.011)


def test_a_stale_cached_series_is_still_calibrated(env):
    configure(env, default=0.5, extra=EVERY_MINUTE)
    env.run(T0)
    env.state.drop(FORECAST_ENTITY)                          # forecast gone: cache used
    env.run(T0 + timedelta(minutes=1))
    assert solar_rem(env) == pytest.approx(0.5 * raw_remaining(env), abs=0.011)


def test_zero_solar_fallback_is_not_calibrated_or_marked(env):
    configure(env, default=0.5)
    env.state.drop(FORECAST_ENTITY)
    env.run()
    assert "solar_zero_fallback" in degraded(env)
    assert not [m for m in degraded(env) if m.startswith("solar_ratio")]
    assert solar_rem(env) == 0.0


# ---- the history keeps the raw forecast -------------------------------------------

def test_history_records_the_raw_forecast_and_the_ratio(env):
    solar_counter = "sensor.pv_total"
    configure(env, default=0.5,
              extra="history:\n  sensors:\n    solar: [%s]\n" % solar_counter)
    env.state.set(solar_counter, "100.0", {"unit_of_measurement": "kWh"})
    env.run(T0)
    env.state.set(solar_counter, "100.06", {"unit_of_measurement": "kWh"})
    env.run(T0 + 3 * STEP)                                   # crosses the 12:45 boundary
    lines = [l for f in sorted((env.tmp / "history").glob("blocks-*.jsonl"))
             for l in f.read_text(encoding="utf-8").splitlines()]
    record = json.loads(lines[0])
    assert record["block_start"] == BLOCK.isoformat()
    assert record["forecast_solar_kwh"] == pytest.approx(0.15)      # raw, not 0.075
    assert record["solar_kwh"] == pytest.approx(0.06)
    assert record["solar_ratio"] == pytest.approx(0.06 / 0.15)      # measured / raw


# ---- recomputed at most once an hour ---------------------------------------------

def test_calibration_is_computed_once_per_hour(env, monkeypatch):
    calls = calibration_calls(env, monkeypatch)
    configure(env, default=0.9)
    for k in range(5):                                       # 14:35 .. 14:55
        env.run(T0 + k * STEP)
    assert len(calls) == 1
    env.run(T0 + 5 * STEP)                                   # 15:00: a new hour
    assert len(calls) == 2
    env.run(T0 + 6 * STEP)
    assert len(calls) == 2


def test_history_files_are_not_read_every_cycle(env, monkeypatch):
    reads = []
    real = env.mod._read_texts

    def counting(paths):
        reads.append(len(paths))
        return real(paths)
    monkeypatch.setattr(env.mod, "_read_texts", counting)
    write_history(env, 5, ratio=0.8)
    configure(env, default=0.9)
    # No load counter is configured, so the usage profile re-reads its window
    # (28 days back to today = 29 files) every cycle. The calibration reads its
    # wider window (29 days back to today = 30 files) once for the whole hour.
    for k in range(4):
        env.run(T0 + k * STEP)
    assert reads.count(30) == 1
    assert reads.count(29) == 4


def test_recomputed_when_the_local_date_changes_within_the_same_hour(env, monkeypatch):
    calls = calibration_calls(env, monkeypatch)
    configure(env, default=0.9)
    env.run(T0)
    env.run(T0 + timedelta(days=1))                          # same clock hour, next day
    assert len(calls) == 2


def test_recomputed_when_the_settings_change(env, monkeypatch):
    calls = calibration_calls(env, monkeypatch)
    configure(env, default=0.9)
    env.run(T0)
    configure(env, default=0.8)
    env.run(T0 + STEP)
    assert len(calls) == 2
    configure(env, default=0.8, weeks=5)
    env.run(T0 + 2 * STEP)
    assert len(calls) == 3
    env.run(T0 + 3 * STEP)
    assert len(calls) == 3


def test_calibration_failure_falls_back_to_the_configured_ratio(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(env.mod.history, "solar_calibration", broken)
    configure(env, default=0.9)
    env.run()
    assert len(env.decisions()) == 1
    assert "solar_ratio_configured=0.90" in degraded(env)
    assert any("solar calibration failed" in m for m in env.log.by_level["warning"])
    assert env.log.by_level["error"] == []


def test_applying_failure_uses_the_raw_forecast(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(env.mod.history, "apply_solar_calibration", broken)
    configure(env, default=0.5)
    env.run()
    assert len(env.decisions()) == 1
    assert solar_rem(env) == pytest.approx(raw_remaining(env), abs=0.011)
    assert any("solar calibration failed" in m for m in env.log.by_level["warning"])


# ---- the sensor -------------------------------------------------------------------

def test_sensor_shows_the_measured_ratio(env):
    write_history(env, 5, ratio=0.8)
    configure(env, default=0.9)
    env.run()
    value, attrs = sensor(env)
    assert value == pytest.approx(0.8)
    assert attrs["source"] == "measured"
    assert attrs["weeks_of_history"] == pytest.approx(4.2, abs=0.1)   # reads x weeks + 1 day
    assert attrs["friendly_name"]


def test_sensor_shows_the_configured_ratio(env):
    write_history(env, 2, ratio=0.8)
    configure(env, default=0.9)
    env.run()
    value, attrs = sensor(env)
    assert value == pytest.approx(0.9) and attrs["source"] == "configured"
    assert attrs["weeks_of_history"] == pytest.approx(2.1, abs=0.2)


def test_sensor_without_history(env):
    env.run()
    value, attrs = sensor(env)
    assert value == 1.0 and attrs["source"] == "configured"
    assert attrs["weeks_of_history"] == 0.0


def test_sensor_is_unknown_without_a_forecast(env):
    env.state.drop(FORECAST_ENTITY)
    env.run()
    value, attrs = sensor(env)
    assert value == "unknown" and attrs["source"] is None


def test_sensor_is_not_published_when_sensors_are_off(env):
    env.write_config("sensors:\n  enabled: false\n")
    env.run()
    assert sensor(env) is None
