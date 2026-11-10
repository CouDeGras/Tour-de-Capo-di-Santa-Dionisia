"""Weather dashboard data access and settings."""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from django.conf import settings

from .i18n import DEFAULT_LANG, LANGS
from .models import MetarReading

BASE_DIR = settings.BASE_DIR
# settings.DATA_DIR (not BASE_DIR) -- respects CAPO_DI_SANTA_DIONISIA_DATA_DIR
# the same way weather.py's own path constants do. Using BASE_DIR here
# was a real bug: inside the AppImage, BASE_DIR resolves to a path inside
# the read-only mount, which gets a fresh random location every single
# launch (/tmp/.mount_XXXXXX/...) -- so this always looked for
# weather_cache.json in a directory that was both wrong and different every
# time, permanently stuck showing an empty dashboard regardless of how much
# real data weather.py correctly wrote to the actual persistent
# CAPO_DI_SANTA_DIONISIA_DATA_DIR.
DATA_DIR = settings.DATA_DIR / "data"

WEATHER_CACHE = DATA_DIR / "weather_cache.json"
SITE_CONFIG = DATA_DIR / "site_config.json"
SITE_CONFIG_FIELDS = ("station", "lang", "sun_projection", "sun_view", "trinity_mode")
WEATHER_SCRIPT = BASE_DIR / "weather.py"
REFRESH_TIMEOUT_SECONDS = 90

CACHE_FILES = (
    WEATHER_CACHE,
    DATA_DIR / "station_geo_cache.json",
    DATA_DIR / "last_ok_metar.json",
    DATA_DIR / "last_ok_om.json",
    DATA_DIR / "last_ok_owm.json",
    DATA_DIR / "last_ok_yr.json",
    DATA_DIR / "weather.txt",
)

HISTORIC_DAYS = 3
HISTORIC_BUCKET_HOURS = 3

# Any 4-letter ICAO airport code is accepted -- weather.py resolves its
# lat/lon (from METAR) and tz (reverse-geocoded via Open-Meteo) dynamically
# and caches the result, so this process only needs to validate the shape of
# the code, not maintain a registry of known stations.
ICAO_RE = re.compile(r"^[A-Z]{4}$")
DEFAULT_STATION = "LIRA"

# The sun-path widget's two radius-vs-elevation mappings (see static/weather.js's
# drawSunPath) -- both give a true circle in the polar-latitude limit and a
# non-circular arc otherwise, they just trade off evenly-spaced elevation
# rings (linear) against a literal bird's-eye view of the sky (orthographic).
SUN_PROJECTIONS = ("linear", "orthographic")
DEFAULT_SUN_PROJECTION = "linear"

# Independent of the radius mapping above: which way the widget is meant to
# be read. 'down' (default) is the standard map convention -- looking down
# at the sky dome from outside it, N top/E right. 'up' mirrors E/W to match
# how planetarium/architectural sun charts are actually used -- held
# overhead, standing on the ground looking up. See static/weather.js's
# azUnit()/drawSunPath() for the mirroring and the current-sun marker glyph
# (dot vs cross) this also switches.
SUN_VIEWS = ("down", "up")
DEFAULT_SUN_VIEW = "down"

TRINITY_MODES = ("on", "off")
DEFAULT_TRINITY_MODE = "on"


def current_lang() -> str:
    """The site-wide UI language: whatever was last saved via the config
    panel, else DEFAULT_LANG. Always one of LANGS -- an unset or corrupted
    site_config.json falls back rather than crashing the page render."""
    if SITE_CONFIG.exists():
        try:
            lang = json.loads(SITE_CONFIG.read_text(encoding="utf-8")).get("lang")
            if lang in LANGS:
                return lang
        except Exception:
            pass
    return DEFAULT_LANG


