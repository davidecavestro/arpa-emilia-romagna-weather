"""Coordinator for consuming REST api endpoints on ARPA Emilia-Romagna Weather component."""
import asyncio
import json
import logging
import re
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from . import const
from .const import (
    API_BASE, CARDINAL_DIRECTIONS, DOMAIN, METEO_OSSERVATI_URL, REALTIME_URL
)

from . import metar
from .provenance import Provenance
from .thresholds import DayThresholds, NightThresholds

from astral import LocationInfo
from astral.sun import sun, elevation
from astral.moon import phase
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta, UTC
from math import radians, cos, sin, asin, sqrt


from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed
)


_LOGGER = logging.getLogger(__name__)

# how many brightness stations to try, ordered by distance
BRIGHTNESS_MAX_STATIONS = 5
# readings older than this no longer describe the current sky
BRIGHTNESS_MAX_AGE_HOURS = 6

# short-lived cache for the large realtime JSONL feed
_REALTIME_CACHE_SECONDS = 60
_REALTIME_CACHE = None



@dataclass
class SkyBrightnessReading:
    """A night sky brightness reading, with the station that observed it."""

    value: float
    station_name: str | None = None
    observed_at: str | None = None


class ArpaEmiliaRomagnaDataUpdateCoordinator(DataUpdateCoordinator):
    """Data update coordinator for ARPA Emilia-Romagna."""

    def __init__(self, hass, config_entry, update_interval):
        """Initialize the data update coordinator."""
        self.station_id = config_entry.data.get("station_id")
        # Entries created before the ARPAE rework hold a qa_stazioni id in
        # ``station_id``; the migration stores the actual meteo station in
        # ``meteo_station_id`` while keeping ``station_id`` stable for the
        # entity registry.
        self.meteo_station_id = (
            config_entry.data.get("meteo_station_id") or self.station_id)
        self.station_name = config_entry.data.get("station_name")
        self.zone_id = config_entry.data.get("zone_id")
        self.zone_name = config_entry.data.get("zone_name")
        self.latitude = config_entry.data.get("station_latitude")
        self.longitude = config_entry.data.get("station_longitude")
        self.infer_condition = config_entry.options.get(const.CONF_INFER_CONDITION)
        air_quality = config_entry.options.get(const.CONF_AIR_QUALITY)
        self.pm10_station_id = air_quality.get(const.CONF_PM10_STATION) if air_quality else None
        self.pm25_station_id = air_quality.get(const.CONF_PM25_STATION) if air_quality else None
        self.ozone_station_id = air_quality.get(const.CONF_OZONE_STATION) if air_quality else None

        observations = config_entry.options.get(const.CONF_OBSERVATIONS) or {}
        metar_station = observations.get(const.CONF_METAR_STATION)
        self.metar_station = metar_station if metar_station not in (None, "None") else None
        self.metar_fill_missing = observations.get(const.CONF_METAR_FILL_MISSING, True)
        self.day_condition = (config_entry.options.get(const.CONF_DAY_CONDITION)
                              or const.CONF_DAY_CONDITION_RADIATION_OR_FORECAST)
        self.night_condition = (config_entry.options.get(const.CONF_NIGHT_CONDITION)
                                or const.CONF_NIGHT_CONDITION_BRIGHTNESS_OR_FORECAST)
        self._metar_observation = None
        self._metar_fetched_at = None
        self._realtime_entries = None
        self._air_quality_cache = {}

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{self.station_id}",
            update_method=self._async_update_data,
            update_interval=update_interval,
        )

    async def _async_update_data(self):
        """Fetch the latest data from the API."""
        _LOGGER.debug("Fetching data")
        self._air_quality_cache = {}
        # The realtime file is large: download it once per refresh and let the
        # station and brightness helpers share the same snapshots.
        self._realtime_entries = await _fetch_realtime_entries()
        sensor_response = self.fetch_station_data(
            self.meteo_station_id, self._realtime_entries)
        forecast_response = _fetch_forecast_data(self.zone_id)
        ozone_response = self.fetch_air_quality_data(
            self.ozone_station_id, 'O3') if self.ozone_station_id and self.ozone_station_id != "None" else None
        pm10_response = self.fetch_air_quality_data(
            self.pm10_station_id, 'PM10') if self.pm10_station_id and self.pm10_station_id != "None" else None
        pm25_response = self.fetch_air_quality_data(
            self.pm25_station_id, 'PM2.5') if self.pm25_station_id and self.pm25_station_id != "None" else None

        sensor_data, sensor_data_raw, sources = await sensor_response
        forecast_data, forecast_raw = await forecast_response

        for key, response in (("pm10", pm10_response),
                              ("pm25", pm25_response),
                              ("ozone", ozone_response)):
            latest = await response if response else None
            sensor_data[key] = latest.get("MEDIA", None) if latest else None
            if latest:
                sources[key] = Provenance(
                    source=const.SOURCE_AIR_QUALITY_STATION,
                    name=latest.get("STAZIONE"),
                    observed_at=_arpae_isoformat(latest.get("DATA")),
                )

        # additional observations, for what the chosen station does not provide
        metar_observation = await self.async_metar_observation()
        if metar_observation and self.metar_fill_missing:
            _fill_missing_values(sensor_data, sources, metar_observation)

        # this one is a forecast, not an observation: it comes from the bulletin
        nearest_forecast = self._nearest_forecast(datetime.now(), forecast_data)
        if nearest_forecast:
            sources["precipitation_probability"] = Provenance(
                source=const.SOURCE_FORECAST,
                name=self.zone_name,
                observed_at=nearest_forecast["datetime"].isoformat(),
            )

        day_cfg = DayThresholds()
        night_cfg = NightThresholds()
        # Determine the current condition based on configuration
        match self.infer_condition:
            case const.CONF_INFER_CONDITION_FROM_SENSORS | None:
                sensor_data["condition"], sources["condition"] = await self._compute_condition_from_sensors(
                    self.latitude, self.longitude, sensor_data, forecast=forecast_data,
                    sources=sources)
            case const.CONF_INFER_CONDITION_FROM_SENSORS_WITH_CUSTOM_THRESHOLDS:
                opts = self.config_entry.options
                day_cfg = DayThresholds(
                    clear=opts.get(
                        const.CONF_INFER_CONDITION_DAY_CLEAR_THRESHOLD,
                        const.CONF_INFER_CONDITION_DAY_CLEAR_THRESHOLD_DEFAULT),
                    partly_cloudy=opts.get(
                        const.CONF_INFER_CONDITION_DAY_PARTLY_THRESHOLD,
                        const.CONF_INFER_CONDITION_DAY_PARTLY_THRESHOLD_DEFAULT),
                )
                night_cfg = NightThresholds(
                    clear=opts.get(
                        const.CONF_INFER_CONDITION_NIGHT_CLEAR_THRESHOLD,
                        const.CONF_INFER_CONDITION_NIGHT_CLEAR_THRESHOLD_DEFAULT),
                    partly_cloudy=opts.get(
                        const.CONF_INFER_CONDITION_NIGHT_PARTLY_THRESHOLD,
                        const.CONF_INFER_CONDITION_NIGHT_PARTLY_THRESHOLD_DEFAULT),
                )

                sensor_data["condition"], sources["condition"] = await self._compute_condition_from_sensors(
                    self.latitude, self.longitude, sensor_data, day_cfg, night_cfg,
                    forecast=forecast_data, sources=sources)
            case _:  # CONF_INFER_CONDITION_DISABLED or any other value
                pass  # Do not compute condition

        return {
            "forecast": forecast_data,
            "forecast_raw": forecast_raw,
            "sensors": sensor_data,  # Include sensor data like temperature
            "sensors_raw": sensor_data_raw,
            "sources": sources,  # Where each value comes from, keyed as in "sensors"
            "metar": metar_observation,  # Additional observations, when configured
        }

    async def async_metar_observation(self):
        """Return the latest METAR observation, refreshed at most every few minutes."""

        if not self.metar_station:
            return None

        now = datetime.now(UTC)
        if (self._metar_fetched_at is None
                or now - self._metar_fetched_at >= metar.MIN_FETCH_INTERVAL):
            observation = await metar.async_fetch_observation(self.metar_station)
            self._metar_fetched_at = now
            if observation is not None:
                self._metar_observation = observation

        # aerodromes with limited opening hours stop reporting overnight, and
        # keeping their last report would describe the sky of hours ago
        if self._metar_observation is None or not self._metar_observation.is_current:
            return None

        return self._metar_observation

    async def fetch_station_data(self, station_id, entries=None):
        """Fetch the newest snapshot of a station from the realtime JSONL endpoint.

        Every line of ``realtime.jsonl`` is a complete snapshot for one station,
        so all lines are parsed and the newest one matching the configured
        station is used. Values are converted from the WMO BUFR units the
        endpoint publishes to the units the sensors and weather entity expect.
        """
        if entries is None:
            entries = await _fetch_realtime_entries()
        if not entries:
            raise UpdateFailed("Empty response from realtime endpoint")

        target = str(station_id or "").strip().lower()
        configured_name = str(self.station_name or "").strip().lower()

        matches = _match_station_snapshots(entries, target, configured_name)

        # Stations owned by the consortia and by other third parties are not
        # part of the realtime JSONL feed; the meteo_osservati dataset behind
        # the official "grafici per stazione" page publishes them. Ask it
        # before falling back to a nearby realtime station, which would
        # otherwise hide the station the user actually selected.
        if not matches:
            candidate = self.meteo_station_id or self.station_name or station_id
            if candidate:
                meteo_station = await _fetch_meteo_osservati_station(candidate)
                if meteo_station is not None:
                    return _extract_meteo_osservati(meteo_station)

        # Entries created before the station list was exposed may hold an
        # unrelated id: fall back to the station nearest to the configured
        # coordinates, if any.
        if not matches:
            nearest = _nearest_station_snapshot(
                entries, self.latitude, self.longitude)
            if nearest is not None:
                matches = [nearest]

        if not matches:
            raise UpdateFailed(
                f"Station {station_id!r} not found in the ARPAE realtime feed; "
                "reconfigure the integration and select the station again"
            )

        latest = max(matches, key=lambda entry: entry.get("date", ""))
        meta_name, meta_lat, meta_lon = _station_meta(latest) or (None, None, None)
        meta_name = meta_name or self.station_name or str(station_id)

        measurements = _latest_measurements(latest)
        observed_at = latest.get("date")

        temperature_k = _as_float(measurements.get("B12101"))
        pressure_pa = _as_float(measurements.get("B10004"))
        wind_ms = _as_float(measurements.get("B11002"))
        wind_degrees = _as_float(measurements.get("B11001"))

        # B13011 is the accumulated rain, published both for the last 15
        # minutes and for the last hour. B14198 is only an occurrence flag
        # (0/1), so it must not be exposed as an intensity in mm/h.
        precipitation_15m = _as_float(_measurement_for_period(latest, "B13011", 900))
        precipitation_1h = _as_float(_measurement_for_period(latest, "B13011", 3600))
        precipitation = (
            precipitation_15m if precipitation_15m is not None else precipitation_1h)
        precipitation_intensity = (
            precipitation_1h if precipitation_1h is not None
            else (round(precipitation_15m * 4, 2)
                  if precipitation_15m is not None else None))

        # UV index (B22070, B22071, B22073, B22074) and leaf wetness (B22196)
        # are published by some stations; extract them when present.
        uv_index = None
        for uv_code in ("B22070", "B22071", "B22073", "B22074"):
            if uv_code in measurements:
                uv_index = measurements[uv_code]
                break
        leaf_wetness = _as_float(measurements.get("B22196"))

        extracted_data = {
            "temperature": round(temperature_k - 273.15, 2) if temperature_k is not None else None,
            "humidity": _as_float(measurements.get("B13003")),
            "wind_speed": round(wind_ms * 3.6, 2) if wind_ms is not None else None,
            "wind_bearing": wind_degrees,
            "wind_direction": _cardinal(wind_degrees),
            "visibility": _as_float(measurements.get("B22032")),
            "pressure": round(pressure_pa / 100, 2) if pressure_pa is not None else None,
            "brightness": _as_float(measurements.get("B07030")),
            "precipitation": precipitation,
            "precipitation_hourly": precipitation_intensity,
            "precipitation_cumulative_1h": precipitation_1h,
            "wind_gust": (round(_as_float(measurements["B11041"]) * 3.6, 2)
                          if _as_float(measurements.get("B11041")) is not None else None),
            "uv_index": _as_float(uv_index),
            "leaf_wetness": leaf_wetness,
        }

        # Temperature extremes over the snapshots published for this station.
        day = (observed_at or "")[:10]
        temperatures = []
        temperatures_by_day = {}
        for entry in matches:
            value = _as_float(_latest_measurements(entry).get("B12101"))
            if value is None:
                continue
            value = round(value - 273.15, 2)
            entry_day = (entry.get("date") or "")[:10]
            if entry_day:
                temperatures_by_day.setdefault(entry_day, []).append(value)
            if entry_day == day:
                temperatures.append(value)
        if temperatures:
            extracted_data["temperature_extremes"] = {
                "min": min(temperatures), "max": max(temperatures)}
        if temperatures_by_day:
            latest_day = max(temperatures_by_day)
            values = temperatures_by_day[latest_day]
            extracted_data["measured_temperature_extremes"] = {
                "date": latest_day, "min": min(values), "max": max(values)}

        # Rolling 15-minute samples; sum consecutive ones to get exact window totals.
        # Only populated when enough samples are available so a gap never understates the value.
        extracted_data.update(_realtime_precipitation_cumulatives(matches))

        extracted_data["last_update"] = datetime.now().isoformat()
        extracted_data["dt"] = _arpae_isoformat(observed_at)
        extracted_data["station_name"] = meta_name
        extracted_data["latitude"] = meta_lat
        extracted_data["longitude"] = meta_lon
        if extracted_data["latitude"] is None and latest.get("lat") is not None:
            extracted_data["latitude"] = latest["lat"] / 10000.0
        if extracted_data["longitude"] is None and latest.get("lon") is not None:
            extracted_data["longitude"] = latest["lon"] / 10000.0

        sources = {}
        for key in ("temperature", "humidity", "wind_speed", "wind_bearing",
                    "wind_direction", "visibility", "pressure", "precipitation",
                    "uv_index", "leaf_wetness"):
            sources[key] = Provenance(
                source=const.SOURCE_STATION,
                name=meta_name,
                observed_at=_arpae_isoformat(observed_at),
            )
        sources["brightness"] = Provenance(
            source=const.SOURCE_BRIGHTNESS_STATION,
            name=meta_name,
            observed_at=_arpae_isoformat(observed_at),
            rule=const.RULE_SKY_BRIGHTNESS,
        )

        # Some stations are published by both datasets, but report their air
        # temperature only through meteo_osservati. Fill whatever the realtime
        # snapshot lacks, without discarding the values it does provide.
        if extracted_data.get("temperature") is None:
            candidate = self.meteo_station_id or self.station_name or meta_name or station_id
            if candidate:
                meteo_station = await _fetch_meteo_osservati_station(candidate)
                if meteo_station is not None:
                    _fill_from_meteo_osservati(extracted_data, sources, meteo_station)

        # raw entries for debug / exposure
        sensor_data_raw = [item for item in latest.get("data", []) if item.get("vars")]
        return extracted_data, sensor_data_raw, sources

    async def fetch_air_quality_data(self, station_id, parametro):
        """Fetch the latest daily air quality value from ``qa_stazioni``.

        ``qa_stazioni`` returns the stations used by the ARPAE air quality
        bulletin. Every station has a ``dati`` mapping keyed by day
        (``YYYYMMDD``); each day holds ``pm10``, ``pm25`` and
        ``o3mediaorariamax`` among others.
        """
        key = str(station_id)
        if key not in self._air_quality_cache:
            self._air_quality_cache[key] = await _fetch_air_quality_station(key)
        station = self._air_quality_cache[key]
        if station is None:
            return None

        field = {
            "O3": "o3mediaorariamax",
            "PM10": "pm10",
            "PM2.5": "pm25",
        }.get(parametro, parametro)

        for day in sorted((station.get("dati") or {}), reverse=True):
            record = station["dati"][day]
            if not isinstance(record, dict):
                continue
            value = _as_float(record.get(field))
            if value is None:
                continue
            return {
                "MEDIA": value,
                "STAZIONE": station.get("nome", station.get("nome_stazione")),
                "DATA": day,
            }

        return None

    async def _compute_condition_from_sensors(self, lat, long, sensor_data, day_cfg=DayThresholds(), night_cfg=NightThresholds(), forecast=None, sources=None):
        """Compute the current condition based on sensor data."""

        dt = await self._local_dt(sensor_data.get("dt"))
        return await self.classify_sky(lat, long, dt, sensor_data, day_cfg, night_cfg, forecast, sources)

    async def _local_dt(self, dt_str):
        """Convert an ISO datetime string to a localized datetime object."""

        if not dt_str:
            return datetime.now(UTC).astimezone()

        try:
            parsed = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return datetime.now(UTC).astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone()

    async def classify_sky(self, lat, lon, dt, sensor_data, day_cfg: DayThresholds, night_cfg: NightThresholds, forecast=None, sources=None):
        """Classifies the sky condition.

        Based on:
        - Day: incident solar radiation (normalized to solar elevation)
        - Night: sky brightness (mag/arcsec²) corrected for Moon phase

        :param lat: latitude (float)
        :param lon: longitude (float)
        :param dt: datetime with tzinfo
        :param sensor_data: sensor data containing "ghi" [W/m²], temperature [°C], humidity [%], wind_speed [km/h], precipitation [mm], visibility [km]
        :param day_cfg: the day thresholds configuration
        :param night_cfg: the night thresholds configuration
        :param forecast: assembled forecast entries, used as a night fallback
        :param sources: the Provenance of each sensor value, keyed as sensor_data
        :return: a tuple with the condition string ["clear-night", "partlycloudy", "cloudy", "unknown"]
            and the Provenance of the data that decided it
        """

        temperature = sensor_data.get("temperature")
        humidity = sensor_data.get("humidity")
        wind_speed = sensor_data.get("wind_speed")
        precipitation = sensor_data.get("precipitation")
        visibility = sensor_data.get("visibility")

        ghi = sensor_data.get("ghi")

        def measured(rule, key):
            """Return the provenance of the value that decided the condition."""

            provenance = (sources or {}).get(key)
            if provenance is not None:
                # the deciding value may well come from an additional source
                return replace(provenance, rule=rule)

            return Provenance(
                source=const.SOURCE_STATION,
                name=sensor_data.get("station_name"),
                observed_at=_arpae_isoformat(sensor_data.get("dt")),
                rule=rule,
            )

        # Precipitation overrides everything
        if precipitation is not None and precipitation > 0:
            rain = measured(const.RULE_PRECIPITATION, "precipitation")
            if temperature is not None:
                # lowest threshold first, otherwise the second branch is dead code
                if temperature <= 0:
                    return "snowy", rain
                elif temperature <= 5:
                    return "snowy-rainy", rain
                if precipitation > 20:
                    return "pouring", rain
            return "rainy", rain

        # Visibility overrides wind
        if visibility is not None:
            if visibility < 1:
                return "fog", measured(const.RULE_VISIBILITY, "visibility")
            if visibility < 2 and humidity and humidity > 99:
                return "fog", measured(const.RULE_VISIBILITY, "visibility")

        if wind_speed and wind_speed > 30:
            return "windy", measured(const.RULE_WIND, "wind_speed")

        city = LocationInfo(latitude=lat, longitude=lon)
        s = sun(city.observer, date=dt.date(), tzinfo=dt.tzinfo)

        is_day = s["sunrise"] <= dt <= s["sunset"]

        if is_day:
            return await self._day_condition(city, dt, ghi, day_cfg, forecast, measured)

        else:
            return await self._night_condition(lat, lon, dt, night_cfg, forecast)

    async def _day_condition(self, city, dt, ghi, day_cfg: DayThresholds, forecast, measured):
        """Report the sky while the sun is above the horizon, as configured.

        Solar radiation is a local measurement and stays the default, but not
        every station carries a pyranometer: where it does not, the day has no
        observed condition at all, and an aerodrome is the only one left.
        """

        match self.day_condition:
            case const.CONF_DAY_CONDITION_UNKNOWN:
                return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

            case const.CONF_DAY_CONDITION_FORECAST:
                return self._forecast_condition(dt, forecast)

            case const.CONF_DAY_CONDITION_METAR:
                condition, provenance = await self._metar_condition(is_day=True)
                if condition is not None:
                    return condition, provenance
                # no report available: better a forecast than nothing
                return self._forecast_condition(dt, forecast)

            case const.CONF_DAY_CONDITION_RADIATION_OR_FORECAST:
                condition, provenance = self._radiation_condition(
                    city, dt, ghi, day_cfg, measured)
                if condition is not None:
                    return condition, provenance
                # no pyranometer, or the sun too low for the ratio to mean
                # anything: fall back to the bulletin
                return self._forecast_condition(dt, forecast)

            case const.CONF_DAY_CONDITION_RADIATION_OR_METAR:
                condition, provenance = self._radiation_condition(city, dt, ghi, day_cfg, measured)
                if condition is not None:
                    return condition, provenance
                # no pyranometer, or the sun too low for the ratio to mean
                # anything: the aerodrome observes the sky either way
                condition, provenance = await self._metar_condition(is_day=True)
                if condition is not None:
                    return condition, provenance
                # measured sources only: no forecast is served here
                return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

            case _:  # CONF_DAY_CONDITION_RADIATION, the default
                condition, provenance = self._radiation_condition(city, dt, ghi, day_cfg, measured)
                if condition is not None:
                    return condition, provenance
                return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

    def _radiation_condition(self, city, dt, ghi, day_cfg: DayThresholds, measured):
        """Report the sky from the incident solar radiation, or (None, None)."""

        if ghi is None:
            return None, None

        # Sun elevation in degrees
        h_sun = elevation(city.observer, dt)

        if h_sun <= 0:
            # Sun below horizon (transitions)
            return None, None

        radiation = measured(const.RULE_SOLAR_RADIATION, "ghi")

        # Theoretical maximum irradiance (approximated)
        ghi_clear = 1000 * sin(radians(h_sun))

        # Normalization
        ghi_ratio = ghi / ghi_clear if ghi_clear > 0 else 0

        # Empirical thresholds (to be calibrated on ARPA data)
        if ghi_ratio > day_cfg.clear:
            return "sunny", radiation
        elif ghi_ratio > day_cfg.partly_cloudy:
            return "partlycloudy", radiation

        return "cloudy", radiation

    async def _night_condition(self, lat, lon, dt, night_cfg: NightThresholds, forecast=None):
        """Report the sky while the sun is below the horizon, as configured.

        No source works everywhere: the sky brightness network publishes daily
        batches, so its readings describe the previous night rather than the
        current one, and an aerodrome observes the sky in real time but from
        however far away it happens to be. The choice is therefore left to the
        user, defaulting to the historical behaviour.
        """

        match self.night_condition:
            case const.CONF_NIGHT_CONDITION_UNKNOWN:
                return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

            case const.CONF_NIGHT_CONDITION_FORECAST:
                return self._forecast_condition(dt, forecast, night=True)

            case const.CONF_NIGHT_CONDITION_METAR:
                condition, provenance = await self._metar_condition(is_day=False)
                if condition is not None:
                    return condition, provenance
                # no report available: better a forecast than nothing
                return self._forecast_condition(dt, forecast, night=True)

            case const.CONF_NIGHT_CONDITION_BRIGHTNESS:
                condition, provenance = await self._brightness_condition(lat, lon, dt, night_cfg)
                if condition is not None:
                    return condition, provenance
                return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

            case _:  # CONF_NIGHT_CONDITION_BRIGHTNESS_OR_FORECAST, the default
                condition, provenance = await self._brightness_condition(lat, lon, dt, night_cfg)
                if condition is not None:
                    return condition, provenance
                # the brightness network is published in daily batches, so at
                # night there is often no fresh reading: fall back to the bulletin
                return self._forecast_condition(dt, forecast, night=True)

    async def _brightness_condition(self, lat, lon, dt, night_cfg: NightThresholds):
        """Report the sky from the night sky brightness, or (None, None)."""

        reading = await self.get_night_sky_brightness(lat, lon)
        if reading is None:
            return None, None

        provenance = Provenance(
            source=const.SOURCE_BRIGHTNESS_STATION,
            name=reading.station_name,
            observed_at=reading.observed_at,
            rule=const.RULE_SKY_BRIGHTNESS,
        )

        illum = moon_illumination(dt)
        if illum > 50:
            offset = -2
        elif illum > 10:
            offset = -1
        else:
            offset = 0

        if reading.value + offset > night_cfg.clear:
            return "clear-night", provenance
        elif reading.value + offset > night_cfg.partly_cloudy:
            return "partlycloudy", provenance

        return "cloudy", provenance

    async def _metar_condition(self, is_day):
        """Report the sky from the configured aerodrome, or (None, None)."""

        observation = await self.async_metar_observation()
        condition = metar.condition(observation, is_day)
        if condition is None:
            return None, None

        return condition, Provenance(
            source=const.SOURCE_METAR,
            name=observation.name,
            observed_at=observation.observed_at,
            rule=const.RULE_CLOUD_COVER,
        )

    def _forecast_condition(self, dt, forecast, night=False):
        """Return the forecast condition closest to a datetime, with its provenance."""

        nearest = self._nearest_forecast(dt, forecast)
        if nearest is None:
            return "unknown", Provenance(source=const.SOURCE_UNKNOWN)

        condition = nearest["condition"]
        if night and condition in ("sunny", "clear"):
            condition = "clear-night"

        return condition, Provenance(
            source=const.SOURCE_FORECAST,
            name=self.zone_name,
            observed_at=nearest["datetime"].isoformat(),
            rule=const.RULE_FORECAST,
        )

    def _nearest_forecast(self, dt, forecast):
        """Return the forecast entry closest to the given datetime, or None."""

        candidates = [
            f for f in (forecast or [])
            if f.get("condition") and isinstance(f.get("datetime"), datetime)
        ]
        # half-day entries track the current sky better than the daily summary
        candidates = [f for f in candidates if f.get("type") == "twice_daily"] or candidates
        if not candidates:
            return None

        def as_utc(value):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

        target = as_utc(dt)
        return min(candidates, key=lambda f: abs(as_utc(f["datetime"]) - target))

    async def get_night_sky_brightness(self, lat, lon):
        """Return the nearest usable night sky brightness reading, or None."""

        entries = self._realtime_entries or await _fetch_realtime_entries()
        if not entries or lat is None or lon is None:
            return None

        # Keep the newest snapshot of every station that carries a brightness
        # reading, then pick the closest one that is recent enough.
        stations = {}
        for entry in entries:
            meta = _station_meta(entry)
            if not meta:
                continue
            name, station_lat, station_lon = meta
            if station_lat is None or station_lon is None:
                continue
            measurements = _latest_measurements(entry)
            brightness = _as_float(measurements.get("B07030"))
            if brightness is None or brightness <= 0:
                brightness = _as_float(measurements.get("B07031"))
            if brightness is None or brightness <= 0:
                continue
            observed_at = entry.get("date") or ""
            current = stations.get(name)
            if current is None or observed_at > current["observed_at"]:
                stations[name] = {
                    "nome": name,
                    "latitudine": station_lat,
                    "longitudine": station_lon,
                    "brightness": brightness,
                    "observed_at": observed_at,
                }

        nearest = sort_locations_by_distance(list(stations.values()), lat, lon)
        for station in nearest[:BRIGHTNESS_MAX_STATIONS]:
            name = station["nome"]
            observed_at = station["observed_at"]
            try:
                reading_dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if datetime.now(UTC) - reading_dt > timedelta(hours=BRIGHTNESS_MAX_AGE_HOURS):
                continue
            return SkyBrightnessReading(
                value=station["brightness"],
                station_name=name,
                observed_at=_arpae_isoformat(observed_at),
            )

        _LOGGER.debug("No usable sky brightness reading from the nearby stations")
        return None


