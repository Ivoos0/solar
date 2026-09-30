import dataclasses
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import series

TZ = ZoneInfo("Europe/Brussels")
START = datetime(2026, 6, 2, 10, 0, tzinfo=TZ)  # a Tuesday


def _end(hours):
    return START + timedelta(hours=hours)


def _hourly(values_wh, first=START):
    """Hourly periods starting at `first`; keys are period ENDS (forecast.solar)."""
    return {
        (first + timedelta(hours=i + 1)).strftime("%Y-%m-%d %H:%M:%S"): wh
        for i, wh in enumerate(values_wh)
    }


def test_hourly_1000wh_becomes_four_blocks_of_quarter_kwh(site_config):
    s = series.solar_series(_hourly([1000]), site_config, START, _end(1))
    assert [b.expected_kwh for b in s] == pytest.approx([0.25] * 4)


def test_energy_conservation_wh_to_kwh_and_split(site_config):
    payload = _hourly([0, 350, 1200, 2500, 3100, 1800, 400])
    s = series.solar_series(payload, site_config, START, _end(7))
    assert sum(b.expected_kwh for b in s) == pytest.approx(sum(payload.values()) / 1000.0)
    assert len(s) == 28
    assert all(b.source_resolution_minutes == 60 for b in s)
    assert not any(b.is_zero_fallback for b in s)


def test_sunrise_sunset_irregular_timestamps_per_block(site_config):
    # Keys are period ENDS; sunrise entry is ~0, 06:00 covers 05:15-06:00.
    payload = {
        "2026-06-02 05:15:00": 0,
        "2026-06-02 06:00:00": 100,
        "2026-06-02 07:00:00": 400,
        "2026-06-02 08:00:00": 800,
        "2026-06-02 09:00:00": 1200,
    }
    st = datetime(2026, 6, 2, 5, 15, tzinfo=TZ)
    s = series.solar_series(payload, site_config, st, datetime(2026, 6, 2, 9, 0, tzinfo=TZ))
    by = {b.block_start.strftime("%H:%M"): b for b in s}
    # 45-minute 06:00 entry: three equal blocks, no zero hole.
    for hm in ("05:15", "05:30", "05:45"):
        assert by[hm].expected_kwh == pytest.approx(0.1 / 3)
        assert by[hm].source_resolution_minutes == 45
    # Each hourly entry splits into four equal quarters; no zero block.
    for hour, total in ((6, 0.4), (7, 0.8), (8, 1.2)):
        for m in (0, 15, 30, 45):
            b = by["%02d:%02d" % (hour, m)]
            assert b.expected_kwh == pytest.approx(total / 4)
            assert b.source_resolution_minutes == 60
    assert all(b.expected_kwh > 0 for b in s)
    assert sum(b.expected_kwh for b in s) == pytest.approx(sum(payload.values()) / 1000.0)


def test_overnight_gap_is_capped_not_smeared(site_config):
    payload = {"2026-06-02 20:00:00": 500, "2026-06-03 05:00:00": 100}
    st = datetime(2026, 6, 2, 19, 0, tzinfo=TZ)
    s = series.solar_series(payload, site_config, st, datetime(2026, 6, 3, 5, 0, tzinfo=TZ))
    by = {b.block_start: b for b in s}
    assert by[datetime(2026, 6, 3, 3, 0, tzinfo=TZ)].expected_kwh == 0.0
    assert by[datetime(2026, 6, 3, 4, 45, tzinfo=TZ)].expected_kwh == pytest.approx(0.025)


def test_series_reaches_horizon_end_padded_with_zeros(site_config):
    s = series.solar_series(_hourly([1000, 1000]), site_config, START, _end(6))
    assert len(s) == 24
    assert s[-1].block_start == _end(6) - timedelta(minutes=15)
    assert all(b.expected_kwh == 0.0 for b in s[8:])
    assert not any(b.is_zero_fallback for b in s)
    steps = {b.block_start - a.block_start for a, b in zip(s, s[1:])}
    assert steps == {timedelta(minutes=15)}


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"garbage": "x"}, {"2026-06-02 10:00:00": "abc"}, "nope"],
)
def test_zero_fallback(site_config, payload):
    s = series.solar_series(payload, site_config, START, _end(3))
    assert len(s) == 12
    assert all(b.expected_kwh == 0.0 and b.is_zero_fallback for b in s)


