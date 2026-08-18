"""IntesisBox Climate Platform."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

DOMAIN = "intesisbox"
PLATFORMS = ["climate"]

# Giving up lets Home Assistant retry this entry on its own schedule, instead
# of holding up startup for every other integration.
CONNECT_TIMEOUT = 30


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entities."""
    host = entry.data[CONF_HOST]

    from . import intesisbox

    controller = intesisbox.IntesisBox(host, loop=hass.loop)
    controller.connect()
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            while not controller.is_connected:
                await asyncio.sleep(0.1)
    except TimeoutError as err:
        controller.stop()
        raise ConfigEntryNotReady(
            f"Timed out connecting to IntesisBox at {host}"
        ) from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = controller

    if entry.unique_id is None:
        hass.config_entries.async_update_entry(
            entry, unique_id=controller.device_mac_address
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    controller = hass.data[DOMAIN][entry.entry_id]
    controller.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