async def _fetch_air_quality_station(station_id):
    """Return one ``qa_stazioni`` record by id, or None."""

    where = quote(json.dumps({"_id": {"$eq": str(station_id)}}))
    url = (
        f"{API_BASE}/qa_stazioni"
        f"?projection=%7B%22foto%22%3A0%7D&where={where}&max_results=1"
    )
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        if response.status != 200:
            return None
        data = await response.json()

    items = data.get("_items") or []
    return items[0] if items else None


async def _fetch_meteo_osservati_station(name):
    """Return one ``meteo_osservati`` record by station name, or None.

    This is the dataset behind the official ARPAE "grafici per stazione" page.
    It lists the stations of the regional network together with the ones owned
    by consortia and other third parties, which the realtime JSONL feed omits.
    """

    where = quote(json.dumps({"anagrafica.nome": str(name)}))
    url = f"{METEO_OSSERVATI_URL}?where={where}&max_results=1"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        if response.status != 200:
            return None
        data = await response.json()

    items = data.get("_items") or []
    return items[0] if items else None


# Default bulletin zone for a province: plain/coast unless the user chooses a
# different (e.g. mountain) zone afterwards via Reconfigure.
_ZONE_FOR_PROVINCE = {
    "PIACENZA": ("C13", "PC pianura"),
    "PARMA": ("C12", "PR pianura"),
    "REGGIO EMILIA": ("C11", "MO-RE pianura"),
    "MODENA": ("C11", "MO-RE pianura"),
    "BOLOGNA": ("C10", "BO pianura"),
    "FERRARA": ("C9", "FE pianura"),
    "RAVENNA": ("C7", "RA costa"),
    "FORLI-CESENA": ("C6", "FC-RA pianura"),
    "RIMINI": ("C5", "RN-FC costa"),
}


