"""Config flow for Intesis integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol  # type: ignore

from homeassistant import config_entries  # type: ignore
from homeassistant.const import CONF_HOST, CONF_NAME  # type: ignore
from homeassistant.core import HomeAssistant, callback  # type: ignore
from homeassistant.data_entry_flow import AbortFlow, FlowResult  # type: ignore

from .const import (
    CONF_DISPLAY_FAHRENHEIT,
    CONF_ENABLE_PING,
    CONF_FAN_MODE_1,
    CONF_FAN_MODE_2,
    CONF_FAN_MODE_3,
    CONF_FAN_MODE_4,
    CONF_FAN_MODE_5,
    CONF_FAN_MODE_6,
    CONF_FAN_MODE_7,
    CONF_FAN_MODE_8,
    CONF_FAN_MODE_9,
    CONF_FAN_MODE_AUTO,
    CONF_SYNC_TIME,
    CONF_USE_LOCAL_TIME,
    CONF_VANE_HORIZONTAL_1,
    CONF_VANE_HORIZONTAL_2,
    CONF_VANE_HORIZONTAL_3,
    CONF_VANE_HORIZONTAL_4,
    CONF_VANE_HORIZONTAL_5,
    CONF_VANE_HORIZONTAL_6,
    CONF_VANE_HORIZONTAL_7,
    CONF_VANE_HORIZONTAL_8,
    CONF_VANE_HORIZONTAL_9,
    CONF_VANE_HORIZONTAL_AUTO,
    CONF_VANE_HORIZONTAL_SWING,
    CONF_VANE_VERTICAL_1,
    CONF_VANE_VERTICAL_2,
    CONF_VANE_VERTICAL_3,
    CONF_VANE_VERTICAL_4,
    CONF_VANE_VERTICAL_5,
    CONF_VANE_VERTICAL_6,
    CONF_VANE_VERTICAL_7,
    CONF_VANE_VERTICAL_8,
    CONF_VANE_VERTICAL_9,
    CONF_VANE_VERTICAL_AUTO,
    CONF_VANE_VERTICAL_SWING,
    DEFAULT_DISPLAY_FAHRENHEIT,
    DEFAULT_ENABLE_PING,
    DEFAULT_FAN_MODES,
    DEFAULT_NAME,
    DEFAULT_SYNC_TIME,
    DEFAULT_USE_LOCAL_TIME,
    DEFAULT_VANE_HORIZONTAL_MODES,
    DEFAULT_VANE_VERTICAL_MODES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONNECTION_TIMEOUT = 5


async def validate_connection(hass: HomeAssistant, host: str) -> dict[str, Any]:
    """Validate the connection to the Intesis Gateway."""
    from .intesisbox import IntesisBox

    controller = IntesisBox(host, loop=hass.loop)

    try:
        controller.connect()

        # Wait for connection with timeout
        for _ in range(CONNECTION_TIMEOUT * 10):
            if controller.is_connected and len(controller.fan_speed_list) > 0:
                # Get device info
                mac = controller.device_mac_address
                model = controller.device_model

                # Get supported features
                fan_speeds = controller.fan_speed_list
                vane_vertical = controller.vane_vertical_list
                vane_horizontal = controller.vane_horizontal_list

                # Stop the controller - we'll create a new one in async_setup_entry
                controller.stop()
                # Wait for complete disconnect to prevent ghost connections
                await controller.wait_for_disconnect(timeout=3.0)
                # Additional delay to ensure socket is fully closed
                await asyncio.sleep(1.0)

                return {
                    "mac": mac,
                    "model": model,
                    "fan_speeds": fan_speeds,
                    "vane_vertical": vane_vertical,
                    "vane_horizontal": vane_horizontal,
                }
            await asyncio.sleep(0.1)

        controller.stop()
        await controller.wait_for_disconnect(timeout=3.0)
        await asyncio.sleep(1.0)
        raise ConnectionError("Connection timeout")

    except Exception as err:
        _LOGGER.error("Failed to connect to Intesis Gateway at %s: %s", host, err)
        if controller:
            controller.stop()
            await controller.wait_for_disconnect(timeout=3.0)
            await asyncio.sleep(1.0)
        raise


class IntesisBoxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Intesis Gateway."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str | None = None
        self._name: str | None = None
        self._mac: str | None = None
        self._model: str | None = None
        self._fan_speeds: list[str] = []
        self._vane_vertical: list[str] = []
        self._vane_horizontal: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                info = await validate_connection(self.hass, user_input[CONF_HOST])

                self._host = user_input[CONF_HOST]
                self._name = user_input.get(CONF_NAME, DEFAULT_NAME)
                self._mac = info["mac"]
                self._model = info["model"]
                self._fan_speeds = info["fan_speeds"]
                self._vane_vertical = info["vane_vertical"]
                self._vane_horizontal = info["vane_horizontal"]

                # Set unique ID based on MAC address
                await self.async_set_unique_id(self._mac)
                self._abort_if_unique_id_configured()

                # Create entry with default mappings
                # Users can change these later via options
                return self.async_create_entry(
                    title=self._name,
                    data={
                        CONF_HOST: self._host,
                        CONF_NAME: self._name,
                        "fan_modes": DEFAULT_FAN_MODES,
                        "vane_vertical_modes": DEFAULT_VANE_VERTICAL_MODES,
                        "vane_horizontal_modes": DEFAULT_VANE_HORIZONTAL_MODES,
                        CONF_SYNC_TIME: DEFAULT_SYNC_TIME,
                        CONF_USE_LOCAL_TIME: DEFAULT_USE_LOCAL_TIME,
                        CONF_DISPLAY_FAHRENHEIT: DEFAULT_DISPLAY_FAHRENHEIT,
                    },
                )

            except AbortFlow:
                # Let AbortFlow exceptions propagate to Home Assistant's flow handler
                raise
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, import_config: dict[str, Any]) -> FlowResult:
        """Import a config entry from configuration.yaml."""
        # Support importing old YAML configurations
        _LOGGER.info("Importing Intesis Gateway configuration from YAML")

        # Extract host from import config
        host = import_config.get(CONF_HOST)
        if not host:
            _LOGGER.error("No host specified in YAML import")
            return self.async_abort(reason="missing_host")

        # Check if already configured
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        # Try to validate connection and get device info
        try:
            device_info = await validate_connection(self.hass, host)
            mac = device_info["mac"]

            # Use MAC as unique ID if available
            if mac:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()

            # Create entry with imported data
            return self.async_create_entry(
                title=import_config.get(CONF_NAME, host),
                data={
                    CONF_HOST: host,
                    CONF_NAME: import_config.get(CONF_NAME, DEFAULT_NAME),
                    "fan_modes": import_config.get("fan_modes", DEFAULT_FAN_MODES),
                    "vane_vertical_modes": import_config.get(
                        "vane_vertical_modes", DEFAULT_VANE_VERTICAL_MODES
                    ),
                    "vane_horizontal_modes": import_config.get(
                        "vane_horizontal_modes", DEFAULT_VANE_HORIZONTAL_MODES
                    ),
                    CONF_SYNC_TIME: import_config.get(
                        CONF_SYNC_TIME, DEFAULT_SYNC_TIME
                    ),
                    CONF_USE_LOCAL_TIME: import_config.get(
                        CONF_USE_LOCAL_TIME, DEFAULT_USE_LOCAL_TIME
                    ),
                    CONF_DISPLAY_FAHRENHEIT: import_config.get(
                        CONF_DISPLAY_FAHRENHEIT, DEFAULT_DISPLAY_FAHRENHEIT
                    ),
                },
            )

        except Exception as err:
            _LOGGER.error("Failed to import YAML config for %s: %s", host, err)
            return self.async_abort(reason="cannot_connect")

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IntesisBoxOptionsFlow:
        """Get the options flow for this handler."""
        return IntesisBoxOptionsFlow(config_entry)


class IntesisBoxOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Intesis Gateway."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._fan_speeds: list[str] = []
        self._vane_vertical: list[str] = []
        self._vane_horizontal: list[str] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show configuration menu."""
        # Get current controller to check supported features
        controller = self.hass.data[DOMAIN].get(self._config_entry.entry_id)

        if controller:
            self._fan_speeds = [x.upper() for x in controller.fan_speed_list]
            self._vane_vertical = [x.upper() for x in controller.vane_vertical_list]
            self._vane_horizontal = [x.upper() for x in controller.vane_horizontal_list]

        if not self._fan_speeds:
            return self.async_abort(reason="device_not_ready")

        # Build menu options based on what device supports
        menu_options = ["options", "fan_modes"]
        if self._vane_vertical:
            menu_options.append("vane_vertical")
        if self._vane_horizontal:
            menu_options.append("vane_horizontal")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_fan_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure fan mode names."""
        if user_input is not None:
            # Build fan modes dictionary from user input
            fan_modes = {}
            speed_to_config = {
                "AUTO": CONF_FAN_MODE_AUTO,
                "1": CONF_FAN_MODE_1,
                "2": CONF_FAN_MODE_2,
                "3": CONF_FAN_MODE_3,
                "4": CONF_FAN_MODE_4,
                "5": CONF_FAN_MODE_5,
                "6": CONF_FAN_MODE_6,
                "7": CONF_FAN_MODE_7,
                "8": CONF_FAN_MODE_8,
                "9": CONF_FAN_MODE_9,
            }

            for device_speed in self._fan_speeds:
                config_key = speed_to_config.get(device_speed)
                if config_key and config_key in user_input:
                    fan_modes[device_speed] = user_input[config_key]

            # Update config entry with fan modes only
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    "fan_modes": fan_modes,
                },
            )

            # Return to menu
            return await self.async_step_init()

        # Get current fan modes from config
        current_fan_modes = self._config_entry.data.get("fan_modes", {})

        # Build schema based on what the device supports
        schema_dict = {}
        speed_to_config = {
            "AUTO": CONF_FAN_MODE_AUTO,
            "1": CONF_FAN_MODE_1,
            "2": CONF_FAN_MODE_2,
            "3": CONF_FAN_MODE_3,
            "4": CONF_FAN_MODE_4,
            "5": CONF_FAN_MODE_5,
            "6": CONF_FAN_MODE_6,
            "7": CONF_FAN_MODE_7,
            "8": CONF_FAN_MODE_8,
            "9": CONF_FAN_MODE_9,
        }

        for device_speed in self._fan_speeds:
            default_value = current_fan_modes.get(
                device_speed, DEFAULT_FAN_MODES.get(device_speed, device_speed.lower())
            )
            config_key = speed_to_config.get(device_speed)
            if config_key:
                schema_dict[vol.Optional(config_key, default=default_value)] = str

        return self.async_show_form(
            step_id="fan_modes",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "fan_speeds": ", ".join(self._fan_speeds),
            },
        )

    async def async_step_vane_vertical(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure vertical vane positions."""
        if user_input is not None:
            # Build vertical vane modes mapping
            vane_vertical_modes = {}
            vane_vertical_to_config = {
                "AUTO": CONF_VANE_VERTICAL_AUTO,
                "1": CONF_VANE_VERTICAL_1,
                "2": CONF_VANE_VERTICAL_2,
                "3": CONF_VANE_VERTICAL_3,
                "4": CONF_VANE_VERTICAL_4,
                "5": CONF_VANE_VERTICAL_5,
                "6": CONF_VANE_VERTICAL_6,
                "7": CONF_VANE_VERTICAL_7,
                "8": CONF_VANE_VERTICAL_8,
                "9": CONF_VANE_VERTICAL_9,
                "SWING": CONF_VANE_VERTICAL_SWING,
            }

            for device_vane in self._vane_vertical:
                config_key = vane_vertical_to_config.get(device_vane)
                if config_key and config_key in user_input:
                    vane_vertical_modes[device_vane] = user_input[config_key]

            # Update config entry with vertical vane modes only
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    "vane_vertical_modes": vane_vertical_modes,
                },
            )

            # Return to menu
            return await self.async_step_init()

        # Get current vertical vane modes from config
        current_vane_vertical_modes = self._config_entry.data.get(
            "vane_vertical_modes", {}
        )

        # Build schema
        schema_dict = {}
        vane_vertical_to_config = {
            "AUTO": CONF_VANE_VERTICAL_AUTO,
            "1": CONF_VANE_VERTICAL_1,
            "2": CONF_VANE_VERTICAL_2,
            "3": CONF_VANE_VERTICAL_3,
            "4": CONF_VANE_VERTICAL_4,
            "5": CONF_VANE_VERTICAL_5,
            "6": CONF_VANE_VERTICAL_6,
            "7": CONF_VANE_VERTICAL_7,
            "8": CONF_VANE_VERTICAL_8,
            "9": CONF_VANE_VERTICAL_9,
            "SWING": CONF_VANE_VERTICAL_SWING,
        }

        for device_vane in self._vane_vertical:
            default_value = current_vane_vertical_modes.get(
                device_vane,
                DEFAULT_VANE_VERTICAL_MODES.get(device_vane, device_vane.lower()),
            )
            config_key = vane_vertical_to_config.get(device_vane)
            if config_key:
                schema_dict[vol.Optional(config_key, default=default_value)] = str

        return self.async_show_form(
            step_id="vane_vertical",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "vane_vertical": ", ".join(self._vane_vertical),
            },
        )

    async def async_step_vane_horizontal(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure horizontal vane positions."""
        if user_input is not None:
            # Build horizontal vane modes mapping
            vane_horizontal_modes = {}
            vane_horizontal_to_config = {
                "AUTO": CONF_VANE_HORIZONTAL_AUTO,
                "1": CONF_VANE_HORIZONTAL_1,
                "2": CONF_VANE_HORIZONTAL_2,
                "3": CONF_VANE_HORIZONTAL_3,
                "4": CONF_VANE_HORIZONTAL_4,
                "5": CONF_VANE_HORIZONTAL_5,
                "6": CONF_VANE_HORIZONTAL_6,
                "7": CONF_VANE_HORIZONTAL_7,
                "8": CONF_VANE_HORIZONTAL_8,
                "9": CONF_VANE_HORIZONTAL_9,
                "SWING": CONF_VANE_HORIZONTAL_SWING,
            }

            for device_vane in self._vane_horizontal:
                config_key = vane_horizontal_to_config.get(device_vane)
                if config_key and config_key in user_input:
                    vane_horizontal_modes[device_vane] = user_input[config_key]

            # Update config entry with horizontal vane modes only
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    "vane_horizontal_modes": vane_horizontal_modes,
                },
            )

            # Return to menu
            return await self.async_step_init()

        # Get current horizontal vane modes from config
        current_vane_horizontal_modes = self._config_entry.data.get(
            "vane_horizontal_modes", {}
        )

        # Build schema
        schema_dict = {}
        vane_horizontal_to_config = {
            "AUTO": CONF_VANE_HORIZONTAL_AUTO,
            "1": CONF_VANE_HORIZONTAL_1,
            "2": CONF_VANE_HORIZONTAL_2,
            "3": CONF_VANE_HORIZONTAL_3,
            "4": CONF_VANE_HORIZONTAL_4,
            "5": CONF_VANE_HORIZONTAL_5,
            "6": CONF_VANE_HORIZONTAL_6,
            "7": CONF_VANE_HORIZONTAL_7,
            "8": CONF_VANE_HORIZONTAL_8,
            "9": CONF_VANE_HORIZONTAL_9,
            "SWING": CONF_VANE_HORIZONTAL_SWING,
        }

        for device_vane in self._vane_horizontal:
            default_value = current_vane_horizontal_modes.get(
                device_vane,
                DEFAULT_VANE_HORIZONTAL_MODES.get(device_vane, device_vane.lower()),
            )
            config_key = vane_horizontal_to_config.get(device_vane)
            if config_key:
                schema_dict[vol.Optional(config_key, default=default_value)] = str

        return self.async_show_form(
            step_id="vane_horizontal",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "vane_horizontal": ", ".join(self._vane_horizontal),
            },
        )

    async def async_step_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure integration options (ping and datetime sync)."""
        if user_input is not None:
            # Update config entry with options
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={
                    **self._config_entry.data,
                    CONF_ENABLE_PING: user_input.get(
                        CONF_ENABLE_PING, DEFAULT_ENABLE_PING
                    ),
                    CONF_SYNC_TIME: user_input.get(CONF_SYNC_TIME, DEFAULT_SYNC_TIME),
                    CONF_USE_LOCAL_TIME: user_input.get(
                        CONF_USE_LOCAL_TIME, DEFAULT_USE_LOCAL_TIME
                    ),
                    CONF_DISPLAY_FAHRENHEIT: user_input.get(
                        CONF_DISPLAY_FAHRENHEIT, DEFAULT_DISPLAY_FAHRENHEIT
                    ),
                },
            )

            # Return to menu
            return await self.async_step_init()

        # Get current settings
        current_enable_ping = self._config_entry.data.get(
            CONF_ENABLE_PING, DEFAULT_ENABLE_PING
        )
        current_sync_time = self._config_entry.data.get(
            CONF_SYNC_TIME, DEFAULT_SYNC_TIME
        )
        current_use_local_time = self._config_entry.data.get(
            CONF_USE_LOCAL_TIME, DEFAULT_USE_LOCAL_TIME
        )
        current_display_fahrenheit = self._config_entry.data.get(
            CONF_DISPLAY_FAHRENHEIT, DEFAULT_DISPLAY_FAHRENHEIT
        )

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_ENABLE_PING,
                        default=current_enable_ping,
                    ): bool,
                    vol.Optional(
                        CONF_SYNC_TIME,
                        default=current_sync_time,
                    ): bool,
                    vol.Optional(
                        CONF_USE_LOCAL_TIME,
                        default=current_use_local_time,
                    ): bool,
                    vol.Optional(
                        CONF_DISPLAY_FAHRENHEIT,
                        default=current_display_fahrenheit,
                    ): bool,
                }
            ),
        )
