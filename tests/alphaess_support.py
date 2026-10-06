"""Shared helpers for the AlphaESS sensor tests (planner and guard).

Both adapters read their config from a YAML file; these helpers write one with
extra `battery:` keys, which the existing `write_config` helpers cannot do.
"""
import os
from pathlib import Path


_last_bump = 0.0


def bump_mtime(path, extra=10):
    """Move the config file's mtime forward so a running adapter reloads it.

    Strictly increasing across calls: two rewrites inside one clock tick would
    otherwise get the same mtime, and the adapter (which caches by mtime)
    would miss the second change.
    """
    global _last_bump
    path = Path(path)
    mtime = path.stat().st_mtime if path.exists() else 0
    _last_bump = max(mtime + extra, _last_bump + 1.0)
    os.utime(path, (_last_bump, _last_bump))


def battery_config(path, battery_lines=(), extra="", mode=None,
                   alert_lines=("sensor_enabled: false",)):
    """Write a valid user config with `battery_lines` inside `battery:`.

    The file's mtime is bumped so a running adapter reloads it.
    """
    path = Path(path)
    text = "battery:\n  capacity_kwh: 10.0\n"
    for line in battery_lines:
        text += "  %s\n" % line
    text += "alerts:\n  address: owner@example.com\n  notify_service: test_notifier\n"
    text += "  peak_enabled: false\n"
    for line in alert_lines:
        text += "  %s\n" % line
    if mode is not None:
        text += ("capacity_tariff:\n  quarter_hour_average_mode: %s\n"
                 "  stay_under_percent: 100\n" % mode)
    text += extra
    path.write_text(text, encoding="utf-8")
    bump_mtime(path)