def _history(days, per_block=lambda ts: 0.25, end=START):
    out = []
    t = end - timedelta(days=days)
    while t < end:
        out.append((t, per_block(t)))
        t += timedelta(minutes=15)
    return out


def test_flat_history_gives_flat_profile(site_config):
    u = series.usage_profile(_history(14), site_config, START, _end(30))
    assert all(b.expected_kwh == pytest.approx(0.25) for b in u)
    assert len(u) == 120


def test_weekday_specific_history(site_config):
    def f(ts):
        return 0.5 if ts.weekday() == 1 else 0.1  # Tuesday heavy

    u = series.usage_profile(_history(14, f), site_config, START, _end(48))
    tue = [b.expected_kwh for b in u if b.block_start.weekday() == 1]
    wed = [b.expected_kwh for b in u if b.block_start.weekday() == 2]
    assert tue and wed
    assert all(v == pytest.approx(0.5) for v in tue)
    assert all(v == pytest.approx(0.1) for v in wed)


def test_sample_days_counts_contributing_days(site_config):
    u = series.usage_profile(_history(21), site_config, START, _end(2))
    assert all(b.sample_days == 3 for b in u)


def test_short_history_reports_honest_per_slot_counts(site_config):
    u = series.usage_profile(_history(3), site_config, START, _end(150))
    counts = {b.sample_days for b in u}
    assert counts <= {0, 1}
    assert 1 in counts and 0 in counts


def test_empty_bucket_uses_overall_mean_not_zero(site_config):
    u = series.usage_profile(_history(2), site_config, START, _end(30))
    empty = [b for b in u if b.sample_days == 0]
    assert empty
    assert all(b.expected_kwh == pytest.approx(0.25) for b in empty)


def test_no_history_gives_zeros_without_raising(site_config):
    for h in ([], None):
        u = series.usage_profile(h, site_config, START, _end(3))
        assert len(u) == 12
        assert all(b.expected_kwh == 0.0 and b.sample_days == 0 for b in u)


def test_usage_spans_horizon_when_history_is_short(site_config):
    u = series.usage_profile(_history(1), site_config, START, _end(35))
    assert len(u) == 140
    assert u[-1].block_start == _end(35) - timedelta(minutes=15)


# --- FR-059: configurable window and grouping --------------------------------
# START is Tuesday 2026-06-02 10:00, so with the default 4-week window the
# cutoff is Tuesday 2026-05-05 10:00. One reading per date, at 18:00 local.

def _cfg(site_config, grouping, weeks=4):
    return dataclasses.replace(site_config, usage_grouping=grouping,
                               usage_history_weeks=weeks)


def _at18(spec):
    """{(month, day): kwh} -> readings at 18:00 local on those 2026 dates."""
    return [(datetime(2026, m, d, 18, 0, tzinfo=TZ), v) for (m, d), v in spec.items()]


def _slot(profile, month, day):
    want = datetime(2026, month, day, 18, 0, tzinfo=TZ)
    return next(b for b in profile if b.block_start == want)


def _profile(cfg, history):
    return series.usage_profile(history, cfg, START, START + timedelta(days=8))


# Wednesdays 6/13/20/27 May = 0.2/0.4/0.6/0.8, Tuesdays 12/19/26 May = 1.0,
# Thursday 7 May = 5.0, Sat 9 May = 2.0, Sun 10 May = 4.0.
_MIXED = {(5, 6): 0.2, (5, 13): 0.4, (5, 20): 0.6, (5, 27): 0.8,
          (5, 12): 1.0, (5, 19): 1.0, (5, 26): 1.0,
          (5, 7): 5.0, (5, 9): 2.0, (5, 10): 4.0}


