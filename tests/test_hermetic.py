"""
Guards the property the rest of the suite depends on: no test can reach a real thing.

Every test here runs against an in-process Home Assistant and a stand-in for the radio. That
is a deliberate choice, not a coincidence of how the tests happen to be written — a suite that
needs a node on the desk to go green stops being run.
"""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError

MESHTASTIC_TCP_PORT = 4403


def test_the_suite_cannot_open_a_network_connection() -> None:
    """
    Reaching a real radio or a real Home Assistant is impossible here, not merely avoided.

    Sockets are blocked for the whole suite, so a test that tried to talk to a device would
    fail loudly rather than pass on one machine and hang on another. Anything that genuinely
    needs a socket has to opt in through the `socket_enabled` fixture, which is a visible
    signal in review that a test is reaching outside the process.
    """
    with pytest.raises(SocketBlockedError), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("192.0.2.1", MESHTASTIC_TCP_PORT))
