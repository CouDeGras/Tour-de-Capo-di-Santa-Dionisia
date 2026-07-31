#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from cryptography.hazmat.primitives import padding as _aes_padding
    from cryptography.hazmat.primitives.ciphers import Cipher as _AesCipher, algorithms as _aes_algorithms, modes as _aes_modes
except Exception:
    _aes_padding = None
    _AesCipher = None
    _aes_algorithms = None
    _aes_modes = None

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import subprocess
import time
import uuid

try:
    import paho.mqtt.publish as _mqtt_publish  # pip install paho-mqtt
except Exception:
    _mqtt_publish = None

try:
    import paho.mqtt.client as _mqtt_client
except Exception:
    _mqtt_client = None

# This script runs as its own OS process -- historically timer-triggered,
# now a persistent service (see run_service()/--service) -- separate from
# the Django dashboard's WSGI process. It bootstraps Django purely to share
# one schema definition (dashboard/models.py) for irrigation-decision/METAR
# history, instead of hand-writing CSV rows the dashboard then hand-parses
# back (the old data/irrigation_history.csv, data/metar_history.csv).
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()
from django.db.models import Max

from dashboard.models import IrrigationDecision, MetarReading

# ====== MQTT SEND CONFIG ======
# Published after every forecast run (every RUN_INTERVAL_HOURS, driven by
# run_service()'s loop). The payload always carries the NEXT TWO
# irrigation events (epoch + percent + pump seconds), so the pumps can be
# stateless between runs.
#
# Topic namespace: "tour_genoise/capo_di_santa_dionisia/*" -- scoped to this
# property so it can't collide with other (deprecated) sensors that used to
# share the same broker under the old "fuenteazahar/..." prefix, and again
# under "notre_dame/sainte_croix/saignes_en_padaine/..." before this rename.
MQTT_SEND_ENABLED = True
MQTT_BROKER_HOST = "broker.emqx.io"                    # placeholder broker
MQTT_BROKER_PORT = 1883
MQTT_TOPIC_PUB = "tour_genoise/capo_di_santa_dionisia/pub"
MQTT_TOPIC_ACK = "tour_genoise/capo_di_santa_dionisia/ack"
MQTT_QOS = 1
MQTT_RETAIN = True          # retained -> a pump that reconnects gets the latest schedule
MQTT_CLIENT_ID_PREFIX = "capo-di-santa-dionisia-sched"
MQTT_ACK_CLIENT_ID_PREFIX = "capo-di-santa-dionisia-ackwatch"
MQTT_KEEPALIVE_SECONDS = 30
MQTT_USERNAME = None        # set if your broker needs auth
MQTT_PASSWORD = None

# AES-128-CBC key for both the pub-topic AND ack-topic payloads (same key,
# same scheme both directions), derived from a human-typed passphrase rather
# than requiring someone to paste a random base64 key: key = MD5(password)
# (see mqtt_pub_password_to_key() below) -- MD5 is a convenient fit only
# because its 16-byte digest happens to be exactly an AES-128 key's length,
# NOT for any cryptographic property. A plain, unsalted, unstretched hash of
# a human-memorable password is guessable by brute force far faster than a
# real KDF (PBKDF2/scrypt/argon2) would allow -- acceptable here (a hobby
# irrigation controller, not a system with a real threat model), not a
# pattern to copy into anything higher-stakes. site_config.json's
# "mqtt_pub_password" field, set via refresh_site_config_overrides() below.
# None means "not configured yet": publish_mqtt_schedule() falls back to
# plaintext JSON and warns loudly (and _on_ack_message() expects plaintext
# JSON acks to match), so a fresh checkout / pre-firmware-rollout deployment
# keeps working.
MQTT_PUB_AES_KEY: Optional[bytes] = None


def mqtt_pub_password_to_key(password: str) -> bytes:
    return hashlib.md5(password.encode("utf-8")).digest()

# ====== APP DATA DIRECTORY ======
# When CAPO_DI_SANTA_DIONISIA_DATA_DIR is set (the Electron/AppImage-bundled
# deployment sets it to a real writable per-user directory, since an
# AppImage mounts read-only from a fresh temp path every launch), all
# runtime state below -- JSON caches, site config, weather.txt -- lives
# under <that>/data/ instead of next to this script. Unset (the existing
# bare-metal/systemd deployment) keeps today's exact behavior: everything
# under this project's own data/ directory. Same env var core/settings.py
# reads for db.sqlite3's location, so both processes share one writable
# root.
_APP_DATA_ROOT = Path(os.environ.get("CAPO_DI_SANTA_DIONISIA_DATA_DIR") or Path(__file__).resolve().parent)
_DATA_DIR = _APP_DATA_ROOT / "data"

# Pump nodes publish an ACK (device id/MAC + what they did) after every wake.
# We keep a persistent background subscriber (not tied to the 3h forecast
# cycle) so the dashboard can show nodes checking in in near-real time.
PUMP_ACKS_JSON = str(_DATA_DIR / "pump_acks.json")
PUMP_ACKS_RECENT_MAX = 50

# ====== OPENCLAW SEND CONFIG ======
OPENCLAW_SEND_ENABLED = True
OPENCLAW_CHANNEL = "discord"
OPENCLAW_TARGET = "user:1294746659376337029"   # or "channel:1234567890"
OPENCLAW_MESSAGE = "Garden weather + irrigation cache"
OPENCLAW_SILENT = False

# ====== DEFAULT LOCATION / MODE ======
# The dashboard accepts any 4-letter ICAO airport code (set via the config
# panel -> data/site_config.json). Its lat/lon/tz are resolved dynamically --
# METAR gives lat/lon (see resolve_station_geo below), Open-Meteo's
# timezone=auto reverse-geocodes the IANA tz from that point -- and cached in
# STATION_GEO_CACHE_JSON so the lookup only happens once per airport rather
# than on every run. DEFAULT_STATION/_LAT/_LON/_TZ are just the bootstrap
# fallback used before any site config exists or if a lookup ever fails.
ICAO_RE = re.compile(r"^[A-Z]{4}$")
USE_IP_LOCATION = False
DEFAULT_STATION = "ZSNJ"
DEFAULT_LAT = 31.7420
DEFAULT_LON = 118.8622
DEFAULT_TZ = "Asia/Shanghai"
STATION_GEO_CACHE_JSON = str(_DATA_DIR / "station_geo_cache.json")

# ====== SITE CONFIG OVERRIDE ======
# User-editable via the dashboard's config panel. data/site_config.json is
# written by main.py's /api/config endpoint; if it's missing, unreadable, or
# a field is blank/absent, that field's hardcoded default above/below is used
# untouched -- this file is optional, not required.
SITE_CONFIG_JSON = str(_DATA_DIR / "site_config.json")


def load_site_config(path: str) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def refresh_site_config_overrides() -> None:
    """Re-read data/site_config.json and refresh the module-level
    station/broker/topic overrides it can set (DEFAULT_STATION,
    MQTT_BROKER_HOST/PORT, MQTT_TOPIC_PUB/ACK).

    Historically this only needed to run once at import time, because every
    cycle was a fresh `python3 weather_mqtt.py` process (timer-triggered) --
    re-importing the module naturally picked up config changes every 3
    hours. Now that --service is one persistent process (loaded once,
    looping internally -- see run_service()), nothing re-imports this
    module anymore, so run_service() calls this explicitly before every
    cycle instead. Still called once at module load too, for the
    --current-only/--fetch-only/no-flag single-shot invocations, matching
    the original one-read-per-process behavior exactly."""
    global DEFAULT_STATION, MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_TOPIC_PUB, MQTT_TOPIC_ACK, MQTT_PUB_AES_KEY
    site_cfg = load_site_config(SITE_CONFIG_JSON)

    station_raw = str(site_cfg.get("station") or "").strip().upper()
    if ICAO_RE.match(station_raw):
        DEFAULT_STATION = station_raw

    broker_raw = str(site_cfg.get("broker") or "").strip()
    if broker_raw:
        if ":" in broker_raw:
            broker_host, broker_port_s = broker_raw.rsplit(":", 1)
            try:
                MQTT_BROKER_HOST, MQTT_BROKER_PORT = broker_host, int(broker_port_s)
            except ValueError:
                MQTT_BROKER_HOST = broker_raw
        else:
            MQTT_BROKER_HOST = broker_raw

    root_topic_raw = str(site_cfg.get("root_topic") or "").strip().strip("/")
    if root_topic_raw:
        MQTT_TOPIC_PUB = f"{root_topic_raw}/pub"
        MQTT_TOPIC_ACK = f"{root_topic_raw}/ack"

    # Any non-empty string derives a valid 16-byte key via MD5 (see
    # mqtt_pub_password_to_key()), so unlike the old base64-key field there's
    # no "invalid" state to warn about here -- just configured or not.
    password_raw = str(site_cfg.get("mqtt_pub_password") or "").strip()
    MQTT_PUB_AES_KEY = mqtt_pub_password_to_key(password_raw) if password_raw else None


refresh_site_config_overrides()

# ====== WEATHER CONFIG ======
YR_API_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
YR_USER_AGENT = os.getenv(
    "YR_USER_AGENT",
    "nanjing-weather-client/1.0 (contact: yisu.fang@outlook.com)",
)

OWM_API_URL = "https://api.openweathermap.org/data/2.5/forecast"
OWM_APPID = os.getenv("OWM_APPID", "4fb277504a118b9320ba6378abbdaf71")

OPEN_METEO_API_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_FORECAST_DAYS = 10  # covers HOURS_AHEAD=120 with margin; no API key needed

TIMEOUT_SECONDS = 20
HOURS_AHEAD = 120

# ====== IRRIGATION CONFIG ======
EXPOSURE_FACTOR = 0.75
FUTURE_CREDIT_MIN_MULTIPLIER = 0.50
W24 = 0.70
W12 = 0.30
BASELINE_PUMP_SECONDS_NORMAL = 120
MIN_PUMP_SECONDS_IF_RUNNING = 20

# Legacy: full fixed drench is gone — every scheduled event now gets a fresh
# fractional percentage of BASELINE_PUMP_SECONDS_NORMAL. Kept for reference.
FULL_DRENCH_SECONDS = BASELINE_PUMP_SECONDS_NORMAL
FREEZE_HOLD_TEMP_C = 0.0
EXTREME_HEAT_MAX_C = 40.0
RAIN_POSTPONE_FRACTION = 0.20
RAIN_RESET_FRACTION = 0.80
MAX_INTERVAL_DAYS = 14.0
NEXT_WATERING_JSON = str(_DATA_DIR / "next_watering.json")
WEATHER_CACHE_JSON = str(_DATA_DIR / "weather_cache.json")
# Irrigation decisions and METAR readings live in the ORM instead (see the
# django.setup() bootstrap above and dashboard/models.py's
# IrrigationDecision/MetarReading) -- both the hourly current-only refresh
# and the tri-hourly full run write MetarReading rows (see
# append_metar_log_row).
#
# Quantized wall-clock boundaries (00:00, 03:00, ... every
# RUN_INTERVAL_HOURS, 8x/day) so a suspended/sleeping machine can't desync an
# internal sleep() from the schedule. Used both by run_service()'s loop and
# to compute the *next* boundary for the dashboard's countdown display.
RUN_INTERVAL_HOURS = 3

# Per-source fetch retry + stale-fallback cache. A source's raw payload is
# cached after every successful fetch; if a run's live fetch keeps failing
# (e.g. a transient timeout) after all retries, the cached payload is
# re-extracted against the CURRENT now/horizon so the chart still shows the
# last known-good forecast instead of going blank, trimmed to whichever of
# its timestamps are still in the future.
FETCH_RETRY_ATTEMPTS = 3
FETCH_RETRY_WAIT_SECONDS = 5
SOURCE_PAYLOAD_CACHE = {
    "Yr.no": str(_DATA_DIR / "last_ok_yr.json"),
    "OWM": str(_DATA_DIR / "last_ok_owm.json"),
    "Open-Meteo": str(_DATA_DIR / "last_ok_om.json"),
    "METAR": str(_DATA_DIR / "last_ok_metar.json"),
}

#DEFAULT_OUTPUT = os.getenv("WEATHER_TEXT_OUTPUT", "./garden_weather_cache.txt")
DEFAULT_OUTPUT = str(_DATA_DIR / "weather.txt")

@dataclass
class LocationInfo:
    lat: float
    lon: float
    tz_name: str
    mode: str
    source_text: str
    station: str = ""
    station_name: str = ""


@dataclass
class Row:
    time_utc: datetime
    temp_c: Optional[float]
    rh_pct: Optional[float]
    wind_mps: Optional[float]
    wind_deg: Optional[float]
    precip_mm_period: Optional[float]
    period_hours: Optional[int]
    period_source: str
    precip_mm_contrib: float
    precip_mm_cum: float
    symbol_code: str
    interval_h: float = 1.0


def _env_bool(name: str, default: Optional[bool] = None) -> Optional[bool]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in ("1", "true", "yes", "y", "on", "ip"):
        return True
    if v in ("0", "false", "no", "n", "off", "fixed", "default"):
        return False
    return default


def get_tz(tz_name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    if tz_name == "Asia/Shanghai":
        return timezone(timedelta(hours=8))
    return timezone.utc


def next_quantized_run_epoch(now_local: datetime, interval_hours: int = RUN_INTERVAL_HOURS) -> int:
    """Next wall-clock boundary that's a multiple of interval_hours past local
    midnight (e.g. 00:00/03:00/06:00/.../21:00 for interval_hours=3). Used
    both for the dashboard's "next reading" countdown display and, since
    run_service() replaced saignes-weather.timer, to actually schedule the
    next full cycle."""
    slot = (now_local.hour // interval_hours + 1) * interval_hours
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight + timedelta(hours=slot)).timestamp())


def _load_json_dict(path: str) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_station_geo_cache() -> Dict[str, Any]:
    return _load_json_dict(STATION_GEO_CACHE_JSON)


