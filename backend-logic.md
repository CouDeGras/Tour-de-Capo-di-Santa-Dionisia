# Weather dashboard

The Django dashboard displays airport observations, forecast comparisons from Yr.no, OpenWeatherMap and Open-Meteo, and local solar charts.

`weather.py --service` fetches all weather sources at startup and every three hours. It refreshes airport observations at each hourly :30 mark. Provider retries and cached forecast fallback remain available when a provider fails.

The manual refresh button runs `weather.py` once. Weather settings select the airport, language, solar projection/view and three-source chart layout. Observation history is stored in `MetarReading`; fetched forecasts live in `data/weather_cache.json`.

Run `python manage.py migrate` before starting the updated application. Start the dashboard with `python manage.py runserver` and the weather worker with `python weather.py --service`.
