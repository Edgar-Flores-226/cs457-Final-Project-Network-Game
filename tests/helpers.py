"""Shared helpers for the integration tests: a raw-socket test client."""
import json
import socket
import struct
import threading
import time
import unittest

import server as srv
from framing import encode_frame, read_frame

TIMEOUT = 3.0


class RawClient:
    """A bare-bones C4P client that lets tests send ANY bytes."""

    def __init__(self, port, alias="Tester"):
        self.alias = alias
        self.sock = socket.create_connection(("127.0.0.1", port),
                                             timeout=TIMEOUT)
        self.player_id = "UNASSIGNED"

    # --- sending ---
    def send(self, msg_type, payload=None, player_id=None):
        message = {"msg_type": msg_type,
                   "player_id": player_id or self.player_id,
                   "payload": {} if payload is None else payload,
                   "timestamp": int(time.time())}
        self.sock.sendall(encode_frame(message))

    def send_raw(self, data):
        self.sock.sendall(data)

    def connect(self):
        self.send("CONNECT", {"alias": self.alias, "protocol_version": 1},
                  "UNASSIGNED")

    def move(self, col, player_id=None):
        self.send("MOVE", {"col": col}, player_id)

    # --- receiving ---
    def recv_body(self, timeout=TIMEOUT):
        self.sock.settimeout(timeout)
        return read_frame(self.sock)

    def recv(self, timeout=TIMEOUT):
        body = self.recv_body(timeout)
        return None if body is None else json.loads(body)

    def expect(self, msg_type, timeout=TIMEOUT):
        msg = self.recv(timeout)
        assert msg is not None, f"EOF while waiting for {msg_type}"
        assert msg["msg_type"] == msg_type, f"wanted {msg_type}, got {msg}"
        return msg

    def expect_error(self, code, fatal=False):
        msg = self.expect("ERROR")
        assert msg["payload"]["code"] == code, msg
        assert msg["payload"]["fatal"] is fatal, msg
        return msg

    def expect_eof(self):
        assert self.recv() is None, "expected the server to close"

    def drain(self, quiet=0.15):
        """Read everything that arrives until the socket goes quiet."""
        out = []
        while True:
            try:
                msg = self.recv(quiet)
            except (socket.timeout, OSError):
                return out
            if msg is None:
                return out
            out.append(msg)

    def abort(self):
        """Close with RST (as if the process were killed)."""
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
        self.sock.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class ServerTestCase(unittest.TestCase):
    """Starts a fresh server (ephemeral port) for every test."""

    def setUp(self):
        srv.reset_state()
        self.clients = []
        self.listener = srv.create_listener(0, "127.0.0.1")
        self.port = self.listener.getsockname()[1]
        self.accept_thread = threading.Thread(
            target=srv.accept_loop, args=(self.listener,), daemon=True)
        self.accept_thread.start()

    def tearDown(self):
        for client in self.clients:
            client.close()
        try:
            # close() alone does not wake a blocked accept() on Linux;
            # shutdown() does, so the accept thread exits immediately.
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.accept_thread.join(2)
        time.sleep(0.05)

    def client(self, alias="Tester"):
        c = RawClient(self.port, alias)
        self.clients.append(c)
        return c

    def start_game(self, alias1="Alice", alias2="Bob"):
        """Connect two players; consume the handshake messages."""
        p1, p2 = self.client(alias1), self.client(alias2)
        p1.connect()
        p1.expect("LOBBY_WAIT")
        p2.connect()
        for client in (p1, p2):
            start = client.expect("GAME_START")
            client.player_id = start["payload"]["you"]
        return p1, p2

    def wait_until(self, predicate, timeout=TIMEOUT):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False
