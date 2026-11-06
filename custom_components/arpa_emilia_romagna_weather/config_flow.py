"""Config flow to configure ARPA Emilia-Romagna Weather component."""
import json
import logging
import re

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from .const import (
    DOMAIN,
    API_BASE,
    REALTIME_URL,
    CONF_EXPOSE_FORECAST_JSON,
    CONF_EXPOSE_FORECAST_RAW,
    CONF_EXPOSE_SENSORS_RAW,
    CONF_INFER_CONDITION,
    CONF_INFER_CONDITION_FROM_SENSORS_WITH_CUSTOM_THRESHOLDS as CUSTOM_THRESHOLDS,
    CONF_INFER_CONDITION_FROM_SENSORS,
    CONF_INFER_CONDITION_DISABLED,
    CONF_INFER_CONDITION_DAY_CLEAR_THRESHOLD as DAY_CLEAR_THRESHOLD,
    CONF_INFER_CONDITION_DAY_CLEAR_THRESHOLD_DEFAULT as DAY_CLEAR_THRESHOLD_DEFAULT,
    CONF_INFER_CONDITION_DAY_PARTLY_THRESHOLD as DAY_PARTLY_THRESHOLD,
    CONF_INFER_CONDITION_DAY_PARTLY_THRESHOLD_DEFAULT as DAY_PARTLY_THRESHOLD_DEFAULT,
    CONF_INFER_CONDITION_NIGHT_CLEAR_THRESHOLD as NIGHT_CLEAR_THRESHOLD,
    CONF_INFER_CONDITION_NIGHT_CLEAR_THRESHOLD_DEFAULT as NIGHT_CLEAR_THRESHOLD_DEFAULT,
    CONF_INFER_CONDITION_NIGHT_PARTLY_THRESHOLD as NIGHT_PARTLY_THRESHOLD,
    CONF_INFER_CONDITION_NIGHT_PARTLY_THRESHOLD_DEFAULT as NIGHT_PARTLY_THRESHOLD_DEFAULT,
    CONF_AIR_QUALITY,
    CONF_PM10_STATION,
    CONF_PM25_STATION,
    CONF_OZONE_STATION,
    CONF_DAY_CONDITION,
    CONF_DAY_CONDITION_RADIATION,
    CONF_DAY_CONDITION_RADIATION_OR_FORECAST,
    CONF_DAY_CONDITION_RADIATION_OR_METAR,
    CONF_DAY_CONDITION_METAR,
    CONF_DAY_CONDITION_FORECAST,
    CONF_DAY_CONDITION_UNKNOWN,
    CONF_NIGHT_CONDITION,
    CONF_NIGHT_CONDITION_BRIGHTNESS_OR_FORECAST,
    CONF_NIGHT_CONDITION_BRIGHTNESS,
    CONF_NIGHT_CONDITION_METAR,
    CONF_NIGHT_CONDITION_FORECAST,
    CONF_NIGHT_CONDITION_UNKNOWN,
    CONF_OBSERVATIONS,
    CONF_METAR_STATION,
    CONF_METAR_FILL_MISSING,
    CONF_ARCHIVE_BRIGHTNESS,
)
from . import metar
from .coordinator import haversine

_LOGGER = logging.getLogger(__name__)

def _natural_key(text):
    """Sort labels containing numbers in a human order (C2 before C10)."""

    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", str(text))]


async def _fetch_realtime_entries():
    """Return the realtime JSONL snapshots, one dict per station snapshot."""

    async with aiohttp.ClientSession() as session, session.get(REALTIME_URL) as response:
        response.raise_for_status()
        text = await response.text()

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


