"""Battery planner: the pyscript adapter. The ONLY code that touches Home
Assistant state, files and the clock. Every decision lives in pyscript/modules/.

Cycle (FR-021..024, FR-034, NFR-006, NFR-009), fired every minute and gated on
config.evaluation_interval_minutes so that value is really configurable:

  config -> prices (missing/stale: HALT) -> forecast (missing: zero solar +
  bounded retry) -> solar/usage series (cache) -> charge (stub) -> battery ->
  trajectory -> grid state -> rules.decide -> decision.build -> inverter.apply

Documented readings and guesses (this file cannot be run outside Home Assistant)
--------------------------------------------------------------------------------
* I/O: every open()/os call is inside a @pyscript_executor helper (native code
  in an executor thread). Never merely @pyscript_compile (R-02 correction).
* NATIVE CORE LOADER. Files under pyscript/modules/ are *pyscript* modules:
  pyscript runs them in its own AST interpreter, which lacks generator
  expressions, @property, native callbacks to pyscript functions (key=fn) and
  validating __post_init__. The pure core (config, prices, series, battery,
  trajectory, capacity, rules, decision, cache, history) uses all of these, so it must
  run as ordinary CPython. _load_core (a @pyscript_executor helper, so the
  import stays off the event loop) loads each file with importlib under the
  private name battery_planner_core_<name>; no sys.path entry is added, so
  nothing can shadow or be shadowed by another `config`/`cache`/`series`/
  `decision`. The ten core modules are ALSO registered under their bare names
  in sys.modules for the process lifetime, because core functions import
  siblings at call time (decision.build does `import capacity`) and the core
  directory is not on sys.path in HA. Trade-off: those ten bare names now
  resolve to the core for every other importer in the HA process. Only these
  ten are aliased (never `inverter`); a failed load undoes its aliases.
  The adapter binds the results as its globals in _ensure_core(). `inverter`
  stays a normal pyscript import (it uses `log` and @pyscript_executor).
  CAVEAT: natively loaded modules are NOT hot-reloaded. A change to any core
  file needs a Home Assistant restart (pyscript.reload does not help).
  Only this file and pyscript/modules/inverter.py are interpreted; a test lints
  both for the constructs the interpreter cannot run.
* Overlap: a module-level busy marker (_cycle_started_at), checked and set at
  the top of the trigger (no await between, so atomic in pyscript) and cleared
  in `finally`. A DUE cycle that finds it set logs a warning and writes a SKIP
  line to the day's decision log; a marker older than 2 intervals (a hung cycle, e.g. an
  executor blocked on NAS I/O) is logged as an error. Not @task_unique: its
  kill_me kills the NEW call before it can log (NFR-001).
* Gate: cron fires at :00 of each minute, so elapsed time is compared with a
  GATE_TOLERANCE_SECONDS allowance, else jitter would halve the rate.
* took= covers config to just before the record is built; the final append
  cannot be inside the record it writes.
* Price map keeps the fixed-offset keys prices.expand_to_blocks produces; it is
  never re-keyed in local time (repeated fall-back hour).
* Two degraded modes differ. Prices missing/stale/unknown/unavailable or last
  block elapsed -> HALT: no decision, one HALT line per halted cycle in
  the day's decision log (own small executor helper, since inverter.apply only accepts
  DecisionRecords), one alert on entry then at most one per
  alerts.realert_minutes, and a RECOVERED line when prices return. Halt state
  is in memory only: an HA restart during an outage alerts once more.
  Forecast missing -> zero_solar_series + solar_zero_fallback marker and a
  bounded homeassistant.update_entity retry (never a halt).
* Alerts: service.call("notify", alerts.notify_service, target=[alerts.address]).
  Both the halt alert and the month-peak notice go through _notify().
* Month-peak notice (alerts.peak_enabled): each cycle, before the price check
  (so it also works during a halt), _check_peak_alert compares the meter's month
  peak with capacity_tariff.billing_floor_kw. First crossing of a calendar month
  (local time) and each further rise of >= capacity.PEAK_ALERT_MIN_STEP_KW send
  one e-mail; (month, peak) is saved to battery_planner/state/peak_alert.json
  only after a successful send. A failed send is retried next cycle. Missing or
  unreadable file = nothing sent yet: one notice per start at most. It never
  affects a decision.
  Mail account settings live in HA's own configuration; none appear here.
* Cache: verdicts come from cache.evaluate; every series taken FROM the cache,
  fresh or stale, adds a cache_age_<kind> marker (FR-027/SC-014); a series just
  rebuilt adds none. Solar is
  rebuilt when the forecast signature changes, usage once per local day.
  Extra keys (forecast_signature, history_days) ride along in the JSON; the
  cache module ignores them. Series are built over a fixed span (local
  midnight + SERIES_SPAN_HOURS) so a cached usage profile stays dense to any
  price horizon. The trajectory is never cached. A zero-solar fallback or an
  empty-history usage profile is never written to the cache.
* Energy history (pyscript/modules/history.py): each cycle, on the first cycle at
  or after a block boundary, the configured cumulative kWh counters are read
  and the FINISHED block's record is appended to
  <config>/battery_planner/history/blocks-YYYY-MM-DD.jsonl (local date of
  block_start); the snapshot is also kept in last_snapshot.json so a restart
  does not lose the block in progress. Timing imprecision: the snapshot is read
  up to one evaluation interval after the boundary, so a block's energy covers
  [read_prev, read_this), not exactly the block; both read times are recorded.
  The recorder is wrapped: a failure is logged (rate-limited) and never touches
  the decision. Files are never deleted by the planner.
* Usage history: read_usage_history() is the single seam; see its docstring.
  Coverage (distinct local days present vs the window) is reported as
  usage_samples=N; per-slot sample_days is not the coverage signal.
* Peak guard coordination: if pyscript.peak_guard_shaving is "on" and the
  decision would charge from the grid (charge_source == "grid"), the decision
  is replaced by an idle S6 Decision whose reasoning says so, and the veto
  field records GUARD(suppressed <selector> charge). The same downgrade is
  applied (marker NOGRID) when capacity is enabled but the grid sensors are
  unreadable, because rules.decide with no grid state would grid-charge with no
  budget cap.
* State that survives a restart (all under <config>/battery_planner/state/,
  written atomically, read once per process in @pyscript_executor helpers; a
  missing or corrupt file means a fresh start, never an error):
    halt.json                  price-outage halt (cause, entered_at,
                               last_alert_at): a restart during an outage
                               neither re-alerts early nor loses the outage
    average_mode_planner.json  the detector samples that can still vote, so
                               a detected quarter-hour mode stays "detected"
    peak_alert.json            last month-peak e-mail (see _check_peak_alert)
    last_command.json          written by inverter.apply (command de-dup)
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import inverter          # the only core file pyscript interprets

# Pure core, bound natively by _ensure_core() (see docstring).
battery = cache = capacity = config = decision = history = None
prices = rules = series = trajectory = None

# ---- locations (tests redirect these) --------------------------------------
CONFIG_PATH = "/config/battery_planner/user_config.yaml"
CACHE_DIR = "/config/battery_planner/cache/"
DECISIONS_LOG_DIR = inverter.DEFAULT_LOG_DIR
HISTORY_DIR = "/config/battery_planner/history/"
STATE_DIR = "/config/battery_planner/state/"
PEAK_ALERT_PATH = "/config/battery_planner/state/peak_alert.json"
HALT_STATE_PATH = "/config/battery_planner/state/halt.json"
MODE_STATE_PATH = "/config/battery_planner/state/average_mode_planner.json"
CORE_DIR = "/config/pyscript/modules"
# Dependency order (rules needs capacity). inverter is NOT in this list.
CORE_MODULES = ("config", "prices", "series", "battery", "trajectory",
                "capacity", "rules", "decision", "cache", "history")

# ---- Home Assistant entities -----------------------------------------------
# The price, forecast and SlimmeLezer entity ids and attribute names come from
# the config (prices.entity, prices.attribute, solar.forecast_entity,
# solar.forecast_attribute, capacity_tariff.quarter_hour_average_sensor and
# month_peak_sensor). A wrong name shows as a constant price halt whose cause
# names the configured entity and attribute.
# The netted offtake sensor id also comes from config
# (capacity_tariff.offtake_sensor, default sensor.slimmelezer_power_consumed).
GUARD_FLAG_ENTITY = "pyscript.peak_guard_shaving"   # set by the peak guard (WP12)
_BAD_STATES = (None, "", "unknown", "unavailable", "none", "None")

# ---- tuning that is not user config ----------------------------------------
GATE_TOLERANCE_SECONDS = 30
CONFIG_RETRY_MINUTES = 5              # cadence while the config is unusable
SERIES_SPAN_HOURS = 72
MAX_FETCHES_PER_HOUR = 12             # NFR-002, shared with the hourly poll
FORECAST_FAILURES_BEFORE_RETRY = 2
FORECAST_AGE_MARKER_MINUTES = 75      # older than a normal hourly refresh: mark it
SLOW_CYCLE_MS = 5000                  # NFR-001
MAX_MODE_SAMPLES = 400
HISTORY_WARN_MINUTES = 60             # per warning kind, energy history

# ---- module state -----------------------------------------------------------
_core_ready = False
_cycle_started_at = None
_last_run = None
_config = None
_config_mtime = None
_config_error = None
_halt_state = None
_halt_loaded = False                  # halt.json read once per process
_forecast_failures = 0
_refresh_calls = []
_mode_samples = []
_mode_loaded = False                  # average_mode_planner.json read once
_mode_saved = None                    # what that file holds (skip equal writes)
_hist_last = None                     # history.Snapshot at the last boundary
_hist_loaded = False                  # last_snapshot.json read once per process
_hist_warned = {}                     # warning kind -> last time logged
_peak_alert_loaded = False            # peak_alert.json read once per process
_peak_alert_last = None               # (YYYY-MM, kW) of the last notified peak
_last_grid_charge = None              # (local time, kW) of the last recorded grid-charge decision


def _now():
    return datetime.now(timezone.utc)


# ---- native core loader --------------------------------------------------------

@pyscript_executor  # noqa: F821  (provided by pyscript at runtime)
def _load_core(core_dir, names):
    """Import the pure core as CPython modules. Returns {name: module}."""
    import importlib.util
    import os
    import sys
    loaded, saved = {}, {}
    try:
        for name in names:
            full = "battery_planner_core_" + name
            module = sys.modules.get(full)
            if module is None:
                spec = importlib.util.spec_from_file_location(
                    full, os.path.join(core_dir, name + ".py"))
                module = importlib.util.module_from_spec(spec)
                sys.modules[full] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(full, None)
                    raise
            loaded[name] = module
            # Bare alias, kept for the process lifetime: core code also imports
            # siblings inside functions (decision.build -> `import capacity`),
            # long after loading, and core_dir is not on sys.path.
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
    global _core_ready, battery, cache, capacity, config, decision, history
    global prices, rules, series, trajectory
    if _core_ready:
        return
    mods = _load_core(CORE_DIR, CORE_MODULES)
    battery, cache, capacity = mods["battery"], mods["cache"], mods["capacity"]
    config, decision, prices = mods["config"], mods["decision"], mods["prices"]
    rules, series, trajectory = mods["rules"], mods["series"], mods["trajectory"]
    history = mods["history"]
    _core_ready = True


# ---- file helpers: ALL blocking I/O, each in an executor thread -------------

@pyscript_executor  # noqa: F821  (provided by pyscript at runtime)
def _load_yaml(path, last_mtime):
    """Parse the YAML file if its mtime changed. Returns a status dict."""
    import os
    import yaml
    try:
        mtime = os.stat(path).st_mtime
    except OSError as exc:
        return {"status": "error", "mtime": None,
                "error": "cannot read %s: %s" % (path, exc)}
    if last_mtime is not None and mtime == last_mtime:
        return {"status": "unchanged", "mtime": mtime}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception as exc:
        return {"status": "error", "mtime": mtime,
                "error": "cannot parse %s: %s" % (path, exc)}
    return {"status": "loaded", "mtime": mtime, "data": data}


@pyscript_executor  # noqa: F821
def _read_json(path):
    """Decoded JSON, or None when missing/unreadable (a cache miss)."""
    import json
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


@pyscript_executor  # noqa: F821
def _write_json_atomic(path, payload):
    """Write to a temp file, fsync, rename over the target. Error text or None."""
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
def _append_line(path, line):
    """Append one line to the decision log (HALT/RECOVERED lines only)."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