async def async_migrate_legacy_entry(hass, entry):
    """Return updated data for an entry created before the ARPAE rework.

    Those entries hold a ``qa_stazioni`` id in ``station_id``; it is an air
    quality station, not a meteo station. The nearest meteo station is used as
    the observation source and a plain/coast bulletin zone of the same
    province as the forecast zone. The original ids are kept so the entity
    registry stays stable, and the meteo station is stored in
    ``meteo_station_id``.
    """

    data = entry.data
    station_id = str(data.get("station_id") or "")
    if not station_id.isdigit():
        return None

    entries = await _fetch_realtime_entries()
    latest_by_station = {}
    stations_with_temperature = set()
    for snapshot in entries:
        meta = _station_meta(snapshot)
        if not meta or not meta[0]:
            continue
        if "B12101" in _latest_measurements(snapshot):
            stations_with_temperature.add(meta[0])
        current = latest_by_station.get(meta[0])
        if current is None or snapshot.get("date", "") > current.get("date", ""):
            latest_by_station[meta[0]] = snapshot

    # A previous migration may already have resolved this entry. Repair it
    # only when its station never reports air temperature, which is the
    # minimum needed by the weather entity.
    current_meteo = data.get("meteo_station_id")
    if current_meteo and current_meteo in stations_with_temperature:
        return None

    latitude = data.get("station_latitude")
    longitude = data.get("station_longitude")
    province = ""
    needs_zone = not str(data.get("zone_id") or "").startswith("C")
    if latitude is None or longitude is None or needs_zone:
        qa_station = await _fetch_air_quality_station(station_id)
        if qa_station:
            province = (qa_station.get("provincia") or "").strip().upper()
            coordinates = (qa_station.get("loc") or {}).get("coordinates") or []
            if len(coordinates) >= 2:
                longitude, latitude = coordinates[0], coordinates[1]
    if latitude is None or longitude is None:
        return None

    candidates = []
    for name, snapshot in latest_by_station.items():
        meta = _station_meta(snapshot)
        if not meta or meta[1] is None or meta[2] is None:
            continue
        codes = _latest_measurements(snapshot)
        # Brightness-only stations are numerous and often very close; only a
        # station that reports air temperature can feed the weather entity.
        has_temperature = "B12101" in codes
        candidates.append((
            not has_temperature,
            haversine(meta[1], meta[2], latitude, longitude),
            name, meta[1], meta[2],
        ))
    if not candidates:
        return None

    _, _, name, station_latitude, station_longitude = min(candidates)
    new_data = {
        **data,
        "meteo_station_id": name,
        "meteo_station_name": name,
        "station_name": name,
        "comune_name": name,
        "station_latitude": station_latitude,
        "station_longitude": station_longitude,
    }

    if needs_zone:
        zone = _ZONE_FOR_PROVINCE.get(province)
        if zone:
            new_data["zone_id"], new_data["zone_name"] = zone

    _LOGGER.info(
        "Migrated legacy entry %s to meteo station %s (zone %s)",
        entry.entry_id, name, new_data.get("zone_id"))
    return new_data


