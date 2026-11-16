"""C4P v1 constants, error codes, and message validation.

Specification: docs/protocol_blueprint.md, Sections 3-6.  Only the message
types, field names and error codes defined there are used here.
"""
import json
import re
from typing import Any, Dict, List

PROTOCOL_VERSION = 1
ROWS = 6
COLS = 7
EMPTY = "."
PLAYER_IDS = ("Player_1", "Player_2")
SYMBOLS = {"Player_1": "X", "Player_2": "O"}
UNASSIGNED = "UNASSIGNED"  # player_id used in CONNECT
SERVER_ID = "SERVER"  # player_id on every server -> client message

# --- the 8 message types --------------------------------------------------
CONNECT = "CONNECT"
LOBBY_WAIT = "LOBBY_WAIT"
GAME_START = "GAME_START"
MOVE = "MOVE"
STATE_UPDATE = "STATE_UPDATE"
ERROR = "ERROR"
DISCONNECT = "DISCONNECT"
GAME_OVER = "GAME_OVER"

CLIENT_TYPES = frozenset({CONNECT, MOVE, DISCONNECT})
SERVER_TYPES = frozenset(
    {LOBBY_WAIT, GAME_START, STATE_UPDATE, ERROR, GAME_OVER}
)

# --- error codes: code -> is the error fatal (connection will be closed)? --
ERROR_CODES = {
    "MALFORMED_JSON": False,
    "SCHEMA_VIOLATION": False,
    "UNKNOWN_MSG_TYPE": False,
    "WRONG_STATE": False,
    "ID_MISMATCH": False,
    "OUT_OF_TURN": False,
    "INVALID_COLUMN": False,
    "COLUMN_FULL": False,
    "VERSION_UNSUPPORTED": True,
    "GAME_FULL": True,
    "FRAME_EMPTY": True,
    "FRAME_TOO_LARGE": True,
    "FRAME_TIMEOUT": True,
    "HANDSHAKE_TIMEOUT": True,
}

ALIAS_RE = re.compile(r"[A-Za-z0-9_]{1,16}")
RESULTS = ("WIN", "DRAW", "FORFEIT")