async def fetch_stations():
    """Fetch the meteo stations from the verified realtime JSONL endpoint.

    The station name from ``B01019`` is used as id: it is stable, human
    readable, and it is what the coordinator matches against the feed.
    """
    entries = await _fetch_realtime_entries()

    stations = {}
    with_temperature = set()
    for entry in entries:
        name = latitude = longitude = None
        has_temperature = False
        for item in entry.get("data", []):
            variables = item.get("vars") or {}
            if "B12101" in variables:
                has_temperature = True
            if "B01019" not in variables:
                continue
            name = variables["B01019"].get("v")
            latitude = variables.get("B05001", {}).get("v")
            longitude = variables.get("B06001", {}).get("v")
        if not name:
            continue
        if has_temperature:
            with_temperature.add(name)
        observed_at = entry.get("date") or ""
        current = stations.get(name)
        if current is None or observed_at > current.get("_observed_at", ""):
            stations[name] = {
                "id": name,
                "nome": name,
                "latitudine": latitude,
                "longitudine": longitude,
                "_observed_at": observed_at,
            }

    # Brightness-only stations cannot feed the weather entity: offer the
    # stations that actually report air temperature, unless none do.
    meteo_stations = {
        name: station for name, station in stations.items()
        if name in with_temperature
    }
    if meteo_stations:
        stations = meteo_stations

    sorted_names = sorted(stations, key=_natural_key)
    id_to_name = {name: name for name in sorted_names}
    return id_to_name, {name: stations[name] for name in sorted_names}


async def fetch_forecast_zones():
    """Fetch the forecast zones of the newest ARPAE bulletin.

    The bulletin's ``regionale.mappe`` maps one icon per zone; the zone label
    in ``descrizione`` (e.g. ``FC-RA pianura``) is used as the human name.
    """
    url = f"{API_BASE}/meteo_bollettini/?sort=-_id&max_results=1"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        response.raise_for_status()
        json_data = await response.json()

    items = json_data.get("_items") or []
    if not items:
        raise ValueError("Empty forecast bulletin")

    bollettino = (items[0].get("oggi") or {}).get("bollettino") or {}
    mappe = (bollettino.get("regionale") or {}).get("mappe") or {}
    zones = {}
    for slot in ("mattina", "pomeriggio", "sera_notte"):
        for zone_id, zone_info in (mappe.get(slot) or {}).items():
            if not str(zone_id).startswith("C"):
                continue
            description = (zone_info.get("descrizione") or "").strip()
            if description:
                zones.setdefault(zone_id, description)

    if not zones:
        raise ValueError("No forecast zones in the bulletin")
    return dict(sorted(zones.items(), key=lambda item: _natural_key(item[0])))

async def fetch_air_quality_stations():
    """Fetch every air quality station from the observable ``qa_stazioni`` endpoint."""

    # The endpoint defaults to 25 results: ask explicitly for the full list.
    url = (
        f"{API_BASE}/qa_stazioni"
        "?projection=%7B%22foto%22%3A0%7D&max_results=1000"
    )
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        response.raise_for_status()
        json_data = await response.json()
    return json_data.get("_items", json_data.get("data", []))


def _air_station_options(data, cod_parametro):
    """Return the stations measuring a legacy parameter code.

    Maps the legacy parameter codes (7=Ozone, 25=PM10, 39=PM2.5) to the actual
    field names present in qa_stazioni ``dati[date]`` records.
    """

    param_field_map = {
        "7": "o3mediaorariamax",   # Ozone
        "25": "pm10",              # PM10
        "39": "pm25",              # PM2.5
    }
    target_field = param_field_map.get(cod_parametro, cod_parametro)

    filtered = []
    for entry in data:
        dati = entry.get("dati", {})
        if not isinstance(dati, dict):
            continue
        # Check if any date's record has the target field with a non-empty value
        for date_data in dati.values():
            if isinstance(date_data, dict) and date_data.get(target_field):
                filtered.append(entry)
                break

    sorted_stations = sorted(
        filtered, key=lambda x: x.get("nome", x.get("nome_stazione", "")))
    id_to_name = {
        entry.get("_id", entry.get("id")):
            f"{entry.get('nome', entry.get('nome_stazione'))} ({entry.get('provincia', '-')})"
        for entry in sorted_stations
    }
    stations = {item.get("_id", item.get("id")): item for item in sorted_stations}
    return id_to_name, stations


async def fetch_stations_air(cod_parametro):
    """Fetch the stations measuring a legacy air quality parameter code."""

    data = await fetch_air_quality_stations()
    return _air_station_options(data, cod_parametro)


class ArpaEmiliaRomagnaWeatherConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ARPA Emilia-Romagna Weather."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Implement OptionsFlow."""
        return ArpaEmiliaRomagnaWeatherOptionsFlowHandler()

    async def async_step_reconfigure(self, user_input=None):
        """Change the station and forecast zone of an existing entry."""
        errors = {}
        self.reconfiguring = self._get_reconfigure_entry()

        if user_input is not None:
            self.selected_station_id = user_input["station_id"]
            self.selected_station_name = self.station_id_to_name[self.selected_station_id]
            try:
                self.zone_id_to_name = await fetch_forecast_zones()
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning("Unable to fetch the forecast zones: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_zone()

        try:
            self.station_id_to_name, self.stations = await fetch_stations()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Unable to fetch the meteo stations: %s", err)
            errors["base"] = "cannot_connect"
            self.station_id_to_name, self.stations = {}, {}

        schema = vol.Schema(
            {vol.Required("station_id"): vol.In(self.station_id_to_name)}
        )

        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors)

    async def async_step_user(self, user_input=None):
        """Handle the first step: meteo station selection."""
        errors = {}
        self.reconfiguring = None

        if user_input is not None:
            self.selected_station_id = user_input["station_id"]
            self.selected_station_name = self.station_id_to_name[self.selected_station_id]
            try:
                self.zone_id_to_name = await fetch_forecast_zones()
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                _LOGGER.warning("Unable to fetch the forecast zones: %s", err)
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_zone()

        try:
            self.station_id_to_name, self.stations = await fetch_stations()
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Unable to fetch the meteo stations: %s", err)
            errors["base"] = "cannot_connect"
            self.station_id_to_name, self.stations = {}, {}

        schema = vol.Schema(
            {vol.Required("station_id"): vol.In(self.station_id_to_name)}
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_zone(self, user_input=None):
        """Handle the forecast zone selection step."""
        errors = {}

        if user_input is not None:
            self.selected_zone_id = user_input["zone_id"]
            self.selected_zone_name = self.zone_id_to_name[self.selected_zone_id]

            station = self.stations.get(self.selected_station_id, {})
            data = {
                "comune_id": self.selected_station_id,
                "comune_name": self.selected_station_name,
                "zone_id": self.selected_zone_id,
                "zone_name": self.selected_zone_name,
                "station_id": self.selected_station_id,
                "station_name": self.selected_station_name,
                "meteo_station_id": self.selected_station_id,
                "meteo_station_name": self.selected_station_name,
                "station_latitude": station.get("latitudine"),
                "station_longitude": station.get("longitudine"),
            }
            if self.reconfiguring is not None:
                return self.async_update_reload_and_abort(
                    self.reconfiguring,
                    title=f"Stazione di {self.selected_station_name}",
                    data=data,
                )
            return self.async_create_entry(
                title=f"Stazione di {self.selected_station_name}",
                data=data,
            )

        default_zone = next(iter(self.zone_id_to_name), None)
        schema = vol.Schema(
            {
                vol.Required("zone_id", default=default_zone): vol.In(
                    self.zone_id_to_name
                ),
            }
        )

        return self.async_show_form(
            step_id="zone",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "selected_station": self.selected_station_name,
            },
        )

