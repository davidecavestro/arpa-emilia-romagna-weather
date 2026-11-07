"""Unit tests for the ARPAE parsing helpers that do not touch the network."""


from custom_components.arpa_emilia_romagna_weather.coordinator import (
    _condition_from_zone,
    _measurement_for_period,
    _parse_precipitation_prob,
    _zone_temperatures,
)


def _snapshot(entries):
    return {"data": entries}


def test_zone_temperatures():
    """Read the zonal minimum and maximum for the requested zone."""
    dati = {
        "temperatura_minima": {
            "t9": {"dato": "9.5"},
            "T9": {"dato": 3, "valore": None},  # missing midpoint
        },
        "temperatura_massima": {
            "T9": {"dato": 14.0},
            "t9": {"dato": 1, "valore": None},
        },
    }
    result = _zone_temperatures(dati, "C9")
    assert result == {"min": 9.5, "max": 14.0}


def test_zone_temperatures_invalid_prefix_is_empty():
    """Return no temperatures when the zone id is not a bulletin zone."""
    assert _zone_temperatures({}, "Rurale") == {}
    assert _zone_temperatures({}, "") == {}


def test_measurement_for_period_selects_matching_interval():
    """Pick the accumulation whose timerange matches the requested period."""
    snapshot = _snapshot([
        {"vars": {"B13011": {"v": 0.3}}, "timerange": [1, 0, 900]},
        {"vars": {"B13011": {"v": 0.6}}, "timerange": [1, 0, 3600]},
    ])
    assert _measurement_for_period(snapshot, "B13011", 3600) == 0.6
    # The 15-minute value is preferred when no hour value exists.
    snapshot = _snapshot([
        {"vars": {"B13011": {"v": 0.3}}, "timerange": [1, 0, 900]},
    ])
    assert _measurement_for_period(snapshot, "B13011", 900) == 0.3


def test_condition_from_zone_icon_mapping():
    """Map bulletin icon codes to conditions before consulting the text."""
    # Each entry has an explicit icon code that is looked up before the Italian
    # text is ever consulted.
    zones = {
        "a014": {"icona": "a014", "it": "anything"},
        "b001": {"icona": "b001", "it": "any text"},
        "cloudy": {"icona": "a013", "it": "any text"},
    }
    assert _condition_from_zone(zones["a014"]) == "rainy"
    assert _condition_from_zone(zones["b001"]) == "sunny"
    assert _condition_from_zone(zones["cloudy"]) == "cloudy"


def test_condition_from_zone_italian_text_fallback():
    """Derive a condition from the Italian text when no icon is present."""
    # No icon, but the Italian text still yields a condition.
    assert _condition_from_zone({"it": "pioggia forte"}) == "rainy"
    assert _condition_from_zone({"it": "variabile"}) == "partlycloudy"
    assert _condition_from_zone({"it": "sereno"}) == "sunny"
    assert _condition_from_zone({"it": ""}) is None


def test_parse_precipitation_prob_strips_suffix():
    """Accept a trailing percent sign and reject empty values."""
    assert _parse_precipitation_prob("7%") == 7
    assert _parse_precipitation_prob("") is None



