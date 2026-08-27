from __future__ import annotations

import struct
import unittest
from unittest.mock import patch

from websocket import ABNF, WebSocketProtocolException

from polymarket import ws_transport


class WebSocketTransportHardeningTests(unittest.TestCase):
    def test_oversized_frame_is_rejected_before_payload_read(self) -> None:
        declared_size = ws_transport.WEBSOCKET_MAX_FRAME_BYTES + 1
        wire = bytearray(b"\x82\x7f" + struct.pack("!Q", declared_size))
        requested_sizes: list[int] = []

        def receive(size: int) -> bytes:
            requested_sizes.append(size)
            chunk = bytes(wire[:size])
            del wire[:size]
            return chunk

        connection = ws_transport.BoundedWebSocket()
        connection.frame_buffer.recv = receive

        with self.assertRaisesRegex(WebSocketProtocolException, "frame exceeds"):
            connection.recv_frame()

        self.assertEqual(requested_sizes, [2, 8])
        self.assertEqual(wire, b"")

    def test_fragmented_message_is_rejected_before_aggregate_allocation(self) -> None:
        first_payload = b"a" * (ws_transport.WEBSOCKET_MAX_MESSAGE_BYTES // 2 + 1)
        second_payload = b"b" * (ws_transport.WEBSOCKET_MAX_MESSAGE_BYTES // 2)
        connection = ws_transport.BoundedWebSocket()
        first = ABNF(0, 0, 0, 0, ABNF.OPCODE_BINARY, 0, first_payload)
        second = ABNF(1, 0, 0, 0, ABNF.OPCODE_CONT, 0, second_payload)

        connection.cont_frame.validate(first)
        connection.cont_frame.add(first)
        connection.cont_frame.validate(second)
        with self.assertRaisesRegex(WebSocketProtocolException, "message exceeds"):
            connection.cont_frame.add(second)

        self.assertEqual(len(connection.cont_frame.cont_data[1]), len(first_payload))

    def test_managed_factory_receives_bounded_connection_class(self) -> None:
        calls = []

        class FakeConnection:
            def __init__(self) -> None:
                self.read_timeout = None

            def getstatus(self) -> int:
                return ws_transport.WEBSOCKET_HANDSHAKE_STATUS

            def settimeout(self, value: float) -> None:
                self.read_timeout = value

        def factory(url: str, **kwargs):
            calls.append((url, kwargs))
            return FakeConnection()

        with patch.object(
            ws_transport,
            "_ORIGINAL_WEBSOCKET_CREATE_CONNECTION",
            factory,
        ):
            connection = ws_transport.open_websocket_connection(
                "wss://example.test/ws",
                connection_factory=factory,
            )

        self.assertIs(calls[0][1]["class_"], ws_transport.BoundedWebSocket)
        self.assertEqual(calls[0][1]["redirect_limit"], 0)
        self.assertEqual(
            connection.read_timeout,
            ws_transport.WEBSOCKET_IO_TIMEOUT_SECONDS,
        )

    def test_injected_factory_contract_remains_backward_compatible(self) -> None:
        calls = []

        class FakeConnection:
            def getstatus(self) -> int:
                return ws_transport.WEBSOCKET_HANDSHAKE_STATUS

            def settimeout(self, _value: float) -> None:
                return None

        def factory(url: str, *, timeout: float, redirect_limit: int):
            calls.append((url, timeout, redirect_limit))
            return FakeConnection()

        ws_transport.open_websocket_connection(
            "wss://example.test/ws",
            connection_factory=factory,
        )

        self.assertEqual(calls[0][0], "wss://example.test/ws")
        self.assertGreater(calls[0][1], 0)
        self.assertEqual(calls[0][2], 0)


if __name__ == "__main__":
    unittest.main()