def _historic_rows(tz_name: str, station: str) -> list:
    """Last HISTORIC_DAYS of real METAR readings from MetarReading,
    bucketed into the same shape/cadence as the forecast comparison rows
    (see weather.py's build_comparison_rows) so the frontend can just
    prepend them to comparison.rows and chart the two back to back -- the
    dashboard's column layout is index-based, not proportional to elapsed
    time, so exact alignment with the forecast's own first timestamp isn't
    needed (see colLayout's docstring in weather.js).

    METAR is one ground-truth source, not per-provider, so the same
    temperature fills owm/yr/om_temp_c alike (all three charts show the same
    grayed-out historic line). There's no cached historic precipitation
    total (only present-weather codes get logged, not an mm amount) -- rain
    fields are always None here, which the chart renderer already treats as
    "nothing to draw" for that column, i.e. padded blank rather than guessed.
    A bucket with no logged reading (a gap in the hourly log, or before this
    logging existed) is likewise left at None instead of interpolated.

    Scoped to `station` -- otherwise switching the configured station would
    mix another location's temperature readings into this location's chart.
    """
    tz = ZoneInfo(tz_name) if ZoneInfo else timezone.utc
    try:
        now = datetime.now(tz)
    except Exception:
        now = datetime.now(timezone.utc)

    cutoff = now - timedelta(days=HISTORIC_DAYS)
    readings = list(
        MetarReading.objects.filter(
            obs_time_epoch__gte=cutoff.timestamp(),
            temp_c__isnull=False,
            station=station,
        ).values_list("obs_time_epoch", "temp_c")
    )

    n_buckets = (HISTORIC_DAYS * 24) // HISTORIC_BUCKET_HOURS
    rows = []
    for i in range(n_buckets):
        start = now - timedelta(hours=HISTORIC_BUCKET_HOURS * (n_buckets - i))
        end = start + timedelta(hours=HISTORIC_BUCKET_HOURS)
        bucket_temps = [
            t for (epoch, t) in readings if start.timestamp() <= epoch < end.timestamp()
        ]
        temp = round(sum(bucket_temps) / len(bucket_temps), 1) if bucket_temps else None
        rows.append(
            {
                "local_time": start.strftime("%m-%d %H:%M"),
                "epoch": int(start.timestamp()),
                "owm_temp_c": temp,
                "yr_temp_c": temp,
                "om_temp_c": temp,
                "owm_rain_3h_mm": None,
                "yr_rain_3h_mm": None,
                "om_rain_3h_mm": None,
                "historic": True,
            }
        )
    return rows


def api_status() -> dict:
    site_cfg = api_config_get()
    sun_projection = site_cfg.get("sun_projection", DEFAULT_SUN_PROJECTION)
    sun_view = site_cfg.get("sun_view", DEFAULT_SUN_VIEW)
    trinity_mode = site_cfg.get("trinity_mode", DEFAULT_TRINITY_MODE)

    data = (
        json.loads(WEATHER_CACHE.read_text(encoding="utf-8"))
        if WEATHER_CACHE.exists()
        else {}
    )
    loc = data.setdefault("location", {})
    station = _effective_station()
    loc.setdefault("station", station)
    # location.station_name is only refreshed by the tri-hourly full run;
    # current.station_name comes from the hourly METAR-only refresh and is
    # usually fresher after switching to a new airport, so prefer it. Bare
    # code is the last resort, e.g. right after switching before either has
    # run once against the new station.
    current_station_name = (data.get("current") or {}).get("station_name")
    city = loc.get("station_name") or current_station_name or station
    # current.station_name is the raw METAR station name (aviationweather.gov's
    # "name" field, e.g. "Shanghai/Hongqiao Intl, SH, CN") -- unlike
    # location.station_name, which weather.py's resolve_station_geo
    # already trims to just the part before the slash (see that function's
    # docstring). Trimmed here too so the widget shows "Shanghai" regardless
    # of which of the two sources actually won above.
    loc["city"] = city.split("/", 1)[0].strip() if city else city

    comparison = data.setdefault("comparison", {})
    comparison["rows"] = _historic_rows(loc.get("tz") or "UTC", station) + list(
        comparison.get("rows") or []
    )

    data["sun_projection"] = sun_projection
    data["sun_view"] = sun_view
    data["trinity_mode"] = trinity_mode
    return data


