"""Site configuration: parse and validate a plain dict. Pure - no I/O."""
import hashlib
import re
from dataclasses import dataclass, fields

_MODES = ("auto", "running", "accumulating")
_GROUPINGS = ("same_weekday", "day_type")

# (section, file key, attribute)
_MAP = [
    ("prices", "consumption_multiplier", "consumption_multiplier"),
    ("prices", "consumption_offset", "consumption_offset"),
    ("prices", "injection_multiplier", "injection_multiplier"),
    ("prices", "injection_offset", "injection_offset"),
    ("battery", "capacity_kwh", "capacity_kwh"),
    ("battery", "reserve_percent", "reserve_percent"),
    ("battery", "max_charge_kw", "max_charge_kw"),
    ("battery", "max_discharge_kw", "max_discharge_kw"),
    ("battery", "round_trip_efficiency", "round_trip_efficiency"),
    ("solar", "latitude", "latitude"),
    ("solar", "longitude", "longitude"),
    ("solar", "kwp", "array_kwp"),
    ("solar", "declination", "array_declination"),
    ("solar", "azimuth", "array_azimuth"),
    ("timing", "block_minutes", "block_minutes"),
    ("timing", "evaluation_interval_minutes", "evaluation_interval_minutes"),
    ("timing", "forecast_refresh_minutes", "forecast_refresh_minutes"),
    ("timing", "forecast_retry_minutes", "forecast_retry_minutes"),
    ("timing", "solar_cache_stale_minutes", "solar_cache_stale_minutes"),
    ("timing", "usage_cache_stale_minutes", "usage_cache_stale_minutes"),
    ("usage", "history_weeks", "usage_history_weeks"),
    ("usage", "grouping", "usage_grouping"),
    ("alerts", "address", "alert_address"),
    ("alerts", "notify_service", "notify_service"),
    ("alerts", "realert_minutes", "realert_minutes"),
    (None, "timezone", "timezone"),
    ("capacity_tariff", "enabled", "capacity_enabled"),
    ("capacity_tariff", "billing_floor_kw", "billing_floor_kw"),
    ("capacity_tariff", "rate_eur_per_kw_year", "capacity_rate_eur_per_kw_year"),
    ("capacity_tariff", "guard_interval_seconds", "guard_interval_seconds"),
    ("capacity_tariff", "peak_averaging_months", "peak_averaging_months"),
    ("capacity_tariff", "quarter_hour_average_mode", "quarter_hour_average_mode"),
    ("capacity_tariff", "offtake_sensor", "offtake_sensor"),
    ("capacity_tariff", "stay_under_percent", "stay_under_percent"),
]

_NON_NUMERIC = (
    "alert_address", "notify_service", "timezone", "offtake_sensor",
    "quarter_hour_average_mode", "capacity_enabled", "usage_grouping",
)