@pyscript_executor  # noqa: F821
def _append_text(path, text):
    """Append `text` to a file in ONE write, flushed and fsynced. Error or None."""
    import os
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        return repr(exc)
    return None


@pyscript_executor  # noqa: F821
def _read_texts(paths):
    """{"texts": {path: text}, "errors": [...]}; a missing file is not an error."""
    texts, errors = {}, []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                texts[path] = handle.read()
        except FileNotFoundError:
            continue
        except Exception as exc:
            errors.append("%s: %r" % (path, exc))
    return {"texts": texts, "errors": errors}


def _log_line(line, when):
    """Append `line` to the decision log file of the local day of `when`."""
    try:
        _append_line(inverter.log_path_for(when, DECISIONS_LOG_DIR), line)
    except Exception as exc:
        log.error(f"battery_planner: cannot append to decision log: {exc!r}")  # noqa: F821


# ---- Home Assistant state ----------------------------------------------------

def _state_value(entity):
    """State string, or None when absent/unknown/unavailable."""
    try:
        value = state.get(entity)  # noqa: F821  (may raise NameError if absent)
    except Exception:
        return None
    return None if value in _BAD_STATES else value


def _state_attr(entity, name):
    if _state_value(entity) is None:
        return None
    try:
        attrs = state.getattr(entity)  # noqa: F821
    except Exception:
        return None
    return attrs.get(name) if attrs else None


