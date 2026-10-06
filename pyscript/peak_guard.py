"""Peak guard: evaluate the capacity-tariff shave (S0) every ~30 s.

A pyscript top-level SCRIPT. It is deliberately narrow: three meter
numbers, the capacity arithmetic from capacity.py, the V5 empty-battery veto and the
inverter boundary. No trajectory, no prices, no series, no cache, no forecast.

THE TWO LOOPS AND WHICH WINS (read before changing either)
----------------------------------------------------------
The five-minute planner (battery_planner.py) and this guard both command the
battery. Pyscript scripts cannot import each other, so they coordinate through
one Home Assistant state entity:

    pyscript.peak_guard_shaving      "on" while the guard is shaving a peak,
                                     "off" otherwise. Attributes: window_start,
                                     shave_kw, since, last_beat.

THE GUARD WINS. While the entity is "on" the planner must treat any grid-charge
proposal as vetoed (the rule holds across both processes). Scenario: a cheap
hour makes the planner want a grid charge, the household load steps up and a
peak starts forming. Guard: shave_kw > 0, entity goes "on", discharge recorded.
Planner (next cycle): sees "on", suppresses grid charging. Shave ends (the
window rolls over or the load drops): entity goes "off", planner resumes on
its next cycle. Budget arithmetic (V3) normally prevents the clash first; the
entity is the belt to that braces because the guard has fresher numbers.
The entity is cleared on stop, when capacity is disabled, and when the meter
has been unreadable for GRACE_SECONDS (a dead sensor must not freeze the
planner out of charging).

HEARTBEAT. A guard that stops running (crash, kill, reload mid-shave) cannot
clear the entity, and a stuck "on" would keep the planner from grid charging for
good. So while shaving, every evaluation (at most once per guard interval)
rewrites the entity with last_beat = now (local ISO). The planner treats the flag
as off when last_beat is missing or older than 3 x guard_interval_seconds
(battery_planner.GUARD_BEAT_FACTOR). A flag that only ever changed on a
transition would look the same to the planner dead or alive; the beat is what
tells them apart.

ADD-BACK (why a real inverter does not make the shave flip every tick). The
offtake sensor is NET: it already contains the effect of the discharge the guard
commanded. With a real driver and the battery shaving, the offtake drops to the
allowed rate, the projection then says "no peak", shave_kw would fall to 0, the
guard would stop, offtake would rise, and it would start again: a command flip
every ~30 s, which is hard on an inverter. So the guard keeps the power it is
currently commanding (_flags["shave_kw"]) and ADDS it back to the metered
offtake before projecting: unshaved offtake = metered offtake + commanded
discharge. shave_kw then stays stable while the unshaved load persists and falls
to 0 only when that load no longer threatens the ceiling. The window energy
comes from the meter average (already actual, includes the discharge), so it is
not touched: no double counting. Rules: nothing is added with the "logging" driver
(it transmits nothing, so the meter shows no effect); the figure counts only
while the guard is shaving and has re-affirmed it within 2 x guard interval (a
skipped tick or a killed run must not leave a stale figure behind); it is
clamped to 0..max_discharge_kw. The predictive e-mail warning is fed the METERED
state, so it still fires if a commanded shave is not taking effect.

DOCUMENTED READINGS / DEVIATIONS FROM THE WP TEXT
-------------------------------------------------
* Sensor: offtake is the meter NETTED total (config.offtake_sensor, default
  sensor.slimmelezer_power_consumed). Per-phase sensors are never read or
  summed (a real sample had per-phase imports summing to 0.937 kW where
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
  tick is a no-op (logging below). The margin covers clock skew:
  if the meter (ESPHome) clock runs a few seconds behind HA, the previous
  window's average could be rewritten just after HA's boundary and carry a
  "fresh" stamp. ASSUMPTION: the meter rewrites the average after each window
  boundary, and any skew between meter and HA is under
  GUARD_BOUNDARY_MARGIN_S. The margin applies only when a stamp exists; the
  no-timestamp fallback below is unchanged. If neither timestamp exists (nothing measured) the average is
  ignored for the first STALE_FALLBACK_SECONDS (15 s) of every window, with no
  warning. A stale value NEVER produces a discharge.
  LIVE FINDING: the SlimmeLezer sensors can publish to HA only when their
  value CHANGES (a quiet house read 0.0 with a 50-minute-old last_reported and
  last_updated while the device was alive). A stale stamp with a low value is
  therefore normal for a quiet house and harmless. Logging: stale and below
  GUARD_STALE_WARN_FRACTION * billing_floor_kw (half the floor, 1.25 kW by
  default): one INFO line per window. Stale and at or above it: one WARNING
  per window, with the stamp (local ISO), its age in seconds and the value; a
  high average with an old stamp is the case worth investigating. When a stale
  sensor becomes fresh again one INFO line "refreshed (stamp ...)" is logged
  per stale episode. Logging only: the no-action-while-stale logic is unchanged.
* The billed 13-month average sensor is NOT read: GridState has no field for it
  and costing belongs to the planner.
* Entities: the quarter-hour average and month-peak sensors are read from
  config (capacity_tariff.quarter_hour_average_sensor and month_peak_sensor),
  as is the offtake sensor (capacity_tariff.offtake_sensor).
* Triggers - KNOWN LIMITATION: @state_trigger names the default offtake sensor
  sensor.slimmelezer_power_consumed LITERALLY (decorator arguments are
  evaluated once, at decoration time, before any config is read, so they
  cannot come from config). A changed config.offtake_sensor changes what is
  READ but not what triggers: with a different name the guard runs only on the
  30 s tick, not on every meter update. To trigger on a different sensor, edit
  the decorator argument in this file.
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
  when relevant, meter_restored. The configured average mode is stated in
  why.
* Vetoes: only vetoes that forbid "discharge" BLOCK a shave, i.e. V5 (battery
  empty: charge 0 %). V5 needs a reading: with battery.soc_sensor configured
  but unreadable (degraded=soc_unavailable) it is not evaluated and the guard
  still shaves; V7 (no battery reading) fires but forbids only grid charging
  and export, so it blocks nothing here. The reserve (V1) forbids EXPORT only, so the guard shaves
  below the reserve; the planner cannot know the inverter's own minimum charge,
  which the inverter / driver enforces itself. V3
  (budget <= 0) will normally be fired in a peak but forbids only grid
  charging, so it blocks nothing; it is still RENDERED, bare, in the vetoes
  field of shave and stop records (same as the planner: decision.render_vetoes
  over the fired list), e.g. vetoes=V3. V2 needs a price: none here. A vetoed
  shave is recorded as action=idle, selector S0, with
  vetoes=V5(suppressed S0 discharge) (plus any bare fired ones, e.g. ,V1,V3).
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
* Net offtake and the commanded discharge: handled by the add-back above.
  Still true: a command (and its decision record) is emitted only on change, so
  inverter.apply periodic re-send (resend_minutes) does not run for a steady
  shave; a driver whose command times out on its own must be given a duration
  that outlasts a quarter-hour, or must tolerate a repeat.
* NATIVE CORE LOADER (same pattern as battery_planner.py). Files under
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
* Config is loaded with a ~20-line copy of the planner adapter loader.
  Duplication is accepted for now; keep the two in step.
* Predictive warning (alerts.peak_warning_enabled): after the shave decision
  each evaluation calls _predictive_warning, which asks capacity.peak_warning_due
  whether the projected window average has exceeded the ceiling on
  alerts.peak_warning_ticks (default 2) consecutive evaluations (see that function for the full rules) and then e-mails ONE
  warning per window, rate-limited by alerts.peak_warning_min_interval_minutes
  (memory only: a restart forgets it). The sender is a thin local _notify (a
  pyscript script cannot import the planner's; native code cannot reach
  `service`); the rules and wording are shared in capacity.py. It never changes
  the shave decision and never raises.
* All file I/O goes through @pyscript_executor helpers.
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
# Restart-surviving state (written atomically; a missing or corrupt file means
# a fresh start): the last peak warning.
# inverter.apply keeps last_command.json here too (shared with the planner).
STATE_DIR = "/config/battery_planner/state/"
WARN_STATE_PATH = "/config/battery_planner/state/peak_warning.json"
# Dependency order (rules needs capacity). Deliberately narrow: never
# trajectory, prices, series or cache. inverter is NOT in this list.
CORE_MODULES = ("config", "capacity", "battery", "rules", "decision")
SHAVING_ENTITY = "pyscript.peak_guard_shaving"

MIN_STATE_GAP_SECONDS = 1.0
GRACE_SECONDS = 120.0
WARN_EVERY_SECONDS = 600.0
SHAVE_CHANGE_KW = 0.25
STALE_FALLBACK_SECONDS = 15.0
# A stamp must be this far past the window start to count as fresh (meter
# clock skew; see the docstring). Only used when a timestamp exists.
GUARD_BOUNDARY_MARGIN_S = 2
# A stale average below GUARD_STALE_WARN_FRACTION * billing_floor_kw is logged
# at INFO, at or above it at WARNING. Why: below half the billing floor
# nothing could need shaving even if the stale value were real (nothing under
# the floor is ever billed), and it is what a quiet house on a publish-on-change
# meter looks like. A fraction (not a fixed kW) follows a changed floor.
GUARD_STALE_WARN_FRACTION = 0.5
_UNIT_FACTORS = {"kW": 1.0, "W": 0.001}
_BAD = ("unavailable", "unknown", "none", "")

# ---- module state (mutated in place; pyscript keeps it between triggers) ---
_core_ready = False
_cfg = {"mtime": None, "config": None, "zone": None, "zone_name": None}
_flags = {
    "shaving": None,       # None = unknown (fresh start), True, False
    "shave_kw": 0.0,       # power of the last recorded shave
    "since": None,
    "last_run": -1.0e9,
    "command_at": None,    # monotonic time the current shave was last affirmed
    "beat_at": -1.0e9,     # monotonic time of the last entity write while on
    "unreadable_since": None,
    "gap": False,          # meter was unreadable and has just recovered
    "vetoed_window": None,
    "warned": {},
    "primed": False,       # entity reconciled to "off" since import/reload
    "inverter_marker": None,  # degraded marker from the last charge reading
    "limits_marker": None,  # battery_limits_fallback while a limit sensor fails
    "reserve_marker": None,  # battery_reserve_fallback while the reserve sensor fails
    "stale_window": None,  # window a stale-average WARNING was already logged
    "stale_info_window": None,  # window a low stale-average INFO was logged
    "stale_episode": False,  # a stale average was seen and has not refreshed yet
    "peak_warn": {},       # capacity.peak_warning_due memory
    "warn_loaded": False,  # peak_warning.json read once per process
}


# ---- I/O helpers: the ONLY places that touch files -------------------------
# @pyscript_executor (not merely @pyscript_compile) so the blocking work runs
# in a worker thread, off the HA event loop.

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
def _read_json(path):
    """Decoded JSON, or None when missing or unreadable."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


