import json

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
    # citrus_on gates the Irrigation nav link and the Station settings
    # fields (see base.html) -- when off (the default), weather_mqtt.py
    # never computes or publishes an irrigation decision either (see its
    # own CITRUS_MODE), so there's nothing there to look at.
    citrus_on = services.api_config_get().get("citrus_mode") == "on"
    response = render(request, template_name, {
        "lang": lang, "t": strings, "i18n_json": i18n_json, "active_page": active_page,
        "citrus_on": citrus_on,
    })
    return _no_store(response)


@require_GET
def weather_page(request):
    return _render_page(request, "weather.html", "weather")


@require_GET
def irrigation_page(request):
    return _render_page(request, "irrigation.html", "irrigation")


@require_GET
def stations_page(request):
    return _render_page(request, "stations.html", "stations")


@require_GET
def api_status(request):
    return _no_store(JsonResponse(services.api_status(), safe=False, **_JSON_PARAMS))


@require_GET
def api_history(request):
    try:
        n = int(request.GET.get("n", "14"))
    except ValueError:
        n = 14
    return _no_store(JsonResponse(services.api_history(n), safe=False, **_JSON_PARAMS))


@require_GET
def api_acks(request):
    return _no_store(JsonResponse(services.api_acks(), safe=False, **_JSON_PARAMS))


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_config(request):
    if request.method == "GET":
        return _no_store(JsonResponse(services.api_config_get(), safe=False, **_JSON_PARAMS))

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
    return _no_store(JsonResponse(services.api_clear_cache(), safe=False, **_JSON_PARAMS))
