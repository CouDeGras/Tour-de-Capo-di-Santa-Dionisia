from django.urls import path

from . import views

urlpatterns = [
    path("", views.weather_page, name="weather"),
    path("irrigation", views.irrigation_page, name="irrigation"),
    path("stations", views.stations_page, name="stations"),
    path("api/status", views.api_status, name="api-status"),
    path("api/history", views.api_history, name="api-history"),
    path("api/acks", views.api_acks, name="api-acks"),
    path("api/config", views.api_config, name="api-config"),
    path("api/refresh", views.api_refresh, name="api-refresh"),
    path("api/clear-cache", views.api_clear_cache, name="api-clear-cache"),
]
