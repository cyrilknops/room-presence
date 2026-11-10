"""Room sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
    # Older versions made one sensor per source: remove those
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if ent.unique_id.startswith((f"{entry.entry_id}_source_", f"{entry.entry_id}_motion_")) and not ent.unique_id.endswith("_motion_room") or ent.unique_id == f"{entry.entry_id}_last_motion":
            ent_reg.async_remove(ent.entity_id)
    async_add_entities(
        [
            RoomSensor(tracker),
            RawRoomSensor(tracker),
            StrongestSourceSensor(tracker),
            MotionRoomSensor(tracker),
            WifiSensor(tracker),
            PhoneSensor(tracker),
        ]
    )


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


class WifiSensor(_Base):
    """The access point the phone is on, and its floor."""

    _attr_icon = "mdi:wifi-marker"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "wifi"

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_wifi"

    @property
    def native_value(self) -> str | None:
        w = self.tracker.wifi
        if not w.get("bssid"):
            return None
        return w.get("access_point") or w.get("bssid")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        w = self.tracker.wifi
        return {"bssid": w.get("bssid"), "floor_id": w.get("floor"), "floor_from": w.get("from")}


class PhoneSensor(_Base):
    """Still, normal or moving: from the screen and activity sensors of the phone."""

    _attr_icon = "mdi:cellphone-information"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "phone"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["still", "normal", "moving"]

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_phone"

    @property
    def native_value(self) -> str | None:
        return self.tracker.phone.get("state")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self.tracker.phone
        return {"screen": p.get("screen"), "activity": p.get("activity"), "screen_on_recent": p.get("screen_on_recent")}



class StrongestSourceSensor(_Base):
    """The Bluetooth proxy that hears the beacon best now."""

    _attr_icon = "mdi:bluetooth-audio"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "strongest_source"
    _unrecorded_attributes = frozenset({"rssi", "sources_hearing"})

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_strongest_source"

    @property
    def native_value(self) -> str | None:
        best = self.tracker.strongest_source()
        return best["name"] if best else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        best = self.tracker.strongest_source() or {}
        return {
            "source": best.get("source"),
            "rssi": best.get("rssi"),
            "area": best.get("area"),
            "sources_hearing": best.get("hearing", 0),
        }


class MotionRoomSensor(_Base):
    """The room with motion where the beacon most likely is, or None without motion anywhere."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "motion_room"

    def __init__(self, tracker: RoomTracker) -> None:
        super().__init__(tracker)
        self._attr_unique_id = f"{tracker.entry.entry_id}_motion_room"

    @property
    def icon(self) -> str:
        return "mdi:motion-sensor" if self.tracker.motion_room() else "mdi:motion-sensor-off"

    @property
    def native_value(self) -> str:
        best = self.tracker.motion_room()
        return (self.tracker.area_name(best["area_id"]) or "None") if best else "None"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        best = self.tracker.motion_room() or {}
        return {
            "sensor": best.get("name"),
            "entity_id": best.get("entity_id"),
            "status": best.get("status"),
            "rssi": best.get("rssi"),
        }
