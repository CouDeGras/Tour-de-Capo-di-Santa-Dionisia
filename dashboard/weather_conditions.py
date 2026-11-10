"""Provider-independent weather voting; no frontend or network calls."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

HORIZONS = (0, 3, 6, 12, 24)
PROVIDERS = ("metar", "yr", "owm", "open_meteo")
FORECAST_MAX_AGE_SECONDS = 6 * 3600
METAR_MAX_AGE_SECONDS = 2 * 3600
MIN_COVERAGE = 0.8


@lru_cache(maxsize=1)
def catalog():
    data = json.loads(
        Path(__file__).with_name("weather_labels.json").read_text(encoding="utf-8")
    )
    keys = data["conditions"]
    for key, value in keys.items():
        if any(not value["label"].get(lang) for lang in data["locales"]):
            raise ValueError(f"Missing weather translation: {key}")
    for source in data["sources"].values():
        references = list(source.get("map", {}).values())
        references += [rule["key"] for rule in source.get("weatherRules", [])]
        references += list(source.get("cloudCover", {}).values())
        references += [source.get("cloudFallback")]
        for key in references:
            if key is not None and key not in keys:
                raise ValueError(f"Unknown weather condition: {key}")
    return data


def condition_key(provider, code):
    if code is None:
        return "unknown"
    source = catalog()["sources"]["wmo" if provider == "open_meteo" else provider]
    code = str(code)
    if provider == "yr":
        for suffix in source["stripSuffixes"]:
            if code.endswith(suffix):
                code = code[: -len(suffix)]
                break
    return source["map"].get(code) or "unknown"


def _epoch(value):
    if isinstance(value, (int, float)):
        return float(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc).timestamp()
        if parsed.tzinfo is None
        else parsed.timestamp()
    )


def forecast_snapshot(provider, payload, now):
    """Retain original codes and nonoverlapping validity intervals, including now."""
    points = []
    if provider == "yr":
        for item in payload.get("properties", {}).get("timeseries", []):
            data = item.get("data") or {}
            for hours in (1, 6, 12):
                code = (data.get(f"next_{hours}_hours", {}).get("summary") or {}).get(
                    "symbol_code"
                )
                if code:
                    points.append((_epoch(item["time"]), hours * 3600, code))
                    break
    elif provider == "owm":
        for item in payload.get("list", []):
            weather = item.get("weather") or []
            points.append(
                (
                    _epoch(item["dt"]),
                    3 * 3600,
                    weather[0].get("id") if weather else None,
                )
            )
    elif provider == "open_meteo":
        hourly = payload.get("hourly") or {}
        codes = hourly.get("weather_code") or []
        for index, stamp in enumerate(hourly.get("time") or []):
            points.append(
                (_epoch(stamp), 3600, codes[index] if index < len(codes) else None)
            )
    else:
        raise ValueError(f"Unknown forecast provider: {provider}")
    points.sort(key=lambda item: item[0])
    samples = []
    for index, (start, duration, code) in enumerate(points):
        end = (
            min(start + duration, points[index + 1][0])
            if index + 1 < len(points)
            else start + duration
        )
        if end > start:
            samples.append(
                {
                    "start": start,
                    "end": end,
                    "condition": condition_key(provider, code),
                    "code": code,
                }
            )
    return {"fetched_at": now.timestamp(), "samples": samples}


def owm_current_snapshot(payload, now):
    """The Current Weather API's observation is never used as a forecast."""
    weather = payload.get("weather") or []
    if not weather or payload.get("dt") is None:
        raise ValueError("OpenWeatherMap current response has no dated condition")
    code = weather[0].get("id")
    observed_at = _epoch(payload["dt"])
    return {
        "fetched_at": now.timestamp(),
        "observed_at": observed_at,
        "feed": "current_weather",
        "samples": [
            {
                "start": observed_at,
                "end": observed_at + METAR_MAX_AGE_SECONDS,
                "condition": condition_key("owm", code),
                "code": code,
                "description": weather[0].get("description"),
            }
        ],
    }


def metar_condition(observation):
    """Present weather first; cloud cover only when actually reported."""
    source = catalog()["sources"]["metar"]
    raw = observation.get("raw_ob") or ""
    groups = (observation.get("wx_string") or "").split()
    # raw METAR is also available when the structured weather field is empty.
    if not groups:
        groups = raw.split()
    found = []
    for group in groups:
        if group in source["ignoreTokens"] or any(
            group.startswith(prefix) for prefix in source["ignorePrefixes"]
        ):
            continue
        for rule in source["weatherRules"]:
            if re.search(rule["pattern"], group):
                found.append(rule["key"])
                break
    if found:
        return max(
            found, key=lambda key: (catalog()["conditions"][key]["priority"], key)
        )
    # A nonempty structured weather group that we cannot decode is unknown,
    # never clear. It must not silently become a cloud-only vote.
    if observation.get("wx_string") and any(
        group not in source["ignoreTokens"]
        and not any(group.startswith(prefix) for prefix in source["ignorePrefixes"])
        for group in groups
    ):
        return "unknown"
    covers = [
        layer.get("cover")
        for layer in observation.get("clouds") or []
        if isinstance(layer, dict)
    ]
    covers += re.findall(r"\b(FEW|SCT|BKN|OVC|VV)(?:\d{3}|///)", raw)
    covers += [token for token in raw.split() if token in source["cloudCover"]]
    cloud_keys = [
        source["cloudCover"][cover] for cover in covers if cover in source["cloudCover"]
    ]
    return (
        max(cloud_keys, key=lambda key: (catalog()["conditions"][key]["priority"], key))
        if cloud_keys
        else "unknown"
    )


