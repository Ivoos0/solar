"""Trajectory and battery tests. Every expectation is a hand-derived constant
(arithmetic in tests/fixtures/trajectory_cases.py); nothing is computed by
the code under test."""
from datetime import datetime, timedelta, timezone

import pytest

import battery
import prices
import series
import trajectory
from fixtures import trajectory_cases as fx

TOL = 1e-9


def run(case, price_map=None):
    state = battery.from_percent(case["start_percent"], case["cfg"])
    return trajectory.project(
        state, fx.solar_slots(case["solar"]), fx.usage_slots(case["usage"]),
        price_map if price_map is not None else {}, case["cfg"])


def idx_start(i):
    return None if i is None else fx.starts(i + 1)[i]


# ---- battery --------------------------------------------------------------

@pytest.mark.parametrize("pct,stored,headroom", [
    (0.0, 0.0, 10.0),           # empty
    (5.0, 0.5, 9.5),            # below the 10% reserve
    (10.0, 1.0, 9.0),           # exactly at reserve
    (50.0, 5.0, 5.0),
    (100.0, 10.0, 0.0),
])
def test_battery_derived_figures(site_config, pct, stored, headroom):
    b = battery.from_percent(pct, site_config)
    assert b.stored_kwh == pytest.approx(stored)
    assert b.headroom_kwh == pytest.approx(headroom)
    assert b.charge_percent == pct


def test_battery_stub_default_and_override(site_config):
    assert battery.from_percent(50, site_config).is_stubbed is True
    assert battery.from_percent(50, site_config, is_stubbed=False).is_stubbed is False


@pytest.mark.parametrize("bad", [-1, 100.5])
def test_battery_rejects_out_of_range(site_config, bad):
    with pytest.raises(ValueError):
        battery.from_percent(bad, site_config)


# ---- golden fixtures ------------------------------------------------------

@pytest.mark.parametrize("name", sorted(fx.ALL))
def test_golden_headlines(name):
    case = fx.ALL[name]
    t = run(case)
    assert t.saturation_block == idx_start(case["saturation_index"])
    assert t.reserve_breach_block == idx_start(case["breach_index"])
    assert t.total_spill_kwh == pytest.approx(case["total_spill"], abs=TOL)
    assert t.leftover_kwh == pytest.approx(case["leftover"], abs=TOL)
    assert t.blocks[-1].projected_charge_kwh == pytest.approx(case["end_charge"], abs=TOL)
    assert len(t.blocks) == len(case["solar"])


@pytest.mark.parametrize("name", sorted(fx.ALL))
def test_energy_conservation_every_block(name):
    case = fx.ALL[name]
    cap = case["cfg"].capacity_kwh
    reserve = cap * case["cfg"].reserve_percent / 100
    for b in run(case).blocks:
        assert b.solar_kwh == pytest.approx(
            b.usage_covered_kwh + b.absorbed_kwh + b.spilled_kwh, abs=TOL)
        assert b.usage_kwh == pytest.approx(
            b.usage_covered_kwh + b.discharged_kwh + b.grid_shortfall_kwh, abs=TOL)
        assert b.projected_charge_kwh <= cap + TOL
        assert b.projected_charge_kwh >= reserve - TOL


def test_charge_continuity_matches_flows():
    case = fx.SUNNY_SATURATES
    prev = case["cfg"].capacity_kwh * case["start_percent"] / 100
    for b in run(case).blocks:
        assert b.projected_charge_kwh == pytest.approx(
            prev + b.absorbed_kwh - b.discharged_kwh, abs=TOL)
        prev = b.projected_charge_kwh


def test_sunny_saturates_block_detail():
    b = run(fx.SUNNY_SATURATES).blocks
    # b2: surplus 2.0, headroom 4.0-3.0 = 1.0 -> absorb 1.0, spill 1.0
    assert b[2].absorbed_kwh == pytest.approx(1.0)
    assert b[2].spilled_kwh == pytest.approx(1.0)
    assert b[2].projected_charge_kwh == pytest.approx(4.0)


