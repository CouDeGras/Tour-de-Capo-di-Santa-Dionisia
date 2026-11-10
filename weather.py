#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
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
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import time
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()
from dashboard.models import MetarReading

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
_APP_DATA_ROOT = Path(
    os.environ.get("CAPO_DI_SANTA_DIONISIA_DATA_DIR") or Path(__file__).resolve().parent
)
_DATA_DIR = _APP_DATA_ROOT / "data"

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
DEFAULT_STATION = "LIRA"
DEFAULT_LAT = 41.79944
DEFAULT_LON = 12.59722
DEFAULT_TZ = "Europe/Rome"
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
    """Reload the selected weather station before each service cycle."""
    global DEFAULT_STATION
    cfg = load_site_config(SITE_CONFIG_JSON)
    station = str(cfg.get("station") or "").strip().upper()
    DEFAULT_STATION = station if ICAO_RE.match(station) else "LIRA"


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

WEATHER_CACHE_JSON = str(_DATA_DIR / "weather_cache.json")
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

# DEFAULT_OUTPUT = os.getenv("WEATHER_TEXT_OUTPUT", "./garden_weather_cache.txt")
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


def next_quantized_run_epoch(
    now_local: datetime, interval_hours: int = RUN_INTERVAL_HOURS
) -> int:
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
        return (
            float(cached["lat"]),
            float(cached["lon"]),
            str(cached["tz"]),
            str(cached["name"]),
        )

    with requests.Session() as session:
        session.headers.update({"Connection": "close"})
        info_resp = session.get(
            STATION_INFO_API_URL,
            params={"ids": station_id, "format": "json"},
            timeout=TIMEOUT_SECONDS,
        )
        if info_resp.status_code != 200:
            raise RuntimeError(
                f"aviationweather.gov stationinfo HTTP {info_resp.status_code}: {info_resp.text[:300]}"
            )
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
            params={
                "latitude": lat,
                "longitude": lon,
                "timezone": "auto",
                "forecast_days": 1,
            },
            timeout=TIMEOUT_SECONDS,
        )
        if tz_resp.status_code != 200:
            raise RuntimeError(
                f"Open-Meteo timezone lookup HTTP {tz_resp.status_code}: {tz_resp.text[:300]}"
            )
        tz_name = tz_resp.json().get("timezone") or DEFAULT_TZ

    cache[station_id] = {
        "lat": lat,
        "lon": lon,
        "tz": tz_name,
        "name": name,
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
        "tmin24_c": mean_available(
            yr_s.get("tmin24_c"), owm_s.get("tmin24_c"), om_s.get("tmin24_c")
        ),
        "tmax24_c": mean_available(
            yr_s.get("tmax24_c"), owm_s.get("tmax24_c"), om_s.get("tmax24_c")
        ),
        "tmean72_c": mean_available(
            yr_s.get("tmean72_c"), owm_s.get("tmean72_c"), om_s.get("tmean72_c")
        ),
    }


# Same shape as rows_temp_stats/ensemble_temp_stats above, just for relative
# humidity's next-24h min/max instead of temperature's -- the dashboard's
# Humidity tile wants a forecast range under it the same way the Temp tile
# already does (m-temp-range), rather than the METAR observation-age
# caption every OTHER tile skips anyway.
def rows_rh_stats(rows: List["Row"], now_utc: datetime) -> Dict[str, Optional[float]]:
    vals24: List[float] = []
    for r in rows:
        if r.rh_pct is None:
            continue
        h = (r.time_utc - now_utc).total_seconds() / 3600.0
        if h < 0 or h >= 24.0:
            continue
        vals24.append(float(r.rh_pct))
    return {
        "rhmin24_pct": min(vals24) if vals24 else None,
        "rhmax24_pct": max(vals24) if vals24 else None,
    }


def ensemble_rh_stats(
    yr_rows: Optional[List["Row"]],
    owm_rows: Optional[List["Row"]],
    om_rows: Optional[List["Row"]],
    now_utc: datetime,
) -> Dict[str, Optional[float]]:
    yr_s = rows_rh_stats(yr_rows, now_utc) if yr_rows is not None else {}
    owm_s = rows_rh_stats(owm_rows, now_utc) if owm_rows is not None else {}
    om_s = rows_rh_stats(om_rows, now_utc) if om_rows is not None else {}
    return {
        "rhmin24_pct": mean_available(
            yr_s.get("rhmin24_pct"), owm_s.get("rhmin24_pct"), om_s.get("rhmin24_pct")
        ),
        "rhmax24_pct": mean_available(
            yr_s.get("rhmax24_pct"), owm_s.get("rhmax24_pct"), om_s.get("rhmax24_pct")
        ),
    }


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


