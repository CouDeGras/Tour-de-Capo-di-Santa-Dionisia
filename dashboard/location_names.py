"""Resolve locality names once, then retain all language variants in SQLite."""

import logging
from difflib import SequenceMatcher
import math
import os
import re
import threading
import time
import unicodedata

import requests

from .models import LocalityName
from .chinese import simplified

USER_AGENT = (
    "CapoDiSantaDionisiaWeather/1.0 "
    "(https://github.com/CouDeGras/Tour-de-Capo-di-Santa-Dionisia)"
)
_lock = threading.Lock()
_last_request = 0.0
_logger = logging.getLogger(__name__)


def city_hint(name):
    """METAR registry names often append Intl, airport, or a terminal name."""
    city = str(name or "").split("/", 1)[0].split(",", 1)[0].strip()
    city = re.split(
        r"\s+(?:international|intl\.?|airport|aerodrome|airfield)\b",
        city,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return city.strip()


def _normalized(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return "".join(char for char in value if char.isalnum())


def _candidate_names(place):
    yield place.get("name", "")
    for key, value in (place.get("namedetails") or {}).items():
        if isinstance(value, str) and (
            key in {"name", "short_name", "alt_name", "official_name"}
            or key.startswith(("name:", "short_name:", "alt_name:"))
        ):
            yield from value.split(";")


def _name_relevance(place, city):
    requested = _normalized(city_hint(city))
    if not requested:
        return 0.0
    return max(
        (
            SequenceMatcher(None, requested, _normalized(name)).ratio()
            for name in _candidate_names(place)
        ),
        default=0.0,
    )


def _city_result(place, city=""):
    # Use the requested name and Nominatim's address-layer result, rather
    # than country-dependent interpretations of city/province/state ranks.
    return (
        isinstance(place, dict)
        and place.get("category", place.get("class")) in {"place", "boundary"}
        and _name_relevance(place, city) >= 0.82
    )


def _airport_name(name):
    return bool(
        re.search(
            r"\b(?:airport|aerodrome|airfield|cargo\s+area|intl)\b",
            name or "",
            re.IGNORECASE,
        )
    )


def _languages(lang):
    lang = "zh-Hans" if lang == "zhs" else lang
    parts = lang.replace("_", "-").split("-")
    return tuple("-".join(parts[:length]) for length in range(len(parts), 0, -1))


def _tagged_name(names, lang):
    folded = {key.casefold(): value for key, value in names.items()}
    for code in _languages(lang):
        name = folded.get("name:" + code.casefold())
        if isinstance(name, str) and name.strip():
            return name.strip()
    if lang == "zhs":
        return folded.get("name:zh-hant", "").strip()
    return ""


def _place_name(data, lang):
    name = _tagged_name(data.get("namedetails") or {}, lang) or data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    address = data.get("address") or {}
    for key in ("city", "town", "municipality", "village", "county", "state"):
        name = address.get(key)
        if isinstance(name, str) and name.strip():
            return name.strip()
    return ""


def _lookup(base_url, lat, lon, lang, city):
    global _last_request
    # Only uncached places reach here. This spaces their initial requests;
    # successful lookups never expire and are never periodically refreshed.
    delay = 1.1 - (time.monotonic() - _last_request)
    if delay > 0:
        time.sleep(delay)
    _last_request = time.monotonic()
    response = requests.get(
        base_url.rstrip("/") + "/search",
        params={
            "city": city_hint(city),
            "featureType": "city",
            "viewbox": f"{max(-180, lon - 2)},{min(90, lat + 2)},{min(180, lon + 2)},{max(-90, lat - 2)}",
            "bounded": 1,
            "limit": 5,
            "format": "jsonv2",
            "addressdetails": 1,
            "namedetails": 1,
            "accept-language": ",".join(_languages(lang)),
        },
        headers={"User-Agent": USER_AGENT},
        timeout=(3, 5),
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        data = [place for place in data if _city_result(place, city)]
    if not isinstance(data, list) or not data:
        raise ValueError("Nominatim has no matching city near this airport")
    longitude_scale = math.cos(math.radians(lat))

    def distance(place):
        return (float(place.get("lat", lat)) - lat) ** 2 + (
            (float(place.get("lon", lon)) - lon) * longitude_scale
        ) ** 2

    def rank(place):
        details = place.get("namedetails") or {}
        folded = {key.casefold(): value for key, value in details.items()}
        translation_rank = next(
            (
                len(_languages(lang)) - index
                for index, code in enumerate(_languages(lang))
                if isinstance(folded.get("name:" + code.casefold()), str)
                and folded["name:" + code.casefold()].strip()
            ),
            0,
        )
        coverage = sum(
            key.startswith("name:") and bool(value) for key, value in details.items()
        )
        return (
            _name_relevance(place, city),
            translation_rank,
            coverage,
            float(place.get("importance") or 0),
            -distance(place),
        )

    # Resolve geographic ambiguity before preferring richer translations.
    best_match = max(_name_relevance(place, city) for place in data)
    data = [place for place in data if _name_relevance(place, city) == best_match]
    nearest = min(data, key=distance)
    data = [
        place
        for place in data
        if (
            (float(place.get("lat", lat)) - float(nearest.get("lat", lat))) ** 2
            + (
                (float(place.get("lon", lon)) - float(nearest.get("lon", lon)))
                * longitude_scale
            )
            ** 2
        )
        <= 0.01
    ]
    data.sort(key=rank, reverse=True)
    selected = dict(data[0])
    merged = dict(selected.get("namedetails") or {})
    # A city node and its administrative boundary may carry different name
    # variants. Merge only equally matching records with nearby centers.
    for candidate in data[1:]:
        if _name_relevance(candidate, city) < _name_relevance(selected, city):
            continue
        center_distance = (
            float(candidate.get("lat", lat)) - float(selected.get("lat", lat))
        ) ** 2 + (
            (float(candidate.get("lon", lon)) - float(selected.get("lon", lon)))
            * longitude_scale
        ) ** 2
        if center_distance > 0.01:
            continue
        for key, value in (candidate.get("namedetails") or {}).items():
            if key.startswith("name:") and isinstance(value, str) and value.strip():
                merged.setdefault(key, value)
    selected["namedetails"] = merged
    return selected


def _display(place, lang):
    name = _tagged_name(place.names, lang) or place.local_name or place.english_name
    if lang == "zhs":
        name = simplified(name)
    return {
        "name": name,
        "english_name": place.english_name,
        "available": True,
    }


def locality_names(lat, lon, lang, fallback):
    """One permanent record per location, shared by every UI language."""
    fallback = city_hint(fallback)
    empty = {"name": fallback, "english_name": fallback, "available": False}
    try:
        lat, lon = float(lat), float(lon)
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not (-90 <= lat <= 90 and -180 <= lon <= 180)
        ):
            return empty
    except (TypeError, ValueError):
        return empty
    key = f"{lat:.5f},{lon:.5f}"
    with _lock:
        place = LocalityName.objects.filter(coordinates=key).first()
        if place and not _airport_name(place.english_name):
            return _display(place, lang)
        # Configurable without changing/rebuilding the application.
        base_url = os.environ.get(
            "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
        )
        try:
            data = _lookup(base_url, lat, lon, lang, fallback)
            selected = _place_name(data, lang)
            if not selected:
                raise ValueError("Nominatim returned no locality name")
            names = {
                k: v.strip()
                for k, v in (data.get("namedetails") or {}).items()
                if isinstance(v, str) and v.strip()
            }
            local = names.get("name") or data.get("name") or selected
            english = _tagged_name(names, "en")
            if not english:
                english_data = (
                    data
                    if lang == "en"
                    else _lookup(base_url, lat, lon, "en", fallback)
                )
                english = _place_name(english_data, "en")
            if not english:
                raise ValueError("Nominatim returned no English locality name")
            names.setdefault("name:" + ("zh-Hans" if lang == "zhs" else lang), selected)
            names["name:en"] = english
            place, _ = LocalityName.objects.update_or_create(
                coordinates=key,
                defaults={"local_name": local, "english_name": english, "names": names},
            )
            return _display(place, lang)
        except (requests.RequestException, ValueError, TypeError):
            # Failed calls do not become permanent false names. The frontend
            # does not retry in a loop; a later page load may try again.
            _logger.warning("Locality name lookup unavailable", exc_info=True)
            return empty