def test_power_limited_spills_without_saturating():
    case = fx.POWER_LIMITED
    t = run(case)
    assert t.total_spill_kwh > 0
    assert t.saturation_block is None
    # 3.0 kWh surplus, 4 kW * 0.25 h = 1.0 kWh cap: absorb 1.0, spill 2.0
    assert t.blocks[2].absorbed_kwh == pytest.approx(1.0)
    assert t.blocks[2].spilled_kwh == pytest.approx(2.0)
    assert t.blocks[2].projected_charge_kwh < case["cfg"].capacity_kwh - 10


def test_winter_breach_grid_shortfall_recorded():
    case = fx.WINTER_BREACH
    t = run(case)
    assert sum(b.grid_shortfall_kwh for b in t.blocks) == pytest.approx(case["total_shortfall"])
    assert sum(b.discharged_kwh for b in t.blocks) == pytest.approx(case["total_discharged"])
    # b0: need 1.5, power limit 4 kW*0.25 = 1.0 -> shortfall 0.5 despite charge
    assert t.blocks[0].discharged_kwh == pytest.approx(1.0)
    assert t.blocks[0].grid_shortfall_kwh == pytest.approx(0.5)
    assert t.total_spill_kwh == 0.0


def test_saturate_then_drain_reports_first_crossing():
    t = run(fx.SATURATE_THEN_DRAIN)
    assert t.saturation_block == fx.starts(2)[1]
    assert t.saturation_block != fx.starts(5)[4]


def test_idle_block_leaves_charge_unchanged():
    case = dict(fx.BALANCED, solar=[0.0] * 3, usage=[0.0] * 3)
    t = run(case)
    assert [b.projected_charge_kwh for b in t.blocks] == [5.0, 5.0, 5.0]


def test_starting_below_reserve_is_not_lifted():
    # 5% of 10 = 0.5 kWh, floor 1.0. Usage 0.4/block, no solar: nothing to
    # discharge, all usage is grid shortfall, charge stays 0.5 (no free energy).
    case = dict(fx.WINTER_BREACH, start_percent=5.0, solar=[0.0] * 2, usage=[0.4] * 2)
    t = run(case)
    assert all(b.projected_charge_kwh == pytest.approx(0.5) for b in t.blocks)
    assert sum(b.grid_shortfall_kwh for b in t.blocks) == pytest.approx(0.8)
    assert t.reserve_breach_block == fx.starts(1)[0]
    assert t.leftover_kwh == 0.0


def test_start_full_saturates_immediately():
    case = dict(fx.BALANCED, start_percent=100.0)
    t = run(case)
    assert t.saturation_block == fx.starts(1)[0]


def test_tolerance_within_1e_9_counts_as_saturated():
    # cap 4.0, start 3.0, surplus 1.0 - 1e-9 -> charge 4.0 - 1e-9: saturated
    case = dict(fx.SUNNY_SATURATES, start_percent=75.0,
                solar=[1.5 - 1e-9, 0.5], usage=[0.5, 0.5])
    t = run(case)
    assert t.saturation_block == fx.starts(1)[0]
    # 1e-3 short is NOT saturated
    case = dict(case, solar=[1.5 - 1e-3, 0.5])
    assert run(case).saturation_block is None


# ---- joins, horizon, errors -----------------------------------------------

def test_missing_usage_entry_raises():
    case = fx.BALANCED
    state = battery.from_percent(50, case["cfg"])
    usage = fx.usage_slots(case["usage"])
    del usage[3]
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(state, fx.solar_slots(case["solar"]), usage, {}, case["cfg"])


def test_usage_joined_by_start_not_position():
    case = fx.BALANCED
    state = battery.from_percent(50, case["cfg"])
    usage = list(reversed(fx.usage_slots(case["usage"])))
    t = trajectory.project(state, fx.solar_slots(case["solar"]), usage, {}, case["cfg"])
    assert [b.usage_kwh for b in t.blocks] == case["solar"]


