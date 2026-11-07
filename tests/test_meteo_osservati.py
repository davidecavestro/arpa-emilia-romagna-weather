"""Tests for the meteo_osservati source, which carries third-party stations."""

import asyncio
from datetime import UTC, datetime

from custom_components.arpa_emilia_romagna_weather import coordinator
from custom_components.arpa_emilia_romagna_weather.config_flow import (
    _station_from_meteo_osservati,
)
from custom_components.arpa_emilia_romagna_weather.coordinator import (
    _extract_meteo_osservati,
    _meteo_osservati_latest_slot,
)


def make_station(name="BAGAGLIONE"):
    """Return a BAGAGLIONE-like record, as published by meteo_osservati."""

    return {
        "_id": "53209",
        "_updated": "Sat, 07 Nov 2026 14:53:24 GMT",
        "anagrafica": {
            "nome": name,
            "altitudine": -3.0,
            "geometry": {
                "type": "Point",
                "coordinates": [12.04094, 44.841143],
            },
            "variabili": [
                "direzione_vento_istantanea_10m",
                "precipitazione_cumulata_1h",
                "temperatura_istantanea_2m",
                "umidita_istantanea_2m",
                "velocita_vento_istantanea_10m",
            ],
        },
        "dati": {
            "20261106": {
                "2300": {
                    "precipitazione_cumulata_1h": None,
                    "temperatura_istantanea_2m": 9.0,
                    "umidita_istantanea_2m": 93.0,
                    "direzione_vento_istantanea_10m": 68.0,
                    "velocita_vento_istantanea_10m": 4.2,
                },
            },
            "20261107": {
                "1300": {
                    "precipitazione_cumulata_1h": None,
                    "temperatura_istantanea_2m": 11.3,
                    "umidita_istantanea_2m": 90.0,
                    "direzione_vento_istantanea_10m": 45.0,
                    "velocita_vento_istantanea_10m": 7.5,
                },
                "1400": {
                    "precipitazione_cumulata_1h": None,
                    "temperatura_istantanea_2m": 11.1,
                    "umidita_istantanea_2m": 86.0,
                    "direzione_vento_istantanea_10m": 45.0,
                    "velocita_vento_istantanea_10m": 7.8,
                },
                # The current hour is often published before its values are:
                # an empty slot must not become the latest observation.
                "1500": {
                    "precipitazione_cumulata_1h": None,
                    "temperatura_istantanea_2m": None,
                    "umidita_istantanea_2m": None,
                    "direzione_vento_istantanea_10m": None,
                    "velocita_vento_istantanea_10m": None,
                },
            },
        },
    }


def test_latest_slot_skips_empty_slots():
    """Ignore the newest slot while it has no measured values yet."""
    observed_at, values = _meteo_osservati_latest_slot(make_station())

    assert observed_at == datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    assert values["temperatura_istantanea_2m"] == 11.1


def test_latest_slot_ignores_malformed_slots():
    """Skip slots whose time key is not a four-digit HHMM."""
    station = make_station()
    station["dati"]["20261107"]["not-a-time"] = {
        "temperatura_istantanea_2m": 99.0,
    }

    observed_at, values = _meteo_osservati_latest_slot(station)

    assert observed_at == datetime(2026, 11, 7, 14, 0, tzinfo=UTC)
    assert values["temperatura_istantanea_2m"] == 11.1


def test_extract_meteo_osservati_maps_units_and_metadata():
    """Convert the dataset units and expose station metadata and provenance."""
    data, raw, sources = _extract_meteo_osservati(make_station())

    # Temperature and humidity are already in °C and %.
    assert data["temperature"] == 11.1
    assert data["humidity"] == 86.0
    # The dataset publishes wind in m/s; the sensors expect km/h.
    assert data["wind_speed"] == 28.08
    # A numeric bearing with the cardinal direction next to it, never "NE°".
    assert data["wind_bearing"] == 45.0
    assert data["wind_direction"] == "NE"
    # Values the dataset does not measure stay missing.
    assert data["visibility"] is None
    assert data["pressure"] is None
    assert data["uv_index"] is None
    assert data["leaf_wetness"] is None
    assert data["ghi"] is None

    assert data["station_name"] == "BAGAGLIONE"
    assert data["latitude"] == 44.841143
    assert data["longitude"] == 12.04094
    assert data["dt"] == "2026-11-07T14:00:00+00:00"
    assert data["measured_temperature_extremes"] == {
        "date": "2026-11-07", "min": 11.1, "max": 11.3}
    assert data["temperature_extremes"] == {"min": 11.1, "max": 11.3}

    assert sources["temperature"].source == "station"
    assert sources["temperature"].name == "BAGAGLIONE"
    assert sources["temperature"].observed_at == "2026-11-07T14:00:00+00:00"
    raw_vars = {
        variable: payload["v"]
        for item in raw
        for variable, payload in item["vars"].items()
    }
    assert raw_vars["temperatura_istantanea_2m"] == 11.1


def _build_station_with_precipitations(day, values):
    """Return a meteo_osservati station populated with given hourly rainfall.

    ``values`` maps ``"HHMM"`` -> mm, applied only to the requested day so the
    snapshot stays compact enough to share with the existing tests.
    """

    station = make_station()
    station["dati"] = {day: {}}
    for hour, mm in values.items():
        station["dati"][day][hour] = {"precipitazione_cumulata_1h": float(mm)}
    return station


