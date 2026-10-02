"""Peak guard: evaluate the capacity-tariff shave (S0) every ~30 s.

A pyscript top-level SCRIPT. It is deliberately narrow (FR-051, NFR-010,
NFR-011): three meter numbers, WP11 arithmetic, the V1 reserve veto and the
inverter boundary. No trajectory, no prices, no series, no cache, no forecast.

THE TWO LOOPS AND WHICH WINS (read before changing either)
----------------------------------------------------------
The five-minute planner (battery_planner.py) and this guard both command the
battery. Pyscript scripts cannot import each other, so they coordinate through
one Home Assistant state entity:

    pyscript.peak_guard_shaving      "on" while the guard is shaving a peak,
                                     "off" otherwise. Attributes: window_start,
                                     shave_kw, since.

THE GUARD WINS. While the entity is "on" the planner must treat any grid-charge
proposal as vetoed (FR-050 expressed across two processes). Scenario: a cheap
hour makes the planner want a grid charge, the household load steps up and a
peak starts forming. Guard: shave_kw > 0, entity goes "on", discharge recorded.
Planner (next cycle): sees "on", suppresses grid charging. Shave ends (the
window rolls over or the load drops): entity goes "off", planner resumes on
its next cycle. Budget arithmetic (V3) normally prevents the clash first; the
entity is the belt to that braces because the guard has fresher numbers.
The entity is cleared on stop, when capacity is disabled, and when the meter
has been unreadable for GRACE_SECONDS (a dead sensor must not freeze the
planner out of charging).

DOCUMENTED READINGS / DEVIATIONS FROM THE WP TEXT
-------------------------------------------------
* Sensor: offtake is the meter NETTED total (config.offtake_sensor, default
  sensor.slimmelezer_power_consumed). Per-phase sensors are never read or
  summed (C-012: a real sample had per-phase imports summing to 0.937 kW where
  the meter said 0.003 kW).
* Units: the unit_of_measurement attribute of all three sensors is read. "kW"
  is used as is, "W" is divided by 1000, anything else (or no attribute) makes
  the reading unreadable: logged (rate limited), no action.
* Stale quarter-hour average: at a window boundary the average entity can still
  hold the PREVIOUS window's value (the state trigger fires on the offtake
  sensor; ESPHome publishes a telegram's sensors one after another). Freshness
  comes from the StateVal that state.get(entity) returns: pyscript sets
  .last_reported, .last_updated and .last_changed on it. They are NOT in the
  dict returned by state.getattr (pyscript strips STATE_VIRTUAL_ATTRS), so
  getattr is used for unit_of_measurement only. .last_reported is preferred
  (HA >= 2024.3): HA does not bump last_updated when a sensor rewrites an
  identical state, so a genuinely current but unchanged average would look
  stale; last_reported moves on every write. Older HA: .last_updated. Values
  may be datetime or ISO string; naive = UTC. The stamp must be at or after
  the current window start PLUS GUARD_BOUNDARY_MARGIN_S (2 s), otherwise the
  tick is a no-op with one warning per window. The margin covers clock skew:
  if the meter (ESPHome) clock runs a few seconds behind HA, the previous
  window's average could be rewritten just after HA's boundary and carry a
  "fresh" stamp. ASSUMPTION: the meter rewrites the average after each window
  boundary, and any skew between meter and HA is under
  GUARD_BOUNDARY_MARGIN_S. The margin applies only when a stamp exists; the
  no-timestamp fallback below is unchanged. If neither timestamp exists (nothing measured) the average is
  ignored for the first STALE_FALLBACK_SECONDS (15 s) of every window, with no
  warning. A stale value NEVER produces a discharge.
* The billed 13-month average sensor is NOT read: GridState has no field for it
  and costing belongs to the planner.
* Triggers: @state_trigger names the default offtake sensor literally
  (decorator arguments are static); a changed config.offtake_sensor changes
  what is READ but not what triggers, the 30 s tick still covers it.
  @time_trigger period is fixed at 30 s; guard_interval_seconds > 30 is honoured
  by skipping time ticks that arrive sooner than the interval. State-triggered
  runs are only debounced (MIN_STATE_GAP_SECONDS).
* Logging volume (NEVER one line per tick). Decision-log records are written
  only when: a shave starts; the shave power moves by >= SHAVE_CHANGE_KW; a
  shave is vetoed (once per window); a shave stops. A steady shave therefore
  yields ONE record, not one per tick. Unreadable-sensor and error warnings go
  to the HA log at most once per WARN_EVERY_SECONDS per kind.
* Record fields the guard cannot know (no trajectory): forecast_remaining,
  usage_remaining and spill are None and render n/a (an explicit "not
  evaluated", never a projected 0.00kWh), saturation and breach render
  "none", end_soc is the CURRENT stored kWh (same fallback decision.build uses
  with no blocks), cons/inj render n/a (no prices). why says "no trajectory".
  avg/ceiling/budget are real (capacity.*). degraded carries soc_stubbed and,
  when relevant, avg_mode_assumed / meter_restored. The detected average mode
  and its confidence are stated in why (FR-058).
* Vetoes: only vetoes that forbid "discharge" BLOCK a shave, i.e. V1. V3
  (budget <= 0) will normally be fired in a peak but forbids only grid
  charging, so it blocks nothing; it is still RENDERED, bare, in the vetoes
  field of shave and stop records (same as the planner: decision.render_vetoes
  over the fired list), e.g. vetoes=V3. V2 needs a price: none here. A vetoed
  shave is recorded as action=idle, selector S0, with
  vetoes=V1(suppressed S0 discharge) (plus any bare fired ones, e.g. ,V3).
* Cancellation (@task_unique default kill_me=False: a new trigger KILLS the
  running task, so a hung run can never blind the guard). A kill can land at
  the await inside inverter.apply. Therefore module state and
  pyscript.peak_guard_shaving are updated BEFORE the record is applied. Worst
  case a killed run loses one log line; it can never leave the planner flag
  lagging or make the next run write a duplicate start/stop record.
* Reload / errors: on the first run after import the entity is forced to "off"
  before anything else (module state is lost on reload, the entity is not). An
  unknown module state (None) is treated like "maybe shaving" by the grace
  logic. An exception in a tick clears the flag after the same GRACE_SECONDS.
* FUTURE RISK (not implemented this mission): the guard reads NET offtake,
  which its own discharge lowers. Before real transmission exists the
  commanded discharge power must be added back, or the shave will bang-bang
  on/off; a discharge command probably also needs a heartbeat re-send, while
  today records are written only on change.
* Average-mode detection needs votes from several windows, yet samples from
  different windows must not be compared. The per-window buffer is discarded
  at every window boundary as required; before it is, its (at most two)
  detector-relevant samples are archived to a bounded history so the detector
  can accumulate its DETECT_MIN_LEAD votes. Nothing else crosses a boundary.
* NATIVE CORE LOADER (same pattern as battery_planner.py, WP10). Files under
  pyscript/modules/ are *pyscript* modules, run by pyscript's AST interpreter,
  which lacks generator expressions, @property, native callbacks to pyscript
  functions and validating __post_init__; the core uses all of them, so it must
  run as ordinary CPython. _load_core (a @pyscript_executor helper) loads
  config, capacity, battery, rules and decision with importlib under the
  private names peak_guard_core_<name>; no sys.path entry is added. They are
  ALSO registered under their bare names for the process lifetime because core
  code imports siblings at call time (rules: `import capacity`) and the core
  directory is not on sys.path in HA. Trade-off: those five bare names resolve
  to the core for every other importer in the HA process; `inverter` is never
  aliased and stays a normal pyscript import. SHARING WITH THE PLANNER: both
  scripts live in one HA process. If a bare name already points at the same
  file (registered by the planner, or by an earlier load) it is REUSED, never
  reloaded, so both scripts share one copy; if the guard loads first, the
  planner later loads its own copies and re-points the bare names, which is
  harmless because each script's modules are internally consistent. A failed
  load undoes its aliases. Natively loaded modules are NOT hot-reloaded: a
  change to a core file needs a Home Assistant restart. Only this file and
  pyscript/modules/inverter.py are interpreted; a test lints this file.
* Config is loaded with a ~20-line copy of the planner adapter loader (WP10).
  Duplication is accepted for now; keep the two in step.
* All file I/O goes through @pyscript_executor helpers (research.md R-02).
"""
import math
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import inverter          # the only core file pyscript interprets

