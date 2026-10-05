"""Daily report: summarise one local day of decisions and energy history. Pure.

No file I/O, no clock (the generation time is a parameter), no Home Assistant
imports. The housekeeping script reads the two input files, calls
``build_report`` and stores the Markdown it returns.

Inputs (both optional, either may be missing or partly broken)
-----------------------------------------------------------------
* the day's decision log, one line per decision in the fixed field order
  (see decision.format_record), plus HALT / RECOVERED / SKIP lines
* the day's energy history, one JSON object per block (see history.py)

Rules
-----
* Tolerant: a line that cannot be understood is skipped and counted, never an
  error. Counting never double-counts: a veto or degraded marker is counted
  once per record.
* Units and rounding follow the log: kW and kWh with two decimals.
* ``build_report`` returns None when there is nothing to report (both inputs
  missing or blank): the caller writes no file.
* Expected blocks for a local day come from the real length of that day in
  the zone (92 on the spring DST change, 100 on the autumn one).
"""
import math
import re
from datetime import date, datetime, timedelta, timezone

import history

ACTIONS = ("charge", "discharge", "export", "idle")
# S2 no longer exists, but older logs still carry it and must still parse.
SELECTORS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6")
SPECIAL_KINDS = ("HALT", "RECOVERED", "SKIP")
GUARD_GAP_SECONDS = 120          # a longer pause between guard lines ends an episode
MAX_EPISODES_LISTED = 10
NULL_SHARE_NOTED = 0.10          # a quantity missing in >= 10 % of blocks is noted

# (history field, label) in report order.
ENERGY_ROWS = (
    ("import_kwh", "Imported from the grid"),
    ("export_kwh", "Exported to the grid"),
    ("solar_kwh", "Solar produced"),
    ("battery_charge_kwh", "Battery charged"),
    ("battery_discharge_kwh", "Battery discharged"),
    ("load_kwh", "Household load"),
)

_KW = re.compile(r"^(-?[0-9]+(?:\.[0-9]+)?)kW$")
_VETO_ID = re.compile(r"V[0-9]+")


def decisions_name(day):
    return "decisions-%s.log" % day.isoformat()


def history_name(day):
    return "blocks-%s.jsonl" % day.isoformat()


def report_name(day):
    return "report-%s.md" % day.isoformat()