def _forecast_vote(snapshot, now, hours, max_age=FORECAST_MAX_AGE_SECONDS):
    if not snapshot:
        return None, "unavailable"
    age = now - snapshot["fetched_at"]
    if age < -300 or age > max_age:
        return None, "stale"
    observed_at = snapshot.get("observed_at")
    if observed_at is not None and not (-300 <= now - observed_at <= max_age):
        return None, "stale"
    samples = snapshot.get("samples") or []
    if hours == 0:
        sample = next(
            (
                sample
                for sample in samples
                if sample["start"] <= now + (300 if observed_at is not None else 0)
                and now < sample["end"]
            ),
            None,
        )
        if not sample:
            return None, "no_current_forecast"
        if sample["condition"] == "unknown":
            return None, "unknown_code"
        return {
            "condition": sample["condition"],
            "coverage": 1.0,
            "codes": [sample["code"]],
            "fetched_at": snapshot["fetched_at"],
            "observed_at": snapshot.get("observed_at"),
            "feed": snapshot.get("feed", "forecast"),
            "source_description": sample.get("description"),
        }, None
    end = now + hours * 3600
    durations = defaultdict(float)
    codes = []
    for sample in samples:
        overlap = max(0, min(end, sample["end"]) - max(now, sample["start"]))
        if overlap and sample["condition"] != "unknown":
            durations[sample["condition"]] += overlap
            if sample["code"] not in codes:
                codes.append(sample["code"])
    covered = sum(durations.values())
    coverage = covered / (hours * 3600)
    if coverage < MIN_COVERAGE:
        return None, "insufficient_coverage"
    winner = max(
        durations,
        key=lambda key: (durations[key], catalog()["conditions"][key]["priority"], key),
    )
    return {
        "condition": winner,
        "coverage": round(coverage, 4),
        "dominant_share": round(durations[winner] / covered, 4),
        "duration_seconds": dict(durations),
        "codes": codes,
        "fetched_at": snapshot["fetched_at"],
    }, None


def calculate_descriptors(station, now, forecasts, observation=None, live_errors=None):
    """One vote per source; periods are rolling [now, now + horizon)."""
    current_epoch = now.timestamp()
    live_errors = live_errors or {}
    items = []
    for hours in HORIZONS:
        votes, excluded = [], {}
        for provider in PROVIDERS:
            if provider == "metar":
                if hours:
                    excluded[provider] = "observation_only"
                    continue
                if observation and observation.get("station") not in (
                    None,
                    "",
                    station,
                ):
                    excluded[provider] = "station_mismatch"
                    continue
                age = current_epoch - float(
                    (observation or {}).get("obs_time_epoch") or 0
                )
                if not observation or age > METAR_MAX_AGE_SECONDS or age < -300:
                    excluded[provider] = "stale" if observation else "unavailable"
                    continue
                key = metar_condition(observation)
                if key == "unknown":
                    excluded[provider] = "unknown_condition"
                    continue
                vote = {
                    "condition": key,
                    "observed_at": observation["obs_time_epoch"],
                    "coverage": 1.0,
                }
            else:
                is_owm_current = provider == "owm" and hours == 0
                vote, reason = _forecast_vote(
                    forecasts.get("owm_current" if is_owm_current else provider),
                    current_epoch,
                    hours,
                    max_age=METAR_MAX_AGE_SECONDS
                    if is_owm_current
                    else FORECAST_MAX_AGE_SECONDS,
                )
                if not vote:
                    excluded[provider] = reason
                    continue
            votes.append(
                {
                    "source": provider,
                    "live_fetch_ok": not bool(
                        live_errors.get(
                            "owm_current"
                            if provider == "owm" and hours == 0
                            else provider
                        )
                    ),
                    **vote,
                }
            )
        counts = Counter(vote["condition"] for vote in votes)
        tie_break = None
        if counts:
            best = max(counts.values())
            tied = [key for key, count in counts.items() if count == best]
            metar = next(
                (vote["condition"] for vote in votes if vote["source"] == "metar"), None
            )
            if len(tied) == 1:
                winner = tied[0]
            elif not hours and metar in tied:
                winner, tie_break = metar, "metar_observation"
            else:
                winner = max(
                    tied,
                    key=lambda key: (catalog()["conditions"][key]["priority"], key),
                )
                tie_break = "condition_priority_then_key"
        else:
            winner = "unknown"
        items.append(
            {
                "horizon_hours": hours,
                "kind": "current" if hours == 0 else "upcoming_period",
                "start_epoch": current_epoch,
                "end_epoch": current_epoch + hours * 3600,
                "condition": winner,
                "labels": catalog()["conditions"][winner]["label"],
                "agreement": round(counts.get(winner, 0) / len(votes), 4)
                if votes
                else None,
                "voter_count": len(votes),
                "counts": dict(counts),
                "tie_break": tie_break,
                "votes": votes,
                "excluded_sources": excluded,
            }
        )
    return {
        "station": station,
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_epoch": current_epoch,
        "expires_at_epoch": current_epoch + 90 * 60,
        "period_semantics": "current_and_cumulative_upcoming_hours",
        "items": items,
    }
