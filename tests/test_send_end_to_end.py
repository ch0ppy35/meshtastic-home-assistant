"""
End-to-end tests for the outbound message path. No radio, no live Home Assistant.

These drive the chain the way a user does — `hass.services.async_call` — and assert on what
a dashboard would read back. Everything in between is production code: the service schemas,
the gateway service handlers, `MeshtasticApiClient.send_text`, the message logger, and the
entity/device registry lookups that pick the target entity.

`FakeRadio` is the only seam. It sits exactly where the mesh would, which is what lets the
whole chain be proven without a node to talk to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.logbook.helpers import async_determine_event_types, async_filter_entities
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.components.recorder import get_instance
from homeassistant.const import CONF_DEVICE_ID, CONF_ENTITY_ID
from homeassistant.core import Context
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, mock_component
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.meshtastic import _add_entities_for_entry, services
from custom_components.meshtastic.aiomeshtastic.interface import MeshNode
from custom_components.meshtastic.api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    ATTR_EVENT_MESHTASTIC_API_NODE,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    MeshtasticApiClient,
    MeshtasticApiClientError,
)
from custom_components.meshtastic.const import (
    ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL,
    ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE,
    ATTR_SERVICE_DATA_CHANNEL,
    ATTR_SERVICE_DATA_TO,
    ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE,
    ATTR_SERVICE_SEND_TEXT_DATA_TEXT,
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    DOMAIN,
    EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME,
    SERVICE_BROADCAST_CHANNEL_MESSAGE,
    SERVICE_SEND_DIRECT_MESSAGE,
    SERVICE_SEND_TEXT,
    ConnectionType,
    MeshtasticMessageDirection,
)
from custom_components.meshtastic.data import MeshtasticData
from custom_components.meshtastic.entity import GatewayChannelEntity, GatewayDirectMessageEntity, GatewayEntity
from custom_components.meshtastic.logbook import async_setup_message_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from datetime import datetime

    from homeassistant.core import Event, HomeAssistant

ENTRY_ID = "entry1"
GATEWAY_NODE_ID = 111
PEER_NODE_ID = 222
PRIMARY_CHANNEL_INDEX = 0
SECONDARY_CHANNEL_INDEX = 1

GATEWAY_USER_ID = "!0000006f"
PEER_USER_ID = "!000000de"
GATEWAY_NAME = f"Home Gateway ({GATEWAY_USER_ID})"
PEER_NAME = f"Kitchen Node ({PEER_USER_ID})"

NODES = {
    GATEWAY_NODE_ID: MeshNode(id=GATEWAY_NODE_ID, user_id=GATEWAY_USER_ID, short_name="HGW", long_name="Home Gateway"),
    PEER_NODE_ID: MeshNode(id=PEER_NODE_ID, user_id=PEER_USER_ID, short_name="KIT", long_name="Kitchen Node"),
}

CHANNEL_SETTINGS = {"psk": "", "uplinkEnabled": False, "downlinkEnabled": False}


class FakeRadio:
    """
    The mesh, minus the mesh.

    Implements only the slice of `MeshInterface` the outbound path touches, so that every
    layer above it — `MeshtasticApiClient` included — is the real implementation.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_with: BaseException | None = None

    async def send_text_message(
        self,
        text: str,
        destination: int | str | None = None,
        *,
        want_ack: bool = False,
        channel_index: int | None = None,
    ) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(
            {"text": text, "destination": destination, "want_ack": want_ack, "channel_index": channel_index}
        )

    def connected_node(self) -> dict[str, Any]:
        return {"num": GATEWAY_NODE_ID, "user": {"id": GATEWAY_USER_ID, "longName": "Home Gateway"}}

    async def connected_node_ready(self) -> bool:
        return True

    def find_node(self, node_id: int) -> MeshNode | None:
        return NODES.get(node_id)


@dataclass
class Gateway:
    """The handles a test needs to address the gateway it was given."""

    entry: MockConfigEntry
    radio: FakeRadio
    gateway_device_id: str
    peer_device_id: str
    primary_channel_entity_id: str
    secondary_channel_entity_id: str
    dm_entity_id: str


