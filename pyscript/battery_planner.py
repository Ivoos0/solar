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
  trajectory, capacity, rules, decision, cache) uses all of these, so it must
  run as ordinary CPython. _load_core (a @pyscript_executor helper, so the
  import stays off the event loop) loads each file with importlib under the
  private name battery_planner_core_<name>; no sys.path entry is added, so
  nothing can shadow or be shadowed by another `config`/`cache`/`series`/
  `decision`. The nine core modules are ALSO registered under their bare names
  in sys.modules for the process lifetime, because core functions import
  siblings at call time (decision.build does `import capacity`) and the core
  directory is not on sys.path in HA. Trade-off: those nine bare names now
  resolve to the core for every other importer in the HA process. Only these
  nine are aliased (never `inverter`); a failed load undoes its aliases.
  The adapter binds the results as its globals in _ensure_core(). `inverter`
  stays a normal pyscript import (it uses `log` and @pyscript_executor).
  CAVEAT: natively loaded modules are NOT hot-reloaded. A change to any core
  file needs a Home Assistant restart (pyscript.reload does not help).
  Only this file and pyscript/modules/inverter.py are interpreted; a test lints
  both for the constructs the interpreter cannot run.
* Overlap: a module-level busy marker (_cycle_started_at), checked and set at
  the top of the trigger (no await between, so atomic in pyscript) and cleared
  in `finally`. A DUE cycle that finds it set logs a warning and writes a SKIP
  line to decisions.log; a marker older than 2 intervals (a hung cycle, e.g. an
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
  decisions.log (own small executor helper, since inverter.apply only accepts
  DecisionRecords), one alert on entry then at most one per
  alerts.realert_minutes, and a RECOVERED line when prices return. Halt state
  is in memory only: an HA restart during an outage alerts once more.
  Forecast missing -> zero_solar_series + solar_zero_fallback marker and a
  bounded homeassistant.update_entity retry (never a halt).
* Alerts: service.call("notify", NOTIFY_SERVICE, target=[alerts.address]).
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
* Average-mode samples for capacity.detect_average_mode are kept in memory
  only; after a restart the mode is "assumed" until enough windows are seen.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import inverter          # the only core file pyscript interprets

# Pure core, bound natively by _ensure_core() (see docstring).
battery = cache = capacity = config = decision = None
prices = rules = series = trajectory = None

# ---- locations (tests redirect these) --------------------------------------
CONFIG_PATH = "/config/battery_planner/user_config.yaml"
CACHE_DIR = "/config/battery_planner/cache/"
DECISIONS_LOG_PATH = inverter.DEFAULT_LOG_PATH
CORE_DIR = "/config/pyscript/modules"
# Dependency order (rules needs capacity). inverter is NOT in this list.
CORE_MODULES = ("config", "prices", "series", "battery", "trajectory",
                "capacity", "rules", "decision", "cache")

# ---- Home Assistant entities -----------------------------------------------
# UNCONFIRMED on the live install (research.md open item 2 / WP08 T039): verify
# in Developer Tools -> States. A wrong name shows as a constant price halt.
PRICE_ENTITY = "sensor.entso_prices_current_electricity_market_price"
PRICE_ATTRIBUTE = "prices"            # list of {"time": ..., "price": ...}
FORECAST_ENTITY = "sensor.forecast_solar_estimate"
FORECAST_ATTRIBUTE = "watt_hours_period"
QUARTER_AVG_ENTITY = "sensor.slimmelezer_huidig_kwartiervermogen"
MONTH_PEAK_ENTITY = "sensor.slimmelezer_maandpiek"
# The netted offtake sensor id comes from config (capacity_tariff.offtake_sensor,
# default sensor.slimmelezer_power_consumed).
GUARD_FLAG_ENTITY = "pyscript.peak_guard_shaving"   # set by the peak guard (WP12)
NOTIFY_SERVICE = "notify"             # GUESS: name of the configured notifier
_BAD_STATES = (None, "", "unknown", "unavailable", "none", "None")

# ---- tuning that is not user config ----------------------------------------
GATE_TOLERANCE_SECONDS = 30
CONFIG_RETRY_MINUTES = 5              # cadence while the config is unusable
SERIES_SPAN_HOURS = 72
MAX_FETCHES_PER_HOUR = 12             # NFR-002, shared with the hourly poll
FORECAST_FAILURES_BEFORE_RETRY = 2
SLOW_CYCLE_MS = 5000                  # NFR-001
MAX_MODE_SAMPLES = 400

# ---- module state -----------------------------------------------------------
_core_ready = False
_cycle_started_at = None
_last_run = None
_config = None
_config_mtime = None
_config_error = None
_halt_state = None
_forecast_failures = 0
_refresh_calls = []
_mode_samples = []


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
    global _core_ready, battery, cache, capacity, config, decision
    global prices, rules, series, trajectory
    if _core_ready:
        return
    mods = _load_core(CORE_DIR, CORE_MODULES)
    battery, cache, capacity = mods["battery"], mods["cache"], mods["capacity"]
    config, decision, prices = mods["config"], mods["decision"], mods["prices"]
    rules, series, trajectory = mods["rules"], mods["series"], mods["trajectory"]
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


def _log_line(line):
    try:
        _append_line(DECISIONS_LOG_PATH, line)
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
    if _state_value(PRICE_ENTITY) is None:
        return None, "price entity %s unavailable" % PRICE_ENTITY
    entries = _state_attr(PRICE_ENTITY, PRICE_ATTRIBUTE)
    if not isinstance(entries, list) or not entries:
        return None, "attribute %s missing or empty" % PRICE_ATTRIBUTE
    try:
        price_map = prices.expand_to_blocks(entries, cfg, local)
    except (KeyError, TypeError, ValueError) as exc:
        return None, "price entries unparseable: %r" % (exc,)
    end = prices.horizon_end(price_map, cfg.block_minutes)
    if end is None or end <= local:  # input validation: a stale series is absent
        return None, "price series empty or already elapsed"
    return price_map, None


def _send_alert(cfg, cause, entered):
    try:
        service.call(  # noqa: F821
            "notify", NOTIFY_SERVICE,
            title="Battery planner halted: no price data",
            message=("No decisions are being made. Cause: %s. Halted since %s. "
                     "Re-alert every %d min." % (
                         cause, entered.isoformat(timespec="seconds"),
                         cfg.realert_minutes)),
            target=[cfg.alert_address])
        return True
    except Exception as exc:
        log.error(f"battery_planner: alert send failed: {exc!r}")  # noqa: F821
        return False


def _halt(cfg, local, cause):
    """Price outage: no decision; alert on entry, then per realert_minutes."""
    global _halt_state
    if _halt_state is None:
        log.error(f"battery_planner: HALT, {cause}")  # noqa: F821
        _halt_state = decision.HaltState(True, cause, local, None)
    last = _halt_state.last_alert_at
    if last is None or (local - last).total_seconds() >= cfg.realert_minutes * 60:
        if _send_alert(cfg, _halt_state.cause, _halt_state.entered_at):
            _halt_state = decision.HaltState(
                True, _halt_state.cause, _halt_state.entered_at, local)
    _log_line(decision.format_halt(_halt_state, local))


def _recover(local):
    global _halt_state
    if _halt_state is None:
        return
    entered = _halt_state.entered_at
    minutes = int((local - entered).total_seconds() // 60)
    _log_line(" | ".join([
        local.isoformat(timespec="seconds"), "RECOVERED",
        "cause=%s" % _halt_state.cause,
        "entered=%s" % entered.isoformat(timespec="seconds"),
        "halted_for=%dm" % minutes]))
    log.info("battery_planner: prices recovered, resuming decisions")  # noqa: F821
    _halt_state = None


# ---- forecast and solar series -----------------------------------------------

def _read_forecast():
    payload = _state_attr(FORECAST_ENTITY, FORECAST_ATTRIBUTE)
    return payload if isinstance(payload, dict) and payload else None


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
            "homeassistant", "update_entity", entity_id=FORECAST_ENTITY)
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
            _store("solar", path, built, cfg, local, FORECAST_ENTITY,
                   {"forecast_signature": sig})
            return built, False
    if usable is not None:                   # refresh impossible: use, mark age
        markers.append(cache.age_marker(cached, local))
        return usable, False
    return series.zero_solar_series(cfg, span_start, span_end), True


def read_usage_history(cfg, local):
    """Per-INTERVAL household kWh as [(aware datetime, kwh)], oldest first.

    NOT IMPLEMENTED: returns [] so the caller records usage_history_unavailable.
    No household-consumption entity is confirmed on this install, and HA's
    recorder statistics are hourly cumulative sums whose access from pyscript
    (recorder.get_statistics response or a websocket call) is unverified.
    Replace ONLY this function once the entity and route are confirmed; the
    rest of the adapter needs nothing else. Must return interval energy, not
    meter totals, covering cfg.usage_history_weeks weeks.
    """
    return []


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
    history = read_usage_history(cfg, local)
    if history:
        tz = ZoneInfo(cfg.timezone)
        days = len({ts.astimezone(tz).date() for ts, _ in history})
        built = series.usage_profile(history, cfg, span_start, span_end)
        _store("usage", path, built, cfg, local, "recorder", {"history_days": days})
        return built, days
    markers.append("usage_history_unavailable")
    if usable is not None:                   # refresh impossible: use, mark age
        markers.append(cache.age_marker(cached, local))
        return usable, days
    return series.usage_profile([], cfg, span_start, span_end), 0


# ---- grid state ----------------------------------------------------------------

def _grid_state(cfg, local):
    """capacity.GridState, or None (capacity off, or a sensor is unreadable)."""
    global _mode_samples
    if not cfg.capacity_enabled:
        return None
    offtake = _sensor_kw(cfg.offtake_sensor)
    reported = _sensor_kw(QUARTER_AVG_ENTITY)
    peak = _sensor_kw(MONTH_PEAK_ENTITY)
    if offtake is None or reported is None or peak is None:
        return None
    start = capacity.window_start_of(local)
    _mode_samples.append(capacity.Sample(
        start, (local - start).total_seconds() / 60.0, reported, offtake))
    _mode_samples = _mode_samples[-MAX_MODE_SAMPLES:]
    verdict = capacity.detect_average_mode(_mode_samples, cfg)
    return capacity.build_state(
        offtake, 0.0, local, peak, cfg, reported_average_kw=reported,
        average_mode=verdict.mode, mode_confidence=verdict.confidence)


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
    price_map, cause = _read_prices(cfg, local)
    if cause is not None:
        _halt(cfg, local, cause)
        return
    _recover(local)

    markers = []
    payload = _read_forecast()
    if payload is None:
        _forecast_failed(cfg, now)
    else:
        _forecast_ok()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    span_end = midnight.astimezone(timezone.utc) + timedelta(hours=SERIES_SPAN_HOURS)
    solar, zero_fallback = _solar(cfg, local, midnight, span_end, payload, markers)
    usage, history_days = _usage(cfg, local, midnight, span_end, markers)
    window_days = cfg.usage_history_weeks * 7
    coverage = history_days if history_days < window_days else None

    charge = inverter.read_charge_percent()
    bat = battery.from_percent(charge, cfg, is_stubbed=True)
    block_start = local.replace(
        minute=(local.minute // cfg.block_minutes) * cfg.block_minutes,
        second=0, microsecond=0)
    traj = trajectory.project(bat, solar, usage, price_map, cfg,
                              start_time=block_start)
    grid = _grid_state(cfg, local)
    d = rules.decide(traj, price_map, bat, grid, cfg, local)

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
                          log_path=DECISIONS_LOG_PATH):
        log.error("battery_planner: decision could not be recorded")  # noqa: F821
    if took > SLOW_CYCLE_MS:
        log.warning(f"battery_planner: slow cycle {took}ms")  # noqa: F821


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
        "cause=previous_cycle_running", "busy_for=%ds" % busy]))


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
