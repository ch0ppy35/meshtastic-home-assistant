"""Tests for turning mesh text messages into message log events and logbook entries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID
from homeassistant.core import Context, Event
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.meshtastic.aiomeshtastic.interface import MeshNode
from custom_components.meshtastic.api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    ATTR_EVENT_MESHTASTIC_API_NODE,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
)
from custom_components.meshtastic.const import (
    DOMAIN,
    EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME,
    MeshtasticMessageDirection,
)
from custom_components.meshtastic.entity import GatewayChannelEntity, GatewayDirectMessageEntity
from custom_components.meshtastic.logbook import async_describe_events, async_setup_message_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from homeassistant.core import HomeAssistant

ENTRY_ID = "entry1"
GATEWAY_NODE_ID = 111
PEER_NODE_ID = 222
PRIMARY_CHANNEL_INDEX = 0
UNREGISTERED_CHANNEL_INDEX = 7

CHANNEL_ENTITY_ID = "meshtastic.home_gateway_channel_primary"
DM_ENTITY_ID = "meshtastic.home_gateway_dm"

GATEWAY_NAME = "Home Gateway (!0000006f)"
PEER_NAME = "Kitchen Node (!000000de)"

NODES = {
    GATEWAY_NODE_ID: MeshNode(id=GATEWAY_NODE_ID, user_id="!0000006f", short_name="HGW", long_name="Home Gateway"),
    PEER_NODE_ID: MeshNode(id=PEER_NODE_ID, user_id="!000000de", short_name="KIT", long_name="Kitchen Node"),
}


class LogbookRow:
    """
    Stand-in for the lazy row the logbook hands to describers.

    The logbook does not pass a `core.Event`; it passes a row wrapper over the recorder
    entry, of which describers only use `data` and `context_id`.
    """

    def __init__(self, event: Event) -> None:
        self.data = event.data
        self.context_id = event.context.id


def api_event_data(to: dict[str, int | None], message: str, *, from_node: int, entry_id: str = ENTRY_ID) -> dict:
    """Build the payload shape both the received and sent api events use."""
    return {
        ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: entry_id,
        ATTR_EVENT_MESHTASTIC_API_NODE: from_node,
        ATTR_EVENT_MESHTASTIC_API_DATA: {
            "from": from_node,
            "to": to,
            "gateway": GATEWAY_NODE_ID,
            "message": message,
        },
    }


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register a config entry with its gateway/peer devices and channel/DM entities."""
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id=ENTRY_ID, unique_id=str(GATEWAY_NODE_ID))
    config_entry.add_to_hass(hass)
    config_entry.runtime_data = Mock(client=Mock(get_node_info=NODES.get))

    device_registry = dr.async_get(hass)
    for node_id, name in ((GATEWAY_NODE_ID, "Home Gateway"), (PEER_NODE_ID, "Kitchen Node")):
        device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            identifiers={(DOMAIN, str(node_id))},
            name=name,
        )

    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        DOMAIN,
        DOMAIN,
        GatewayChannelEntity.build_unique_id(ENTRY_ID, GATEWAY_NODE_ID, PRIMARY_CHANNEL_INDEX),
        suggested_object_id="home_gateway_channel_primary",
        original_name="Channel Primary",
        config_entry=config_entry,
    )
    entity_registry.async_get_or_create(
        DOMAIN,
        DOMAIN,
        GatewayDirectMessageEntity.build_unique_id(ENTRY_ID, GATEWAY_NODE_ID),
        suggested_object_id="home_gateway_dm",
        original_name="Direct Messages",
        config_entry=config_entry,
    )
    return config_entry


@pytest.fixture
async def message_log(hass: HomeAssistant, entry: MockConfigEntry) -> AsyncGenerator[list[Event]]:
    """Capture the message log events produced while the logger is running."""
    cancel = await async_setup_message_logger(hass, entry)
    events = async_capture_events(hass, EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG)
    yield events
    cancel()


@pytest.fixture
def describe_message_event(hass: HomeAssistant) -> Callable[[Event], dict[str, str]]:
    """Collect the logbook describer the integration registers for message log events."""
    describers: dict[str, Callable[[Event], dict[str, str]]] = {}

    def capture(_domain: str, event_type: str, describe_callback: Callable[[Event], dict[str, str]]) -> None:
        describers[event_type] = describe_callback

    async_describe_events(hass, capture)
    return describers[EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG]


async def test_sent_broadcast_is_logged_against_the_channel(hass: HomeAssistant, message_log: list[Event]) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data({"node": None, "channel": PRIMARY_CHANNEL_INDEX}, "on my way", from_node=GATEWAY_NODE_ID),
    )
    await hass.async_block_till_done()

    assert len(message_log) == 1
    assert message_log[0].data == {
        CONF_ENTITY_ID: CHANNEL_ENTITY_ID,
        CONF_DEVICE_ID: dr.async_get(hass).async_get_device(identifiers={(DOMAIN, str(GATEWAY_NODE_ID))}).id,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: GATEWAY_NAME,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: False,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: "on my way",
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION: MeshtasticMessageDirection.OUT,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME: "Channel Primary",
    }


