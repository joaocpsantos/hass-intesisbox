"""Support for Intesis Air Conditioning Gateways using the WMP Protocol.

For more details about this platform, please refer to the documentation at
https://github.com/aroberts/hass-intesisbox
"""

from datetime import timedelta
import logging
import time
from typing import Any, cast

import voluptuous as vol  # type: ignore

from homeassistant.components.climate import (  # type: ignore
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.const import (  # type: ignore
    ATTR_TEMPERATURE,
    CONF_HOST,
    CONF_NAME,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.exceptions import PlatformNotReady  # type: ignore
import homeassistant.helpers.config_validation as cv  # type: ignore
from homeassistant.helpers.entity_platform import AddEntitiesCallback  # type: ignore
from homeassistant.helpers.event import async_call_later  # type: ignore

from .const import (
    CELSIUS_TO_FAHRENHEIT,
    CONF_DISPLAY_FAHRENHEIT,
    DEFAULT_DISPLAY_FAHRENHEIT,
    DEFAULT_FAN_MODES,
    DEFAULT_NAME,
    DEFAULT_VANE_HORIZONTAL_MODES,
    DEFAULT_VANE_VERTICAL_MODES,
    DOMAIN,
    FAHRENHEIT_TO_CELSIUS,
)
from .intesisbox import IntesisBox

_LOGGER = logging.getLogger(__name__)

# Timing constants (configurable)
CONNECTION_TIMEOUT = 5  # seconds to wait for initial connection
RECONNECT_INTERVAL = (
    30  # seconds between reconnection attempts (reduced from 60 for faster recovery)
)
INITIAL_RECONNECT_DELAY = (
    5  # seconds to wait before first reconnect attempt after disconnect
)

# Fan mode configuration (for YAML config)
CONF_FAN_MODES = "fan_modes"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_FAN_MODES, default=DEFAULT_FAN_MODES): {cv.string: cv.string},
    }
)

# Return cached results if last scan time was less than this value.
# If a persistent connection is established for the controller, changes to
# values are in realtime.
SCAN_INTERVAL = timedelta(seconds=300)

MAP_OPERATION_MODE_TO_HA = {
    "AUTO": HVACMode.HEAT_COOL,
    "FAN": HVACMode.FAN_ONLY,
    "HEAT": HVACMode.HEAT,
    "DRY": HVACMode.DRY,
    "COOL": HVACMode.COOL,
    "OFF": HVACMode.OFF,
}
MAP_OPERATION_MODE_TO_IB = {v: k for k, v in MAP_OPERATION_MODE_TO_HA.items()}


def _celsius_setpoint_to_fahrenheit(celsius: float) -> int:
    """Map a °C setpoint echoed by the gateway to its RNNUM °F label."""
    rounded_c = round(celsius)
    if rounded_c in CELSIUS_TO_FAHRENHEIT:
        return CELSIUS_TO_FAHRENHEIT[rounded_c]
    return round((celsius * 1.8 + 32) / 2) * 2