def test_same_weekday_wednesday_is_mean_of_exactly_the_wednesdays(site_config):
    cfg = _cfg(site_config, "same_weekday")
    p = _profile(cfg, _at18(_MIXED))
    wed = _slot(p, 6, 3)
    assert wed.block_start.weekday() == 2
    # (0.2 + 0.4 + 0.6 + 0.8) / 4 = 0.5 ; Thursday's 5.0 and Tuesdays excluded
    assert wed.expected_kwh == pytest.approx(0.5)
    assert wed.sample_days == 4
    # Tuesday: (1.0 + 1.0 + 1.0) / 3 = 1.0, differs from Wednesday
    tue = _slot(p, 6, 9)
    assert tue.expected_kwh == pytest.approx(1.0) and tue.sample_days == 3
    # Saturday 2.0 and Sunday 4.0 are not pooled
    assert _slot(p, 6, 6).expected_kwh == pytest.approx(2.0)
    assert _slot(p, 6, 7).expected_kwh == pytest.approx(4.0)


def test_day_type_pools_weekdays_and_weekends_separately(site_config):
    cfg = _cfg(site_config, "day_type")
    p = _profile(cfg, _at18(_MIXED))
    # weekdays: Wed 2.0 + Tue 3.0 + Thu 5.0 = 10.0 over 4+3+1 = 8 dates -> 1.25
    tue, wed = _slot(p, 6, 9), _slot(p, 6, 3)
    assert wed.expected_kwh == pytest.approx(1.25)
    assert tue.expected_kwh == pytest.approx(1.25)
    assert wed.sample_days == tue.sample_days == 8
    # weekend: (2.0 + 4.0) / 2 = 3.0 for both Saturday and Sunday, never mixed
    sat, sun = _slot(p, 6, 6), _slot(p, 6, 7)
    assert sat.block_start.weekday() == 5 and sun.block_start.weekday() == 6
    assert sat.expected_kwh == pytest.approx(3.0)
    assert sun.expected_kwh == pytest.approx(3.0)
    assert sat.sample_days == sun.sample_days == 2
    assert sat.expected_kwh != pytest.approx(wed.expected_kwh)


@pytest.mark.parametrize("grouping", ["same_weekday", "day_type"])
def test_readings_older_than_window_are_ignored(site_config, grouping):
    cfg = _cfg(site_config, grouping)
    base = _profile(cfg, _at18(_MIXED))
    # Wed 29 Apr and Tue 28 Apr are 5 weeks old (cutoff is 5 May 10:00)
    stale = dict(_MIXED)
    stale[(4, 29)] = 100.0
    stale[(4, 28)] = 100.0
    with_stale = _profile(cfg, _at18(stale))
    assert [(b.expected_kwh, b.sample_days) for b in with_stale] == \
           [(b.expected_kwh, b.sample_days) for b in base]


def test_history_weeks_shrinks_the_window(site_config):
    # weeks=2 -> cutoff 19 May 10:00 keeps Wed 20 May (0.6) and 27 May (0.8):
    # (0.6 + 0.8) / 2 = 0.7
    p = _profile(_cfg(site_config, "same_weekday", weeks=2), _at18(_MIXED))
    wed = _slot(p, 6, 3)
    assert wed.expected_kwh == pytest.approx(0.7) and wed.sample_days == 2


def test_switching_grouping_changes_output_for_identical_history(site_config):
    hist = _at18(_MIXED)
    a = _slot(_profile(_cfg(site_config, "same_weekday"), hist), 6, 3)
    b = _slot(_profile(_cfg(site_config, "day_type"), hist), 6, 3)
    assert (a.expected_kwh, b.expected_kwh) == (pytest.approx(0.5), pytest.approx(1.25))


