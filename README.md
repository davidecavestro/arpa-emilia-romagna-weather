# ARPA Emilia-Romagna Weather integration for Home Assistant
Home Assistant unofficial integration for ARPA Emilia-Romagna (ARPAE) realtime weather and forecast data.

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

> [!NOTE]
> Starting with Home Assistant 2026.11, HACS ships with Home Assistant as the **Marketplace** integration under **Settings > Marketplace**. The integration can still be installed via HACS on older Home Assistant versions.

This is a fork/clone of the original [**Arpa Veneto Weather**](https://github.com/davidecavestro/arpa-veneto-weather/) integration, adapted for ARPA Emilia-Romagna (ARPAE) data sources by a free AI, so expect some rough edges.


This integration retrieves forecast data from the ARPAE bulletin endpoint and current conditions from the ARPAE realtime observations feed, completing it with the `meteo_osservati` dataset for stations run by consortia and other third parties.

> [!TIP]
> This integration is maintained on a volunteer basis. If you find it useful and want to support its development, you can directly contribute with Pull Requests or fund the developer at [buymetokens.net/davidecavestro](https://buymetokens.net/davidecavestro) (or suggesting any other way to provide project-specific support).

For this integration you must choose a weather station for current conditions and one of the bulletin zones for the forecast.

#### This integration will set up the following platforms.

Platform | Description
-- | --
`weather` | A Home Assistant `weather` entity, with current data and twice-daily forecast data.
`sensor` | A Home Assistant `sensor` entity, with all available sensor data from the API.

## Installation

Starting with Home Assistant 2026.11, HACS ships with Home Assistant itself as the **Marketplace** integration under **Settings > Marketplace**: HACS itself doesn't need to be installed anymore. Before that version, install [HACS](https://hacs.xyz/) first.

1. Install **Marketplace** (Home Assistant 2026.11+) or **HACS** (older versions).
2. Register `davidecavestro/arpa-emilia-romagna-weather` as a custom repository:
   - In Home Assistant 2026.11+: follow the [Marketplace publishing documentation](https://contributors.home-assistant.io/marketplace/) to add custom repositories through **Settings > Marketplace**.
   - With HACS: add it as an [HACS custom repository](https://www.hacs.xyz/docs/faq/custom_repositories/).
3. Install the integration.

## Manual installation

1. Create a new folder in your configuration folder (where the `configuration.yaml` lives) called `custom_components`
2. Download the [latest version](https://github.com/davidecavestro/arpa-emilia-romagna-weather/releases) into the `custom_components` folder so that the full path from your config folder is `custom_components/arpa_emilia_romagna_weather/`
3. Restart Home Assistant.
4. Once Home Assistant is started, from the UI go to _Configuration > Integrations > Add Integrations_.
Search for "Arpa Emilia-Romagna Weather".
After selecting, it could take up to a minute.

## Configuration

To add Arpa Emilia-Romagna Weather to your installation, do the following:

- Go to _Settings_ then _Devices % services_.
- From the _Integrations_ tab click the ⊕ _Add Integration_ button on the lower right corner.
- Search for *ARPA Emilia-Romagna Weather* and click the integration.
- When loaded, there will be a configuration wizard, where you must enter:

  | Parameter | Required | Default Value | Description |
  | --------- | -------- | ------------- | ----------- |
  | `Weather Station` | Yes | None | Choose the ARPAE weather station for *current conditions*. The list merges the realtime observations feed with the ARPAE `meteo_osservati` dataset, so stations owned by consortia and other third parties are selectable too. |
  | `Forecast Zone` | Yes | First zone | Choose the zone of the ARPAE bulletin used for the forecast, for example `FC-RA pianura`. |

- Complete the wizard to save your data.
  If all goes well you should now have a new Weather entity with data from ArpaE Forecast

> [!TIP]
> You can configure multiple instances of the Integration, so properly name them to avoid confusion.
>
> Entries created before the ARPAE rework may still point at an old station or zone. Use the integration's _Reconfigure_ action to pick the station and forecast zone again.

### Third-party weather stations

The ARPAE open-data realtime feed (`realtime.jsonl`) only publishes part of the regional network. Stations owned by consortia — for example _Consorzio di Difesa di Bologna e Ferrara_ or _Bonifica Renana_ — and by other bodies are not in it, even though the official [grafici per stazione](https://www.arpae.it/it/temi-ambientali/meteo/dati-e-osservazioni/dati-in-tempo-reale/grafici-per-stazione) page displays them.

To make those stations selectable, the station list also reads the observable `meteo_osservati` REST dataset behind that page. When the configured station is not present in the realtime feed, the coordinator reads its measurements from `meteo_osservati` and maps them to the same sensors: air temperature, humidity, wind speed (converted from m/s to km/h), wind direction, hourly precipitation, pressure and solar radiation where the station measures them. Anything the dataset does not measure stays `unknown`.

### Preferences

Once you have configured a station, you can control some options from ⚙ (its _Configure_ action):
- **Expose JSON extra attribute for internal forecast data**: Expose additional attributes on the weather entity used internally for forecasts
- **Expose JSON extra attribute for raw original forecast data**: Expose additional attributes on the weather entity based on the data obtained from the remote api call
- **Expose extra attributes for raw original sensor data**: Expose as sensors the data obtaining from the remote api call
- **Compute the current condition**: Expose the weather state as computed from available metrics
- **Choose stations providing air-quality data**: Since the set of [stations differs](https://www.arpae.it/dati-ambientali/dati-in-diretta/aria/qualita-aria-dati-in-diretta) between PM10, PM2.5 and Ozone, the user can choose a specific station for each of them.
- **Current condition during the day**: Choose which source describes the sky while the sun is above the horizon
- **Current condition at night**: Choose which source describes the sky while the sun is below the horizon
- **Additional observations**: Optionally choose an aerodrome publishing METAR reports, to observe the sky and to complete the data the chosen station does not provide

## Compute the current weather condition

Since the stations don't provide data for current sky condition, the weather state has been historically left to _Unknown_. Since v0.5 this integration optionally computes the current sky condition with a best-effort approach based on available sensors:

<dl>
<dt>
day - sun above the horizon
</dt>
<dd>
it compares the actual sunlight (Global Horizontal Irradiance) with the maximum expected for the current sun position, as its elevation over the horizon and the azimuth actually reflects the <i>latitude</i>, the <i>season</i> and the <i>time</i> of the day.<br>
Based on the nearest realtime data available for <a href="https://www.arpae.it/dati-ambientali/dati-in-diretta/meteo-idro-nivo/variabili_meteo">solar radiation</a>.
It also uses data such as <i>precipitation</i>, <i>wind speed</i> and <i>visibility</i> to make the best possible estimate of the actual sky conditions.
</dd>

<dt>
night - sun below the horizon
</dt>
<dd>
it compares the actual night sky brightness with the maximum expected, based on the moon phase.<br>
Based on the realtime data available from the nearest station for the <a href="https://www.arpae.it/dati-ambientali/dati-in-diretta/luminosita-del-cielo/brillanza">night sky brightness</a>:
please note that this measure is not typically supplied directly by the chosen station, so we have to fallback to the nearest station providing this data.
</dd>
</dl>

> [!NOTE]
> The current weather condition computation is enabled by default since v0.12.<br>
> In order to disable it, go to <i>Configuration > Integrations > Arpa Emilia-Romagna Weather</i>.<br>
> Click on the gear on the station you are interested in.<br>
> Then choose <i>Compute the current condition: > <b>Don't compute</b></i>.

> [!TIP]
> Choose <i>Compute the current condition</i>: <i>From sensors using custom thresholds</b></i> to set custom thresholds for separately switching between <i>clear</i>, <i>partly cloudy</i> and <i>cloudy</i> during day and night.

### The current condition during the day

Solar radiation, where the feed provides a usable global-radiation value, gives a local near real-time measurement of the daytime sky. Most ARPAE realtime stations do not expose one, and the sun very low over the horizon also makes the ratio between measured and expected irradiance unreliable around sunrise and sunset. The default therefore uses radiation when it is available and falls back to the bulletin otherwise.

The choice is left to the user, under _Current condition during the day_:

| Option | Behaviour |
| ------ | --------- |
| _Solar radiation, forecast bulletin as a fallback_ | The default: use the irradiance ratio when available, otherwise the bulletin |
| _Solar radiation only_ | Use the irradiance ratio, and leave the condition unknown when the station measures no radiation |
| _Solar radiation, METAR observation as a fallback_ | Use the irradiance ratio when available, otherwise the cloud cover observed by the configured aerodrome. Both are observations, so no forecast is ever served |
| _METAR observation, forecast as a fallback_ | Always use the aerodrome, and the bulletin when no report is available |
| _Forecast bulletin_ | Always use the bulletin |
| _Leave it unknown_ | Report no condition unless the station measurements alone decide it (rain, fog, wind) |

### The current condition at night

The night is the harder part, because no single source works everywhere:

- the **night sky brightness** network publishes its readings in a single daily batch (around 09:30, standard time), so at night the latest reading normally describes the *previous* night rather than the current one, and is discarded as stale;
- the **forecast bulletin** is always available and covers the whole region, but it is a forecast, not an observation;
- a **METAR** report is an actual observation of the sky, published every 30 minutes, but it comes from the nearest aerodrome, which may be tens of kilometres away;
- some setups are better off with **no value at all** than with a value that is not measured.

The choice is therefore left to the user, under _Current condition at night_:

| Option | Behaviour |
| ------ | --------- |
| _Sky brightness, forecast as a fallback_ | The default, and the historical behaviour: use the brightness reading when it is fresh enough, otherwise the bulletin |
| _Sky brightness only_ | Use the brightness reading when fresh, otherwise leave the condition unknown |
| _METAR observation, forecast as a fallback_ | Use the cloud cover observed by the configured aerodrome, and the bulletin when no report is available |
| _Forecast bulletin_ | Always use the bulletin |
| _Leave it unknown_ | Report no condition unless the station measurements alone decide it (rain, fog, wind) |

Whatever the choice, the weather entity says which one was used through its `condition_source` attributes: see [Data provenance](#data-provenance).

## Additional observations

ARPAE stations do not observe the state of the sky, and several of them report neither visibility nor pressure: those sensors stay `unknown`. Aerodromes do observe all of it, and publish a [METAR](https://en.wikipedia.org/wiki/METAR) report every 30 minutes, with cloud cover reported layer by layer.

Under _Additional observations_ you can select one, from the list of aerodromes around the station, **sorted by distance**. When configured, it provides:

- the **cloud coverage** of the weather entity, as a percentage of the sky;
- the **current condition**, day or night, if you selected one of the METAR options above;
- any value **missing** from the chosen ARPAE station, typically visibility and pressure, unless you turn that off.

Values taken from a METAR are marked with `source: metar`, so they can always be told apart from the ones measured by the ARPAE station.

> [!IMPORTANT]
> Distance matters more than it seems: an aerodrome 60 km away describes the general state of the sky well, and local fog or low stratus poorly, which in the Po valley is exactly the typical winter situation. The distance of each aerodrome is shown in the selection list.

> [!TIP]
> Not every aerodrome reports around the clock: the smaller ones only publish during their opening hours, and the list says which ones are reporting at the moment. Choosing one is perfectly fine — reports older than 90 minutes are ignored, so outside those hours the values simply go back to being missing and the night condition falls back to whatever you configured. A nearby aerodrome reporting only by day can still be a better choice than a distant one reporting always.

> [!NOTE]
> Reports are retrieved from the [Aviation Weather Center](https://aviationweather.gov/data/api/) of the US National Weather Service, which requires no credentials, and are fetched at most once every 10 minutes, since a METAR is issued every 30.

## Data provenance

A single weather station in Home Assistant is fed by several distinct ARPAE networks: the [meteogram](https://www.arpae.it/dati-ambientali/dati-in-diretta/meteo-idro-nivo/variabili_meteo) network for the observations (through the realtime feed, or through the `meteo_osservati` dataset for third-party stations), the [air quality](https://www.arpae.it/dati-ambientali/dati-in-diretta/aria/qualita-aria-dati-in-diretta) network, the [night sky brightness](https://www.arpae.it/dati-ambientali/dati-in-diretta/luminosita-del-cielo/brillanza) network and the forecast bulletin. They have different locations, different sampling intervals and different publishing latencies, and not every value is an observation: some are forecasts.

Every entity therefore reports where its value comes from, as extra state attributes:

| Attribute | Description |
| --------- | ----------- |
| `source` | Origin of the value: `station`, `air_quality_station`, `sky_brightness_station`, `metar`, `forecast` or `unknown` |
| `source_name` | The reporting station, or the forecast zone |
| `source_observed_at` | When the value was observed at the origin, which can be well before the last update |

The weather entity exposes the same information for the current condition, prefixed with `condition_`, plus `condition_source_rule`: the criterion that decided the condition, one of `precipitation`, `visibility`, `wind`, `solar_radiation`, `sky_brightness`, `cloud_cover` or `forecast`.

> [!TIP]
> `condition_source` is the honest way to tell a measured condition from a forecast one: for example a `cloudy` state coming from `source: station, source_rule: solar_radiation` is measured sunlight, while `source: forecast` means the sky could not be measured and the bulletin was used instead.

## Archive of the night sky brightness

The [night sky brightness](https://www.arpae.it/dati-ambientali/dati-in-diretta/luminosita-del-cielo/brillanza) network publishes its readings in a single batch a day, around 09:30 standard time, so a night value only becomes available the morning after. That makes it unusable to describe the current sky, but it remains a genuine measurement of the night that has just passed.

Enabling _Archive the night sky brightness as long-term statistics_ imports those readings with their real timestamps, for the three brightness stations nearest to the chosen weather station:

- statistics are hourly, with mean, minimum and maximum, in `mag/arcsec²`;
- they are named `arpa_emilia_romagna_weather:sky_brightness_<station>` and can be charted with a `statistics-graph` card;
- the API exposes about the last 72 hours and they are all re-imported at every run, so a day spent offline recovers by itself, with no duplicates;
- the import runs at 11:15 local time, after the daily batch, and once at startup. The `arpa_emilia_romagna_weather.archive_brightness` service runs it on demand.

> [!NOTE]
> This is entirely separate from the real-time path: no entity and no automation is fed by these statistics. They serve to recalibrate the night thresholds on the values a given area actually reaches, and to check after the fact what the forecast fallback got right. Requires the `recorder` integration, which is enabled by default.

## Expose raw data

In order to get the raw data for advanced stuff in HA, from the UI go to _Configuration > Integrations > Arpa Emilia-Romagna Weather_.
<br>
Press the _CONFIGURE_ button on the integration entry.
<br>
Enable the option you need among:

<dl>
<dt>
Expose JSON extra attribute for internal forecast data
</dt>
<dd>
JSON representation of the internal data structure serving forecasts
</dd>
<dt>
Expose JSON extra attribute for raw original forecast data
</dt>
<dd>
JSON representation of the raw forecast data, as obtained from the bulletin remote endpoint
</dd>
<dt>
Expose extra attributes for raw original sensor data
</dt>
<dd>
Raw data from the meteogram remote endpoint, exposed both as single attributes (prefixed with <i>raw_</i> leading to related value) plus the list of raw objects representing last available sensor observation, along with description, unit of measure, date/time.
</dd>
</dl>

## Enable Debug Logging

If logs are needed for debugging or reporting an issue, use the following configuration.yaml:

```yaml
logger:
  default: error
  logs:
    custom_components.arpa-emilia-romagna-weather: debug
```

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by ARPAE (Agenzia Regionale per la Prevenzione e Protezione Ambientale dell'Emilia-Romagna).

The data and information provided through this integration are sourced from the ARPAE API, which is publicly available and licensed under the Creative Commons Attribution 4.0 Italy (CC BY 4.0) license unless otherwise specified.

### Terms of Use

All trademarks, logos, and distinctive signs visible on ARPAE's website are the property of ARPAE and cannot be used without prior authorization.

Any reproduction, distribution, modification, or use of ARPAE's content must attribute the source by citing "ARPAE" and providing the URL: http://www.arpae.it.

This project utilizes ARPAE's data strictly within the terms of the [Creative Commons Attribution 4.0 Italy license](https://creativecommons.org/licenses/by/4.0/deed.it).

### Limitations of Liability

ARPAE disclaims all responsibility for the accuracy, completeness, and timeliness of the data provided via their API, and for any issues arising from its use. Users should refer to ARPAE's official site for authoritative information.

For more details about ARPAE's copyright and licensing terms, visit their [website](http://www.arpae.it/).


<!-- Badges -->
[releases-shield]: https://img.shields.io/github/v/release/davidecavestro/arpa-emilia-romagna-weather?style=for-the-badge
[releases]: https://github.com/davidecavestro/arpa-emilia-romagna-weather/releases

[commits-shield]: https://img.shields.io/github/commit-activity/m/davidecavestro/arpa-emilia-romagna-weather?style=for-the-badge
[commits]: https://github.com/davidecavestro/arpa-emilia-romagna-weather/commits/main

[license-shield]: https://img.shields.io/github/license/davidecavestro/arpa-emilia-romagna-weather?style=for-the-badge

[hacsbadge]: https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge
[hacs]: https://github.com/hacs/default