@pytest.fixture
def radio() -> FakeRadio:
    return FakeRadio()


@pytest.fixture
async def gateway(hass: HomeAssistant, radio: FakeRadio) -> AsyncGenerator[Gateway]:
    """
    Bring up everything the outbound path needs, short of a radio and the sensor platforms.

    `async_setup` on its own registers the entity component and the services without opening
    a connection, so no `async_setup_entry` — and therefore no node — is involved. The config
    entry is added afterwards for the same reason: `async_setup_component` would otherwise try
    to set it up for real.
    """
    assert await async_setup_component(hass, DOMAIN, {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        unique_id=str(GATEWAY_NODE_ID),
        data={
            CONF_CONNECTION_TYPE: ConnectionType.TCP.value,
            CONF_CONNECTION_TCP_HOST: "192.0.2.1",
            CONF_CONNECTION_TCP_PORT: 4403,
        },
    )
    entry.add_to_hass(hass)

    # The real client, built from real config. Swapping the interface out is what keeps this
    # off the network: the TCP connection object it was constructed with is never started.
    client = MeshtasticApiClient(entry.data, hass=hass, config_entry_id=entry.entry_id)
    client._interface = radio  # noqa: SLF001
    entry.runtime_data = MeshtasticData(
        client=client, coordinator=None, integration=None, gateway_node=radio.connected_node()
    )

    device_registry = dr.async_get(hass)
    gateway_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(GATEWAY_NODE_ID))},
        name="Home Gateway",
    )
    peer_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(PEER_NODE_ID))},
        name="Kitchen Node",
        via_device=(DOMAIN, str(GATEWAY_NODE_ID)),
    )

    gateway_entity = GatewayEntity(ENTRY_ID, GATEWAY_NODE_ID, "Home Gateway", "HGW", {}, {})
    primary = GatewayChannelEntity(
        ENTRY_ID, GATEWAY_NODE_ID, gateway_entity, PRIMARY_CHANNEL_INDEX, "", CHANNEL_SETTINGS, primary=True
    )
    secondary = GatewayChannelEntity(
        ENTRY_ID, GATEWAY_NODE_ID, gateway_entity, SECONDARY_CHANNEL_INDEX, "Backup", CHANNEL_SETTINGS
    )
    direct_messages = GatewayDirectMessageEntity(ENTRY_ID, GATEWAY_NODE_ID, gateway_entity)
    # The same helper production uses, so the entity ids and registry links under test are
    # the ones a running instance would have.
    await _add_entities_for_entry(hass, [gateway_entity, primary, secondary, direct_messages], entry)

    await services.async_register_gateway(hass, entry)
    cancel = await async_setup_message_logger(hass, entry)

    yield Gateway(
        entry=entry,
        radio=radio,
        gateway_device_id=gateway_device.id,
        peer_device_id=peer_device.id,
        primary_channel_entity_id=primary.entity_id,
        secondary_channel_entity_id=secondary.entity_id,
        dm_entity_id=direct_messages.entity_id,
    )

    cancel()
    await services.async_unregister_gateway(hass, entry)


@pytest.fixture
def message_log(hass: HomeAssistant) -> list[Event]:
    return async_capture_events(hass, EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG)


