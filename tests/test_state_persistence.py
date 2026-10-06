"""State that survives a Home Assistant restart.

Planner: the price-outage halt (no early re-alert). Guard: the peak-warning
send time. Command de-duplication has its own tests in
test_inverter.py (and the planner/guard wiring is checked at the end here).

A "restart" is simulated the way pyscript does it: module state is lost, files
stay. For the planner the module globals are reset to their import-time values;
for the guard a new module is loaded against the same state directory.
"""
import builtins
import json
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import capacity
from test_battery_planner import (  # noqa: F401  (env is a fixture)
    MONTH_PEAK_ENTITY, NOTIFY, OFFTAKE_ENTITY, PRICE_ENTITY, QUARTER_AVG_ENTITY,
    STEP, T0, env, fields_of)
from test_peak_guard import (  # noqa: F401  (make_guard is a fixture)
    PEAK_ARGS, make_guard)
from test_peak_warning import (  # noqa: F401
    FakeService, HIGH, go, two_ticks, warned)

TZ = ZoneInfo("Europe/Brussels")


def restart_planner(env):
    mod = env.mod
    mod._halt_state = None
    mod._halt_loaded = False
    mod._last_run = None
    mod._config = None
    mod._config_mtime = None


def alerts(env):
    return env.service.of("notify", NOTIFY)


def outage(env):
    env.state.set(PRICE_ENTITY, "unavailable", {})


# ---- planner: price-outage halt ------------------------------------------------

def test_halt_state_is_written_on_entry_and_after_the_alert(env):
    outage(env)
    env.run(T0)
    data = json.loads(env.halt_path.read_text(encoding="utf-8"))
    assert data["active"] is True
    assert data["entered_at"].startswith("2026-09-30T14:35:00")
    assert data["last_alert_at"].startswith("2026-09-30T14:35:00")
    assert data["cause"]


def test_restart_during_an_outage_does_not_alert_early(env):
    outage(env)
    env.run(T0)
    assert len(alerts(env)) == 1
    restart_planner(env)
    env.run(T0 + STEP)
    env.run(T0 + 2 * STEP)
    assert len(alerts(env)) == 1                     # realert is 60 min
    halts = [l for l in env.lines() if " | HALT | " in l]
    assert len(halts) == 3
    first, last = halts[0].split(" | "), halts[-1].split(" | ")
    assert last[3] == first[3] and last[3].startswith("entered=")  # same outage
    assert last[4] == first[4] and last[4].startswith("alerted=")
    assert not any("HALT, " in m for m in env.log.by_level["error"][1:])


def test_restart_during_an_outage_still_realerts_when_due(env):
    outage(env)
    env.run(T0)
    restart_planner(env)
    env.run(T0 + timedelta(minutes=65))
    assert len(alerts(env)) == 2


def test_restart_without_the_state_file_alerts_again(env):
    # Documents what the file buys: without it a restart re-alerts at once.
    outage(env)
    env.run(T0)
    env.halt_path.unlink()
    restart_planner(env)
    env.run(T0 + STEP)
    assert len(alerts(env)) == 2


def test_recovery_after_a_restart_reports_the_whole_outage(env):
    outage(env)
    env.run(T0)
    restart_planner(env)
    env.state.set(PRICE_ENTITY, "0.10", {"prices": __import__(
        "test_battery_planner").price_entries()})
    env.run(T0 + timedelta(minutes=20))
    rec = [l for l in env.lines() if " | RECOVERED | " in l]
    assert len(rec) == 1 and "halted_for=20m" in rec[0]
    assert json.loads(env.halt_path.read_text())["active"] is False
    restart_planner(env)
    env.run(T0 + timedelta(minutes=25))
    assert sum(" | RECOVERED | " in l for l in env.lines()) == 1


