"""Dashboard data access and API payload assembly.

Ported as-is from the previous stdlib main.py: most dashboard state (weather
cache, pump acks, site config) lives in flat files under data/, written by
weather_mqtt.py's scheduled/ack-listener processes and by api_config_save()
below. Irrigation decisions and METAR history live in the ORM instead
(dashboard/models.py's IrrigationDecision/MetarReading) -- weather_mqtt.py
writes them directly via the same models, sharing one schema definition
instead of this module hand-parsing CSV rows weather_mqtt.py hand-wrote.
"""
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
from .models import IrrigationDecision, MetarReading

BASE_DIR = settings.BASE_DIR
# settings.DATA_DIR (not BASE_DIR) -- respects CAPO_DI_SANTA_DIONISIA_DATA_DIR
# the same way weather_mqtt.py's own path constants do. Using BASE_DIR here
# was a real bug: inside the AppImage, BASE_DIR resolves to a path inside
# the read-only mount, which gets a fresh random location every single
# launch (/tmp/.mount_XXXXXX/...) -- so this always looked for
# weather_cache.json in a directory that was both wrong and different every
# time, permanently stuck showing an empty dashboard regardless of how much
# real data weather_mqtt.py correctly wrote to the actual persistent
# CAPO_DI_SANTA_DIONISIA_DATA_DIR.
DATA_DIR = settings.DATA_DIR / "data"

WEATHER_CACHE = DATA_DIR / "weather_cache.json"
NEXT_WATERING = DATA_DIR / "next_watering.json"
PUMP_ACKS = DATA_DIR / "pump_acks.json"
SITE_CONFIG = DATA_DIR / "site_config.json"
SITE_CONFIG_FIELDS = (
    "station", "broker", "root_topic", "lang", "sun_projection", "sun_view",
    "mqtt_pub_password", "pump_flow_rate_lpm", "pump_target_volume_l", "citrus_mode",
    "trinity_mode",
)
WEATHER_MQTT_SCRIPT = BASE_DIR / "weather_mqtt.py"
REFRESH_TIMEOUT_SECONDS = 90

# Every file under data/ EXCEPT site_config.json -- that one holds explicit
# user settings (station/broker/lang/etc, the config panel's own fields),
# not derived/fetched state, so "clear cache" must leave it alone. Everything
# else here is either a weather_mqtt.py-written cache that regenerates on
# the next scheduled/manual fetch (weather_cache.json, next_watering.json,
# station_geo_cache.json, last_ok_*.json, weather.txt) or accumulated device
# ack history (pump_acks.json) -- all safe to drop.
CACHE_FILES = (
    WEATHER_CACHE, NEXT_WATERING, PUMP_ACKS,
    DATA_DIR / "station_geo_cache.json",
    DATA_DIR / "last_ok_metar.json",
    DATA_DIR / "last_ok_om.json",
    DATA_DIR / "last_ok_owm.json",
    DATA_DIR / "last_ok_yr.json",
    DATA_DIR / "weather.txt",
)

HISTORIC_DAYS = 3
HISTORIC_BUCKET_HOURS = 3

# Any 4-letter ICAO airport code is accepted -- weather_mqtt.py resolves its
# lat/lon (from METAR) and tz (reverse-geocoded via Open-Meteo) dynamically
# and caches the result, so this process only needs to validate the shape of
# the code, not maintain a registry of known stations.
ICAO_RE = re.compile(r"^[A-Z]{4}$")
DEFAULT_STATION = "ZSNJ"

# Mirrors weather_mqtt.py's own hardcoded MQTT_BROKER_HOST/PORT and topic
# namespace (same duplication pattern as DEFAULT_STATION above -- this
# process and weather_mqtt.py are separate scripts and don't import each
# other). Used so the config popup shows what broker/topic is actually in
# effect instead of a blank field when site_config.json doesn't set one.
DEFAULT_BROKER = "broker.emqx.io:1883"
DEFAULT_ROOT_TOPIC = "tour_genoise/capo_di_santa_dionisia"

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

