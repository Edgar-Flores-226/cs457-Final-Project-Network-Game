"""Unit tests for framing.py (docs/protocol_blueprint.md, Section 2)."""
import socket
import struct
import threading
import time
import unittest

import framing
from framing import FrameError, encode_frame, read_frame, recv_exact


class FramingTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.b.settimeout(3)

    def test_encode_uses_big_endian_length_prefix(self):
        frame = encode_frame({"k": "v"})
        body = b'{"k":"v"}'
        self.assertEqual(frame[:4], struct.pack("!I", len(body)))
        self.assertEqual(frame[:4], b"\x00\x00\x00\x09")
        self.assertEqual(frame[4:], body)

    def test_roundtrip(self):
        self.a.sendall(encode_frame({"msg": "hi"}))
        self.assertEqual(read_frame(self.b), b'{"msg":"hi"}')

    def test_fragmentation_one_byte_at_a_time(self):
        frame = encode_frame({"msg_type": "MOVE", "payload": {"col": 3}})

        def dribble():
            for i in range(len(frame)):
                self.a.sendall(frame[i:i + 1])
                time.sleep(0.001)

        threading.Thread(target=dribble, daemon=True).start()
        self.assertEqual(read_frame(self.b), frame[4:])

    def test_coalescing_three_frames_in_one_send(self):
        msgs = [{"n": i} for i in range(3)]
        self.a.sendall(b"".join(encode_frame(m) for m in msgs))
        bodies = [read_frame(self.b) for _ in range(3)]
        self.assertEqual(bodies, [encode_frame(m)[4:] for m in msgs])

    def test_zero_length_is_fatal(self):
        self.a.sendall(b"\x00\x00\x00\x00")
        with self.assertRaises(FrameError) as ctx:
            read_frame(self.b)
        self.assertEqual(ctx.exception.code, "FRAME_EMPTY")

    def test_oversized_length_is_fatal(self):
        self.a.sendall(b"\xff\xff\xff\xff")
        with self.assertRaises(FrameError) as ctx:
            read_frame(self.b)
        self.assertEqual(ctx.exception.code, "FRAME_TOO_LARGE")

    def test_max_length_boundary(self):
        self.a.sendall(struct.pack("!I", 4097))
        with self.assertRaises(FrameError):
            read_frame(self.b)

    def test_eof_before_header_returns_none(self):
        self.a.close()
        self.assertIsNone(read_frame(self.b))

    def test_eof_in_middle_of_header_returns_none(self):
        self.a.sendall(b"\x00\x00")
        self.a.close()
        self.assertIsNone(read_frame(self.b))

    def test_eof_in_middle_of_body_returns_none(self):
        self.a.sendall(struct.pack("!I", 50) + b"short")
        self.a.close()
        self.assertIsNone(read_frame(self.b))

    def test_stalled_body_times_out(self):
        old = framing.BODY_TIMEOUT_S
        framing.BODY_TIMEOUT_S = 0.3
        self.addCleanup(setattr, framing, "BODY_TIMEOUT_S", old)
        self.a.sendall(struct.pack("!I", 50) + b"partial")
        with self.assertRaises(FrameError) as ctx:
            read_frame(self.b)
        self.assertEqual(ctx.exception.code, "FRAME_TIMEOUT")
        self.assertEqual(self.b.gettimeout(), 3)  # timeout restored

    def test_recv_exact_reassembles(self):
        self.a.sendall(b"ab")
        self.a.sendall(b"cd")
        self.assertEqual(recv_exact(self.b, 4), b"abcd")

    def test_oversize_message_cannot_be_encoded(self):
        with self.assertRaises(ValueError):
            encode_frame({"x": "a" * 5000})


if __name__ == "__main__":
    unittest.main()
