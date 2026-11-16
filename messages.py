"""Builders for every C4P v1 message (docs/protocol_blueprint.md, Sec. 5).

Each builder returns the complete 4-key envelope as a dict.  Field names and
nesting match the blueprint exactly.
"""
import time
from typing import Any, Dict, List, Optional

from protocol import (
    CONNECT, DISCONNECT, ERROR, ERROR_CODES, GAME_OVER, GAME_START,
    LOBBY_WAIT, MOVE, PROTOCOL_VERSION, SERVER_ID, STATE_UPDATE, UNASSIGNED,
)

Message = Dict[str, Any]


def _envelope(msg_type: str, player_id: str, payload: Message) -> Message:
    """Wrap a payload in the standard envelope (key order matches docs)."""
    return {
        "msg_type": msg_type,
        "player_id": player_id,
        "payload": payload,
        "timestamp": int(time.time()),
    }


# --- client -> server -----------------------------------------------------
def build_connect(alias: str) -> Message:
    return _envelope(CONNECT, UNASSIGNED, {
        "alias": alias, "protocol_version": PROTOCOL_VERSION})


def build_move(player_id: str, col: int) -> Message:
    return _envelope(MOVE, player_id, {"col": col})


def build_disconnect(player_id: str) -> Message:
    return _envelope(DISCONNECT, player_id, {"reason": "QUIT"})


# --- server -> client -----------------------------------------------------
def build_lobby_wait(players_connected: int = 1) -> Message:
    return _envelope(LOBBY_WAIT, SERVER_ID, {
        "players_connected": players_connected, "players_needed": 2})


def build_game_start(you: str, symbol: str, opponent_alias: str,
                     board: List[str]) -> Message:
    return _envelope(GAME_START, SERVER_ID, {
        "you": you,
        "symbol": symbol,
        "opponent_alias": opponent_alias,
        "first_turn": "Player_1",
        "board": board,
    })


def build_state_update(board: List[str], last_player: str, row: int,
                       col: int, next_turn: str, move_number: int) -> Message:
    return _envelope(STATE_UPDATE, SERVER_ID, {
        "board": board,
        "last_move": {"player_id": last_player, "row": row, "col": col},
        "next_turn": next_turn,
        "move_number": move_number,
    })


def build_error(code: str, message: str) -> Message:
    """'fatal' is derived from the code so callers cannot get it wrong."""
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code {code!r}")
    return _envelope(ERROR, SERVER_ID, {
        "code": code, "message": message, "fatal": ERROR_CODES[code]})


def build_game_over(result: str, winner: Optional[str], reason: str,
                    winning_cells: List[List[int]], board: List[str],
                    scores: Dict[str, int]) -> Message:
    return _envelope(GAME_OVER, SERVER_ID, {
        "result": result,
        "winner": winner,
        "reason": reason,
        "winning_cells": winning_cells,
        "board": board,
        "scores": scores,
    })