async def test_broadcast_channel_message_reaches_the_radio_and_the_log(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """The whole point: a service call a dashboard makes ends up as a log entry it can read."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "on my way",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert gateway.radio.sent == [
        {"text": "on my way", "destination": "^all", "want_ack": True, "channel_index": PRIMARY_CHANNEL_INDEX}
    ]
    assert len(message_log) == 1
    assert message_log[0].data == {
        CONF_ENTITY_ID: gateway.primary_channel_entity_id,
        CONF_DEVICE_ID: gateway.gateway_device_id,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: GATEWAY_NAME,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: False,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: "on my way",
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION: MeshtasticMessageDirection.OUT,
        EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME: "Channel Primary",
    }


async def test_sent_message_carries_the_service_call_context(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """
    Correlating a sent message with its log entry rests on the context surviving the trip.

    A caller that knows the context id of its own service call can recognise the resulting
    log entry as its own instead of guessing by text and timestamp.
    """
    context = Context()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "hello",
        },
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()

    assert message_log[0].context.id == context.id


async def test_send_direct_message_logs_against_the_dm_entity(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    context = Context()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_DIRECT_MESSAGE,
        {
            ATTR_SERVICE_DATA_TO: gateway.peer_device_id,
            ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE: "psst",
        },
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()

    assert gateway.radio.sent == [
        {"text": "psst", "destination": PEER_NODE_ID, "want_ack": True, "channel_index": None}
    ]
    assert len(message_log) == 1
    data = message_log[0].data
    assert data[CONF_ENTITY_ID] == gateway.dm_entity_id
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI] is True
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME] == PEER_NAME
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_DIRECTION] == MeshtasticMessageDirection.OUT
    assert message_log[0].context.id == context.id


async def test_send_text_without_a_destination_logs_against_the_primary_channel(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """An unaddressed `send_text` broadcasts, and an unset channel index means the primary one."""
    context = Context()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEXT,
        {ATTR_SERVICE_SEND_TEXT_DATA_TEXT: "anyone about?"},
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()

    assert gateway.radio.sent == [
        {"text": "anyone about?", "destination": "^all", "want_ack": False, "channel_index": None}
    ]
    assert message_log[0].data[CONF_ENTITY_ID] == gateway.primary_channel_entity_id
    assert message_log[0].context.id == context.id


async def test_send_text_to_a_secondary_channel_logs_against_that_channel(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEXT,
        {
            ATTR_SERVICE_SEND_TEXT_DATA_TEXT: "over here",
            ATTR_SERVICE_DATA_CHANNEL: gateway.secondary_channel_entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert gateway.radio.sent[0]["channel_index"] == SECONDARY_CHANNEL_INDEX
    assert message_log[0].data[CONF_ENTITY_ID] == gateway.secondary_channel_entity_id
    assert message_log[0].data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME] == "Channel Backup"


async def test_send_text_addressed_to_a_node_logs_as_a_direct_message(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """`send_text` with a node address is a direct message, however it was addressed."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_TEXT,
        {ATTR_SERVICE_SEND_TEXT_DATA_TEXT: "just you", ATTR_SERVICE_DATA_TO: str(PEER_NODE_ID)},
        blocking=True,
    )
    await hass.async_block_till_done()

    data = message_log[0].data
    assert data[CONF_ENTITY_ID] == gateway.dm_entity_id
    assert data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_TO_NAME] == PEER_NAME


