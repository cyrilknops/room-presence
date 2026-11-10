"""Room sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import RoomPresenceConfigEntry
from .const import DOMAIN, VERSION
from .tracker import RoomTracker


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoomPresenceConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    tracker = entry.runtime_data
    async_add_entities([RoomSensor(tracker), RawRoomSensor(tracker)])


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, tracker: RoomTracker) -> None:
        self.tracker = tracker
        entry = tracker.entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Room Presence",
            model="BLE room tracker",
            sw_version=VERSION,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.tracker.add_listener(self.async_write_ha_state))


class RoomSensor(_Base):
    """The room, with hold against noise. The state is the area name."""

    _attr_translation_key = "room"
    _attr_icon = "mdi:home-account"
    # These change every few seconds: keep them out of the history database
    _unrecorded_attributes = frozenset({"areas", "lead_db", "reason", "last_seen_s", "context", "wifi_floor"})

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_room"

    @property
    def native_value(self) -> str | None:
        d = self.tracker.decision
        return self.tracker.area_name(d.area) if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.tracker
        d = t.decision
        ctx = t.context
        return {
            "room_presence": True,
            "area_id": d.area if d else None,
            "floor_id": t.engine.area_floor.get(d.area) if d and d.area else None,
            "lead_db": round(d.lead, 1) if d and d.lead is not None else None,
            "reason": d.reason if d else None,
            "last_seen_s": t.last_seen_ago,
            "areas": {a: round(v, 1) for a, v in sorted((d.areas if d else {}).items(), key=lambda x: -x[1])[:6]},
            "context": "moving" if ctx.moving else "still" if ctx.still else "normal",
            "wifi_floor": ctx.wifi_floor,
        }


class RawRoomSensor(_Base):
    """The strongest room at this moment, without hold. For comparison and tuning."""

    _attr_translation_key = "raw_room"
    _attr_icon = "mdi:access-point"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_raw_room"

    @property
    def native_value(self) -> str | None:
        return self.tracker.area_name(self.tracker.raw_best)