# Mirrors weather_mqtt.py's own DEFAULT_PUMP_FLOW_RATE_LPM/
# DEFAULT_PUMP_TARGET_VOLUME_L (same duplication pattern as DEFAULT_STATION
# above) -- together these ARE the baseline ("100%") pump duration now
# (target_volume / flow_rate), not a fixed time constant. 24L / 12L/min =
# 120s, the exact duration the old hardcoded BASELINE_PUMP_SECONDS_NORMAL
# used, so these defaults change nothing for an install that never touches
# the fields.
DEFAULT_PUMP_FLOW_RATE_LPM = 12.0
DEFAULT_PUMP_TARGET_VOLUME_L = 24.0

# Master kill switch for the whole irrigation-decision/pump-command pipeline
# -- mirrors weather_mqtt.py's own CITRUS_MODES/DEFAULT_CITRUS_MODE/
# CITRUS_MODE (see that module's own comment for the full rationale; this
# is deliberately not named/labeled anything like "irrigation enabled" in
# the dashboard UI, by explicit design choice -- see the settings panel's
# "citrus mode" toggle). "off" (the default) also hides the Irrigation nav
# link and the Station settings fields (see dashboard/views.py) -- nothing
# to look at there when nothing's being computed or published.
CITRUS_MODES = ("on", "off")
DEFAULT_CITRUS_MODE = "off"

# Whether the weather/irrigation rain+temp charts show all three sources
# (OWM/Yr.no/Open-Meteo) side by side or a single mean-of-sources chart --
# see weather.js's/irrigation.js's renderRainCharts/renderMeanChart. Purely
# a display preference (unlike citrus_mode above), so it needs no gating in
# views.py: both pages already carry the DOM for both chart layouts and just
# toggle which one's visible client-side. Defaults "on" (three sources) to
# match the weather page's original always-on behavior.
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
    (see weather_mqtt.py's build_comparison_rows) so the frontend can just
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
        MetarReading.objects
        .filter(obs_time_epoch__gte=cutoff.timestamp(), temp_c__isnull=False, station=station)
        .values_list("obs_time_epoch", "temp_c")
    )

    n_buckets = (HISTORIC_DAYS * 24) // HISTORIC_BUCKET_HOURS
    rows = []
    for i in range(n_buckets):
        start = now - timedelta(hours=HISTORIC_BUCKET_HOURS * (n_buckets - i))
        end = start + timedelta(hours=HISTORIC_BUCKET_HOURS)
        bucket_temps = [t for (epoch, t) in readings if start.timestamp() <= epoch < end.timestamp()]
        temp = round(sum(bucket_temps) / len(bucket_temps), 1) if bucket_temps else None
        rows.append({
            "local_time": start.strftime("%m-%d %H:%M"),
            "epoch": int(start.timestamp()),
            "owm_temp_c": temp, "yr_temp_c": temp, "om_temp_c": temp,
            "owm_rain_3h_mm": None, "yr_rain_3h_mm": None, "om_rain_3h_mm": None,
            "historic": True,
        })
    return rows


