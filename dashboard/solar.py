"""Station-local daily almanac using the supplied Skyfield integration."""
from datetime import date, datetime, time, timedelta
from functools import lru_cache
import logging
from threading import Lock, Thread
from zoneinfo import ZoneInfo

import numpy as np

from django.conf import settings

from skyfield import almanac
from skyfield.api import load, load_file, wgs84
from skyfield.nutationlib import iau2000b_radians

_lock = Lock()
_preparation_lock = Lock()
_preparation_started = False


def _prepare_worker(kernel):
    try:
        from .prepare_ephemeris import prepare
        prepare(kernel, ensure=True)
    except Exception:
        logging.getLogger(__name__).exception("Background solar data preparation failed")


def _start_preparation(kernel):
    global _preparation_started
    with _preparation_lock:
        if _preparation_started or kernel.is_file():
            return
        _preparation_started = True
        Thread(target=_prepare_worker, args=(kernel,), name="solar-data", daemon=True).start()

DAY_ALTITUDE = -50.0 / 60.0


def _phase_function(observer, sun):
    def phase_at(t):
        t._nutation_angles_radians = iau2000b_radians(t)
        altitude = observer.at(t).observe(sun).apparent().altaz()[0].degrees
        # Equality belongs to the brighter stage, including exactly -50 arcmin.
        return np.searchsorted([-18.0, -12.0, -6.0, DAY_ALTITUDE], altitude, side="right")
    phase_at.step_days = 0.04
    return phase_at


@lru_cache(maxsize=2)
def _load_resources(path, modified):
    return load.timescale(builtin=True), load_file(path)


def _resources():
    from .prepare_ephemeris import FALLBACK
    kernel = settings.DATA_DIR / "data" / "solar" / "sun-earth.bsp"
    if not kernel.is_file():
        # First actual solar use starts preparation without delaying the UI.
        _start_preparation(kernel)
    path = kernel if kernel.is_file() else FALLBACK
    return _load_resources(str(path), path.stat().st_mtime_ns)


def _daily(latitude, longitude, tz_name, day):
    ts, eph = _resources()
    return _daily_cached(latitude, longitude, tz_name, day, ts, eph)


@lru_cache(maxsize=32)
def _daily_cached(latitude, longitude, tz_name, day, ts, eph):
    tz = ZoneInfo(tz_name)
    start = datetime.combine(date.fromisoformat(day), time.min, tzinfo=tz)
    # Construct both local midnights so DST days have their actual duration.
    end = datetime.combine(start.date() + timedelta(days=1), time.min, tzinfo=tz)
    d0, d1 = ts.from_datetime(start), ts.from_datetime(end)
    sun = eph["sun"]
    place = wgs84.latlon(latitude, longitude)
    observer = eph["earth"] + place
    phases = _phase_function(observer, sun)

    def iso(t):
        return t.astimezone(tz).isoformat(timespec="seconds")

    rise_times, rise_valid = almanac.find_risings(observer, sun, d0, d1, horizon_degrees=DAY_ALTITUDE)
    set_times, set_valid = almanac.find_settings(observer, sun, d0, d1, horizon_degrees=DAY_ALTITUDE)
    rises = [t for t, valid in zip(rise_times, rise_valid) if valid]
    sets = [t for t, valid in zip(set_times, set_valid) if valid]
    def daylight(t):
        return phases(t) == 4
    is_day = bool(daylight(d0))
    cursor = start.timestamp()
    seconds = 0.0
    for instant, entering_day in sorted(
        [(t.astimezone(tz).timestamp(), True) for t in rises]
        + [(t.astimezone(tz).timestamp(), False) for t in sets]
    ):
        if is_day:
            seconds += instant - cursor
        cursor, is_day = instant, entering_day
    if is_day:
        seconds += end.timestamp() - cursor

    twilight = phases
    times, codes = almanac.find_discrete(d0, d1, twilight)
    names = ["night", "astronomical", "nautical", "civil", "day"]
    # List actual events only; midnight is a day boundary, not a transition.
    stages = []
    previous = int(twilight(d0))
    for t, code in zip(times, codes):
        code = int(code)
        # Sunrise/set below use the same exact events as daylight duration.
        if code != 4 and previous != 4:
            stages.append({"time": iso(t), "stage": names[code], "phase": names[code]})
        previous = code
    for stage, events in (("sunrise", rises), ("sunset", sets)):
        for t in events:
            altitude, azimuth, _ = observer.at(t).observe(sun).apparent().altaz()
            stages.append({"time": iso(t), "stage": stage, "az": float(azimuth.degrees), "phase": "day" if stage == "sunrise" else "civil"})
    stages += [{"time": iso(t), "stage": "solar_noon", "phase": names[int(phases(t))]}
               for t in almanac.find_transits(observer, sun, d0, d1)]
    stages.sort(key=lambda item: datetime.fromisoformat(item["time"]).timestamp())
    # Sample the actual station-local day, including 23/25-hour DST days.
    # Include exact rise/set instants so the path meets their horizon ticks.
    boundaries = {t.astimezone(tz).timestamp() for t in rises + sets}
    epochs = sorted(set(range(int(start.timestamp()), int(end.timestamp()), 300))
                    | boundaries | {end.timestamp()})
    samples = ts.from_datetimes([datetime.fromtimestamp(epoch, tz) for epoch in epochs])
    altitudes, azimuths, _ = observer.at(samples).observe(sun).apparent().altaz()
    visibility = daylight(samples)
    path = [{"elevation": float(alt), "az": float(az),
             "visible": bool(visible) or epoch in boundaries}
            for epoch, alt, az, visible in zip(epochs, altitudes.degrees, azimuths.degrees, visibility)]
    return {"date": day, "timezone": tz_name, "day_start": start.isoformat(), "day_end": end.isoformat(),
            "day_length_seconds": round(seconds),
            "stages": stages, "path": path, "initial_phase": names[int(phases(d0))]}


def daily_solar(latitude, longitude, tz_name):
    latitude, longitude = float(latitude), float(longitude)
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Invalid station coordinates")
    day = datetime.now(ZoneInfo(tz_name)).date().isoformat()
    # Serialize cache misses and first-time ephemeris downloads across requests.
    with _lock:
        result = _daily(latitude, longitude, tz_name, day)
        ts, eph = _resources()
        now = ts.now()
        place = wgs84.latlon(latitude, longitude)
        altitude, azimuth, _ = (eph["earth"] + place).at(now).observe(eph["sun"]).apparent().altaz()
        code = int(_phase_function(eph["earth"] + place, eph["sun"])(now))
        current = {"elevation": float(altitude.degrees), "az": float(azimuth.degrees),
                   "visible": code == 4, "phase": ["night", "astronomical", "nautical", "civil", "day"][code]}
        return {**result, "current": current}