def _fahrenheit_setpoint_to_celsius(fahrenheit: float) -> float:
    """Map a user-requested °F setpoint to the °C value the gateway expects."""
    snapped_f = int((fahrenheit + 1) // 2 * 2)
    if snapped_f in FAHRENHEIT_TO_CELSIUS:
        return float(FAHRENHEIT_TO_CELSIUS[snapped_f])
    return round((fahrenheit - 32) / 1.8)


def _celsius_ambient_to_fahrenheit(celsius: float) -> float:
    """Convert measured ambient °C to °F, preserving 0.1° resolution."""
    return round(celsius * 1.8 + 32, 1)


MAP_STATE_ICONS = {
    HVACMode.HEAT: "mdi:white-balance-sunny",
    HVACMode.HEAT_COOL: "mdi:cached",
    HVACMode.COOL: "mdi:snowflake",
    HVACMode.DRY: "mdi:water-off",
    HVACMode.FAN_ONLY: "mdi:fan",
}


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    """Set up Intesisbox from legacy YAML configuration."""
    _LOGGER.warning(
        "Loading Intesis Gateway via platform configuration (climate.yaml) is deprecated. "
        "Your configuration has been imported to the UI. "
        "Please remove the YAML configuration."
    )

    # Trigger import flow to migrate YAML config to UI
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data=config,
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Intesis Gateway from a config entry."""
    controller = hass.data[DOMAIN][entry.entry_id]

    # Get mappings from config entry data
    fan_modes = entry.data.get("fan_modes", DEFAULT_FAN_MODES)
    vane_vertical_modes = entry.data.get(
        "vane_vertical_modes", DEFAULT_VANE_VERTICAL_MODES
    )
    vane_horizontal_modes = entry.data.get(
        "vane_horizontal_modes", DEFAULT_VANE_HORIZONTAL_MODES
    )
    display_fahrenheit = entry.data.get(
        CONF_DISPLAY_FAHRENHEIT, DEFAULT_DISPLAY_FAHRENHEIT
    )
    # Use entry title (device name) for the entity's friendly name
    name = entry.title

    async_add_entities(
        [
            IntesisBoxAC(
                controller,
                name,
                fan_modes,
                vane_vertical_modes,
                vane_horizontal_modes,
                display_fahrenheit,
            )
        ],
        True,
    )


class IntesisBoxAC(ClimateEntity):
    """Represents an Intesisbox air conditioning device."""

    _attr_should_poll = True
    # Enables the integration's icon translations (icons.json). Does not affect
    # the entity name: has_entity_name is False and _attr_name is set explicitly.
    _attr_translation_key = "intesisbox"

    def __init__(
        self,
        controller: IntesisBox,
        name: str | None = None,
        fan_modes: dict[str, str] | None = None,
        vane_vertical_modes: dict[str, str] | None = None,
        vane_horizontal_modes: dict[str, str] | None = None,
        display_fahrenheit: bool = DEFAULT_DISPLAY_FAHRENHEIT,
    ) -> None:
        """Initialize the thermostat."""
        self._controller = controller
        self._display_fahrenheit = display_fahrenheit

        if display_fahrenheit:
            self._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
            self._attr_target_temperature_step = 2.0
        else:
            self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._deviceid = controller.device_mac_address
        self._attr_name = name or controller.device_mac_address
        self._attr_unique_id = controller.device_mac_address
        # Set entity_id explicitly based on MAC address
        # This prevents entity_id from changing when device is renamed
        if controller.device_mac_address:
            self.entity_id = f"climate.{controller.device_mac_address.lower()}"
        self._connected = controller.is_connected

        _LOGGER.debug("%s Setting up climate device", self._log_prefix)

        self._attr_max_temp = controller.max_setpoint
        self._attr_min_temp = controller.min_setpoint
        self._target_temperature: float | None = None
        self._current_temp: float | None = None
        self._power = False
        self._current_operation: HVACMode | str = STATE_UNKNOWN
        self._connection_retries = 0
        self._last_reconnect_attempt = 0.0
        self._is_removing = False

        # Fan mode mapping (device value -> friendly name)
        self._fan_mode_map = fan_modes or DEFAULT_FAN_MODES
        # Reverse mapping (friendly name -> device value)
        self._fan_mode_reverse = {v: k for k, v in self._fan_mode_map.items()}

        # Vane mode mappings (device value -> friendly name)
        self._vane_vertical_map = vane_vertical_modes or DEFAULT_VANE_VERTICAL_MODES
        self._vane_horizontal_map = (
            vane_horizontal_modes or DEFAULT_VANE_HORIZONTAL_MODES
        )
        # Reverse mappings (friendly name -> device value)
        self._vane_vertical_reverse = {v: k for k, v in self._vane_vertical_map.items()}
        self._vane_horizontal_reverse = {
            v: k for k, v in self._vane_horizontal_map.items()
        }

        # Setup fan list - filter to only modes supported by the device
        device_fan_speeds = [x.upper() for x in self._controller.fan_speed_list]
        self._fan_list = []
        for device_mode in device_fan_speeds:
            if device_mode in self._fan_mode_map:
                self._fan_list.append(self._fan_mode_map[device_mode])

        # Fan speeds are optional - some devices may not support fan control
        if len(self._fan_list) < 1:
            _LOGGER.info(
                "%s No fan speeds available (device may not support fan control)",
                self._log_prefix,
            )
        self._fan_speed: str | None = None

        # Setup operation list
        self._operation_list = [HVACMode.OFF]
        for operation in self._controller.operation_list:
            self._operation_list.append(MAP_OPERATION_MODE_TO_HA[operation])
        if len(self._operation_list) == 1:
            raise PlatformNotReady("No operation modes available from controller")

        # Setup feature support
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )

        if len(self._fan_list) > 0:
            features |= ClimateEntityFeature.FAN_MODE

        # Setup swing control
        # Per architecture discussion #553 Option 2:
        # - If device has independent vertical AND horizontal control:
        #   use swing_vertical_modes + swing_horizontal_modes (NOT swing_modes)
        # - If device has only one type of swing control:
        #   use swing_modes (NOT swing_vertical_modes/swing_horizontal_modes)
        self._swing_list: list[str] = []  # Vertical vane positions
        self._swing_horizontal_list: list[str] = []  # Horizontal vane positions
        self._current_vane_vertical: str | None = None
        self._current_vane_horizontal: str | None = None

        # Determine if we have vanes
        has_vertical = len(self._controller.vane_vertical_list) > 0
        has_horizontal = len(self._controller.vane_horizontal_list) > 0
        self._has_independent_control = has_vertical and has_horizontal

        if has_vertical or has_horizontal:
            features |= ClimateEntityFeature.SWING_MODE

            _LOGGER.info(
                "%s Vane control detected - vertical: %s, horizontal: %s, independent: %s",
                self._log_prefix,
                self._controller.vane_vertical_list,
                self._controller.vane_horizontal_list,
                self._has_independent_control,
            )

            # Architecture:
            # - If BOTH vertical and horizontal: use swing_modes for vertical, swing_horizontal_modes for horizontal
            # - If ONLY vertical: use swing_modes
            # - If ONLY horizontal: use swing_modes (map horizontal to primary swing control)

            # Build vertical swing modes (if available)
            if has_vertical:
                # Build list with mapped names, filtering to only device-supported positions
                device_vane_positions = [
                    x.upper() for x in self._controller.vane_vertical_list
                ]
                self._swing_list = []
                for device_vane in device_vane_positions:
                    if device_vane in self._vane_vertical_map:
                        self._swing_list.append(self._vane_vertical_map[device_vane])

                self._attr_swing_modes = self._swing_list  # Set _attr_ for base class
                _LOGGER.info(
                    "%s Vertical swing modes: %s", self._log_prefix, self._swing_list
                )

            # Build horizontal swing modes (if available)
            if has_horizontal:
                # Build list with mapped names, filtering to only device-supported positions
                device_vane_positions = [
                    x.upper() for x in self._controller.vane_horizontal_list
                ]
                self._swing_horizontal_list = []
                for device_vane in device_vane_positions:
                    if device_vane in self._vane_horizontal_map:
                        self._swing_horizontal_list.append(
                            self._vane_horizontal_map[device_vane]
                        )

                if self._has_independent_control:
                    # Both vanes exist - use horizontal modes for horizontal control
                    features |= ClimateEntityFeature.SWING_HORIZONTAL_MODE
                    self._attr_swing_horizontal_modes = self._swing_horizontal_list
                    _LOGGER.info(
                        "%s Horizontal swing modes: %s",
                        self._log_prefix,
                        self._swing_horizontal_list,
                    )
                else:
                    # Only horizontal vanes - map to primary swing_modes
                    self._swing_list = self._swing_horizontal_list
                    self._attr_swing_modes = self._swing_list
                    _LOGGER.info(
                        "%s Horizontal vanes (as primary swing): %s",
                        self._log_prefix,
                        self._swing_list,
                    )
        else:
            self._has_independent_control = False
            _LOGGER.info(
                "%s No vane control - vertical: %s, horizontal: %s",
                self._log_prefix,
                self._controller.vane_vertical_list,
                self._controller.vane_horizontal_list,
            )

        self._attr_supported_features = features

        # Debug: Log what swing features we have enabled
        _LOGGER.info(
            "%s Swing features - SWING_MODE: %s, SWING_HORIZONTAL_MODE: %s",
            self._log_prefix,
            bool(features & ClimateEntityFeature.SWING_MODE),
            bool(features & ClimateEntityFeature.SWING_HORIZONTAL_MODE),
        )

        _LOGGER.debug("%s Finished setting up climate entity", self._log_prefix)
        self._controller.add_update_callback(self.update_callback)

    @property
    def device_info(self) -> dict[str, Any]:
        """Info about the Intesis Gateway itself."""
        device_info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Intesis",
            "model": self._controller.device_model,
            "sw_version": self._controller.firmware_version,
        }

        # Add MAC address if available
        if self._controller.device_mac_address:
            device_info["connections"] = {("mac", self._controller.device_mac_address)}

        return device_info

    @property
    def _log_prefix(self) -> str:
        """Return formatted log prefix with entity identifier."""
        if self._deviceid:
            entity_id = f"climate.{self._deviceid.lower()}"
            if self._attr_name and self._attr_name != self._deviceid:
                return f"[{self._attr_name}({entity_id})]"
            return f"[{entity_id}]"
        return "[climate]"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the device specific state attributes."""
        attrs = {}

        # Add vane position details if vanes are supported
        if self._current_vane_vertical is not None:
            attrs["vane_vertical"] = self._current_vane_vertical
        if self._current_vane_horizontal is not None:
            attrs["vane_horizontal"] = self._current_vane_horizontal

        if self._controller.is_connected:
            attrs["ha_update_type"] = "push"
        else:
            attrs["ha_update_type"] = "poll"

        return attrs

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        _LOGGER.debug("%s async_set_temperature: %s", self._log_prefix, kwargs)

        temperature = kwargs.get(ATTR_TEMPERATURE)
        operation_mode = kwargs.get("hvac_mode")

        if operation_mode:
            await self.async_set_hvac_mode(operation_mode)

        if temperature:
            if self._display_fahrenheit:
                celsius = _fahrenheit_setpoint_to_celsius(temperature)
                _LOGGER.debug(
                    "%s Fahrenheit setpoint %s mapped to %s°C",
                    self._log_prefix,
                    temperature,
                    celsius,
                )
            else:
                celsius = temperature
            try:
                await self.hass.async_add_executor_job(
                    self._controller.set_temperature, celsius
                )
            except Exception as err:
                _LOGGER.error(
                    "%s Failed to set temperature to %s: %s",
                    self._log_prefix,
                    temperature,
                    err,
                )
                raise

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set operation mode."""
        _LOGGER.debug("%s async_set_hvac_mode: %s", self._log_prefix, hvac_mode)

        try:
            if hvac_mode == HVACMode.OFF:
                await self.hass.async_add_executor_job(self._controller.set_power_off)
                self._power = False
            else:
                await self.hass.async_add_executor_job(
                    self._controller.set_mode, MAP_OPERATION_MODE_TO_IB[hvac_mode]
                )

                # Send the temperature again in case changing modes has changed it
                if self._target_temperature:
                    await self.hass.async_add_executor_job(
                        self._controller.set_temperature, self._target_temperature
                    )
        except Exception as err:
            _LOGGER.error(
                "%s Failed to set HVAC mode to %s: %s", self._log_prefix, hvac_mode, err
            )
            raise

        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn thermostat on."""
        try:
            await self.hass.async_add_executor_job(self._controller.set_power_on)
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("%s Failed to turn on: %s", self._log_prefix, err)
            raise

    async def async_turn_off(self) -> None:
        """Turn thermostat off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set fan mode (from quiet, low, medium, high, auto)."""
        _LOGGER.debug("%s async_set_fan_mode: %s", self._log_prefix, fan_mode)

        # Convert friendly name to device value
        device_value = self._fan_mode_reverse.get(fan_mode)
        if not device_value:
            _LOGGER.error("%s Unknown fan mode: %s", self._log_prefix, fan_mode)
            return

        try:
            await self.hass.async_add_executor_job(
                self._controller.set_fan_speed, device_value
            )
        except Exception as err:
            _LOGGER.error(
                "%s Failed to set fan mode to %s: %s", self._log_prefix, fan_mode, err
            )
            raise

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Set the swing mode (vertical vanes, or horizontal if device only has horizontal)."""
        _LOGGER.debug("%s async_set_swing_mode: %s", self._log_prefix, swing_mode)

        # If only horizontal vanes exist, route to horizontal control
        if (
            not self._controller.vane_vertical_list
            and self._controller.vane_horizontal_list
        ):
            _LOGGER.debug(
                "%s Routing swing_mode to horizontal vane (device has horizontal only)",
                self._log_prefix,
            )
            device_value = self._vane_horizontal_reverse.get(swing_mode)
            if not device_value:
                _LOGGER.error(
                    "%s Unknown horizontal swing mode: %s", self._log_prefix, swing_mode
                )
                return
            try:
                await self.hass.async_add_executor_job(
                    self._controller.set_horizontal_vane, device_value
                )
            except Exception as err:
                _LOGGER.error(
                    "%s Failed to set horizontal swing to %s: %s",
                    self._log_prefix,
                    swing_mode,
                    err,
                )
                raise
        else:
            # Normal vertical vane control
            device_value = self._vane_vertical_reverse.get(swing_mode)
            if not device_value:
                _LOGGER.error(
                    "%s Unknown vertical swing mode: %s", self._log_prefix, swing_mode
                )
                return
            try:
                await self.hass.async_add_executor_job(
                    self._controller.set_vertical_vane, device_value
                )
            except Exception as err:
                _LOGGER.error(
                    "%s Failed to set vertical swing to %s: %s",
                    self._log_prefix,
                    swing_mode,
                    err,
                )
                raise

    async def async_set_swing_horizontal_mode(self, swing_mode: str) -> None:
        """Set the horizontal swing mode."""
        _LOGGER.debug(
            "%s async_set_swing_horizontal_mode: %s", self._log_prefix, swing_mode
        )

        # Convert friendly name to device value
        device_value = self._vane_horizontal_reverse.get(swing_mode)
        if not device_value:
            _LOGGER.error(
                "%s Unknown horizontal swing mode: %s", self._log_prefix, swing_mode
            )
            return

        try:
            await self.hass.async_add_executor_job(
                self._controller.set_horizontal_vane, device_value
            )
        except Exception as err:
            _LOGGER.error(
                "%s Failed to set horizontal swing to %s: %s",
                self._log_prefix,
                swing_mode,
                err,
            )
            raise

    async def async_update(self) -> None:
        """Copy values from controller dictionary to climate device."""
        # Don't attempt reconnection if entity is being removed
        if self._is_removing:
            return

        # Track connection lost/restored FIRST, before reconnect logic
        # This ensures the timer is set when connection is first detected as lost
        if self._connected != self._controller.is_connected:
            self._connected = self._controller.is_connected
            if self._connected:
                _LOGGER.info("%s Connection was restored", self._log_prefix)
            else:
                _LOGGER.warning("%s Lost connection", self._log_prefix)
                # Set reconnect timer so first attempt happens after INITIAL_RECONNECT_DELAY
                self._last_reconnect_attempt = (
                    time.time() - RECONNECT_INTERVAL + INITIAL_RECONNECT_DELAY
                )

                # Schedule an update after the delay to ensure reconnect attempt happens
                async def schedule_reconnect(_now):
                    self.async_schedule_update_ha_state(True)

                async_call_later(
                    self.hass, INITIAL_RECONNECT_DELAY + 1, schedule_reconnect
                )

        if not self._controller.is_connected:
            # Only attempt reconnection if enough time has passed
            now = time.time()
            if now - self._last_reconnect_attempt < RECONNECT_INTERVAL:
                _LOGGER.debug(
                    "%s Skipping reconnect attempt, last attempt was %.1f seconds ago",
                    self._log_prefix,
                    now - self._last_reconnect_attempt,
                )
                return

            self._last_reconnect_attempt = now
            _LOGGER.info(
                "%s Attempting to reconnect (attempt #%d)",
                self._log_prefix,
                self._connection_retries + 1,
            )

            try:
                # Force disconnect first to reset any stuck connection state
                await self.hass.async_add_executor_job(self._controller.disconnect)
                # Now attempt to reconnect
                await self.hass.async_add_executor_job(self._controller.connect)
                self._connection_retries += 1

                # If still not connected after attempt, schedule next try
                if not self._controller.is_connected:

                    async def schedule_next_attempt(_now):
                        self.async_schedule_update_ha_state(True)

                    async_call_later(
                        self.hass, RECONNECT_INTERVAL + 1, schedule_next_attempt
                    )
            except Exception as err:
                _LOGGER.error(
                    "%s Reconnection attempt failed: %s", self._log_prefix, err
                )

                # Schedule next reconnect attempt
                async def schedule_retry(_now):
                    self.async_schedule_update_ha_state(True)

                async_call_later(self.hass, RECONNECT_INTERVAL + 1, schedule_retry)
                return
        else:
            # Reset retry counter when connected
            if self._connection_retries > 0:
                _LOGGER.info("%s Successfully reconnected", self._log_prefix)
                self._connection_retries = 0

        # Update all state from controller
        self._power = self._controller.is_on
        self._current_temp = self._controller.ambient_temperature
        self._attr_min_temp = self._controller.min_setpoint
        self._attr_max_temp = self._controller.max_setpoint
        self._target_temperature = self._controller.setpoint

        # Map device fan speed to friendly name
        if self._controller.fan_speed:
            device_speed = self._controller.fan_speed.upper()
            self._fan_speed = self._fan_mode_map.get(device_speed, device_speed.lower())

        # Operation mode
        ib_mode = self._controller.mode
        if ib_mode is not None:
            self._current_operation = MAP_OPERATION_MODE_TO_HA.get(
                ib_mode, STATE_UNKNOWN
            )
        else:
            self._current_operation = STATE_UNKNOWN

        # Swing mode (vane position)
        # Map device values to friendly names
        raw_vertical = self._controller.vertical_swing()
        raw_horizontal = self._controller.horizontal_swing()

        if raw_vertical:
            device_vertical = raw_vertical.upper()
            self._current_vane_vertical = self._vane_vertical_map.get(
                device_vertical, device_vertical.lower()
            )
        else:
            self._current_vane_vertical = None

        if raw_horizontal:
            device_horizontal = raw_horizontal.upper()
            self._current_vane_horizontal = self._vane_horizontal_map.get(
                device_horizontal, device_horizontal.lower()
            )
        else:
            self._current_vane_horizontal = None

    async def async_will_remove_from_hass(self) -> None:
        """Shutdown the controller when the device is being removed."""
        _LOGGER.debug(
            "%s Climate entity being removed, stopping controller", self._log_prefix
        )
        self._is_removing = True
        try:
            await self.hass.async_add_executor_job(self._controller.stop)
        except Exception as err:
            _LOGGER.error("%s Error stopping controller: %s", self._log_prefix, err)

    @property
    def icon(self) -> str | None:
        """Return the icon for the current state."""
        if not self._power:
            return None
        if not isinstance(self._current_operation, HVACMode):
            return None
        # After isinstance check, explicitly cast for mypy
        operation: HVACMode = cast(HVACMode, self._current_operation)
        return MAP_STATE_ICONS.get(operation)

    def update_callback(self) -> None:
        """Let HA know there has been an update from the controller."""
        _LOGGER.debug(
            "%s update_callback: Intesis Gateway sent a status update", self._log_prefix
        )
        if self.hass:
            self.schedule_update_ha_state(True)

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature for the current mode of operation."""
        celsius = self._attr_min_temp if self._attr_min_temp is not None else 16.0
        if self._display_fahrenheit:
            return float(_celsius_setpoint_to_fahrenheit(celsius))
        return celsius

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature for the current mode of operation."""
        celsius = self._attr_max_temp if self._attr_max_temp is not None else 30.0
        if self._display_fahrenheit:
            return float(_celsius_setpoint_to_fahrenheit(celsius))
        return celsius

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """List of available operation modes."""
        return self._operation_list

    @property
    def fan_mode(self) -> str | None:
        """Return the current fan mode."""
        return self._fan_speed

    @property
    def swing_mode(self) -> str | None:
        """Return current swing mode (vertical, or horizontal if device only has horizontal)."""
        # If only horizontal vanes exist, return horizontal state
        if (
            not self._controller.vane_vertical_list
            and self._controller.vane_horizontal_list
        ):
            return self._current_vane_horizontal
        # Otherwise return vertical state
        return self._current_vane_vertical

    @property
    def swing_horizontal_mode(self) -> str | None:
        """Return current horizontal swing mode."""
        return self._current_vane_horizontal

    @property
    def fan_modes(self) -> list[str]:
        """List of available fan modes."""
        return self._fan_list

    @property
    def assumed_state(self) -> bool:
        """If the device is not connected we have to assume state."""
        return not self._connected

    @property
    def available(self) -> bool:
        """Device is available only when connected."""
        return self._connected

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        if self._current_temp is None:
            return None
        if self._display_fahrenheit:
            return _celsius_ambient_to_fahrenheit(self._current_temp)
        return self._current_temp

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current mode of operation if unit is on."""
        if self._power:
            return self._current_operation
        return HVACMode.OFF

    @property
    def target_temperature(self) -> float | None:
        """Return the current setpoint temperature if unit is on."""
        if not self._power or self.hvac_mode in [HVACMode.FAN_ONLY, HVACMode.OFF]:
            return None
        if self._target_temperature is None:
            return None
        if self._display_fahrenheit:
            return float(_celsius_setpoint_to_fahrenheit(self._target_temperature))
        return self._target_temperature
