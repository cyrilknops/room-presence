"""Integration tests with fake Bluetooth scanners."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import bluetooth as real_bt
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from custom_components.room_presence.const import DOMAIN
from custom_components.room_presence.tracker import ibeacon_matches, parse_beacon

UUID = "8e16244496a544dfa721c7cc6f9933f7"
BEACON = f"{UUID}_100_40004"
ADDR = "4A:11:22:33:44:55"
LIVING = "AA:00:00:00:00:01"
KITCHEN = "AA:00:00:00:00:02"


def ibeacon_data(uuid=UUID, major=100, minor=40004):
    return bytes([0x02, 0x15]) + bytes.fromhex(uuid) + major.to_bytes(2, "big") + minor.to_bytes(2, "big") + b"\xc5"


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


class FakeBT:
    """Stands in for homeassistant.components.bluetooth."""

    BluetoothCallbackMatcher = real_bt.BluetoothCallbackMatcher
    BluetoothScanningMode = real_bt.BluetoothScanningMode

    def __init__(self):
        self.now = 1000.0
        self.scanners = {s: SimpleNamespace(source=s, _previous_service_info={}) for s in (LIVING, KITCHEN)}
        self.rssi = {}
        self.callbacks = []

    def MONOTONIC_TIME(self):  # noqa: N802
        return self.now

    def advertise(self, **rssi):
        """One advertisement per scanner at the current time."""
        for name, value in rssi.items():
            src = {"living": LIVING, "kitchen": KITCHEN}[name]
            self.rssi[src] = value
            self.scanners[src]._previous_service_info[ADDR] = SimpleNamespace(time=self.now)

    def async_register_callback(self, hass, cb, matcher, mode):
        self.callbacks.append(cb)
        return lambda: None

    def async_discovered_service_info(self, hass, connectable=True):
        return [SimpleNamespace(address=ADDR, manufacturer_data={76: ibeacon_data()})]

    def async_current_scanners(self, hass):
        return list(self.scanners.values())

    def async_scanner_devices_by_address(self, hass, address, connectable=True):
        return [
            SimpleNamespace(scanner=self.scanners[s], advertisement=SimpleNamespace(rssi=r, manufacturer_data={76: ibeacon_data()}))
            for s, r in self.rssi.items()
            if address == ADDR
        ]


async def _setup(hass, fake, options=None):
    hass.config.components.add("bluetooth")
    areas = ar.async_get(hass)
    living = areas.async_create("Living Room")
    kitchen = areas.async_create("Kitchen")
    owner = MockConfigEntry(domain="esphome")
    owner.add_to_hass(hass)
    devs = dr.async_get(hass)
    for mac, area in ((LIVING, living.id), (KITCHEN, kitchen.id)):
        d = devs.async_get_or_create(config_entry_id=owner.entry_id, connections={(dr.CONNECTION_BLUETOOTH, mac)})
        devs.async_update_device(d.id, area_id=area)
    entry = MockConfigEntry(domain=DOMAIN, title="Cyril", data={"beacon": BEACON}, options=options or {}, unique_id=BEACON)
    entry.add_to_hass(hass)
    with patch("custom_components.room_presence.tracker.bluetooth", fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, living, kitchen


def _step(hass, entry, fake, seconds=1.0, **rssi):
    fake.now += seconds
    if rssi:
        fake.advertise(**rssi)
    with patch("custom_components.room_presence.tracker.bluetooth", fake):
        entry.runtime_data._tick()


def test_beacon_parsing():
    assert parse_beacon(BEACON)[0] == "ibeacon"
    assert parse_beacon("aa:bb:cc:dd:ee:ff")[0] == "mac"
    _, uuid, major, minor = parse_beacon(BEACON)
    assert ibeacon_matches(ibeacon_data(), uuid, major, minor)
    assert not ibeacon_matches(ibeacon_data(minor=1), uuid, major, minor)


async def test_room_follows_beacon(hass: HomeAssistant) -> None:
    fake = FakeBT()
    entry, living, kitchen = await _setup(hass, fake)
    _step(hass, entry, fake, living=-58, kitchen=-80)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.cyril_room")
    assert state.state == "Living Room"
    assert state.attributes["area_id"] == living.id
    assert state.attributes["room_presence"] is True

    # Walk to the kitchen: a clear lead for more than the dwell time
    for _ in range(8):
        _step(hass, entry, fake, living=-82, kitchen=-57)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cyril_room").state == "Kitchen"
    assert hass.states.get("sensor.cyril_strongest_room").state == "Kitchen"


async def test_still_phone_and_motion(hass: HomeAssistant) -> None:
    fake = FakeBT()
    hass.states.async_set("binary_sensor.flip7_interactive", "off")
    entry, living, kitchen = await _setup(hass, fake, {"interactive_entity": "binary_sensor.flip7_interactive"})
    _step(hass, entry, fake, living=-60, kitchen=-80)
    # Phone on the table, screen off: an 8 dB lead for 10 s is not enough
    for _ in range(10):
        _step(hass, entry, fake, living=-69, kitchen=-61)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cyril_room").state == "Living Room"
    assert hass.states.get("sensor.cyril_room").attributes["context"] == "still"

    # Motion in the kitchen: 10 - 4 = 6 dB, 1 s dwell
    ent = er.async_get(hass).async_get_or_create("binary_sensor", "zha", "kmotion", suggested_object_id="kitchen_motion")
    er.async_get(hass).async_update_entity(ent.entity_id, area_id=kitchen.id)
    hass.states.async_set(ent.entity_id, "on", {"device_class": "motion"})
    for _ in range(3):
        _step(hass, entry, fake, living=-69, kitchen=-61)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.cyril_room").state == "Kitchen"


async def test_config_flow_lists_companion_beacon(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.flip7_ble_transmitter", "Transmitting", {"id": BEACON, "friendly_name": "Flip7 BLE transmitter"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema["beacon"].config["options"]
    assert options[0]["value"] == BEACON
    bad = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Cyril", "beacon": "nonsense"})
    assert bad["errors"] == {"beacon": "bad_beacon"}
    with patch("custom_components.room_presence.async_setup_entry", return_value=True):
        done = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Cyril", "beacon": BEACON})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["title"] == "Cyril"
    assert done["data"] == {"beacon": BEACON, "use_motion": True}
