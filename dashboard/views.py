import json
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .i18n import STRINGS

# JSON payloads carry accented text (French/Italian strings, station names)
# straight through rather than \uXXXX-escaping it, matching the previous
# json.dumps(..., ensure_ascii=False) behavior.
_JSON_PARAMS = {"json_dumps_params": {"ensure_ascii": False}}


def _no_store(response):
    # The previous stdlib server set this on every response it sent (HTML
    # and JSON alike) -- this is a single-garden dashboard where every
    # reader wants the current state, never a cached one.
    response["Cache-Control"] = "no-store"
    return response


def _render_page(request, template_name, active_page):
    lang = services.current_lang()
    strings = STRINGS[lang]
    i18n_json = json.dumps(strings, ensure_ascii=False).replace("</", "<\\/")
    site_cfg = services.api_config_get()
    trinity_on = site_cfg.get("trinity_mode") == "on"
    response = render(
        request,
        template_name,
        {
            "lang": lang,
            "t": strings,
            "i18n_json": i18n_json,
            "active_page": active_page,
            "trinity_on": trinity_on,
        },
    )
    return _no_store(response)


@require_GET
def weather_page(request):
    return _render_page(request, "weather.html", "weather")


@require_GET
def api_status(request):
    return _no_store(JsonResponse(services.api_status(), safe=False, **_JSON_PARAMS))


@require_GET
def api_solar(request):
    # Separate from weather status: an initial ephemeris download must not
    # prevent the rest of the dashboard from displaying weather.
    try:
        from .solar import daily_solar

        data = (
            json.loads(services.WEATHER_CACHE.read_text(encoding="utf-8"))
            if services.WEATHER_CACHE.exists()
            else {}
        )
        loc = data.get("location") or {}
        station = services.api_config_get()["station"]
        if (
            loc.get("lat") is None
            or loc.get("lon") is None
            or not loc.get("tz")
            or loc.get("station") != station
        ):
            return _no_store(JsonResponse({"available": False}))
        result = daily_solar(loc["lat"], loc["lon"], loc["tz"])
        return _no_store(
            JsonResponse(
                {"available": True, "station": station, **result}, **_JSON_PARAMS
            )
        )
    except Exception:
        logging.getLogger(__name__).exception("Daily solar calculation failed")
        return _no_store(JsonResponse({"available": False}, status=503))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_config(request):
    if request.method == "GET":
        return _no_store(
            JsonResponse(services.api_config_get(), safe=False, **_JSON_PARAMS)
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return HttpResponse("Invalid JSON", status=400, content_type="text/plain")
    try:
        saved = services.api_config_save(payload)
    except ValueError as e:
        return HttpResponse(str(e), status=400, content_type="text/plain")
    return _no_store(JsonResponse(saved, safe=False, **_JSON_PARAMS))


@csrf_exempt
@require_http_methods(["POST"])
def api_refresh(request):
    try:
        data = services.api_refresh()
    except RuntimeError as e:
        return HttpResponse(str(e), status=502, content_type="text/plain")
    return _no_store(JsonResponse(data, safe=False, **_JSON_PARAMS))


@csrf_exempt
@require_http_methods(["POST"])
def api_clear_cache(request):
    return _no_store(
        JsonResponse(services.api_clear_cache(), safe=False, **_JSON_PARAMS)
    )