@pytest.mark.parametrize("content", [
    "", "garbage", "[]", "null", "{}", '{"active": true}',
    '{"active": true, "cause": "x", "entered_at": "soon", "last_alert_at": null}',
    '{"active": true, "cause": "x", "entered_at": "2026-09-30T14:35:00",'
    ' "last_alert_at": null}',
    '{"active": true, "cause": "a | b", "entered_at": "2026-09-30T14:35:00+02:00",'
    ' "last_alert_at": "never"}',
    '{"active": true, "cause": 5, "entered_at": "2026-09-30T14:35:00+02:00"}',
])
def test_corrupt_halt_file_starts_fresh_and_never_crashes(env, content):
    env.state_dir.mkdir(parents=True, exist_ok=True)
    env.halt_path.write_text(content, encoding="utf-8")
    outage(env)
    env.run(T0)
    halts = [l for l in env.lines() if " | HALT | " in l]
    assert len(halts) == 1
    assert len(alerts(env)) == 1                     # a fresh outage alerts
    assert json.loads(env.halt_path.read_text())["active"] is True   # healed


def test_stale_active_file_is_closed_when_prices_are_fine(env):
    outage(env)
    env.run(T0)
    restart_planner(env)
    env.state.set(PRICE_ENTITY, "0.10", {"prices": __import__(
        "test_battery_planner").price_entries()})
    env.run(T0 + STEP)
    assert len(env.decisions()) == 1
    assert json.loads(env.halt_path.read_text())["active"] is False


