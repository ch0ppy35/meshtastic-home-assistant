"""Tests for reporting messages sent by this Home Assistant instance."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.meshtastic.api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    ATTR_EVENT_MESHTASTIC_API_NODE,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
    MeshtasticApiClient,
    MeshtasticApiClientError,
    _resolve_text_destination,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

GATEWAY_NODE_ID = 111
PEER_NODE_ID = 42
ENTRY_ID = "entry1"


class StubApiClient(MeshtasticApiClient):
    """A client wired to a stub interface, bypassing connection setup."""

    def __init__(self, hass: HomeAssistant, interface: AsyncMock, own_node: dict[str, Any]) -> None:
        self._hass = hass
        self._config_entry_id = ENTRY_ID
        self._interface = interface
        self._own_node = own_node

    def get_own_node(self) -> dict[str, Any]:
        return self._own_node


@pytest.fixture
def interface() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def client(hass: HomeAssistant, interface: AsyncMock) -> StubApiClient:
    return StubApiClient(hass, interface, {"num": GATEWAY_NODE_ID})


@pytest.mark.parametrize(
    ("destination_id", "channel_index", "expected"),
    [
        # A broadcast without an explicit channel lands on the primary channel, because an
        # unset channel index leaves the protobuf default of 0.
        ("^all", None, {"node": None, "channel": 0}),
        ("^all", 0, {"node": None, "channel": 0}),
        ("^all", 2, {"node": None, "channel": 2}),
        (None, None, {"node": None, "channel": 0}),
        (0xFFFFFFFF, None, {"node": None, "channel": 0}),
        # Anything addressed at a node is a direct message, mirroring how received
        # messages are classified.
        ("!0000002a", None, {"node": PEER_NODE_ID, "channel": None}),
        (PEER_NODE_ID, None, {"node": PEER_NODE_ID, "channel": None}),
        (PEER_NODE_ID, 3, {"node": PEER_NODE_ID, "channel": None}),
    ],
)
def test_resolve_text_destination(
    destination_id: int | str | None, channel_index: int | None, expected: dict[str, int | None]
) -> None:
    assert _resolve_text_destination(destination_id, channel_index) == expected


async def test_send_text_reports_broadcast(hass: HomeAssistant, client: StubApiClient) -> None:
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)

    assert await client.send_text("on my way", channel_index=2) is True
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: ENTRY_ID,
        ATTR_EVENT_MESHTASTIC_API_NODE: GATEWAY_NODE_ID,
        ATTR_EVENT_MESHTASTIC_API_DATA: {
            "from": GATEWAY_NODE_ID,
            "to": {"node": None, "channel": 2},
            "gateway": GATEWAY_NODE_ID,
            "message": "on my way",
        },
    }


async def test_send_text_reports_direct_message(hass: HomeAssistant, client: StubApiClient) -> None:
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)

    assert await client.send_text("psst", destination_id=PEER_NODE_ID) is True
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data[ATTR_EVENT_MESHTASTIC_API_DATA]["to"] == {"node": PEER_NODE_ID, "channel": None}


async def test_send_text_propagates_context(hass: HomeAssistant, client: StubApiClient) -> None:
    """The caller's context must survive so a sender can correlate its own log entry."""
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)
    context = Context()

    assert await client.send_text("hello", context=context) is True
    await hass.async_block_till_done()

    assert events[0].context.id == context.id


async def test_send_text_reports_on_acknowledgement_timeout(
    hass: HomeAssistant, client: StubApiClient, interface: AsyncMock
) -> None:
    """The 30s timeout is on the ack, not the transmission, so the message was still sent."""
    interface.send_text_message.side_effect = TimeoutError
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)

    assert await client.send_text("did it land?") is False
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_send_text_does_not_report_failed_transmission(
    hass: HomeAssistant, client: StubApiClient, interface: AsyncMock
) -> None:
    interface.send_text_message.side_effect = OSError("radio is gone")
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)

    with pytest.raises(MeshtasticApiClientError):
        await client.send_text("into the void")
    await hass.async_block_till_done()

    assert events == []


async def test_send_text_does_not_report_when_own_node_is_unknown(hass: HomeAssistant, interface: AsyncMock) -> None:
    client = StubApiClient(hass, interface, {})
    events = async_capture_events(hass, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT)

    assert await client.send_text("too early") is True
    await hass.async_block_till_done()

    assert events == []
