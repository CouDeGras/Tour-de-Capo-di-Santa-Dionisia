"""Airport weather observation history."""
from django.db import models


class LocalityName(models.Model):
    """Permanent Nominatim result, retaining every available name variant."""

    coordinates = models.CharField(max_length=48, unique=True)
    local_name = models.CharField(max_length=255)
    english_name = models.CharField(max_length=255)
    names = models.JSONField(default=dict)


class MetarReading(models.Model):
    """One row per successful METAR poll (hourly current-only refresh, and
    the tri-hourly full cycle, both of which fetch METAR -- see
    weather.py's append_metar_log_row, now write via this model
    instead). Plain append, no upsert -- a stale-fallback reading logged
    twice in a row just honestly shows the poll happened but nothing new
    arrived from the station.
    """

    logged_at_epoch = models.BigIntegerField()
    logged_at_local = models.CharField(max_length=40)
    obs_time_epoch = models.FloatField(null=True)
    obs_time_local = models.CharField(max_length=40, blank=True)
    age_minutes = models.FloatField(null=True)
    station = models.CharField(max_length=8, blank=True)
    station_name = models.CharField(max_length=128, blank=True)
    temp_c = models.FloatField(null=True)
    dewpoint_c = models.FloatField(null=True)
    rh_pct = models.FloatField(null=True)
    wind_mps = models.FloatField(null=True)
    # NOT numeric -- METAR reports "VRB" for variable wind direction, a real
    # value real stations return (straight from aviationweather.gov's API,
    # see weather.py's metar_latest_observation / latest.get("wdir")).
    wind_dir_deg = models.CharField(max_length=16, blank=True, null=True)
    pressure_hpa = models.FloatField(null=True)
    vpd_kpa = models.FloatField(null=True)
    raw_ob = models.TextField(blank=True)

    class Meta:
        ordering = ["obs_time_epoch"]
        indexes = [models.Index(fields=["obs_time_epoch"])]

    def __str__(self):
        return f"{self.station} {self.obs_time_local} {self.temp_c}°C"
