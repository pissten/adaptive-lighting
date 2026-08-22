"""KNX bus hooks so physical off/on is handled on the telegram, not the HA state.

Incoming GroupValueWrite/Response OFF on a light's switch/state address marks the
light as physically off immediately (before HA entity state_changed). Outgoing
brightness/ON telegrams for a guarded light are dropped in the xknx outgoing
queue so an already-started Adaptive Lighting ``light.turn_on`` cannot blink
the fixture back on.

Incoming ON and brightness writes (PIR, wall button) start adaptation immediately
using Adaptive Lighting's currently calculated values, without waiting for the
interval.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

KNX_DOMAIN = "knx"

_ON_REMOTE_VALUE_ATTRS = (
    "switch",
    "brightness",
    "color",
    "rgbw_color",
    "rgbw",
    "hue",
    "saturation",
    "xyy_color",
    "tunable_white",
    "color_temperature",
)


def get_xknx(hass: Any) -> Any | None:
    """Return the running XKNX instance if the KNX integration is loaded."""
    for key in ("knx_module", "knx"):
        module = hass.data.get(key)
        if module is not None and hasattr(module, "xknx"):
            return module.xknx
    for module in hass.data.values():
        xknx = getattr(module, "xknx", None)
        if xknx is not None and hasattr(xknx, "telegram_queue"):
            return xknx
    return None


def telegram_destination(telegram: Any) -> str | None:
    """Return the group address of a telegram as a string."""
    dest = getattr(telegram, "destination_address", None)
    if dest is None:
        return None
    return str(dest)


def is_group_write_or_response(payload: Any) -> bool:
    """Return whether the APCI payload is a write or response."""
    return type(payload).__name__ in {"GroupValueWrite", "GroupValueResponse"}


def payload_is_binary_off(payload: Any) -> bool | None:
    """Return True for 1-bit OFF, False for 1-bit ON, None if not 1-bit."""
    value = getattr(payload, "value", None)
    if value is None:
        return None
    raw = getattr(value, "value", value)
    if isinstance(raw, bool):
        return raw is False
    if isinstance(raw, int) and raw in (0, 1):
        return raw == 0
    return None


def payload_is_brightness(payload: Any) -> bool:
    """Return whether the payload is a dimming/brightness write, not 1-bit on/off."""
    if not is_group_write_or_response(payload):
        return False
    return payload_is_binary_off(payload) is None


def payload_brightness_is_zero(payload: Any) -> bool:
    """Return whether a brightness payload is 0% (treated as off)."""
    value = getattr(payload, "value", None)
    if value is None:
        return False
    raw = getattr(value, "value", value)
    if isinstance(raw, (bytes, bytearray, tuple, list)):
        raw = raw[0] if raw else 0
    if isinstance(raw, int):
        return raw == 0
    return False


def _ga_strings(group_address: Any) -> list[str]:
    if group_address is None:
        return []
    if isinstance(group_address, (list, tuple)):
        return [str(item) for item in group_address if item is not None]
    return [str(group_address)]


def _add_ga(mapping: dict[str, list[str]], group_address: Any, entity_id: str) -> None:
    for ga in _ga_strings(group_address):
        lights = mapping.setdefault(ga, [])
        if entity_id not in lights:
            lights.append(entity_id)


def _iter_on_remote_values(device: Any) -> list[Any]:
    values: list[Any] = []
    for name in _ON_REMOTE_VALUE_ATTRS:
        remote_value = getattr(device, name, None)
        if remote_value is not None and hasattr(remote_value, "group_address"):
            values.append(remote_value)
    for color in ("red", "green", "blue", "white"):
        part = getattr(device, color, None)
        if part is None:
            continue
        for name in ("switch", "brightness"):
            remote_value = getattr(part, name, None)
            if remote_value is not None and hasattr(remote_value, "group_address"):
                values.append(remote_value)
    return values


def collect_light_addresses(
    hass: Any,
    lights: set[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]], set[str], dict[str, list[str]]]:
    """Map KNX group addresses for Adaptive Lighting lights.

    Returns
    -------
    off_listen
        Switch command/state addresses that mean the light was turned off/on.
    block
        Addresses whose outgoing writes would turn the light on (switch + brightness/color).
    switch_gas
        Switch *command* addresses, so outgoing OFF is still allowed (HA turn_off / undo).
    brightness_listen
        Brightness command/state addresses (PIR absolute dimming).
    """
    off_listen: dict[str, list[str]] = {}
    block: dict[str, list[str]] = {}
    switch_gas: set[str] = set()
    brightness_listen: dict[str, list[str]] = {}
    if not lights:
        return off_listen, block, switch_gas, brightness_listen

    try:
        from homeassistant.helpers.entity_platform import async_get_platforms
    except ImportError:
        return off_listen, block, switch_gas, brightness_listen

    for platform in async_get_platforms(hass, KNX_DOMAIN):
        if getattr(platform, "domain", None) != "light":
            continue
        for entity_id, entity in platform.entities.items():
            if entity_id not in lights:
                continue
            device = getattr(entity, "_device", None)
            if device is None:
                continue
            switch = getattr(device, "switch", None)
            if switch is not None:
                command = getattr(switch, "group_address", None)
                state = getattr(switch, "group_address_state", None)
                _add_ga(off_listen, command, entity_id)
                _add_ga(off_listen, state, entity_id)
                _add_ga(block, command, entity_id)
                switch_gas.update(_ga_strings(command))
            brightness = getattr(device, "brightness", None)
            if brightness is not None:
                _add_ga(
                    brightness_listen,
                    getattr(brightness, "group_address", None),
                    entity_id,
                )
                _add_ga(
                    brightness_listen,
                    getattr(brightness, "group_address_state", None),
                    entity_id,
                )
            for remote_value in _iter_on_remote_values(device):
                _add_ga(block, getattr(remote_value, "group_address", None), entity_id)

    return off_listen, block, switch_gas, brightness_listen


class KnxPhysicalOffHook:
    """Listen for KNX OFF and drop follow-up Adaptive Lighting writes."""

    def __init__(self, hass: Any, manager: Any) -> None:
        self.hass = hass
        self.manager = manager
        self._xknx: Any | None = None
        self._cb: Any | None = None
        self._original_outgoing: Any | None = None
        self._off_listen: dict[str, list[str]] = {}
        self._block: dict[str, list[str]] = {}
        self._switch_gas: set[str] = set()
        self._brightness_listen: dict[str, list[str]] = {}
        self._map_key: frozenset[str] | None = None

    @property
    def hooked(self) -> bool:
        """Return whether the KNX telegram queue is hooked."""
        return self._cb is not None and self._xknx is not None

    def setup(self) -> bool:
        """Attach incoming callback and outgoing filter. Safe to call repeatedly."""
        xknx = get_xknx(self.hass)
        if xknx is None:
            return False
        if self.hooked and self._xknx is xknx:
            return True
        self.unhook()
        queue = xknx.telegram_queue
        self._xknx = xknx
        self._cb = queue.register_telegram_received_cb(
            self._on_incoming,
            match_for_outgoing=False,
        )
        self._original_outgoing = queue.process_telegram_outgoing
        queue.process_telegram_outgoing = self._process_outgoing
        self._map_key = None
        self.refresh_map()
        _LOGGER.info(
            "physical_off_guard: listening for KNX off telegrams "
            "(%s switch/state addresses)",
            len(self._off_listen),
        )
        return True

    def unhook(self) -> None:
        """Remove callbacks and restore the original outgoing handler."""
        if self._xknx is not None and self._cb is not None:
            try:
                self._xknx.telegram_queue.unregister_telegram_received_cb(self._cb)
            except (ValueError, AttributeError):
                pass
        if (
            self._xknx is not None
            and self._original_outgoing is not None
            and getattr(self._xknx.telegram_queue, "process_telegram_outgoing", None)
            is self._process_outgoing
        ):
            self._xknx.telegram_queue.process_telegram_outgoing = self._original_outgoing
        self._cb = None
        self._original_outgoing = None
        self._xknx = None

    def refresh_map(self) -> None:
        """Rebuild group-address maps when the managed light set changes."""
        key = frozenset(self.manager.lights)
        if key == self._map_key:
            return
        (
            self._off_listen,
            self._block,
            self._switch_gas,
            self._brightness_listen,
        ) = collect_light_addresses(
            self.hass,
            self.manager.lights,
        )
        self._map_key = key
        if self._off_listen:
            _LOGGER.debug(
                "physical_off_guard: KNX off-listen addresses %s",
                self._off_listen,
            )

    def _on_incoming(self, telegram: Any) -> None:
        """Handle incoming OFF/ON/brightness before HA entity state_changed."""
        if not self.manager.lights:
            return
        dest = telegram_destination(telegram)
        if dest is None:
            return
        self.refresh_map()
        payload = getattr(telegram, "payload", None)
        if not is_group_write_or_response(payload):
            return

        switch_lights = self._off_listen.get(dest)
        if switch_lights:
            binary_off = payload_is_binary_off(payload)
            if binary_off is True:
                for light in switch_lights:
                    if light not in self.manager.lights:
                        continue
                    _LOGGER.debug(
                        "physical_off_guard: incoming KNX OFF on %s for '%s'",
                        dest,
                        light,
                    )
                    self.manager.mark_physical_off(light)
                return
            if binary_off is False:
                for light in switch_lights:
                    if light not in self.manager.lights:
                        continue
                    _LOGGER.debug(
                        "physical_off_guard: incoming KNX ON on %s for '%s'",
                        dest,
                        light,
                    )
                    self.manager.notify_knx_physical_on(light)
                return

        brightness_lights = self._brightness_listen.get(dest)
        if brightness_lights and payload_is_brightness(payload):
            if payload_brightness_is_zero(payload):
                for light in brightness_lights:
                    if light not in self.manager.lights:
                        continue
                    _LOGGER.debug(
                        "physical_off_guard: incoming KNX brightness 0 on %s "
                        "for '%s'",
                        dest,
                        light,
                    )
                    self.manager.mark_physical_off(light)
                return
            for light in brightness_lights:
                if light not in self.manager.lights:
                    continue
                _LOGGER.debug(
                    "physical_off_guard: incoming KNX brightness on %s for '%s'",
                    dest,
                    light,
                )
                self.manager.notify_knx_brightness(light)

    async def _process_outgoing(self, telegram: Any) -> None:
        if self._should_drop(telegram):
            _LOGGER.debug(
                "physical_off_guard: dropping outgoing KNX telegram to %s",
                telegram_destination(telegram),
            )
            return
        await self._original_outgoing(telegram)

    def _should_drop(self, telegram: Any) -> bool:
        dest = telegram_destination(telegram)
        if dest is None:
            return False
        self.refresh_map()
        lights = self._block.get(dest)
        if not lights:
            return False
        payload = getattr(telegram, "payload", None)
        if not is_group_write_or_response(payload):
            return False
        if not any(self.manager.is_physical_off(light) for light in lights):
            return False
        if dest in self._switch_gas:
            # Keep outgoing OFF (HA turn_off / undo). Block outgoing ON.
            return payload_is_binary_off(payload) is not True
        return True
