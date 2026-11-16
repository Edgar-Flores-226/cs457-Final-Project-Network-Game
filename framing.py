"""C4P v1 wire framing: 4-byte big-endian length prefix + UTF-8 JSON body.

Specification: docs/protocol_blueprint.md, Section 2.

TCP is a byte stream, so one recv() may return half a message or several
messages.  Every function here therefore reads *exactly* the number of bytes
the framing rule calls for, looping until they have all arrived.
"""
import json
import logging
import socket
import struct
from typing import Optional

HEADER_FORMAT = "!I"  # "!" = network byte order (big-endian), I = uint32
HEADER_BYTES = 4
MAX_FRAME_BYTES = 4096
BODY_TIMEOUT_S = 10  # body must arrive this soon after its header

log = logging.getLogger(__name__)


class FrameError(Exception):
    """A fatal framing error: the byte stream can no longer be trusted."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def encode_frame(message: dict) -> bytes:
    """Serialise a message dict into header + compact UTF-8 JSON body."""
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise ValueError(f"message is {len(body)} bytes (max 4096)")
    return struct.pack(HEADER_FORMAT, len(body)) + body


def recv_exact(sock: socket.socket, n_bytes: int) -> Optional[bytes]:
    """Return exactly n_bytes, or None if the peer closed first (EOF).

    recv() returning b"" is the TCP end-of-file signal (peer sent FIN).
    Checking for it prevents an infinite 100%-CPU loop.
    """
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = sock.recv(n_bytes - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock: socket.socket) -> Optional[bytes]:
    """Read one frame and return its body bytes.

    Returns None on EOF (at any point, including in the middle of a frame).
    Raises FrameError for a bad length (FRAME_EMPTY / FRAME_TOO_LARGE) or a
    body that stalls (FRAME_TIMEOUT).  The wait for the *header* uses
    whatever timeout the owning thread set on the socket; only the body is
    subject to BODY_TIMEOUT_S.
    """
    header = recv_exact(sock, HEADER_BYTES)
    if header is None:
        return None
    (length,) = struct.unpack(HEADER_FORMAT, header)
    if length == 0:
        raise FrameError("FRAME_EMPTY", "length header is 0")
    if length > MAX_FRAME_BYTES:
        raise FrameError("FRAME_TOO_LARGE", f"length header is {length}")

    previous_timeout = sock.gettimeout()
    sock.settimeout(BODY_TIMEOUT_S)
    try:
        body = recv_exact(sock, length)
    except socket.timeout:
        raise FrameError("FRAME_TIMEOUT", "body stalled") from None
    finally:
        sock.settimeout(previous_timeout)

    if body is not None and log.isEnabledFor(logging.DEBUG):
        log.debug("RX header=%s body=%s", header.hex(" "),
                  body.decode("utf-8", "replace"))
    return body


def send_bytes(sock: socket.socket, data: bytes) -> None:
    """Send an already-encoded frame (lets a broadcast encode only once)."""
    sock.sendall(data)
    if log.isEnabledFor(logging.DEBUG):
        log.debug("TX header=%s body=%s", data[:HEADER_BYTES].hex(" "),
                  data[HEADER_BYTES:].decode("utf-8", "replace"))


def send_frame(sock: socket.socket, message: dict) -> None:
    """Encode a message dict and send it."""
    send_bytes(sock, encode_frame(message))