class ProtocolError(Exception):
    """A well-framed message that breaks the protocol rules."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def is_fatal(code: str) -> bool:
    """True if the server closes the connection after this error code."""
    return ERROR_CODES[code]


def is_int(value: Any) -> bool:
    """Strict int check.  bool is a subclass of int in Python, so
    isinstance() alone would wrongly accept JSON true/false."""
    return type(value) is int


def valid_board(board: Any) -> bool:
    """A board is 6 strings of 7 characters, each '.', 'X' or 'O'."""
    if not isinstance(board, list) or len(board) != ROWS:
        return False
    return all(
        isinstance(row, str)
        and len(row) == COLS
        and set(row) <= {EMPTY, "X", "O"}
        for row in board
    )


# --- parsing and validation -----------------------------------------------
def parse_message(body: bytes) -> Dict[str, Any]:
    """Frame body -> dict.  Raises MALFORMED_JSON on any decode problem."""
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("MALFORMED_JSON", "body is not valid JSON") \
            from None
    if not isinstance(msg, dict):
        raise ProtocolError("MALFORMED_JSON", "body is not a JSON object")
    return msg


def validate_envelope(msg: Dict[str, Any]) -> None:
    """Check the four required top-level keys and their types."""
    for key, kind in (("msg_type", str), ("player_id", str),
                      ("payload", dict)):
        if not isinstance(msg.get(key), kind):
            raise ProtocolError(
                "SCHEMA_VIOLATION", f"'{key}' must be a {kind.__name__}")
    if not is_int(msg.get("timestamp")):
        raise ProtocolError("SCHEMA_VIOLATION", "'timestamp' must be an int")


def _require(payload: Dict[str, Any], key: str, check, what: str) -> Any:
    """Return payload[key] if check(value) passes, else SCHEMA_VIOLATION."""
    if key not in payload or not check(payload[key]):
        raise ProtocolError("SCHEMA_VIOLATION", f"'{key}' must be {what}")
    return payload[key]


def validate_client_message(msg: Dict[str, Any]) -> None:
    """Validate a client -> server message (CONNECT, MOVE, DISCONNECT).

    Range checks that need game state (column 0-6, column full) are NOT done
    here; the FSM performs them inside the lock (EVALUATE_MOVE).
    """
    validate_envelope(msg)
    msg_type, payload = msg["msg_type"], msg["payload"]
    if msg_type not in CLIENT_TYPES:
        raise ProtocolError("UNKNOWN_MSG_TYPE", f"unexpected {msg_type!r}")

    if msg_type == CONNECT:
        _require(payload, "alias",
                 lambda v: isinstance(v, str) and ALIAS_RE.fullmatch(v),
                 "1-16 letters, digits or underscores")
        version = _require(payload, "protocol_version", is_int, "an int")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                "VERSION_UNSUPPORTED", f"server speaks version "
                f"{PROTOCOL_VERSION}, client sent {version}")
    elif msg_type == MOVE:
        _require(payload, "col", is_int, "an int")
    else:  # DISCONNECT
        _require(payload, "reason", lambda v: v == "QUIT", "'QUIT'")


def validate_server_message(msg: Dict[str, Any]) -> None:
    """Validate a server -> client message (used by client.py)."""
    validate_envelope(msg)
    msg_type, payload = msg["msg_type"], msg["payload"]
    if msg_type not in SERVER_TYPES:
        raise ProtocolError("UNKNOWN_MSG_TYPE", f"unexpected {msg_type!r}")

    def is_pid(value: Any) -> bool:
        return value in PLAYER_IDS

    if msg_type == LOBBY_WAIT:
        _require(payload, "players_connected", is_int, "an int")
        _require(payload, "players_needed", is_int, "an int")
    elif msg_type == GAME_START:
        _require(payload, "you", is_pid, "Player_1 or Player_2")
        _require(payload, "symbol", lambda v: v in ("X", "O"), "X or O")
        _require(payload, "opponent_alias", lambda v: isinstance(v, str),
                 "a string")
        _require(payload, "first_turn", is_pid, "Player_1 or Player_2")
        _require(payload, "board", valid_board, "a 6x7 board")
    elif msg_type == STATE_UPDATE:
        _require(payload, "board", valid_board, "a 6x7 board")
        last = _require(payload, "last_move", lambda v: isinstance(v, dict),
                        "an object")
        if not (is_pid(last.get("player_id")) and is_int(last.get("row"))
                and is_int(last.get("col"))):
            raise ProtocolError("SCHEMA_VIOLATION", "bad 'last_move'")
        _require(payload, "next_turn", is_pid, "Player_1 or Player_2")
        _require(payload, "move_number", is_int, "an int")
    elif msg_type == ERROR:
        _require(payload, "code", lambda v: isinstance(v, str), "a string")
        _require(payload, "message", lambda v: isinstance(v, str),
                 "a string")
        _require(payload, "fatal", lambda v: isinstance(v, bool), "a bool")
    else:  # GAME_OVER
        _require(payload, "result", lambda v: v in RESULTS,
                 "WIN, DRAW or FORFEIT")
        _require(payload, "winner", lambda v: v is None or is_pid(v),
                 "a player id or null")
        _require(payload, "reason", lambda v: isinstance(v, str),
                 "a string")
        _require(payload, "winning_cells", lambda v: isinstance(v, list),
                 "a list")
        _require(payload, "board", valid_board, "a 6x7 board")
        scores = _require(payload, "scores", lambda v: isinstance(v, dict),
                          "an object")
        if not all(is_int(scores.get(pid)) for pid in PLAYER_IDS):
            raise ProtocolError("SCHEMA_VIOLATION", "bad 'scores'")


def other(player_id: str) -> str:
    """The opposing player's id."""
    return "Player_2" if player_id == "Player_1" else "Player_1"


def empty_board() -> List[str]:
    """A fresh 6x7 board in wire format (list of 6 strings)."""
    return [EMPTY * COLS for _ in range(ROWS)]
