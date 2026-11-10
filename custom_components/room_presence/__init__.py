"""Room Presence: fast and stable room tracking for one BLE beacon."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .tracker import RoomTracker

PLATFORMS = [Platform.SENSOR]

type RoomPresenceConfigEntry = ConfigEntry[RoomTracker]


async def async_setup_entry(hass: HomeAssistant, entry: RoomPresenceConfigEntry) -> bool:
    if "bluetooth" not in hass.config.components:
        raise ConfigEntryNotReady("Bluetooth is not loaded yet")
    tracker = RoomTracker(hass, entry)
    await tracker.async_start()
    entry.runtime_data = tracker
    entry.async_on_unload(tracker.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RoomPresenceConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: RoomPresenceConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
