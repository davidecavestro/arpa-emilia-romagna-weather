"""Archive of the ARPAE night sky brightness as long-term statistics.

The brightness network publishes its readings in a single batch a day, around
09:30 standard time, so a night value only becomes available the morning after:
it can never describe the current sky, but it is a genuine measurement of the
night that has just passed.

Its honest use is therefore as history. Home Assistant cannot back-date states,
but it can import long-term statistics with their real timestamps, so the
readings are archived as hourly external statistics. The API exposes roughly the
last 72 hours, which are re-imported every time: writing the same hour twice
overwrites it, so a missed day recovers by itself.

This is entirely separate from the real-time path: no entity and no automation
is fed by these statistics. They serve to recalibrate the sky thresholds on the
values a given area actually reaches, and to check after the fact what the
forecast fallback got right.
"""

import json
import logging
from datetime import UTC, datetime

import aiohttp

from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import sort_locations_by_distance

_LOGGER = logging.getLogger(__name__)

UNIT_OF_MEASUREMENT = "mag/arcsec²"

# how many of the nearest stations to archive
DEFAULT_STATIONS = 3

# several stations of the network publish nothing at all, so looking only at the
# nearest ones would archive less than asked
MAX_STATIONS_TRIED = 6

# an hour described by fewer readings than this is not worth archiving
MIN_READINGS_PER_HOUR = 3


async def async_archive_brightness(hass, latitude, longitude, stations=DEFAULT_STATIONS):
    """Archive the brightness readings the API exposes, nearest stations first.

    :return: the number of hourly statistics imported
    """

    if "recorder" not in hass.config.components:
        _LOGGER.debug("The recorder is not enabled: nothing to archive")
        return 0

    imported = 0
    archived = 0
    async with aiohttp.ClientSession() as session:
        # The verified realtime endpoint is newline-delimited JSON: every line
        # is one station snapshot, so it must be read as text and split.
        url = "https://dati-simc.arpae.it/opendata/osservati/meteo/realtime/realtime.jsonl"
        async with session.get(url) as response:
            if response.status != 200:
                _LOGGER.debug("Unable to fetch realtime data: %s", response.status)
                return 0
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
    if not entries:
        _LOGGER.debug("Empty response from realtime endpoint")
        return 0

    # Collect the brightness snapshots per station. B07030/B07031 carry the
    # sky brightness readings, B05001/B06001 the coordinates in degrees.
    stations_data = {}
    for entry in entries:
        name = latitude_value = longitude_value = None
        measurements = {}
        for item in entry.get("data", []):
            variables = item.get("vars") or {}
            if "B01019" in variables:
                name = variables["B01019"].get("v")
                latitude_value = variables.get("B05001", {}).get("v")
                longitude_value = variables.get("B06001", {}).get("v")
            for code, payload in variables.items():
                if isinstance(payload, dict) and "v" in payload:
                    measurements[code] = payload["v"]

        if not name:
            continue
        try:
            brightness = float(measurements.get("B07030") or measurements.get("B07031"))
        except (TypeError, ValueError):
            continue
        # 0 marks a reading with no valid measurement, e.g. during daylight
        if brightness <= 0:
            continue

        station = stations_data.setdefault(name, {
            "codseqst": name,
            "nome_stazione": name,
            "latitudine": latitude_value,
            "longitudine": longitude_value,
            "readings": [],
        })
        station["readings"].append({
            "valore": brightness,
            "dataora": entry.get("date", ""),
        })

    stations_list = [
        station for station in stations_data.values()
        if station["latitudine"] is not None and station["longitudine"] is not None
    ]
    nearest = sort_locations_by_distance(stations_list, latitude, longitude)

    for station in nearest[:MAX_STATIONS_TRIED]:
        if archived >= stations:
            break

        hourly = _hourly_statistics(station.get("readings", []))
        if not hourly:
            _LOGGER.debug("No usable brightness reading from station %s", station["codseqst"])
            continue

        async_add_external_statistics(hass, _metadata(station), hourly)
        imported += len(hourly)
        archived += 1

    return imported


def _hourly_statistics(readings):
    """Aggregate the readings of a station into hourly statistics."""

    by_hour = {}
    for item in readings:
        try:
            value = float(item.get("valore"))
        except (TypeError, ValueError):
            continue

        # 0 marks a reading with no valid measurement, e.g. during daylight
        if value <= 0:
            continue

        try:
            observed_at = datetime.fromisoformat(
                str(item["dataora"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)

        hour = observed_at.replace(
            minute=0, second=0, microsecond=0).astimezone(UTC)
        by_hour.setdefault(hour, []).append(value)

    return [
        {
            "start": hour,
            "mean": round(sum(values) / len(values), 2),
            "min": min(values),
            "max": max(values),
        }
        for hour, values in sorted(by_hour.items())
        if len(values) >= MIN_READINGS_PER_HOUR
    ]


def _metadata(station):
    """Return the statistic metadata describing a brightness station."""

    name = station.get("nome_stazione") or str(station.get("codseqst"))

    return StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=f"Night sky brightness in {name}",
        source=DOMAIN,
        statistic_id=f"{DOMAIN}:sky_brightness_{slugify(name)}",
        unit_class=None,
        unit_of_measurement=UNIT_OF_MEASUREMENT,
    )