async def _fetch_realtime_entries(force=False):
    """Return every snapshot of the ARPAE realtime endpoint as a list of dicts.

    The endpoint serves newline-delimited JSON (one station snapshot per line),
    so the response is read as text and decoded line by line. The file weighs
    several megabytes, so a short-lived cache avoids downloading it twice when
    the setup migration and the first coordinator refresh run back to back.
    """

    global _REALTIME_CACHE

    now = time.monotonic()
    if (not force and _REALTIME_CACHE is not None
            and now - _REALTIME_CACHE[0] < _REALTIME_CACHE_SECONDS):
        return _REALTIME_CACHE[1]

    async with aiohttp.ClientSession() as session, session.get(REALTIME_URL) as response:
        if response.status != 200:
            raise UpdateFailed(f"Error fetching data: {response.status}")
        text = await response.text()

    entries = await asyncio.to_thread(_parse_realtime_text, text)
    _REALTIME_CACHE = (time.monotonic(), entries)
    return entries


def _parse_realtime_text(text):
    """Decode the newline-delimited realtime payload outside the event loop."""

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            _LOGGER.debug("Skipping malformed realtime JSONL line")
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _station_meta(entry):
    """Return (name, latitude, longitude) of a realtime snapshot, or None."""

    for item in entry.get("data", []):
        variables = item.get("vars") or {}
        if "B01019" not in variables:
            continue
        name = variables["B01019"].get("v")
        latitude = _as_float(variables.get("B05001", {}).get("v"))
        longitude = _as_float(variables.get("B06001", {}).get("v"))
        return name, latitude, longitude
    return None


