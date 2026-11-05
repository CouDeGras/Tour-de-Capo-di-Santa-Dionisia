"""One-time import of the pre-SQL data/irrigation_history.csv and
data/metar_history.csv into IrrigationDecision/MetarReading.

Run once by hand after the SQL migration lands (`python3 manage.py
import_csv_history`), not wired into any service -- the CSV files are the
old storage, not an ongoing input. Safe to re-run: irrigation rows are
upserted by local_date (same key the CSV was keyed by); METAR rows are
skipped if a row with the same obs_time_epoch/logged_at_epoch pair already
exists, since the CSV had no unique key of its own to upsert against.
"""
import csv
from datetime import date

from django.core.management.base import BaseCommand

from dashboard.models import IrrigationDecision, MetarReading
from dashboard.services import DATA_DIR

IRRIGATION_CSV = DATA_DIR / "irrigation_history.csv"
METAR_CSV = DATA_DIR / "metar_history.csv"


def _float(v):
    if v in (None, "", "None", "null"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(v, default=None):
    f = _float(v)
    return default if f is None else int(f)


def _bool(v):
    return str(v or "").strip().lower() in ("1", "true", "yes")


class Command(BaseCommand):
    help = "Import the legacy irrigation_history.csv and metar_history.csv into the SQL tables."

    def handle(self, *args, **options):
        self._import_irrigation()
        self._import_metar()

    def _import_irrigation(self):
        if not IRRIGATION_CSV.exists():
            self.stdout.write(f"skip: {IRRIGATION_CSV} not found")
            return
        n = 0
        with IRRIGATION_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                IrrigationDecision.objects.update_or_create(
                    local_date=date.fromisoformat(row["local_date"]),
                    defaults={
                        "generated_at_local": row.get("generated_at_local", ""),
                        "source_mode": row.get("source_mode", ""),
                        "forecast_tmin24_c": _float(row.get("forecast_tmin24_c")),
                        "forecast_tmean72_c": _float(row.get("forecast_tmean72_c")),
                        "forecast_tmax24_c": _float(row.get("forecast_tmax24_c")),
                        "target_interval_days": _float(row.get("target_interval_days")),
                        "interval_demand_mm": _float(row.get("interval_demand_mm")),
                        "forecast_precip_local_day_mm": _float(row.get("forecast_precip_local_day_mm")),
                        "forecast_precip_next24_mm": _float(row.get("forecast_precip_next24_mm")),
                        "effective_rain_today_mm": _float(row.get("effective_rain_today_mm")),
                        "recent_rain_mm": _float(row.get("recent_rain_mm")),
                        "rain_postpone_threshold_mm": _float(row.get("rain_postpone_threshold_mm")),
                        "rain_reset_threshold_mm": _float(row.get("rain_reset_threshold_mm")),
                        "decision_code": row.get("decision_code", ""),
                        "decision_label": row.get("decision_label", ""),
                        "should_irrigate_now": _bool(row.get("should_irrigate_now")),
                        "commanded_percent": _int(row.get("commanded_percent"), 0),
                        "commanded_pump_seconds": _int(row.get("commanded_pump_seconds"), 0),
                        "event_completed": _bool(row.get("event_completed")),
                        "event_epoch": _int(row.get("event_epoch")),
                        "event_solar_anchor": row.get("event_solar_anchor", ""),
                        "last_event_epoch": _int(row.get("last_event_epoch")),
                        "next_watering_epoch": _int(row.get("next_watering_epoch"), 0),
                        "next_watering_iso_local": row.get("next_watering_iso_local", ""),
                        "projected_following_epoch": _int(row.get("projected_following_epoch"), 0),
                        "projected_following_iso_local": row.get("projected_following_iso_local", ""),
                        "schedule_armed": _bool(row.get("schedule_armed")),
                        "note": row.get("note", ""),
                    },
                )
                n += 1
        self.stdout.write(self.style.SUCCESS(f"irrigation_history.csv: upserted {n} rows"))

    def _import_metar(self):
        if not METAR_CSV.exists():
            self.stdout.write(f"skip: {METAR_CSV} not found")
            return
        n = 0
        skipped = 0
        with METAR_CSV.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                logged_at_epoch = _int(row.get("logged_at_epoch"), 0)
                obs_time_epoch = _float(row.get("obs_time_epoch"))
                if MetarReading.objects.filter(
                    logged_at_epoch=logged_at_epoch, obs_time_epoch=obs_time_epoch
                ).exists():
                    skipped += 1
                    continue
                MetarReading.objects.create(
                    logged_at_epoch=logged_at_epoch,
                    logged_at_local=row.get("logged_at_local", ""),
                    obs_time_epoch=obs_time_epoch,
                    obs_time_local=row.get("obs_time_local", ""),
                    age_minutes=_float(row.get("age_minutes")),
                    station=row.get("station", ""),
                    station_name=row.get("station_name", ""),
                    temp_c=_float(row.get("temp_c")),
                    dewpoint_c=_float(row.get("dewpoint_c")),
                    rh_pct=_float(row.get("rh_pct")),
                    wind_mps=_float(row.get("wind_mps")),
                    wind_dir_deg=row.get("wind_dir_deg") or None,
                    pressure_hpa=_float(row.get("pressure_hpa")),
                    vpd_kpa=_float(row.get("vpd_kpa")),
                    raw_ob=row.get("raw_ob", ""),
                )
                n += 1
        self.stdout.write(self.style.SUCCESS(f"metar_history.csv: imported {n} rows ({skipped} already present)"))