@pyscript_executor  # noqa: F821
def _write_json_atomic(path, payload):
    """Temp file, fsync, rename over the target. Error text or None."""
    import json
    import os
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        return repr(exc)
    return None


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


def _read_soc(entity):
    """Battery charge in percent from a sensor; None when unreadable
    (unknown/unavailable, not a number, or outside 0..100)."""
    try:
        raw = state.get(entity)  # noqa: F821
    except Exception:
        return None
    if raw is None or str(raw).strip().lower() in _BAD:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 100:                # also False for NaN
        return None
    return value


def _notify(cfg, title, message):
    """Send one e-mail through the configured notify service (the same call
    shape as battery_planner._notify; a pyscript script cannot import it and
    native code cannot reach `service`, so this thin wrapper is duplicated).
    True when sent; a failure is warned (rate-limited) and returns False."""
    try:
        service.call(  # noqa: F821
            "notify", cfg.notify_service,
            title=title, message=message, target=[cfg.alert_address])
        return True
    except Exception as exc:
        _warn("alert", "alert send failed: %r" % (exc,))
        return False


def _guard_note(cfg, shave, vetoed, batt, charge_is_stub):
    """One sentence on what the guard is doing about a predicted crossing."""
    if vetoed:
        note = ("The guard CANNOT shave it: veto V5, the battery is empty "
                "(%.1f%%). Being under the %.1f%% reserve would not stop it; "
                "the reserve only limits exporting." % (
                    batt.charge_percent, cfg.reserve_percent))
    elif shave > 0:
        note = "The guard is shaving: discharging %.2f kW to hold it." % shave
    else:
        note = "The guard is not shaving (nothing it could shave right now)."
    if charge_is_stub:
        note += " The battery charge is a stub value (no real reading)."
    if cfg.inverter_type == "logging":
        note += (" The inverter driver is 'logging': a shave is recorded but "
                 "nothing is sent to the battery.")
    elif cfg.inverter_dry_run:
        note += (" Dry run is on: a shave is recorded but nothing is sent to "
                 "the battery.")
    return note