def api_status() -> dict:
    # No demo/placeholder fallback -- before the first successful
    # weather_mqtt.py fetch (a genuine first run, or right after the
    # settings panel's "Clear cache" wipes weather_cache.json), `data` just
    # stays {} and every field below defaults the same honest way it would
    # for a real cache missing that particular key. The frontend already
    # renders every one of these as a blank/dash/empty-state (see
    # weather.js's renderMetrics/renderLocation, irrigation.js's
    # I18N.no_history/no_acks) since a partially-stale real cache can leave
    # individual fields missing too -- there's no field here that's only
    # ever absent in the theoretical "brand new install" case.
    site_cfg = api_config_get()
    sun_projection = site_cfg.get("sun_projection", DEFAULT_SUN_PROJECTION)
    sun_view = site_cfg.get("sun_view", DEFAULT_SUN_VIEW)
    trinity_mode = site_cfg.get("trinity_mode", DEFAULT_TRINITY_MODE)

    data = json.loads(WEATHER_CACHE.read_text(encoding="utf-8")) if WEATHER_CACHE.exists() else {}
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
    # location.station_name, which weather_mqtt.py's resolve_station_geo
    # already trims to just the part before the slash (see that function's
    # docstring). Trimmed here too so the widget shows "Shanghai" regardless
    # of which of the two sources actually won above.
    loc["city"] = city.split("/", 1)[0].strip() if city else city

    # Supplement with next_watering.json for freshest event list
    if NEXT_WATERING.exists():
        nw = json.loads(NEXT_WATERING.read_text(encoding="utf-8"))
        sched = data.setdefault("irrigation", {}).setdefault("schedule", {})
        if isinstance(sched, dict):
            sched.setdefault("json_payload", nw)

    comparison = data.setdefault("comparison", {})
    comparison["rows"] = _historic_rows(loc.get("tz") or "UTC", station) + list(comparison.get("rows") or [])

    data["sun_projection"] = sun_projection
    data["sun_view"] = sun_view
    data["trinity_mode"] = trinity_mode
    return data


def api_history(n: int = 14) -> dict:
    # Scoped to the currently-configured station -- otherwise switching
    # station would show another location's irrigation decisions as if they
    # were continuous history for this one. Rows written before per-station
    # tracking existed (station="") are excluded here too, not guessed at.
    qs = IrrigationDecision.objects.filter(station=_effective_station())
    rows = list(qs.order_by("local_date").values())
    return {"rows": rows[-n:]}


def api_acks() -> dict:
    if not PUMP_ACKS.exists():
        return {"devices": {}}
    data = json.loads(PUMP_ACKS.read_text(encoding="utf-8"))
    return {"devices": data.get("devices") or {}}


