from django.contrib import admin

from .models import IrrigationDecision, MetarReading


@admin.register(IrrigationDecision)
class IrrigationDecisionAdmin(admin.ModelAdmin):
    list_display = (
        "local_date", "decision_code", "decision_label",
        "commanded_percent", "commanded_pump_seconds", "schedule_armed",
    )
    list_filter = ("decision_code", "schedule_armed")
    ordering = ("-local_date",)


@admin.register(MetarReading)
class MetarReadingAdmin(admin.ModelAdmin):
    list_display = ("obs_time_local", "station", "temp_c", "rh_pct", "wind_mps", "raw_ob")
    list_filter = ("station",)
    ordering = ("-obs_time_epoch",)
