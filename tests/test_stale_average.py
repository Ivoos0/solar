"""A stale quarter-hour average must not become a false peak.

Right after a window boundary a sensor that has not published yet still shows
the previous window's value; the accumulating mode multiplies that by
15 / elapsed. The pure verdict is tested on capacity.py, the adapter through
the `env` fixture of test_battery_planner.
"""
from datetime import datetime, timedelta, timezone

import pytest

import capacity as cap
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    MONTH_PEAK_ENTITY, OFFTAKE_ENTITY, QUARTER_AVG_ENTITY, T0, env, fields_of)

UTC = timezone.utc
START = datetime(2026, 9, 30, 12, 30, tzinfo=UTC)


def at(seconds):
    return START + timedelta(seconds=seconds)


# ---- pure ---------------------------------------------------------------------

@pytest.mark.parametrize("stamp,now,expected", [
    (at(30), at(60), None),                  # written after the boundary
    (at(2), at(60), None),                   # exactly at the margin
    (at(1), at(60), "stale"),                # inside the margin: still last window
    (at(-40), at(60), "stale"),              # previous window
    (None, at(10), "unknown"),               # no timestamp, window just opened
    (None, at(15), None),                    # no timestamp, past the fallback
])
def test_average_staleness(stamp, now, expected):
    assert cap.average_staleness(stamp, now, START) == expected


@pytest.mark.parametrize("staleness,reported,elapsed,expected", [
    (None, 4.0, 0.5, "use"),                 # fresh is always used
    ("stale", 4.0, 0.5, "ignore"),           # early: the old value would be x30
    ("unknown", 0.1, 4.9, "ignore"),
    ("stale", 4.0, 5.0, "unusable"),         # later and high: cannot be trusted
    ("stale", 1.25, 8.0, "unusable"),        # exactly half the 2.5 floor
    ("stale", 1.2, 8.0, "use"),              # later and low: a quiet house
])
def test_stale_average_verdict(site_config, staleness, reported, elapsed, expected):
    assert cap.stale_average_verdict(
        staleness, reported, elapsed, site_config) == expected


# ---- adapter ---------------------------------------------------------------------

def spy_grid(env):
    seen = []
    real = env.mod.rules.decide

    def spy(traj, price_map, bat, grid, cfg, now, **kw):
        seen.append(grid)
        return real(traj, price_map, bat, grid, cfg, now, **kw)

    env.mod.rules.decide = spy
    return seen


def set_average(env, value, stamp):
    env.state.set(QUARTER_AVG_ENTITY, value, {"unit_of_measurement": "kW"},
                  last_reported=stamp)


EARLY = T0 - timedelta(minutes=4)            # 14:31, one minute into the window
LATE = T0 + timedelta(minutes=5)             # 14:40, ten minutes in
OLD = T0 - timedelta(minutes=6)              # 14:29, the previous window


def test_a_stale_value_early_in_the_window_counts_as_no_energy(env):
    seen = spy_grid(env)
    set_average(env, "0.4", OLD)             # 0.4 * 15 / 1 min = 6 kW if used
    env.run(EARLY)
    assert seen[0].running_average_kw == pytest.approx(0.0)
    assert seen[0].window_energy_kwh == pytest.approx(0.0)


def test_a_fresh_value_early_in_the_window_is_used(env):
    seen = spy_grid(env)
    set_average(env, "0.4", EARLY - timedelta(seconds=10) + timedelta(seconds=40))
    env.run(EARLY)
    assert seen[0].running_average_kw == pytest.approx(6.0)


def test_a_stale_high_value_later_makes_the_grid_state_unusable(env):
    seen = spy_grid(env)
    set_average(env, "2.0", OLD)
    env.run(LATE)
    assert seen[0] is None
    degraded = fields_of(env.decisions()[0])["degraded"]
    assert "quarter_hour_average_stale" in degraded
    assert "grid_sensors_unavailable" in degraded


def test_a_stale_low_value_later_is_used(env):
    seen = spy_grid(env)
    set_average(env, "0.3", OLD)
    env.run(LATE)
    assert seen[0] is not None
    assert "quarter_hour_average_stale" not in fields_of(
        env.decisions()[0])["degraded"]


def test_a_missing_timestamp_is_not_stale_past_the_fallback(env):
    seen = spy_grid(env)
    env.state.set(QUARTER_AVG_ENTITY, "2.0", {"unit_of_measurement": "kW"})
    env.run(LATE)
    assert seen[0] is not None