async def test_a_message_the_radio_rejects_is_not_logged(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """Nothing left the antenna, so nothing belongs in the log."""
    gateway.radio.fail_with = OSError("radio is gone")

    with pytest.raises(MeshtasticApiClientError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BROADCAST_CHANNEL_MESSAGE,
            {
                ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: gateway.primary_channel_entity_id,
                ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "into the void",
            },
            blocking=True,
        )
    await hass.async_block_till_done()

    assert message_log == []


async def test_a_message_whose_acknowledgement_times_out_is_still_logged(
    hass: HomeAssistant, gateway: Gateway, message_log: list[Event]
) -> None:
    """
    An ack timeout is not a send failure.

    The packet was handed to the radio and went out; only the confirmation is missing. Dropping
    it from the log would lose a message that other nodes did in fact receive.
    """
    gateway.radio.fail_with = TimeoutError

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "did it land?",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert len(message_log) == 1
    assert message_log[0].data[EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE] == "did it land?"


async def query_logbook(hass: HomeAssistant, entity_id: str, start: datetime) -> list[dict[str, Any]]:
    """
    Read back what a `logbook/get_events` subscription would deliver for one entity.

    Mirrors the websocket handler: filter the entities, work out which event types are
    loggable for them, then run the processor the handler delegates to. Going through
    `async_determine_event_types` matters — it is what proves the describer was discovered
    and that `meshtastic_message_log` is therefore queryable at all.
    """
    await async_wait_recording_done(hass)

    entity_ids = async_filter_entities(hass, [entity_id])
    assert entity_ids == [entity_id], "the logbook filtered the channel entity out"
    event_types = async_determine_event_types(hass, entity_ids, None)
    assert EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG in event_types, "the logbook describer was never registered"

    processor = EventProcessor(hass, event_types, entity_ids, None, None, timestamp=True, include_entity_name=False)
    return await get_instance(hass).async_add_executor_job(processor.get_events, start, dt_util.utcnow())


@pytest.fixture
async def recorded_gateway(
    recorder_mock: Any,
    hass: HomeAssistant,
    gateway: Gateway,
) -> Gateway:
    """
    Persist the gateway's message log and make it readable through the logbook.

    Tests must request this fixture *before* `hass`, because the recorder has to claim its
    database before Home Assistant is built. Listing `hass` first trips an assertion inside
    `recorder_db_url`.

    The logbook declares `frontend` as a dependency but only calls into it to register a
    sidebar panel, which is a plain callback. Standing it in avoids pulling the shipped
    frontend bundle into the test requirements for no gain.
    """
    mock_component(hass, "frontend")
    assert await async_setup_component(hass, "logbook", {})
    await hass.async_block_till_done()
    return gateway


async def test_a_sent_message_survives_in_the_logbook(recorded_gateway: Gateway, hass: HomeAssistant) -> None:
    """
    The reason any of this exists: the message is still there after the dashboard reloads.

    A card holding the message only in memory loses it on reload and never shows it in another
    tab. Reading it back out of the recorder is what makes it real, so that is what is asserted
    here rather than the event that produced it.
    """
    start = dt_util.utcnow()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: recorded_gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "on my way",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    events = await query_logbook(hass, recorded_gateway.primary_channel_entity_id, start)

    assert len(events) == 1
    assert events[0]["message"] == "«on my way» to Channel Primary"
    assert events[0]["icon"] == "mdi:message-arrow-right"
    assert events[0]["entity_id"] == recorded_gateway.primary_channel_entity_id


async def test_a_sent_message_can_be_matched_to_the_call_that_sent_it(
    recorded_gateway: Gateway, hass: HomeAssistant
) -> None:
    """
    The recorded row carries the context id, which is what makes de-duplication exact.

    A sender that rendered its own message optimistically can recognise the row as the same
    message and collapse the two, instead of matching on text and timestamp and guessing.
    """
    start = dt_util.utcnow()
    context = Context()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: recorded_gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "hello",
        },
        blocking=True,
        context=context,
    )
    await hass.async_block_till_done()

    events = await query_logbook(hass, recorded_gateway.primary_channel_entity_id, start)

    assert events[0]["context_id"] == context.id


async def test_sent_and_received_messages_interleave_in_one_conversation(
    recorded_gateway: Gateway, hass: HomeAssistant
) -> None:
    """
    Both directions land on the same entity, which is what makes the log a conversation.

    The received wording is upstream's and is asserted verbatim: the verb is the only thing
    telling the two directions apart once they are rows in a database.
    """
    start = dt_util.utcnow()

    hass.bus.async_fire(
        EVENT_MESHTASTIC_API_TEXT_MESSAGE,
        {
            ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: ENTRY_ID,
            ATTR_EVENT_MESHTASTIC_API_NODE: PEER_NODE_ID,
            ATTR_EVENT_MESHTASTIC_API_DATA: {
                "from": PEER_NODE_ID,
                "to": {"node": None, "channel": PRIMARY_CHANNEL_INDEX},
                "gateway": GATEWAY_NODE_ID,
                "message": "dinner?",
            },
        },
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_BROADCAST_CHANNEL_MESSAGE,
        {
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL: recorded_gateway.primary_channel_entity_id,
            ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE: "on my way",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    events = await query_logbook(hass, recorded_gateway.primary_channel_entity_id, start)

    assert [e["message"] for e in events] == [f"«dinner?» by {PEER_NAME}", "«on my way» to Channel Primary"]
    assert [e["icon"] for e in events] == ["mdi:message", "mdi:message-arrow-right"]
