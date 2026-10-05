"""Housekeeping: the daily report and the retention cleanup. A pyscript
script; the only code here that touches files and the clock. The summarising
lives in pyscript/modules/report.py, the choice of what may be deleted in
pyscript/modules/retention.py.

Schedule (HA's own time zone fires the cron; the DAY is decided in the
configured `timezone`)
  00:10  housekeeping_report    write the report of the day that just ended
  03:30  housekeeping_cleanup   delete files older than retention.keep_days

Daily report
------------
* Output: <config>/battery_planner/history/report-YYYY-MM-DD.md, from that
  day's decisions-YYYY-MM-DD.log and history/blocks-YYYY-MM-DD.jsonl. Either
  input may be missing; with neither, nothing is written.
* Every run looks at the last 7 completed local days and writes the reports
  that are MISSING (so a restart, an outage at 00:10 or a first install fills
  the gaps). An existing report is never rewritten.
* report.enabled: false turns it off. A broken config file means no report
  that night (logged), never an exception.

Retention cleanup
-----------------
* Deletes, by the DATE IN THE FILE NAME (never the modification time), files
  strictly older than retention.keep_days (default 90; the file exactly
  keep_days old stays): battery_planner/decisions-YYYY-MM-DD.log and
  battery_planner/history/blocks-YYYY-MM-DD.jsonl and report-YYYY-MM-DD.md.
  Nothing else is ever touched (state/, cache/, last_snapshot.json,
  user_config.yaml, other names, directories, symlinks).
* Cron only (no run at start). It runs after the report so a day is
  reported before it can expire. A file that vanished meanwhile is ignored;
  a file that cannot be removed gets a warning (at most MAX_FAILURE_WARNINGS
  per run) and the next night tries again. One info line with the counts.
* A broken config file means nothing is deleted that night (logged).

Pyscript notes
--------------
* Every open()/os call is inside a @pyscript_executor helper (native code in
  an executor thread).
* NATIVE CORE LOADER: same pattern as battery_planner.py and peak_guard.py (a
  pyscript script cannot import another, so the ~25-line loader is copied).
  Files under pyscript/modules/ use constructs pyscript's interpreter lacks
  (generator expressions, @property, ...), so config, history, report and retention are
  loaded as ordinary CPython under the private names housekeeping_core_<name>
  and also registered under their bare names for the process lifetime (core
  code imports siblings at import/call time, and the core directory is not on
  sys.path in HA). A bare name already pointing at the same file (loaded by
  the planner) is REUSED. The bare names `report` and `retention` resolve to
  these modules for every other importer in the HA process. A failed load
  undoes its aliases. Natively loaded modules are NOT hot-reloaded: a change
  to a core file needs a Home Assistant restart.
* Only this file and pyscript/modules/inverter.py (and the other two
  top-level scripts) are interpreted; a test lints them.
* Never raises: a failing job logs one error and ends.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Pure core, bound natively by _ensure_core().
site_config = history = report = retention = None

# ---- locations (tests redirect these) --------------------------------------
CONFIG_PATH = "/config/battery_planner/user_config.yaml"
LOG_DIR = "/config/battery_planner/"
HISTORY_DIR = "/config/battery_planner/history/"
CORE_DIR = "/config/pyscript/modules"
# Dependency order (report needs history). inverter is NOT in this list.
CORE_MODULES = ("config", "history", "report", "retention")

REPORT_BACKFILL_DAYS = 7
MAX_FAILURE_WARNINGS = 5          # per cleanup run

_core_ready = False
_cfg = {"mtime": None, "config": None, "error": None}


def _now():
    return datetime.now(timezone.utc)


# ---- native core loader ------------------------------------------------------

@pyscript_executor  # noqa: F821  (provided by pyscript at runtime)
def _load_core(core_dir, names):
    """Import the pure core as CPython modules. Returns {name: module}."""
    import importlib.util
    import os
    import sys
    loaded, saved = {}, {}
    try:
        for name in names:
            path = os.path.join(core_dir, name + ".py")
            full = "housekeeping_core_" + name
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
    global _core_ready, site_config, history, report, retention
    if _core_ready:
        return
    mods = _load_core(CORE_DIR, CORE_MODULES)
    site_config, history, report = (mods["config"], mods["history"],
                                    mods["report"])
    retention = mods["retention"]
    _core_ready = True


# ---- file helpers: ALL blocking I/O, each in an executor thread -------------

@pyscript_executor  # noqa: F821
def _load_yaml_if_changed(path, known_mtime):
    """(mtime, parsed) or (mtime, None) when the file is unchanged."""
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


@pyscript_executor  # noqa: F821
def _list_names(directory):
    """Names of the regular files (symlinks excluded) in `directory`; [] if
    the directory is missing or unreadable."""
    import os
    try:
        with os.scandir(directory) as entries:
            return sorted([e.name for e in entries
                           if e.is_file(follow_symlinks=False)])
    except OSError:
        return []


@pyscript_executor  # noqa: F821
def _read_text(path):
    """File contents, or None when missing or unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


