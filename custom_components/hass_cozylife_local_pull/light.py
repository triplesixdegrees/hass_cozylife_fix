"""
Platform for light integration for CozyLife.
Optimized version for high-frequency updates (e.g., HyperHDR).
Polling is disabled in favor of optimistic updates.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DOMAIN, LIGHT_TYPE_CODE
from .tcp_client import tcp_client

_LOGGER = logging.getLogger(__name__)

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the CozyLife light platform."""
    if discovery_info is None:
        return

    lights = []
    if DOMAIN in hass.data and 'tcp_client' in hass.data[DOMAIN]:
        for item in hass.data[DOMAIN]['tcp_client']:
            if LIGHT_TYPE_CODE == item.device_type_code:
                lights.append(CozyLifeLight(item))

    if lights:
        async_add_entities(lights)

class CozyLifeLight(LightEntity):
    """Representation of a CozyLife Light optimized for high-frequency control."""
    _attr_should_poll = False

    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, tcp_client_instance: tcp_client) -> None:
        """Initialize the light."""
        self._tcp_client = tcp_client_instance
        self._attr_unique_id = self._tcp_client.device_id
        self._attr_name = f"{self._tcp_client.device_model_name} {self._tcp_client.device_id[-4:]}"
        self._attr_supported_color_modes = set()
        dpid = self._tcp_client.dpid

        if 5 in dpid and 6 in dpid:
            self._attr_supported_color_modes.add(ColorMode.HS)
        if 3 in dpid:
            self._attr_supported_color_modes.add(ColorMode.COLOR_TEMP)
        if 4 in dpid and not self._attr_supported_color_modes:
            self._attr_supported_color_modes.add(ColorMode.BRIGHTNESS)
        if not self._attr_supported_color_modes:
            self._attr_supported_color_modes.add(ColorMode.ONOFF)

        # Derive initial color mode from supported modes rather than hard-coding
        if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.HS in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode.HS
        elif ColorMode.BRIGHTNESS in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_color_mode = ColorMode.ONOFF

        # Start as unknown until async_added_to_hass syncs real device state
        self._attr_is_on = False
        self._attr_brightness = None
        self._attr_hs_color = None
        self._attr_color_temp_kelvin = None
        self._attr_available = True

    async def async_added_to_hass(self) -> None:
        """Sync initial state from device once added to HA."""
        state = await self.hass.async_add_executor_job(self._tcp_client.query)
        if state:
            self._apply_state(state)
            self.async_write_ha_state()

    def _apply_state(self, state: dict) -> None:
        """Update attributes from a raw device query result."""
        self._attr_is_on = int(state.get('1', 0)) != 0

        if '4' in state:
            self._attr_brightness = round(int(state['4']) * 255 / 1000)

        # Work mode 0 = white/color-temp, 1 = color/HS
        work_mode = int(state.get('2', 0))
        if work_mode == 1 and ColorMode.HS in self._attr_supported_color_modes:
            if '5' in state and '6' in state:
                self._attr_hs_color = (float(state['5']), float(state['6']) / 10)
            self._attr_color_mode = ColorMode.HS
            self._attr_color_temp_kelvin = None
        elif ColorMode.COLOR_TEMP in self._attr_supported_color_modes and '3' in state:
            kelvin_range = self._attr_max_color_temp_kelvin - self._attr_min_color_temp_kelvin
            self._attr_color_temp_kelvin = round(
                int(state['3']) / 1000 * kelvin_range + self._attr_min_color_temp_kelvin
            )
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_hs_color = None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Instruct the light to turn on with optimistic updates."""
        payload = {'1': 255, '2': 0}

        target_brightness_ha = kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness)
        target_hs_color = kwargs.get(ATTR_HS_COLOR, self._attr_hs_color)
        target_color_temp_kelvin = kwargs.get(ATTR_COLOR_TEMP_KELVIN, self._attr_color_temp_kelvin)

        active_mode = self._attr_color_mode
        if ATTR_HS_COLOR in kwargs:
            active_mode = ColorMode.HS
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            active_mode = ColorMode.COLOR_TEMP

        if target_brightness_ha is not None:
            payload['4'] = round(target_brightness_ha * 1000 / 255)

        if active_mode == ColorMode.HS and target_hs_color:
            payload['5'] = int(target_hs_color[0])
            payload['6'] = int(target_hs_color[1] * 10)
        elif active_mode == ColorMode.COLOR_TEMP and target_color_temp_kelvin:
            kelvin_range = self._attr_max_color_temp_kelvin - self._attr_min_color_temp_kelvin
            normalized_val = (target_color_temp_kelvin - self._attr_min_color_temp_kelvin) / kelvin_range
            payload['3'] = round(max(0, min(1000, normalized_val * 1000)))

        await self.hass.async_add_executor_job(self._tcp_client.control, payload)

        self._attr_is_on = True
        if target_brightness_ha is not None:
            self._attr_brightness = target_brightness_ha

        if active_mode == ColorMode.HS and target_hs_color:
            self._attr_color_mode = ColorMode.HS
            self._attr_hs_color = target_hs_color
            self._attr_color_temp_kelvin = None
        elif active_mode == ColorMode.COLOR_TEMP and target_color_temp_kelvin:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = target_color_temp_kelvin
            self._attr_hs_color = None

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Instruct the light to turn off."""
        await self.hass.async_add_executor_job(self._tcp_client.control, {'1': 0})
        self._attr_is_on = False
        self.async_write_ha_state()