def _match_station_snapshots(entries, target, configured_name):
    """Return the realtime snapshots whose station name matches the target."""

    def station_name(entry):
        """Return the B01019 station name of a snapshot, lower-cased."""
        meta = _station_meta(entry)
        return (meta[0] or "").strip().lower() if meta else ""

    matches = [entry for entry in entries if target and station_name(entry) == target]
    if not matches and configured_name:
        matches = [entry for entry in entries if station_name(entry) == configured_name]
    if not matches and target:
        matches = [entry for entry in entries if target in station_name(entry)]
    if not matches and configured_name:
        matches = [entry for entry in entries if configured_name in station_name(entry)]

    # Entries created before the station list was exposed may hold a name
    # with qualifiers, e.g. "ALBARETO (MODENA)": match on the significant
    # words as a last reasonable guess before giving up.
    if not matches:
        tokens = [
            token for token in re.split(r"[^a-z0-9]+", configured_name or target)
            if len(token) > 3
        ]
        if tokens:
            matches = [
                entry for entry in entries
                if any(token in station_name(entry) for token in tokens)
            ]
    return matches


def _nearest_station_snapshot(entries, latitude, longitude):
    """Return the nearest snapshot reporting temperature, or the nearest one."""

    if latitude is None or longitude is None:
        return None

    candidates = []
    for entry in entries:
        meta = _station_meta(entry)
        if not meta or meta[1] is None or meta[2] is None:
            continue
        codes = _latest_measurements(entry)
        distance = haversine(meta[1], meta[2], latitude, longitude)
        candidates.append(("B12101" not in codes, distance, entry))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _latest_measurements(entry):
    """Merge the B-codes of all observations of a snapshot; later values win."""

    measurements = {}
    for item in entry.get("data", []):
        for code, payload in (item.get("vars") or {}).items():
            if isinstance(payload, dict) and "v" in payload:
                measurements[code] = payload["v"]
    return measurements


def _measurement_for_period(entry, code, period):
    """Return the last value of a code accumulated over ``period`` seconds."""

    value = None
    for item in entry.get("data", []):
        payload = (item.get("vars") or {}).get(code)
        if not isinstance(payload, dict) or "v" not in payload:
            continue
        timerange = item.get("timerange") or []
        if len(timerange) >= 3 and timerange[2] == period:
            value = payload["v"]
    return value