def api_config_get() -> dict:
    try:
        data = json.loads(SITE_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    cfg = {key: data.get(key, "") for key in SITE_CONFIG_FIELDS}
    station = str(cfg["station"] or "").strip().upper()
    cfg["station"] = station if ICAO_RE.match(station) else DEFAULT_STATION
    for key, values, default in (
        ("lang", LANGS, DEFAULT_LANG),
        ("sun_projection", SUN_PROJECTIONS, DEFAULT_SUN_PROJECTION),
        ("sun_view", SUN_VIEWS, DEFAULT_SUN_VIEW),
        ("trinity_mode", TRINITY_MODES, DEFAULT_TRINITY_MODE),
    ):
        if cfg[key] not in values:
            cfg[key] = default
    return cfg


def api_config_save(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object.")

    cfg = {}
    station = str(payload.get("station") or "").strip().upper()
    if station and not ICAO_RE.match(station):
        raise ValueError(
            "'station' must be a 4-letter ICAO airport code (e.g. LIRA, KJFK, EGLL)."
        )
    cfg["station"] = station
    lang = str(payload.get("lang") or "").strip()
    if lang and lang not in LANGS:
        raise ValueError(f"'lang' must be one of {', '.join(LANGS)}.")
    cfg["lang"] = lang or DEFAULT_LANG

    sun_projection = str(payload.get("sun_projection") or "").strip()
    if sun_projection and sun_projection not in SUN_PROJECTIONS:
        raise ValueError(
            f"'sun_projection' must be one of {', '.join(SUN_PROJECTIONS)}."
        )
    cfg["sun_projection"] = sun_projection or DEFAULT_SUN_PROJECTION

    sun_view = str(payload.get("sun_view") or "").strip()
    if sun_view and sun_view not in SUN_VIEWS:
        raise ValueError(f"'sun_view' must be one of {', '.join(SUN_VIEWS)}.")
    cfg["sun_view"] = sun_view or DEFAULT_SUN_VIEW

    trinity_mode = str(payload.get("trinity_mode") or "").strip().lower()
    if trinity_mode and trinity_mode not in TRINITY_MODES:
        raise ValueError(f"'trinity_mode' must be one of {', '.join(TRINITY_MODES)}.")
    cfg["trinity_mode"] = trinity_mode or DEFAULT_TRINITY_MODE

    SITE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SITE_CONFIG)

    return cfg


def api_refresh() -> dict:
    """Fetch current weather from all providers."""
    try:
        proc = subprocess.run(
            [sys.executable, str(WEATHER_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=REFRESH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Refresh timed out after {REFRESH_TIMEOUT_SECONDS}s.")
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or proc.stdout or "").strip()[-500:]
            or "Weather refresh failed."
        )
    return api_status()


def api_clear_cache() -> dict:
    """Clear weather caches and observations; reset airport and language."""
    MetarReading.objects.all().delete()
    for f in CACHE_FILES:
        f.unlink(missing_ok=True)
    try:
        cfg = json.loads(SITE_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    cfg.update(station=DEFAULT_STATION, lang=DEFAULT_LANG)
    SITE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = SITE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SITE_CONFIG)
    return {"cleared": True}


def _effective_station() -> str:
    """Same override order weather.py uses: an explicit site_config.json
    value first, else whatever station the last successful forecast run
    resolved to, else DEFAULT_STATION."""
    if SITE_CONFIG.exists():
        try:
            station = (
                str(
                    json.loads(SITE_CONFIG.read_text(encoding="utf-8")).get("station")
                    or ""
                )
                .strip()
                .upper()
            )
            if ICAO_RE.match(station):
                return station
        except Exception:
            pass
    if WEATHER_CACHE.exists():
        try:
            station = (
                str(
                    (
                        json.loads(WEATHER_CACHE.read_text(encoding="utf-8")).get(
                            "location"
                        )
                        or {}
                    ).get("station")
                    or ""
                )
                .strip()
                .upper()
            )
            if ICAO_RE.match(station):
                return station
        except Exception:
            pass
    return DEFAULT_STATION
