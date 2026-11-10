"""Config flow for Room Presence."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_ACTIVITY,
    CONF_BEACON,
    CONF_BSSID,
    CONF_BSSID_FLOORS,
    CONF_INTERACTIVE,
    CONF_USE_MOTION,
    DOMAIN,
    TUNING,
)
from .tracker import parse_beacon

OPTIONAL_ENTITIES = (CONF_INTERACTIVE, CONF_ACTIVITY, CONF_BSSID)


def _beacons(hass: HomeAssistant) -> list[SelectOptionDict]:
    """iBeacons sent by the companion app (sensor.*_ble_transmitter)."""
    out: list[SelectOptionDict] = []
    for state in hass.states.async_all("sensor"):
        if state.entity_id.endswith("_ble_transmitter") and (bid := state.attributes.get("id")):
            name = (state.attributes.get("friendly_name") or state.entity_id).replace(" BLE transmitter", "")
            out.append(SelectOptionDict(value=str(bid), label=f"{name} ({bid})"))
    return out


def _entity_fields() -> dict:
    return {
        vol.Optional(CONF_INTERACTIVE): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
        vol.Optional(CONF_ACTIVITY): EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_BSSID): EntitySelector(EntitySelectorConfig(domain="sensor")),
        vol.Optional(CONF_USE_MOTION, default=True): BooleanSelector(),
    }


def _valid_beacon(value: str) -> bool:
    try:
        parse_beacon(value)
    except (ValueError, IndexError):
        return False
    return True


class RoomPresenceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _valid_beacon(user_input[CONF_BEACON]):
                errors[CONF_BEACON] = "bad_beacon"
            else:
                await self.async_set_unique_id(user_input[CONF_BEACON].strip().lower())
                self._abort_if_unique_id_configured()
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(title=name, data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): str,
                vol.Required(CONF_BEACON): SelectSelector(
                    SelectSelectorConfig(options=_beacons(self.hass), custom_value=True, mode=SelectSelectorMode.DROPDOWN)
                ),
                **_entity_fields(),
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=self.add_suggested_values_to_schema(schema, user_input or {}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RoomPresenceOptionsFlow:
        return RoomPresenceOptionsFlow()


class RoomPresenceOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for key in OPTIONAL_ENTITIES:
                user_input.setdefault(key, None)
            user_input.setdefault(CONF_BSSID_FLOORS, "")
            return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        tuning = {
            vol.Optional(key, default=d[0]): NumberSelector(
                NumberSelectorConfig(min=d[1], max=d[2], step=d[3], unit_of_measurement=d[4], mode=NumberSelectorMode.BOX)
            )
            for key, d in TUNING.items()
        }
        schema = vol.Schema(
            {
                **_entity_fields(),
                vol.Optional(CONF_BSSID_FLOORS): TextSelector(TextSelectorConfig(multiline=True)),
                **tuning,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, {k: v for k, v in current.items() if v is not None}),
        )
