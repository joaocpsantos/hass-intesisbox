"""The Intesis integration for Intesis Air Conditioning Gateways (WMP Protocol)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging

from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.const import CONF_HOST, Platform  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.exceptions import ConfigEntryNotReady  # type: ignore

from .const import (
    CONF_ENABLE_PING,
    CONF_SYNC_TIME,
    CONF_USE_LOCAL_TIME,
    DEFAULT_ENABLE_PING,
    DEFAULT_SYNC_TIME,
    DEFAULT_USE_LOCAL_TIME,
    DOMAIN,
)
from .intesisbox import IntesisBox

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intesis Gateway from a config entry."""
    host = entry.data[CONF_HOST]
    name = entry.title

    log_prefix = f"[{name} ({host})]"
    _LOGGER.info("%s Setting up Intesis Gateway integration", log_prefix)

    # Ensure DOMAIN exists in hass.data
    hass.data.setdefault(DOMAIN, {})

    # Check if there's already a controller for this entry (shouldn't happen but be safe)
    existing_controller = hass.data[DOMAIN].get(entry.entry_id)
    if existing_controller:
        _LOGGER.warning(
            "%s Found existing controller, cleaning up before creating new one",
            log_prefix,
        )
        existing_controller.stop()
        await existing_controller.wait_for_disconnect(timeout=2.0)
        hass.data[DOMAIN].pop(entry.entry_id, None)

    # Create controller
    enable_ping = entry.data.get(CONF_ENABLE_PING, DEFAULT_ENABLE_PING)
    controller = IntesisBox(host, loop=hass.loop, name=name, enable_ping=enable_ping)

    # Connect to device (this is synchronous but schedules async work)
    _LOGGER.debug("%s Calling controller.connect()", log_prefix)
    controller.connect()

    # Wait for the connection to be established and initialized
    _LOGGER.debug("%s Waiting for connection and initialization...", log_prefix)
    for i in range(150):  # Wait up to 15 seconds for all limits including vanes
        if controller.is_connected and len(controller.operation_list) > 0:
            # Core initialization is done (have connection and operation modes)
            # Wait a bit longer for optional features (fan speeds, vanes)
            if i >= 60:  # After 6 seconds, proceed even if optional features missing
                # Build log prefix matching the format used elsewhere
                if controller.device_mac_address:
                    entity_id = f"climate.{controller.device_mac_address.lower()}"
                    log_prefix = f"[{name}({entity_id})]"
                else:
                    log_prefix = f"[{name}({host})]"

                _LOGGER.info(
                    "%s Intesis Gateway initialized (fans: %d, vanes: v=%d h=%d)",
                    log_prefix,
                    len(controller.fan_speed_list),
                    len(controller.vane_vertical_list),
                    len(controller.vane_horizontal_list),
                )

                # Query device datetime (always done, just for logging)
                _LOGGER.debug("%s Querying device datetime", log_prefix)
                controller.query_datetime()
                await asyncio.sleep(0.5)  # Give device time to respond

                # Check if we should sync time
                sync_time = entry.data.get(CONF_SYNC_TIME, DEFAULT_SYNC_TIME)
                if sync_time:
                    use_local_time = entry.data.get(
                        CONF_USE_LOCAL_TIME, DEFAULT_USE_LOCAL_TIME
                    )

                    # Get current time
                    if use_local_time:
                        now = datetime.now()
                        time_type = "local"
                    else:
                        now = datetime.now(UTC)
                        time_type = "UTC"

                    # Format as DD/MM/YYYY HH:MM:SS
                    datetime_str = now.strftime("%d/%m/%Y %H:%M:%S")

                    _LOGGER.info(
                        "%s Setting device time to %s (%s)",
                        log_prefix,
                        datetime_str,
                        time_type,
                    )
                    await controller.set_datetime_async(datetime_str)
                    await asyncio.sleep(0.5)  # Give device time to process

                    # Query again to confirm
                    controller.query_datetime()
                    await asyncio.sleep(0.5)

                # SUCCESS - Store controller ONLY after successful initialization
                hass.data[DOMAIN][entry.entry_id] = controller

                break
        await asyncio.sleep(0.1)
    else:
        # Timeout - device not responding
        _LOGGER.warning(
            "%s Initialization timeout after 15 seconds - device not responding. Will retry automatically.",
            log_prefix,
        )

        # Clean up failed connection
        controller.stop()
        await controller.wait_for_disconnect(timeout=5.0)

        # Make absolutely sure nothing is stored
        hass.data[DOMAIN].pop(entry.entry_id, None)

        # Raise ConfigEntryNotReady to trigger HA's automatic retry
        raise ConfigEntryNotReady(f"Device at {host} not available")

    # Forward entry setup to climate platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options changes
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    log_prefix = f"[{entry.title} ({entry.data[CONF_HOST]})]"
    _LOGGER.info("%s Unloading Intesis Gateway integration", log_prefix)

    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Stop and remove controller
        controller = hass.data[DOMAIN].pop(entry.entry_id, None)
        if controller:
            _LOGGER.debug("%s Stopping controller", log_prefix)
            controller.stop()
            # Wait for connection to ACTUALLY close (not just initiated)
            disconnect_ok = await controller.wait_for_disconnect(timeout=5.0)
            if disconnect_ok:
                # Connection closed successfully, now enforce protocol minimum delay
                _LOGGER.debug(
                    "%s Connection closed, waiting protocol minimum delay", log_prefix
                )
                await asyncio.sleep(1.0)
            else:
                # Timeout waiting for disconnect, use longer delay to be safe
                _LOGGER.warning(
                    "%s Disconnect timeout, using extended delay", log_prefix
                )
                await asyncio.sleep(3.0)

    return unload_ok


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)
