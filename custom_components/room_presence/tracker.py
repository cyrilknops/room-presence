"""Reads the beacon from all Bluetooth scanners and feeds the room engine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    ADDRESS_TTL,
    CONF_ACTIVITY,
    CONF_BEACON,
    CONF_BSSID,
    CONF_BSSID_FLOORS,
    CONF_INTERACTIVE,
    CONF_USE_MOTION,
    IBEACON_MANUFACTURER_ID,
    MOTION_CLASSES,
    MOTION_SKIP,
    MOTION_SKIP_PLATFORMS,
    MOVING_ACTIVITIES,
    SCREEN_ON_MOVING,
    STILL_ACTIVITIES,
    TUNING,
    UPDATE_INTERVAL,
)
from .engine import Context, Decision, RoomEngine, Settings

_LOGGER = logging.getLogger(__name__)
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def parse_beacon(value: str) -> tuple[str, bytes | None, int | None, int | None]:
    """Return ('mac', None, None, None) for a MAC address, else ('ibeacon', uuid, major, minor)."""
    v = value.strip()
    if MAC_RE.match(v.upper()):
        return "mac", None, None, None
    parts = v.split("_")
    uuid = bytes.fromhex(parts[0].replace("-", ""))
    if len(uuid) != 16:
        raise ValueError("iBeacon UUID must be 16 bytes")
    major = int(parts[1]) if len(parts) > 1 and parts[1] != "" else None
    minor = int(parts[2]) if len(parts) > 2 and parts[2] != "" else None
    return "ibeacon", uuid, major, minor


def ibeacon_matches(data: bytes | None, uuid: bytes, major: int | None, minor: int | None) -> bool:
    if not data or len(data) < 22 or data[0] != 0x02 or data[1] != 0x15 or data[2:18] != uuid:
        return False
    if major is not None and int.from_bytes(data[18:20], "big") != major:
        return False
    return minor is None or int.from_bytes(data[20:22], "big") == minor


def parse_bssid_floors(text: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").replace(",", "\n").splitlines():
        if "=" not in line:
            continue
        bssid, floor = (x.strip() for x in line.split("=", 1))
        if bssid and floor:
            out[bssid.lower()] = floor
    return out


def _mac_bytes(mac: str) -> bytes | None:
    try:
        b = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    except ValueError:
        return None
    return b if len(b) == 6 else None


def match_access_point(bssid: str, macs: dict[str, str]) -> str | None:
    """Find the access point of a BSSID: same MAC except the last byte (and the local bit).

    A BSSID is the AP MAC with a small change per radio and SSID. Returns the value of
    the best match in `macs` (mac -> value), or None.
    """
    b = _mac_bytes(bssid)
    if b is None:
        return None
    best: tuple[int, str] | None = None
    for mac, value in macs.items():
        m = _mac_bytes(mac)
        if m is None or (m[0] | 0x02) != (b[0] | 0x02) or m[1:5] != b[1:5]:
            continue
        diff = abs(m[5] - b[5])
        if diff <= 16 and (best is None or diff < best[0]):
            best = (diff, value)
    return best[1] if best else None


def _adv_time(scanner: Any, address: str) -> float | None:
    """Monotonic time of the last advertisement of an address on one scanner."""
    prev = getattr(scanner, "_previous_service_info", None)
    if isinstance(prev, dict):
        info = prev.get(address)
        return getattr(info, "time", None) if info else None
    stamps = getattr(scanner, "discovered_device_timestamps", None)
    return stamps.get(address) if isinstance(stamps, dict) else None


class RoomTracker:
    """One tracked beacon (for example a phone with the companion app BLE transmitter)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        cfg = {**entry.data, **entry.options}
        self.cfg = cfg
        self.mode, self.uuid, self.major, self.minor = parse_beacon(cfg[CONF_BEACON])
        self.addresses: dict[str, float] = {}  # address -> monotonic time last seen
        if self.mode == "mac":
            self.addresses[cfg[CONF_BEACON].strip().upper()] = float("inf")
        settings = Settings(**{k: float(cfg.get(k, d[0])) for k, d in TUNING.items()})
        self.engine = RoomEngine(settings)
        self.bssid_floors = parse_bssid_floors(cfg.get(CONF_BSSID_FLOORS))
        self.ap_floor: dict[str, str] = {}
        self.decision: Decision | None = None
        self.raw_best: str | None = None
        self.context = Context()
        self._last_stamp: dict[tuple[str, str], float] = {}
        self._listeners: list[Callable[[], None]] = []
        # Every Bluetooth source seen: source -> display name (for the diagnostic sensors)
        self.sources: dict[str, str] = {}
        # Other sources, for the diagnostic sensors
        self.ap_name: dict[str, str] = {}
        self.wifi: dict[str, Any] = {}
        self.phone: dict[str, Any] = {}
        self.motion_sources: dict[str, dict[str, Any]] = {}
        self._signature: tuple | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._map_at = -1e9
        self._screen_on_at: float | None = None
        self._last_interactive: str | None = None

    # -------------------------------------------------------------- setup
    async def async_start(self) -> None:
        if self.mode == "ibeacon":
            matcher = bluetooth.BluetoothCallbackMatcher(
                manufacturer_id=IBEACON_MANUFACTURER_ID,
                manufacturer_data_start=[0x02, 0x15, *self.uuid],
                connectable=False,
            )
            self._unsubs.append(
                bluetooth.async_register_callback(
                    self.hass, self._on_advertisement, matcher, bluetooth.BluetoothScanningMode.PASSIVE
                )
            )
            for info in bluetooth.async_discovered_service_info(self.hass, connectable=False):
                self._learn(info)
        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=UPDATE_INTERVAL))
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    def add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    # -------------------------------------------------------------- beacon
    def _learn(self, info: Any) -> bool:
        data = (info.manufacturer_data or {}).get(IBEACON_MANUFACTURER_ID)
        if self.uuid is None or not ibeacon_matches(data, self.uuid, self.major, self.minor):
            return False
        self.addresses[info.address.upper()] = bluetooth.MONOTONIC_TIME()
        return True

    @callback
    def _on_advertisement(self, info: Any, change: Any) -> None:
        if self._learn(info):
            self._collect()
            self._decide()

    # -------------------------------------------------------------- scanners -> areas
    def _build_map(self) -> None:
        dev_reg = dr.async_get(self.hass)
        area_reg = ar.async_get(self.hass)
        scanner_area: dict[str, str] = {}
        for scanner in bluetooth.async_current_scanners(self.hass):
            source = scanner.source.upper()
            device = None
            for kind in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC):
                device = dev_reg.async_get_device(connections={(kind, source)}) or dev_reg.async_get_device(
                    connections={(kind, source.lower())}
                )
                if device:
                    break
            if device and device.area_id:
                scanner_area[source] = device.area_id
            name = (device.name_by_user or device.name) if device else (getattr(scanner, "name", None) or source)
            name = re.sub(r"\s*\([0-9A-Fa-f:]{17}\)\s*$", "", name or source).strip() or source
            self.sources[source] = name
        self.engine.scanner_area = scanner_area
        self.engine.area_floor = {a.id: a.floor_id for a in area_reg.async_list_areas()}
        # Access points (any device with a MAC and an area on a floor) for the Wi-Fi floor
        ap_floor: dict[str, str] = {}
        ap_name: dict[str, str] = {}
        for device in dev_reg.devices.values():
            floor = self.engine.area_floor.get(device.area_id) if device.area_id else None
            if not floor:
                continue
            for kind, mac in device.connections:
                if kind == dr.CONNECTION_NETWORK_MAC:
                    ap_floor[mac.lower()] = floor
                    ap_name[mac.lower()] = device.name_by_user or device.name or mac
        self.ap_floor = ap_floor
        self.ap_name = ap_name

    def _collect(self) -> None:
        now = bluetooth.MONOTONIC_TIME()
        for address, seen in list(self.addresses.items()):
            if now - seen > ADDRESS_TTL:
                del self.addresses[address]
                continue
            for sd in bluetooth.async_scanner_devices_by_address(self.hass, address, connectable=False):
                source = sd.scanner.source.upper()
                stamp = _adv_time(sd.scanner, address)
                if stamp is None:
                    continue
                key = (source, address)
                if self._last_stamp.get(key) == stamp:
                    continue
                self._last_stamp[key] = stamp
                if self.mode == "ibeacon":
                    data = (sd.advertisement.manufacturer_data or {}).get(IBEACON_MANUFACTURER_ID)
                    if not ibeacon_matches(data, self.uuid, self.major, self.minor):
                        continue
                    self.addresses[address] = max(self.addresses[address], stamp)
                self.engine.add_sample(source, sd.advertisement.rssi, stamp)
                if source not in self.engine.scanner_area and now - self._map_at > 10:
                    # A proxy that connected after the start: map it to its area now
                    self._map_at = now
                    self._build_map()

    # -------------------------------------------------------------- context
    def _mono_of(self, when) -> float:
        return bluetooth.MONOTONIC_TIME() - (dt_util.utcnow() - when).total_seconds()

    def _build_context(self) -> Context:
        hass = self.hass
        cfg = self.cfg
        now = bluetooth.MONOTONIC_TIME()
        ctx = Context()
        inter = hass.states.get(cfg.get(CONF_INTERACTIVE) or "")
        if inter is not None:
            if inter.state == "on" and self._last_interactive == "off":
                self._screen_on_at = now
            self._last_interactive = inter.state
        act = hass.states.get(cfg.get(CONF_ACTIVITY) or "")
        activity = act.state.lower() if act else None
        screen_recent = self._screen_on_at is not None and now - self._screen_on_at < SCREEN_ON_MOVING
        ctx.moving = (activity in MOVING_ACTIVITIES) or screen_recent
        ctx.still = (not ctx.moving and inter is not None and inter.state == "off"
                     and (activity is None or activity in STILL_ACTIVITIES))
        self.phone = {
            "state": "moving" if ctx.moving else "still" if ctx.still else "normal",
            "screen": inter.state if inter else None,
            "activity": activity,
            "screen_on_recent": screen_recent,
        }
        motion: dict[str, dict[str, Any]] = {}
        if cfg.get(CONF_USE_MOTION, True):
            window = self.engine.settings.motion_window
            ent_reg = er.async_get(hass)
            dev_reg = dr.async_get(hass)
            candidates = [
                st for st in hass.states.async_all("binary_sensor")
                if st.attributes.get("device_class") in MOTION_CLASSES and st.state in ("on", "off")
            ]
            for state in candidates:
                eid = state.entity_id
                if any(s in eid for s in MOTION_SKIP):
                    continue
                entry = ent_reg.async_get(eid)
                if entry is None or entry.hidden or entry.platform in MOTION_SKIP_PLATFORMS:
                    continue
                area = entry.area_id
                if not area and entry.device_id and (dev := dev_reg.async_get(entry.device_id)):
                    area = dev.area_id
                if not area:
                    continue
                status = "clear"
                if state.state == "on":
                    ctx.motion_on.add(area)
                    ctx.motion_recent.add(area)
                    status = "motion"
                elif now - self._mono_of(state.last_changed) < window:
                    ctx.motion_recent.add(area)
                    status = "recent"
                motion[eid] = {
                    "status": status,
                    "area_id": area,
                    "name": state.attributes.get("friendly_name") or eid,
                    "last_change": state.last_changed.isoformat(),
                }
        self.motion_sources = motion
        bssid = hass.states.get(cfg.get(CONF_BSSID) or "")
        self.wifi = {"bssid": bssid.state if bssid else None, "access_point": None, "floor": None, "from": None}
        if bssid is not None and bssid.state not in ("unknown", "unavailable", "<not connected>"):
            # The manual list first, else the floor of the access point device
            manual = self.bssid_floors.get(bssid.state.lower())
            ap_mac = match_access_point(bssid.state, {m: m for m in self.ap_floor})
            ctx.wifi_floor = manual or (self.ap_floor.get(ap_mac) if ap_mac else None)
            self.wifi.update(
                access_point=self.ap_name.get(ap_mac) if ap_mac else None,
                floor=ctx.wifi_floor,
                **{"from": "manual list" if manual else "access point area" if ap_mac else None},
            )
        return ctx

    # -------------------------------------------------------------- loop
    def _decide(self) -> None:
        now = bluetooth.MONOTONIC_TIME()
        self.context = self._build_context()
        decision = self.engine.evaluate(now, self.context)
        # Also tell the sensors at once when motion, Wi-Fi or the phone state changes
        signature = (
            tuple(sorted((e, i["status"]) for e, i in self.motion_sources.items())),
            self.wifi.get("access_point") or self.wifi.get("bssid"),
            self.phone.get("state"),
        )
        changed = (
            self.decision is None
            or decision.area != self.decision.area
            or decision.best != self.raw_best
            or signature != self._signature
        )
        self._signature = signature
        if decision.changed:
            _LOGGER.debug("%s: %s -> %s (%s)", self.entry.title, self.decision and self.decision.area, decision.area, decision.reason)
        self.decision = decision
        self.raw_best = decision.best
        if changed or int(now) % 5 == 0:
            for update in list(self._listeners):
                update()

    @callback
    def _tick(self, _now=None) -> None:
        mono = bluetooth.MONOTONIC_TIME()
        if mono - self._map_at > 60:
            self._map_at = mono
            self._build_map()
        self._collect()
        self._decide()

    # -------------------------------------------------------------- output helpers
    def area_name(self, area_id: str | None) -> str | None:
        if not area_id:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else area_id

    def strongest_source(self) -> dict[str, Any] | None:
        values = self.engine.scanner_values(bluetooth.MONOTONIC_TIME())
        if not values:
            return None
        source = max(values, key=values.get)
        return {
            "source": source,
            "name": self.sources.get(source, source),
            "rssi": round(values[source]),
            "area": self.area_name(self.engine.scanner_area.get(source)),
            "hearing": len(values),
        }

    def motion_room(self) -> dict[str, Any] | None:
        """The room with motion now (or a moment ago) where the beacon most likely is.

        Among the rooms with motion, the one with the strongest Bluetooth signal wins;
        motion now goes before motion a moment ago. None when there is no motion anywhere.
        """
        active = [{"entity_id": eid, **i} for eid, i in self.motion_sources.items() if i["status"] in ("motion", "recent")]
        if not active:
            return None
        areas = self.engine.area_values(bluetooth.MONOTONIC_TIME())
        best = max(
            active,
            key=lambda i: (areas.get(i["area_id"], -999.0), i["status"] == "motion", i["last_change"]),
        )
        return {**best, "rssi": round(areas[best["area_id"]]) if best["area_id"] in areas else None}

    @property
    def last_seen_ago(self) -> float | None:
        if self.engine.last_seen is None:
            return None
        return round(bluetooth.MONOTONIC_TIME() - self.engine.last_seen, 1)