def test_price_gap_blocks_projected_unshifted():
    cfg = fx.PRICE_GAP_CFG
    pm = prices.expand_to_blocks(fx.PRICE_GAP_RAW, cfg, fx.START)
    state = battery.from_percent(50, cfg)
    t = trajectory.project(state, fx.solar_slots(fx.PRICE_GAP_SOLAR),
                           fx.usage_slots(fx.PRICE_GAP_USAGE), pm, cfg)
    assert len(t.blocks) == 16
    for i, b in enumerate(t.blocks):
        assert b.block_start == fx.starts(16)[i]
        assert b.solar_kwh == pytest.approx(0.1 * (i + 1))   # own solar
        assert b.usage_kwh == pytest.approx(0.05 * (i + 1))  # own usage
        expected = fx.PRICE_GAP_EXPECTED_MARKET[i]
        assert b.has_price == (expected is not None)
        if expected is not None:
            assert pm[b.block_start].market_price == expected
    assert [b.has_price for b in t.blocks[8:12]] == [False] * 4
    assert all(b.has_price for b in t.blocks[:8] + t.blocks[12:])
    # last priced block starts 13:45, so the horizon ends 14:00
    assert t.horizon_end == fx.START + timedelta(hours=4)


# Horizon regressions. Config: capacity 50, 40 kW both ways (nothing clamps),
# start 50% = 25.0, reserve 10% = 5.0. Prices (price_gap) end at 14:00 -> 16 blocks.

def _gap_prices():
    cfg = fx.PRICE_GAP_CFG
    return cfg, prices.expand_to_blocks(fx.PRICE_GAP_RAW, cfg, fx.START)


def test_short_solar_is_padded_to_price_horizon_by_project():
    cfg, pm = _gap_prices()
    # 4 solar blocks of 0.4 (1.6 total), usage 1.0 x 16 = 16.0, NO manual pad.
    # charge = 25.0 + 1.6 - 16.0 = 10.6 -> leftover 10.6 - 5.0 = 5.6.
    t = trajectory.project(battery.from_percent(50, cfg),
                           fx.solar_slots([0.4] * 4), fx.usage_slots([1.0] * 16), pm, cfg)
    assert len(t.blocks) == 16
    assert t.horizon_end == fx.START + timedelta(hours=4)
    assert t.blocks[-1].block_start == t.horizon_end - timedelta(minutes=15)
    assert all(b.solar_kwh == 0.0 for b in t.blocks[4:])
    assert t.blocks[-1].projected_charge_kwh == pytest.approx(10.6)
    assert t.leftover_kwh == pytest.approx(5.6)
    assert t.reserve_breach_block is None


def test_short_solar_reaches_breach_inside_padded_region():
    cfg, pm = _gap_prices()
    # usage 2.0 x 16, solar 0.4 x 4: b0..b3 net -1.6 each -> 25 - 6.4 = 18.6;
    # then -2.0 per block: b4 16.6, b5 14.6, b6 12.6, b7 10.6, b8 8.6, b9 6.6;
    # b10 has only 6.6 - 5.0 = 1.6 available -> discharges 1.6 -> 5.0 = floor:
    # breach at index 10, shortfall 0.4, leftover 0.
    t = trajectory.project(battery.from_percent(50, cfg),
                           fx.solar_slots([0.4] * 4), fx.usage_slots([2.0] * 16), pm, cfg)
    assert t.reserve_breach_block == fx.starts(16)[10]
    assert t.blocks[10].grid_shortfall_kwh == pytest.approx(0.4)
    assert t.leftover_kwh == 0.0


def test_long_solar_is_truncated_at_horizon_end():
    cfg, pm = _gap_prices()
    # 24 solar blocks of 0.5, usage 1.0 x 16 -> only 16 projected:
    # 25 + 16 * (0.5 - 1.0) = 17.0 ; leftover 12.0 ; nothing past 14:00.
    t = trajectory.project(battery.from_percent(50, cfg),
                           fx.solar_slots([0.5] * 24), fx.usage_slots([1.0] * 16), pm, cfg)
    assert len(t.blocks) == 16
    assert t.blocks[-1].block_start == fx.START + timedelta(hours=4) - timedelta(minutes=15)
    assert t.blocks[-1].projected_charge_kwh == pytest.approx(17.0)
    assert t.leftover_kwh == pytest.approx(12.0)