def test_unwritable_halt_state_is_logged_but_does_not_stop_the_halt(
        env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    env.mod.HALT_STATE_PATH = str(blocker / "halt.json")
    outage(env)
    env.run(T0)
    assert len(alerts(env)) == 1
    assert any("cannot save halt state" in m for m in env.log.by_level["error"])
    assert len([l for l in env.lines() if " | HALT | " in l]) == 1


def test_normal_cycles_write_no_halt_file(env):
    env.run(T0)
    env.run(T0 + STEP)
    assert not env.halt_path.exists()


# ---- guard: peak warning -------------------------------------------------------

@pytest.fixture
def svc(monkeypatch):
    service = FakeService()
    monkeypatch.setattr(builtins, "service", service, raising=False)
    return service


def _warn_guard(make_guard, svc, state):
    g = make_guard(state=state)
    g.svc = svc
    return g


def test_warning_send_time_is_saved_as_wall_clock(make_guard, svc):
    g = _warn_guard(make_guard, svc, "keep")
    two_ticks(g, **HIGH)
    assert len(warned(g)) == 1
    data = json.loads(open(g.mod.WARN_STATE_PATH).read())
    assert data["sent_window"].startswith("2026-09-30T12:00:00")
    assert data["sent_at"].startswith("2026-09-30T12:08:00")


def test_restart_in_the_same_window_does_not_warn_again(make_guard, svc):
    g1 = _warn_guard(make_guard, svc, "keep")
    two_ticks(g1, **HIGH)
    g2 = _warn_guard(make_guard, svc, "keep")
    two_ticks(g2, **HIGH)
    assert len(warned(g2)) == 1


def test_restart_respects_the_minimum_interval_in_a_later_window(
        make_guard, svc):
    g1 = _warn_guard(make_guard, svc, "keep")
    two_ticks(g1, **HIGH)                            # 12:08, interval 60 min
    g2 = _warn_guard(make_guard, svc, "keep")
    go(g2, 12, 22, 30, **HIGH)
    go(g2, 12, 23, 0, **HIGH)                        # next window, 15 min later
    assert len(warned(g2)) == 1
    g2.clock[0] += 3600.0                            # the monotonic clock follows
    go(g2, 13, 22, 30, **HIGH)
    go(g2, 13, 23, 0, **HIGH)                        # 75 min later: due again
    assert len(warned(g2)) == 2


def test_without_the_file_a_restart_warns_again(make_guard, svc):
    g1 = _warn_guard(make_guard, svc, "keep")
    two_ticks(g1, **HIGH)
    g2 = _warn_guard(make_guard, svc, "fresh")
    two_ticks(g2, **HIGH)
    assert len(warned(g2)) == 2


@pytest.mark.parametrize("content", [
    "", "junk", "null", "{}", '{"sent_window": "x", "sent_at": "y"}',
    '{"sent_window": "2026-09-30T12:00:00", "sent_at": "2026-09-30T12:08:00"}',
    '{"sent_window": 1, "sent_at": 2}'])
def test_corrupt_warning_file_means_warn_normally(make_guard, svc, content):
    import os
    g = _warn_guard(make_guard, svc, "bad")
    os.makedirs(os.path.dirname(g.mod.WARN_STATE_PATH), exist_ok=True)
    with open(g.mod.WARN_STATE_PATH, "w") as handle:
        handle.write(content)
    two_ticks(g, **HIGH)
    assert len(warned(g)) == 1


def test_a_failed_send_is_not_saved(make_guard, svc):
    import os
    svc.fail = True
    g = _warn_guard(make_guard, svc, "keep")
    two_ticks(g, **HIGH)
    assert not os.path.exists(g.mod.WARN_STATE_PATH)


def test_peak_warning_restore_converts_to_the_monotonic_clock():
    window = datetime(2026, 9, 30, 12, 0, tzinfo=TZ)
    mem = {"sent_window": window, "sent_at": 500.0}
    wall = datetime(2026, 9, 30, 12, 8, tzinfo=TZ)
    data = capacity.peak_warning_to_data(mem, wall)
    fresh = {}
    later = wall + timedelta(minutes=10)
    assert capacity.peak_warning_restore(fresh, data, later, 2000.0)
    assert fresh["sent_at"] == pytest.approx(2000.0 - 600.0)
    assert fresh["sent_window"] == window


def test_peak_warning_restore_never_goes_into_the_future():
    wall = datetime(2026, 9, 30, 12, 8, tzinfo=TZ)
    data = {"sent_window": wall.isoformat(), "sent_at": wall.isoformat()}
    mem = {}
    assert capacity.peak_warning_restore(
        mem, data, wall - timedelta(minutes=5), 100.0)   # clock went back
    assert mem["sent_at"] == 100.0


def test_peak_warning_to_data_needs_a_sent_warning():
    assert capacity.peak_warning_to_data({}, datetime.now(timezone.utc)) is None


# ---- wiring: planner and guard pass the de-dup arguments ------------------------

def test_planner_passes_the_resend_window_and_state_dir(env, monkeypatch):
    seen = {}
    real = env.mod.inverter.apply

    def spy(action, power, record, **kw):
        seen.update(kw)
        return real(action, power, record, **kw)
    monkeypatch.setattr(env.mod.inverter, "apply", spy)
    env.write_config("inverter:\n  resend_minutes: 7\n")
    env.run(T0)
    assert seen["resend_minutes"] == 7
    assert seen["state_dir"] == env.mod.STATE_DIR


def test_guard_passes_the_resend_window_and_state_dir(make_guard):
    g = make_guard(extra="inverter:\n  resend_minutes: 0\n").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.inv.kwargs["resend_minutes"] == 0
    assert g.inv.kwargs["state_dir"] == g.mod.STATE_DIR


def test_planner_passes_dry_run_to_the_boundary(env, monkeypatch):
    seen = {}
    real = env.mod.inverter.apply

    def spy(action, power, record, **kw):
        seen.update(kw)
        return real(action, power, record, **kw)
    monkeypatch.setattr(env.mod.inverter, "apply", spy)
    env.run(T0)
    assert seen["dry_run"] is False
    env.write_config("inverter:\n  dry_run: true\n")
    env.run(T0 + timedelta(minutes=5))
    assert seen["dry_run"] is True


def test_guard_passes_dry_run_to_the_boundary(make_guard):
    g = make_guard(extra="inverter:\n  dry_run: true\n").at(12, 7, 30)
    g.tick(**PEAK_ARGS)
    assert g.inv.kwargs["dry_run"] is True
