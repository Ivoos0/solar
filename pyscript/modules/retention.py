"""Retention: which old files the daily cleanup may delete. Pure.

No file I/O, no clock (today's local date is a parameter). The housekeeping
script lists the two directories, asks this module, and removes what it says.

Only three exact file name patterns are ever selected, each in ONE directory:

    battery_planner/            decisions-YYYY-MM-DD.log
    battery_planner/history/    blocks-YYYY-MM-DD.jsonl
    battery_planner/history/    report-YYYY-MM-DD.md

The date IN THE FILE NAME decides (never the modification time, which a copy or
restore resets). A file is selected when its date is strictly older than
`today - keep_days`: with keep_days=90 the file dated exactly 90 days ago is
kept and the one dated 91 days ago goes. Anything else is never selected: other
names, wrong directory for a pattern, names with an impossible date, names with
extra text before or after the pattern (temp files, backups). The caller passes
only regular files (no directories, no symlinks).
"""
import re
from datetime import date, datetime, timedelta

# (directory key, compiled pattern). Digits are ASCII only.
LOG_DIR_KEY = ""
HISTORY_DIR_KEY = "history"
_PATTERNS = (
    (LOG_DIR_KEY,
     re.compile(r"decisions-([0-9]{4})-([0-9]{2})-([0-9]{2})\.log")),
    (HISTORY_DIR_KEY,
     re.compile(r"blocks-([0-9]{4})-([0-9]{2})-([0-9]{2})\.jsonl")),
    (HISTORY_DIR_KEY,
     re.compile(r"report-([0-9]{4})-([0-9]{2})-([0-9]{2})\.md")),
)


def name_date(name, directory):
    """The date in `name` if it is a managed file name for `directory`
    (LOG_DIR_KEY or HISTORY_DIR_KEY), else None. Impossible dates give None."""
    for key, pattern in _PATTERNS:
        if key != directory:
            continue
        match = pattern.fullmatch(name)
        if match is None:
            continue
        try:
            return date(int(match.group(1)), int(match.group(2)),
                        int(match.group(3)))
        except ValueError:
            return None
    return None


def files_to_delete(log_names, history_names, today, keep_days):
    """[(directory key, file name)] to delete, oldest first.

    log_names: regular file names in battery_planner/. history_names: regular
    file names in battery_planner/history/. today: the local date (a date, not
    a datetime). keep_days: int >= 1 (ValueError otherwise, so a bad value can
    never delete everything).
    """
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) \
            or keep_days < 1:
        raise ValueError("keep_days must be an integer >= 1, got %r"
                         % (keep_days,))
    if not isinstance(today, date) or isinstance(today, datetime):
        raise ValueError("today must be a date, got %r" % (today,))
    cutoff = today - timedelta(days=keep_days)
    found = []
    for key, names in ((LOG_DIR_KEY, log_names),
                       (HISTORY_DIR_KEY, history_names)):
        for name in names:
            when = name_date(name, key)
            if when is not None and when < cutoff:
                found.append((when, key, name))
    found.sort()
    return [(key, name) for _when, key, name in found]