def test_hole_in_solar_series_is_zero_not_skipped():
    cfg, pm = _gap_prices()
    solar = fx.solar_slots([1.0] * 16)
    del solar[3]
    # usage 1.0: 15 blocks net 0, the hole block draws 1.0 -> 25 - 1 = 24.0
    t = trajectory.project(battery.from_percent(50, cfg), solar,
                           fx.usage_slots([1.0] * 16), pm, cfg)
    assert len(t.blocks) == 16
    assert t.blocks[3].solar_kwh == 0.0
    assert t.blocks[3].projected_charge_kwh == pytest.approx(24.0)
    assert t.leftover_kwh == pytest.approx(19.0)


def test_usage_beyond_series_end_raises():
    cfg, pm = _gap_prices()
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(battery.from_percent(50, cfg), fx.solar_slots([1.0] * 16),
                           fx.usage_slots([1.0] * 15), pm, cfg)


def test_usage_hole_raises():
    cfg, pm = _gap_prices()
    usage = fx.usage_slots([1.0] * 16)
    del usage[5]
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(battery.from_percent(50, cfg), fx.solar_slots([1.0] * 16),
                           usage, pm, cfg)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1])
@pytest.mark.parametrize("which", ["solar", "usage"])
def test_non_finite_or_negative_energy_raises(bad, which):
    cfg = fx.site(10.0, 5.0, 5.0)
    solar, usage = [0.1, 0.1], [0.1, 0.1]
    (solar if which == "solar" else usage)[1] = bad
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(battery.from_percent(50, cfg), fx.solar_slots(solar),
                           fx.usage_slots(usage), {}, cfg)


def test_empty_input_raises():
    cfg = fx.site(10.0, 5.0, 5.0)
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(battery.from_percent(50, cfg), [], [], {}, cfg)


def test_explicit_start_time_pads_leading_solar():
    cfg = fx.site(10.0, 5.0, 5.0)
    st = fx.START - timedelta(minutes=30)
    t = trajectory.project(battery.from_percent(50, cfg), fx.solar_slots([0.1] * 2),
                           [series.UsageSlot(st + i * fx.STEP, 0.1, 7) for i in range(4)],
                           {}, cfg, start_time=st)
    assert len(t.blocks) == 4
    assert t.blocks[0].block_start == st and t.blocks[0].solar_kwh == 0.0


# ---- DST (Europe/Brussels) ------------------------------------------------

UTC = timezone.utc


def _utc_day(y, m, d):
    """UTC-stepped block instants covering local day y-m-d, built independently
    of series._grid."""
    start = datetime(y, m, d, tzinfo=fx.TZ).astimezone(UTC)
    nxt = datetime(y, m, d) + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=fx.TZ).astimezone(UTC)
    out = []
    t = start
    while t < end:
        out.append(t)
        t += fx.STEP
    return out


def _hourly_prices(instants):
    raw = []
    for k, t in enumerate(instants[::4]):
        loc = t.astimezone(fx.TZ)
        raw.append({"time": loc.astimezone(timezone(loc.utcoffset())).isoformat(),
                    "price": round(0.10 + 0.01 * k, 2)})
    return raw


def _dst_run(y, m, d):
    instants = _utc_day(y, m, d)
    cfg = fx.site(10.0, 5.0, 5.0)
    n = len(instants)
    local = [t.astimezone(fx.TZ) for t in instants]
    # solar rises through the day (unique per block), usage constant
    solar = [series.ForecastSlot(l, 0.001 * i, False, 60) for i, l in enumerate(local)]
    usage = list(reversed([series.UsageSlot(l, 0.05, 7) for l in local]))
    pm = prices.expand_to_blocks(_hourly_prices(instants), cfg, local[0])
    t = trajectory.project(battery.from_percent(50, cfg), solar, usage, pm, cfg)
    return t, instants, pm, n


