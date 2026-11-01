# Configuration file - WeatherCLient section

This configures the [weather client library](https://spello-consulting.github.io/sc-weather/) so that the app can download currently forecast data from OpenWeatherMap or Open Meteo. OpenWeatherMap requires an API key - if one isn't provdied, the weather client will fall back to the free Open Meteo.

The weather client will use the lat/long as configured in the [Location section](location.md) to determine the location to get the weather forecast for.

This weather data is used by the optional WeatherConstraint key in an Output configuration. See [Outputs](outputs.md) for more information. 

| Key | Description | 
|:--|:--|
| RefreshIntervalMin | How often to refresh the weather data (in minutes). |
| PreferredProvider | Preferred weather data provider. One of "owm" (OpenWeatherMap) or "open_meteo" (Open Meteo). The app will fall back to Open-Meteo if OWM is selected but no valid API key is provided. |
| OWMAPIKey | OpenWeatherMap API key (optional — falls back to Open-Meteo if blank). Can also be set in the OWM_API_KEY environment variable. |