@pyscript_executor  # noqa: F821
def _write_text_new(path, text):
    """Write `text` atomically (temp file, fsync, rename) unless `path`
    already exists. Error text or None."""
    import os
    tmp = path + ".tmp"
    try:
        if os.path.exists(path):
            return "exists"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        return repr(exc)
    return None


@pyscript_executor  # noqa: F821
def _delete_files(paths):
    """Remove each regular file in `paths`. Returns (deleted, failures) where
    failures is a list of (path, error text). A file that is already gone is
    not a failure; a symlink or directory is skipped, never followed."""
    import os
    deleted, failures = [], []
    for path in paths:
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            os.remove(path)
            deleted.append(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append((path, repr(exc)))
    return deleted, failures


# ---- config ------------------------------------------------------------------

def _load_config():
    """Current SiteConfig (reloaded when the file changes), or None."""
    try:
        mtime, data = _load_yaml_if_changed(CONFIG_PATH, _cfg["mtime"])
        if data is not None or _cfg["config"] is None:
            _cfg["config"] = site_config.from_dict(data)
            _cfg["error"] = None
        _cfg["mtime"] = mtime
    except Exception as exc:
        _cfg["config"], _cfg["mtime"], _cfg["error"] = None, None, str(exc)
        log.error(f"housekeeping: config unusable, nothing done: {exc}")  # noqa: F821
    return _cfg["config"]


# ---- daily report ------------------------------------------------------------

def _report_day(cfg, tz, day, now):
    """Write the report of `day` if there is data. True when written."""
    decisions_text = _read_text(LOG_DIR + report.decisions_name(day))
    history_text = _read_text(HISTORY_DIR + report.history_name(day))
    text = report.build_report(
        day, decisions_text, history_text, tz=tz,
        block_minutes=cfg.block_minutes, now=now)
    if text is None:
        return False
    err = _write_text_new(HISTORY_DIR + report.report_name(day), text)
    if err == "exists":
        return False
    if err is not None:
        log.warning(f"housekeeping: report for {day} not written: {err}")  # noqa: F821
        return False
    return True


def run_report(now=None):
    """Write the reports that are missing for the last completed days."""
    try:
        now = now if now is not None else _now()
        _ensure_core()
        cfg = _load_config()
        if cfg is None or not cfg.report_enabled:
            return
        tz = _zone(cfg.timezone)
        today = now.astimezone(tz).date()
        history_names = _list_names(HISTORY_DIR)
        days = report.days_to_report(
            today, _list_names(LOG_DIR), history_names, history_names,
            REPORT_BACKFILL_DAYS)
        written = []
        for day in days:
            try:
                if _report_day(cfg, tz, day, now):
                    written.append(day.isoformat())
            except Exception as exc:
                log.warning(f"housekeeping: report for {day} failed: {exc!r}")  # noqa: F821
        if written:
            log.info("housekeeping: wrote daily report for " + ", ".join(written))  # noqa: F821
    except Exception as exc:
        log.error(f"housekeeping: daily report failed: {exc!r}")  # noqa: F821


@time_trigger("cron(10 0 * * *)")  # noqa: F821
def housekeeping_report():
    run_report()


# ---- retention cleanup ---------------------------------------------------------

def _count_prefix(paths, prefix):
    return len([p for p in paths if p.rsplit("/", 1)[-1].startswith(prefix)])


def run_cleanup(now=None):
    """Delete the files older than retention.keep_days. Never raises."""
    try:
        now = now if now is not None else _now()
        _ensure_core()
        cfg = _load_config()
        if cfg is None:
            return
        tz = _zone(cfg.timezone)
        today = now.astimezone(tz).date()
        found = retention.files_to_delete(
            _list_names(LOG_DIR), _list_names(HISTORY_DIR), today,
            cfg.retention_keep_days)
        paths = []
        for key, name in found:
            base = HISTORY_DIR if key == retention.HISTORY_DIR_KEY else LOG_DIR
            paths.append(base + name)
        deleted, failures = _delete_files(paths)
        n_logs = _count_prefix(deleted, "decisions-")
        n_blocks = _count_prefix(deleted, "blocks-")
        n_reports = _count_prefix(deleted, "report-")
        log.info(  # noqa: F821
            f"housekeeping: cleanup removed {len(deleted)} files older than "
            f"{cfg.retention_keep_days} days ({n_logs} decision logs, "
            f"{n_blocks} history files, {n_reports} reports), "
            f"{len(failures)} could not be removed")
        for path, err in failures[:MAX_FAILURE_WARNINGS]:
            log.warning(f"housekeeping: cannot remove {path}: {err}")  # noqa: F821
        if len(failures) > MAX_FAILURE_WARNINGS:
            log.warning(  # noqa: F821
                f"housekeeping: {len(failures) - MAX_FAILURE_WARNINGS} more "
                "files could not be removed")
    except Exception as exc:
        log.error(f"housekeeping: cleanup failed: {exc!r}")  # noqa: F821


@time_trigger("cron(30 3 * * *)")  # noqa: F821
def housekeeping_cleanup():
    run_cleanup()
