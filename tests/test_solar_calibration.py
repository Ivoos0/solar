"""Solar calibration: the stored ratio, the per-time-of-day ratios, applying
them, the markers and the new settings. Pure functions (history.py, config.py).
The adapter side (cache, throttle, sensors) is in test_solar_calibration_adapter.py.
"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
import history as h
import series

UTC = ZoneInfo("UTC")
BRU = ZoneInfo("Europe/Brussels")
STEP = timedelta(minutes=15)
D0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = D0 + timedelta(days=9)            # the history below covers days 0..8


def rec(start, solar, forecast, **extra):
    r = dict.fromkeys(h.RECORD_FIELDS)
    r.update(block_start=start.astimezone(timezone.utc).isoformat(),
             solar_kwh=solar, forecast_solar_kwh=forecast)
    r.update(extra)
    return r


def at(day, minute):
    """UTC datetime `day` days after D0 at `minute` minutes past midnight."""
    return D0 + timedelta(days=day, minutes=minute)


def cal_of(records, weeks=1, default=1.0, now=NOW, tz=UTC):
    return h.solar_calibration(records, now, weeks, default, tz, 15)


def slot_ratios(minutes, f=1.0, s=1.0, days=range(0, 9)):
    """One (forecast f, solar s) block per day at each given minute of day."""
    return [rec(at(d, m), s, f) for d in days for m in minutes]


# ---- the stored ratio -------------------------------------------------------------

B0 = datetime(2026, 9, 30, 12, 30, tzinfo=timezone.utc)


def one_block(solar_delta, forecast, with_solar=True):
    readings = {"solar": [100.0]} if with_solar else {}
    a = h.make_snapshot(B0, B0, readings, forecast_solar_kwh=forecast)
    end = {"solar": [100.0 + solar_delta]} if with_solar else {}
    b = h.make_snapshot(B0 + STEP, B0 + STEP, end)
    return h.block_record(a, b, 15, UTC)


def test_ratio_is_measured_over_forecast():
    assert one_block(0.4, 0.5)["solar_ratio"] == pytest.approx(0.8)


def test_ratio_field_is_part_of_the_record():
    assert "solar_ratio" in h.RECORD_FIELDS
    assert "solar_ratio" in one_block(0.4, 0.5)


def test_ratio_is_not_clamped_in_the_record():
    assert one_block(1.0, 0.1)["solar_ratio"] == pytest.approx(10.0)


@pytest.mark.parametrize("delta, forecast, with_solar", [
    (0.4, None, True),          # forecast unknown
    (0.4, 0.5, False),          # no solar counter
    (0.0, 0.04, True),          # forecast below the 0.05 kWh minimum
    (0.4, 0.0, True),
])
def test_ratio_is_null_unless_both_are_known(delta, forecast, with_solar):
    assert one_block(delta, forecast, with_solar)["solar_ratio"] is None


def test_ratio_forecast_boundary_is_inclusive():
    assert h.SOLAR_RATIO_MIN_FORECAST_KWH == 0.05
    assert one_block(0.05, 0.05)["solar_ratio"] == pytest.approx(1.0)
    assert one_block(0.05, 0.0499)["solar_ratio"] is None


def test_ratio_zero_production_is_a_real_zero():
    assert one_block(0.0, 0.5)["solar_ratio"] == 0.0


def test_ratio_null_after_a_counter_reset():
    r = one_block(-5.0, 0.5)               # negative delta: solar_kwh is None
    assert r["solar_kwh"] is None and r["solar_ratio"] is None


def test_gap_records_have_a_null_ratio():
    a = h.make_snapshot(B0, B0, {"solar": [1.0]}, forecast_solar_kwh=0.5)
    b = h.make_snapshot(B0 + 3 * STEP, B0 + 3 * STEP, {"solar": [2.0]})
    gap = h.records_between(a, b, 15, UTC)
    assert len(gap) == 3 and all(r["solar_ratio"] is None for r in gap)


# ---- fewer than x weeks of history -> the default --------------------------------

def test_less_than_the_window_uses_the_default_everywhere():
    records = slot_ratios(range(600, 780, 15), s=0.5, days=range(0, 7))  # 6 days
    cal = cal_of(records, weeks=1, default=0.9)
    assert cal["enough"] is False and cal["ratios"] == {}
    assert h.calibration_ratio(cal, 720) == (0.9, "configured")


def test_span_boundary_exactly_x_weeks_is_enough():
    records = slot_ratios([720], s=0.5, days=range(0, 8))        # 7 days apart
    assert cal_of(records, weeks=1)["enough"] is True
    records = slot_ratios([720], s=0.5, days=range(0, 29))
    assert cal_of(records, weeks=4)["enough"] is True


def test_span_one_block_short_of_x_weeks_is_not_enough():
    records = slot_ratios([720], s=0.5, days=range(0, 7))        # oldest day 0
    records.append(rec(at(7, 705), 0.5, 1.0))                    # 15 min short
    assert cal_of(records, weeks=1)["enough"] is False
    records.append(rec(at(7, 720), 0.5, 1.0))                    # exactly 7 days
    assert cal_of(records, weeks=1)["enough"] is True


def test_span_counts_only_blocks_that_have_a_ratio():
    records = slot_ratios([720], s=0.5, days=range(1, 8))        # 6 days of span
    records.append(rec(at(0, 720), None, 1.0))                   # no solar: ignored
    records.append(rec(at(0, 735), 0.5, 0.01))                   # forecast too small
    assert cal_of(records, weeks=1)["enough"] is False


def test_no_history_means_default():
    cal = cal_of([], weeks=4, default=0.8)
    assert cal["enough"] is False
    assert h.calibration_ratio(cal, 720) == (0.8, "configured")
    assert h.weeks_of_history(cal) == 0.0


# ---- the +- 1 hour window ---------------------------------------------------------

def window_records():
    """Blocks per slot, 9 days. Around T=12:00 (minute 720):
    11:00 forecast 1 solar 0; 13:00 forecast 1 solar 2; 10:45 and 13:15
    forecast 100 solar 0 (just outside); every other slot 1 / 1."""
    out = slot_ratios(range(540, 900, 15), s=1.0, f=1.0)
    out = [r for r in out if not _slot_in(r, (660, 780, 645, 795))]
    out += slot_ratios([660], f=1.0, s=0.0)
    out += slot_ratios([780], f=1.0, s=2.0)
    out += slot_ratios([645, 795], f=100.0, s=0.0)
    return out


def _slot_in(r, minutes):
    t = datetime.fromisoformat(r["block_start"])
    return t.hour * 60 + t.minute in minutes


def test_window_includes_one_hour_either_side_and_excludes_the_next_block():
    cal = cal_of(window_records(), weeks=1)
    # inside: 11:00 (0/1) .. 13:00 (2/1) -> solar 9, forecast 9; outside: 10:45, 13:15
    assert cal["ratios"][720] == pytest.approx(9.0 / 9.0)
    # mutation guard: the same data with a boundary block left out would differ
    only_lower = [r for r in window_records() if not _slot_in(r, (780,))]
    assert cal_of(only_lower, weeks=1)["ratios"][720] == pytest.approx(7.0 / 8.0)
    only_upper = [r for r in window_records() if not _slot_in(r, (660,))]
    assert cal_of(only_upper, weeks=1)["ratios"][720] == pytest.approx(9.0 / 8.0)


def test_window_constants():
    assert h.SOLAR_RATIO_WINDOW_MINUTES == 60


def test_window_on_flat_data_gives_the_flat_ratio():
    records = slot_ratios(range(540, 900, 15), s=0.6, days=range(0, 9))
    cal = cal_of(records, weeks=1)
    assert cal["ratios"][720] == pytest.approx(0.6)


def test_window_uses_local_time_of_day():
    # 10:00 and 10:15 UTC are 12:00 and 12:15 in Brussels in summer time
    records = slot_ratios([600, 615], s=0.5, days=range(0, 13))
    cal = h.solar_calibration(records, at(13, 0), 1, 1.0, BRU, 15)
    assert cal["ratios"][720] == pytest.approx(0.5)
    assert 600 not in cal["ratios"]


def test_blocks_older_than_the_window_do_not_count_but_widen_the_span():
    old = slot_ratios([720, 735], s=0.2, days=range(-20, 23))    # ratio 0.2, up to a week old
    new = slot_ratios([720, 735], s=0.8, days=range(23, 30))     # last week
    cal = cal_of(old + new, weeks=1,
                 now=at(29, 780))
    assert cal["enough"] is True
    assert cal["ratios"][720] == pytest.approx(0.8)              # old ones ignored


def test_blocks_after_now_do_not_count():
    past = slot_ratios([720, 735], s=0.8, days=range(0, 9))
    future = slot_ratios([720, 735], s=0.1, days=range(9, 14))
    assert cal_of(past + future, weeks=1)["ratios"][720] == pytest.approx(0.8)


def test_window_wraps_around_midnight():
    records = (slot_ratios([1410, 1425], s=0.2, days=range(0, 9))
               + slot_ratios([0, 15, 30, 45], s=0.8, days=range(0, 9)))
    cal = cal_of(records, weeks=1)
    assert cal["ratios"][0] == pytest.approx((2 * 0.2 + 4 * 0.8) / 6)


def test_ratio_of_negative_production_is_unknown():
    assert h.solar_ratio_of(-0.1, 0.5) is None
    assert h.solar_ratio_of(0.0, 0.5) == 0.0


# ---- the energy-weighted average --------------------------------------------------

def test_average_is_energy_weighted_not_a_mean_of_ratios():
    # block A forecast 1.0 actual 0.8 (ratio 0.8); block B forecast 0.5 actual
    # 0.2 (ratio 0.4). Weighted: 1.0 / 1.5 = 0.667. Plain mean of ratios: 0.6.
    records = []
    for d in range(0, 9):
        records.append(rec(at(d, 720), 0.8, 1.0))
        records.append(rec(at(d, 735), 0.2, 0.5))
    cal = cal_of(records, weeks=1)
    assert cal["ratios"][720] == pytest.approx(1.0 / 1.5)
    assert cal["ratios"][720] != pytest.approx(0.6)


def test_old_records_without_the_ratio_field_still_count():
    records = slot_ratios([720, 735], s=0.8, days=range(0, 9))
    for r in records:
        assert "solar_ratio" in r and r["solar_ratio"] is None   # field absent/null
        r.pop("solar_ratio")
    assert cal_of(records, weeks=1)["ratios"][720] == pytest.approx(0.8)


def test_duplicate_blocks_are_counted_once():
    records = slot_ratios([720], s=0.8, days=range(0, 9))
    assert cal_of(records * 2, weeks=1)["ratios"] == {}          # 9 blocks < 12
    assert cal_of(records + slot_ratios([735], s=0.8, days=range(0, 9)),
                  weeks=1)["ratios"][720] == pytest.approx(0.8)  # 18 blocks


# ---- at least 12 blocks ---------------------------------------------------------

def test_fewer_than_twelve_blocks_use_the_default():
    assert h.SOLAR_RATIO_MIN_BLOCKS == 12
    days = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 14]                     # 11 blocks, span 14 d
    records = [rec(at(d, 720), 0.5, 1.0) for d in days]
    cal = cal_of(records, weeks=2, default=0.9, now=at(14, 720))
    assert cal["enough"] is True
    assert 720 not in cal["ratios"]
    assert h.calibration_ratio(cal, 720) == (0.9, "configured")


def test_twelve_blocks_are_enough():
    days = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14]                 # 12 blocks
    records = [rec(at(d, 720), 0.5, 1.0) for d in days]
    cal = cal_of(records, weeks=2, default=0.9, now=at(14, 720))
    assert h.calibration_ratio(cal, 720) == (0.5, "measured")


def test_mixed_per_time_of_day_some_measured_some_default():
    records = slot_ratios(range(660, 780, 15), s=0.7)             # midday only
    cal = cal_of(records, weeks=1, default=0.9)
    assert h.calibration_ratio(cal, 720)[1] == "measured"
    assert h.calibration_ratio(cal, 240) == (0.9, "configured")   # 04:00: no data


# ---- clamp -----------------------------------------------------------------------

def test_applied_ratio_is_clamped_to_two():
    records = slot_ratios([720, 735], f=0.1, s=0.5)               # ratio 5.0
    cal = cal_of(records, weeks=1)
    assert cal["ratios"][720] == pytest.approx(2.0)
    assert h.calibration_ratio(cal, 720) == (2.0, "measured")


def test_clamp_applies_to_the_default_too():
    cal = h.default_calibration(3.5, 4)
    assert h.calibration_ratio(cal, 720) == (2.0, "configured")
    assert h.calibration_ratio(h.default_calibration(-1.0, 4), 720)[0] == 0.0


def test_zero_measured_solar_gives_a_zero_ratio():
    records = slot_ratios([720, 735], f=0.5, s=0.0)
    cal = cal_of(records, weeks=1)
    assert h.calibration_ratio(cal, 720) == (0.0, "measured")


# ---- applying and the markers ---------------------------------------------------

def slots_for(day, minutes, kwh=1.0, zero=False):
    base = datetime(2026, 9, day, tzinfo=timezone.utc)
    return [series.ForecastSlot(base + timedelta(minutes=m), kwh, zero, 60)
            for m in minutes]


def test_apply_scales_each_block_by_its_time_of_day_ratio():
    cal = {"default": 0.9, "weeks": 4, "span_days": 30.0, "enough": True,
           "ratios": {720: 0.5, 735: 1.5}}
    raw = slots_for(30, [720, 735, 750])
    out = h.apply_solar_calibration(raw, cal, UTC, 15)
    assert [s.expected_kwh for s in out] == pytest.approx([0.5, 1.5, 0.9])
    assert [s.block_start for s in out] == [s.block_start for s in raw]
    assert [s.expected_kwh for s in raw] == [1.0, 1.0, 1.0]       # input untouched


def test_apply_uses_local_time_of_day():
    cal = {"default": 1.0, "weeks": 4, "span_days": 30.0, "enough": True,
           "ratios": {720: 0.5}}
    raw = slots_for(30, [600])                                    # 10:00Z = 12:00 BRU
    assert h.apply_solar_calibration(raw, cal, BRU, 15)[0].expected_kwh == 0.5
    assert h.apply_solar_calibration(raw, cal, UTC, 15)[0].expected_kwh == 1.0


def test_apply_leaves_zero_fallback_slots_alone():
    cal = h.default_calibration(0.5, 4)
    raw = slots_for(30, [720], kwh=0.0, zero=True)
    assert h.apply_solar_calibration(raw, cal, UTC, 15) == raw


def test_apply_is_not_idempotent_so_callers_must_start_from_raw():
    cal = h.default_calibration(0.5, 4)
    raw = slots_for(30, [720])
    once = h.apply_solar_calibration(raw, cal, UTC, 15)
    twice = h.apply_solar_calibration(once, cal, UTC, 15)
    assert once[0].expected_kwh == 0.5 and twice[0].expected_kwh == 0.25


def test_slots_are_frozen():
    with pytest.raises(FrozenInstanceError):
        slots_for(30, [720])[0].expected_kwh = 2.0


NOON = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)


def measured(ratio):
    return {"default": 1.0, "weeks": 4, "span_days": 40.0, "enough": True,
            "ratios": {720: ratio, 735: ratio}}


def test_marker_for_a_measured_ratio():
    raw = slots_for(30, [720, 735])
    assert h.calibration_marker(measured(0.83), raw, NOON, UTC, 15) == "solar_ratio=0.83"


def test_marker_for_the_configured_ratio():
    raw = slots_for(30, [720, 735])
    cal = h.default_calibration(0.8, 4)
    assert h.calibration_marker(cal, raw, NOON, UTC, 15) == "solar_ratio_configured=0.80"


def test_no_marker_at_a_ratio_of_one():
    raw = slots_for(30, [720, 735])
    assert h.calibration_marker(h.default_calibration(1.0, 4), raw, NOON, UTC, 15) is None
    assert h.calibration_marker(measured(1.0), raw, NOON, UTC, 15) is None
    assert h.calibration_marker(measured(1.004), raw, NOON, UTC, 15) is None


def test_marker_looks_at_the_next_block_when_the_current_one_has_no_forecast():
    raw = slots_for(30, [720], kwh=0.0) + slots_for(30, [735], kwh=1.0)
    assert h.calibration_marker(measured(0.83), raw, NOON, UTC, 15) == "solar_ratio=0.83"


def test_marker_skips_blocks_without_forecast_solar():
    raw = slots_for(30, [720, 735], kwh=0.0)                      # night
    assert h.calibration_marker(h.default_calibration(0.8, 4), raw, NOON, UTC, 15) is None


def test_marker_ignores_blocks_after_the_next_one():
    raw = slots_for(30, [720, 735], kwh=0.0) + slots_for(30, [750], kwh=1.0)
    assert h.calibration_marker(h.default_calibration(0.8, 4), raw, NOON, UTC, 15) is None


def test_marker_not_for_a_zero_fallback_series():
    raw = slots_for(30, [720, 735], kwh=0.0, zero=True)
    assert h.calibration_marker(h.default_calibration(0.8, 4), raw, NOON, UTC, 15) is None


def test_current_ratio_and_weeks_of_history():
    cal = measured(0.83)
    assert h.current_solar_ratio(cal, NOON, UTC, 15) == (0.83, "measured")
    assert h.weeks_of_history(cal) == 5.7
    night = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
    assert h.current_solar_ratio(cal, night, UTC, 15) == (1.0, "configured")


def test_reading_the_calibration_does_not_need_a_clock_or_io():
    import inspect
    source = inspect.getsource(h)
    assert "datetime.now" not in source and "time.time" not in source


# ---- settings --------------------------------------------------------------------

def _cfg(**sections):
    raw = {"battery": {"capacity_kwh": 10.0, "soc_sensor": "sensor.test_battery_soc"}, "alerts": {"address": "a@b.c"}}
    raw.update(sections)
    return config.from_dict(raw)


def test_calibration_defaults():
    c = _cfg()
    assert c.solar_calibration_default == 1.0 and c.solar_calibration_weeks == 4


def test_calibration_settings_are_read():
    c = _cfg(solar={"calibration_default": 0.8, "calibration_weeks": 2})
    assert c.solar_calibration_default == 0.8 and c.solar_calibration_weeks == 2


@pytest.mark.parametrize("value", [0, -0.5, 2.01, 3, "0.8", True, None])
def test_calibration_default_must_be_above_zero_and_at_most_two(value):
    if value is None:                  # null means "not set": the default applies
        assert _cfg(solar={"calibration_default": None}).solar_calibration_default == 1.0
        return
    with pytest.raises(config.ConfigError, match=r"calibration_default|solar"):
        _cfg(solar={"calibration_default": value})


@pytest.mark.parametrize("value", [0.01, 1, 2])
def test_calibration_default_accepts_the_range(value):
    assert _cfg(solar={"calibration_default": value}).solar_calibration_default == value


@pytest.mark.parametrize("value", [0, -1, 1.5, 2.0, "4", True])
def test_calibration_weeks_must_be_a_whole_number_of_one_or_more(value):
    with pytest.raises(config.ConfigError, match=r"calibration_weeks|solar"):
        _cfg(solar={"calibration_weeks": value})


def test_keep_days_must_cover_the_calibration_window():
    with pytest.raises(config.ConfigError) as err:
        _cfg(solar={"calibration_weeks": 13}, retention={"keep_days": 90})
    assert "retention.keep_days" in str(err.value)
    assert "solar.calibration_weeks" in str(err.value)
    assert "usage.history_weeks" in str(err.value)
    assert "91" in str(err.value)
    assert _cfg(solar={"calibration_weeks": 13},
                retention={"keep_days": 91}).retention_keep_days == 91


def test_keep_days_uses_the_larger_of_the_two_windows():
    ok = _cfg(solar={"calibration_weeks": 2}, usage={"history_weeks": 6},
              retention={"keep_days": 42})
    assert ok.retention_keep_days == 42
    with pytest.raises(config.ConfigError, match="retention.keep_days"):
        _cfg(solar={"calibration_weeks": 2}, usage={"history_weeks": 6},
             retention={"keep_days": 41})
    with pytest.raises(config.ConfigError, match="retention.keep_days"):
        _cfg(solar={"calibration_weeks": 6}, usage={"history_weeks": 2},
             retention={"keep_days": 41})


def test_calibration_is_not_in_the_cache_fingerprint():
    ref = _cfg().fingerprint()
    assert _cfg(solar={"calibration_default": 0.5}).fingerprint() == ref
    assert _cfg(solar={"calibration_weeks": 8},
                retention={"keep_days": 90}).fingerprint() == ref