def _sensor_kw(entity):
    """Numeric sensor as kW (W is converted). None when unreadable."""
    value = _state_value(entity)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    unit = _state_attr(entity, "unit_of_measurement")
    return number / 1000.0 if unit == "W" else number


def _counter_kwh(entity):
    """Cumulative energy counter as kWh (Wh and MWh converted); None if unreadable."""
    value = _state_value(entity)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    unit = _state_attr(entity, "unit_of_measurement")
    if unit is None or unit == "kWh":
        return number
    if unit == "Wh":
        return number / 1000.0
    if unit == "MWh":
        return number * 1000.0
    return None


# ---- config ------------------------------------------------------------------

def _load_config():
    """Current SiteConfig, reloaded when the file's mtime changes; else None."""
    global _config, _config_mtime, _config_error
    result = _load_yaml(CONFIG_PATH, _config_mtime)
    if result["status"] == "loaded":
        _config_mtime = result["mtime"]
        try:
            _config = config.from_dict(result["data"])
            _config_error = None
        except config.ConfigError as exc:
            _config, _config_error = None, str(exc)
    elif result["status"] == "error":
        _config_mtime = result["mtime"]
        _config, _config_error = None, result["error"]
    if _config is None:
        log.error(  # noqa: F821
            f"battery_planner: STARTUP FAILURE, no decision made: {_config_error}")
    return _config


# ---- prices and halt ---------------------------------------------------------

