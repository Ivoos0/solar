import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

import prices

T0 = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


def hourly(n, start=T0, base=0.10):
    return [{"time": start + timedelta(hours=i), "price": base + i / 1000}
            for i in range(n)]


# --- derivation -----------------------------------------------------------

def test_derive_normal_price(site_config):
    assert prices.derive(0.10, site_config) == (0.1140, 0.0830)


def test_zero_market_price_is_asymmetric(site_config):
    consumption, injection = prices.derive(0.0, site_config)
    assert consumption == 0.0070
    assert injection == -0.0110


@pytest.mark.parametrize("market,cons_pos,inj_pos", [
    (0.1000, True, True),     # ordinary hour
    (0.0200, True, True),     # just above the band
    (0.0100, True, False),    # inside the band: V2 is load-bearing
    (0.0000, True, False),    # inside the band
    (-0.0050, True, False),   # inside the band
    (-0.0100, False, False),  # below the band: charge selector fires
    (-0.0500, False, False),  # strongly negative
])
def test_negative_injection_band_table(site_config, market, cons_pos, inj_pos):
    c, i = prices.derive(market, site_config)
    assert (c > 0, i > 0) == (cons_pos, inj_pos)


def test_band_boundaries_derived_from_config(site_config):
    c = site_config
    cons_zero = -c.consumption_offset / c.consumption_multiplier
    inj_zero = -c.injection_offset / c.injection_multiplier
    eps = 0.0005
    assert prices.derive(cons_zero + eps, c)[0] > 0
    assert prices.derive(cons_zero - eps, c)[0] < 0
    assert prices.derive(inj_zero + eps, c)[1] > 0
    assert prices.derive(inj_zero - eps, c)[1] < 0
    # The band: injection negative while consumption still positive.
    assert cons_zero < inj_zero
    mid = (cons_zero + inj_zero) / 2
    consumption, injection = prices.derive(mid, c)
    assert consumption > 0 and injection < 0


def test_different_coefficients_move_the_band(site_config):
    other = dataclasses.replace(
        site_config, injection_offset=-0.03, consumption_offset=0.0)
    inj_zero = -other.injection_offset / other.injection_multiplier
    default_inj_zero = (-site_config.injection_offset
                        / site_config.injection_multiplier)
    assert inj_zero > default_inj_zero
    consumption, injection = prices.derive(0.02, other)
    assert consumption > 0 and injection < 0


# --- expansion ------------------------------------------------------------

def test_hourly_expands_to_four_flat_blocks(site_config):
    raw = hourly(24)
    series = prices.expand_to_blocks(raw, site_config, T0)
    assert len(series) == 96
    for entry in raw:
        blocks = [series[entry["time"] + timedelta(minutes=15 * k)]
                  for k in range(4)]
        assert len({b.consumption_price for b in blocks}) == 1
        assert all(b.market_price == entry["price"] for b in blocks)
        assert all(b.source_resolution_minutes == 60 for b in blocks)


def test_no_interpolation_no_invented_values(site_config):
    raw = hourly(6)
    series = prices.expand_to_blocks(raw, site_config, T0)
    source = {e["price"] for e in raw}
    assert {p.market_price for p in series.values()} <= source


def test_quarter_hour_input_expands_one_to_one(site_config):
    raw = [{"time": T0 + timedelta(minutes=15 * i), "price": 0.05 + i / 100}
           for i in range(8)]
    series = prices.expand_to_blocks(raw, site_config, T0)
    assert len(series) == 8
    assert all(p.source_resolution_minutes == 15 for p in series.values())
    assert series[T0 + timedelta(minutes=30)].market_price == 0.07


def test_single_entry_falls_back_to_hourly(site_config):
    series = prices.expand_to_blocks(hourly(1), site_config, T0)
    assert len(series) == 4
    assert all(p.source_resolution_minutes == 60 for p in series.values())


def test_series_is_mapping_keyed_by_block_start(site_config):
    series = prices.expand_to_blocks(hourly(2), site_config, T0)
    assert isinstance(series, dict)
    assert all(k == p.block_start for k, p in series.items())
    assert all(k.minute % 15 == 0 for k in series)


def test_gap_stays_a_gap_and_neighbours_do_not_shift(site_config):
    raw = hourly(5)
    del raw[2]  # missing 02:00 hour
    series = prices.expand_to_blocks(raw, site_config, T0)
    assert len(series) == 16
    gap = [T0 + timedelta(hours=2, minutes=15 * k) for k in range(4)]
    assert not any(g in series for g in gap)
    assert (series[T0 + timedelta(hours=1, minutes=45)].market_price
            == raw[1]["price"])
    assert series[T0 + timedelta(hours=3)].market_price == raw[2]["price"]
    assert all(p.source_resolution_minutes == 60 for p in series.values())