def _predictive_warning(cfg, grid, shave, vetoed, batt, charge_is_stub, at):
    """E-mail when this quarter-hour is probably going to cross the ceiling.
    Never raises and never touches a decision; a failed send is retried."""
    try:
        if not cfg.peak_warning_enabled:
            return
        mem = _flags["peak_warn"]
        wall = _now(cfg)
        if not _flags["warn_loaded"]:
            _flags["warn_loaded"] = True
            capacity.peak_warning_restore(
                mem, _read_json(WARN_STATE_PATH), wall, at)
        if not capacity.peak_warning_due(mem, grid, cfg, at):
            return
        title, message = capacity.peak_warning_message(
            grid, cfg, _guard_note(cfg, shave, vetoed, batt, charge_is_stub))
        if _notify(cfg, title, message):
            capacity.peak_warning_sent(mem, grid, at)
            err = _write_json_atomic(
                WARN_STATE_PATH, capacity.peak_warning_to_data(mem, wall))
            if err:
                _warn("peak_warning_state",
                      "cannot save the peak warning time: %s" % (err,))
    except Exception as exc:
        _warn("peak_warning", "predictive warning failed: %r" % (exc,))


def _set_shaving_entity(on, window_start, shave_kw, beat=None):
    """Write the flag. `beat` (aware datetime) is the heartbeat; set it
    whenever the flag is turned or kept "on"."""
    since = _flags["since"]
    state.set(  # noqa: F821
        SHAVING_ENTITY, "on" if on else "off",
        new_attributes={
            "window_start": window_start.isoformat() if window_start else None,
            "shave_kw": round(shave_kw, 3),
            "since": since.isoformat() if (on and since) else None,
            "last_beat": beat.isoformat() if (on and beat) else None,
        })


