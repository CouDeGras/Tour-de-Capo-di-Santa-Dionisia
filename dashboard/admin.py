from django.contrib import admin

from .models import MetarReading


@admin.register(MetarReading)
class MetarReadingAdmin(admin.ModelAdmin):
    list_display = (
        "obs_time_local",
        "station",
        "temp_c",
        "rh_pct",
        "wind_mps",
        "raw_ob",
    )
    list_filter = ("station",)
    ordering = ("-obs_time_epoch",)