def api_config_get() -> dict:
    # mqtt_pub_password is returned in plaintext like every other field here
    # -- this panel presupposes whoever can reach it already has PSK-level
    # ownership of the deployment (same trust boundary as reading
    # site_config.json off disk directly), so there's no "hide the secret
    # from the browser" concern to design around the way a real per-user
    # credential would need.
    if not SITE_CONFIG.exists():
        cfg = {k: "" for k in SITE_CONFIG_FIELDS}
        cfg["lang"] = DEFAULT_LANG
        cfg["station"] = DEFAULT_STATION
        cfg["broker"] = DEFAULT_BROKER
        cfg["root_topic"] = DEFAULT_ROOT_TOPIC
        cfg["sun_projection"] = DEFAULT_SUN_PROJECTION
        cfg["sun_view"] = DEFAULT_SUN_VIEW
        cfg["pump_flow_rate_lpm"] = DEFAULT_PUMP_FLOW_RATE_LPM
        cfg["pump_target_volume_l"] = DEFAULT_PUMP_TARGET_VOLUME_L
        cfg["citrus_mode"] = DEFAULT_CITRUS_MODE
        cfg["trinity_mode"] = DEFAULT_TRINITY_MODE
        return cfg
    try:
        data = json.loads(SITE_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    cfg = {k: data.get(k, "") for k in SITE_CONFIG_FIELDS}
    cfg["lang"] = cfg["lang"] if cfg["lang"] in LANGS else DEFAULT_LANG
    station = str(cfg["station"] or "").strip().upper()
    cfg["station"] = station if ICAO_RE.match(station) else DEFAULT_STATION
    cfg["broker"] = str(cfg["broker"] or "").strip() or DEFAULT_BROKER
    cfg["root_topic"] = str(cfg["root_topic"] or "").strip() or DEFAULT_ROOT_TOPIC
    cfg["sun_projection"] = cfg["sun_projection"] if cfg["sun_projection"] in SUN_PROJECTIONS else DEFAULT_SUN_PROJECTION
    cfg["sun_view"] = cfg["sun_view"] if cfg["sun_view"] in SUN_VIEWS else DEFAULT_SUN_VIEW
    cfg["mqtt_pub_password"] = str(cfg["mqtt_pub_password"] or "")
    # Same "optional, not required" contract as every other field -- blank/
    # unparseable/non-positive falls back to the default rather than
    # surfacing a broken value the panel would just have to reject anyway.
    try:
        flow_rate = float(cfg["pump_flow_rate_lpm"])
        cfg["pump_flow_rate_lpm"] = flow_rate if flow_rate > 0 else DEFAULT_PUMP_FLOW_RATE_LPM
    except (TypeError, ValueError):
        cfg["pump_flow_rate_lpm"] = DEFAULT_PUMP_FLOW_RATE_LPM
    try:
        target_volume = float(cfg["pump_target_volume_l"])
        cfg["pump_target_volume_l"] = target_volume if target_volume > 0 else DEFAULT_PUMP_TARGET_VOLUME_L
    except (TypeError, ValueError):
        cfg["pump_target_volume_l"] = DEFAULT_PUMP_TARGET_VOLUME_L
    citrus_mode = str(cfg["citrus_mode"] or "").strip().lower()
    cfg["citrus_mode"] = citrus_mode if citrus_mode in CITRUS_MODES else DEFAULT_CITRUS_MODE
    trinity_mode = str(cfg["trinity_mode"] or "").strip().lower()
    cfg["trinity_mode"] = trinity_mode if trinity_mode in TRINITY_MODES else DEFAULT_TRINITY_MODE
    return cfg


def api_config_save(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object.")

    # Start from whatever's already on disk (not {}) and only overwrite the
    # fields this form actually manages (SITE_CONFIG_FIELDS). Anything else
    # set out-of-band must still survive a config-panel save instead of
    # being silently dropped the next time someone edits their station or
    # broker.
    cfg = {}
    if SITE_CONFIG.exists():
        try:
            existing = json.loads(SITE_CONFIG.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                cfg = {k: v for k, v in existing.items() if k not in SITE_CONFIG_FIELDS}
        except Exception:
            pass

    station = str(payload.get("station") or "").strip().upper()
    if station and not ICAO_RE.match(station):
        raise ValueError("'station' must be a 4-letter ICAO airport code (e.g. ZSNJ, KJFK, EGLL).")
    cfg["station"] = station
    for key in ("broker", "root_topic", "mqtt_pub_password"):
        v = payload.get(key, "")
        cfg[key] = "" if v is None else str(v).strip()
    lang = str(payload.get("lang") or "").strip()
    if lang and lang not in LANGS:
        raise ValueError(f"'lang' must be one of {', '.join(LANGS)}.")
    cfg["lang"] = lang or DEFAULT_LANG

    sun_projection = str(payload.get("sun_projection") or "").strip()
    if sun_projection and sun_projection not in SUN_PROJECTIONS:
        raise ValueError(f"'sun_projection' must be one of {', '.join(SUN_PROJECTIONS)}.")
    cfg["sun_projection"] = sun_projection or DEFAULT_SUN_PROJECTION

    sun_view = str(payload.get("sun_view") or "").strip()
    if sun_view and sun_view not in SUN_VIEWS:
        raise ValueError(f"'sun_view' must be one of {', '.join(SUN_VIEWS)}.")
    cfg["sun_view"] = sun_view or DEFAULT_SUN_VIEW

    # Together these ARE the baseline ("100%") pump duration weather_mqtt.py
    # computes every other percentage from (see its baseline_pump_seconds_normal()) --
    # unlike every other field here, a bad value doesn't just mis-set a
    # label, it feeds a real hardware command, so blank/non-positive/
    # unparseable is rejected outright rather than silently substituting a
    # default the user might not notice took effect.
    for key, default in (
        ("pump_flow_rate_lpm", DEFAULT_PUMP_FLOW_RATE_LPM),
        ("pump_target_volume_l", DEFAULT_PUMP_TARGET_VOLUME_L),
    ):
        raw = payload.get(key)
        if raw in (None, ""):
            cfg[key] = default
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be a positive number.")
        if value <= 0:
            raise ValueError(f"'{key}' must be a positive number.")
        cfg[key] = value

    citrus_mode = str(payload.get("citrus_mode") or "").strip().lower()
    if citrus_mode and citrus_mode not in CITRUS_MODES:
        raise ValueError(f"'citrus_mode' must be one of {', '.join(CITRUS_MODES)}.")
    cfg["citrus_mode"] = citrus_mode or DEFAULT_CITRUS_MODE

    trinity_mode = str(payload.get("trinity_mode") or "").strip().lower()
    if trinity_mode and trinity_mode not in TRINITY_MODES:
        raise ValueError(f"'trinity_mode' must be one of {', '.join(TRINITY_MODES)}.")
    cfg["trinity_mode"] = trinity_mode or DEFAULT_TRINITY_MODE

    tmp = SITE_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SITE_CONFIG)

    return cfg


def api_refresh() -> dict:
    """Manual dashboard refresh button: re-fetch Yr.no/OWM/Open-Meteo/METAR
    right now via `weather_mqtt.py --fetch-only`, which refreshes the
    forecast/current/ensemble sections of weather_cache.json but -- unlike a
    normal scheduled run -- never recomputes the irrigation decision, never
    touches the IrrigationDecision table/next_watering.json, and never publishes to
    the MQTT broker (so it can't accidentally command a pump or consume a due
    watering event outside the normal 3-hourly cadence). See that flag's
    --help and run_fetch_only()'s docstring for the full reasoning."""
    try:
        proc = subprocess.run(
            [sys.executable, str(WEATHER_MQTT_SCRIPT), "--fetch-only"],
            capture_output=True, text=True, timeout=REFRESH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Refresh timed out after {REFRESH_TIMEOUT_SECONDS}s.")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[-500:] or "weather_mqtt.py --fetch-only failed.")
    return api_status()


def api_clear_cache() -> dict:
    """Settings panel's "Clear cache" action: wipes every fetched/derived
    weather+irrigation record -- the ORM tables (IrrigationDecision,
    MetarReading) and each file in CACHE_FILES -- so the dashboard shows its
    honest empty state (see api_status()'s docstring) until the next fetch
    runs, same as a genuine first run. site_config.json is deliberately not
    touched (see CACHE_FILES).

    Row deletion (not dropping/truncating the table) so this works
    identically whether it's reached from the systemd-deployed webUI or the
    Electron shell, neither of which restarts Django/re-runs migrations
    afterward. Also doubles as the easy way to reset a dev or Electron build
    back to a clean state without hand-deleting files under the app's data
    dir.
    """
    IrrigationDecision.objects.all().delete()
    MetarReading.objects.all().delete()
    for f in CACHE_FILES:
        f.unlink(missing_ok=True)
    return {"cleared": True}


def _effective_station() -> str:
    """Same override order weather_mqtt.py uses: an explicit site_config.json
    value first, else whatever station the last successful forecast run
    resolved to, else DEFAULT_STATION."""
    if SITE_CONFIG.exists():
        try:
            station = str(json.loads(SITE_CONFIG.read_text(encoding="utf-8")).get("station") or "").strip().upper()
            if ICAO_RE.match(station):
                return station
        except Exception:
            pass
    if WEATHER_CACHE.exists():
        try:
            station = str((json.loads(WEATHER_CACHE.read_text(encoding="utf-8")).get("location") or {}).get("station") or "").strip().upper()
            if ICAO_RE.match(station):
                return station
        except Exception:
            pass
    return DEFAULT_STATION