# Pure core, bound natively by _ensure_core() (see the NATIVE CORE LOADER note).
battery = capacity = decision = rules = site_config = None

# ---- constants (tests point these at tmp_path / patch them) ----------------
CONFIG_PATH = "/config/battery_planner/user_config.yaml"
CORE_DIR = "/config/pyscript/modules"
# Dependency order (rules needs capacity). Deliberately narrow: never
# trajectory, prices, series or cache (FR-051). inverter is NOT in this list.
CORE_MODULES = ("config", "capacity", "battery", "rules", "decision")
ENTITY_QUARTER_AVG = "sensor.slimmelezer_huidig_kwartiervermogen"
ENTITY_MONTH_PEAK = "sensor.slimmelezer_maandpiek"
SHAVING_ENTITY = "pyscript.peak_guard_shaving"

MIN_STATE_GAP_SECONDS = 1.0
GRACE_SECONDS = 120.0
WARN_EVERY_SECONDS = 600.0
SHAVE_CHANGE_KW = 0.25
# 48 archived samples = 24 windows (two detector samples are kept per window)
MAX_HISTORY_SAMPLES = 48
STALE_FALLBACK_SECONDS = 15.0
# A stamp must be this far past the window start to count as fresh (meter
# clock skew; see the docstring). Only used when a timestamp exists.
GUARD_BOUNDARY_MARGIN_S = 2
_UNIT_FACTORS = {"kW": 1.0, "W": 0.001}
_BAD = ("unavailable", "unknown", "none", "")

