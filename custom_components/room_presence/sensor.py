"""Room sensors."""

from __future__ import annotations

from typing import Any

import time

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
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
    added: set[str] = set()

    def _add_sources(sources: list[str]) -> None:
        fresh = [s for s in sources if s not in added]
        added.update(fresh)
        if fresh:
            async_add_entities(SourceSensor(tracker, s) for s in fresh)

    tracker.on_new_sources(_add_sources)
    async_add_entities([WifiSensor(tracker), PhoneSensor(tracker)])
    motion_added: set[str] = set()

    def _add_motion(eids: list[str]) -> None:
        fresh = [e for e in eids if e not in motion_added]
        motion_added.update(fresh)
        if fresh:
            async_add_entities(MotionSourceSensor(tracker, e) for e in fresh)

    tracker.on_new_motion(_add_motion)


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


class SourceSensor(_Base):
    """What one Bluetooth source (proxy) hears from the beacon, in dBm.

    It writes its state only on a change of 3 dB or more, or once a minute, to keep the history small.
    """

    _attr_icon = "mdi:bluetooth-audio"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _unrecorded_attributes = frozenset({"last_sample_s", "report_gap_s"})
    MIN_STEP = 3
    MAX_AGE = 60.0

    def __init__(self, tracker: RoomTracker, source: str) -> None:
        super().__init__(tracker)
        self.source = source
        self._attr_unique_id = f"{tracker.entry.entry_id}_source_{source.replace(':', '').lower()}"
        self._attr_name = f"{tracker.sources.get(source, source)} signal"
        self._info: dict[str, Any] = tracker.source_info(source)
        self._written_at = 0.0

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.tracker.add_listener(self._maybe_write))

    def _maybe_write(self) -> None:
        info = self.tracker.source_info(self.source)
        old = self._info.get("rssi")
        new = info["rssi"]
        changed = (old is None) != (new is None) or (old is not None and abs(new - old) >= self.MIN_STEP)
        if changed or info["area"] != self._info.get("area") or time.monotonic() - self._written_at >= self.MAX_AGE:
            self._info = info
            self._written_at = time.monotonic()
            self.async_write_ha_state()

    @property
    def native_value(self) -> int | None:
        return self._info.get("rssi")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "area": self._info.get("area"),
            "last_sample_s": self._info.get("last_sample_s"),
            "report_gap_s": self._info.get("report_gap_s"),
        }


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


class MotionSourceSensor(_Base):
    """One motion sensor that Room Presence uses: motion now, recent (still counts) or clear."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["motion", "recent", "clear"]

    def __init__(self, tracker: RoomTracker, entity_id: str) -> None:
        super().__init__(tracker)
        self.source = entity_id
        info = tracker.motion_sources.get(entity_id, {})
        self._attr_unique_id = f"{tracker.entry.entry_id}_motion_{entity_id.split('.', 1)[1]}"
        area = tracker.area_name(info.get("area_id")) or ""
        self._attr_name = f"Motion {area} ({info.get('name', entity_id)})".replace("  ", " ")

    @property
    def icon(self) -> str:
        return "mdi:motion-sensor" if self.native_value != "clear" else "mdi:motion-sensor-off"

    @property
    def native_value(self) -> str | None:
        return self.tracker.motion_sources.get(self.source, {}).get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self.tracker.motion_sources.get(self.source, {})
        return {"source": self.source, "area": self.tracker.area_name(info.get("area_id")), "last_change": info.get("last_change")}