def _read_prices(cfg, local):
    """(price_map, cause). cause is None when usable prices are present."""
    if _state_value(cfg.price_entity) is None:
        return None, "price entity %s unavailable" % cfg.price_entity
    entries = _state_attr(cfg.price_entity, cfg.price_attribute)
    if not isinstance(entries, list) or not entries:
        return None, "attribute %s missing or empty on %s" % (
            cfg.price_attribute, cfg.price_entity)
    try:
        price_map = prices.expand_to_blocks(entries, cfg, local)
    except (KeyError, TypeError, ValueError) as exc:
        return None, "price entries unparseable: %r" % (exc,)
    end = prices.horizon_end(price_map, cfg.block_minutes)
    if end is None or end <= local:  # input validation: a stale series is absent
        return None, "price series empty or already elapsed"
    return price_map, None


def _notify(cfg, title, message):
    """Send one e-mail via the configured notify service. True when sent;
    a failure is logged and returns False so the caller retries later."""
    try:
        service.call(  # noqa: F821
            "notify", cfg.notify_service,
            title=title, message=message, target=[cfg.alert_address])
        return True
    except Exception as exc:
        log.error(f"battery_planner: alert send failed: {exc!r}")  # noqa: F821
        return False


def _send_alert(cfg, cause, entered):
    return _notify(
        cfg, "Battery planner halted: no price data",
        "No decisions are being made. Cause: %s. Halted since %s. "
        "Re-alert every %d min." % (
            cause, entered.isoformat(timespec="seconds"),
            cfg.realert_minutes))


def _save_halt():
    """Persist the halt state (or "not halted") so a restart can resume it."""
    h = _halt_state
    if h is None:
        payload = {"active": False}
    else:
        payload = {
            "active": True, "cause": h.cause,
            "entered_at": h.entered_at.isoformat(),
            "last_alert_at": (h.last_alert_at.isoformat()
                              if h.last_alert_at is not None else None)}
    err = _write_json_atomic(HALT_STATE_PATH, payload)
    if err:
        log.error(f"battery_planner: cannot save halt state: {err}")  # noqa: F821


def _load_halt():
    """Resume a halt that was running when the process stopped (once).

    Anything unreadable counts as "not halted"; the next outage then starts
    fresh. An outage that ended while we were down is closed by _recover.
    """
    global _halt_state, _halt_loaded
    if _halt_loaded:
        return
    _halt_loaded = True
    if _halt_state is not None:
        return
    data = _read_json(HALT_STATE_PATH)
    if not isinstance(data, dict) or data.get("active") is not True:
        return
    try:
        entered = datetime.fromisoformat(data["entered_at"])
        raw = data.get("last_alert_at")
        last = None if raw is None else datetime.fromisoformat(raw)
        if entered.tzinfo is None or (last is not None and last.tzinfo is None):
            return
        _halt_state = decision.HaltState(
            True, decision.one_line(data["cause"]), entered, last)
    except Exception:
        _halt_state = None


def _halt(cfg, local, cause):
    """Price outage: no decision; alert on entry, then per realert_minutes."""
    global _halt_state
    _load_halt()
    cause = decision.one_line(cause)     # may embed an exception repr
    if _halt_state is None:
        log.error(f"battery_planner: HALT, {cause}")  # noqa: F821
        _halt_state = decision.HaltState(True, cause, local, None)
        _save_halt()
    last = _halt_state.last_alert_at
    if last is None or (local - last).total_seconds() >= cfg.realert_minutes * 60:
        if _send_alert(cfg, _halt_state.cause, _halt_state.entered_at):
            _halt_state = decision.HaltState(
                True, _halt_state.cause, _halt_state.entered_at, local)
            _save_halt()
    _log_line(decision.format_halt(_halt_state, local), local)