# ---- module state (mutated in place; pyscript keeps it between triggers) ---
_samples = []      # current-window detector samples (reset at each boundary)
_history = []      # archived detector samples of closed windows (bounded)
_core_ready = False
_cfg = {"mtime": None, "config": None, "zone": None, "zone_name": None}
_flags = {
    "window": None,        # window_start the _samples belong to
    "shaving": None,       # None = unknown (fresh start), True, False
    "shave_kw": 0.0,       # power of the last recorded shave
    "since": None,
    "last_run": -1.0e9,
    "unreadable_since": None,
    "gap": False,          # meter was unreadable and has just recovered
    "vetoed_window": None,
    "warned": {},
    "primed": False,       # entity reconciled to "off" since import/reload
    "inverter_marker": None,  # degraded marker from the last charge reading
    "stale_window": None,  # window a stale-average warning was already logged
}


# ---- I/O helpers: the ONLY places that touch files -------------------------
# @pyscript_executor (not merely @pyscript_compile) so the blocking work runs
# in a worker thread, off the HA event loop (research.md R-02).

@pyscript_executor  # noqa: F821
def _load_core(core_dir, names):
    """Import the pure core as CPython modules. Returns {name: module}."""
    import importlib.util
    import os
    import sys
    loaded, saved = {}, {}
    try:
        for name in names:
            path = os.path.join(core_dir, name + ".py")
            full = "peak_guard_core_" + name
            module = None
            for candidate in (sys.modules.get(name), sys.modules.get(full)):
                cfile = getattr(candidate, "__file__", None)
                if cfile and os.path.realpath(cfile) == os.path.realpath(path):
                    module = candidate      # same file already loaded: share it
                    break
            if module is None:
                spec = importlib.util.spec_from_file_location(full, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[full] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(full, None)
                    raise
            loaded[name] = module
            # Bare alias for the process lifetime (call-time sibling imports).
            saved.setdefault(name, sys.modules.get(name))
            sys.modules[name] = module
    except BaseException:
        for bare, previous in saved.items():     # failed load: undo aliases
            if previous is None:
                sys.modules.pop(bare, None)
            else:
                sys.modules[bare] = previous
        raise
    return loaded


def _ensure_core():
    """Bind the natively loaded core modules as this file's globals (once)."""
    global _core_ready, battery, capacity, decision, rules, site_config
    if _core_ready:
        return
    mods = _load_core(CORE_DIR, CORE_MODULES)
    battery, capacity, decision = (mods["battery"], mods["capacity"],
                                   mods["decision"])
    rules, site_config = mods["rules"], mods["config"]
    _core_ready = True


@pyscript_executor  # noqa: F821
def _load_yaml_if_changed(path, known_mtime):
    """Return (mtime, parsed) or (mtime, None) when the file is unchanged."""
    import os
    import yaml
    mtime = os.stat(path).st_mtime
    if known_mtime is not None and mtime == known_mtime:
        return mtime, None
    with open(path, encoding="utf-8") as handle:
        return mtime, yaml.safe_load(handle)


@pyscript_executor  # noqa: F821
def _zone(name):
    """ZoneInfo() reads tz files; keep that off the loop."""
    return ZoneInfo(name)


# ---- small helpers ----------------------------------------------------------

def _monotonic():
    return time.monotonic()


def _now(config):
    if _cfg["zone"] is None or _cfg["zone_name"] != config.timezone:
        _cfg["zone"] = _zone(config.timezone)
        _cfg["zone_name"] = config.timezone
    return datetime.now(_cfg["zone"])


def _warn(kind, message):
    """Rate-limited warning: a persistent fault must not become a tick log."""
    at = _monotonic()
    last = _flags["warned"].get(kind)
    if last is None or at - last >= WARN_EVERY_SECONDS:
        _flags["warned"][kind] = at
        log.warning("peak_guard: " + message)  # noqa: F821


def _get_config():
    mtime, raw = _load_yaml_if_changed(CONFIG_PATH, _cfg["mtime"])
    if raw is not None or _cfg["config"] is None:
        _cfg["config"] = site_config.from_dict(raw)   # ConfigError -> caller
    _cfg["mtime"] = mtime
    return _cfg["config"]


def _as_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _read_sensor(entity):
    """(kW, freshness stamp, problem): problem is None when the value is usable."""
    try:
        raw = state.get(entity)  # noqa: F821
    except Exception:
        return None, None, "missing"
    if raw is None or str(raw).strip().lower() in _BAD:
        return None, None, "unavailable"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, None, "not a number"
    if math.isnan(value) or math.isinf(value):
        return None, None, "not a number"
    try:
        attrs = state.getattr(entity)  # noqa: F821
    except Exception:
        attrs = None
    if not isinstance(attrs, dict):
        attrs = {}
    unit = attrs.get("unit_of_measurement")
    factor = _UNIT_FACTORS.get(unit.strip() if isinstance(unit, str) else None)
    if factor is None:
        return None, None, "unit %r not kW/W" % (unit,)
    stamp = (_as_datetime(getattr(raw, "last_reported", None))
             or _as_datetime(getattr(raw, "last_updated", None)))
    return value * factor, stamp, None


def _set_shaving_entity(on, window_start, shave_kw):
    since = _flags["since"]
    state.set(  # noqa: F821
        SHAVING_ENTITY, "on" if on else "off",
        new_attributes={
            "window_start": window_start.isoformat() if window_start else None,
            "shave_kw": round(shave_kw, 3),
            "since": since.isoformat() if (on and since) else None,
        })


# ---- average-mode detection buffer -----------------------------------------

def _archive_window():
    for sample in _samples:
        _history.append(sample)
    while len(_history) > MAX_HISTORY_SAMPLES:
        _history.pop(0)
    _samples.clear()


def _roll_window(now):
    """Discard the per-window buffer at a boundary; return the window start."""
    start = capacity.window_start_of(now)
    if _flags["window"] != start:
        _archive_window()              # buffer discarded at the boundary
        _flags["window"] = start
    return start


def _feed_detector(cfg, now, reported_kw, offtake_kw):
    """Buffer this window's samples and return the ModeVerdict."""
    start = _roll_window(now)
    if cfg.quarter_hour_average_mode != "auto":
        return capacity.detect_average_mode([], cfg)
    elapsed = (now - start).total_seconds() / 60.0
    if capacity.DETECT_MIN_MIN <= elapsed <= capacity.DETECT_MAX_MIN:
        sample = capacity.Sample(start, elapsed, reported_kw, offtake_kw)
        if len(_samples) < 2:          # only earliest + latest are ever used
            _samples.append(sample)
        else:
            _samples[1] = sample
    return capacity.detect_average_mode(_history + _samples, cfg)


# ---- records ----------------------------------------------------------------

def _make_record(now, cfg, grid, batt, verdict, took_ms, action, power_kw,
                 vetoes, reasoning):
    """Full DecisionRecord for a guard decision (see docstring for renderings)."""
    degraded = decision.degraded_markers(batt)
    if _flags["inverter_marker"]:
        degraded.append(_flags["inverter_marker"])
    if verdict.confidence == "assumed":
        degraded.append("avg_mode_assumed")
    if grid.is_restored:
        degraded.append("meter_restored")
    why = ("%s [average mode %s (%s); guard has no trajectory, forecast/usage/"
           "spill/saturation/breach not evaluated]"
           % (reasoning, verdict.mode, verdict.confidence))
    return decision.DecisionRecord(
        timestamp=now, action=action, target_power_kw=power_kw,
        charge_percent=batt.charge_percent, charge_kwh=batt.stored_kwh,
        consumption_price=None, injection_price=None,
        forecast_remaining_kwh=None, usage_remaining_kwh=None,
        saturation_block=None, spill_kwh=None, reserve_breach_block=None,
        projected_end_charge_kwh=batt.stored_kwh, duration_ms=int(took_ms),
        running_average_kw=grid.running_average_kw,
        ceiling_kw=capacity.ceiling_kw(grid, cfg),
        budget_kw=capacity.budget_kw(grid, cfg),
        vetoes_applied=vetoes, selector="S0", reasoning=why,
        degraded_inputs=degraded, source="guard")


def _emit(action, power_kw, record, cfg):
    if not inverter.apply(action, power_kw, record,
                          inverter_type=cfg.inverter_type,
                          driver_dir=CORE_DIR):
        _warn("apply", "inverter.apply did not record the %s decision" % action)


# ---- evaluation --------------------------------------------------------------

def _clear_shaving(window_start, why):
    """Drop the shaving flag without a decision record (no state to build one)."""
    if _flags["shaving"] is not False:
        log.warning("peak_guard: shaving flag cleared (%s)" % why)  # noqa: F821
    _flags["shaving"] = False
    _flags["shave_kw"] = 0.0
    _flags["since"] = None
    _set_shaving_entity(False, window_start, 0.0)


def _handle_unreadable(missing):
    """Unreadable meter: log, do nothing, never discharge on a guess."""
    if _flags["unreadable_since"] is None:
        _flags["unreadable_since"] = _monotonic()
    _flags["gap"] = True
    _warn("unreadable", "meter unreadable (%s); no action this tick"
          % ", ".join(missing))
    waited = _monotonic() - _flags["unreadable_since"]
    if _flags["shaving"] is not False and waited >= GRACE_SECONDS:
        _clear_shaving(None, "meter unreadable for %.0f s" % waited)


def _handle_error():
    """A failing tick clears a possibly stale flag after the same grace."""
    if _flags["unreadable_since"] is None:
        _flags["unreadable_since"] = _monotonic()
    waited = _monotonic() - _flags["unreadable_since"]
    if _flags["shaving"] is not False and waited >= GRACE_SECONDS:
        _clear_shaving(None, "ticks failing for %.0f s" % waited)


def _avg_staleness(stamp, now, start):
    """None = fresh, "stale" = measured stale, "unknown" = no timestamp."""
    if stamp is not None:
        earliest = start + timedelta(seconds=GUARD_BOUNDARY_MARGIN_S)
        return "stale" if stamp < earliest else None
    if (now - start).total_seconds() < STALE_FALLBACK_SECONDS:
        return "unknown"
    return None


def _render(action, power, fired, suppressed=()):
    return decision.render_vetoes(rules.Decision(
        action, power, "S0", "", list(fired), list(suppressed)))


def _evaluate(trigger_type, started):
    _ensure_core()
    if not _flags["primed"]:
        # module state is lost on reload, the entity is not: never trust "on"
        _set_shaving_entity(False, None, 0.0)
        _flags["primed"] = True
    cfg = _get_config()
    gap = started - _flags["last_run"]
    if trigger_type == "time":
        if gap < cfg.guard_interval_seconds - 1.0:
            return
    elif gap < MIN_STATE_GAP_SECONDS:
        return
    _flags["last_run"] = started
    now = _now(cfg)

    if not cfg.capacity_enabled:
        if _flags["shaving"] is not False:
            _clear_shaving(None, "capacity tariff disabled")
        return

    offtake, _, p_off = _read_sensor(cfg.offtake_sensor)
    reported, avg_updated, p_avg = _read_sensor(ENTITY_QUARTER_AVG)
    month_peak, _, p_peak = _read_sensor(ENTITY_MONTH_PEAK)
    missing = ["%s (%s)" % (name, problem) for name, problem in (
        (cfg.offtake_sensor, p_off), (ENTITY_QUARTER_AVG, p_avg),
        (ENTITY_MONTH_PEAK, p_peak)) if problem]
    if missing:
        _handle_unreadable(missing)
        return
    is_restored = _flags["gap"]
    _flags["gap"] = False
    _flags["unreadable_since"] = None

    start = _roll_window(now)
    staleness = _avg_staleness(avg_updated, now, start)
    if staleness:
        if staleness == "stale" and _flags["stale_window"] != start:
            _flags["stale_window"] = start
            log.warning(  # noqa: F821
                "peak_guard: %s not refreshed since window %s began; no "
                "action until it is" % (ENTITY_QUARTER_AVG, start.isoformat()))
        return

    verdict = _feed_detector(cfg, now, reported, offtake)
    grid = capacity.build_state(
        offtake, 0.0, now, month_peak, cfg, is_restored=is_restored,
        reported_average_kw=reported, average_mode=verdict.mode,
        mode_confidence=verdict.confidence)
    shave = capacity.shave_kw(grid, cfg)

    charge, charge_is_stub, charge_marker = inverter.read_charge(
        cfg.inverter_type, CORE_DIR)
    _flags["inverter_marker"] = charge_marker
    batt = battery.from_percent(charge, cfg, is_stubbed=charge_is_stub)
    forbidden, fired = rules.establish_vetoes(
        batt, None, cfg, grid, usage_history_available=True)   # never grid-charges: V4 is moot
    blocking = "+".join([v for v in fired
                         if rules.FORBID_DISCHARGE in rules.VETO_FORBIDS[v]])
    took_ms = (_monotonic() - started) * 1000.0
    window = grid.window_start
    vetoed = shave > 0 and rules.FORBID_DISCHARGE in forbidden
    record_written = False

    if vetoed:
        shave = 0.0
        if _flags["vetoed_window"] != window:
            _flags["vetoed_window"] = window
            as_decided = rules.Decision(
                "idle", 0.0, "S0", "", list(fired),
                [("S0", "discharge", blocking)])
            record = _make_record(
                now, cfg, grid, batt, verdict, took_ms, "idle", 0.0,
                _render("idle", 0.0, fired, as_decided.suppressed),
                "peak shave vetoed by %s: battery at %.1f%% is at or below the "
                "%.1f%% reserve, so the peak is allowed to form (offtake "
                "%.2f kW, running average %.2f kW, ceiling %.2f kW)"
                % (blocking, batt.charge_percent, cfg.reserve_percent,
                   grid.offtake_kw, grid.running_average_kw,
                   capacity.ceiling_kw(grid, cfg)))
            _emit("idle", 0.0, record, cfg)
            record_written = True

    if shave > 0:
        moved = abs(shave - _flags["shave_kw"]) >= SHAVE_CHANGE_KW
        if not _flags["shaving"] or moved:
            starting = not _flags["shaving"]
            if starting:
                _flags["since"] = now
            record = _make_record(
                now, cfg, grid, batt, verdict, took_ms, "discharge", shave,
                _render("discharge", shave, fired),
                "peak shave (guard): %s; offtake %.2f kW, running average "
                "%.2f kW heading above the %.2f kW ceiling; discharge %.2f kW "
                "to the house to hold it"
                % ("shaving started" if starting else "shave power changed",
                   grid.offtake_kw, grid.running_average_kw,
                   capacity.ceiling_kw(grid, cfg), shave))
            # flags and entity BEFORE the (cancellable) apply: see docstring
            _flags["shave_kw"] = shave
            _flags["shaving"] = True
            _set_shaving_entity(True, window, shave)
            _emit("discharge", shave, record, cfg)
        return

    # nothing to shave (or vetoed): only a transition is worth a line
    if _flags["shaving"]:
        _flags["shaving"] = False              # before the cancellable apply
        _flags["shave_kw"] = 0.0
        _flags["since"] = None
        _set_shaving_entity(False, window, 0.0)
        if not record_written:
            record = _make_record(
                now, cfg, grid, batt, verdict, took_ms, "idle", 0.0,
                _render("idle", 0.0, fired),
                "peak shave (guard): shaving stopped, projected average is "
                "back at or under the %.2f kW ceiling (offtake %.2f kW, "
                "running average %.2f kW)"
                % (capacity.ceiling_kw(grid, cfg), grid.offtake_kw,
                   grid.running_average_kw))
            _emit("idle", 0.0, record, cfg)
    elif _flags["shaving"] is None:
        _flags["shaving"] = False              # fresh start: reconcile silently
        _set_shaving_entity(False, window, 0.0)


@state_trigger("sensor.slimmelezer_power_consumed")  # noqa: F821
@time_trigger("period(now, 30sec)")  # noqa: F821
@task_unique("peak_guard")  # noqa: F821
def peak_guard(trigger_type=None, **kwargs):
    """Guard tick. Never raises: a dead guard shows up on a bill, not a log."""
    try:
        _evaluate(trigger_type, _monotonic())
    except Exception as exc:
        _warn("error", "tick failed, guard stays armed: %r" % (exc,))
        try:
            _handle_error()
        except Exception:
            pass