def test_extract_meteo_osservati_computes_precipitation_cumulatives():
    """Sum consecutive hourly values into the 3/6/12/24h and today windows.

    ``precitazione_cumulata_1h`` is a rolling one-hour total published at the
    top of each local hour, so each value is exactly the rainfall of the
    previous hour. Summing consecutive samples therefore yields exact window
    totals; a window is only returned when every sample it needs is present,
    so a missing slot leaves the sensor unknown rather than understating the
    rainfall. "Today" follows the Italian local day, so it keeps working across
    the daylight-saving change.
    """
    # The newest slot is at 1700, so each rolling window is the set of the
    # newest ``hours`` consecutive hourly samples ending there. Every earlier
    # listed value is exactly the previous hour's rainfall, so the sums below
    # are exact window totals - provided every sample they need exists.
    station = _build_station_with_precipitations(
        "20261107", {
            "0900": 0.4, "1000": 0.8, "1100": 1.2, "1200": 1.6,
            "1300": 2.0, "1400": 2.5, "1500": 3.0, "1600": 3.5, "1700": 4.0,
        })
    data, _, _ = _extract_meteo_osservati(station)

    # Three newest consecutive hours: 1700+1600+1500 = 4+3.5+3.0 = 10.5
    assert data["precipitation_cumulative_3h"] == 10.5
    # Six newest consecutive hours: 1700..1200 = 4+3.5+3+2.5+2+1.6 = 16.6
    assert data["precipitation_cumulative_6h"] == 16.6
    # Only twelve hours available, so no 12h or 24h window can form.
    assert data["precipitation_cumulative_12h"] is None
    assert data["precipitation_cumulative_24h"] is None
    assert data["precipitation_cumulative_today"] is not None


def test_extract_meteo_osservati_missing_slot_leaves_window_unknown():
    """A slot inside the newest window makes the smaller window unknown."""
    # Drop 1500 so the 3h window (ending at 1700: 1700, 1600, 1500) breaks.
    station = _build_station_with_precipitations(
        "20261107", {
            "0900": 0.4, "1000": 0.8, "1100": 1.2, "1200": 1.6,
            "1300": 2.0, "1400": 2.5, "1600": 3.5, "1700": 4.0,
        })
    data, _, _ = _extract_meteo_osservati(station)

    assert data["precipitation_cumulative_3h"] is None   # 1500 missing
    assert data["precipitation_cumulative_6h"] is None   # also needs 1500
    assert data["precipitation_cumulative_today"] is not None  # enough for today


def test_extract_meteo_osservati_maps_pressure_and_radiation():
    """Map pressure and solar radiation from the meteo_osservati fields."""
    station = make_station()
    station["dati"]["20261107"]["1400"].update({
        "pressione_atmosferica": 1013.2,
        "radiazione_media_oraria_alto": 250.0,
    })

    data, _, sources = _extract_meteo_osservati(station)

    assert data["pressure"] == 1013.2
    assert data["ghi"] == 250.0
    assert sources["ghi"].rule == "solar_radiation"


def test_station_from_meteo_osservati_requires_temperature():
    """Only expose stations that actually measure temperature."""
    assert _station_from_meteo_osservati(make_station()) == {
        "id": "BAGAGLIONE",
        "nome": "BAGAGLIONE",
        "latitudine": 44.841143,
        "longitudine": 12.04094,
    }

    station = make_station()
    station["anagrafica"]["variabili"] = ["umidita_istantanea_2m"]
    assert _station_from_meteo_osservati(station) is None


def test_fetch_station_data_falls_back_to_meteo_osservati(monkeypatch):
    """A third-party station must win over the nearest realtime station."""

    async def fake_fetch(name):
        assert name == "BAGAGLIONE"
        return make_station()

    monkeypatch.setattr(
        coordinator, "_fetch_meteo_osservati_station", fake_fetch)

    class FakeCoordinator:
        meteo_station_id = "BAGAGLIONE"
        station_name = "BAGAGLIONE"
        latitude = 44.841143
        longitude = 12.04094

    realtime = [{
        "date": "2026-11-07T14:45:00Z",
        "data": [{
            "vars": {
                "B01019": {"v": "NEARBY"},
                "B05001": {"v": 44.84},
                "B06001": {"v": 12.04},
                "B12101": {"v": 280.0},
            },
        }],
    }]

    data, _, sources = asyncio.run(
        coordinator.ArpaEmiliaRomagnaDataUpdateCoordinator.fetch_station_data(
            FakeCoordinator(), "BAGAGLIONE", entries=realtime))

    assert data["station_name"] == "BAGAGLIONE"
    assert data["temperature"] == 11.1
    assert sources["temperature"].name == "BAGAGLIONE"


def test_fetch_station_data_fills_missing_realtime_values(monkeypatch):
    """A realtime station without temperature is completed by meteo_osservati."""

    async def fake_fetch(name):
        assert name == "CODIGORO"
        return make_station("CODIGORO")

    monkeypatch.setattr(
        coordinator, "_fetch_meteo_osservati_station", fake_fetch)

    class FakeCoordinator:
        meteo_station_id = "CODIGORO"
        station_name = "CODIGORO"
        latitude = 44.84
        longitude = 12.04

    realtime = [{
        "date": "2026-11-07T14:45:00Z",
        "data": [{
            "vars": {
                "B01019": {"v": "CODIGORO"},
                "B07030": {"v": 12.5},
            },
        }],
    }]

    data, _, sources = asyncio.run(
        coordinator.ArpaEmiliaRomagnaDataUpdateCoordinator.fetch_station_data(
            FakeCoordinator(), "CODIGORO", entries=realtime))

    # The realtime values are kept...
    assert data["station_name"] == "CODIGORO"
    assert data["brightness"] == 12.5
    # ...and the missing ones come from meteo_osservati, with their provenance.
    assert data["temperature"] == 11.1
    assert data["humidity"] == 86.0
    assert data["wind_speed"] == 28.08
    assert sources["temperature"].name == "CODIGORO"
    assert sources["temperature"].observed_at == "2026-11-07T14:00:00+00:00"