def _kw(value):
    match = _KW.match(value or "")
    return float(match.group(1)) if match else None


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def expected_blocks(day, tz, block_minutes):
    """Blocks in the local calendar `day` (DST-aware: 92 / 96 / 100 at 15 min)."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=tz)
    minutes = (end.astimezone(timezone.utc)
               - start.astimezone(timezone.utc)).total_seconds() / 60
    return int(round(minutes / block_minutes))


# ---- decision log ------------------------------------------------------------

def _parse_line(line):
    """(kind, time, fields) or None for a line that cannot be understood."""
    parts = line.split(" | ")
    if len(parts) < 2:
        return None
    try:
        when = datetime.fromisoformat(parts[0].strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    head = parts[1].strip()
    kind = head if head in SPECIAL_KINDS else "decision"
    fields = {}
    for token in parts[1:] if kind == "decision" else parts[2:]:
        key, sep, value = token.partition("=")
        if not sep or not key.strip():
            return None
        fields[key.strip()] = value.strip()
    if kind == "decision" and (fields.get("action") not in ACTIONS
                               or fields.get("selector") not in SELECTORS):
        return None
    return kind, when, fields


def _bump(counter, key):
    counter[key] = counter.get(key, 0) + 1


def parse_decision_log(text):
    """Summarise a decision log. Never raises; bad lines are counted."""
    s = {"records": 0, "malformed": 0, "actions": {}, "selectors": {},
         "vetoes": {}, "suppressed": 0, "degraded": {},
         "halt_cycles": 0, "outages": {}, "recovered": [], "skips": 0,
         "guard_records": 0, "episodes": [],
         "max_avg": None, "max_avg_at": None, "max_ceiling": None,
         "min_budget": None, "min_budget_at": None}
    last_guard = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parsed = _parse_line(line)
        if parsed is None:
            s["malformed"] += 1
            continue
        kind, when, fields = parsed
        if kind == "HALT":
            s["halt_cycles"] += 1
            key = fields.get("entered") or when.isoformat(timespec="seconds")
            s["outages"].setdefault(key, fields.get("cause", "unknown"))
            continue
        if kind == "RECOVERED":
            s["recovered"].append({"time": when,
                                   "halted_for": fields.get("halted_for")})
            continue
        if kind == "SKIP":
            s["skips"] += 1
            continue
        s["records"] += 1
        _bump(s["actions"], fields["action"])
        _bump(s["selectors"], fields["selector"])
        veto_ids = set()
        for token in (fields.get("vetoes") or "none").split(","):
            token = token.strip()
            if not token or token == "none":
                continue
            veto_ids.update(_VETO_ID.findall(token.split("(")[0]))
            if "(suppressed" in token:
                s["suppressed"] += 1
        for veto in veto_ids:
            _bump(s["vetoes"], veto)
        markers = set()
        for token in (fields.get("degraded") or "none").split(","):
            token = token.strip()
            if token and token != "none":
                markers.add(token.split("=")[0])
        for marker in markers:
            _bump(s["degraded"], marker)
        avg = _kw(fields.get("avg"))
        if avg is not None and (s["max_avg"] is None or avg > s["max_avg"]):
            s["max_avg"], s["max_avg_at"] = avg, when
        ceiling = _kw(fields.get("ceiling"))
        if ceiling is not None and (s["max_ceiling"] is None
                                    or ceiling > s["max_ceiling"]):
            s["max_ceiling"] = ceiling
        budget = _kw(fields.get("budget"))
        if budget is not None and (s["min_budget"] is None
                                   or budget < s["min_budget"]):
            s["min_budget"], s["min_budget_at"] = budget, when
        if fields.get("source") == "guard":
            s["guard_records"] += 1
            power = _kw(fields.get("power")) or 0.0
            if (last_guard is None or (when - last_guard).total_seconds()
                    > GUARD_GAP_SECONDS):
                s["episodes"].append({"start": when, "end": when,
                                      "records": 0, "max_kw": 0.0})
            episode = s["episodes"][-1]
            episode["end"] = when
            episode["records"] += 1
            episode["max_kw"] = max(episode["max_kw"], power)
            last_guard = when
    return s


# ---- energy history ------------------------------------------------------------

def summarise_history(text, day, tz, block_minutes):
    """Summarise a day's block records (duplicates by block start: last wins)."""
    records, bad = history.parse_lines(text)
    by_start = {}
    for rec in records:
        start = datetime.fromisoformat(rec["block_start"])
        if start.tzinfo is None:
            bad += 1
            continue
        start = start.astimezone(timezone.utc)
        if start.astimezone(tz).date() == day:
            by_start[start] = rec
    blocks = [by_start[k] for k in sorted(by_start)]
    totals = {}
    for field, _label in ENERGY_ROWS:
        values = [b[field] for b in blocks if _finite(b.get(field))]
        totals[field] = {"sum": sum(values) if values else None,
                         "known": len(values)}
    paired = [b for b in blocks
              if _finite(b.get("solar_kwh"))
              and _finite(b.get("forecast_solar_kwh"))]
    paired_solar = sum(b["solar_kwh"] for b in paired)
    paired_forecast = sum(b["forecast_solar_kwh"] for b in paired)
    ratio = paired_solar / paired_forecast if paired_forecast > 0 else None
    return {
        "malformed": bad,
        "recorded": len(blocks),
        "complete": len([b for b in blocks if b.get("complete") is True]),
        "expected": expected_blocks(day, tz, block_minutes),
        "totals": totals,
        "paired_blocks": len(paired), "paired_solar": paired_solar,
        "paired_forecast": paired_forecast, "ratio": ratio,
        "load_measured": len([b for b in blocks
                              if b.get("load_source") == "measured"
                              and _finite(b.get("load_kwh"))]),
        "load_derived": len([b for b in blocks
                             if b.get("load_source") == "derived"
                             and _finite(b.get("load_kwh"))]),
    }