def _recover(local):
    global _halt_state
    _load_halt()
    if _halt_state is None:
        return
    entered = _halt_state.entered_at
    minutes = int((local - entered).total_seconds() // 60)
    _log_line(" | ".join([
        local.isoformat(timespec="seconds"), "RECOVERED",
        "cause=%s" % _halt_state.cause,
        "entered=%s" % entered.isoformat(timespec="seconds"),
        "halted_for=%dm" % minutes]), local)
    log.info("battery_planner: prices recovered, resuming decisions")  # noqa: F821
    _halt_state = None
    _save_halt()


# ---- month-peak notice ---------------------------------------------------------

def _peak_alert_state():
    """(month, kW) last notified, or None. Read from disk once per process; an
    unreadable or malformed file counts as "nothing sent yet", so a restart
    notifies at most once for an old peak (then memory takes over)."""
    global _peak_alert_loaded, _peak_alert_last
    if not _peak_alert_loaded:
        _peak_alert_loaded = True
        data = _read_json(PEAK_ALERT_PATH)
        if (isinstance(data, dict) and isinstance(data.get("month"), str)
                and isinstance(data.get("peak_kw"), (int, float))
                and not isinstance(data.get("peak_kw"), bool)):
            _peak_alert_last = (data["month"], float(data["peak_kw"]))
    return _peak_alert_last


def _check_peak_alert(cfg, local):
    """E-mail when the meter's month peak exceeds the billing floor.

    Independent of prices and the halt state; never raises and never touches
    a decision. A failed send is not recorded, so the next cycle retries.
    """
    global _peak_alert_last
    try:
        if not (cfg.peak_alert_enabled and cfg.capacity_enabled):
            return
        peak = _sensor_kw(cfg.month_peak_sensor)
        if peak is None:
            return
        month = "%04d-%02d" % (local.year, local.month)
        last = _peak_alert_state()
        last_month = last[0] if last else None
        last_peak = last[1] if last else None
        if not capacity.peak_alert_due(peak, month, last_month, last_peak, cfg):
            return
        previous = last_peak if last_month == month else None
        title, message = capacity.peak_alert_message(
            peak, local.isoformat(timespec="seconds"), previous, cfg)
        if not _notify(cfg, title, message):
            return
        _peak_alert_last = (month, peak)
        err = _write_json_atomic(PEAK_ALERT_PATH,
                                 {"month": month, "peak_kw": peak})
        if err:
            log.error(  # noqa: F821
                f"battery_planner: cannot save peak alert state: {err}")
    except Exception as exc:
        log.error(f"battery_planner: peak alert failed: {exc!r}")  # noqa: F821


# ---- forecast and solar series -----------------------------------------------

def _state_stamp(entity):
    """Freshness stamp of a sensor, or None. last_reported moves on every poll
    even when the value is unchanged; last_updated is the fallback. pyscript
    keeps both on the StateVal from state.get(), not in state.getattr()."""
    try:
        raw = state.get(entity)  # noqa: F821
    except Exception:
        return None
    stamp = cache.as_datetime(getattr(raw, "last_reported", None))
    if stamp is None:
        stamp = cache.as_datetime(getattr(raw, "last_updated", None))
    return stamp


def _read_forecast(cfg, now):
    """(payload, age in minutes). payload is None when the attribute is
    missing/empty or the sensor's data is older than timing.solar_cache_stale_minutes
    (then the old payload is not used). age is None when the sensor carries no
    usable stamp; the age check is skipped silently in that case."""
    payload = _state_attr(cfg.forecast_entity, cfg.forecast_attribute)
    if not isinstance(payload, dict) or not payload:
        return None, None
    age = cache.stamp_age_minutes(_state_stamp(cfg.forecast_entity), now)
    if age is not None and age > cfg.solar_cache_stale_minutes:
        _warn_hourly("forecast_age",
                     "forecast sensor %s has not refreshed for %s (limit %dm); "
                     "ignoring its old data" % (
                         cfg.forecast_entity, cache.format_minutes(age),
                         cfg.solar_cache_stale_minutes), now)
        return None, age
    return payload, age


def _forecast_ok():
    global _forecast_failures
    _forecast_failures = 0


def _forecast_failed(cfg, now):
    """Count the failure; past the threshold, nudge the REST sensor (bounded)."""
    global _forecast_failures, _refresh_calls
    _forecast_failures += 1
    if _forecast_failures < FORECAST_FAILURES_BEFORE_RETRY:
        return
    _refresh_calls = [t for t in _refresh_calls if (now - t).total_seconds() < 3600]
    if len(_refresh_calls) >= MAX_FETCHES_PER_HOUR - 1:  # keep 1 for the hourly poll
        return
    spacing = cfg.forecast_retry_minutes * 60 - GATE_TOLERANCE_SECONDS
    if _refresh_calls and (now - _refresh_calls[-1]).total_seconds() < spacing:
        return
    try:
        service.call(  # noqa: F821
            "homeassistant", "update_entity", entity_id=cfg.forecast_entity)
        _refresh_calls.append(now)
    except Exception as exc:
        log.warning(f"battery_planner: forecast refresh failed: {exc!r}")  # noqa: F821


def _signature(payload):
    text = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _solar_from_cache(cached):
    out = []
    for b in cached.blocks:
        out.append(series.ForecastSlot(
            datetime.fromisoformat(b["block_start"]),
            float(b["expected_kwh"]), False,
            int(b["source_resolution_minutes"])))
    return out


def _usage_from_cache(cached):
    out = []
    for b in cached.blocks:
        out.append(series.UsageSlot(
            datetime.fromisoformat(b["block_start"]),
            float(b["expected_kwh"]), int(b["sample_days"])))
    return out


def _store(kind, path, slots, cfg, local, source, extra):
    if kind == "solar":
        blocks = [{"block_start": s.block_start.isoformat(),
                   "expected_kwh": s.expected_kwh,
                   "source_resolution_minutes": s.source_resolution_minutes}
                  for s in slots]
    else:
        blocks = [{"block_start": s.block_start.isoformat(),
                   "expected_kwh": s.expected_kwh,
                   "sample_days": s.sample_days} for s in slots]
    payload = cache.to_dict(cache.CachedSeries(
        kind, local, source, cfg.fingerprint(), blocks))
    payload.update(extra)
    err = _write_json_atomic(path, payload)
    if err:
        log.warning(f"battery_planner: cache write failed ({kind}): {err}")  # noqa: F821


def _solar(cfg, local, span_start, span_end, payload, markers):
    """(slots, zero_fallback_flag). Never raises for missing inputs."""
    path = CACHE_DIR + "solar.json"
    data = _read_json(path)
    verdict, cached, _ = cache.evaluate(data, "solar", local, cfg)
    sig = _signature(payload) if payload else None
    usable = None
    if cached is not None:
        try:
            usable = _solar_from_cache(cached)
        except (KeyError, TypeError, ValueError):
            usable = None
    if usable is not None and verdict == "fresh" and (
            sig is None or data.get("forecast_signature") == sig):
        markers.append(cache.age_marker(cached, local))     # SC-014
        return usable, False
    if payload:
        built = series.solar_series(payload, cfg, span_start, span_end)
        if built and not built[0].is_zero_fallback:
            _store("solar", path, built, cfg, local, cfg.forecast_entity,
                   {"forecast_signature": sig})
            return built, False
    if usable is not None:                   # refresh impossible: use, mark age
        markers.append(cache.age_marker(cached, local))
        return usable, False
    return series.zero_solar_series(cfg, span_start, span_end), True


def _warn_hourly(kind, message, now):
    """log.warning at most once per HISTORY_WARN_MINUTES per kind."""
    last = _hist_warned.get(kind)
    if last is not None and (now - last).total_seconds() < HISTORY_WARN_MINUTES * 60:
        return
    _hist_warned[kind] = now
    log.warning("battery_planner: " + message)  # noqa: F821


def _hist_warn(kind, message, now):
    _warn_hourly(kind, "energy history: " + message, now)


def _history_block_context(cfg, boundary, price_map, solar, zero_fallback):
    """(forecast kWh, consumption price, injection price) of the block STARTING
    at `boundary`; each None when unknown (zero-solar fallback is not a forecast)."""
    forecast, consumption, injection = None, None, None
    if solar and not zero_fallback:
        for slot in solar:
            if slot.block_start == boundary and not slot.is_zero_fallback:
                forecast = slot.expected_kwh
    if price_map:
        width = timedelta(minutes=cfg.block_minutes)
        for point in price_map.values():
            if point.block_start <= boundary < point.block_start + width:
                consumption = point.consumption_price
                injection = point.injection_price
    return forecast, consumption, injection


def _record_history_inner(cfg, now, price_map, solar, zero_fallback, soc):
    global _hist_last, _hist_loaded
    if not cfg.history_enabled:
        return
    sensors = cfg.history_sensors()
    if not any(sensors.values()):
        return
    if not _hist_loaded:
        _hist_loaded = True
        _hist_last = history.snapshot_from_dict(
            _read_json(HISTORY_DIR + "last_snapshot.json"))
    boundary = history.block_floor(now, cfg.block_minutes)
    if _hist_last is not None and boundary <= _hist_last.boundary:
        return
    readings = {}
    for quantity in sensors:
        readings[quantity] = [_counter_kwh(e) for e in sensors[quantity]]
    forecast, consumption, injection = _history_block_context(
        cfg, boundary, price_map, solar, zero_fallback)
    snap = history.make_snapshot(boundary, now, readings, forecast,
                                 consumption, injection, soc)
    records = history.records_between(
        _hist_last, snap, cfg.block_minutes, ZoneInfo(cfg.timezone))
    by_date = {}
    for rec in records:
        by_date.setdefault(rec["local_date"], []).append(history.to_line(rec))
    for day in sorted(by_date):
        err = _append_text(HISTORY_DIR + "blocks-%s.jsonl" % day,
                           "\n".join(by_date[day]) + "\n")
        if err:                      # keep the old snapshot: retried next cycle
            _hist_warn("write", "cannot append %s records: %s" % (day, err), now)
            return
    _hist_last = snap
    err = _write_json_atomic(HISTORY_DIR + "last_snapshot.json",
                             history.snapshot_to_dict(snap))
    if err:
        _hist_warn("snapshot", "cannot save last_snapshot.json: %s" % err, now)
    if snap.failed:
        _hist_warn("sensors", "counters unreadable for %s; those values are "
                   "recorded as null" % ", ".join(snap.failed), now)


def _record_history(cfg, now, price_map=None, solar=None, zero_fallback=False,
                    soc=None):
    """Energy recorder entry point. NEVER raises, never affects a decision."""
    try:
        _record_history_inner(cfg, now, price_map, solar, zero_fallback, soc)
    except Exception as exc:
        try:
            _hist_warn("exception", "recorder failed: %r" % (exc,), now)
        except Exception:
            pass


def read_usage_history(cfg, local):
    """Per-INTERVAL household kWh as [(aware datetime, kwh)], oldest first.

    Read from the planner's own daily history files (blocks-YYYY-MM-DD.jsonl)
    covering cfg.usage_history_weeks weeks. Only blocks whose load_kwh is known
    are returned, so with no `load` counter and no complete solar + battery
    counter set configured this is [] (and V4 keeps grid charging off). Missing
    files are normal; unreadable files and corrupt lines are skipped, counted
    and warned about (rate-limited). Interval energy, never meter totals.
    """
    tz = ZoneInfo(cfg.timezone)
    window = timedelta(weeks=cfg.usage_history_weeks)
    day = (local - window).astimezone(tz).date()
    last = local.astimezone(tz).date()
    paths = []
    while day <= last:
        paths.append(HISTORY_DIR + "blocks-%s.jsonl" % day.isoformat())
        day = day + timedelta(days=1)
    found = _read_texts(paths)
    records, bad = [], 0
    for path in paths:
        text = found["texts"].get(path)
        if text is not None:
            recs, n = history.parse_lines(text)
            records.extend(recs)
            bad += n
    if bad or found["errors"]:
        _hist_warn("read", "skipped %d corrupt line(s), %d unreadable file(s)"
                   % (bad, len(found["errors"])), local)
    return history.usage_series(records, since=local - window)


def _usage(cfg, local, span_start, span_end, markers):
    """(slots, distinct_history_days). Rebuilt daily (cache.evaluate)."""
    path = CACHE_DIR + "usage.json"
    data = _read_json(path)
    verdict, cached, _ = cache.evaluate(data, "usage", local, cfg)
    usable, days = None, 0
    if cached is not None:
        try:
            usable, days = _usage_from_cache(cached), int(data.get("history_days", 0))
        except (KeyError, TypeError, ValueError):
            usable = None
    if usable is not None and verdict == "fresh":
        markers.append(cache.age_marker(cached, local))     # SC-014
        return usable, days
    samples = read_usage_history(cfg, local)
    if samples:
        tz = ZoneInfo(cfg.timezone)
        days = len({ts.astimezone(tz).date() for ts, _ in samples})
        built = series.usage_profile(samples, cfg, span_start, span_end)
        _store("usage", path, built, cfg, local, "energy_history",
               {"history_days": days})
        return built, days
    markers.append("usage_history_unavailable")
    if usable is not None:                   # refresh impossible: use, mark age
        markers.append(cache.age_marker(cached, local))
        return usable, days
    return series.usage_profile([], cfg, span_start, span_end), 0


# ---- grid state ----------------------------------------------------------------

def _own_grid_charge_kw(cfg, local):
    """Grid power this planner is itself drawing right now (kW), else 0.0.

    Non-zero only when a real driver (not "logging", which transmits nothing)
    was handed the previous decision, that decision was a grid charge, and it
    is at most 2 evaluation intervals old (a skipped cycle or halt must not
    leave a stale figure behind). The metered offtake includes this charge;
    capacity.budget_kw subtracts it to get household draw, so the budget does
    not shrink by the planner's own charging (no every-other-cycle flapping).
    """
    if cfg.inverter_type == "logging" or _last_grid_charge is None:
        return 0.0
    when, kw = _last_grid_charge
    age = (local - when).total_seconds()
    if age < 0 or age > 2 * cfg.evaluation_interval_minutes * 60:
        return 0.0
    return kw


def _load_mode_samples():
    """Restore the detector samples saved by an earlier process (once)."""
    global _mode_samples, _mode_loaded, _mode_saved
    if _mode_loaded:
        return
    _mode_loaded = True
    loaded = capacity.samples_from_data(
        _read_json(MODE_STATE_PATH), MAX_MODE_SAMPLES)
    _mode_saved = capacity.samples_to_data(loaded)
    _mode_samples = loaded + _mode_samples


def _save_mode_samples():
    """Persist the samples that can still vote; skipped when unchanged."""
    global _mode_saved
    data = capacity.samples_to_data(_mode_samples)
    if data == _mode_saved:
        return
    err = _write_json_atomic(MODE_STATE_PATH, data)
    if err:
        log.error(  # noqa: F821
            f"battery_planner: cannot save average-mode state: {err}")
    else:
        _mode_saved = data


def _grid_state(cfg, local):
    """capacity.GridState, or None (capacity off, or a sensor is unreadable)."""
    global _mode_samples
    if not cfg.capacity_enabled:
        return None
    offtake = _sensor_kw(cfg.offtake_sensor)
    reported = _sensor_kw(cfg.quarter_hour_average_sensor)
    peak = _sensor_kw(cfg.month_peak_sensor)
    if offtake is None or reported is None or peak is None:
        return None
    start = capacity.window_start_of(local)
    _load_mode_samples()
    _mode_samples.append(capacity.Sample(
        start, (local - start).total_seconds() / 60.0, reported, offtake))
    _mode_samples = _mode_samples[-MAX_MODE_SAMPLES:]
    _save_mode_samples()
    verdict = capacity.detect_average_mode(_mode_samples, cfg)
    return capacity.build_state(
        offtake, 0.0, local, peak, cfg, reported_average_kw=reported,
        average_mode=verdict.mode, mode_confidence=verdict.confidence,
        own_grid_charge_kw=_own_grid_charge_kw(cfg, local))


def _remember_grid_charge(d, local):
    """Note a recorded decision's grid-charge power for the next cycle."""
    global _last_grid_charge
    if d.action == "charge" and d.charge_source == "grid":
        _last_grid_charge = (local, d.target_power_kw)
    else:
        _last_grid_charge = None


def _suppress_grid_charge(d, veto, why):
    """Replace a grid-charge decision with idle; keep the original visible."""
    return rules.Decision(
        "idle", 0.0, "S6",
        "%s; grid charge suppressed (was %s: %s)" % (why, d.selector, d.reasoning),
        list(d.vetoes_fired),
        list(d.suppressed) + [(d.selector, "charge", veto)],
        None, d.block_start)


# ---- the cycle -----------------------------------------------------------------

def _cycle(now):
    started = _now()
    cfg = _load_config()
    if cfg is None:
        return
    local = now.astimezone(ZoneInfo(cfg.timezone))
    _check_peak_alert(cfg, local)        # before prices: also works in a halt
    price_map, cause = _read_prices(cfg, local)
    if cause is not None:
        _halt(cfg, local, cause)
        _record_history(cfg, now)        # counters keep counting during a halt
        return
    _recover(local)

    markers = []
    payload, forecast_age = _read_forecast(cfg, now)
    if payload is None:
        _forecast_failed(cfg, now)
    else:
        _forecast_ok()
        if forecast_age is not None and forecast_age >= FORECAST_AGE_MARKER_MINUTES:
            markers.append("forecast_age=" + cache.format_minutes(forecast_age))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    span_end = midnight.astimezone(timezone.utc) + timedelta(hours=SERIES_SPAN_HOURS)
    solar, zero_fallback = _solar(cfg, local, midnight, span_end, payload, markers)
    usage, history_days = _usage(cfg, local, midnight, span_end, markers)
    window_days = cfg.usage_history_weeks * 7
    coverage = history_days if history_days < window_days else None

    charge, charge_is_stub, charge_marker = inverter.read_charge(
        cfg.inverter_type, CORE_DIR)
    if charge_marker:
        markers.append(charge_marker)
    bat = battery.from_percent(charge, cfg, is_stubbed=charge_is_stub)
    block_start = local.replace(
        minute=(local.minute // cfg.block_minutes) * cfg.block_minutes,
        second=0, microsecond=0)
    traj = trajectory.project(bat, solar, usage, price_map, cfg,
                              start_time=block_start)
    grid = _grid_state(cfg, local)
    d = rules.decide(traj, price_map, bat, grid, cfg, local,
                     usage_history_available=history_days > 0)

    if d.action == "charge" and d.charge_source == "grid":
        if _state_value(GUARD_FLAG_ENTITY) == "on":
            d = _suppress_grid_charge(d, "GUARD", "peak guard is shaving")
        elif cfg.capacity_enabled and grid is None:
            d = _suppress_grid_charge(d, "NOGRID", "grid sensors unreadable")
    if cfg.capacity_enabled and grid is None:
        markers.append("grid_sensors_unavailable")

    price_now = None
    for p in price_map.values():
        if p.block_start <= local < p.block_start + timedelta(minutes=cfg.block_minutes):
            price_now = p
    degraded = decision.degraded_markers(
        bat, solar_zero_fallback=zero_fallback, cache_markers=markers,
        usage_samples=coverage)
    took = int((_now() - started).total_seconds() * 1000)
    record = decision.build(d, traj, bat, price_now, degraded, now=local,
                            duration_ms=took, grid_state=grid, config=cfg)
    if not inverter.apply(d.action, d.target_power_kw, record,
                          log_dir=DECISIONS_LOG_DIR,
                          inverter_type=cfg.inverter_type,
                          driver_dir=CORE_DIR,
                          resend_minutes=cfg.inverter_resend_minutes,
                          state_dir=STATE_DIR):
        log.error("battery_planner: decision could not be recorded")  # noqa: F821
    else:
        _remember_grid_charge(d, local)
    if took > SLOW_CYCLE_MS:
        log.warning(f"battery_planner: slow cycle {took}ms")  # noqa: F821
    _record_history(cfg, now, price_map, solar, zero_fallback,
                    None if charge_is_stub else charge)


def _due(now):
    if _last_run is None:
        return True
    minutes = _config.evaluation_interval_minutes if _config else CONFIG_RETRY_MINUTES
    return (now - _last_run).total_seconds() >= minutes * 60 - GATE_TOLERANCE_SECONDS


def _skip(now, started):
    """A due cycle found the previous one still running: say so (NFR-001)."""
    interval = _config.evaluation_interval_minutes if _config else CONFIG_RETRY_MINUTES
    busy = int((now - started).total_seconds())
    msg = "battery_planner: cycle skipped, previous cycle running for %ds" % busy
    if busy > 2 * interval * 60:
        log.error(msg + " (hung? older than 2 intervals)")  # noqa: F821
    else:
        log.warning(msg)  # noqa: F821
    tz = ZoneInfo(_config.timezone) if _config else timezone.utc
    _log_line(" | ".join([
        now.astimezone(tz).isoformat(timespec="seconds"), "SKIP",
        "cause=previous_cycle_running", "busy_for=%ds" % busy]),
        now.astimezone(tz))


def run_cycle(now=None):
    """Gate on the interval, run one cycle, never let an exception escape."""
    global _last_run, _cycle_started_at
    now = now if now is not None else _now()
    owner = False
    try:
        if not _due(now):
            return
        if _cycle_started_at is not None:     # no await between check and set
            _skip(now, _cycle_started_at)
            return
        _cycle_started_at = now
        owner = True
        _last_run = now
        _ensure_core()
        _cycle(now)
    except Exception as exc:
        log.error(f"battery_planner: cycle failed, trigger kept: {exc!r}")  # noqa: F821
    finally:
        if owner:
            _cycle_started_at = None


@time_trigger("cron(* * * * *)")  # noqa: F821
def battery_planner_cycle():
    run_cycle()