_FINGERPRINT_FIELDS = (
    "latitude", "longitude", "array_kwp",
    "array_declination", "array_azimuth", "block_minutes",
    "usage_history_weeks", "usage_grouping",
)


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. A startup error."""


@dataclass(frozen=True)
class SiteConfig:
    capacity_kwh: float
    alert_address: str
    consumption_multiplier: float = 1.07
    consumption_offset: float = 0.007
    injection_multiplier: float = 0.94
    injection_offset: float = -0.011
    reserve_percent: float = 10.0
    max_charge_kw: float = 5.0
    max_discharge_kw: float = 5.0
    round_trip_efficiency: float = 0.90
    latitude: float = 51.12
    longitude: float = 3.85
    array_kwp: float = 8.1
    array_declination: int = 50
    array_azimuth: int = -10
    block_minutes: int = 15
    evaluation_interval_minutes: int = 5
    forecast_refresh_minutes: int = 60
    forecast_retry_minutes: int = 10
    solar_cache_stale_minutes: int = 120
    usage_cache_stale_minutes: int = 2880
    realert_minutes: int = 60
    notify_service: str = "gmail_alert"
    timezone: str = "Europe/Brussels"
    capacity_enabled: bool = True
    billing_floor_kw: float = 2.5
    capacity_rate_eur_per_kw_year: float = 40.0
    guard_interval_seconds: int = 30
    peak_averaging_months: int = 13
    quarter_hour_average_mode: str = "auto"
    offtake_sensor: str = "sensor.slimmelezer_power_consumed"
    stay_under_percent: float = 80.0
    usage_history_weeks: int = 4
    usage_grouping: str = "same_weekday"

    def fingerprint(self):
        """Stable 8-hex digest of the fields that change what a block means."""
        canon = "|".join(
            "%s=%r" % (n, getattr(self, n)) for n in _FINGERPRINT_FIELDS
        )
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _errors(cfg):
    errs = []

    def bad(field, value, rule):
        errs.append("%s=%r: %s" % (field, value, rule))

    for f in fields(SiteConfig):
        if f.name in _NON_NUMERIC:
            continue
        v = getattr(cfg, f.name)
        if not _num(v):
            bad(f.name, v, "must be a number")
    if errs:
        return errs
    if cfg.capacity_kwh <= 0:
        bad("capacity_kwh", cfg.capacity_kwh, "must be > 0")
    if not 0 <= cfg.reserve_percent < 100:
        bad("reserve_percent", cfg.reserve_percent, "must be >= 0 and < 100")
    if not 0 < cfg.round_trip_efficiency <= 1:
        bad("round_trip_efficiency", cfg.round_trip_efficiency,
            "must be > 0 and <= 1")
    if cfg.block_minutes <= 0 or 60 % cfg.block_minutes != 0:
        bad("block_minutes", cfg.block_minutes, "must divide 60")
    if not -180 <= cfg.array_azimuth <= 180:
        bad("azimuth", cfg.array_azimuth, "must be within -180..180")
    if not 0 <= cfg.array_declination <= 90:
        bad("declination", cfg.array_declination, "must be within 0..90")
    if not isinstance(cfg.alert_address, str) or not cfg.alert_address.strip():
        bad("alerts.address", cfg.alert_address, "must be non-empty")
    if (not isinstance(cfg.notify_service, str)
            or not re.fullmatch(r"[a-z0-9_]+", cfg.notify_service)):
        bad("alerts.notify_service", cfg.notify_service,
            "must be a service slug: lowercase letters, digits, underscore")
    if cfg.peak_averaging_months < 1:
        bad("peak_averaging_months", cfg.peak_averaging_months, "must be >= 1")
    if cfg.max_charge_kw <= 0:
        bad("max_charge_kw", cfg.max_charge_kw, "must be > 0")
    if cfg.max_discharge_kw <= 0:
        bad("max_discharge_kw", cfg.max_discharge_kw, "must be > 0")
    if not 0 < cfg.stay_under_percent <= 100:
        bad("stay_under_percent", cfg.stay_under_percent,
            "must be > 0 and <= 100")
    if cfg.quarter_hour_average_mode not in _MODES:
        bad("quarter_hour_average_mode", cfg.quarter_hour_average_mode,
            "must be one of %s" % ", ".join(_MODES))
    if isinstance(cfg.usage_history_weeks, float) or cfg.usage_history_weeks < 1:
        bad("usage_history_weeks", cfg.usage_history_weeks,
            "must be an integer >= 1")
    if cfg.usage_grouping not in _GROUPINGS:
        bad("usage_grouping", cfg.usage_grouping,
            "must be one of %s" % ", ".join(_GROUPINGS))
    return errs


def from_dict(raw):
    """Build a validated SiteConfig from the parsed YAML mapping."""
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a mapping, got %r" % (raw,))
    kwargs = {}
    errs = []
    for section, key, attr in _MAP:
        if section is None:
            container = raw
        else:
            container = raw.get(section)
            if container is None:
                container = {}
            elif not isinstance(container, dict):
                msg = "%s=%r: must be a mapping" % (section, container)
                if msg not in errs:
                    errs.append(msg)
                continue
        if key in container and container[key] is not None:
            kwargs[attr] = container[key]
    for req, label in (("capacity_kwh", "battery.capacity_kwh"),
                       ("alert_address", "alerts.address")):
        if req not in kwargs:
            errs.append("%s: required, no default" % label)
            kwargs[req] = None
    if errs:
        raise ConfigError("Invalid configuration: " + "; ".join(errs))
    cfg = SiteConfig(**kwargs)
    errs = _errors(cfg)
    if errs:
        raise ConfigError("Invalid configuration: " + "; ".join(errs))
    return cfg
