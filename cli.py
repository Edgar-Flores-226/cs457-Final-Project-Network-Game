"""Shared command-line argument validators for client.py and server.py."""
import argparse

from protocol import ALIAS_RE

MIN_PORT = 1024
MAX_PORT = 65535


def port_number(value: str) -> int:
    """argparse type: an integer port in 1024-65535."""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid port {value!r}: must be a whole number between "
            f"{MIN_PORT} and {MAX_PORT}") from None
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(
            f"port {port} is out of range: use {MIN_PORT}-{MAX_PORT}")
    return port


def hostname_arg(value: str) -> str:
    """argparse type: a non-empty hostname or IP address string."""
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("hostname must not be empty")
    return value


def alias_arg(value: str) -> str:
    """argparse type: a player alias (1-16 letters, digits, underscores)."""
    if not ALIAS_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid alias {value!r}: use 1-16 letters, digits or "
            "underscores")
    return value