def test_thin_history_day_type_fills_weekdays_same_weekday_falls_back(site_config):
    # Mon 25 May 0.4, Tue 26 May 1.0, Sun 24 May 2.2 (all at 18:00)
    hist = _at18({(5, 25): 0.4, (5, 26): 1.0, (5, 24): 2.2})
    dt = _slot(_profile(_cfg(site_config, "day_type"), hist), 6, 3)
    # weekday group: (0.4 + 1.0) / 2 = 0.7 from 2 dates
    assert dt.expected_kwh == pytest.approx(0.7) and dt.sample_days == 2
    sw = _slot(_profile(_cfg(site_config, "same_weekday"), hist), 6, 3)
    # no Wednesday yet -> overall mean (0.4 + 1.0 + 2.2) / 3 = 1.2, sample_days 0
    assert sw.expected_kwh == pytest.approx(1.2) and sw.sample_days == 0


@pytest.mark.parametrize("grouping", ["same_weekday", "day_type"])
def test_flat_history_flat_profile_both_groupings(site_config, grouping):
    cfg = _cfg(site_config, grouping)
    u = series.usage_profile(_history(28), cfg, START, _end(30))
    assert len(u) == 120
    assert all(b.expected_kwh == pytest.approx(0.25) for b in u)


@pytest.mark.parametrize("grouping", ["same_weekday", "day_type"])
def test_no_history_zeros_both_groupings(site_config, grouping):
    u = series.usage_profile([], _cfg(site_config, grouping), START, _end(3))
    assert all(b.expected_kwh == 0.0 and b.sample_days == 0 for b in u)


# ---- DST grids (Europe/Brussels) ------------------------------------------

def _day(y, m, d):
    start = datetime(y, m, d, tzinfo=TZ)
    nxt = datetime(y, m, d) + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=TZ)


@pytest.mark.parametrize("ymd,count", [((2026, 10, 25), 100), ((2026, 3, 29), 92),
                                       ((2026, 6, 2), 96)])
def test_grid_counts_on_dst_days(site_config, ymd, count):
    start, end = _day(*ymd)
    grid = series._grid(site_config, start, end)
    assert len(grid) == count
    utc = [t.astimezone(timezone.utc) for t in grid]
    assert len(set(utc)) == count
    assert all(b - a == timedelta(minutes=15) for a, b in zip(utc, utc[1:]))
    for t in grid:  # every local time is a real Brussels time for its instant
        rt = t.astimezone(timezone.utc).astimezone(TZ)
        assert rt.replace(tzinfo=None) == t.replace(tzinfo=None)
        assert rt.utcoffset() == t.utcoffset()


def test_fall_back_grid_repeats_the_02_hour_with_both_offsets(site_config):
    start, end = _day(2026, 10, 25)
    grid = series._grid(site_config, start, end)
    hour2 = [t for t in grid if t.hour == 2]
    assert len(hour2) == 8
    assert sorted({t.utcoffset() for t in hour2}) == [timedelta(hours=1), timedelta(hours=2)]


def test_spring_forward_grid_has_no_02_hour(site_config):
    start, end = _day(2026, 3, 29)
    assert not any(t.hour == 2 for t in series._grid(site_config, start, end))


def test_series_on_fall_back_day_are_dense_100(site_config):
    start, end = _day(2026, 10, 25)
    # one hourly entry per hour; keys are period ENDS (local wall time, naive
    # keys resolve to fold=0, so use offset-explicit ISO keys)
    utc0 = start.astimezone(timezone.utc)
    payload = {}
    for k in range(1, 26):
        loc = (utc0 + timedelta(hours=k)).astimezone(TZ)
        payload[loc.astimezone(timezone(loc.utcoffset())).isoformat()] = 1000
    s = series.solar_series(payload, site_config, start, end)
    assert len(s) == 100
    assert len({b.block_start.astimezone(timezone.utc) for b in s}) == 100
    assert sum(b.expected_kwh for b in s) == pytest.approx(25.0)
    u = series.usage_profile([], site_config, start, end)
    assert len(u) == 100