def test_fall_back_day_has_100_distinct_blocks_each_with_own_price():
    t, instants, pm, n = _dst_run(2026, 10, 25)
    assert n == 100 and len(t.blocks) == 100
    assert [b.block_start.astimezone(UTC) for b in t.blocks] == instants
    assert len({b.block_start.astimezone(UTC) for b in t.blocks}) == 100
    assert all(b.has_price for b in t.blocks)
    # repeated hour: local 02:00-02:45 occurs at +02:00 (hour k=2) then +01:00 (k=3)
    first = [b for b in t.blocks if b.block_start.hour == 2
             and b.block_start.utcoffset() == timedelta(hours=2)]
    second = [b for b in t.blocks if b.block_start.hour == 2
              and b.block_start.utcoffset() == timedelta(hours=1)]
    assert len(first) == len(second) == 4
    for grp, price in ((first, 0.12), (second, 0.13)):
        for b in grp:
            key = b.block_start.astimezone(timezone(b.block_start.utcoffset()))
            assert pm[key].market_price == price
    # usage input was reversed; per-block solar 0.001*i is unique, so order holds
    assert [b.solar_kwh for b in t.blocks] == pytest.approx(
        [0.001 * i for i in range(100)])
    assert t.horizon_end.astimezone(UTC) == instants[-1] + fx.STEP


def test_fall_back_day_energy_conservation():
    t = _dst_run(2026, 10, 25)[0]
    for b in t.blocks:
        assert b.solar_kwh == pytest.approx(
            b.usage_covered_kwh + b.absorbed_kwh + b.spilled_kwh, abs=TOL)
        assert b.usage_kwh == pytest.approx(
            b.usage_covered_kwh + b.discharged_kwh + b.grid_shortfall_kwh, abs=TOL)
    # usage 0.05 x 100 = 5.0, solar 0.001*i. Start 5.0 kWh, nothing clamps.
    # i=0..50 (solar <= 0.05): deficit sum = 51*0.05 - 0.001*1275 = 1.275
    # i=51..99: surplus sum = 0.001*3675 - 49*0.05 = 1.225
    # end charge = 5.0 - 1.275 + 1.225 = 4.95
    assert t.blocks[-1].projected_charge_kwh == pytest.approx(4.95)


def test_spring_forward_day_has_92_valid_blocks():
    t, instants, pm, n = _dst_run(2026, 3, 29)
    assert n == 92 and len(t.blocks) == 92
    assert all(b.has_price for b in t.blocks)
    assert not any(b.block_start.hour == 2 for b in t.blocks)  # 02:xx does not exist
    for b in t.blocks:
        assert b.block_start.astimezone(UTC).astimezone(fx.TZ).replace(
            tzinfo=None) == b.block_start.replace(tzinfo=None)
    assert len({b.block_start.astimezone(UTC) for b in t.blocks}) == 92


def test_duplicate_or_negative_input_raises():
    cfg = fx.site(10.0, 5.0, 5.0)
    state = battery.from_percent(50, cfg)
    s = fx.solar_slots([0.1, 0.1])
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(state, s + [s[0]], fx.usage_slots([0.1, 0.1]), {}, cfg)
    with pytest.raises(trajectory.TrajectoryError):
        trajectory.project(state, fx.solar_slots([-0.1]), fx.usage_slots([0.1]), {}, cfg)


def test_projection_is_recomputed_from_live_charge():
    case = fx.SUNNY_SATURATES
    a = run(case)
    b = run(dict(case, start_percent=100.0))
    assert a is not b
    assert a.blocks[0].projected_charge_kwh != b.blocks[0].projected_charge_kwh


# ---- misaligned solar and ZoneInfo-keyed prices -----------------------------

def _aligned_inputs(n=4):
    cfg = fx.site(10.0, 5.0, 5.0)
    return cfg, battery.from_percent(50, cfg), fx.usage_slots([0.1] * n)


def test_off_grid_solar_block_raises_instead_of_being_dropped():
    cfg, state, usage = _aligned_inputs()
    solar = fx.solar_slots([0.2] * 4)
    solar[2] = series.ForecastSlot(solar[2].block_start + timedelta(minutes=7),
                                   0.2, False, 60)
    with pytest.raises(trajectory.TrajectoryError, match="off the 15-minute grid"):
        trajectory.project(state, solar, usage, {}, cfg)