def resolve_station_geo(station_id: str) -> Tuple[float, float, str, str]:
    """Resolve an ICAO airport code to (lat, lon, tz_name, display_name).

    lat/lon come from aviationweather.gov's /stationinfo endpoint -- static
    registry metadata (site name, state, country), not a live observation.
    Deliberately NOT sourced from METAR: a station can be a registered, real
    airport with zero recent METAR observations (e.g. Hualien/RCYU, whose
    sensor just isn't currently reporting), in which case /metar returns
    nothing at all and there'd be no lat/lon/name to resolve. /stationinfo
    answers "does this airport exist and where is it" independently of "is
    it reporting weather right now" (that's still METAR's job, in
    fetch_metar_rain_with_retry / metar_latest_observation). tz_name is
    reverse-geocoded from that point via Open-Meteo's timezone=auto (free,
    no key, already used elsewhere in this file for forecasts). display_name
    is derived straight from stationinfo's own "site" field (e.g.
    "Shanghai/Hongqiao" -> "Shanghai") instead of a Nominatim reverse
    geocode -- the part before the slash is the city, the part after is the
    specific airport/terminal, which is more detail than this display needs.
    Successful lookups are cached in STATION_GEO_CACHE_JSON keyed by
    station_id -- airport coordinates don't move, so this network round trip
    only needs to happen once per airport rather than on every
    3-hourly/hourly run."""
    cache = _load_station_geo_cache()
    cached = cache.get(station_id)
    if cached:
        return float(cached["lat"]), float(cached["lon"]), str(cached["tz"]), str(cached["name"])

    with requests.Session() as session:
        session.headers.update({"Connection": "close"})
        info_resp = session.get(
            STATION_INFO_API_URL, params={"ids": station_id, "format": "json"}, timeout=TIMEOUT_SECONDS,
        )
        if info_resp.status_code != 200:
            raise RuntimeError(f"aviationweather.gov stationinfo HTTP {info_resp.status_code}: {info_resp.text[:300]}")
        records = info_resp.json()
        if not isinstance(records, list) or not records:
            raise RuntimeError(f"no station registered for '{station_id}'")
        info = records[0]
        if info.get("lat") is None or info.get("lon") is None:
            raise RuntimeError(f"stationinfo for '{station_id}' has no lat/lon")
        lat, lon = float(info["lat"]), float(info["lon"])
        site = str(info.get("site") or station_id)
        name = site.split("/", 1)[0].strip() or station_id

        tz_resp = session.get(
            OPEN_METEO_API_URL,
            params={"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1},
            timeout=TIMEOUT_SECONDS,
        )
        if tz_resp.status_code != 200:
            raise RuntimeError(f"Open-Meteo timezone lookup HTTP {tz_resp.status_code}: {tz_resp.text[:300]}")
        tz_name = tz_resp.json().get("timezone") or DEFAULT_TZ

    cache[station_id] = {
        "lat": lat, "lon": lon, "tz": tz_name, "name": name,
        "resolved_at_epoch": int(time.time()),
    }
    atomic_write_json(STATION_GEO_CACHE_JSON, cache)
    return lat, lon, tz_name, name


def resolve_location(
    use_ip_location: Optional[bool],
    lat_override: Optional[float],
    lon_override: Optional[float],
    tz_override: Optional[str],
) -> LocationInfo:
    if lat_override is not None or lon_override is not None or tz_override is not None:
        lat = float(lat_override if lat_override is not None else DEFAULT_LAT)
        lon = float(lon_override if lon_override is not None else DEFAULT_LON)
        tz_name = str(tz_override or DEFAULT_TZ)
        return LocationInfo(
            lat=lat,
            lon=lon,
            tz_name=tz_name,
            mode="override",
            source_text=f"override lat={lat:.4f}, lon={lon:.4f}, tz={tz_name}",
        )

    use_ip = use_ip_location
    if use_ip is None:
        use_ip = _env_bool("IRRIG_USE_IP_LOCATION", USE_IP_LOCATION)

    if use_ip:
        try:
            with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as r:
                geo = json.loads(r.read())
            lat_s, lon_s = geo.get("loc", f"{DEFAULT_LAT},{DEFAULT_LON}").split(",")
            tz_name = geo.get("timezone", DEFAULT_TZ)
            city = geo.get("city") or "?"
            country = geo.get("country") or "?"
            return LocationInfo(
                lat=float(lat_s),
                lon=float(lon_s),
                tz_name=str(tz_name),
                mode="ip",
                source_text=f"ip city={city}, country={country}, lat={lat_s}, lon={lon_s}, tz={tz_name}",
            )
        except Exception as e:
            return LocationInfo(
                lat=DEFAULT_LAT,
                lon=DEFAULT_LON,
                tz_name=DEFAULT_TZ,
                mode="fixed_fallback",
                source_text=f"ip lookup failed ({type(e).__name__}: {e}); using defaults",
            )

    try:
        lat, lon, tz_name, display_name = resolve_station_geo(DEFAULT_STATION)
        return LocationInfo(
            lat=lat,
            lon=lon,
            tz_name=tz_name,
            mode="station",
            source_text=f"{DEFAULT_STATION} ({display_name}), lat={lat:.4f}, lon={lon:.4f}, tz={tz_name}",
            station=DEFAULT_STATION,
            station_name=display_name,
        )
    except Exception as e:
        return LocationInfo(
            lat=DEFAULT_LAT,
            lon=DEFAULT_LON,
            tz_name=DEFAULT_TZ,
            mode="fixed_fallback",
            source_text=(
                f"station lookup for {DEFAULT_STATION} failed ({type(e).__name__}: {e}); "
                f"using hardcoded fallback lat={DEFAULT_LAT:.4f}, lon={DEFAULT_LON:.4f}, tz={DEFAULT_TZ}"
            ),
            station=DEFAULT_STATION,
        )


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def mean(vals: List[float]) -> Optional[float]:
    return (sum(vals) / len(vals)) if vals else None


def mean_available(*vals: Optional[float]) -> Optional[float]:
    clean = [float(v) for v in vals if v is not None]
    return (sum(clean) / len(clean)) if clean else None


def lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if abs(x1 - x0) <= 1e-9:
        return y0
    t = clamp((x - x0) / (x1 - x0), 0.0, 1.0)
    return y0 + (y1 - y0) * t


def daily_precip_map_from_rows(rows: List["Row"], tz_name: str) -> Dict[str, float]:
    tz = get_tz(tz_name)
    out: Dict[str, float] = {}
    for r in rows:
        key = r.time_utc.astimezone(tz).date().isoformat()
        out[key] = out.get(key, 0.0) + float(r.precip_mm_contrib or 0.0)
    return out


def ensemble_daily_precip_map(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    tz_name: str,
) -> Dict[str, float]:
    maps: List[Dict[str, float]] = []
    if yr_rows is not None:
        maps.append(daily_precip_map_from_rows(yr_rows, tz_name))
    if owm_rows is not None:
        maps.append(daily_precip_map_from_rows(owm_rows, tz_name))
    if om_rows is not None:
        maps.append(daily_precip_map_from_rows(om_rows, tz_name))

    merged: Dict[str, float] = {}
    for key in sorted({k for mp in maps for k in mp.keys()}):
        vals = [mp[key] for mp in maps if key in mp]
        if vals:
            merged[key] = sum(vals) / len(vals)
    return merged


def rows_precip_between_hours(rows: List["Row"], now_utc: datetime, start_h: float, end_h: float) -> float:
    total = 0.0
    for r in rows:
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < start_h or h >= end_h:
            continue
        total += float(r.precip_mm_contrib or 0.0)
    return total


def ensemble_precip_between_hours(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    now_utc: datetime,
    start_h: float,
    end_h: float,
) -> Optional[float]:
    return mean_available(
        rows_precip_between_hours(yr_rows, now_utc, start_h, end_h) if yr_rows is not None else None,
        rows_precip_between_hours(owm_rows, now_utc, start_h, end_h) if owm_rows is not None else None,
        rows_precip_between_hours(om_rows, now_utc, start_h, end_h) if om_rows is not None else None,
    )


def rows_temp_stats(rows: List["Row"], now_utc: datetime) -> Dict[str, Optional[float]]:
    vals24: List[float] = []
    vals72: List[float] = []
    for r in rows:
        if r.temp_c is None:
            continue
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < 0:
            continue
        if h < 24.0:
            vals24.append(float(r.temp_c))
        if h < 72.0:
            vals72.append(float(r.temp_c))
    return {
        "tmin24_c": min(vals24) if vals24 else None,
        "tmax24_c": max(vals24) if vals24 else None,
        "tmean72_c": mean(vals72) if vals72 else None,
    }


def ensemble_temp_stats(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    now_utc: datetime,
) -> Dict[str, Optional[float]]:
    yr_s = rows_temp_stats(yr_rows, now_utc) if yr_rows is not None else {}
    owm_s = rows_temp_stats(owm_rows, now_utc) if owm_rows is not None else {}
    om_s = rows_temp_stats(om_rows, now_utc) if om_rows is not None else {}
    return {
        "tmin24_c": mean_available(yr_s.get("tmin24_c"), owm_s.get("tmin24_c"), om_s.get("tmin24_c")),
        "tmax24_c": mean_available(yr_s.get("tmax24_c"), owm_s.get("tmax24_c"), om_s.get("tmax24_c")),
        "tmean72_c": mean_available(yr_s.get("tmean72_c"), owm_s.get("tmean72_c"), om_s.get("tmean72_c")),
    }


def target_interval_days_from_temperature(
    tmin24_c: Optional[float],
    tmean72_c: Optional[float],
    tmax24_c: Optional[float],
) -> Tuple[float, str]:
    tmin24 = float(tmin24_c) if tmin24_c is not None else 5.0
    tmean72 = float(tmean72_c) if tmean72_c is not None else 15.0
    tmax24 = float(tmax24_c) if tmax24_c is not None else tmean72

    if tmax24 > EXTREME_HEAT_MAX_C:
        return 0.5, "EXTREME_HEAT_TWICE_DAILY"
    if tmin24 < FREEZE_HOLD_TEMP_C:
        return float("inf"), "FREEZE_HOLD"

    anchors = [
        (5.0, 14.0),
        (10.0, 10.0),
        (15.0, 7.0),
        (20.0, 3.5),
        (25.0, 2.0),
        (29.0, 1.0),
    ]

    if tmean72 <= anchors[0][0]:
        return anchors[0][1], "TEMPERATURE_CADENCE"
    if tmean72 >= anchors[-1][0]:
        return anchors[-1][1], "TEMPERATURE_CADENCE"

    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= tmean72 <= x1:
            return lerp(tmean72, x0, x1, y0, y1), "TEMPERATURE_CADENCE"

    return 7.0, "TEMPERATURE_CADENCE"


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(out_path.parent),
        prefix=out_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    os.replace(tmp_name, out_path)


def encrypt_pub_payload(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-CBC-encrypt `plaintext` under `key` (exactly 16 bytes) with a
    fresh random IV, PKCS7-padded, and return base64(iv || ciphertext) --
    ASCII bytes safe to hand straight to paho as the MQTT payload. Wire
    format is deliberately the simplest thing an embedded AES library
    (mbedTLS, ESP32's own crypto, etc.) can decrypt: base64-decode, split
    off the first 16 bytes as the IV, AES-128-CBC-decrypt the rest, strip
    PKCS7 padding. No authentication tag (that's GCM, not CBC) -- fine for
    the same reason it's fine on the ack topic's own decrypt_ack_payload:
    a hobby irrigation controller without a real forged/tampered-command
    threat model, not a pattern to copy into anything higher-stakes."""
    if _AesCipher is None:
        raise RuntimeError(
            "the 'cryptography' package is not installed; run 'pip install cryptography' "
            "to enable MQTT payload encryption."
        )
    iv = os.urandom(16)
    padder = _aes_padding.PKCS7(_aes_algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = _AesCipher(_aes_algorithms.AES(key), _aes_modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext)


def decrypt_ack_payload(wire_payload: bytes, key: bytes) -> bytes:
    """Reverse of encrypt_pub_payload, for the ack topic (pump -> dashboard) --
    now using the identical key=MD5(password) / AES-128-CBC / base64(iv ||
    ciphertext) wire format as the pub topic, instead of the old plaintext-
    JSON-with-the-full-schedule-echoed-back ack shape. base64-decode, split
    the first 16 bytes off as the IV, AES-128-CBC-decrypt the rest, strip
    PKCS7 padding."""
    if _AesCipher is None:
        raise RuntimeError(
            "the 'cryptography' package is not installed; run 'pip install cryptography' "
            "to enable MQTT payload decryption."
        )
    raw = base64.b64decode(wire_payload)
    iv, ciphertext = raw[:16], raw[16:]
    decryptor = _AesCipher(_aes_algorithms.AES(key), _aes_modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = _aes_padding.PKCS7(_aes_algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def publish_mqtt_schedule(payload: Dict[str, Any]) -> str:
    """Publish the irrigation schedule payload to the MQTT broker.

    Uses a one-shot connect/publish/disconnect (paho.mqtt.publish.single) so
    the script stays stateless and cron-friendly. Returns a short human
    description of what was published. Raises RuntimeError if paho-mqtt is not
    installed, or propagates any connection/publish error from paho so the
    caller can decide whether to treat it as fatal.

    When MQTT_PUB_AES_KEY is configured (derived from site_config.json's
    mqtt_pub_password), the wire payload is AES-128-CBC-encrypted and
    base64-transported instead of raw JSON -- see encrypt_pub_payload().
    With no password configured at all, it publishes plaintext JSON and
    warns, so a fresh checkout / pre-firmware-rollout deployment keeps
    working.
    """
    if _mqtt_publish is None:
        raise RuntimeError(
            "paho-mqtt is not installed; run 'pip install paho-mqtt' to enable "
            "MQTT publishing (or set MQTT_SEND_ENABLED = False)."
        )

    auth = None
    if MQTT_USERNAME:
        auth = {"username": MQTT_USERNAME, "password": MQTT_PASSWORD or ""}

    client_id = f"{MQTT_CLIENT_ID_PREFIX}-{uuid.uuid4().hex[:6]}"
    body_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    if MQTT_PUB_AES_KEY is not None:
        wire_body = encrypt_pub_payload(body_json.encode("utf-8"), MQTT_PUB_AES_KEY)
        encryption_note = "aes128-cbc+b64"
    else:
        wire_body = body_json
        encryption_note = "PLAINTEXT (no mqtt_pub_password configured)"
        print(
            "WARNING: publishing MQTT schedule as plaintext JSON -- set "
            "mqtt_pub_password in site_config.json to encrypt it.",
            file=sys.stderr,
        )

    _mqtt_publish.single(
        MQTT_TOPIC_PUB,
        payload=wire_body,
        qos=MQTT_QOS,
        retain=MQTT_RETAIN,
        hostname=MQTT_BROKER_HOST,
        port=MQTT_BROKER_PORT,
        client_id=client_id,
        keepalive=MQTT_KEEPALIVE_SECONDS,
        auth=auth,
    )
    return (
        f"{len(wire_body)} bytes ({encryption_note}) -> {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT} "
        f"topic '{MQTT_TOPIC_PUB}' (qos={MQTT_QOS}, retain={MQTT_RETAIN}, client_id={client_id})"
    )


def _load_pump_acks(path: str) -> Dict[str, Any]:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"devices": {}, "recent": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"devices": {}, "recent": []}


def _on_ack_message(_client, _userdata, msg) -> None:
    """paho on_message callback for the ack topic.

    Merges the ack into PUMP_ACKS_JSON: one "latest" entry per device_id/MAC
    (so the dashboard can list every known node), plus a capped "recent" feed
    (so it can show acks arriving over time, not just the latest per node).
    Best-effort: a malformed payload or a write race is logged and dropped,
    never raised (this runs on paho's network thread).

    The wire payload is now the same key=MD5(password) / AES-128-CBC /
    base64(iv || ciphertext) scheme as the pub topic (see decrypt_ack_payload)
    instead of plaintext JSON with the full schedule echoed back -- the
    decrypted body carries an "md5" field (a hash of what the pump received)
    rather than the payload itself, so a corrupted/mismatched delivery still
    shows up without the ack needing to repeat the whole schedule back over
    the air. Falls back to plaintext JSON when MQTT_PUB_AES_KEY isn't
    configured, matching publish_mqtt_schedule's own pre-encryption fallback.
    """
    try:
        if MQTT_PUB_AES_KEY is not None:
            body = decrypt_ack_payload(msg.payload, MQTT_PUB_AES_KEY)
        else:
            body = msg.payload
        ack = json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"WARNING: dropped malformed ACK payload ({type(e).__name__}: {e})", file=sys.stderr)
        return

    device_id = str(ack.get("device_id") or "unknown")
    received_epoch = time.time()
    ack["received_at_epoch"] = received_epoch
    ack["received_at_local"] = datetime.now().astimezone().isoformat(timespec="seconds")

    store = _load_pump_acks(PUMP_ACKS_JSON)
    store.setdefault("devices", {})[device_id] = ack
    recent = store.setdefault("recent", [])
    recent.append(ack)
    del recent[:-PUMP_ACKS_RECENT_MAX]  # keep only the last N, oldest first

    try:
        atomic_write_json(PUMP_ACKS_JSON, store)
    except Exception as e:
        print(f"WARNING: could not persist pump ack ({type(e).__name__}: {e})", file=sys.stderr)
        return

    print(
        f"MQTT: ACK from {device_id} (executed={ack.get('executed')}, md5={ack.get('md5')})",
        file=sys.stderr,
    )


def start_ack_subscriber() -> Optional["_mqtt_client.Client"]:
    """Start a persistent background MQTT subscriber on MQTT_TOPIC_ACK.

    Unlike publish_mqtt_schedule() (a one-shot connect/publish/disconnect run
    once per forecast cycle), pump acks can arrive at any time -- each node
    wakes on its own schedule and reports in. So this client is created once
    for the lifetime of the process -- called from run_service() (the normal
    persistent saignes-weather.service) or standalone via `weather_mqtt.py
    --ack-listener` for debugging -- and kept connected via paho's own
    network thread (loop_start) with auto-reconnect, rather than being
    re-opened per cycle. Runs independently of whatever the calling
    process's main thread is doing (e.g. run_service()'s sleep loop).
    Returns the client (so callers could stop it) or None if unavailable.
    """
    if _mqtt_client is None:
        print(
            "WARNING: paho-mqtt client module unavailable; pump ACKs will not be tracked.",
            file=sys.stderr,
        )
        return None

    client_id = f"{MQTT_ACK_CLIENT_ID_PREFIX}-{uuid.uuid4().hex[:6]}"
    # paho-mqtt >=2.0 requires picking a callback API version explicitly;
    # VERSION1 keeps the classic on_connect(client, userdata, flags, rc)
    # signature used below.
    client = _mqtt_client.Client(_mqtt_client.CallbackAPIVersion.VERSION1, client_id=client_id)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or "")
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    client.on_message = _on_ack_message

    def _on_connect(c, _userdata, _flags, rc, *_args):
        if rc == 0:
            c.subscribe(MQTT_TOPIC_ACK, qos=MQTT_QOS)
            print(f"MQTT: ack subscriber connected, subscribed '{MQTT_TOPIC_ACK}'", file=sys.stderr)
        else:
            print(f"WARNING: ack subscriber connect failed, rc={rc}", file=sys.stderr)

    client.on_connect = _on_connect

    # connect_async() queues the connection for loop_start()'s background
    # thread instead of dialing synchronously here. connect() would block
    # main() (and therefore the whole service's startup, hanging
    # `systemctl restart`) for as long as the TCP handshake takes to fail --
    # which can be minutes if the broker's port is silently dropped by a
    # firewall rather than actively refused.
    try:
        client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=MQTT_KEEPALIVE_SECONDS)
    except Exception as e:
        print(f"WARNING: ack subscriber initial connect failed ({type(e).__name__}: {e}); will retry in background.", file=sys.stderr)
    client.loop_start()
    return client


def upsert_irrigation_decision(row: Dict[str, Any]) -> None:
    """Upsert one IrrigationDecision row keyed by local_date -- same
    upsert-by-date behavior the CSV era's upsert_irrigation_db_row had."""
    local_date = row["local_date"]
    defaults = {k: v for k, v in row.items() if k != "local_date"}
    IrrigationDecision.objects.update_or_create(local_date=local_date, defaults=defaults)


def latest_db_row(qs) -> Optional["IrrigationDecision"]:
    return qs.order_by("local_date", "generated_at_local").last()


def last_completed_event_epoch(qs) -> Optional[int]:
    return qs.filter(event_completed=True).aggregate(Max("event_epoch"))["event_epoch__max"]


# ---------- Solar anchors (sunrise / sunset quantization) ----------
ZENITH_OFFICIAL_DEG = 90.833  # official sunrise/sunset (refraction + solar disc)


def solar_events_utc_minutes(lat_deg: float, lon_deg: float, when_utc: datetime) -> Tuple[float, float]:
    """NOAA approximation: (sunrise, sunset) in minutes after 00:00 UTC of
    `when_utc`'s UTC date. Longitude positive EAST. Clamped for polar cases."""
    n = when_utc.timetuple().tm_yday
    gamma = 2.0 * math.pi / 365.0 * (n - 1 + 0.5)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2.0 * gamma)
        - 0.040849 * math.sin(2.0 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2.0 * gamma)
        + 0.000907 * math.sin(2.0 * gamma)
        - 0.002697 * math.cos(3.0 * gamma)
        + 0.00148 * math.sin(3.0 * gamma)
    )
    lat = math.radians(lat_deg)
    cos_ha = (
        math.cos(math.radians(ZENITH_OFFICIAL_DEG)) / (math.cos(lat) * math.cos(decl))
        - math.tan(lat) * math.tan(decl)
    )
    cos_ha = clamp(cos_ha, -1.0, 1.0)
    ha_deg = math.degrees(math.acos(cos_ha))
    sunrise_min = 720.0 - 4.0 * (lon_deg + ha_deg) - eqtime
    sunset_min = 720.0 - 4.0 * (lon_deg - ha_deg) - eqtime
    return sunrise_min, sunset_min


def solar_anchor_epochs_for_utc_date(lat_deg: float, lon_deg: float, day_utc: datetime) -> List[Tuple[float, str]]:
    midnight = datetime(day_utc.year, day_utc.month, day_utc.day, tzinfo=timezone.utc)
    sr_min, ss_min = solar_events_utc_minutes(lat_deg, lon_deg, midnight)
    base = midnight.timestamp()
    return [(base + sr_min * 60.0, "sunrise"), (base + ss_min * 60.0, "sunset")]


def quantize_epoch_to_solar(due_epoch: float, lat_deg: float, lon_deg: float) -> Tuple[int, str]:
    """Snap a due epoch to the FIRST sunrise or sunset at/after it (mild
    temperature hours), so pumps never run in midday heat or at night."""
    due_dt = datetime.fromtimestamp(float(due_epoch), tz=timezone.utc)
    candidates: List[Tuple[float, str]] = []
    for d in (-1, 0, 1, 2):
        candidates.extend(
            solar_anchor_epochs_for_utc_date(lat_deg, lon_deg, due_dt + timedelta(days=d))
        )
    candidates.sort(key=lambda x: x[0])
    for ep, name in candidates:
        if ep >= float(due_epoch):
            # floor, not round: rounding up can push the returned integer
            # above the true (fractional-second) anchor instant, so a later
            # run that re-quantizes this exact stored value would find its
            # own anchor no longer satisfies `ep >= due_epoch` and silently
            # skip forward to the next one. Flooring guarantees the stored
            # epoch is always <= the true instant, keeping this idempotent.
            return int(math.floor(ep)), name
    ep, name = candidates[-1]
    return int(math.floor(ep)), name


# ---------- Fresh fractional percentage at an arbitrary event time ----------
def _ensemble_demand_at_offset(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    now_utc: datetime,
    now_local: datetime,
    lat_deg: float,
    start_h: float,
    end_h: float,
) -> Optional[float]:
    vals: List[float] = []
    if yr_rows:
        vals.append(climate_demand_window(yr_rows, now_utc, now_local, lat_deg, start_h, end_h)[0])
    if owm_rows:
        vals.append(climate_demand_window(owm_rows, now_utc, now_local, lat_deg, start_h, end_h)[0])
    if om_rows:
        vals.append(climate_demand_window(om_rows, now_utc, now_local, lat_deg, start_h, end_h)[0])
    return mean(vals)


def fractional_percent_for_event(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    now_utc: datetime,
    now_local: datetime,
    lat_deg: float,
    event_epoch: float,
    horizon_h: float,
) -> Tuple[int, int, str, Dict[str, float]]:
    """Recompute the fractional regime (rain index vs climate demand + future
    rain credit) for the 24h window STARTING at the event time, instead of a
    quantized all-or-nothing drench. Returns (percent, pump_seconds,
    percent_basis, debug)."""
    h_event = max(0.0, (float(event_epoch) - now_utc.timestamp()) / 3600.0)
    end_24 = min(h_event + 24.0, horizon_h)

    # Need at least ~6h of forecast coverage around the event to trust a
    # fractional value; beyond the horizon fall back to a full drench that
    # the NEXT cron run (closer in time) will refine.
    if h_event >= horizon_h - 1.0 or (end_24 - h_event) < 6.0:
        pct = 100
        return pct, pump_seconds_from_percent(pct), "default_beyond_forecast", {"h_event": h_event}

    r12 = float(ensemble_precip_between_hours(yr_rows, owm_rows, om_rows, now_utc, h_event, min(h_event + 12.0, horizon_h)) or 0.0)
    r24 = float(ensemble_precip_between_hours(yr_rows, owm_rows, om_rows, now_utc, h_event, end_24) or 0.0)
    demand0 = _ensemble_demand_at_offset(yr_rows, owm_rows, om_rows, now_utc, now_local, lat_deg, h_event, end_24)
    if demand0 is None:
        demand0 = 2.0

    pct_base, dbg = decision_percent_dynamic(r12, r24, demand0, exposure=EXPOSURE_FACTOR)

    # Future rain credit relative to the event (24-72h after it).
    fut_start = min(h_event + 24.0, horizon_h)
    fut_end = min(h_event + 72.0, horizon_h)
    rain_future_eff = float(ensemble_precip_between_hours(yr_rows, owm_rows, om_rows, now_utc, fut_start, fut_end) or 0.0) * EXPOSURE_FACTOR
    d1 = _ensemble_demand_at_offset(yr_rows, owm_rows, om_rows, now_utc, now_local, lat_deg, fut_start, min(h_event + 48.0, horizon_h)) or demand0
    d2 = _ensemble_demand_at_offset(yr_rows, owm_rows, om_rows, now_utc, now_local, lat_deg, min(h_event + 48.0, horizon_h), fut_end) or demand0
    future_need = max(0.0, d1 + d2)
    if future_need <= 1e-6:
        future_need = 2.0 * max(demand0, 0.0)

    mult, cov = future_credit_multiplier(rain_future_eff, future_need, FUTURE_CREDIT_MIN_MULTIPLIER)
    pct = int(round(clamp(float(pct_base) * mult, 0.0, 100.0)))

    dbg = dict(dbg)
    dbg.update({
        "h_event": h_event,
        "pct_base": float(pct_base),
        "future_multiplier": float(mult),
        "future_cover_ratio": float(cov),
        "future_rain_eff_mm": float(rain_future_eff),
        "future_need_mm": float(future_need),
    })
    return pct, pump_seconds_from_percent(pct), "forecast_fractional", dbg


def apply_irrigation_schedule(
    now_local: datetime,
    now_utc: datetime,
    location: "LocationInfo",
    source_mode: str,
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    ensemble: Dict[str, Any],
    db_rows,  # IrrigationDecision queryset (dashboard.models)
    recent_rain_mm: float,
) -> Dict[str, Any]:
    tz = get_tz(location.tz_name)
    now_epoch = int(now_local.timestamp())
    temp_stats = ensemble_temp_stats(yr_rows=yr_rows, owm_rows=owm_rows, om_rows=om_rows, now_utc=now_utc)
    tmin24_c = temp_stats.get("tmin24_c")
    tmean72_c = temp_stats.get("tmean72_c")
    tmax24_c = temp_stats.get("tmax24_c")
    target_interval_days, temp_mode = target_interval_days_from_temperature(tmin24_c, tmean72_c, tmax24_c)

    today = now_local.date()
    daily_precip_map = ensemble_daily_precip_map(yr_rows, owm_rows, om_rows, location.tz_name)
    forecast_precip_local_day_mm = float(daily_precip_map.get(today.isoformat(), 0.0))
    forecast_precip_next24_mm = float(ensemble_precip_between_hours(yr_rows, owm_rows, om_rows, now_utc, 0.0, 24.0) or 0.0)
    effective_rain_today_mm = forecast_precip_local_day_mm * EXPOSURE_FACTOR
    recent_rain_eff_mm = recent_rain_mm * EXPOSURE_FACTOR

    latest_row = latest_db_row(db_rows)
    stored_next_due_epoch = float(latest_row.next_watering_epoch) if latest_row else None
    previous_last_event_epoch = last_completed_event_epoch(db_rows)

    if stored_next_due_epoch is None:
        active_next_due_epoch = float(now_epoch)
        has_existing_schedule = False
    else:
        active_next_due_epoch = float(stored_next_due_epoch)
        has_existing_schedule = True

    due_now = (not has_existing_schedule) or (now_epoch >= int(active_next_due_epoch))

    finite_interval_days = target_interval_days if math.isfinite(target_interval_days) else 1.0
    interval_demand_mm = max(0.10, float(ensemble.get("demand0") or 0.0) * finite_interval_days)
    # No longer used to move the schedule (see Step 1 below) -- kept only as
    # informational metrics on the dashboard/history row.
    rain_postpone_threshold_mm = RAIN_POSTPONE_FRACTION * interval_demand_mm
    rain_reset_threshold_mm = RAIN_RESET_FRACTION * interval_demand_mm
    full_interval_seconds = int(round(finite_interval_days * 86400.0))

    decision_code = "WAIT"
    decision_label = "SKIP"
    should_irrigate_now = False
    event_completed = False
    event_epoch: Optional[int] = None
    schedule_armed = True
    note = ""

    # ---- Step 1: decide the RAW due epoch of the next event (event #1). ----
    # Fixed cadence, like a train timetable: the temperature-derived interval
    # sets the departure times, and nothing about weather ever moves them.
    # Rain's only effect is on the AMOUNT (Step 3's fresh fractional percent,
    # which can already reach 0% on its own under heavy forecast rain) --
    # never on the date. The only exception is FREEZE_HOLD, a safety pause
    # (not a weather-driven optimization), which keeps its own short recheck
    # cadence below.
    if temp_mode == "FREEZE_HOLD":
        decision_code = "FREEZE_HOLD"
        decision_label = "FREEZE_HOLD"
        schedule_armed = False
        event1_due_epoch = now_epoch + 86400  # recheck tomorrow
        note = "forecast low under freezing; routine irrigation halted"
    elif due_now:
        decision_code = "DRENCH"
        should_irrigate_now = True
        event1_due_epoch = now_epoch
        note = "due now; fractional drench scheduled at next sunrise/sunset anchor"
    else:
        decision_code = "WAIT"
        event1_due_epoch = int(active_next_due_epoch)
        note = "waiting for next due date"

    # ---- Step 2: quantize events to forecast sunrise/sunset (mild hours). ----
    event1_epoch, event1_anchor = quantize_epoch_to_solar(float(event1_due_epoch), location.lat, location.lon)
    step_seconds = full_interval_seconds if schedule_armed else 86400
    event2_epoch, event2_anchor = quantize_epoch_to_solar(float(event1_epoch + step_seconds), location.lat, location.lon)
    if event2_epoch <= event1_epoch:
        event2_epoch, event2_anchor = quantize_epoch_to_solar(float(event1_epoch + step_seconds + 3600.0), location.lat, location.lon)

    # ---- Step 3: fresh fractional percentage at each quantized event. ----
    e1_pct, e1_seconds, e1_basis, e1_dbg = fractional_percent_for_event(
        yr_rows, owm_rows, om_rows, now_utc, now_local, location.lat, event1_epoch, float(HOURS_AHEAD)
    )
    e2_pct, e2_seconds, e2_basis, e2_dbg = fractional_percent_for_event(
        yr_rows, owm_rows, om_rows, now_utc, now_local, location.lat, event2_epoch, float(HOURS_AHEAD)
    )

    # Recent (already-elapsed) rain discounts event #1 the same way future
    # rain credit does -- only event #1, since it's the one actually being
    # committed this run; event #2 is a rough preview that a later run will
    # recompute with its own then-current recent-rain state anyway.
    recent_mult, recent_cov = future_credit_multiplier(recent_rain_eff_mm, interval_demand_mm, FUTURE_CREDIT_MIN_MULTIPLIER)
    e1_pct = int(round(clamp(e1_pct * recent_mult, 0.0, 100.0)))
    e1_seconds = pump_seconds_from_percent(e1_pct)

    if not schedule_armed:
        e1_pct, e1_seconds, e1_basis = 0, 0, "freeze_hold"
        e2_pct, e2_seconds, e2_basis = 0, 0, "freeze_hold"

    if decision_code == "DRENCH":
        decision_label = label_from_percent(e1_pct)
        commanded_pump_seconds = e1_seconds
        command_percent = e1_pct
        event_completed = True
        event_epoch = event1_epoch  # the commanded event (executed at its solar anchor)
        # event #1 is being handed to the pumps this run, so the stored
        # pending due date advances to event #2.
        next_watering_epoch = event2_epoch
    else:
        commanded_pump_seconds = 0
        command_percent = 0
        next_watering_epoch = event1_epoch

    projected_following_epoch, _projected_anchor = quantize_epoch_to_solar(
        float(next_watering_epoch + step_seconds), location.lat, location.lon
    )

    pump_events = [
        {
            "sequence": 1,
            "epoch": int(event1_epoch),
            "iso_local": datetime.fromtimestamp(event1_epoch, tz=tz).isoformat(timespec="seconds"),
            "solar_anchor": event1_anchor,
            "percent": int(e1_pct),
            "pump_seconds": int(e1_seconds),
            "percent_basis": e1_basis,
        },
        {
            "sequence": 2,
            "epoch": int(event2_epoch),
            "iso_local": datetime.fromtimestamp(event2_epoch, tz=tz).isoformat(timespec="seconds"),
            "solar_anchor": event2_anchor,
            "percent": int(e2_pct),
            "pump_seconds": int(e2_seconds),
            "percent_basis": e2_basis,
        },
    ]

    last_event_epoch = event_epoch if event_completed else previous_last_event_epoch
    next_watering_iso_local = datetime.fromtimestamp(next_watering_epoch, tz=tz).isoformat(timespec="seconds")
    projected_following_iso_local = datetime.fromtimestamp(projected_following_epoch, tz=tz).isoformat(timespec="seconds")
    next_command_epoch = int(event1_epoch)

    e1_txt = (
        f"event#1 {pump_events[0]['iso_local']} ({event1_anchor}) -> "
        f"{e1_pct}% / {e1_seconds}s"
    )
    e2_txt = (
        f"event#2 {pump_events[1]['iso_local']} ({event2_anchor}) -> "
        f"{e2_pct}% / {e2_seconds}s"
    )

    if decision_code == "DRENCH":
        summary = (
            f"Due now: fractional drench {e1_pct}% ({e1_seconds}s) quantized to {event1_anchor} "
            f"at {pump_events[0]['iso_local']}. "
            f"Temperature cadence = {finite_interval_days:.2f} days ({temp_mode}). "
            f"{e2_txt} (will be refreshed by later runs)."
        )
    elif decision_code == "FREEZE_HOLD":
        summary = (
            f"Freeze hold. Forecast minimum next 24h is {fmt_num(tmin24_c, 1)}°C, below {FREEZE_HOLD_TEMP_C:.1f}°C. "
            f"Routine irrigation is halted; schedule rechecks at {pump_events[0]['iso_local']}."
        )
    else:
        summary = (
            f"No drench commanded today. Temperature cadence = {finite_interval_days:.2f} days ({temp_mode}). "
            f"{e1_txt}; {e2_txt}."
        )

    # Native Python types for IrrigationDecision.objects.update_or_create's
    # defaults -- the CSV era formatted everything to strings here since a
    # CSV row has no other option; the ORM's fields are already typed, so
    # that formatting is gone.
    db_row = {
        # ISO string, not a date object -- this dict also gets embedded into
        # ensemble["schedule"]["db_row"] and written to weather_cache.json
        # via plain json.dumps (atomic_write_json), which can't serialize a
        # raw date. Django's DateField parses "YYYY-MM-DD" strings on write,
        # so the ORM side doesn't need the object form either.
        "local_date": today.isoformat(),
        "station": location.station,
        "generated_at_local": now_local.isoformat(timespec="seconds"),
        "source_mode": source_mode,
        "forecast_tmin24_c": float(tmin24_c) if tmin24_c is not None else None,
        "forecast_tmean72_c": float(tmean72_c) if tmean72_c is not None else None,
        "forecast_tmax24_c": float(tmax24_c) if tmax24_c is not None else None,
        "target_interval_days": target_interval_days if math.isfinite(target_interval_days) else None,
        "interval_demand_mm": float(interval_demand_mm),
        "forecast_precip_local_day_mm": float(forecast_precip_local_day_mm),
        "forecast_precip_next24_mm": float(forecast_precip_next24_mm),
        "effective_rain_today_mm": float(effective_rain_today_mm),
        "recent_rain_mm": float(recent_rain_mm),
        "rain_postpone_threshold_mm": float(rain_postpone_threshold_mm),
        "rain_reset_threshold_mm": float(rain_reset_threshold_mm),
        "decision_code": decision_code,
        "decision_label": decision_label,
        "should_irrigate_now": bool(should_irrigate_now),
        "commanded_percent": int(command_percent),
        "commanded_pump_seconds": int(commanded_pump_seconds),
        "event_completed": bool(event_completed),
        "event_epoch": None if event_epoch is None else int(event_epoch),
        "event_solar_anchor": event1_anchor if should_irrigate_now else "",
        "last_event_epoch": None if last_event_epoch is None else int(last_event_epoch),
        "next_watering_epoch": int(next_watering_epoch),
        "next_watering_iso_local": next_watering_iso_local,
        "projected_following_epoch": int(projected_following_epoch),
        "projected_following_iso_local": projected_following_iso_local,
        "schedule_armed": bool(schedule_armed),
        "note": note,
    }

    # This payload is both written to NEXT_WATERING_JSON and published over
    # MQTT after every run: it always carries the next two events.
    next_watering_payload = {
        "schema_version": "2.0",
        "generated_at_epoch": now_epoch,
        "generated_at_local": now_local.isoformat(timespec="seconds"),
        "location_tz": location.tz_name,
        "schedule_armed": schedule_armed,
        "should_irrigate_now": should_irrigate_now,
        "commanded_percent": int(command_percent),
        "commanded_pump_seconds": int(commanded_pump_seconds),
        "decision_code": decision_code,
        "decision_label": decision_label,
        "baseline_pump_seconds_normal": BASELINE_PUMP_SECONDS_NORMAL,
        "events": pump_events,
        "next_watering_epoch": int(next_command_epoch),
        "next_watering_iso_local": datetime.fromtimestamp(next_command_epoch, tz=tz).isoformat(timespec="seconds"),
        "projected_following_epoch": int(projected_following_epoch),
        "projected_following_iso_local": projected_following_iso_local,
        "note": note,
    }

    return {
        "tmin24_c": tmin24_c,
        "tmean72_c": tmean72_c,
        "tmax24_c": tmax24_c,
        "temperature_mode": temp_mode,
        "target_interval_days": None if not math.isfinite(target_interval_days) else float(target_interval_days),
        "forecast_precip_local_day_mm": forecast_precip_local_day_mm,
        "forecast_precip_next24_mm": forecast_precip_next24_mm,
        "effective_rain_today_mm": effective_rain_today_mm,
        "recent_rain_mm": recent_rain_mm,
        "interval_demand_mm": interval_demand_mm,
        "rain_postpone_threshold_mm": rain_postpone_threshold_mm,
        "rain_reset_threshold_mm": rain_reset_threshold_mm,
        "due_now": due_now,
        "command_percent": command_percent,
        "commanded_pump_seconds": int(commanded_pump_seconds),
        "decision_code": decision_code,
        "decision_label": decision_label,
        "summary_text": summary,
        "db_row": db_row,
        "db_path": "sqlite (dashboard.models.IrrigationDecision)",
        "next_watering_epoch": int(next_command_epoch),
        "next_watering_iso_local": datetime.fromtimestamp(next_command_epoch, tz=tz).isoformat(timespec="seconds"),
        "projected_following_epoch": int(projected_following_epoch),
        "projected_following_iso_local": projected_following_iso_local,
        "schedule_armed": schedule_armed,
        "should_irrigate_now": should_irrigate_now,
        "json_path": NEXT_WATERING_JSON,
        "json_payload": next_watering_payload,
        "note": note,
    }


def parse_iso_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def to_float_or_none(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def k_to_c(v: Any) -> Optional[float]:
    try:
        return float(v) - 273.15 if v is not None else None
    except Exception:
        return None


def fmt_num(v: Any, digits: int = 2, none_text: str = "-") -> str:
    if v is None:
        return none_text
    try:
        x = float(v)
        if x.is_integer():
            return str(int(x))
        return f"{x:.{digits}f}"
    except Exception:
        return str(v)


def mean_abs_diff(vals: List[Tuple[Optional[float], Optional[float]]]) -> Optional[float]:
    diffs: List[float] = []
    for a, b in vals:
        if a is None or b is None:
            continue
        diffs.append(abs(a - b))
    return mean(diffs)


# ---------- Yr.no ----------
def fetch_yr_forecast(session: requests.Session, lat: float, lon: float) -> Dict[str, Any]:
    headers = {
        "User-Agent": YR_USER_AGENT,
        "Accept": "application/json",
    }
    params = {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}
    resp = session.get(YR_API_URL, params=params, headers=headers, timeout=TIMEOUT_SECONDS)

    if resp.status_code == 403:
        raise RuntimeError("Yr.no 403 Forbidden: set a real custom User-Agent.")
    if resp.status_code == 429:
        raise RuntimeError("Yr.no 429 Too Many Requests.")
    if resp.status_code not in (200, 203):
        raise RuntimeError(f"Yr.no HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def pick_precip_block_yr(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[int], str, str]:
    for key, hours in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
        block = data.get(key)
        if isinstance(block, dict):
            details = block.get("details", {})
            summary = block.get("summary", {})
            precip = details.get("precipitation_amount")
            symbol = str(summary.get("symbol_code") or "")
            return to_float_or_none(precip), hours, f"yr_{key}", symbol
    return None, None, "", ""


def extract_rows_yr_with_cumulative(
    forecast: Dict[str, Any], now_utc: datetime, horizon_h: int = 72
) -> List[Row]:
    end_utc = now_utc + timedelta(hours=horizon_h)
    timeseries = forecast.get("properties", {}).get("timeseries", [])
    if not isinstance(timeseries, list):
        raise RuntimeError("Invalid Yr.no payload: properties.timeseries missing or not a list.")

    parsed_items: List[Tuple[datetime, Dict[str, Any]]] = []
    for item in timeseries:
        if not isinstance(item, dict):
            continue
        ts = item.get("time")
        if not isinstance(ts, str):
            continue
        try:
            t = parse_iso_utc(ts)
        except Exception:
            continue
        parsed_items.append((t, item))

    parsed_items.sort(key=lambda x: x[0])
    rows: List[Row] = []
    cumulative = 0.0

    for i, (t, item) in enumerate(parsed_items):
        if t < now_utc or t >= end_utc:
            continue

        next_t = parsed_items[i + 1][0] if (i + 1) < len(parsed_items) else end_utc
        interval_end = min(next_t, end_utc)
        interval_h = max(0.0, (interval_end - t).total_seconds() / 3600.0)
        if interval_h <= 0.0:
            continue

        data = item.get("data", {}) or {}
        instant = (data.get("instant") or {}).get("details", {}) or {}
        precip_mm, period_h, source, symbol = pick_precip_block_yr(data)

        contrib = 0.0
        if precip_mm is not None and period_h is not None and period_h > 0:
            covered_h = min(interval_h, float(period_h))
            contrib = float(precip_mm) * (covered_h / float(period_h))

        cumulative += contrib

        rows.append(
            Row(
                time_utc=t,
                temp_c=to_float_or_none(instant.get("air_temperature")),
                rh_pct=to_float_or_none(instant.get("relative_humidity")),
                wind_mps=to_float_or_none(instant.get("wind_speed")),
                wind_deg=to_float_or_none(instant.get("wind_from_direction")),
                precip_mm_period=precip_mm,
                period_hours=period_h,
                period_source=source,
                precip_mm_contrib=contrib,
                precip_mm_cum=cumulative,
                symbol_code=symbol,
                interval_h=interval_h,
            )
        )
    return rows


# ---------- OpenWeatherMap ----------
def fetch_owm_forecast(session: requests.Session, lat: float, lon: float) -> Dict[str, Any]:
    params = {
        "lat": f"{lat:.4f}",
        "lon": f"{lon:.4f}",
        "appid": OWM_APPID,
    }
    resp = session.get(OWM_API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(f"OWM HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def extract_rows_owm_with_cumulative(
    forecast: Dict[str, Any], now_utc: datetime, horizon_h: int = 72
) -> List[Row]:
    end_utc = now_utc + timedelta(hours=horizon_h)
    lst = forecast.get("list", [])
    if not isinstance(lst, list):
        raise RuntimeError("Invalid OWM payload: 'list' missing or not a list.")

    parsed_items: List[Tuple[datetime, Dict[str, Any]]] = []
    for item in lst:
        if not isinstance(item, dict):
            continue
        dt_unix = item.get("dt")
        if not isinstance(dt_unix, (int, float)):
            continue
        t = datetime.fromtimestamp(float(dt_unix), tz=timezone.utc)
        parsed_items.append((t, item))

    parsed_items.sort(key=lambda x: x[0])
    rows: List[Row] = []
    cumulative = 0.0

    for i, (t, item) in enumerate(parsed_items):
        if t < now_utc or t >= end_utc:
            continue

        next_t = parsed_items[i + 1][0] if (i + 1) < len(parsed_items) else end_utc
        interval_end = min(next_t, end_utc)
        interval_h = max(0.0, (interval_end - t).total_seconds() / 3600.0)
        if interval_h <= 0.0:
            continue

        main = item.get("main", {}) or {}
        wind = item.get("wind", {}) or {}
        rain = item.get("rain", {}) or {}
        weather_list = item.get("weather", []) or []
        w0 = weather_list[0] if weather_list and isinstance(weather_list[0], dict) else {}

        precip_3h = to_float_or_none(rain.get("3h"))
        if precip_3h is None:
            precip_3h = 0.0

        period_h = 3
        covered_h = min(interval_h, float(period_h))
        contrib = float(precip_3h) * (covered_h / float(period_h))
        cumulative += contrib

        rows.append(
            Row(
                time_utc=t,
                temp_c=k_to_c(main.get("temp")),
                rh_pct=to_float_or_none(main.get("humidity")),
                wind_mps=to_float_or_none(wind.get("speed")),
                wind_deg=to_float_or_none(wind.get("deg")),
                precip_mm_period=precip_3h,
                period_hours=3,
                period_source="owm_3h",
                precip_mm_contrib=contrib,
                precip_mm_cum=cumulative,
                symbol_code=str(w0.get("description") or w0.get("icon") or ""),
                interval_h=interval_h,
            )
        )
    return rows


# ---------- Open-Meteo ----------
def fetch_open_meteo_forecast(session: requests.Session, lat: float, lon: float) -> Dict[str, Any]:
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "wind_speed_unit": "ms",  # default is km/h; match Yr/OWM's m/s
        "timezone": "UTC",        # so hourly.time strings can be parsed as naive UTC
        "forecast_days": OPEN_METEO_FORECAST_DAYS,
        "models": "cma_grapes_global",
    }
    resp = session.get(OPEN_METEO_API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(f"Open-Meteo HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


WMO_CODE_TEXT: Dict[int, str] = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "dense freezing rain",
    71: "slight snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "slight showers", 81: "showers", 82: "violent showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ heavy hail",
}


def wmo_code_text(code: Any) -> str:
    try:
        return WMO_CODE_TEXT.get(int(code), f"code {int(code)}")
    except Exception:
        return ""


def extract_rows_open_meteo_with_cumulative(
    forecast: Dict[str, Any], now_utc: datetime, horizon_h: int = 72
) -> List[Row]:
    end_utc = now_utc + timedelta(hours=horizon_h)
    hourly = forecast.get("hourly", {}) or {}
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    rhs = hourly.get("relative_humidity_2m", [])
    precs = hourly.get("precipitation", [])
    winds = hourly.get("wind_speed_10m", [])
    codes = hourly.get("weather_code", [])
    if not isinstance(times, list):
        raise RuntimeError("Invalid Open-Meteo payload: hourly.time missing or not a list.")

    parsed_items: List[Tuple[datetime, int]] = []
    for i, ts in enumerate(times):
        if not isinstance(ts, str):
            continue
        try:
            t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        parsed_items.append((t, i))

    parsed_items.sort(key=lambda x: x[0])
    rows: List[Row] = []
    cumulative = 0.0

    for j, (t, i) in enumerate(parsed_items):
        if t < now_utc or t >= end_utc:
            continue

        next_t = parsed_items[j + 1][0] if (j + 1) < len(parsed_items) else end_utc
        interval_end = min(next_t, end_utc)
        interval_h = max(0.0, (interval_end - t).total_seconds() / 3600.0)
        if interval_h <= 0.0:
            continue

        precip_1h = to_float_or_none(precs[i]) if i < len(precs) else None
        if precip_1h is None:
            precip_1h = 0.0

        period_h = 1
        covered_h = min(interval_h, float(period_h))
        contrib = float(precip_1h) * (covered_h / float(period_h))
        cumulative += contrib

        rows.append(
            Row(
                time_utc=t,
                temp_c=to_float_or_none(temps[i]) if i < len(temps) else None,
                rh_pct=to_float_or_none(rhs[i]) if i < len(rhs) else None,
                wind_mps=to_float_or_none(winds[i]) if i < len(winds) else None,
                wind_deg=None,
                precip_mm_period=precip_1h,
                period_hours=period_h,
                period_source="open_meteo_1h",
                precip_mm_contrib=contrib,
                precip_mm_cum=cumulative,
                symbol_code=wmo_code_text(codes[i]) if i < len(codes) else "",
                interval_h=interval_h,
            )
        )
    return rows


# ---------- Irrigation ----------
def rain_windows(rows: List[Row], now_utc: datetime) -> Tuple[float, float, float]:
    r12 = 0.0
    r24 = 0.0
    for r in rows:
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < 0:
            continue
        if h < 12:
            r12 += r.precip_mm_contrib
        if h < 24:
            r24 += r.precip_mm_contrib
    r72 = rows[-1].precip_mm_cum if rows else 0.0
    return r12, r24, r72


def rain_windows_3days(rows: List[Row], now_utc: datetime) -> Tuple[float, float, float]:
    r0_24 = 0.0
    r24_48 = 0.0
    r48_72 = 0.0
    for r in rows:
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < 0:
            continue
        if h < 24.0:
            r0_24 += r.precip_mm_contrib
        elif h < 48.0:
            r24_48 += r.precip_mm_contrib
        elif h < 72.0:
            r48_72 += r.precip_mm_contrib
    return r0_24, r24_48, r48_72


def daylength_hours(lat_deg: float, day_of_year: int) -> float:
    lat = math.radians(lat_deg)
    decl = math.radians(23.44) * math.sin(2.0 * math.pi * (284 + day_of_year) / 365.0)
    x = -math.tan(lat) * math.tan(decl)
    x = clamp(x, -1.0, 1.0)
    h0 = math.acos(x)
    return 24.0 * h0 / math.pi


def sat_vapor_pressure_kpa(temp_c: float) -> float:
    return 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))


def vpd_kpa(temp_c: float, rh_pct: float) -> float:
    es = sat_vapor_pressure_kpa(temp_c)
    ea = es * clamp(rh_pct, 0.0, 100.0) / 100.0
    return max(0.0, es - ea)


def climate_demand_window(
    rows: List[Row],
    now_utc: datetime,
    anchor_local: datetime,
    lat_deg: float,
    start_h: float,
    end_h: float,
) -> Tuple[float, Dict[str, float]]:
    t_win: List[float] = []
    rh_win: List[float] = []
    w_win: List[float] = []
    t_first12: List[float] = []

    for r in rows:
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < start_h or h >= end_h:
            continue
        if r.temp_c is not None:
            t_win.append(r.temp_c)
        if r.rh_pct is not None:
            rh_win.append(r.rh_pct)
        if r.wind_mps is not None:
            w_win.append(r.wind_mps)
        if (h - start_h) < 12.0 and r.temp_c is not None:
            t_first12.append(r.temp_c)

    t_mean = mean(t_win) if t_win else 12.0
    rh_mean = mean(rh_win) if rh_win else 70.0
    w_mean = mean(w_win) if w_win else 1.2
    t_min12 = min(t_first12) if t_first12 else t_mean

    vpd = vpd_kpa(t_mean, rh_mean)
    mid_local = anchor_local + timedelta(hours=0.5 * (start_h + end_h))
    doy = mid_local.timetuple().tm_yday
    daylen_h = daylength_hours(lat_deg, doy)

    solar_term = clamp((daylen_h - 10.0) / 4.5, 0.0, 1.0)
    temp_term = clamp((t_mean - 2.0) / 28.0, 0.0, 1.2)
    vpd_term = clamp(vpd / 1.8, 0.0, 1.5)
    wind_term = clamp(w_mean / 7.0, 0.0, 1.0)

    demand_mm = (
        0.35
        + 2.40 * solar_term
        + 1.80 * temp_term
        + 1.60 * vpd_term
        + 0.60 * wind_term
    )

    frost_factor = 1.0
    if t_min12 <= 1.5:
        frost_factor = 0.35
    elif t_min12 <= 4.0:
        frost_factor = 0.60

    demand_mm *= frost_factor
    demand_mm = clamp(demand_mm, 0.20, 8.00)

    dbg = {
        "demand_mm_24h": float(demand_mm),
        "t_mean_24h_c": float(t_mean),
        "rh_mean_24h_pct": float(rh_mean),
        "wind_mean_24h_mps": float(w_mean),
        "t_min_12h_c": float(t_min12),
        "vpd_kpa": float(vpd),
        "day_of_year": float(doy),
        "daylength_h": float(daylen_h),
        "solar_term": float(solar_term),
        "temp_term": float(temp_term),
        "vpd_term": float(vpd_term),
        "wind_term": float(wind_term),
        "frost_factor": float(frost_factor),
        "start_h": float(start_h),
        "end_h": float(end_h),
    }
    return demand_mm, dbg


# ---------- METAR (aviationweather.gov) recent-rain ground truth ----------
# ZSNJ/ZSSS report plain international METARs with no US-style RMK precip
# accumulation group, so there's no direct mm reading -- but there IS a real
# present-weather code (wxString) at each observation, which is actual
# observed fact, not a forecast. We treat whatever precip code was reported
# at an observation as persisting until the next observation, convert it to
# an approximate mm/hour rate via intensity qualifier, and integrate over
# time. This is a coarse estimate (no gauge-accurate amounts exist for these
# stations) but it's grounded in what was actually observed at the airport,
# unlike the old approach of banking a forecast and reading it back later.
METAR_API_URL = "https://aviationweather.gov/api/data/metar"
# Static station registry (site name/state/country/lat/lon), independent of
# live observations -- used by resolve_station_geo() so a station can be
# located even when it currently has zero METAR reports (see that function's
# docstring).
STATION_INFO_API_URL = "https://aviationweather.gov/api/data/stationinfo"
METAR_RAIN_LOOKBACK_HOURS = 24.0

# Intensity-qualifier -> approximate rate. '-' light, '' moderate, '+' heavy.
# VC (vicinity, i.e. not falling at the station itself) is excluded entirely.
_METAR_INTENSITY_RATE_MM_H = {"-": 1.0, "": 4.0, "+": 10.0}
_METAR_PRECIP_CODES = ("DZ", "RA", "SN", "SG", "IC", "PL", "GR", "GS", "UP")


def fetch_metar_history(session: requests.Session, station_id: str, hours: float) -> List[Dict[str, Any]]:
    params = {"ids": station_id, "format": "json", "hours": str(int(math.ceil(hours)))}
    resp = session.get(METAR_API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(f"aviationweather.gov METAR HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    return data if isinstance(data, list) else []


def metar_wx_rate_mm_per_hour(wx_string: Optional[str]) -> float:
    """Peak precip rate (mm/h) implied by a METAR wxString, or 0.0 if none of
    its groups are actual falling precipitation at the station."""
    if not wx_string:
        return 0.0
    rate = 0.0
    for group in wx_string.split():
        if group.startswith("VC"):
            continue  # in the vicinity, not at the station
        intensity = ""
        if group and group[0] in "-+":
            intensity, group = group[0], group[1:]
        if any(code in group for code in _METAR_PRECIP_CODES):
            rate = max(rate, _METAR_INTENSITY_RATE_MM_H.get(intensity, _METAR_INTENSITY_RATE_MM_H[""]))
    return rate


# aviationweather.gov's JSON API reports wspd in knots regardless of the
# units the raw METAR text uses (rawOb often says "...MPS" for these two
# Chinese stations, but the parsed wspd field is knots) -- confirmed by
# cross-checking rawOb groups against wspd across many observations.
_KT_TO_MPS = 0.514444


def metar_latest_observation(
    observations: List[Dict[str, Any]], now_utc: datetime
) -> Optional[Dict[str, Any]]:
    """Ground-truth current conditions: the single most recent METAR
    observation, not an average across forecast sources. Returns None if
    there are no dated observations to pick from."""
    dated = [o for o in observations if o.get("obsTime") is not None]
    if not dated:
        return None
    latest = max(dated, key=lambda o: o["obsTime"])

    temp_c = latest.get("temp")
    dewp_c = latest.get("dewp")
    rh_pct: Optional[float] = None
    vpd: Optional[float] = None
    if temp_c is not None and dewp_c is not None:
        es = sat_vapor_pressure_kpa(float(temp_c))
        ea = sat_vapor_pressure_kpa(float(dewp_c))
        rh_pct = clamp(100.0 * ea / es, 0.0, 100.0) if es > 0 else None
        vpd = max(0.0, es - ea)

    wspd_kt = latest.get("wspd")
    wind_mps = wspd_kt * _KT_TO_MPS if isinstance(wspd_kt, (int, float)) else None

    obs_epoch = float(latest["obsTime"])
    return {
        "station": latest.get("icaoId"),
        "station_name": latest.get("name"),
        "obs_time_epoch": obs_epoch,
        "age_minutes": max(0.0, (now_utc.timestamp() - obs_epoch) / 60.0),
        "temp_c": temp_c,
        "dewpoint_c": dewp_c,
        "rh_pct": rh_pct,
        "wind_mps": wind_mps,
        "wind_dir_deg": latest.get("wdir"),
        "pressure_hpa": latest.get("altim"),
        "vpd_kpa": vpd,
        "raw_ob": latest.get("rawOb"),
    }


def metar_accumulated_rain_mm(
    observations: List[Dict[str, Any]], now_utc: datetime, lookback_hours: float = METAR_RAIN_LOOKBACK_HOURS
) -> float:
    """Integrate each observation's implied rain rate over the time until the
    next observation (or until now_utc, for the most recent one), summed over
    the lookback window. observations need not be sorted."""
    now_epoch = now_utc.timestamp()
    cutoff = now_epoch - lookback_hours * 3600.0
    obs = sorted(
        (o for o in observations if o.get("obsTime") is not None),
        key=lambda o: o["obsTime"],
    )
    total = 0.0
    for i, o in enumerate(obs):
        start = float(o["obsTime"])
        end = float(obs[i + 1]["obsTime"]) if i + 1 < len(obs) else now_epoch
        end = min(end, now_epoch)
        start = max(start, cutoff)
        if end <= start:
            continue
        rate = metar_wx_rate_mm_per_hour(o.get("wxString"))
        if rate > 0:
            total += rate * (end - start) / 3600.0
    return total


def future_credit_multiplier(
    future_rain_eff_mm: float,
    future_need_mm: float,
    min_multiplier: float = FUTURE_CREDIT_MIN_MULTIPLIER,
) -> Tuple[float, float]:
    eps = 1e-6
    cov = clamp(future_rain_eff_mm / max(future_need_mm, eps), 0.0, 1.0)
    m = 1.0 - (1.0 - clamp(min_multiplier, 0.0, 1.0)) * cov
    return m, cov


def decision_percent_dynamic(
    r12_mm: float, r24_mm: float, demand_mm_24h: float, exposure: float = 0.8
) -> Tuple[int, Dict[str, float]]:
    r12_eff = r12_mm * exposure
    r24_eff = r24_mm * exposure
    rain_index = (W24 * r24_eff) + (W12 * r12_eff)

    start_reduce = 0.20 * demand_mm_24h + 0.15
    full_skip = 1.15 * demand_mm_24h + 0.25
    if full_skip <= start_reduce:
        full_skip = start_reduce + 0.5

    if rain_index <= start_reduce:
        pct = 100.0
    elif rain_index >= full_skip:
        pct = 0.0
    else:
        pct = 100.0 * (full_skip - rain_index) / (full_skip - start_reduce)

    pct_int = int(round(clamp(pct, 0.0, 100.0)))
    dbg = {
        "r12_eff_mm": r12_eff,
        "r24_eff_mm": r24_eff,
        "rain_index_mm": rain_index,
        "demand_mm_24h": demand_mm_24h,
        "start_reduce_mm": start_reduce,
        "full_skip_mm": full_skip,
    }
    return pct_int, dbg


def label_from_percent(p: int) -> str:
    if p <= 0:
        return "SKIP"
    if p >= 100:
        return "NORMAL"
    if p < 50:
        return "LIGHT"
    return "REDUCED"


def pump_seconds_from_percent(
    percent: int,
    baseline_seconds_normal: int = BASELINE_PUMP_SECONDS_NORMAL,
    min_seconds_if_running: int = MIN_PUMP_SECONDS_IF_RUNNING,
) -> int:
    p = int(clamp(float(percent), 0.0, 100.0))
    if p <= 0:
        return 0
    sec = int(round(baseline_seconds_normal * (p / 100.0)))
    if 0 < sec < min_seconds_if_running:
        sec = min_seconds_if_running
    return sec


def compute_source_metrics(
    rows: List[Row], now_utc: datetime, now_local: datetime, lat_deg: float
) -> Dict[str, Any]:
    r12, r24, r72 = rain_windows(rows, now_utc)
    _r0_24, r24_48, r48_72 = rain_windows_3days(rows, now_utc)

    demand0, cdbg0 = climate_demand_window(rows, now_utc, now_local, lat_deg, 0.0, 24.0)
    demand1, cdbg1 = climate_demand_window(rows, now_utc, now_local, lat_deg, 24.0, 48.0)
    demand2, cdbg2 = climate_demand_window(rows, now_utc, now_local, lat_deg, 48.0, 72.0)

    return {
        "rows": rows,
        "r12": r12,
        "r24": r24,
        "r72": r72,
        "r24_48": r24_48,
        "r48_72": r48_72,
        "demand0": demand0,
        "demand1": demand1,
        "demand2": demand2,
        "cdbg": cdbg0,
        "cdbg1": cdbg1,
        "cdbg2": cdbg2,
    }


def summarize(
    label: str,
    pct: int,
    pump_seconds: int,
    r0_24_raw: float,
    r0_24_eff: float,
    r24_48_raw: float,
    r24_48_eff: float,
    r48_72_raw: float,
    r48_72_eff: float,
    demand_mm_24h: float,
    rain_index: float,
    start_reduce: float,
    full_skip: float,
) -> str:
    if pct <= 0:
        action = "SKIP IRRIGATION"
    elif pct >= 100:
        action = "NORMAL IRRIGATION"
    else:
        action = f"{label} TO {pct}% IRRIGATION"

    net_need = max(0.0, demand_mm_24h - r0_24_eff)
    rule = f"rain_index {rain_index:.2f} between {start_reduce:.2f} and {full_skip:.2f}"

    return (
        f"{action} ({pump_seconds}s): "
        f"rain 0–24h = {r0_24_raw:.2f} mm (eff {r0_24_eff:.2f}); "
        f"rain 24–48h = {r24_48_raw:.2f} mm (eff {r24_48_eff:.2f}); "
        f"rain 48–72h = {r48_72_raw:.2f} mm (eff {r48_72_eff:.2f}); "
        f"demand 0–24h = {demand_mm_24h:.2f} mm; "
        f"net need ≈ {net_need:.2f} mm ({rule})."
    )


# ---------- Comparison table ----------
#
# Yr.no's timeseries interval widens from 1h (near-term) to 3h/6h/12h (later
# forecast). Earlier this window-sum treated each row's rain total as a point
# mass sitting at its own timestamp, so whichever output slot happened to
# contain that timestamp got the *whole* block's rain and neighbouring slots
# got exactly zero — the coarse block's rain never actually reached the slots
# it was supposed to cover. These helpers instead spread (interpolate) each
# row's contribution across the real interval it represents (row.interval_h),
# by time-overlap with the requested window, and flag any window touched by
# a >1h-native row as interpolated so the UI can hatch it.
NATIVE_RESOLUTION_H = 1.0  # Yr.no's finest reporting interval


def precip_sum_in_window(rows: List[Row], start_utc: datetime, hours: float) -> float:
    end_utc = start_utc + timedelta(hours=hours)
    total = 0.0
    for r in rows:
        if r.interval_h <= 0:
            continue
        r_end = r.time_utc + timedelta(hours=r.interval_h)
        overlap_h = (min(end_utc, r_end) - max(start_utc, r.time_utc)).total_seconds() / 3600.0
        if overlap_h <= 0:
            continue
        total += float(r.precip_mm_contrib or 0.0) * (overlap_h / r.interval_h)
    return total


def is_coarse_in_window(rows: List[Row], start_utc: datetime, hours: float) -> bool:
    """True if rain/values in [start, start+hours) were spread from a >1h-native Yr.no row."""
    end_utc = start_utc + timedelta(hours=hours)
    for r in rows:
        if r.interval_h <= NATIVE_RESOLUTION_H + 1e-6:
            continue
        r_end = r.time_utc + timedelta(hours=r.interval_h)
        if max(start_utc, r.time_utc) < min(end_utc, r_end):
            return True
    return False


def rain_block_id(rows: List[Row], start_utc: datetime, hours: float) -> Optional[str]:
    """Identifies which single >1h-native Yr.no row this window's rain was mostly
    spread from (by time-overlap). Windows sharing a block id came from splitting
    the same coarse reading, so the UI can group them into one rectangle instead
    of guessing from the (possibly coincidentally equal) redistributed values."""
    end_utc = start_utc + timedelta(hours=hours)
    best: Optional[Row] = None
    best_overlap = 0.0
    for r in rows:
        if r.interval_h <= NATIVE_RESOLUTION_H + 1e-6:
            continue
        r_end = r.time_utc + timedelta(hours=r.interval_h)
        overlap = (min(end_utc, r_end) - max(start_utc, r.time_utc)).total_seconds()
        if overlap > best_overlap:
            best_overlap = overlap
            best = r
    return best.time_utc.isoformat() if best is not None else None


def interp_field_at(rows: List[Row], t_utc: datetime, field: str) -> Tuple[Optional[float], bool]:
    """Value of `field` at t_utc: exact/near row if close, else linear interpolation
    between the bracketing rows. Returns (value, is_interpolated)."""
    before: Optional[Row] = None
    after: Optional[Row] = None
    for r in rows:
        v = getattr(r, field)
        if v is None:
            continue
        if r.time_utc <= t_utc and (before is None or r.time_utc > before.time_utc):
            before = r
        if r.time_utc >= t_utc and (after is None or r.time_utc < after.time_utc):
            after = r

    if before is not None and abs((before.time_utc - t_utc).total_seconds()) < 60:
        return float(getattr(before, field)), False
    if before is None and after is None:
        return None, False
    if before is None:
        gap_h = (after.time_utc - t_utc).total_seconds() / 3600.0
        return float(getattr(after, field)), gap_h > NATIVE_RESOLUTION_H
    if after is None:
        gap_h = (t_utc - before.time_utc).total_seconds() / 3600.0
        return float(getattr(before, field)), gap_h > NATIVE_RESOLUTION_H

    span_s = (after.time_utc - before.time_utc).total_seconds()
    if span_s <= 0:
        return float(getattr(before, field)), False
    frac = (t_utc - before.time_utc).total_seconds() / span_s
    v0, v1 = float(getattr(before, field)), float(getattr(after, field))
    value = v0 + frac * (v1 - v0)
    is_interp = span_s / 3600.0 > NATIVE_RESOLUTION_H + 1e-6
    return value, is_interp


def nearest_row(rows: List[Row], t_utc: datetime, max_delta_hours: float = 1.5) -> Optional[Row]:
    best = None
    best_dt = None
    for r in rows:
        d = abs((r.time_utc - t_utc).total_seconds())
        if best_dt is None or d < best_dt:
            best_dt = d
            best = r
    if best is None or best_dt is None:
        return None
    if best_dt > max_delta_hours * 3600.0:
        return None
    return best


def build_comparison_rows(
    owm_rows: List[Row],
    yr_rows: List[Row],
    om_rows: List[Row],
    tz_name: str,
) -> List[Dict[str, Any]]:
    tz = get_tz(tz_name)
    out: List[Dict[str, Any]] = []

    for o in owm_rows:
        y = nearest_row(yr_rows, o.time_utc)
        m = nearest_row(om_rows, o.time_utc)

        yr_temp_c, temp_interp = interp_field_at(yr_rows, o.time_utc, "temp_c")
        yr_rh_pct, rh_interp = interp_field_at(yr_rows, o.time_utc, "rh_pct")
        yr_wind_mps, wind_interp = interp_field_at(yr_rows, o.time_utc, "wind_mps")
        rain_interp = is_coarse_in_window(yr_rows, o.time_utc, 3.0)
        rain_block = rain_block_id(yr_rows, o.time_utc, 3.0)

        # Open-Meteo's hourly forecast stays at 1h native resolution across
        # its whole horizon (unlike Yr.no, which widens to 3h/6h/12h further
        # out), so it never needs the coarse-block merge treatment above.
        om_temp_c, om_temp_interp = interp_field_at(om_rows, o.time_utc, "temp_c")
        om_rh_pct, om_rh_interp = interp_field_at(om_rows, o.time_utc, "rh_pct")
        om_wind_mps, om_wind_interp = interp_field_at(om_rows, o.time_utc, "wind_mps")

        out.append({
            "local_time": o.time_utc.astimezone(tz).strftime("%m-%d %H:%M"),
            "epoch": int(o.time_utc.timestamp()),
            "owm_temp_c": o.temp_c,
            "yr_temp_c": yr_temp_c,
            "om_temp_c": om_temp_c,
            "owm_rh_pct": o.rh_pct,
            "yr_rh_pct": yr_rh_pct,
            "om_rh_pct": om_rh_pct,
            "owm_wind_mps": o.wind_mps,
            "yr_wind_mps": yr_wind_mps,
            "om_wind_mps": om_wind_mps,
            "owm_rain_3h_mm": o.precip_mm_period if o.precip_mm_period is not None else 0.0,
            "yr_rain_3h_mm": precip_sum_in_window(yr_rows, o.time_utc, 3.0),
            "om_rain_3h_mm": precip_sum_in_window(om_rows, o.time_utc, 3.0),
            "yr_rain_interpolated": rain_interp,
            "yr_rain_block": rain_block,
            "yr_temp_interpolated": temp_interp,
            "yr_rh_interpolated": rh_interp,
            "yr_wind_interpolated": wind_interp,
            "om_temp_interpolated": om_temp_interp,
            "om_rh_interpolated": om_rh_interp,
            "om_wind_interpolated": om_wind_interp,
            "owm_desc": o.symbol_code,
            "yr_sym": y.symbol_code if y else "",
            "om_desc": m.symbol_code if m else "",
        })
    return out


def overall_outlook_text(r24_mm: float, r72_mm: float, demand0_mm: float, pct: int) -> str:
    if pct <= 0:
        irrig = "skip irrigation"
    elif pct < 100:
        irrig = f"reduce irrigation to {pct}%"
    else:
        irrig = "normal irrigation"

    if r24_mm >= 10.0:
        wx = "wet next 24h"
    elif r24_mm >= 3.0:
        wx = "some rain next 24h"
    elif r72_mm >= 8.0:
        wx = "drier today but wetter later in the 72h window"
    else:
        wx = "mostly light or limited rain"

    return f"{wx}; estimated 0–24h demand {demand0_mm:.2f} mm; recommendation: {irrig}."


def render_report(
    location: LocationInfo,
    now_local: datetime,
    source_mode: str,
    yr_err: Optional[str],
    owm_err: Optional[str],
    om_err: Optional[str],
    yr_m: Optional[Dict[str, Any]],
    owm_m: Optional[Dict[str, Any]],
    om_m: Optional[Dict[str, Any]],
    comparison_rows: List[Dict[str, Any]],
    pct: int,
    label: str,
    pump_seconds: int,
    summary_text: str,
    ensemble: Dict[str, Any],
) -> str:
    lines: List[str] = []

    lines.append("Tour génoise de Capo di Santa Dionisia Irrigation Decision")
    lines.append("=" * 80)
    lines.append(f"{now_local.isoformat(timespec='seconds')}")
    lines.append(f"location_source={location.source_text}")
    lines.append(f"horizon_hours={HOURS_AHEAD}")
    lines.append("")

    sched_obj = ensemble.get("schedule") or {}
    payload = sched_obj.get("json_payload") or {}
    events = payload.get("events") or []

    lines.append("Irrigation schedule")
    lines.append("-" * 80)
    lines.append(f"{summary_text}")
    lines.append("")
    if events:
        lines.append("Next events (sent to the pumps):")
        for ev in events:
            lines.append(
                f"  event#{ev['sequence']}  {ev['iso_local']} ({ev['solar_anchor']})"
                f"  ->  {ev['percent']}% / {ev['pump_seconds']}s   [{ev['percent_basis']}]"
            )
        lines.append("")
    if payload:
        lines.append("Raw payload (-> next_watering.json + MQTT):")
        lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
        lines.append("")

    schedule = ensemble.get("schedule") or {}
    if schedule:
        lines.append("Schedule")
        lines.append("-" * 80)
        lines.append(f"temperature_mode={schedule.get('temperature_mode')}")
        lines.append(f"target_interval_days={fmt_num(schedule.get('target_interval_days'), 3)}")
        lines.append(f"due_now={1 if schedule.get('due_now') else 0}")
        lines.append(f"forecast_tmin24_c={fmt_num(schedule.get('tmin24_c'), 3)}")
        lines.append(f"forecast_tmean72_c={fmt_num(schedule.get('tmean72_c'), 3)}")
        lines.append(f"forecast_tmax24_c={fmt_num(schedule.get('tmax24_c'), 3)}")
        lines.append(f"forecast_precip_local_day_mm={fmt_num(schedule.get('forecast_precip_local_day_mm'), 3)}")
        lines.append(f"forecast_precip_next24_mm={fmt_num(schedule.get('forecast_precip_next24_mm'), 3)}")
        lines.append(f"effective_rain_today_mm={fmt_num(schedule.get('effective_rain_today_mm'), 3)}")
        lines.append(f"interval_demand_mm={fmt_num(schedule.get('interval_demand_mm'), 3)}")
        lines.append(f"rain_postpone_threshold_mm={fmt_num(schedule.get('rain_postpone_threshold_mm'), 3)}")
        lines.append(f"rain_reset_threshold_mm={fmt_num(schedule.get('rain_reset_threshold_mm'), 3)}")
        lines.append(f"next_watering_epoch={schedule.get('next_watering_epoch')}")
        lines.append(f"next_watering_iso_local={schedule.get('next_watering_iso_local')}")
        lines.append(f"projected_following_epoch={schedule.get('projected_following_epoch')}")
        lines.append(f"projected_following_iso_local={schedule.get('projected_following_iso_local')}")
        lines.append(f"schedule_armed={1 if schedule.get('schedule_armed') else 0}")
        lines.append(f"db_path={schedule.get('db_path')}")
        lines.append(f"json_path={schedule.get('json_path')}")
        lines.append("")

    lines.append("Forecast summary")
    lines.append("-" * 80)
    lines.append(f"source_mode={source_mode}")
    lines.append(f"overall_outlook={ensemble['overall_outlook']}")
    lines.append(f"yr_ok={1 if yr_err is None else 0}")
    lines.append(f"owm_ok={1 if owm_err is None else 0}")
    lines.append(f"om_ok={1 if om_err is None else 0}")
    if yr_err:
        lines.append(f"yr_error={yr_err}")
    if owm_err:
        lines.append(f"owm_error={owm_err}")
    if om_err:
        lines.append(f"om_error={om_err}")
    lines.append(f"ensemble_r12_raw_mm={fmt_num(ensemble['r12'], 3)}")
    lines.append(f"ensemble_r24_raw_mm={fmt_num(ensemble['r24'], 3)}")
    lines.append(f"ensemble_r72_raw_mm={fmt_num(ensemble['r72'], 3)}")
    lines.append(f"ensemble_r24_48_raw_mm={fmt_num(ensemble['r24_48'], 3)}")
    lines.append(f"ensemble_r48_72_raw_mm={fmt_num(ensemble['r48_72'], 3)}")
    lines.append(f"ensemble_demand0_0_24_mm={fmt_num(ensemble['demand0'], 3)}")
    lines.append(f"ensemble_demand1_24_48_mm={fmt_num(ensemble['demand1'], 3)}")
    lines.append(f"ensemble_demand2_48_72_mm={fmt_num(ensemble['demand2'], 3)}")
    lines.append(f"future_cover_ratio_0_1={fmt_num(ensemble['future_cover_ratio'], 3)}")
    lines.append(f"future_multiplier={fmt_num(ensemble['future_multiplier'], 3)}")
    lines.append(f"pct_base_0_24={ensemble['pct_base']}")
    lines.append(f"pct_final={pct}")
    lines.append("")

    lines.append("Climate debug (decision window 0–24h)")
    lines.append("-" * 80)
    cdbg = ensemble["cdbg"]
    dbg = ensemble["dbg"]
    lines.append(f"exposure_factor={fmt_num(EXPOSURE_FACTOR, 3)}")
    lines.append(f"rain_index_mm={fmt_num(dbg['rain_index_mm'], 3)}")
    lines.append(f"start_reduce_mm={fmt_num(dbg['start_reduce_mm'], 3)}")
    lines.append(f"full_skip_mm={fmt_num(dbg['full_skip_mm'], 3)}")
    lines.append(f"r12_eff_mm={fmt_num(dbg['r12_eff_mm'], 3)}")
    lines.append(f"r24_eff_mm={fmt_num(dbg['r24_eff_mm'], 3)}")
    lines.append(f"demand_mm_24h={fmt_num(cdbg['demand_mm_24h'], 3)}")
    lines.append(f"t_mean_24h_c={fmt_num(cdbg['t_mean_24h_c'], 3)}")
    lines.append(f"rh_mean_24h_pct={fmt_num(cdbg['rh_mean_24h_pct'], 3)}")
    lines.append(f"wind_mean_24h_mps={fmt_num(cdbg['wind_mean_24h_mps'], 3)}")
    lines.append(f"t_min_12h_c={fmt_num(cdbg['t_min_12h_c'], 3)}")
    lines.append(f"vpd_kpa={fmt_num(cdbg['vpd_kpa'], 3)}")
    lines.append(f"daylength_h={fmt_num(cdbg['daylength_h'], 3)}")
    lines.append(f"solar_term={fmt_num(cdbg['solar_term'], 3)}")
    lines.append(f"temp_term={fmt_num(cdbg['temp_term'], 3)}")
    lines.append(f"vpd_term={fmt_num(cdbg['vpd_term'], 3)}")
    lines.append(f"wind_term={fmt_num(cdbg['wind_term'], 3)}")
    lines.append(f"frost_factor={fmt_num(cdbg['frost_factor'], 3)}")
    lines.append(f"baseline_pump_seconds_normal={BASELINE_PUMP_SECONDS_NORMAL}")
    lines.append(f"min_pump_seconds_if_running={MIN_PUMP_SECONDS_IF_RUNNING}")
    lines.append("")

    if yr_m is not None:
        lines.append("Yr.no metrics")
        lines.append("-" * 80)
        lines.append(f"yr_r12_mm={fmt_num(yr_m['r12'], 3)}")
        lines.append(f"yr_r24_mm={fmt_num(yr_m['r24'], 3)}")
        lines.append(f"yr_r72_mm={fmt_num(yr_m['r72'], 3)}")
        lines.append(f"yr_demand0_0_24_mm={fmt_num(yr_m['demand0'], 3)}")
        lines.append(f"yr_demand1_24_48_mm={fmt_num(yr_m['demand1'], 3)}")
        lines.append(f"yr_demand2_48_72_mm={fmt_num(yr_m['demand2'], 3)}")
        lines.append("")

    if owm_m is not None:
        lines.append("OpenWeatherMap metrics")
        lines.append("-" * 80)
        lines.append(f"owm_r12_mm={fmt_num(owm_m['r12'], 3)}")
        lines.append(f"owm_r24_mm={fmt_num(owm_m['r24'], 3)}")
        lines.append(f"owm_r72_mm={fmt_num(owm_m['r72'], 3)}")
        lines.append(f"owm_demand0_0_24_mm={fmt_num(owm_m['demand0'], 3)}")
        lines.append(f"owm_demand1_24_48_mm={fmt_num(owm_m['demand1'], 3)}")
        lines.append(f"owm_demand2_48_72_mm={fmt_num(owm_m['demand2'], 3)}")
        lines.append("")

    if om_m is not None:
        lines.append("Open-Meteo metrics")
        lines.append("-" * 80)
        lines.append(f"om_r12_mm={fmt_num(om_m['r12'], 3)}")
        lines.append(f"om_r24_mm={fmt_num(om_m['r24'], 3)}")
        lines.append(f"om_r72_mm={fmt_num(om_m['r72'], 3)}")
        lines.append(f"om_demand0_0_24_mm={fmt_num(om_m['demand0'], 3)}")
        lines.append(f"om_demand1_24_48_mm={fmt_num(om_m['demand1'], 3)}")
        lines.append(f"om_demand2_48_72_mm={fmt_num(om_m['demand2'], 3)}")
        lines.append("")

    lines.append("OWM vs Yr comparison (3-hour slots)")
    lines.append("-" * 80)
    headers = ["time_local", "OWM_T", "YR_T", "OWM_RH", "YR_RH", "OWM_W", "YR_W", "OWM_R3h", "YR_R3h", "OWM_desc", "YR_sym"]
    table: List[List[str]] = []
    for r in comparison_rows:
        table.append([
            r["local_time"],
            fmt_num(r["owm_temp_c"], 1),
            fmt_num(r["yr_temp_c"], 1),
            fmt_num(r["owm_rh_pct"], 0),
            fmt_num(r["yr_rh_pct"], 0),
            fmt_num(r["owm_wind_mps"], 1),
            fmt_num(r["yr_wind_mps"], 1),
            fmt_num(r["owm_rain_3h_mm"], 1),
            fmt_num(r["yr_rain_3h_mm"], 1),
            str(r["owm_desc"] or ""),
            str(r["yr_sym"] or ""),
        ])

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: List[str]) -> str:
        return " | ".join(cells[i].ljust(widths[i]) for i in range(len(cells)))

    sep = "-+-".join("-" * w for w in widths)
    lines.append(line(headers))
    lines.append(sep)
    for row in table:
        lines.append(line(row))

    return "\n".join(lines) + "\n"


def atomic_write_text(path: str, text: str) -> None:
    out_path = Path(path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(out_path.parent),
        prefix=out_path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name

    os.replace(tmp_name, out_path)

def current_conditions_block(
    current_obs: Optional[Dict[str, Any]], tz_name: str
) -> Optional[Dict[str, Any]]:
    """Shared by the full report build and the hourly current-only refresh
    (run_current_only) so both write the exact same shape into
    weather_cache.json's "current" key."""
    if current_obs is None:
        return None
    return {
        "source": "METAR",
        "station": current_obs["station"],
        "station_name": current_obs["station_name"],
        "obs_time_epoch": current_obs["obs_time_epoch"],
        "obs_time_local": datetime.fromtimestamp(
            current_obs["obs_time_epoch"], tz=get_tz(tz_name)
        ).isoformat(timespec="minutes"),
        "age_minutes": current_obs["age_minutes"],
        "temp_c": current_obs["temp_c"],
        "dewpoint_c": current_obs["dewpoint_c"],
        "rh_pct": current_obs["rh_pct"],
        "wind_mps": current_obs["wind_mps"],
        "wind_dir_deg": current_obs["wind_dir_deg"],
        "pressure_hpa": current_obs["pressure_hpa"],
        "vpd_kpa": current_obs["vpd_kpa"],
        "raw_ob": current_obs["raw_ob"],
    }


def append_metar_log_row(current_block: Optional[Dict[str, Any]], tz_name: str) -> None:
    """Create one MetarReading row for a successful current-conditions fetch
    (current_block is current_conditions_block()'s output -- None means the
    fetch had nothing to show, e.g. no prior cache to fall back to, so
    there's nothing worth logging). Called once per hourly current-only
    refresh and once per tri-hourly full run, so together they log every
    METAR poll this script actually makes -- unlike weather_cache.json's
    "current" key, which only ever holds the latest reading, this is a real
    time series. Plain create (no upsert/dedup): a stale-fallback reading
    logged twice in a row just honestly shows the poll happened but nothing
    new arrived from the station."""
    if current_block is None:
        return
    now_local = datetime.now(get_tz(tz_name))
    wind_dir_deg = current_block.get("wind_dir_deg")
    MetarReading.objects.create(
        logged_at_epoch=int(now_local.timestamp()),
        logged_at_local=now_local.isoformat(timespec="seconds"),
        obs_time_epoch=current_block.get("obs_time_epoch"),
        obs_time_local=current_block.get("obs_time_local") or "",
        age_minutes=current_block.get("age_minutes"),
        station=current_block.get("station") or "",
        station_name=current_block.get("station_name") or "",
        temp_c=current_block.get("temp_c"),
        dewpoint_c=current_block.get("dewpoint_c"),
        rh_pct=current_block.get("rh_pct"),
        wind_mps=current_block.get("wind_mps"),
        wind_dir_deg=None if wind_dir_deg is None else str(wind_dir_deg),
        pressure_hpa=current_block.get("pressure_hpa"),
        vpd_kpa=current_block.get("vpd_kpa"),
        raw_ob=current_block.get("raw_ob") or "",
    )


def build_report_object(
    location: LocationInfo,
    now_local: datetime,
    source_mode: str,
    yr_err: Optional[str],
    owm_err: Optional[str],
    om_err: Optional[str],
    metar_err: Optional[str],
    current_obs: Optional[Dict[str, Any]],
    yr_m: Optional[Dict[str, Any]],
    owm_m: Optional[Dict[str, Any]],
    om_m: Optional[Dict[str, Any]],
    comparison_rows: List[Dict[str, Any]],
    ensemble: Dict[str, Any],
    output_path: str,
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "report_type": "garden_weather_irrigation",
        "generated_at": now_local.isoformat(timespec="seconds"),
        "location": {
            "mode": location.mode,
            "lat": location.lat,
            "lon": location.lon,
            "tz": location.tz_name,
            "source_text": location.source_text,
            "station": location.station,
            "station_name": location.station_name,
        },
        "horizon_hours": HOURS_AHEAD,
        "status": {
            "source_mode": source_mode,
            # Based on the live-fetch error, not row presence: a stale-cache
            # fallback still populates rows (so the chart isn't blank and the
            # source still contributes to the ensemble), but the status dot
            # should honestly reflect that the live fetch actually failed.
            "yr_ok": yr_err is None,
            "owm_ok": owm_err is None,
            "om_ok": om_err is None,
            "metar_ok": metar_err is None,
            "yr_error": yr_err,
            "owm_error": owm_err,
            "om_error": om_err,
            "metar_error": metar_err,
        },
        # Current-conditions ground truth: the single latest METAR observation
        # at the station, not an average across the Yr.no/OWM/Open-Meteo
        # forecast sources. Those three remain forecast-only inputs (see
        # "sources" / "ensemble" below); this block is real-world "right now".
        "current": current_conditions_block(current_obs, location.tz_name),
        "irrigation": {
            "decision_percent": ensemble["pct"],
            "decision_label": ensemble["label"],
            "pump_seconds": ensemble["pump_seconds"],
            "overall_outlook": ensemble["overall_outlook"],
            "summary": ensemble["summary_text"],
            "schedule": ensemble.get("schedule"),
        },
        "ensemble": {
            "rain_mm": {
                "r12": ensemble["r12"],
                "r24": ensemble["r24"],
                "r72": ensemble["r72"],
                "r24_48": ensemble["r24_48"],
                "r48_72": ensemble["r48_72"],
            },
            "demand_mm": {
                "d0_24": ensemble["demand0"],
                "d24_48": ensemble["demand1"],
                "d48_72": ensemble["demand2"],
            },
            "future_credit": {
                "cover_ratio_0_1": ensemble["future_cover_ratio"],
                "multiplier": ensemble["future_multiplier"],
            },
            "decision_debug": {
                "pct_base": ensemble["pct_base"],
                "pct_final": ensemble["pct"],
                "exposure_factor": EXPOSURE_FACTOR,
                **ensemble["dbg"],
            },
            "climate_debug": {
                **ensemble["cdbg"],
                "baseline_pump_seconds_normal": BASELINE_PUMP_SECONDS_NORMAL,
                "min_pump_seconds_if_running": MIN_PUMP_SECONDS_IF_RUNNING,
            },
        },
        "sources": {
            "yr": None if yr_m is None else {
                "rain_mm": {
                    "r12": yr_m["r12"],
                    "r24": yr_m["r24"],
                    "r72": yr_m["r72"],
                },
                "demand_mm": {
                    "d0_24": yr_m["demand0"],
                    "d24_48": yr_m["demand1"],
                    "d48_72": yr_m["demand2"],
                },
            },
            "owm": None if owm_m is None else {
                "rain_mm": {
                    "r12": owm_m["r12"],
                    "r24": owm_m["r24"],
                    "r72": owm_m["r72"],
                },
                "demand_mm": {
                    "d0_24": owm_m["demand0"],
                    "d24_48": owm_m["demand1"],
                    "d48_72": owm_m["demand2"],
                },
            },
            "om": None if om_m is None else {
                "rain_mm": {
                    "r12": om_m["r12"],
                    "r24": om_m["r24"],
                    "r72": om_m["r72"],
                },
                "demand_mm": {
                    "d0_24": om_m["demand0"],
                    "d24_48": om_m["demand1"],
                    "d48_72": om_m["demand2"],
                },
            },
        },
        "comparison": {
            "columns": [
                "local_time",
                "owm_temp_c",
                "yr_temp_c",
                "om_temp_c",
                "owm_rh_pct",
                "yr_rh_pct",
                "om_rh_pct",
                "owm_wind_mps",
                "yr_wind_mps",
                "om_wind_mps",
                "owm_rain_3h_mm",
                "yr_rain_3h_mm",
                "om_rain_3h_mm",
                "yr_rain_interpolated",
                "yr_rain_block",
                "yr_temp_interpolated",
                "yr_rh_interpolated",
                "yr_wind_interpolated",
                "om_temp_interpolated",
                "om_rh_interpolated",
                "om_wind_interpolated",
                "owm_desc",
                "yr_sym",
                "om_desc",
            ],
            "rows": comparison_rows,
        },
        "artifacts": {
            "text_report_path": output_path,
            "irrigation_db": (ensemble.get("schedule") or {}).get("db_path"),
            "next_watering_json_path": (ensemble.get("schedule") or {}).get("json_path"),
        },
    }


def _clip_text(s: str, limit: int = 1800) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _comparison_preview_text(rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    del limit  # kept only so the call signature stays compatible

    if not rows:
        return ["No comparison rows."]

    headers = [
        "time_local", "OWM_T", "YR_T", "OWM_RH", "YR_RH",
        "OWM_W", "YR_W", "OWM_R3h", "YR_R3h", "OWM_desc", "YR_sym"
    ]

    table: List[List[str]] = []
    for r in rows:
        table.append([
            str(r["local_time"]),
            fmt_num(r["owm_temp_c"], 1),
            fmt_num(r["yr_temp_c"], 1),
            fmt_num(r["owm_rh_pct"], 0),
            fmt_num(r["yr_rh_pct"], 0),
            fmt_num(r["owm_wind_mps"], 1),
            fmt_num(r["yr_wind_mps"], 1),
            fmt_num(r["owm_rain_3h_mm"], 1),
            fmt_num(r["yr_rain_3h_mm"], 1),
            str(r["owm_desc"] or ""),
            str(r["yr_sym"] or ""),
        ])

    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: List[str]) -> str:
        return " | ".join(cells[i].ljust(widths[i]) for i in range(len(cells)))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    for row in table:
        lines.append(fmt_row(row))

    chunks: List[str] = []
    current: List[str] = []
    current_len = 8  # code fences + slack

    for line in lines:
        add_len = len(line) + 1
        if current and (current_len + add_len) > 1700:
            chunks.append("```" + "\n".join(current) + "```")
            current = [line]
            current_len = 8 + add_len
        else:
            current.append(line)
            current_len += add_len

    if current:
        chunks.append("```" + "\n".join(current) + "```")

    return chunks


def build_openclaw_components(report_obj: Dict[str, Any]) -> Dict[str, Any]:
    loc = report_obj["location"]
    status = report_obj["status"]
    irrigation = report_obj["irrigation"]
    ens = report_obj["ensemble"]
    rain = ens["rain_mm"]
    demand = ens["demand_mm"]
    future = ens["future_credit"]
    ddbg = ens["decision_debug"]
    cdbg = ens["climate_debug"]
    comparison_rows = report_obj["comparison"]["rows"]
    sources = report_obj["sources"]

    blocks: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": "**Irrigation Decision**"
        },
        {
            "type": "text",
            "text": _clip_text(
                f"{report_obj['generated_at']}\n"
                f"lat={loc['lat']:.4f}  lon={loc['lon']:.4f}\n"
                f"tz={loc['tz']}"
            )
        },

        {"type": "separator"},

        {
            "type": "text",
            "text": "**Irrigation decision**"
        },
        {
            "type": "text",
            "text": _clip_text(
                f"{irrigation['decision_label']}\n"
                f"{irrigation['pump_seconds']} seconds\n"
                f"{irrigation['summary']}"
            )
        },

        {"type": "separator"},

        {
            "type": "text",
            "text": "**Forecast summary**"
        },
        {
            "type": "text",
            "text": _clip_text(
                f"overall_outlook={irrigation['overall_outlook']}\n"
                f"r12={fmt_num(rain['r12'], 3)} mm\n"
                f"r24={fmt_num(rain['r24'], 3)} mm\n"
                f"r72={fmt_num(rain['r72'], 3)} mm\n"
                f"demand0={fmt_num(demand['d0_24'], 3)} mm\n"
                f"demand1={fmt_num(demand['d24_48'], 3)} mm\n"
                f"demand2={fmt_num(demand['d48_72'], 3)} mm\n"
                f"future_multiplier={fmt_num(future['multiplier'], 3)}"
            )
        },

        {"type": "separator"},

        {
            "type": "text",
            "text": "**Climate debug (0–24h)**"
        },
        {
            "type": "text",
            "text": _clip_text(
                f"rain_index_mm={fmt_num(ddbg['rain_index_mm'], 3)}\n"
                f"start_reduce_mm={fmt_num(ddbg['start_reduce_mm'], 3)}\n"
                f"full_skip_mm={fmt_num(ddbg['full_skip_mm'], 3)}\n"
                f"demand_mm_24h={fmt_num(cdbg['demand_mm_24h'], 3)}\n"
                f"t_mean_24h_c={fmt_num(cdbg['t_mean_24h_c'], 2)}\n"
                f"rh_mean_24h_pct={fmt_num(cdbg['rh_mean_24h_pct'], 2)}\n"
                f"wind_mean_24h_mps={fmt_num(cdbg['wind_mean_24h_mps'], 2)}\n"
                f"vpd_kpa={fmt_num(cdbg['vpd_kpa'], 3)}"
            )
        },
    ]

    src_lines: List[str] = []
    if sources["yr"] is not None:
        src_lines.append(
            "Yr.no: "
            f"r24={fmt_num(sources['yr']['rain_mm']['r24'], 3)} mm, "
            f"r72={fmt_num(sources['yr']['rain_mm']['r72'], 3)} mm, "
            f"demand24={fmt_num(sources['yr']['demand_mm']['d0_24'], 3)} mm"
        )
    if sources["owm"] is not None:
        src_lines.append(
            "OWM: "
            f"r24={fmt_num(sources['owm']['rain_mm']['r24'], 3)} mm, "
            f"r72={fmt_num(sources['owm']['rain_mm']['r72'], 3)} mm, "
            f"demand24={fmt_num(sources['owm']['demand_mm']['d0_24'], 3)} mm"
        )

    if src_lines:
        blocks.extend([
            {"type": "separator"},
            {"type": "text", "text": "**Source metrics**"},
            {"type": "text", "text": _clip_text("\n".join(src_lines))}
        ])

    '''blocks.extend([
        {"type": "separator"},
        {"type": "text", "text": "**OWM vs Yr comparison (full range)**"}
    ])

    for chunk in _comparison_preview_text(comparison_rows):
        blocks.append({"type": "text", "text": chunk})'''

    return {
        "blocks": blocks,
    }


def send_openclaw_components(components_payload: Dict[str, Any]) -> None:
    cmd = [
        "openclaw", "message", "send",
        "--channel", OPENCLAW_CHANNEL,
        "--target", OPENCLAW_TARGET,
        "--message", OPENCLAW_MESSAGE,
        "--components", json.dumps(components_payload, ensure_ascii=False),
    ]
    if OPENCLAW_SILENT:
        cmd.append("--silent")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"OpenClaw send failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    
def build_ensemble(
    now_utc: datetime,
    now_local: datetime,
    lat: float,
    yr_rows: Optional[List[Row]],
    owm_rows: Optional[List[Row]],
    om_rows: Optional[List[Row]],
    yr_err: Optional[str],
    owm_err: Optional[str],
    om_err: Optional[str],
) -> Tuple[str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    if yr_rows is None and owm_rows is None and om_rows is None:
        raise RuntimeError(
            f"All forecast sources failed. yr_err={yr_err!r}; owm_err={owm_err!r}; om_err={om_err!r}"
        )

    yr_m = compute_source_metrics(yr_rows, now_utc, now_local, lat) if yr_rows is not None else None
    owm_m = compute_source_metrics(owm_rows, now_utc, now_local, lat) if owm_rows is not None else None
    om_m = compute_source_metrics(om_rows, now_utc, now_local, lat) if om_rows is not None else None

    # Generalized N-way merge: average whichever sources are present rather
    # than hardcoding a 2-source split, so adding/losing a source (e.g. an
    # API outage) just changes who's in `active`.
    active = [(name, m) for name, m in (("YR", yr_m), ("OWM", owm_m), ("OM", om_m)) if m is not None]

    if len(active) == 1:
        name, m = active[0]
        source_mode = f"{name}_ONLY"
        r12, r24, r72 = m["r12"], m["r24"], m["r72"]
        r24_48, r48_72 = m["r24_48"], m["r48_72"]
        demand0, demand1, demand2 = m["demand0"], m["demand1"], m["demand2"]
        cdbg = m["cdbg"]
    else:
        source_mode = "ENSEMBLE_AVG_" + "+".join(name for name, _ in active)
        r12 = mean_available(*(m["r12"] for _, m in active))
        r24 = mean_available(*(m["r24"] for _, m in active))
        r72 = mean_available(*(m["r72"] for _, m in active))
        r24_48 = mean_available(*(m["r24_48"] for _, m in active))
        r48_72 = mean_available(*(m["r48_72"] for _, m in active))
        demand0 = mean_available(*(m["demand0"] for _, m in active))
        demand1 = mean_available(*(m["demand1"] for _, m in active))
        demand2 = mean_available(*(m["demand2"] for _, m in active))
        cdbg = {
            "demand_mm_24h": demand0,
            "t_mean_24h_c": mean_available(*(m["cdbg"].get("t_mean_24h_c") for _, m in active)),
            "rh_mean_24h_pct": mean_available(*(m["cdbg"].get("rh_mean_24h_pct") for _, m in active)),
            "wind_mean_24h_mps": mean_available(*(m["cdbg"].get("wind_mean_24h_mps") for _, m in active)),
            "t_min_12h_c": mean_available(*(m["cdbg"].get("t_min_12h_c") for _, m in active)),
            "vpd_kpa": mean_available(*(m["cdbg"].get("vpd_kpa") for _, m in active)),
            "daylength_h": active[0][1]["cdbg"].get("daylength_h"),
            "solar_term": mean_available(*(m["cdbg"].get("solar_term") for _, m in active)),
            "temp_term": mean_available(*(m["cdbg"].get("temp_term") for _, m in active)),
            "vpd_term": mean_available(*(m["cdbg"].get("vpd_term") for _, m in active)),
            "wind_term": mean_available(*(m["cdbg"].get("wind_term") for _, m in active)),
            "frost_factor": mean_available(*(m["cdbg"].get("frost_factor") for _, m in active)),
        }

    pct_base, dbg = decision_percent_dynamic(r12, r24, demand0, exposure=EXPOSURE_FACTOR)
    r24_72_raw = max(0.0, r72 - r24)
    r24_72_eff = r24_72_raw * EXPOSURE_FACTOR
    future_need_48h = max(0.0, demand1 + demand2)
    if future_need_48h <= 1e-6:
        future_need_48h = 2.0 * max(demand0, 0.0)

    mult, cov = future_credit_multiplier(
        future_rain_eff_mm=r24_72_eff,
        future_need_mm=future_need_48h,
        min_multiplier=FUTURE_CREDIT_MIN_MULTIPLIER,
    )
    pct = int(round(clamp(float(pct_base) * mult, 0.0, 100.0)))
    label = label_from_percent(pct)
    pump_seconds = pump_seconds_from_percent(pct)

    overall_outlook = overall_outlook_text(r24, r72, demand0, pct)

    summary_text = summarize(
        label=label,
        pct=pct,
        pump_seconds=pump_seconds,
        r0_24_raw=r24,
        r0_24_eff=dbg["r24_eff_mm"],
        r24_48_raw=r24_48,
        r24_48_eff=r24_48 * EXPOSURE_FACTOR,
        r48_72_raw=r48_72,
        r48_72_eff=r48_72 * EXPOSURE_FACTOR,
        demand_mm_24h=cdbg["demand_mm_24h"],
        rain_index=dbg["rain_index_mm"],
        start_reduce=dbg["start_reduce_mm"],
        full_skip=dbg["full_skip_mm"],
    )

    ensemble = {
        "r12": r12,
        "r24": r24,
        "r72": r72,
        "r24_48": r24_48,
        "r48_72": r48_72,
        "demand0": demand0,
        "demand1": demand1,
        "demand2": demand2,
        "cdbg": cdbg,
        "dbg": dbg,
        "future_cover_ratio": cov,
        "future_multiplier": mult,
        "pct_base": pct_base,
        "pct": pct,
        "label": label,
        "pump_seconds": pump_seconds,
        "summary_text": summary_text,
        "overall_outlook": overall_outlook,
    }
    return source_mode, yr_m, owm_m, om_m, ensemble


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fetch weather forecasts, compute irrigation decision, and save a text cache file."
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Output text file path.")
    p.add_argument("--hours", type=int, default=HOURS_AHEAD, help="Forecast horizon hours.")
    p.add_argument("--lat", type=float, default=None, help="Override latitude.")
    p.add_argument("--lon", type=float, default=None, help="Override longitude.")
    p.add_argument("--tz", default=None, help="Override timezone name.")
    p.add_argument(
        "--use-ip-location",
        action="store_true",
        help="Use ipinfo.io geolocation unless explicit lat/lon/tz overrides are given.",
    )
    p.add_argument(
        "--print",
        dest="also_print",
        action="store_true",
        help="Also print the generated report to stdout.",
    )
    p.add_argument(
        "--service",
        action="store_true",
        help=(
            "Run persistently: start the MQTT ack subscriber, then loop "
            "forever running the full tri-hourly cycle at each quantized "
            "boundary and the hourly current-only refresh at each :30 mark. "
            "This is the normal way to run the script (saignes-weather.service) "
            "-- one persistent process instead of systemd timers re-invoking "
            "it. --ack-listener/--current-only/--fetch-only below remain "
            "for manual/debugging use."
        ),
    )
    p.add_argument(
        "--ack-listener",
        action="store_true",
        help=(
            "Run only the persistent MQTT ack subscriber and block forever "
            "(no forecast fetch). Folded into --service for normal operation; "
            "useful standalone for debugging pump ACKs in isolation."
        ),
    )
    p.add_argument(
        "--current-only",
        action="store_true",
        help=(
            "Refresh only the METAR current-conditions block in weather_cache.json "
            "(station ground truth used by the dashboard's metric tiles) without "
            "touching the forecast ensemble, irrigation decision, MetarReading/"
            "IrrigationDecision history, or MQTT publish. Folded into --service's "
            "hourly :30 cadence for normal operation; useful standalone for "
            "manual/debugging refreshes."
        ),
    )
    p.add_argument(
        "--fetch-only",
        action="store_true",
        help=(
            "Re-fetch all four live sources (Yr.no, OWM, Open-Meteo, METAR) and "
            "refresh weather_cache.json's forecast/current/ensemble/comparison "
            "sections, without touching the irrigation decision -- the 'irrigation' "
            "block (commanded percent/pump seconds/next-event schedule) is carried "
            "over unchanged from the existing cache -- and without writing "
            "IrrigationDecision/next_watering.json rows or publishing to MQTT. "
            "For the dashboard's manual refresh button: lets a user pull fresh "
            "data on demand without the risk of a decision commit landing outside "
            "the normal 3-hourly cadence and silently consuming a due event that "
            "never actually reaches the pumps."
        ),
    )
    return p.parse_args()


def fetch_source_with_retry(
    source_name: str,
    fetch_fn,
    extract_fn,
    session: requests.Session,
    lat: float,
    lon: float,
    now_utc: datetime,
    horizon_h: int,
    cache_path: str,
    station_id: str,
    attempts: int = FETCH_RETRY_ATTEMPTS,
    wait_seconds: float = FETCH_RETRY_WAIT_SECONDS,
) -> Tuple[Optional[List["Row"]], Optional[str]]:
    """Fetch one weather source with retries; on total failure, fall back to
    the last successfully-cached payload re-windowed to the current moment
    (extract_fn drops anything older than now_utc on its own) so a transient
    outage shows the last known-good forecast instead of a blank chart.
    Returns (rows, error_message) -- error_message is set whenever the LIVE
    fetch failed, even if a stale fallback filled rows in, so the dashboard's
    status dot still reflects the real live-fetch outcome.

    The cache is a dict keyed by station_id rather than one flat payload --
    same reasoning as fetch_metar_rain_with_retry's cache: a naive
    single-slot cache would silently serve a *different, previously-
    configured* station's last known-good forecast after switching airports
    (e.g. a transient timeout on the very first live fetch for the new
    station), making the dashboard look like the station switch didn't do
    anything. Keying by station means a station with no prior successful
    fetch has nothing to fall back to, and honestly reports failure
    instead."""
    last_err: Optional[str] = None
    for attempt in range(1, attempts + 1):
        try:
            payload = fetch_fn(session, lat, lon)
            rows = extract_fn(payload, now_utc=now_utc, horizon_h=horizon_h)
            if rows:
                try:
                    cache = _load_json_dict(cache_path)
                    cache[station_id] = payload
                    atomic_write_json(cache_path, cache)
                except Exception as e:
                    print(f"WARNING: could not cache {source_name} payload ({type(e).__name__}: {e})", file=sys.stderr)
                return rows, None
            last_err = f"{source_name} returned no rows in horizon."
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < attempts:
            print(
                f"WARNING: {source_name} fetch attempt {attempt}/{attempts} failed "
                f"({last_err}); retrying in {wait_seconds:.0f}s.",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    cached_payload = _load_json_dict(cache_path).get(station_id)
    if cached_payload is not None:
        try:
            fallback_rows = extract_fn(cached_payload, now_utc=now_utc, horizon_h=horizon_h)
            if fallback_rows:
                print(
                    f"WARNING: {source_name} failed after {attempts} attempts ({last_err}); "
                    f"falling back to last cached forecast for {station_id} "
                    f"({len(fallback_rows)} rows still ahead of now).",
                    file=sys.stderr,
                )
                return fallback_rows, f"{last_err} (showing cached forecast)"
        except Exception as e:
            print(f"WARNING: {source_name} cached-payload fallback failed ({type(e).__name__}: {e})", file=sys.stderr)

    return None, last_err


def fetch_metar_rain_with_retry(
    session: requests.Session,
    station_id: str,
    now_utc: datetime,
    lookback_hours: float,
    cache_path: str,
    attempts: int = FETCH_RETRY_ATTEMPTS,
    wait_seconds: float = FETCH_RETRY_WAIT_SECONDS,
) -> Tuple[float, Optional[Dict[str, Any]], Optional[str]]:
    """Same retry + stale-fallback-cache shape as fetch_source_with_retry, but
    for METAR observations feeding the recent-rain ground truth rather than a
    forecast. On total failure, re-integrates the last successfully-cached
    observations for THIS station (they're already timestamped in the past,
    so no re-windowing is needed) instead of treating recent rain as zero.

    The cache is a dict keyed by station_id rather than one flat payload --
    since the dashboard now accepts any ICAO code, a naive single-slot cache
    would silently serve a *different, previously-configured* station's last
    known-good observations after switching airports (e.g. aviationweather.gov
    returning no data for the newly-configured station), mislabeling stale
    data from the old station as current conditions for the new one. Keying
    by station means a station with no prior successful fetch has nothing to
    fall back to, and honestly reports failure instead.

    Also returns the single latest observation (current-conditions ground
    truth for the dashboard's metric tiles), separately from the integrated
    rain total."""
    last_err: Optional[str] = None
    for attempt in range(1, attempts + 1):
        try:
            observations = fetch_metar_history(session, station_id, lookback_hours)
            cache = _load_json_dict(cache_path)
            cache[station_id] = observations
            atomic_write_json(cache_path, cache)
            return (
                metar_accumulated_rain_mm(observations, now_utc, lookback_hours),
                metar_latest_observation(observations, now_utc),
                None,
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"

        if attempt < attempts:
            print(
                f"WARNING: METAR fetch attempt {attempt}/{attempts} failed "
                f"({last_err}); retrying in {wait_seconds:.0f}s.",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    cache = _load_json_dict(cache_path)
    cached_observations = cache.get(station_id)
    if cached_observations is not None:
        try:
            print(
                f"WARNING: METAR failed after {attempts} attempts ({last_err}); "
                f"falling back to last cached observations for {station_id}.",
                file=sys.stderr,
            )
            return (
                metar_accumulated_rain_mm(cached_observations, now_utc, lookback_hours),
                metar_latest_observation(cached_observations, now_utc),
                f"{last_err} (showing cached observations)",
            )
        except Exception as e:
            print(f"WARNING: METAR cached-payload fallback failed ({type(e).__name__}: {e})", file=sys.stderr)

    return 0.0, None, last_err


def run_current_only(args) -> int:
    """Lightweight hourly refresh: re-fetch just the METAR current-conditions
    ground truth and splice it into the existing weather_cache.json, leaving
    the forecast ensemble/irrigation decision/CSV history/MQTT publish
    untouched (those still only update on the tri-hourly full run_once
    cycle). Requires a cache file to already exist from a prior full run."""
    cache_path = Path(WEATHER_CACHE_JSON).expanduser().resolve()
    if not cache_path.exists():
        print(
            "WARNING: weather_cache.json does not exist yet -- run a full "
            "fetch (weather_mqtt.py, no flags) first. Skipping current-only update.",
            file=sys.stderr,
        )
        return 1

    try:
        report_obj = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not read existing weather_cache.json ({type(e).__name__}: {e}); skipping.", file=sys.stderr)
        return 1

    location = resolve_location(
        use_ip_location=True if args.use_ip_location else None,
        lat_override=args.lat,
        lon_override=args.lon,
        tz_override=args.tz,
    )
    tz = get_tz(location.tz_name)
    now_utc = datetime.now(tz).astimezone(timezone.utc)

    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})
            _recent_rain_mm, current_obs, metar_err = fetch_metar_rain_with_retry(
                session, location.station or DEFAULT_STATION, now_utc, METAR_RAIN_LOOKBACK_HOURS,
                SOURCE_PAYLOAD_CACHE["METAR"],
            )
    except Exception as e:
        print(f"ERROR: current-only METAR fetch failed ({type(e).__name__}: {e}); leaving cache untouched.", file=sys.stderr)
        return 1

    report_obj["current"] = current_conditions_block(current_obs, location.tz_name)
    status = report_obj.setdefault("status", {})
    status["metar_ok"] = metar_err is None
    status["metar_error"] = metar_err

    atomic_write_json(WEATHER_CACHE_JSON, report_obj)
    append_metar_log_row(report_obj["current"], location.tz_name)
    print(
        f"current-only update: refreshed METAR current conditions in {WEATHER_CACHE_JSON} "
        f"(metar_ok={metar_err is None})"
    )
    return 0


def run_once(args) -> None:
    global HOURS_AHEAD
    HOURS_AHEAD = int(args.hours)

    location = resolve_location(
        use_ip_location=True if args.use_ip_location else None,
        lat_override=args.lat,
        lon_override=args.lon,
        tz_override=args.tz,
    )

    tz = get_tz(location.tz_name)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(timezone.utc)

    yr_err: Optional[str] = None
    owm_err: Optional[str] = None
    om_err: Optional[str] = None
    metar_err: Optional[str] = None
    yr_rows: Optional[List[Row]] = None
    owm_rows: Optional[List[Row]] = None
    om_rows: Optional[List[Row]] = None
    recent_rain_mm: float = 0.0

    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})

            yr_rows, yr_err = fetch_source_with_retry(
                "Yr.no", fetch_yr_forecast, extract_rows_yr_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Yr.no"], location.station or DEFAULT_STATION,
            )
            owm_rows, owm_err = fetch_source_with_retry(
                "OWM", fetch_owm_forecast, extract_rows_owm_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["OWM"], location.station or DEFAULT_STATION,
            )
            om_rows, om_err = fetch_source_with_retry(
                "Open-Meteo", fetch_open_meteo_forecast, extract_rows_open_meteo_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Open-Meteo"], location.station or DEFAULT_STATION,
            )
            recent_rain_mm, current_obs, metar_err = fetch_metar_rain_with_retry(
                session, location.station or DEFAULT_STATION, now_utc, METAR_RAIN_LOOKBACK_HOURS,
                SOURCE_PAYLOAD_CACHE["METAR"],
            )

        source_mode, yr_m, owm_m, om_m, ensemble = build_ensemble(
            now_utc=now_utc,
            now_local=now_local,
            lat=location.lat,
            yr_rows=yr_rows,
            owm_rows=owm_rows,
            om_rows=om_rows,
            yr_err=yr_err,
            owm_err=owm_err,
            om_err=om_err,
        )

        # Scoped to the currently-configured station -- otherwise switching
        # station (dashboard settings panel) would seed the new location's
        # schedule (next_watering_epoch, last-completed-event) off a
        # different physical garden's prior history. Pre-station-tracking
        # rows (station="") are excluded here too, not guessed at.
        db_rows = IrrigationDecision.objects.filter(station=location.station)
        schedule = apply_irrigation_schedule(
            now_local=now_local,
            now_utc=now_utc,
            location=location,
            source_mode=source_mode,
            yr_rows=yr_rows,
            owm_rows=owm_rows,
            om_rows=om_rows,
            ensemble=ensemble,
            db_rows=db_rows,
            recent_rain_mm=recent_rain_mm,
        )
        ensemble["schedule"] = schedule
        ensemble["pct"] = schedule["command_percent"]
        ensemble["label"] = schedule["decision_label"]
        ensemble["pump_seconds"] = schedule["commanded_pump_seconds"]
        ensemble["summary_text"] = schedule["summary_text"]
        ensemble["overall_outlook"] = (
            f"{ensemble['overall_outlook']} Event mode: "
            f"{schedule['decision_code'].lower()}."
        )

        comparison_rows = build_comparison_rows(
            owm_rows=owm_rows or [],
            yr_rows=yr_rows or [],
            om_rows=om_rows or [],
            tz_name=location.tz_name,
        )

        if not comparison_rows:
            fallback_rows = owm_rows or yr_rows or om_rows or []
            comparison_rows = [
                {
                    "local_time": r.time_utc.astimezone(tz).strftime("%m-%d %H:%M"),
                    "epoch": int(r.time_utc.timestamp()),
                    "owm_temp_c": r.temp_c if r in (owm_rows or []) else None,
                    "yr_temp_c": r.temp_c if r in (yr_rows or []) else None,
                    "om_temp_c": r.temp_c if r in (om_rows or []) else None,
                    "owm_rh_pct": r.rh_pct if r in (owm_rows or []) else None,
                    "yr_rh_pct": r.rh_pct if r in (yr_rows or []) else None,
                    "om_rh_pct": r.rh_pct if r in (om_rows or []) else None,
                    "owm_wind_mps": r.wind_mps if r in (owm_rows or []) else None,
                    "yr_wind_mps": r.wind_mps if r in (yr_rows or []) else None,
                    "om_wind_mps": r.wind_mps if r in (om_rows or []) else None,
                    "owm_rain_3h_mm": r.precip_mm_period if r in (owm_rows or []) else None,
                    "yr_rain_3h_mm": r.precip_mm_period if r in (yr_rows or []) else None,
                    "om_rain_3h_mm": r.precip_mm_period if r in (om_rows or []) else None,
                    "owm_desc": r.symbol_code if r in (owm_rows or []) else "",
                    "yr_sym": r.symbol_code if r in (yr_rows or []) else "",
                    "om_desc": r.symbol_code if r in (om_rows or []) else "",
                }
                for r in fallback_rows[:24]
            ]
        report_obj = build_report_object(
            location=location,
            now_local=now_local,
            source_mode=source_mode,
            yr_err=yr_err,
            owm_err=owm_err,
            om_err=om_err,
            metar_err=metar_err,
            current_obs=current_obs,
            yr_m=yr_m,
            owm_m=owm_m,
            om_m=om_m,
            comparison_rows=comparison_rows,
            ensemble=ensemble,
            output_path=args.output,
        )
        report = render_report(
            location=location,
            now_local=now_local,
            source_mode=source_mode,
            yr_err=yr_err,
            owm_err=owm_err,
            om_err=om_err,
            yr_m=yr_m,
            owm_m=owm_m,
            om_m=om_m,
            comparison_rows=comparison_rows,
            pct=ensemble["pct"],
            label=ensemble["label"],
            pump_seconds=ensemble["pump_seconds"],
            summary_text=ensemble["summary_text"],
            ensemble=ensemble,
        )

        sys.stdout.write(report)
        sys.stdout.flush()

        atomic_write_text(args.output, report)
        upsert_irrigation_decision(schedule["db_row"])
        atomic_write_json(NEXT_WATERING_JSON, schedule["json_payload"])
        report_obj['next_run_epoch'] = next_quantized_run_epoch(now_local)
        atomic_write_json(WEATHER_CACHE_JSON, report_obj)
        append_metar_log_row(report_obj.get("current"), location.tz_name)

        # Publish the next-two-events schedule to the pumps. A broker outage
        # must not fail the whole run (the JSON cache on disk is the source of
        # truth and the next cron run will retry), so failures only warn.
        if MQTT_SEND_ENABLED:
            try:
                detail = publish_mqtt_schedule(schedule["json_payload"])
                print(f"MQTT: published {detail}", file=sys.stderr)
            except Exception as e:
                print(
                    f"WARNING: MQTT publish failed ({type(e).__name__}: {e}); "
                    f"schedule was still written to {NEXT_WATERING_JSON}.",
                    file=sys.stderr,
                )

    except Exception:
        raise


def run_fetch_only(args) -> int:
    """Manual dashboard refresh: re-fetch all four live sources (Yr.no, OWM,
    Open-Meteo, METAR) and rebuild weather_cache.json's forecast/current/
    ensemble/comparison sections from them, exactly like run_once -- but
    never touch the stateful irrigation-decision pipeline. apply_irrigation_
    schedule() is what decides a watering event is due and commits that to
    the IrrigationDecision table/next_watering.json (MQTT publish is
    downstream of that same commit); calling it outside the normal 3-hourly
    cadence risks an event coming due in between manual clicks and getting
    silently marked complete without ever reaching a pump, since this path
    deliberately never publishes. So the existing cache's 'irrigation' block
    (commanded percent/pump seconds/next-event schedule) is carried over
    unchanged instead of being recomputed, and the IrrigationDecision table/
    next_watering.json/MQTT are left untouched entirely."""
    global HOURS_AHEAD
    HOURS_AHEAD = int(args.hours)

    cache_path = Path(WEATHER_CACHE_JSON).expanduser().resolve()
    if not cache_path.exists():
        print(
            "WARNING: weather_cache.json does not exist yet -- run a full "
            "fetch (weather_mqtt.py, no flags) first. Skipping fetch-only update.",
            file=sys.stderr,
        )
        return 1
    try:
        previous_report = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERROR: could not read existing weather_cache.json ({type(e).__name__}: {e}); skipping.", file=sys.stderr)
        return 1

    location = resolve_location(
        use_ip_location=True if args.use_ip_location else None,
        lat_override=args.lat,
        lon_override=args.lon,
        tz_override=args.tz,
    )

    tz = get_tz(location.tz_name)
    now_local = datetime.now(tz)
    now_utc = now_local.astimezone(timezone.utc)

    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})

            yr_rows, yr_err = fetch_source_with_retry(
                "Yr.no", fetch_yr_forecast, extract_rows_yr_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Yr.no"], location.station or DEFAULT_STATION,
            )
            owm_rows, owm_err = fetch_source_with_retry(
                "OWM", fetch_owm_forecast, extract_rows_owm_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["OWM"], location.station or DEFAULT_STATION,
            )
            om_rows, om_err = fetch_source_with_retry(
                "Open-Meteo", fetch_open_meteo_forecast, extract_rows_open_meteo_with_cumulative,
                session, location.lat, location.lon, now_utc, HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Open-Meteo"], location.station or DEFAULT_STATION,
            )
            _recent_rain_mm, current_obs, metar_err = fetch_metar_rain_with_retry(
                session, location.station or DEFAULT_STATION, now_utc, METAR_RAIN_LOOKBACK_HOURS,
                SOURCE_PAYLOAD_CACHE["METAR"],
            )

        source_mode, yr_m, owm_m, om_m, ensemble = build_ensemble(
            now_utc=now_utc,
            now_local=now_local,
            lat=location.lat,
            yr_rows=yr_rows,
            owm_rows=owm_rows,
            om_rows=om_rows,
            yr_err=yr_err,
            owm_err=owm_err,
            om_err=om_err,
        )

        comparison_rows = build_comparison_rows(
            owm_rows=owm_rows or [],
            yr_rows=yr_rows or [],
            om_rows=om_rows or [],
            tz_name=location.tz_name,
        )

        report_obj = build_report_object(
            location=location,
            now_local=now_local,
            source_mode=source_mode,
            yr_err=yr_err,
            owm_err=owm_err,
            om_err=om_err,
            metar_err=metar_err,
            current_obs=current_obs,
            yr_m=yr_m,
            owm_m=owm_m,
            om_m=om_m,
            comparison_rows=comparison_rows,
            ensemble=ensemble,
            output_path=args.output,
        )
        # Preserve the last-committed irrigation decision verbatim -- see the
        # docstring above for why this path never recomputes it.
        report_obj["irrigation"] = previous_report.get("irrigation", {})
        report_obj["next_run_epoch"] = next_quantized_run_epoch(now_local)
        atomic_write_json(WEATHER_CACHE_JSON, report_obj)
    except Exception as e:
        print(f"ERROR: fetch-only update failed ({type(e).__name__}: {e}); leaving cache untouched.", file=sys.stderr)
        return 1

    print(
        f"fetch-only update: refreshed forecast/current/ensemble in {WEATHER_CACHE_JSON} "
        f"(source_mode={source_mode}, metar_ok={metar_err is None}); irrigation decision "
        f"and MQTT untouched."
    )
    return 0


def _next_current_only_epoch(now_local: datetime) -> int:
    """Next hourly :30 mark, local wall-clock -- run_service()'s replacement
    for saignes-weather-current.timer's OnCalendar=*-*-* *:30:00."""
    candidate = now_local.replace(minute=30, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(hours=1)
    return int(candidate.timestamp())


def run_service(args) -> int:
    """Persistent replacement for saignes-weather.timer +
    saignes-weather-current.timer + saignes-ack-listener.service: start the
    MQTT ack subscriber once (same as --ack-listener), then loop forever,
    sleeping until the next of {quantized tri-hourly boundary, hourly :30
    mark} and running the matching cycle -- same cadence the two timers
    produced, just internally scheduled instead of systemd re-invoking the
    process. Each cycle is wrapped in its own try/except so a bad run can't
    kill the loop or the ack-listener thread sharing this process -- the ack
    subscriber runs on paho's own background thread via loop_start(), so it
    stays responsive regardless of what this loop is doing."""
    if MQTT_SEND_ENABLED:
        client = start_ack_subscriber()
        if client is None:
            print("WARNING: ack subscriber unavailable; continuing without pump-ack tracking.", file=sys.stderr)
    else:
        print("WARNING: MQTT_SEND_ENABLED is False; no ack subscriber, no schedule publish.", file=sys.stderr)

    # First-run bootstrap: on a completely fresh data directory (no
    # weather_cache.json yet -- a brand-new install, e.g. a freshly-launched
    # AppImage) the dashboard would otherwise sit in demo mode, and even the
    # manual refresh button refuses to help (run_fetch_only requires an
    # existing cache to refresh), until whatever's left of the current
    # RUN_INTERVAL_HOURS window elapses. Run one full cycle immediately
    # instead of making a new install wait; every cycle after this one goes
    # through the normal quantized schedule below.
    if not Path(WEATHER_CACHE_JSON).expanduser().exists():
        print("run_service: no weather_cache.json yet -- running an initial fetch now instead of waiting for the next scheduled cycle.", file=sys.stderr)
        try:
            run_once(args)
        except Exception as e:
            print(f"ERROR: initial run_service fetch failed ({type(e).__name__}: {e})", file=sys.stderr)

    while True:
        now_local = datetime.now().astimezone()
        next_cycle = next_quantized_run_epoch(now_local)
        next_current = _next_current_only_epoch(now_local)
        next_fire = min(next_cycle, next_current)
        sleep_s = max(1.0, next_fire - now_local.timestamp())
        print(f"run_service: sleeping {sleep_s:.0f}s until next fire ({datetime.fromtimestamp(next_fire).astimezone().isoformat(timespec='seconds')})", file=sys.stderr)
        time.sleep(sleep_s)

        now_local = datetime.now().astimezone()
        due_cycle = now_local.timestamp() >= next_cycle - 1
        due_current = now_local.timestamp() >= next_current - 1
        # Pick up any station/broker/topic change made via the dashboard's
        # config panel since the last cycle -- see refresh_site_config_
        # overrides()'s docstring for why this can't just happen once at
        # import time anymore. (A broker/topic change still won't move the
        # ack subscriber's already-open connection to the new broker; only
        # the next scheduled publish and the next location resolution pick
        # up the change.)
        refresh_site_config_overrides()
        try:
            if due_cycle:
                run_once(args)
            elif due_current:
                run_current_only(args)
        except Exception as e:
            print(f"ERROR: run_service cycle failed ({type(e).__name__}: {e})", file=sys.stderr)


def main() -> int:
    args = parse_args()

    # Normal operation: one persistent process instead of systemd timers
    # re-invoking the script. See run_service()'s docstring.
    if args.service:
        return run_service(args)

    # Pump acks can arrive at any time (a node wakes on its own schedule, or
    # executes a future-dated event hours after the forecast that scheduled
    # it). --ack-listener runs just that, standalone, for debugging --
    # normal operation gets it via --service above instead.
    if args.ack_listener:
        if not MQTT_SEND_ENABLED:
            print("ERROR: --ack-listener requires MQTT_SEND_ENABLED = True.", file=sys.stderr)
            return 1
        client = start_ack_subscriber()
        if client is None:
            return 1
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            client.loop_stop()
            return 130

    # Hourly airport-ground-truth refresh, standalone for manual/debugging use
    # -- normal operation gets this via --service's hourly :30 cadence instead.
    if args.current_only:
        return run_current_only(args)

    # Manual on-demand refresh triggered from the dashboard -- fetches fresh
    # data from all sources but deliberately never touches the irrigation
    # decision/DB/MQTT publish. See run_fetch_only()'s docstring.
    if args.fetch_only:
        return run_fetch_only(args)

    # Forecast + irrigation-decision + MQTT-publish cycle: runs once and
    # exits, standalone for manual/debugging use -- normal operation gets
    # this via --service's quantized-boundary cadence instead.
    try:
        run_once(args)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