async def test_sent_direct_message_is_logged_against_the_dm_entity(
    hass: HomeAssistant, message_log: list[Event]
) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data({"node": PEER_NODE_ID, "channel": None}, "psst", from_node=GATEWAY_NODE_ID),
    )
    await hass.async_block_till_done()

    assert len(message_log) == 1
    data = message_log[0].data
    assert data[CONF_ENTITY_ID] == DM_ENTITY_ID
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI] is True
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME] == PEER_NAME
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION] == MeshtasticMessageDirection.OUT


async def test_sent_message_to_unknown_node_falls_back_to_the_node_id(
    hass: HomeAssistant, message_log: list[Event]
) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data({"node": 999, "channel": None}, "anyone there?", from_node=GATEWAY_NODE_ID),
    )
    await hass.async_block_till_done()

    assert message_log[0].data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME] == "!000003e7"


async def test_sent_message_carries_the_originating_context(hass: HomeAssistant, message_log: list[Event]) -> None:
    """Senders correlate their own message with the log entry via the service call context."""
    context = Context()

    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data({"node": None, "channel": PRIMARY_CHANNEL_INDEX}, "hello", from_node=GATEWAY_NODE_ID),
        context=context,
    )
    await hass.async_block_till_done()

    assert message_log[0].context.id == context.id


async def test_sent_message_from_another_config_entry_is_ignored(hass: HomeAssistant, message_log: list[Event]) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data(
            {"node": None, "channel": PRIMARY_CHANNEL_INDEX},
            "not mine",
            from_node=GATEWAY_NODE_ID,
            entry_id="some-other-entry",
        ),
    )
    await hass.async_block_till_done()

    assert message_log == []


async def test_sent_message_on_an_unregistered_channel_is_ignored(
    hass: HomeAssistant, message_log: list[Event]
) -> None:
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
        api_event_data({"node": None, "channel": UNREGISTERED_CHANNEL_INDEX}, "nowhere", from_node=GATEWAY_NODE_ID),
    )
    await hass.async_block_till_done()

    assert message_log == []


async def test_received_message_is_marked_inbound(hass: HomeAssistant, message_log: list[Event]) -> None:
    """Received messages keep their existing payload and gain an explicit direction."""
    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        api_event_data({"node": None, "channel": PRIMARY_CHANNEL_INDEX}, "dinner?", from_node=PEER_NODE_ID),
    )
    await hass.async_block_till_done()

    assert len(message_log) == 1
    data = message_log[0].data
    assert data[CONF_ENTITY_ID] == CHANNEL_ENTITY_ID
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME] == PEER_NAME
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION] == MeshtasticMessageDirection.IN
    assert EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME not in data


@pytest.mark.parametrize(
    ("event_type", "to", "expected_message", "expected_icon"),
    [
        (
            EVENT_MESHTASTIC_API_TEXT_MESSAGE,
            {"node": None, "channel": PRIMARY_CHANNEL_INDEX},
            f"«dinner?» by {PEER_NAME}",
            "mdi:message",
        ),
        (
            EVENT_MESHTASTIC_API_TEXT_MESSAGE,
            {"node": GATEWAY_NODE_ID, "channel": None},
            f"«dinner?» by {PEER_NAME}",
            "mdi:message-lock",
        ),
        (
            EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
            {"node": None, "channel": PRIMARY_CHANNEL_INDEX},
            "«dinner?» to Channel Primary",
            "mdi:message-arrow-right",
        ),
        (
            EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT,
            {"node": PEER_NODE_ID, "channel": None},
            f"«dinner?» to {PEER_NAME}",
            "mdi:message-lock",
        ),
    ],
)
async def test_logbook_entry_encodes_direction_in_the_verb(
    hass: HomeAssistant,
    message_log: list[Event],
    describe_message_event: Callable[[Event], dict[str, str]],
    event_type: str,
    to: dict[str, int | None],
    expected_message: str,
    expected_icon: str,
) -> None:
    """A logbook row carries no event type, so the verb has to reveal the direction."""
    from_node = GATEWAY_NODE_ID if event_type == EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT else PEER_NODE_ID
    hass.bus.async_fire(event_type, api_event_data(to, "dinner?", from_node=from_node))
    await hass.async_block_till_done()

    described = describe_message_event(LogbookRow(message_log[0]))

    assert described[LOGBOOK_ENTRY_MESSAGE] == expected_message
    assert described[LOGBOOK_ENTRY_ICON] == expected_icon
    assert described[LOGBOOK_ENTRY_ENTITY_ID] == message_log[0].data[CONF_ENTITY_ID]


async def test_message_logger_stops_listening_when_cancelled(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Both the received and the sent listener have to be torn down together."""
    cancel = await async_setup_message_logger(hass, entry)
    events = async_capture_events(hass, EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG)
    cancel()

    for event_type in (EVENT_MESHTASTIC_API_TEXT_MESSAGE, EVENT_MESHTASTIC_API_TEXT_MESSAGE_SENT):
        hass.bus.async_fire(
            event_type,
            api_event_data({"node": None, "channel": PRIMARY_CHANNEL_INDEX}, "gone", from_node=GATEWAY_NODE_ID),
        )
    await hass.async_block_till_done()

    assert events == []
