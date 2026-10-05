"""Derived-series cache validity logic. Pure - no I/O, no clock.

The adapter reads and writes the files; this module only decides whether a
parsed series may be used. `now` is always a parameter. Staleness (refresh,
but usable as a fallback) and invalidation (discard) are separate questions.
Nothing here raises except CacheError.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KINDS = ("solar", "usage")
_REQUIRED = ("kind", "computed_at", "source", "config_fingerprint", "blocks")


class CacheError(Exception):
    """Raised when a cache payload is malformed. Callers treat it as a miss."""


@dataclass(frozen=True)
class CachedSeries:
    kind: str
    computed_at: datetime
    source: str
    config_fingerprint: str
    blocks: list


def _aware(value, what):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CacheError("%s must be a timezone-aware datetime" % what)
    return value


def parse_series(data, expected_kind=None):
    """Turn an already-decoded dict into a CachedSeries, or raise CacheError."""
    if not isinstance(data, dict):
        raise CacheError("cache payload is not an object")
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise CacheError("missing fields: %s" % ", ".join(missing))
    kind = data["kind"]
    if kind not in KINDS:
        raise CacheError("unknown kind %r" % (kind,))
    if expected_kind is not None and kind != expected_kind:
        raise CacheError("kind %r read as %r" % (kind, expected_kind))
    raw_at = data["computed_at"]
    if isinstance(raw_at, str):
        try:
            computed_at = datetime.fromisoformat(raw_at)
        except ValueError:
            raise CacheError("computed_at is not ISO 8601: %r" % (raw_at,))
    else:
        computed_at = raw_at
    _aware(computed_at, "computed_at")
    if not isinstance(data["blocks"], list):
        raise CacheError("blocks must be a list")
    fp = data["config_fingerprint"]
    if not isinstance(fp, str) or not fp:
        raise CacheError("config_fingerprint must be a non-empty string")
    return CachedSeries(kind, computed_at, str(data["source"]), fp, data["blocks"])


def to_dict(series):
    """Serialisable dict for the adapter to write (datetime as ISO string)."""
    return {
        "kind": series.kind,
        "computed_at": series.computed_at.isoformat(),
        "source": series.source,
        "config_fingerprint": series.config_fingerprint,
        "blocks": series.blocks,
    }


def age_minutes(series, now):
    """Minutes since computed_at. Negative if computed_at is in the future."""
    _aware(now, "now")
    return (now - series.computed_at).total_seconds() / 60.0


def stale_minutes_for(kind, config):
    if kind == "solar":
        return config.solar_cache_stale_minutes
    if kind == "usage":
        return config.usage_cache_stale_minutes
    raise CacheError("unknown kind %r" % (kind,))


def is_stale(series, now, stale_minutes):
    """True when older than the bound: refresh it, but it may still be used."""
    return age_minutes(series, now) > stale_minutes


def is_from_previous_day(series, now, timezone):
    """True when computed_at is on a different LOCAL calendar date than now.

    Uses != on purpose: an earlier date is the normal rollover; a LATER date
    (clock skew) is also distrusted, which is the conservative choice.
    """
    _aware(now, "now")
    tz = ZoneInfo(timezone)
    return series.computed_at.astimezone(tz).date() != now.astimezone(tz).date()


def fingerprint_matches(series, config):
    return series.config_fingerprint == config.fingerprint()


def invalidation_reason(series, now, config, expected_kind=None):
    """Why the series must be discarded, or None when it is still valid.

    Invalid: wrong kind, different local day, or fingerprint mismatch.
    Staleness is deliberately not checked here.
    """
    if expected_kind is not None and series.kind != expected_kind:
        return "kind mismatch"
    if is_from_previous_day(series, now, config.timezone):
        return "computed on a previous day"
    if not fingerprint_matches(series, config):
        return "config fingerprint mismatch"
    return None


def format_minutes(total):
    """Compact duration: "14m" (<1h), "3h12m" (<24h), "2d3h" (>=24h). Never negative."""
    total = max(0, int(total))
    if total < 60:
        return "%dm" % total
    if total < 1440:
        return "%dh%dm" % (total // 60, total % 60)
    return "%dd%dh" % (total // 1440, (total % 1440) // 60)


def age_description(series, now):
    """Compact age of a cached series, e.g. "3h12m"."""
    return format_minutes(age_minutes(series, now))


def as_datetime(value):
    """A tz-aware datetime from a datetime or ISO string; naive means UTC.
    None for anything else (including an unparsable string)."""
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


def stamp_age_minutes(stamp, now):
    """Minutes from a source's freshness stamp to now, never negative.
    None when there is no usable stamp (the age check is then skipped)."""
    stamp = as_datetime(stamp)
    if stamp is None or not isinstance(now, datetime) or now.tzinfo is None:
        return None
    return max(0.0, (now - stamp).total_seconds() / 60.0)


def age_marker(series, now):
    """Decision-record `degraded`/age token, e.g. "cache_age_solar=3h12m"."""
    return "cache_age_%s=%s" % (series.kind, age_description(series, now))


def degraded_note(series, now):
    """Alias of age_marker: the text recorded when a stale series is used."""
    return age_marker(series, now)


def evaluate(data, kind, now, config):
    """Classify a decoded payload. Never raises.

    Precedence: invalidation (kind, different local day, fingerprint) is
    checked BEFORE staleness, so a series from an earlier local day is
    "invalid" even if young, and a usage series (bound 48h) can only be
    "stale" within a single local day when its bound is configured below
    the time left in that day.

    Returns (status, series, detail), status one of:
      "fresh"   - use as is; detail is the age marker (SC-014 wants the age
                  of every cached series a decision used)
      "stale"   - valid but past its bound: refresh; if impossible, use it
                  and record detail (the degraded note)
      "invalid" - discard and rebuild; detail is the reason (a miss, not a
                  failure). A missing/unreadable file is passed as data=None.
    """
    if data is None:
        return ("invalid", None, "cache missing or unreadable")
    try:
        series = parse_series(data, kind)
        reason = invalidation_reason(series, now, config, kind)
        if reason is not None:
            return ("invalid", None, reason)
        if is_stale(series, now, stale_minutes_for(kind, config)):
            return ("stale", series, degraded_note(series, now))
        return ("fresh", series, age_marker(series, now))
    except (CacheError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return ("invalid", None, "unusable cache: %s" % exc)