# ---------- Yr.no ----------
def fetch_yr_forecast(
    session: requests.Session, lat: float, lon: float
) -> Dict[str, Any]:
    headers = {
        "User-Agent": YR_USER_AGENT,
        "Accept": "application/json",
    }
    params = {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"}
    resp = session.get(
        YR_API_URL, params=params, headers=headers, timeout=TIMEOUT_SECONDS
    )

    if resp.status_code == 403:
        raise RuntimeError("Yr.no 403 Forbidden: set a real custom User-Agent.")
    if resp.status_code == 429:
        raise RuntimeError("Yr.no 429 Too Many Requests.")
    if resp.status_code not in (200, 203):
        raise RuntimeError(f"Yr.no HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def pick_precip_block_yr(
    data: Dict[str, Any]
) -> Tuple[Optional[float], Optional[int], str, str]:
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
        raise RuntimeError(
            "Invalid Yr.no payload: properties.timeseries missing or not a list."
        )

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
def fetch_owm_forecast(
    session: requests.Session, lat: float, lon: float
) -> Dict[str, Any]:
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
        w0 = (
            weather_list[0]
            if weather_list and isinstance(weather_list[0], dict)
            else {}
        )

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
def fetch_open_meteo_forecast(
    session: requests.Session, lat: float, lon: float
) -> Dict[str, Any]:
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m,weather_code",
        "wind_speed_unit": "ms",  # default is km/h; match Yr/OWM's m/s
        "timezone": "UTC",  # so hourly.time strings can be parsed as naive UTC
        "forecast_days": OPEN_METEO_FORECAST_DAYS,
        "models": "cma_grapes_global",
    }
    resp = session.get(OPEN_METEO_API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(f"Open-Meteo HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


WMO_CODE_TEXT: Dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    56: "freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "dense freezing rain",
    71: "slight snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight showers",
    81: "showers",
    82: "violent showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm w/ hail",
    99: "thunderstorm w/ heavy hail",
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
        raise RuntimeError(
            "Invalid Open-Meteo payload: hourly.time missing or not a list."
        )

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


# ---------- Weather observations ----------
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


def rain_windows_3days(
    rows: List[Row], now_utc: datetime
) -> Tuple[float, float, float]:
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


def sat_vapor_pressure_kpa(temp_c: float) -> float:
    return 0.6108 * math.exp((17.27 * temp_c) / (temp_c + 237.3))


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


def fetch_metar_history(
    session: requests.Session, station_id: str, hours: float
) -> List[Dict[str, Any]]:
    params = {"ids": station_id, "format": "json", "hours": str(int(math.ceil(hours)))}
    resp = session.get(METAR_API_URL, params=params, timeout=TIMEOUT_SECONDS)
    if resp.status_code != 200:
        raise RuntimeError(
            f"aviationweather.gov METAR HTTP {resp.status_code}: {resp.text[:300]}"
        )
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
            rate = max(
                rate,
                _METAR_INTENSITY_RATE_MM_H.get(
                    intensity, _METAR_INTENSITY_RATE_MM_H[""]
                ),
            )
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
    observations: List[Dict[str, Any]],
    now_utc: datetime,
    lookback_hours: float = METAR_RAIN_LOOKBACK_HOURS,
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


def compute_source_metrics(rows, now_utc, now_local, lat_deg):
    r12, r24, r72 = rain_windows(rows, now_utc)
    _, r24_48, r48_72 = rain_windows_3days(rows, now_utc)
    return dict(r12=r12, r24=r24, r72=r72, r24_48=r24_48, r48_72=r48_72)


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
        overlap_h = (
            min(end_utc, r_end) - max(start_utc, r.time_utc)
        ).total_seconds() / 3600.0
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


def interp_field_at(
    rows: List[Row], t_utc: datetime, field: str
) -> Tuple[Optional[float], bool]:
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


def nearest_row(
    rows: List[Row], t_utc: datetime, max_delta_hours: float = 1.5
) -> Optional[Row]:
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

        out.append(
            {
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
                "owm_rain_3h_mm": o.precip_mm_period
                if o.precip_mm_period is not None
                else 0.0,
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
            }
        )
    return out


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
    forecast_stats: Dict[str, Optional[float]],
    output_path: str,
) -> Dict[str, Any]:
    return {
        "schema_version": "1.0",
        "report_type": "weather",
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
            "yr_ok": yr_err is None,
            "owm_ok": owm_err is None,
            "om_ok": om_err is None,
            "metar_ok": metar_err is None,
            "yr_error": yr_err,
            "owm_error": owm_err,
            "om_error": om_err,
            "metar_error": metar_err,
        },
        "current": current_conditions_block(current_obs, location.tz_name),
        "forecast_stats": forecast_stats,
        "ensemble": {
            "rain_mm": {
                "r12": ensemble["r12"],
                "r24": ensemble["r24"],
                "r72": ensemble["r72"],
                "r24_48": ensemble["r24_48"],
                "r48_72": ensemble["r48_72"],
            }
        },
        "sources": {
            "yr": None
            if yr_m is None
            else {
                "rain_mm": {"r12": yr_m["r12"], "r24": yr_m["r24"], "r72": yr_m["r72"]}
            },
            "owm": None
            if owm_m is None
            else {
                "rain_mm": {
                    "r12": owm_m["r12"],
                    "r24": owm_m["r24"],
                    "r72": owm_m["r72"],
                }
            },
            "om": None
            if om_m is None
            else {
                "rain_mm": {"r12": om_m["r12"], "r24": om_m["r24"], "r72": om_m["r72"]}
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
        "artifacts": {"text_report_path": output_path},
    }


def build_ensemble(
    now_utc, now_local, lat, yr_rows, owm_rows, om_rows, yr_err, owm_err, om_err
):
    sources = [
        compute_source_metrics(rows, now_utc, now_local, lat)
        if rows is not None
        else None
        for rows in (yr_rows, owm_rows, om_rows)
    ]
    active = [
        (name, metrics)
        for name, metrics in zip(("YR", "OWM", "OM"), sources)
        if metrics is not None
    ]
    if not active:
        raise RuntimeError(
            f"All forecast sources failed: {yr_err}; {owm_err}; {om_err}"
        )
    mode = (
        active[0][0] + "_ONLY"
        if len(active) == 1
        else "ENSEMBLE_AVG_" + "+".join(name for name, _ in active)
    )
    ensemble = {
        key: mean_available(*(metrics[key] for _, metrics in active))
        for key in ("r12", "r24", "r72", "r24_48", "r48_72")
    }
    return mode, *sources, ensemble


def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch weather forecasts and airport observations."
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--hours", type=int, default=HOURS_AHEAD)
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--tz")
    p.add_argument("--use-ip-location", action="store_true")
    p.add_argument("--print", dest="also_print", action="store_true")
    p.add_argument(
        "--service",
        action="store_true",
        help="Refresh forecasts every three hours and observations hourly.",
    )
    p.add_argument(
        "--current-only", action="store_true", help="Refresh airport observations."
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
                    print(
                        f"WARNING: could not cache {source_name} payload ({type(e).__name__}: {e})",
                        file=sys.stderr,
                    )
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
            fallback_rows = extract_fn(
                cached_payload, now_utc=now_utc, horizon_h=horizon_h
            )
            if fallback_rows:
                print(
                    f"WARNING: {source_name} failed after {attempts} attempts ({last_err}); "
                    f"falling back to last cached forecast for {station_id} "
                    f"({len(fallback_rows)} rows still ahead of now).",
                    file=sys.stderr,
                )
                return fallback_rows, f"{last_err} (showing cached forecast)"
        except Exception as e:
            print(
                f"WARNING: {source_name} cached-payload fallback failed ({type(e).__name__}: {e})",
                file=sys.stderr,
            )

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
            print(
                f"WARNING: METAR cached-payload fallback failed ({type(e).__name__}: {e})",
                file=sys.stderr,
            )

    return 0.0, None, last_err


def run_current_only(args) -> int:
    cache_path = Path(WEATHER_CACHE_JSON).expanduser().resolve()
    if not cache_path.exists():
        print(
            "WARNING: weather_cache.json does not exist yet -- run a full fetch (weather.py, no flags) first. Skipping current-only update.",
            file=sys.stderr,
        )
        return 1
    try:
        report_obj = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(
            f"ERROR: could not read existing weather_cache.json ({type(e).__name__}: {e}); skipping.",
            file=sys.stderr,
        )
        return 1
    location = resolve_location(
        use_ip_location=True if args.use_ip_location else None,
        lat_override=args.lat,
        lon_override=args.lon,
        tz_override=args.tz,
    )
    if (report_obj.get("location") or {}).get("station") != location.station:
        return run_once(args)
    tz = get_tz(location.tz_name)
    now_utc = datetime.now(tz).astimezone(timezone.utc)
    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})
            _recent_rain_mm, current_obs, metar_err = fetch_metar_rain_with_retry(
                session,
                location.station or DEFAULT_STATION,
                now_utc,
                METAR_RAIN_LOOKBACK_HOURS,
                SOURCE_PAYLOAD_CACHE["METAR"],
            )
    except Exception as e:
        print(
            f"ERROR: current-only METAR fetch failed ({type(e).__name__}: {e}).",
            file=sys.stderr,
        )
        return 1
    report_obj["current"] = current_conditions_block(current_obs, location.tz_name)
    status = report_obj.setdefault("status", {})
    status["metar_ok"] = metar_err is None
    status["metar_error"] = metar_err
    atomic_write_json(WEATHER_CACHE_JSON, report_obj)
    append_metar_log_row(report_obj["current"], location.tz_name)
    print(
        f"current-only update: refreshed METAR current conditions in {WEATHER_CACHE_JSON} (metar_ok={metar_err is None})"
    )
    return 0


def run_once(args) -> int:
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
    try:
        with requests.Session() as session:
            session.headers.update({"Connection": "close"})
            yr_rows, yr_err = fetch_source_with_retry(
                "Yr.no",
                fetch_yr_forecast,
                extract_rows_yr_with_cumulative,
                session,
                location.lat,
                location.lon,
                now_utc,
                HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Yr.no"],
                location.station or DEFAULT_STATION,
            )
            owm_rows, owm_err = fetch_source_with_retry(
                "OWM",
                fetch_owm_forecast,
                extract_rows_owm_with_cumulative,
                session,
                location.lat,
                location.lon,
                now_utc,
                HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["OWM"],
                location.station or DEFAULT_STATION,
            )
            om_rows, om_err = fetch_source_with_retry(
                "Open-Meteo",
                fetch_open_meteo_forecast,
                extract_rows_open_meteo_with_cumulative,
                session,
                location.lat,
                location.lon,
                now_utc,
                HOURS_AHEAD,
                SOURCE_PAYLOAD_CACHE["Open-Meteo"],
                location.station or DEFAULT_STATION,
            )
            _recent_rain_mm, current_obs, metar_err = fetch_metar_rain_with_retry(
                session,
                location.station or DEFAULT_STATION,
                now_utc,
                METAR_RAIN_LOOKBACK_HOURS,
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
            forecast_stats={
                **ensemble_temp_stats(
                    yr_rows=yr_rows, owm_rows=owm_rows, om_rows=om_rows, now_utc=now_utc
                ),
                **ensemble_rh_stats(
                    yr_rows=yr_rows, owm_rows=owm_rows, om_rows=om_rows, now_utc=now_utc
                ),
            },
            output_path=args.output,
        )
        report_obj["next_run_epoch"] = next_quantized_run_epoch(now_local)
        atomic_write_json(WEATHER_CACHE_JSON, report_obj)
        append_metar_log_row(report_obj["current"], location.tz_name)
        atomic_write_text(
            args.output, json.dumps(report_obj, ensure_ascii=False, indent=2)
        )
        if args.also_print:
            print(json.dumps(report_obj, ensure_ascii=False, indent=2))
    except Exception as e:
        print(
            f"ERROR: Weather update failed ({type(e).__name__}: {e}).", file=sys.stderr
        )
        return 1
    print(f"Weather updated: {WEATHER_CACHE_JSON}")
    return 0


def _next_current_only_epoch(now_local: datetime) -> int:
    """Next hourly :30 mark, local wall-clock -- run_service()'s replacement
    for saignes-weather-current.timer's OnCalendar=*-*-* *:30:00."""
    candidate = now_local.replace(minute=30, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(hours=1)
    return int(candidate.timestamp())


def run_service(args):
    """Refresh weather at three-hour boundaries and observations at hourly :30 marks."""
    refresh_site_config_overrides()
    run_once(args)
    while True:
        now = datetime.now().astimezone()
        next_cycle = next_quantized_run_epoch(now)
        next_current = _next_current_only_epoch(now)
        time.sleep(max(1.0, min(next_cycle, next_current) - now.timestamp()))
        refresh_site_config_overrides()
        try:
            if datetime.now().timestamp() >= next_cycle - 1:
                run_once(args)
            else:
                run_current_only(args)
        except Exception as exc:
            print(f"Weather refresh failed: {exc}", file=sys.stderr)


def main():
    args = parse_args()
    if args.service:
        return run_service(args)
    try:
        return run_current_only(args) if args.current_only else run_once(args)
    except Exception as exc:
        print(f"Weather refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