def _realtime_precipitation_cumulatives(matches):
    """Return cumulative rainfall over 3 and 6 hours from realtime snapshots.

    The realtime feed publishes a rolling 15-minute accumulation (``B13011``
    with a 900-second timerange) for every station and keeps a few hours of
    snapshots. Summing consecutive 15-minute values yields exact window
    totals; a window is only returned when every sample it needs is present,
    so a data gap never understates the value.
    """

    samples = {}
    for entry in matches:
        try:
            observed_at = datetime.fromisoformat(
                str(entry.get("date")).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        value = _as_float(_measurement_for_period(entry, "B13011", 900))
        if value is not None:
            samples[observed_at] = value

    if not samples:
        return {}

    latest = max(samples)
    result = {}
    for hours, key in (
            (3, "precipitation_cumulative_3h"),
            (6, "precipitation_cumulative_6h")):
        expected = [
            latest - timedelta(minutes=15 * offset)
            for offset in range(hours * 4)
        ]
        if all(sample in samples for sample in expected):
            result[key] = round(
                sum(samples[sample] for sample in expected), 2)
    return result


def _as_float(value):
    """Return a value as float, or None when it is missing or not numeric."""

    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cardinal(degrees):
    """Return the cardinal direction matching a bearing in degrees."""

    if degrees is None:
        return None
    return CARDINAL_DIRECTIONS[int((degrees + 11.25) / 22.5) % len(CARDINAL_DIRECTIONS)]


def _meteo_osservati_latest_slot(station):
    """Return ``(observed_at, values)`` of the newest non-empty slot.

    ``meteo_osservati`` groups its values by UTC day (``YYYYMMDD``) and hour
    (``HHMM``); the official widget parses those keys with ``dayjs.utc``. Empty
    trailing slots are common, so they are skipped rather than reported as the
    latest observation.
    """

    latest = None
    for day, slots in (station.get("dati") or {}).items():
        if not isinstance(slots, dict):
            continue
        for time_key, values in slots.items():
            if not isinstance(values, dict):
                continue
            if all(value is None for value in values.values()):
                continue
            try:
                observed_at = datetime.strptime(
                    f"{day}{time_key}", "%Y%m%d%H%M").replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            if latest is None or observed_at > latest[0]:
                latest = (observed_at, values)
    return latest if latest is not None else (None, {})


def _meteo_osservati_temperatures(station):
    """Return ``{day: [temperatures]}`` of a ``meteo_osservati`` record."""

    by_day = {}
    for day, slots in (station.get("dati") or {}).items():
        if not isinstance(slots, dict):
            continue
        for values in slots.values():
            if not isinstance(values, dict):
                continue
            temperature = _as_float(values.get("temperatura_istantanea_2m"))
            if temperature is None:
                continue
            by_day.setdefault(str(day), []).append(temperature)
    return by_day


def _meteo_osservati_precipitation_cumulatives(station):
    """Return cumulative rainfall over 3, 6, 12, 24 hours and today.

    ``precipitazione_cumulata_1h`` is a rolling one-hour total, so the value
    published at the top of each hour is exactly the rainfall of the previous
    hour. Summing consecutive hourly values therefore gives the window total
    without double counting. A window is only returned when every hourly value
    it needs is present; missing data leaves the sensor unknown instead of
    understating the rainfall. "Today" follows the Italian local day, so it
    also works across the daylight-saving change.
    """

    samples = {}
    for day, slots in (station.get("dati") or {}).items():
        if not isinstance(slots, dict):
            continue
        for time_key, values in slots.items():
            if not isinstance(values, dict):
                continue
            value = _as_float(values.get("precipitazione_cumulata_1h"))
            if value is None:
                continue
            try:
                observed_at = datetime.strptime(
                    f"{day}{time_key}", "%Y%m%d%H%M").replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            if observed_at.minute:
                continue
            samples[observed_at] = value

    if not samples:
        return {}

    latest = max(samples)
    result = {}
    for hours, key in (
            (3, "precipitation_cumulative_3h"),
            (6, "precipitation_cumulative_6h"),
            (12, "precipitation_cumulative_12h"),
            (24, "precipitation_cumulative_24h")):
        expected = [
            latest - timedelta(hours=offset)
            for offset in range(hours)
        ]
        if all(sample in samples for sample in expected):
            result[key] = round(
                sum(samples[sample] for sample in expected), 2)

    local_latest = latest.astimezone(ZoneInfo("Europe/Rome"))
    midnight = local_latest.replace(hour=0, minute=0, second=0, microsecond=0)
    expected = []
    cursor = midnight + timedelta(hours=1)
    while cursor <= local_latest:
        expected.append(cursor.astimezone(UTC))
        cursor += timedelta(hours=1)
    if expected and all(sample in samples for sample in expected):
        result["precipitation_cumulative_today"] = round(
            sum(samples[sample] for sample in expected), 2)
    elif not expected and local_latest.hour == 0:
        # The sample at local midnight covers the previous day's last hour.
        result["precipitation_cumulative_today"] = 0.0
    return result


def _extract_meteo_osservati(station):
    """Map a ``meteo_osservati`` record to the coordinator values.

    The dataset publishes the same measurements as the realtime feed, but with
    descriptive names instead of WMO B-codes, and in the units documented by
    the official widget: °C, %, m/s, °, mm, hPa and W/m². Values the dataset
    does not measure are left as None, so the sensors stay ``unknown`` instead
    of inventing a reading.
    """

    anagrafica = station.get("anagrafica") or {}
    name = (anagrafica.get("nome") or station.get("_id") or "").strip() or None

    # GeoJSON coordinates are longitude first.
    coordinates = (anagrafica.get("geometry") or {}).get("coordinates") or []
    longitude = latitude = None
    if len(coordinates) >= 2:
        longitude, latitude = coordinates[0], coordinates[1]

    observed_at, values = _meteo_osservati_latest_slot(station)

    wind_ms = _as_float(values.get("velocita_vento_istantanea_10m"))
    wind_degrees = _as_float(values.get("direzione_vento_istantanea_10m"))
    precipitation_1h = _as_float(values.get("precipitazione_cumulata_1h"))
    radiation = _as_float(values.get("radiazione_media_oraria_alto"))

    extracted_data = {
        "temperature": _as_float(values.get("temperatura_istantanea_2m")),
        "humidity": _as_float(values.get("umidita_istantanea_2m")),
        "wind_speed": round(wind_ms * 3.6, 2) if wind_ms is not None else None,
        "wind_bearing": wind_degrees,
        "wind_direction": _cardinal(wind_degrees),
        "visibility": None,
        "pressure": _as_float(values.get("pressione_atmosferica")),
        "brightness": None,
        # The dataset publishes an hourly accumulation, which is also the last
        # available interval and the average intensity over that hour.
        "precipitation": precipitation_1h,
        "precipitation_hourly": precipitation_1h,
        "precipitation_cumulative_1h": precipitation_1h,
        "wind_gust": None,
        "uv_index": None,
        "leaf_wetness": None,
        # Mean global radiation over the hour, in W/m²: the same quantity the
        # condition computation expects as GHI.
        "ghi": radiation,
    }
    extracted_data.update(_meteo_osservati_precipitation_cumulatives(station))

    temperatures_by_day = _meteo_osservati_temperatures(station)
    if temperatures_by_day:
        latest_day = max(temperatures_by_day)
        latest_values = temperatures_by_day[latest_day]
        extracted_data["measured_temperature_extremes"] = {
            "date": f"{latest_day[:4]}-{latest_day[4:6]}-{latest_day[6:8]}",
            "min": min(latest_values),
            "max": max(latest_values),
        }
        if observed_at is not None:
            observed_day = observed_at.strftime("%Y%m%d")
            day_values = temperatures_by_day.get(observed_day, latest_values)
            extracted_data["temperature_extremes"] = {
                "min": min(day_values), "max": max(day_values)}

    extracted_data["last_update"] = datetime.now().isoformat()
    extracted_data["dt"] = observed_at.isoformat() if observed_at else None
    extracted_data["station_name"] = name
    extracted_data["latitude"] = latitude
    extracted_data["longitude"] = longitude

    observed_iso = observed_at.isoformat() if observed_at else None
    sources = {}
    for key in ("temperature", "humidity", "wind_speed", "wind_bearing",
                "wind_direction", "visibility", "pressure", "precipitation",
                "precipitation_cumulative_1h", "precipitation_cumulative_3h",
                "precipitation_cumulative_6h", "precipitation_cumulative_12h",
                "precipitation_cumulative_24h", "precipitation_cumulative_today",
                "uv_index", "leaf_wetness", "ghi"):
        sources[key] = Provenance(
            source=const.SOURCE_STATION,
            name=name,
            observed_at=observed_iso,
            rule=const.RULE_SOLAR_RADIATION if key == "ghi" else None,
        )

    # Raw values for debug / exposure, in the same shape as the realtime feed.
    sensor_data_raw = [
        {"vars": {variable: {"v": value}}}
        for variable, value in values.items()
    ]
    return extracted_data, sensor_data_raw, sources


def _fill_from_meteo_osservati(extracted_data, sources, station):
    """Complete a realtime snapshot with the values only meteo_osservati has.

    A handful of stations are present in both datasets but publish their air
    temperature only through meteo_osservati. The realtime values are kept as
    they are; only the missing ones are taken from the other dataset, along
    with their provenance.
    """

    meteo_data, _, meteo_sources = _extract_meteo_osservati(station)
    for key in (
            "temperature", "humidity", "wind_speed", "wind_bearing",
            "wind_direction", "visibility", "pressure", "precipitation",
            "precipitation_hourly", "precipitation_cumulative_1h",
            "uv_index", "leaf_wetness", "ghi"):
        if extracted_data.get(key) is None and meteo_data.get(key) is not None:
            extracted_data[key] = meteo_data[key]
            if key in meteo_sources:
                sources[key] = meteo_sources[key]

    for key in ("temperature_extremes", "measured_temperature_extremes"):
        if key not in extracted_data and meteo_data.get(key) is not None:
            extracted_data[key] = meteo_data[key]


def _values_from_observation(entry):
    """Map a single realtime JSONL observation (with B-codes) to the sensor values it feeds."""
    # Extract vars dict from the data entry
    vars_dict = entry.get("vars", {})

    result = {}

    # Station name
    if "B01019" in vars_dict:
        result["station_name"] = str(vars_dict["B01019"].get("v", ""))

    # Latitude (in 10000ths of a degree)
    if "B05001" in vars_dict:
        result["latitude"] = float(vars_dict["B05001"].get("v", 0)) / 10000.0

    # Longitude (in 10000ths of a degree)
    if "B06001" in vars_dict:
        result["longitude"] = float(vars_dict["B06001"].get("v", 0)) / 10000.0

    # Temperature (in Kelvin)
    if "B12101" in vars_dict:
        result["temperature"] = float(vars_dict["B12101"].get("v", 0))

    # Humidity (percentage)
    if "B13003" in vars_dict:
        result["humidity"] = int(vars_dict["B13003"].get("v", 0))

    # Precipitation (mm)
    if "B13011" in vars_dict:
        result["precipitation"] = float(vars_dict["B13011"].get("v", 0))

    # Wind speed (m/s)
    if "B22062" in vars_dict:
        result["wind_speed"] = float(vars_dict["B22062"].get("v", 0))

    # Wind direction (degrees)
    if "B22001" in vars_dict:
        result["wind_bearing"] = int(vars_dict["B22001"].get("v", 0))

    # Pressure (Pa)
    if "B10004" in vars_dict:
        result["pressure"] = float(vars_dict["B10004"].get("v", 0))

    # Visibility (km)
    if "B22032" in vars_dict:
        result["visibility"] = float(vars_dict["B22032"].get("v", 0))

    # UV Index
    if "B22070" in vars_dict:
        result["uv_index"] = int(vars_dict["B22070"].get("v", 0))
    elif "B22071" in vars_dict:
        result["uv_index"] = int(vars_dict["B22071"].get("v", 0))
    elif "B22073" in vars_dict:
        result["uv_index"] = int(vars_dict["B22073"].get("v", 0))
    elif "B22074" in vars_dict:
        result["uv_index"] = int(vars_dict["B22074"].get("v", 0))

    # Leaf wetness (%)
    if "B22196" in vars_dict:
        result["leaf_wetness"] = float(vars_dict["B22196"].get("v", 0))

    # Precipitation rate (mm/h)
    if "B14198" in vars_dict:
        result["precipitation_rate"] = float(vars_dict["B14198"].get("v", 0))

    # Date/time components
    if "B04001" in vars_dict:
        result["year"] = int(vars_dict["B04001"].get("v", 0))
    if "B04002" in vars_dict:
        result["month"] = int(vars_dict["B04002"].get("v", 0))
    if "B04003" in vars_dict:
        result["day"] = int(vars_dict["B04003"].get("v", 0))
    if "B04004" in vars_dict:
        result["hour"] = int(vars_dict["B04004"].get("v", 0))
    if "B04005" in vars_dict:
        result["minute"] = int(vars_dict["B04005"].get("v", 0))
    if "B04006" in vars_dict:
        result["second"] = int(vars_dict["B04006"].get("v", 0))

    # Brightness values
    if "B07030" in vars_dict:
        result["brightness"] = float(vars_dict["B07030"].get("v", 0))
    if "B07031" in vars_dict:
        result["brightness_secondary"] = float(vars_dict["B07031"].get("v", 0))

    return result


def _fill_missing_values(sensor_data, sources, observation):
    """Complete the station data with the values it does not provide."""

    for key, value in observation.values.items():
        if value is None or sensor_data.get(key) is not None:
            continue

        sensor_data[key] = value
        sources[key] = Provenance(
            source=const.SOURCE_METAR,
            name=observation.name,
            observed_at=observation.observed_at,
        )


def _measured_temperature_extremes(temperature_data):
    """Return the extremes measured on the last day the station reported.

    Both the readings and the bulletin are dated by ARPAE, so the day is taken
    as reported rather than converted, and the two can be matched directly.
    """

    values_by_day = {}
    for item in temperature_data:
        try:
            value = float(item.get("valore"))
        except (TypeError, ValueError):
            continue

        day = (item.get("dataora") or "")[:10]
        if day:
            values_by_day.setdefault(day, []).append(value)

    if not values_by_day:
        return None

    day = max(values_by_day)
    values = values_by_day[day]

    return {"date": day, "min": min(values), "max": max(values)}


def _arpae_isoformat(dataora):
    """Return an ARPAE ISO timestamp (``...Z``) as a normalised ISO string."""

    if not dataora:
        return None

    try:
        parsed = datetime.fromisoformat(str(dataora).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return dataora
    return parsed.isoformat()


def find_nearest_location(locations, target_lat, target_lon):
    """Find the nearest location from a list based on latitude and longitude."""
    return min(
        locations,
        key=lambda x: haversine(x['latitudine'], x['longitudine'], target_lat, target_lon)
    )

def sort_locations_by_distance(locations, target_lat, target_lon):
    """Sort locations by distance from the target coordinates."""
    return sorted(
        locations,
        key=lambda x: haversine(x['latitudine'], x['longitudine'], target_lat, target_lon)
    )

def haversine(lat1, lon1, lat2, lon2):
    """Calculate the distance in kilometers between two points."""
    # Convert coordinates to radians
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371  # Radius of Earth in kilometers

    return c * r

def mj_to_wm2(mj_per_m2, seconds=3600):
    """Convert MJ/m² per interval to W/m² (mean irradiance).

    Default assumes an hourly interval.
    """
    return (mj_per_m2 * 1e6) / seconds


def moon_illumination(dt):
    """Approximate Moon illumination (0–100%).

    dt: datetime
    """
    p = phase(dt)  # 0=new, 14=full
    return (1 - cos(radians(p * 360 / 29.53))) / 2 * 100


condition_lookup = {
    "pouring": ["b6.png", "b7.png", "b8.png", "b9.png", "b10.png"],
    "clear-night": ["a1N"],
    "cloudy": ["a4", "a5", "a6"],
    "partlycloudy": ["a3"],
    "fog": ["f1.png", "f2.png", "f3.png", "f4.png"],
    "lightning-rainy": [
        "c1.png", "c2.png", "c3.png", "c5.png", "c6.png",
        "c7.png", "c8.png", "c9.png", "c10.png"
    ],
    "rainy": ["b1.png", "b2.png", "b3.png", "b4.png", "b5.png"],
    "snowy": [
        "d1.png", "d2.png", "d3.png", "d4.png", "d5.png", "d6.png",
        "d7.png", "d8.png", "d9.png", "d10.png"
    ],
    "snowy-rainy": [
        "e1.png", "e2.png", "e3.png", "e4.png", "e5.png", "e6.png",
        "e7.png", "e8.png", "e9.png", "e10.png"
    ],
    "sunny": ["a1", "a2"],
}

def _get_forecast_condition(condition_text):
    """Determine the forecast condition from text."""
    for condition, symbols in condition_lookup.items():
        if any(symbol in condition_text for symbol in symbols):
            return condition
    return None  # or a default value if no match is found

def _get_forecast_temperature_dict(entry):
    """Convert a temperature into a dict with templow if it's a range."""
    # Use a generator to filter keys starting with "temperatura"
    first_valid_value = next(
        (value for key, value in entry.items() if key.startswith(
            "temperatura") and value not in (None, "")),
        None
    )
    retval = None
    if (first_valid_value is None):
        retval = {"tempmiss": True}
    elif "/" in first_valid_value:
        min_temp, max_temp = map(float, first_valid_value.split('/'))
        retval = {"native_templow": min_temp, "native_temperature": max_temp}
    elif first_valid_value != "":
        retval = {"native_temperature": float(first_valid_value)}

    return retval

def _parse_precipitation_prob(prob):
    """Strip the suffix from  a probability text."""
    if prob == "":
        return None
    return int(re.sub('%$', '', prob))


def _get_forecast_type(intervallo):
    """Determine the forecast type (once or twice daily)."""
    if not intervallo:
        return "daily"

    return {
        "night/morning": "twice_daily",
        "afternoon/evening": "twice_daily",
    }.get(intervallo)

def _get_forecast_daytime_dict(intervallo):
    """Determine if the intervallo is about day or night."""
    if not intervallo:
        # return {}
        day_time = True
    else:
        day_time = {
            "night/morning": False, "afternoon/evening": True,
        }.get(intervallo)

    return {"is_daytime": day_time}

def _get_forecast_datetime(scadenza, intervallo):
    """Parse the date string into a date object."""
    date = datetime.strptime(scadenza, "%Y-%m-%d").date()

    # Map intervallo to specific times
    time_mapping = {
        "night/morning": "08:00:00",
        "afternoon/evening": "16:00:00",
        "": "12:00:00"
    }

    # Get the time for the given intervallo
    time_str = time_mapping.get(intervallo.lower())
    if not time_str:
        raise ValueError(f"Unknown intervallo: {intervallo}")

    # Combine date and time into a datetime object
    datetime_str = f"{date} {time_str}"
    return datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")

async def _fetch_forecast_data(zone_id):
    """Fetch the latest forecast bulletin from the ARPAE REST endpoint.

    The newest ``_items`` entry carries one ``bollettino`` per day. Each
    ``bollettino.regionale`` has ``testo`` (cielo/temperatura/vento/mare) and
    ``mappe`` with one icon per zone for ``mattina``/``pomeriggio``/
    ``sera_notte``. The per-zone minimum/maximum temperatures are in ``dati``.

    The bulletin is daily/slot-based, so the returned entries are daily and
    twice-daily only: no hourly or nowcast entries are invented.
    """
    url = f"{API_BASE}/meteo_bollettini/?sort=-_id&max_results=1"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        if response.status != 200:
            raise UpdateFailed(f"Error fetching data: {response.status}")
        data = await response.json()

    items = data.get("_items") or []
    if not items:
        return [], data
    item = items[0]

    forecasts = []
    for day_offset, day_key in enumerate(
            ("oggi", "domani", "dopodomani", "quartogiorno")):
        bollettino = (item.get(day_key) or {}).get("bollettino") or {}
        regionale = bollettino.get("regionale") or {}
        mappe = regionale.get("mappe") or {}
        testo = regionale.get("testo") or {}
        if not mappe:
            continue

        day_date = _parse_italian_date(bollettino.get("validita"))
        if day_date is None:
            day_date = datetime.now(UTC).date() + timedelta(days=day_offset)

        temperatures = _zone_temperatures(bollettino.get("dati") or {}, zone_id)
        description = testo.get("cielo") or ""

        for slot_key, hour, is_daytime in (
                ("mattina", 8, True),
                ("pomeriggio", 14, True),
                ("sera_notte", 22, False)):
            zone_info = (mappe.get(slot_key) or {}).get(zone_id)
            if not isinstance(zone_info, dict):
                continue

            condition = _condition_from_zone(zone_info)
            if condition is None:
                continue

            entry = {
                "datetime": datetime.combine(
                    day_date,
                    datetime.min.time().replace(hour=hour),
                    tzinfo=ZoneInfo("Europe/Rome"),
                ),
                "type": "twice_daily",
                "is_daytime": is_daytime,
                "condition": condition,
                "precipitation_probability": None,
                "forecast_reliability": "",
                "weather_description": description,
                "precipitation_description": zone_info.get("it", ""),
                "slot": slot_key,
                "zone": zone_id,
            }
            if is_daytime and temperatures:
                if temperatures.get("max") is not None:
                    entry["native_temperature"] = temperatures["max"]
                if temperatures.get("min") is not None:
                    entry["native_templow"] = temperatures["min"]
            forecasts.append(entry)

    # The newest bulletin may still be the previous day's issue: drop slots
    # that belong to a day already gone instead of showing them as forecast.
    today = datetime.now(ZoneInfo("Europe/Rome")).date()
    forecasts = [
        forecast for forecast in forecasts
        if forecast["datetime"].date() >= today
    ]

    if not forecasts:
        _LOGGER.warning(
            "Forecast zone %r is not present in the ARPAE bulletin; "
            "reconfigure the integration and select the zone again",
            zone_id,
        )

    return forecasts, data


_MONTHS_IT = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5,
    "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
    "novembre": 11, "dicembre": 12,
}

# ARPAE bulletin icon codes to Home Assistant weather conditions.
_ZONE_CONDITIONS = {
    "a001": "sunny",
    "a002": "partlycloudy",
    "a003": "partlycloudy",
    "a005": "partlycloudy",
    "a010": "cloudy",
    "a012": "cloudy",
    "a013": "cloudy",
    "a014": "rainy",
    "a017": "rainy",
    "b001": "sunny",
    "b002": "partlycloudy",
    "b005": "partlycloudy",
}


def _parse_italian_date(text):
    """Parse ``oggi, giovedì 5 novembre 2026`` into a date, or None."""

    if not text:
        return None

    # ISO date (``YYYY-MM-DD``): the bulletin also dates itself this way.
    iso_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(text).strip())
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return datetime(year, month, day).date()
        except ValueError:
            pass

    match = re.search(r"(\d{1,2})\s+([A-Za-zàèéìòù]+)\s+(\d{4})", str(text))
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS_IT.get(month_name.lower())
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day)).date()
    except ValueError:
        return None


