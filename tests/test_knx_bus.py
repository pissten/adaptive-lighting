"""Tests for KNX physical-off telegram helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from homeassistant.components.adaptive_lighting.knx_bus import (
    KnxPhysicalOffHook,
    is_group_write_or_response,
    payload_brightness_is_zero,
    payload_is_binary_off,
    payload_is_brightness,
    telegram_destination,
)


class GroupValueWrite:
    """Stand-in for xknx.telegram.apci.GroupValueWrite."""

    def __init__(self, value):
        self.value = value


class GroupValueRead:
    """Stand-in for xknx.telegram.apci.GroupValueRead."""

    value = None


class DPTBinary:
    """Stand-in for xknx.dpt.DPTBinary."""

    def __init__(self, value):
        self.value = value


class DPTArray:
    """Stand-in for xknx.dpt.DPTArray."""

    def __init__(self, value):
        self.value = value


def test_payload_is_binary_off():
    """1-bit payloads decode as off/on; brightness payloads are ignored."""
    assert payload_is_binary_off(GroupValueWrite(DPTBinary(0))) is True
    assert payload_is_binary_off(GroupValueWrite(DPTBinary(1))) is False
    assert payload_is_binary_off(GroupValueWrite(0)) is True
    assert payload_is_binary_off(GroupValueWrite(False)) is True
    assert payload_is_binary_off(GroupValueWrite(DPTArray((17,)))) is None
    assert payload_is_binary_off(GroupValueRead()) is None


def test_is_group_write_or_response():
    """Only write/response APCI types are treated as off/on commands."""
    assert is_group_write_or_response(GroupValueWrite(0))
    assert not is_group_write_or_response(GroupValueRead())


def test_telegram_destination():
    """Group addresses stringify for map lookups."""
    telegram = SimpleNamespace(destination_address="1/1/0")
    assert telegram_destination(telegram) == "1/1/0"
    assert telegram_destination(SimpleNamespace(destination_address=None)) is None


def test_incoming_off_marks_physical_off():
    """An incoming OFF on a mapped switch address marks the light immediately."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._off_listen = {"1/1/0": ["light.downlights_stue"]}
    hook._map_key = frozenset(manager.lights)

    hook._on_incoming(
        SimpleNamespace(
            destination_address="1/1/0",
            payload=GroupValueWrite(DPTBinary(0)),
        ),
    )
    manager.mark_physical_off.assert_called_once_with("light.downlights_stue")


def test_incoming_on_does_not_mark_physical_off():
    """Incoming ON must not set the physical-off guard."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._off_listen = {"1/1/0": ["light.downlights_stue"]}
    hook._map_key = frozenset(manager.lights)

    hook._on_incoming(
        SimpleNamespace(
            destination_address="1/1/0",
            payload=GroupValueWrite(DPTBinary(1)),
        ),
    )
    manager.mark_physical_off.assert_not_called()
    manager.notify_knx_physical_on.assert_called_once_with("light.downlights_stue")


def test_payload_is_brightness():
    """Brightness payloads are 1-byte arrays, not 1-bit on/off."""
    assert payload_is_brightness(GroupValueWrite(DPTArray((64,))))
    assert not payload_is_brightness(GroupValueWrite(DPTBinary(1)))
    assert not payload_is_brightness(GroupValueRead())
    assert payload_brightness_is_zero(GroupValueWrite(DPTArray((0,))))
    assert not payload_brightness_is_zero(GroupValueWrite(DPTArray((64,))))


def test_incoming_brightness_notifies_physical_on():
    """Incoming brightness on the dimming address starts immediate adaptation."""
    manager = MagicMock()
    manager.lights = {"light.bad"}
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._off_listen = {}
    hook._brightness_listen = {"1/4/2": ["light.bad"]}
    hook._map_key = frozenset(manager.lights)

    hook._on_incoming(
        SimpleNamespace(
            destination_address="1/4/2",
            payload=GroupValueWrite(DPTArray((64,))),
        ),
    )
    manager.notify_knx_brightness.assert_called_once_with("light.bad")
    manager.mark_physical_off.assert_not_called()


def test_incoming_brightness_zero_marks_physical_off():
    """Brightness 0% is treated as off, not as a PIR turn-on."""
    manager = MagicMock()
    manager.lights = {"light.bad"}
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._off_listen = {}
    hook._brightness_listen = {"1/4/2": ["light.bad"]}
    hook._map_key = frozenset(manager.lights)

    hook._on_incoming(
        SimpleNamespace(
            destination_address="1/4/2",
            payload=GroupValueWrite(DPTArray((0,))),
        ),
    )
    manager.mark_physical_off.assert_called_once_with("light.bad")
    manager.notify_knx_brightness.assert_not_called()


def test_should_drop_brightness_when_guarded():
    """Brightness writes to a physically-off light are dropped."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    manager.is_physical_off.return_value = True
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._block = {"1/1/2": ["light.downlights_stue"]}
    hook._switch_gas = {"1/1/0"}
    hook._map_key = frozenset(manager.lights)

    telegram = SimpleNamespace(
        destination_address="1/1/2",
        payload=GroupValueWrite(DPTArray((17,))),
    )
    assert hook._should_drop(telegram) is True


def test_should_not_drop_outgoing_switch_off():
    """Outgoing OFF on the command address must still be sent (HA turn_off)."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    manager.is_physical_off.return_value = True
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._block = {"1/1/0": ["light.downlights_stue"]}
    hook._switch_gas = {"1/1/0"}
    hook._map_key = frozenset(manager.lights)

    telegram = SimpleNamespace(
        destination_address="1/1/0",
        payload=GroupValueWrite(DPTBinary(0)),
    )
    assert hook._should_drop(telegram) is False


def test_should_drop_outgoing_switch_on_when_guarded():
    """Outgoing ON is dropped while the physical-off guard is active."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    manager.is_physical_off.return_value = True
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._block = {"1/1/0": ["light.downlights_stue"]}
    hook._switch_gas = {"1/1/0"}
    hook._map_key = frozenset(manager.lights)

    telegram = SimpleNamespace(
        destination_address="1/1/0",
        payload=GroupValueWrite(DPTBinary(1)),
    )
    assert hook._should_drop(telegram) is True


def test_should_not_drop_when_not_guarded():
    """Writes pass through when the light is not physically off."""
    manager = MagicMock()
    manager.lights = {"light.downlights_stue"}
    manager.is_physical_off.return_value = False
    hook = KnxPhysicalOffHook(hass=MagicMock(), manager=manager)
    hook._block = {"1/1/2": ["light.downlights_stue"]}
    hook._switch_gas = {"1/1/0"}
    hook._map_key = frozenset(manager.lights)

    telegram = SimpleNamespace(
        destination_address="1/1/2",
        payload=GroupValueWrite(DPTArray((17,))),
    )
    assert hook._should_drop(telegram) is False
