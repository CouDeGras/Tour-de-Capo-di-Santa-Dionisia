# Weather dashboard

The Django dashboard displays airport observations, forecast comparisons from Yr.no, OpenWeatherMap and Open-Meteo, and local solar charts. The current-weather banner lists the available source descriptions separated by slashes, with source names on hover.

`weather.py --service` fetches all weather sources at startup and every three hours. It refreshes airport observations at each hourly :30 mark. Provider retries and cached forecast fallback remain available when a provider fails.

The manual refresh button runs `weather.py` once. Weather settings select the airport, language, solar projection/view and three-source chart layout. Observation history is stored in `MetarReading`; fetched forecasts live in `data/weather_cache.json`.

Run `python manage.py migrate` before starting the updated application. Start the dashboard with `python manage.py runserver` and the weather worker with `python weather.py --service`.

## Weather descriptors (backend only)

Each full weather cycle saves five descriptors: current conditions and the cumulative upcoming 3, 6, 12 and 24 hours. The hourly current-condition cycle refreshes METAR and OpenWeatherMap Current Weather, then refreshes all five using the saved forecasts without fetching forecasts again. Manual weather refresh uses the same full-cycle path. The banner displays the individual sources for the current descriptor; future descriptors remain backend-only.

OpenWeatherMap Current Weather (`/data/2.5/weather`) uses the existing `OWM_APPID` and airport coordinates. Its dated observation is used only for the current descriptor and expires after two hours. The separate `/forecast` feed supplies the future windows. The feeds have independent availability/error status, and a current-feed failure can reuse a still-valid saved observation without stopping the remaining weather updates. No extra calls are made by normal dashboard polling.

Every provider gets one equal vote. For each future window, its vote is the condition occupying the longest duration; at least 80% of the window must have known condition codes. Hourly providers therefore do not outweigh three-hourly providers. Missing and unknown values abstain. METAR is an observation and votes only for current conditions. A future forecast point is never backfilled into a current observation.

The most votes wins. Current-condition ties prefer METAR only if its condition is among the tied winners. Other ties use the condition ordering in the label catalog, then a stable key. This tie rule cannot override a majority. The output retains per-source votes, durations, coverage, vote counts, agreement and the applied tie-break rule; agreement is a fraction of eligible sources, not a probability of weather.

Forecast snapshots expire after six hours; METAR expires after two hours. A failed forecast refresh can reuse a still-valid snapshot, with the failed live fetch recorded. Snapshots are scoped to the airport and coordinates. Results are updated by the worker, not by polling the API, and marked stale after 90 minutes without an update. Windows use elapsed UTC time across daylight-saving changes.

The worker writes `data/condition_forecasts.json` (normalized codes and validity intervals) and `data/weather_descriptors.json` (the five results). Both are cleared by the existing weather-cache action. `GET /api/weather-descriptors` returns the saved results, with labels in the selected UI language; `/api/status` also includes them under `weather_descriptors`. An unfetched airport still returns five explicit unknown results. The project copies of the label catalog and schema live in `dashboard/weather_labels.json` and `dashboard/weather_labels.schema.json`; the Downloads originals are not modified.

The location widget looks up the airport city, using its coordinates to disambiguate nearby names through Nominatim. Its main name follows the selected UI language, with the English name beneath. The first successful result, including all available language variants, is stored permanently in the SQLite `LocalityName` table. It has no expiry and survives weather-cache clearing. Switching UI languages reuses this stored result; missing translations fall back to the local name.

Location lookup uses `/api/location`, independently of weather polling. An uncached locality normally needs one request (`namedetails=1`); a second English lookup is made only when the first result lacks an English name. Requests for uncached locations are serialized and spaced at least 1.1 seconds apart. Failed lookups fall back to the airport name and are not retried automatically in the page. The provider can be changed through `NOMINATIM_BASE_URL`. The widget links to OpenStreetMap attribution. Public-server usage policy: https://operations.osmfoundation.org/policies/nominatim/.

Locality selection is country-independent: compare the requested city against OSM name variants, disambiguate by airport proximity, and rank matching nearby records by language coverage and importance. Matching administrative boundaries are eligible regardless of whether their local rank is called city, province or state. Language fallback progressively removes BCP 47 subtags. Complementary translations from equally matching nearby city records are retained in the same permanent database entry.