def _refresh_beat(cfg, now, started):
    """Keep the planner view of "on" alive: rewrite the flag with a fresh
    last_beat at most once per guard interval while shaving."""
    if not _flags["shaving"]:
        return
    if started - _flags["beat_at"] < cfg.guard_interval_seconds - 1.0:
        return
    _flags["beat_at"] = started
    _set_shaving_entity(True, capacity.window_start_of(now),
                        _flags["shave_kw"], now)


def _commanded_discharge_kw(cfg, at):
    """Discharge power the guard is commanding right now (kW), else 0.0.

    Non-zero only when a real driver (not "logging", which transmits nothing)
    is configured, the guard is shaving, and that shave was last affirmed at
    most 2 guard intervals ago. Clamped to 0..max_discharge_kw. The metered
    offtake includes this discharge; the caller adds it back."""
    if (cfg.inverter_type == "logging" or cfg.inverter_dry_run
            or not _flags["shaving"]):
        return 0.0
    when = _flags["command_at"]
    if when is None:
        return 0.0
    age = at - when
    if age < 0 or age > 2 * cfg.guard_interval_seconds:
        return 0.0
    return max(0.0, min(_flags["shave_kw"], cfg.max_discharge_kw))


def _limit_kw(entity):
    """Inverter power limit sensor as kW; None when unreadable or not above 0."""
    kw, _, problem = _read_sensor(entity)
    if problem or kw <= 0:
        return None
    return kw


def _with_limits(cfg):
    """cfg with the inverter's own charge / discharge limits for this tick
    (same rule as battery_planner._with_limits). A configured sensor that
    cannot be read keeps the numeric fallback and sets the degraded marker."""
    _flags["limits_marker"] = None
    if cfg.max_charge_sensor is None and cfg.max_discharge_sensor is None:
        return cfg
    charge = None
    if cfg.max_charge_sensor is not None:
        charge = _limit_kw(cfg.max_charge_sensor)
    discharge = None
    if cfg.max_discharge_sensor is not None:
        discharge = _limit_kw(cfg.max_discharge_sensor)
    if ((cfg.max_charge_sensor is not None and charge is None)
            or (cfg.max_discharge_sensor is not None and discharge is None)):
        _flags["limits_marker"] = "battery_limits_fallback"
    return site_config.with_limits(cfg, charge, discharge)


def _with_reserve(cfg):
    """cfg with the inverter's own minimum charge as the reserve for this tick
    (same rule as battery_planner._with_reserve)."""
    _flags["reserve_marker"] = None
    if cfg.reserve_sensor is None:
        return cfg
    new = site_config.with_reserve(cfg, _read_soc(cfg.reserve_sensor))
    if new is cfg:
        _flags["reserve_marker"] = "battery_reserve_fallback"
    return new