def test_off_grid_solar_raises_against_an_offset_start_time():
    cfg, state, usage = _aligned_inputs()
    with pytest.raises(trajectory.TrajectoryError, match="off the 15-minute grid"):
        trajectory.project(state, fx.solar_slots([0.2] * 4), usage, {}, cfg,
                           start_time=fx.START + timedelta(minutes=5))


def test_solar_outside_the_window_is_still_ignored_not_rejected():
    # documented: entries before start_time / at or beyond horizon_end are
    # ignored, even when misaligned
    cfg, state, usage = _aligned_inputs()
    solar = fx.solar_slots([0.2] * 4)
    solar.append(series.ForecastSlot(fx.START + timedelta(hours=9, minutes=7),
                                     0.2, False, 60))
    solar.append(series.ForecastSlot(fx.START - timedelta(minutes=8),
                                     0.2, False, 60))
    pm = {t: None for t in fx.starts(4)}        # prices fix the horizon at 4 blocks
    t = trajectory.project(state, solar, usage, pm, cfg, start_time=fx.START)
    assert len(t.blocks) == 4 and all(b.solar_kwh == 0.2 for b in t.blocks)


def _zone_priced(instants, skip=()):
    """price_map keyed by ZoneInfo-aware datetimes (what a naive caller builds)."""
    return {t.astimezone(fx.TZ): prices.PricePoint(t.astimezone(fx.TZ), 0.1,
                                                   0.2, 0.05, 15)
            for t in instants if t not in skip}


def _zone_day(y, m, d, price_map_of):
    instants = _utc_day(y, m, d)
    cfg = fx.site(10.0, 5.0, 5.0)
    local = [t.astimezone(fx.TZ) for t in instants]
    solar = [series.ForecastSlot(l, 0.001 * i, False, 60) for i, l in enumerate(local)]
    usage = list(reversed([series.UsageSlot(l, 0.05, 7) for l in local]))
    t = trajectory.project(battery.from_percent(50, cfg), solar, usage,
                           price_map_of(instants), cfg)
    return t, instants


def test_zoneinfo_price_keys_spring_forward_day_join_all_92_blocks():
    t, instants = _zone_day(2026, 3, 29, _zone_priced)
    assert len(t.blocks) == 92
    assert [b.block_start.astimezone(UTC) for b in t.blocks] == instants
    assert all(b.has_price for b in t.blocks)
    assert [b.solar_kwh for b in t.blocks] == pytest.approx(
        [0.001 * i for i in range(92)])


def test_zoneinfo_price_keys_fall_back_day_repeated_hour_dict_collapse():
    # A dict keyed by ZoneInfo datetimes cannot hold both occurrences of the
    # repeated hour (PEP 495: equal + same hash), so only the first (+02:00)
    # survives. Every block must still be projected (100) and the join must be
    # by instant: exactly the four second-occurrence blocks are unpriced.
    t, instants = _zone_day(2026, 10, 25, _zone_priced)
    assert len(t.blocks) == 100 and len(_zone_priced(instants)) == 96
    unpriced = [b for b in t.blocks if not b.has_price]
    assert len(unpriced) == 4
    assert all(b.block_start.hour == 2
               and b.block_start.utcoffset() == timedelta(hours=1)
               for b in unpriced)


def test_zoneinfo_price_keys_fall_back_day_all_100_join_by_instant():
    # project() only iterates price_map keys; a key list keeps all 100
    # ZoneInfo datetimes (fold 0 and 1 of the repeated hour). A join on aware
    # equality would see 96 and leave four blocks unpriced.
    t, instants = _zone_day(
        2026, 10, 25, lambda ins: [x.astimezone(fx.TZ) for x in ins])
    assert len(t.blocks) == 100
    assert all(b.has_price for b in t.blocks)
    assert [b.block_start.astimezone(UTC) for b in t.blocks] == instants
    assert t.horizon_end.astimezone(UTC) == instants[-1] + fx.STEP
