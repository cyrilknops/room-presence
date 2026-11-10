"""Diagnostics: every Bluetooth source, what it hears from the beacon, and the decision."""

from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr

from . import RoomPresenceConfigEntry
from .const import TUNING


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: RoomPresenceConfigEntry) -> dict[str, Any]:
    tracker = entry.runtime_data
    engine = tracker.engine
    now = bluetooth.MONOTONIC_TIME()
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    values = engine.scanner_values(now)

    def area_name(area_id: str | None) -> str | None:
        area = area_reg.async_get_area(area_id) if area_id else None
        return area.name if area else None

    sources = []
    for scanner in bluetooth.async_current_scanners(hass):
        source = scanner.source.upper()
        device = None
        for kind in (dr.CONNECTION_BLUETOOTH, dr.CONNECTION_NETWORK_MAC):
            device = dev_reg.async_get_device(connections={(kind, source)}) or dev_reg.async_get_device(
                connections={(kind, source.lower())}
            )
            if device:
                break
        samples = list(engine._samples.get(source, []))  # noqa: SLF001
        area_id = engine.scanner_area.get(source)
        sources.append(
            {
                "source": source,
                "name": getattr(scanner, "name", None),
                "type": type(scanner).__name__,
                "connectable": getattr(scanner, "connectable", None),
                "device": (device.name_by_user or device.name) if device else None,
                "area_id": area_id,
                "area": area_name(area_id),
                "floor_id": engine.area_floor.get(area_id) if area_id else None,
                "hears_beacon": bool(samples),
                "rssi_now": round(values[source], 1) if source in values else None,
                "last_sample_s": round(now - samples[-1][0], 1) if samples else None,
                "report_gap_s": round(engine.gap(source), 2) if samples else None,
                "samples": [{"age_s": round(now - t, 1), "rssi": r} for t, r in samples[-10:]],
                "offset_db": engine.offsets.get(source, 0.0),
                "problem": None
                if area_id
                else "no area: give this proxy device an area"
                if device
                else "no device found for this source",
            }
        )
    sources.sort(key=lambda s: (s["rssi_now"] is None, -(s["rssi_now"] or -999)))

    d = tracker.decision
    ctx = tracker.context
    cfg = tracker.cfg
    return {
        "config": {
            "title": entry.title,
            "beacon": cfg.get("beacon"),
            "interactive_entity": cfg.get("interactive_entity"),
            "activity_entity": cfg.get("activity_entity"),
            "bssid_entity": cfg.get("bssid_entity"),
            "use_motion": cfg.get("use_motion", True),
            "bssid_floors": tracker.bssid_floors,
            "tuning": {k: cfg.get(k, v[0]) for k, v in TUNING.items()},
        },
        "decision": {
            "area_id": d.area if d else None,
            "area": area_name(d.area) if d else None,
            "strongest": d.best if d else None,
            "lead_db": round(d.lead, 1) if d and d.lead is not None else None,
            "reason": d.reason if d else None,
            "areas_db": {a: round(v, 1) for a, v in (d.areas if d else {}).items()},
            "switched_s_ago": round(now - engine.switched_at, 1) if engine.switched_at else None,
            "last_seen_s": tracker.last_seen_ago,
        },
        "context": {
            "still": ctx.still,
            "moving": ctx.moving,
            "motion_on": sorted(ctx.motion_on),
            "motion_recent": sorted(ctx.motion_recent),
            "wifi_floor": ctx.wifi_floor,
        },
        "beacon_addresses": {a: (round(now - t, 1) if t != float("inf") else "fixed") for a, t in tracker.addresses.items()},
        "access_points": tracker.ap_floor,
        "sources": sources,
    }
