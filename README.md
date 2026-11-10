# Room Presence

Fast and stable room tracking for one BLE beacon (for example the iBeacon of the Home Assistant companion app), with the Bluetooth proxies that you already have.

## How it works

- Every second, it reads the beacon from **all** Bluetooth scanners (ESPHome proxies, Shelly, local adapters).
- The room of a scanner is the area of its device in Home Assistant.
- Per scanner: the median of the last 4 s. A scanner that stops hearing the beacon fades out (4 dB/s after 1.5 s).
- Per room: the strongest scanner in that room.
- A new room must be stronger than the current room by a **margin** for a **dwell time**:

| Situation | Margin | Time |
|---|---|---|
| Normal | 5 dB | 2 s |
| Phone still (screen off, not walking) | 10 dB | 12 s |
| Walking, or screen just turned on | 3 dB | 1 s |
| Fresh motion in the new room | −4 dB, at most 1 s | |
| Motion only in the current room | +4 dB | |
| Wi-Fi access point on the other floor | +6 dB | |

- When no scanner in the current room hears the beacon for 5 s, the strongest other room wins.
- After 90 s without any signal the room is unknown.

## Entities

- `sensor.<name>_room`: the room (area name). Attributes: `area_id`, `floor_id`, `lead_db`, `reason`, `areas` (dB per room), `context`.
- `sensor.<name>_strongest_room` (diagnostic): the strongest room at this moment, without hold. Use it to compare.

## Setup

1. Companion app: Settings → Companion app → Manage sensors → **BLE transmitter**: on.
   For fast tracking set *Advertise mode* to **Low latency** and *Transmit power* to **High** (uses more battery).
2. Optional sensors in the companion app: **Interactive**, **Detected activity**, **Wi-Fi BSSID**.
3. Give every Bluetooth proxy device an area.
4. Settings → Devices & services → Add integration → **Room Presence**. Pick the phone.

All margins and times can be changed in the options.

### Wi-Fi floor

With the phone's **Wi-Fi BSSID** sensor, the integration knows the floor of the access point you are on: give each access point device (for example from the UniFi integration) an area on a floor. A BSSID is matched to the access point with the same MAC address except the last byte. If that does not work for your access points, the options take a manual list: one `BSSID = floor_id` per line.