# ---- rendering -------------------------------------------------------------------

def _counts(counter, order=None):
    keys = list(order) if order else sorted(counter)
    return ", ".join("%s %d" % (k, counter.get(k, 0)) for k in keys
                     if order or k in counter) or "none"


def _n(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _hm(when, tz):
    return when.astimezone(tz).strftime("%H:%M")


def _decision_lines(s, tz):
    out = ["## Decisions", ""]
    out.append("- Records: %d" % s["records"])
    out.append("- Actions: %s" % _counts(s["actions"], ACTIONS))
    out.append("- Selectors: %s" % _counts(s["selectors"]))
    if s["vetoes"]:
        out.append("- Vetoes seen: %s (%d proposals suppressed)"
                   % (_counts(s["vetoes"]), s["suppressed"]))
    else:
        out.append("- Vetoes seen: none")
    out.append("- Degraded inputs: %s" % _counts(s["degraded"]))
    if s["halt_cycles"] or s["recovered"] or s["skips"]:
        outages = ["%s (since %s)" % (cause, entered)
                   for entered, cause in sorted(s["outages"].items())]
        out.append("- Halts: %s without a decision%s"
                   % (_n(s["halt_cycles"], "cycle"),
                      ": " + "; ".join(outages) if outages else ""))
        recovered = ["%s after %s" % (_hm(r["time"], tz),
                                      r["halted_for"] or "?")
                     for r in s["recovered"]]
        out.append("- Recovered: %d%s" % (len(recovered),
                   " (" + ", ".join(recovered) + ")" if recovered else ""))
        out.append("- Skipped cycles: %d" % s["skips"])
    else:
        out.append("- Halts, recoveries, skipped cycles: none")
    if s["episodes"]:
        out.append("- Peak guard: %s (%s)"
                   % (_n(len(s["episodes"]), "shaving episode"),
                      _n(s["guard_records"], "record")))
        for ep in s["episodes"][:MAX_EPISODES_LISTED]:
            out.append("  - %s-%s, %s, up to %.2fkW"
                       % (_hm(ep["start"], tz), _hm(ep["end"], tz),
                          _n(ep["records"], "record"), ep["max_kw"]))
        more = len(s["episodes"]) - MAX_EPISODES_LISTED
        if more > 0:
            out.append("  - and %d more" % more)
    else:
        out.append("- Peak guard: no shaving")
    if s["max_avg"] is None:
        out.append("- Highest quarter-hour average: n/a")
    else:
        line = "- Highest quarter-hour average: %.2fkW at %s" % (
            s["max_avg"], _hm(s["max_avg_at"], tz))
        if s["max_ceiling"] is not None:
            line += " (ceiling %.2fkW" % s["max_ceiling"]
            line += ", above it)" if s["max_avg"] > s["max_ceiling"] else ")"
        out.append(line)
    if s["min_budget"] is None:
        out.append("- Lowest charging budget: n/a")
    else:
        out.append("- Lowest charging budget: %.2fkW at %s"
                   % (s["min_budget"], _hm(s["min_budget_at"], tz)))
    return out


def _energy_lines(h):
    out = ["## Energy", "", "| Quantity | kWh | Blocks with a value |",
           "|---|---:|---:|"]
    for field, label in ENERGY_ROWS:
        t = h["totals"][field]
        value = "n/a" if t["sum"] is None else "%.2f" % t["sum"]
        out.append("| %s | %s | %d |" % (label, value, t["known"]))
    out.append("")
    if h["ratio"] is None:
        out.append("- Solar against forecast: n/a (no block has both)")
    else:
        out.append("- Solar against forecast: %.2f kWh produced of %.2f kWh "
                   "forecast, %.0f%% (%d blocks with both)"
                   % (h["paired_solar"], h["paired_forecast"],
                      100 * h["ratio"], h["paired_blocks"]))
    if h["load_measured"] or h["load_derived"]:
        out.append("- Household load: %d blocks measured, %d derived from the "
                   "other counters" % (h["load_measured"], h["load_derived"]))
    out.append("- Blocks recorded: %d of %d expected (%d complete)"
               % (h["recorded"], h["expected"], h["complete"]))
    return out


def _quality_notes(d, h):
    notes = []
    if d is None:
        notes.append("No decision log for this day.")
    else:
        if d["malformed"]:
            notes.append("%d decision log line%s could not be read and "
                         "%s skipped." % (d["malformed"],
                         "" if d["malformed"] == 1 else "s",
                         "was" if d["malformed"] == 1 else "were"))
    if h is None:
        notes.append("No energy history for this day.")
    else:
        if h["malformed"]:
            notes.append("%d history line%s could not be read and %s skipped."
                         % (h["malformed"], "" if h["malformed"] == 1 else "s",
                            "was" if h["malformed"] == 1 else "were"))
        if h["recorded"] < h["expected"]:
            notes.append("Only %d of %d blocks were recorded (planner or Home "
                         "Assistant was not running, or the sensors were "
                         "unreadable)." % (h["recorded"], h["expected"]))
        if h["recorded"] > h["expected"]:
            notes.append("%d blocks recorded, more than the %d expected."
                         % (h["recorded"], h["expected"]))
        if h["recorded"]:
            for field, label in ENERGY_ROWS:
                known = h["totals"][field]["known"]
                missing = h["recorded"] - known
                if missing and missing >= NULL_SHARE_NOTED * h["recorded"]:
                    why = ("not configured or never readable"
                           if known == 0 else "counter unreadable or reset")
                    notes.append("%s has no value in %d of %d blocks (%s)."
                                 % (label, missing, h["recorded"], why))
    return notes


def build_report(day, decisions_text, history_text, *, tz, block_minutes, now):
    """Markdown report for the local `day`, or None when there is no data.

    decisions_text / history_text: file contents, or None when the file does
    not exist. `now` is the (aware) generation time, shown in the header.
    """
    has_d = decisions_text is not None and bool(decisions_text.strip())
    has_h = history_text is not None and bool(history_text.strip())
    if not has_d and not has_h:
        return None
    d = parse_decision_log(decisions_text) if has_d else None
    h = (summarise_history(history_text, day, tz, block_minutes)
         if has_h else None)
    out = ["# Battery planner report for %s" % day.isoformat(), "",
           "Generated %s (%s)." % (
               now.astimezone(tz).isoformat(timespec="seconds"), tz.key
               if hasattr(tz, "key") else str(tz)), ""]
    if d is not None:
        out += _decision_lines(d, tz) + [""]
    if h is not None:
        out += _energy_lines(h) + [""]
    notes = _quality_notes(d, h)
    if notes:
        out += ["## Data quality", ""] + ["- " + n for n in notes] + [""]
    return "\n".join(out)


# ---- which days still need a report -------------------------------------------------

def days_to_report(today, decision_names, history_names, report_names, span=7):
    """Completed local days (yesterday back `span` days) with data and no report.

    Oldest first. A day counts as having data when its decision log or its
    history file exists. Existing reports are never selected (no overwrite).
    """
    have_data = set(decision_names) | set(history_names)
    existing = set(report_names)
    out = []
    for back in range(span, 0, -1):
        day = today - timedelta(days=back)
        if report_name(day) in existing:
            continue
        if decisions_name(day) in have_data or history_name(day) in have_data:
            out.append(day)
    return out