def _battery_discharge_kw(cfg):
    """Battery power from battery.power_sensor (kW, discharge positive), or
    None: no sensor configured, or it cannot be read (the household draw is
    then estimated from the offtake alone)."""
    if cfg.power_sensor is None:
        return None
    power, _, problem = _read_sensor(cfg.power_sensor)
    if problem:
        return None
    return capacity.battery_discharge_from_power(power, cfg.power_positive)


def _grid_state(offtake_kw, now, month_peak, cfg, is_restored, reported,
                battery_discharge_kw=None):
    return capacity.build_state(
        offtake_kw, 0.0, now, month_peak, cfg, is_restored=is_restored,
        reported_average_kw=reported,
        battery_discharge_kw=battery_discharge_kw)


# ---- records ----------------------------------------------------------------

def _make_record(now, cfg, grid, batt, took_ms, action, power_kw,
                 vetoes, reasoning):
    """Full DecisionRecord for a guard decision (see docstring for renderings)."""
    degraded = decision.degraded_markers(batt)
    if _flags["inverter_marker"] == "soc_unavailable":
        degraded = [m for m in degraded if m != "soc_stubbed"]
    if _flags["inverter_marker"]:
        degraded.append(_flags["inverter_marker"])
    if _flags["limits_marker"]:
        degraded.append(_flags["limits_marker"])
    if _flags["reserve_marker"]:
        degraded.append(_flags["reserve_marker"])
    if grid.is_restored:
        degraded.append("meter_restored")
    why = ("%s [average mode %s; guard has no trajectory, forecast/usage/"
           "spill/saturation/breach not evaluated]"
           % (reasoning, cfg.quarter_hour_average_mode))
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
                          driver_dir=CORE_DIR,
                          resend_minutes=cfg.inverter_resend_minutes,
                          state_dir=STATE_DIR,
                          dry_run=cfg.inverter_dry_run):
        _warn("apply", "inverter.apply did not record the %s decision" % action)


# ---- evaluation --------------------------------------------------------------

def _clear_shaving(window_start, why):
    """Drop the shaving flag without a decision record (no state to build one)."""
    if _flags["shaving"] is not False:
        log.warning("peak_guard: shaving flag cleared (%s)" % why)  # noqa: F821
    _flags["shaving"] = False
    _flags["shave_kw"] = 0.0
    _flags["command_at"] = None
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


def _stamp_text(stamp, now):
    if stamp is None:
        return "none"
    return stamp.astimezone(now.tzinfo).isoformat()