class ArpaEmiliaRomagnaWeatherOptionsFlowHandler(config_entries.OptionsFlow):
    """Reconfigure integration options.

    Available options are:
        * update interval.
        * enable internal forecast JSON attribute
        * enable raw forecast JSON attribute
    """

    @property
    def config_entry(self):
        """Return the config entry."""
        return self.hass.config_entries.async_get_entry(self.handler)

    async def async_step_init(self, user_input=None):
        """Manage the options."""

        if user_input is not None:
            # store data in the flow instance
            self._stored_data = dict(user_input)

            if user_input.get(CONF_INFER_CONDITION) == CUSTOM_THRESHOLDS:
                return await self.async_step_thresholds()

            latitude = self.config_entry.data.get("station_latitude")
            longitude = self.config_entry.data.get("station_longitude")
            # backward compatibility: enrich existing config injecting coordinates if missing
            if latitude is None or longitude is None:
                # Fetch all stations
                _, stations = await fetch_stations()
                station_id = self.config_entry.data.get("station_id")
                station = stations.get(station_id) or {}
                new_data = {**self.config_entry.data,
                            "station_latitude": station.get("latitudine"),
                            "station_longitude": station.get("longitudine")
                            }
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                    options=self.config_entry.options,
                )

            return self.async_create_entry(title="Configure options", data=user_input)

        # Fetch air quality stations once and filter the three parameters locally
        air_stations = await fetch_air_quality_stations()
        self.ozone_stations, _ = _air_station_options(air_stations, "7")
        self.pm10_stations, _ = _air_station_options(air_stations, "25")
        self.pm25_stations, _ = _air_station_options(air_stations, "39")

        # Fetch the aerodromes issuing METAR around the station
        self.metar_stations = await self._async_metar_stations()
        configured_metar = self.config_entry.options.get(
            CONF_OBSERVATIONS, {}).get(CONF_METAR_STATION) or "None"
        if configured_metar != "None" and configured_metar not in self.metar_stations:
            # keep the current choice selectable even if the list is unavailable
            self.metar_stations = {configured_metar: configured_metar} | self.metar_stations

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_EXPOSE_FORECAST_JSON,
                        default=self.config_entry.options.get(
                            CONF_EXPOSE_FORECAST_JSON) or False
                    ): bool,
                    vol.Optional(
                        CONF_EXPOSE_FORECAST_RAW,
                        default=self.config_entry.options.get(
                            CONF_EXPOSE_FORECAST_RAW) or False
                    ): bool,
                    vol.Optional(
                        CONF_EXPOSE_SENSORS_RAW,
                        default=self.config_entry.options.get(
                            CONF_EXPOSE_SENSORS_RAW) or False
                    ): bool,
                    vol.Optional(
                        CONF_INFER_CONDITION,
                        default=self.config_entry.options.get(
                            CONF_INFER_CONDITION) or CONF_INFER_CONDITION_FROM_SENSORS
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                CONF_INFER_CONDITION_FROM_SENSORS,
                                CUSTOM_THRESHOLDS,
                                CONF_INFER_CONDITION_DISABLED,
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key=CONF_INFER_CONDITION
                        ),
                    ),
                    vol.Optional(
                        CONF_ARCHIVE_BRIGHTNESS,
                        default=self.config_entry.options.get(
                            CONF_ARCHIVE_BRIGHTNESS) or False
                    ): bool,
                    vol.Optional(
                        CONF_DAY_CONDITION,
                        default=self.config_entry.options.get(
                            CONF_DAY_CONDITION) or CONF_DAY_CONDITION_RADIATION_OR_FORECAST
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                CONF_DAY_CONDITION_RADIATION_OR_FORECAST,
                                CONF_DAY_CONDITION_RADIATION,
                                CONF_DAY_CONDITION_RADIATION_OR_METAR,
                                CONF_DAY_CONDITION_METAR,
                                CONF_DAY_CONDITION_FORECAST,
                                CONF_DAY_CONDITION_UNKNOWN,
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_DAY_CONDITION
                        ),
                    ),
                    vol.Optional(
                        CONF_NIGHT_CONDITION,
                        default=self.config_entry.options.get(
                            CONF_NIGHT_CONDITION) or CONF_NIGHT_CONDITION_BRIGHTNESS_OR_FORECAST
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                CONF_NIGHT_CONDITION_BRIGHTNESS_OR_FORECAST,
                                CONF_NIGHT_CONDITION_BRIGHTNESS,
                                CONF_NIGHT_CONDITION_METAR,
                                CONF_NIGHT_CONDITION_FORECAST,
                                CONF_NIGHT_CONDITION_UNKNOWN,
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key=CONF_NIGHT_CONDITION
                        ),
                    ),
                    vol.Required(CONF_OBSERVATIONS): section(
                        # the aerodromes are listed nearest first, with their distance:
                        # a far away one describes the local sky poorly
                        vol.Schema(
                            {
                                vol.Optional(
                                    CONF_METAR_STATION,
                                    default=configured_metar
                                ): vol.In({"None": "-"} | self.metar_stations),
                                vol.Optional(
                                    CONF_METAR_FILL_MISSING,
                                    default=self.config_entry.options.get(
                                        CONF_OBSERVATIONS, {}).get(CONF_METAR_FILL_MISSING, True)
                                ): bool,
                            }
                        ),
                        {"collapsed": True}
                    ),
                    vol.Required(CONF_AIR_QUALITY): section(
                        # for air stations, populate the list with fetched stations plus a "None" option
                        # also set the default value from existing config or "None" if not set
                        vol.Schema(
                            {
                                vol.Optional(
                                    CONF_PM10_STATION,
                                    default=self.config_entry.options.get(CONF_AIR_QUALITY, {}).get(
                                        CONF_PM10_STATION) or "None"
                                ): vol.In({"None": "-"} | self.pm10_stations),
                                vol.Optional(
                                    CONF_PM25_STATION,
                                    default=self.config_entry.options.get(CONF_AIR_QUALITY, {}).get(
                                        CONF_PM25_STATION) or "None"
                                ): vol.In({"None": "-"} | self.pm25_stations),
                                vol.Optional(
                                    CONF_OZONE_STATION,
                                    default=self.config_entry.options.get(CONF_AIR_QUALITY, {}).get(
                                        CONF_OZONE_STATION) or "None"
                                ): vol.In({"None": "-"} | self.ozone_stations)
                            }
                        ),
                        {"collapsed": True}
                    )
                }
            )
        )

    async def _async_station_coordinates(self):
        """Return the coordinates of the configured station, fetching them if needed."""

        latitude = self.config_entry.data.get("station_latitude")
        longitude = self.config_entry.data.get("station_longitude")
        if latitude is not None and longitude is not None:
            return latitude, longitude

        # entries created before the coordinates were stored
        _, stations = await fetch_stations()
        station = stations.get(self.config_entry.data.get("station_id")) or {}

        return station.get("latitudine"), station.get("longitudine")

    async def _async_metar_stations(self):
        """Return the aerodromes issuing METAR near the station, nearest first."""

        latitude, longitude = await self._async_station_coordinates()
        if latitude is None or longitude is None:
            return {}

        try:
            latitude = float(latitude)
            longitude = float(longitude)
            stations = await metar.async_fetch_stations(
                latitude,
                longitude,
                lambda lat, lon: haversine(lat, lon, latitude, longitude),
            )
        except (aiohttp.ClientError, TimeoutError, TypeError, ValueError) as err:
            # an optional source must not prevent the options from being edited
            _LOGGER.warning("Unable to list the nearby METAR aerodromes: %s", err)
            return {}

        return {station.icao: station.label for station in stations}

    async def async_step_thresholds(self, user_input=None):
        """Manage the thresholds options."""
        if user_input is not None:
            # Merge thresholds + step1 options
            self._stored_data.update(user_input)
            return self.async_create_entry(title="", data=self._stored_data)

        conf = self.config_entry.options
        return self.async_show_form(
            step_id="thresholds",
            data_schema=vol.Schema({
                vol.Optional(DAY_CLEAR_THRESHOLD,
                             default=conf.get(DAY_CLEAR_THRESHOLD, DAY_CLEAR_THRESHOLD_DEFAULT)
                             ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1, step=0.01, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(DAY_PARTLY_THRESHOLD,
                             default=conf.get(DAY_PARTLY_THRESHOLD, DAY_PARTLY_THRESHOLD_DEFAULT)
                             ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=1, step=0.01, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(NIGHT_CLEAR_THRESHOLD,
                             default=conf.get(NIGHT_CLEAR_THRESHOLD, NIGHT_CLEAR_THRESHOLD_DEFAULT)
                             ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=50, step=0.5, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Optional(NIGHT_PARTLY_THRESHOLD,
                             default=conf.get(NIGHT_PARTLY_THRESHOLD,
                                              NIGHT_PARTLY_THRESHOLD_DEFAULT)
                             ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=50, step=0.5, mode=selector.NumberSelectorMode.BOX)
                )
            })
        )