def test_elapsed_blocks_dropped_current_kept(site_config):
    start = T0 + timedelta(hours=1, minutes=20)
    series = prices.expand_to_blocks(hourly(3), site_config, start)
    assert min(series) == T0 + timedelta(hours=1, minutes=15)


def test_iso_string_times_accepted(site_config):
    raw = [{"time": "2026-09-30T00:00:00+00:00", "price": 0.1}]
    series = prices.expand_to_blocks(raw, site_config, T0)
    assert T0 in series


# --- horizon --------------------------------------------------------------

def test_horizon_24_hours(site_config):
    series = prices.expand_to_blocks(hourly(24), site_config, T0)
    assert prices.horizon_end(series) == T0 + timedelta(hours=24)


def test_horizon_35_hours(site_config):
    series = prices.expand_to_blocks(hourly(35), site_config, T0)
    assert prices.horizon_end(series) == T0 + timedelta(hours=35)


def test_horizon_accepts_list_of_points(site_config):
    series = prices.expand_to_blocks(hourly(2), site_config, T0)
    assert prices.horizon_end(list(series.values())) == T0 + timedelta(hours=2)


def test_horizon_empty_is_none_not_error(site_config):
    assert prices.horizon_end({}) is None
    assert prices.horizon_end([]) is None
    assert prices.expand_to_blocks([], site_config, T0) == {}


def test_expand_to_blocks_zoneinfo_start_on_fall_back_day_keeps_repeated_hour(site_config):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Brussels")
    start = datetime(2026, 10, 25, 0, 0, tzinfo=tz)
    raw = [
        {"time": "2026-10-25T02:00:00+02:00", "price": 0.12},
        {"time": "2026-10-25T02:00:00+01:00", "price": 0.13},
    ]
    pm = prices.expand_to_blocks(raw, site_config, start)
    assert len(pm) == 8  # 4 blocks per repeated hour, none collapsed
    by_utc = {k.astimezone(timezone.utc): v.market_price for k, v in pm.items()}
    first = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)   # 02:00+02:00
    assert [by_utc[first + timedelta(minutes=15 * i)] for i in range(4)] == [0.12] * 4
    assert [by_utc[first + timedelta(hours=1, minutes=15 * i)] for i in range(4)] == [0.13] * 4
    end = prices.horizon_end(pm, site_config.block_minutes)
    assert end.astimezone(timezone.utc) == first + timedelta(hours=2)


def test_expand_to_blocks_drops_blocks_before_zoneinfo_start_by_instant(site_config):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Brussels")
    start = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=tz)  # 01:30Z
    raw = [{"time": "2026-10-25T02:00:00+02:00", "price": 0.12},
           {"time": "2026-10-25T02:00:00+01:00", "price": 0.13}]
    pm = prices.expand_to_blocks(raw, site_config, start)
    assert len(pm) == 2  # only 02:30 and 02:45 (+01:00) survive


# --- the live ENTSO-e entity shape ------------------------------------------

LIVE_ENTRIES = [
    {"time": "2026-10-02 00:00:00+02:00", "price": 0.19622},
    {"time": "2026-10-02 00:15:00+02:00", "price": 0.19019},
]


def test_live_shape_space_separated_string_times(site_config):
    series = prices.expand_to_blocks(
        LIVE_ENTRIES, site_config,
        datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc))
    assert len(series) == 2
    first, second = sorted(series, key=lambda s: s.astimezone(timezone.utc))
    assert first.astimezone(timezone.utc) == datetime(
        2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    assert series[first].market_price == 0.19622
    assert series[second].market_price == 0.19019
    assert series[first].source_resolution_minutes == 15
    assert prices.horizon_end(series, 15) is not None


def test_time_formats_are_equivalent(site_config):
    start = datetime(2026, 10, 1, 21, 0, tzinfo=timezone.utc)
    tz2 = timezone(timedelta(hours=2))
    variants = [
        LIVE_ENTRIES,
        [{"time": "2026-10-02T00:00:00+02:00", "price": 0.19622},
         {"time": "2026-10-02T00:15:00+02:00", "price": 0.19019}],
        [{"time": datetime(2026, 10, 2, 0, 0, tzinfo=tz2), "price": 0.19622},
         {"time": datetime(2026, 10, 2, 0, 15, tzinfo=tz2), "price": 0.19019}],
    ]
    results = [prices.expand_to_blocks(v, site_config, start) for v in variants]

    def norm(r):
        return sorted((k.astimezone(timezone.utc), p.market_price)
                      for k, p in r.items())
    assert norm(results[0]) == norm(results[1]) == norm(results[2])
    assert len(norm(results[0])) == 2
