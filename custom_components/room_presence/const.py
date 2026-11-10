"""Constants for Room Presence."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "room_presence"
VERSION: Final = "2026.11.10"

CONF_BEACON: Final = "beacon"  # iBeacon "uuid_major_minor" or a fixed MAC address
CONF_INTERACTIVE: Final = "interactive_entity"
CONF_ACTIVITY: Final = "activity_entity"
CONF_BSSID: Final = "bssid_entity"
CONF_BSSID_FLOORS: Final = "bssid_floors"  # text: one "aa:bb:cc:dd:ee:ff = floor_id" per line
CONF_USE_MOTION: Final = "use_motion"

# Tuning (options); names match engine.Settings
TUNING: Final = {
    "margin": (5.0, 0, 30, 0.5, "dB"),
    "dwell": (2.0, 0, 60, 0.5, "s"),
    "still_margin": (10.0, 0, 40, 0.5, "dB"),
    "still_dwell": (12.0, 0, 120, 1, "s"),
    "moving_margin": (3.0, 0, 30, 0.5, "dB"),
    "moving_dwell": (1.0, 0, 30, 0.5, "s"),
    "motion_bonus": (4.0, 0, 20, 0.5, "dB"),
    "floor_penalty": (6.0, 0, 30, 0.5, "dB"),
    "away_after": (90.0, 10, 1800, 5, "s"),
}

UPDATE_INTERVAL: Final = 1.0  # s
ADDRESS_TTL: Final = 1800.0  # s that a learned beacon address stays in use
SCREEN_ON_MOVING: Final = 15.0  # s after the screen turns on that the phone counts as moving
MOVING_ACTIVITIES: Final = {"walking", "running", "on_foot", "on_bicycle", "in_vehicle"}
STILL_ACTIVITIES: Final = {"still", "stationary", "unknown", "tilting"}
MOTION_CLASSES: Final = {"motion", "occupancy", "presence"}
MOTION_SKIP: Final = ("doorbell", "echo_dot", "esp32_s3_box", "nspanel")
MOTION_SKIP_PLATFORMS: Final = {"browser_mod", "alexa_devices", "bermuda", "mobile_app"}
IBEACON_MANUFACTURER_ID: Final = 76
