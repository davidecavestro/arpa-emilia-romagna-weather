"""Sensors for ARPA Emilia-Romagna Weather component."""
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator
from .const import (
    DOMAIN,
    SENSOR_TYPES,
    KEY_COORDINATOR
)
from datetime import datetime


def _naive_local(value):
    """Return a datetime as naive local time, so it can be compared with now()."""

    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up ARPA Emilia-Romagna sensors from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][KEY_COORDINATOR]

    # create a sensor for each sensor type in SENSOR_TYPES
    sensors = []
    for sensor_type in SENSOR_TYPES:
        sensors.append(ArpaEmiliaRomagnaSensor(coordinator, config_entry, sensor_type))
    async_add_entities(sensors)

class ArpaEmiliaRomagnaSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """Representation of an ARPA Emilia-Romagna sensor."""

    def __init__(self, coordinator, config_entry, sensor_type):
        """Initialize the sensor."""
        super().__init__(coordinator)  # Initialize the CoordinatorEntity
        self.coordinator = coordinator
        self._config_entry = config_entry
        self.station_id = config_entry.data.get("station_id")
        self.station_name = config_entry.data.get("station_name")
        self.sensor_type = sensor_type
        sensor = SENSOR_TYPES[sensor_type]
        self._attr_device_class = sensor.get("device_class") # type: ignore
        self._attr_state_class = sensor.get("state_class") # type: ignore
        self._attr_native_unit_of_measurement = sensor.get("unit")

        self._attr_unique_id = f"arpae_{sensor_type}_{self.station_id}"

        self._attr_has_entity_name = True
        self._attr_translation_key = sensor_type
        self._attr_translation_placeholders = {"station_name": self.station_name}

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name="ARPA Emilia-Romagna Weather",
            entry_type=DeviceEntryType.SERVICE
        )

    @property
    def state(self):
        """Return the state of the sensor."""
        if self.sensor_type == "precipitation_probability":
            forecasts = self.coordinator.data.get("forecast") or []
            if not forecasts:
                return None

            # Find forecast with datetime closest to now
            now = datetime.now()
            nearest = min(
                forecasts,
                key=lambda forecast: abs(
                    _naive_local(forecast["datetime"]) - now),
            )

            def get_attr(entry, key):
                value = entry.get(key)
                return value if value != "" else None

            return get_attr(nearest, "precipitation_probability")

        return self.coordinator.data["sensors"].get(self.sensor_type)

    @property
    def extra_state_attributes(self):
        """Return additional attributes, including where the value comes from."""
        attributes = {
            "station_id": self.station_id,
            "last_update": self.coordinator.data["sensors"].get("last_update"),
        }

        # the entities of a single station are fed by different networks
        provenance = self.coordinator.data.get("sources", {}).get(self.sensor_type)
        if provenance:
            attributes.update(provenance.as_attributes())

        return attributes

    async def async_update(self):
        """Update the sensor state."""
        await self.coordinator.async_request_refresh()
