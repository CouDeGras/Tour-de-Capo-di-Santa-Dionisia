"""SQL-backed replacements for the old data/irrigation_history.csv and
data/metar_history.csv flat files.

Written by weather_mqtt.py (a separate, non-web process -- see that file's
django.setup() bootstrap near the top) and read by dashboard/services.py for
the dashboard's API responses. This is the one shared schema definition both
sides use, instead of weather_mqtt.py hand-writing CSV rows that
dashboard/services.py then hand-parses back.
"""
from django.db import models


class IrrigationDecision(models.Model):
    """One row per calendar day -- upserted by local_date, same as the old
    CSV's keyed-by-date behavior (see weather_mqtt.py's apply_irrigation_schedule).
    The *_iso_local/*_local fields stay plain strings (not DateTimeField):
    they're already fully-formatted local-offset ISO strings by the time
    they get here, and running them through Django's UTC-normalizing
    DateTimeField would risk silently reformatting the offset on output for
    no benefit anyone asked for.
    """

    local_date = models.DateField(unique=True)
    # The ICAO station this decision was actually computed against (see
    # LocationInfo.station in weather_mqtt.py). Read-side filters on this so
    # switching the configured station (dashboard settings panel) doesn't
    # seed a new location's schedule off a different garden's prior history,
    # and so the dashboard's history table doesn't show decisions computed
    # for somewhere else as if they were continuous local history. Blank on
    # rows written before this field existed -- those predate per-station
    # tracking and are excluded by any station-filtered query rather than
    # guessed at.
    station = models.CharField(max_length=8, blank=True)
    generated_at_local = models.CharField(max_length=40)
    source_mode = models.CharField(max_length=64)
    forecast_tmin24_c = models.FloatField(null=True)
    forecast_tmean72_c = models.FloatField(null=True)
    forecast_tmax24_c = models.FloatField(null=True)
    # None when temperature_mode is FREEZE_HOLD (the CSV era stored this as
    # inf's string form there -- irrelevant now, null just means "no finite
    # interval this row").
    target_interval_days = models.FloatField(null=True)
    interval_demand_mm = models.FloatField(null=True)
    forecast_precip_local_day_mm = models.FloatField(null=True)
    forecast_precip_next24_mm = models.FloatField(null=True)
    effective_rain_today_mm = models.FloatField(null=True)
    recent_rain_mm = models.FloatField(null=True)
    rain_postpone_threshold_mm = models.FloatField(null=True)
    rain_reset_threshold_mm = models.FloatField(null=True)
    decision_code = models.CharField(max_length=32)
    decision_label = models.CharField(max_length=32)
    should_irrigate_now = models.BooleanField(default=False)
    commanded_percent = models.IntegerField(default=0)
    commanded_pump_seconds = models.IntegerField(default=0)
    event_completed = models.BooleanField(default=False)
    event_epoch = models.BigIntegerField(null=True)
    event_solar_anchor = models.CharField(max_length=16, blank=True)
    last_event_epoch = models.BigIntegerField(null=True)
    next_watering_epoch = models.BigIntegerField()
    next_watering_iso_local = models.CharField(max_length=40)
    projected_following_epoch = models.BigIntegerField()
    projected_following_iso_local = models.CharField(max_length=40)
    schedule_armed = models.BooleanField(default=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["local_date"]

    def __str__(self):
        return f"{self.local_date} {self.decision_code} {self.commanded_percent}%"


class MetarReading(models.Model):
    """One row per successful METAR poll (hourly current-only refresh, and
    the tri-hourly full cycle, both of which fetch METAR -- see
    weather_mqtt.py's append_metar_log_row, now write via this model
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
    # see weather_mqtt.py's metar_latest_observation / latest.get("wdir")).
    wind_dir_deg = models.CharField(max_length=16, blank=True, null=True)
    pressure_hpa = models.FloatField(null=True)
    vpd_kpa = models.FloatField(null=True)
    raw_ob = models.TextField(blank=True)

    class Meta:
        ordering = ["obs_time_epoch"]
        indexes = [models.Index(fields=["obs_time_epoch"])]

    def __str__(self):
        return f"{self.station} {self.obs_time_local} {self.temp_c}°C"
