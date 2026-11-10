from django.urls import path

from . import views

urlpatterns = [
    path("", views.weather_page, name="weather"),
    path("api/solar", views.api_solar, name="api-solar"),
    path("api/status", views.api_status, name="api-status"),
    path("api/config", views.api_config, name="api-config"),
    path("api/refresh", views.api_refresh, name="api-refresh"),
    path("api/clear-cache", views.api_clear_cache, name="api-clear-cache"),
]