def _log_stale(cfg, now, start, stamp, value_kw):
    """Log a stale average once per window and level; never affects decisions."""
    entity = cfg.quarter_hour_average_sensor
    if value_kw < cfg.billing_floor_kw * GUARD_STALE_WARN_FRACTION:
        if _flags["stale_info_window"] != start:
            _flags["stale_info_window"] = start
            log.info(  # noqa: F821
                "peak_guard: %s not refreshed since window %s began (last "
                "stamp %s, age %.0fs, value %.3f kW, low: harmless for a "
                "quiet house); no action until it is" % (
                    entity, start.isoformat(), _stamp_text(stamp, now),
                    (now - stamp).total_seconds(), value_kw))
    elif _flags["stale_window"] != start:
        _flags["stale_window"] = start
        log.warning(  # noqa: F821
            "peak_guard: %s not refreshed since window %s began (last stamp "
            "%s, age %.0fs, value %.3f kW); no action until it is" % (
                entity, start.isoformat(), _stamp_text(stamp, now),
                (now - stamp).total_seconds(), value_kw))


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
    cfg = _with_limits(cfg)
    cfg = _with_reserve(cfg)
    _refresh_beat(cfg, now, started)

    offtake, _, p_off = _read_sensor(cfg.offtake_sensor)
    reported, avg_updated, p_avg = _read_sensor(cfg.quarter_hour_average_sensor)
    month_peak, _, p_peak = _read_sensor(cfg.month_peak_sensor)
    missing = ["%s (%s)" % (name, problem) for name, problem in (
        (cfg.offtake_sensor, p_off),
        (cfg.quarter_hour_average_sensor, p_avg),
        (cfg.month_peak_sensor, p_peak)) if problem]
    if missing:
        _handle_unreadable(missing)
        return
    is_restored = _flags["gap"]
    _flags["gap"] = False
    _flags["unreadable_since"] = None

    start = capacity.window_start_of(now)
    staleness = _avg_staleness(avg_updated, now, start)
    if staleness:
        if staleness == "stale":
            _flags["stale_episode"] = True
            _log_stale(cfg, now, start, avg_updated, reported)
        return
    if _flags["stale_episode"]:
        _flags["stale_episode"] = False
        log.info(  # noqa: F821
            "peak_guard: %s refreshed (stamp %s)" % (
                cfg.quarter_hour_average_sensor, _stamp_text(avg_updated, now)))

    # the meter shows offtake AFTER our own discharge: add it back (docstring)
    addback = _commanded_discharge_kw(cfg, started)
    # The sensor reading already includes the commanded discharge. The state
    # with the add-back has offtake + addback, so it takes the add-back out of
    # the sensor figure once: the household draw (offtake + battery) is the
    # same in both and nothing is counted twice.
    sensor_kw = _battery_discharge_kw(cfg)
    metered = _grid_state(offtake, now, month_peak, cfg, is_restored,
                          reported, sensor_kw)
    grid = metered if addback <= 0 else _grid_state(
        offtake + addback, now, month_peak, cfg, is_restored, reported,
        None if sensor_kw is None else sensor_kw - addback)
    if addback > 0:
        offtake_text = ("offtake %.2f kW (metered %.2f kW + %.2f kW commanded "
                        "discharge)" % (grid.offtake_kw, offtake, addback))
    else:
        offtake_text = "offtake %.2f kW" % grid.offtake_kw
    shave = capacity.shave_kw(grid, cfg)

    soc_known = True
    if cfg.soc_sensor is None:
        charge, charge_is_stub, charge_marker = inverter.read_charge(
            cfg.inverter_type, CORE_DIR)
        batt = battery.from_percent(charge, cfg, is_stubbed=charge_is_stub)
    else:                                    # real reading: never the stub
        charge = _read_soc(cfg.soc_sensor)
        soc_known = charge is not None
        charge_is_stub = not soc_known
        charge_marker = None if soc_known else "soc_unavailable"
        batt = (battery.from_percent(charge, cfg, is_stubbed=False)
                if soc_known else battery.unknown(cfg))
    _flags["inverter_marker"] = charge_marker
    forbidden, fired = rules.establish_vetoes(
        batt, None, cfg, grid, usage_history_available=True,   # never grid-charges: V4 is moot
        soc_known=soc_known)
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
                now, cfg, grid, batt, took_ms, "idle", 0.0,
                _render("idle", 0.0, fired, as_decided.suppressed),
                "peak shave vetoed by %s: the battery is empty (%.1f%%), so "
                "the peak is allowed to form (%s, running "
                "average %.2f kW, ceiling %.2f kW)"
                % (blocking, batt.charge_percent,
                   offtake_text, grid.running_average_kw,
                   capacity.ceiling_kw(grid, cfg)))
            _emit("idle", 0.0, record, cfg)
            record_written = True

    _predictive_warning(cfg, metered, shave, vetoed, batt, charge_is_stub,
                        started)

    if shave > 0:
        _flags["command_at"] = started         # affirmed: the add-back stays
        moved = abs(shave - _flags["shave_kw"]) >= SHAVE_CHANGE_KW
        if not _flags["shaving"] or moved:
            starting = not _flags["shaving"]
            if starting:
                _flags["since"] = now
            record = _make_record(
                now, cfg, grid, batt, took_ms, "discharge", shave,
                _render("discharge", shave, fired),
                "peak shave (guard): %s; %s, running average "
                "%.2f kW heading above the %.2f kW ceiling; discharge %.2f kW "
                "to the house to hold it"
                % ("shaving started" if starting else "shave power changed",
                   offtake_text, grid.running_average_kw,
                   capacity.ceiling_kw(grid, cfg), shave))
            # flags and entity BEFORE the (cancellable) apply: see docstring
            _flags["shave_kw"] = shave
            _flags["shaving"] = True
            _flags["beat_at"] = started
            _set_shaving_entity(True, window, shave, now)
            _emit("discharge", shave, record, cfg)
        return

    # nothing to shave (or vetoed): only a transition is worth a line
    if _flags["shaving"]:
        _flags["shaving"] = False              # before the cancellable apply
        _flags["shave_kw"] = 0.0
        _flags["command_at"] = None
        _flags["since"] = None
        _set_shaving_entity(False, window, 0.0)
        if not record_written:
            record = _make_record(
                now, cfg, grid, batt, took_ms, "idle", 0.0,
                _render("idle", 0.0, fired),
                "peak shave (guard): shaving stopped, projected average is "
                "back at or under the %.2f kW ceiling (%s, "
                "running average %.2f kW)"
                % (capacity.ceiling_kw(grid, cfg), offtake_text,
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