def _zone_temperatures(dati, zone_id):
    """Return the min/max temperatures of a zone from the bulletin ``dati``."""

    if not zone_id or not str(zone_id).startswith("C"):
        return {}
    suffix = str(zone_id)[1:]
    minimum = _as_float((dati.get("temperatura_minima") or {}).get(f"t{suffix}", {}).get("dato"))
    maximum = _as_float((dati.get("temperatura_massima") or {}).get(f"T{suffix}", {}).get("dato"))
    return {"min": minimum, "max": maximum}


def _condition_from_zone(zone_info):
    """Return the Home Assistant condition of a bulletin zone icon."""

    icon = str(zone_info.get("icona") or "").strip().lower()
    if icon in _ZONE_CONDITIONS:
        return _ZONE_CONDITIONS[icon]

    text = str(zone_info.get("it") or "").strip().lower()
    # Precipitation mentions take priority over generic "covered" descriptors,
    # otherwise a value like ``coperto con pioviggini`` would wrongly fall to
    # cloudy before being treated as rainy.
    if any(word in text for word in ("temporale", "fulmini")):
        return "lightning-rainy"
    if any(word in text for word in ("pioggia", "pioviggine", "piogge")):
        return "rainy"
    if any(word in text for word in ("rovesci", "forte")):
        return "rainy"
    if "neve" in text:
        return "snowy"
    if "nebbia" in text:
        return "fog"
    if "coperto" in text or "nuvoloso" in text:
        return "cloudy"
    if "variabile" in text or "poco nuvoloso" in text:
        return "partlycloudy"
    if "sereno" in text:
        return "sunny"
    return None

def _assemble_forecast_dict(entry):

    static_props = {
        "datetime": _get_forecast_datetime(entry["scadenza"], entry["intervallo"]),
        "type": _get_forecast_type(entry["intervallo"]),
        "condition": _get_forecast_condition(entry["simbolo"]),
        "precipitation_probability": _parse_precipitation_prob(entry["prob precipitazioni"]),
        "forecast_reliability": entry["attendibilita"],
        "weather_description": entry["testo"],
        "precipitation_description": entry["precipitazioni"],
    }

    temperature_dict = _get_forecast_temperature_dict(entry)
    daytime_dict = {} if static_props["type"] == "daily" else _get_forecast_daytime_dict(
        entry["intervallo"])
    props_dict = static_props | temperature_dict | daytime_dict

    return props_dict

def _cumulate_since(precipitation_data, since):

    # Convert naive datetime to timezone-aware on +0100
    # then sum up recent entries newer than "since" limit
    return round(sum(float(p["valore"]) for p in precipitation_data
                     if datetime.strptime(p["dataora"] + " +0100", "%Y-%m-%dT%H:%M:%S %z").replace(tzinfo=timezone(timedelta(hours=1))) >= since), 2)
