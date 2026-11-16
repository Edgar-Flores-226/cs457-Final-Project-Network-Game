#!/usr/bin/env python3
"""Connect Four game server (C4P v1) - Sprint 3 implementation.

Design documents:
  docs/protocol_blueprint.md   wire format, message types, error codes
  docs/fsm_specification.md    state machine (T1-T15) and validation order
  docs/concurrency_design.md   threading model and locking rules

Concurrency summary (Option A, multi-threading)
  * The main thread only calls accept() and starts a thread per connection.
  * ONE lock, STATE_LOCK, guards all shared state: the lobby, the current
    GameSession, the scoreboard, and every write to a registered client
    socket.  Functions ending in _locked assume the lock is held and never
    take it themselves (threading.Lock is not reentrant).
  * recv() is never called while holding the lock.

Usage:
  python3 server.py -h
  python3 server.py -p 8080
"""
import argparse
import logging
import socket
import sys
import threading
import time
from typing import Dict, Optional

from cli import port_number
from framing import (FrameError, encode_frame, read_frame, send_bytes,
                     send_frame)
from game_fsm import GameSession, State, log_transition
from messages import (build_error, build_game_over, build_game_start,
                      build_lobby_wait, build_state_update)
from protocol import (CONNECT, DISCONNECT, MOVE, SYMBOLS, COLS,
                      ProtocolError, is_fatal, other, parse_message,
                      validate_client_message)

BIND_HOST = "0.0.0.0"
LISTEN_BACKLOG = 5
MAX_CONNECTIONS = 8  # live handler threads; extra sockets are refused
HANDSHAKE_TIMEOUT_S = 10  # a valid CONNECT must arrive this soon

# Every way a socket call can fail when a peer vanishes (EOF is separate:
# recv() returns b"" instead of raising).
SOCKET_ERRORS = (ConnectionResetError, BrokenPipeError,
                 ConnectionAbortedError, TimeoutError, socket.timeout,
                 OSError)

log = logging.getLogger("server")

STATE_LOCK = threading.Lock()  # the ONLY lock in the server


class PlayerConn:
    """One connected client: its socket, alias and assigned seat."""

    def __init__(self, sock: socket.socket, addr, alias: str,
                 player_id: str) -> None:
        self.sock = sock
        self.addr = addr
        self.alias = alias
        self.player_id = player_id
        self.session: Optional[GameSession] = None  # set when matched

    @property
    def symbol(self) -> str:
        return SYMBOLS[self.player_id]

    def __str__(self) -> str:
        return f"{self.player_id} ({self.alias})"


class ServerState:
    """All mutable server data.  Access only while holding STATE_LOCK."""

    def __init__(self) -> None:
        self.lobby: Optional[PlayerConn] = None
        self.session: Optional[GameSession] = None
        # alias -> {"wins": int, "losses": int, "draws": int}
        self.scoreboard: Dict[str, Dict[str, int]] = {}


server = ServerState()


def reset_state() -> None:
    """Discard all server state (used by the test suite)."""
    global server
    server = ServerState()


# --------------------------------------------------------------------------
# Sending helpers
# --------------------------------------------------------------------------
def send_bytes_locked(player: PlayerConn, data: bytes) -> bool:
    """Write one encoded frame to a registered player.  True on success.

    Every write to a registered socket happens under STATE_LOCK so two
    threads can never interleave bytes and corrupt the frame boundaries.
    """
    assert STATE_LOCK.locked(), "socket writes require STATE_LOCK"
    try:
        send_bytes(player.sock, data)
    except SOCKET_ERRORS as exc:
        log.warning("send to %s failed: %s", player, exc)
        return False
    return True


def send_locked(player: PlayerConn, message: dict) -> bool:
    return send_bytes_locked(player, encode_frame(message))


def send_error_locked(player: PlayerConn, code: str, detail: str) -> None:
    """Send a (possibly fatal) ERROR; a failed send means the peer is gone."""
    log.info("ERROR %s -> %s: %s", code, player, detail)
    if not send_locked(player, build_error(code, detail)):
        _handle_departure_locked(player)


def _send_quiet(sock: socket.socket, message: dict) -> None:
    """Send to a not-yet-registered socket (owned by one thread only)."""
    try:
        send_frame(sock, message)
    except SOCKET_ERRORS as exc:
        log.debug("quiet send failed: %s", exc)


def _shutdown_quiet(sock: socket.socket) -> None:
    """shutdown() wakes a thread blocked in recv() on this socket."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed by the peer


# --------------------------------------------------------------------------
# Game completion and scoring (all *_locked: caller holds STATE_LOCK)
# --------------------------------------------------------------------------
def record_result_locked(session: GameSession, result: str,
                         winner: Optional[str]) -> None:
    """Update the in-memory scoreboard.  Runs exactly once per game."""

    def record(player_id: str) -> Dict[str, int]:
        alias = session.players[player_id].alias
        return server.scoreboard.setdefault(
            alias, {"wins": 0, "losses": 0, "draws": 0})

    if result == "DRAW":
        for player_id in session.players:
            record(player_id)["draws"] += 1
    else:  # WIN or FORFEIT
        record(winner)["wins"] += 1
        record(other(winner))["losses"] += 1


def _scores_locked(session: GameSession) -> Dict[str, int]:
    """Wire 'scores': each seat's player's cumulative wins."""
    return {
        player_id: server.scoreboard[player.alias]["wins"]
        for player_id, player in session.players.items()
    }


def finish_game_locked(session: GameSession, result: str,
                       winner: Optional[str]) -> None:
    """End a game: score it, broadcast GAME_OVER, tear down, reset.

    Idempotent: if two threads both notice the end (e.g. both players drop
    at once), only the first has any effect (race R6).
    """
    if session.is_over():
        return
    session.transition(State.GAME_OVER,
                       "T10" if session.state is State.EVALUATE_MOVE
                       else "T13")
    record_result_locked(session, result, winner)
    reasons = {"WIN": "FOUR_IN_A_ROW", "DRAW": "BOARD_FULL",
               "FORFEIT": "OPPONENT_DISCONNECTED"}
    message = build_game_over(
        result, winner, reasons[result],
        session.winning_cells if result == "WIN" else [],
        session.board_rows(), _scores_locked(session))
    data = encode_frame(message)  # encode ONCE; both clients get same bytes
    for player_id, player in session.players.items():
        if result == "FORFEIT" and player_id != winner:
            continue  # the player who left is not sent anything
        send_bytes_locked(player, data)  # failures are logged, not fatal
    log.info("GAME OVER: %s winner=%s scores=%s", result, winner,
             message["payload"]["scores"])

    session.transition(State.CLEANUP, "T14")
    for player in session.players.values():
        _shutdown_quiet(player.sock)  # wakes the other thread's recv()
    server.session = None
    log_transition("CLEANUP", "WAITING_FOR_PLAYERS", "T15")


def _handle_departure_locked(player: PlayerConn) -> None:
    """A player left (DISCONNECT, EOF, reset, failed send).  Idempotent."""
    if server.lobby is player:  # T3: left before a game started
        server.lobby = None
        log_transition("WAITING_FOR_PLAYERS", "WAITING_FOR_PLAYERS", "T3")
        log.info("%s left the lobby", player)
        return
    session = player.session
    if session is None or session.is_over():
        return  # not in a game, or the game already ended
    log.info("%s disconnected mid-game: forfeit", player)
    tid = "T11" if session.state is State.PLAYER_TURN else "T12"
    session.transition(State.FORFEIT_HANDLING, tid)
    finish_game_locked(session, "FORFEIT", winner=other(player.player_id))


def on_client_disconnected(player: Optional[PlayerConn]) -> None:
    """Entry point for every kind of departure (takes STATE_LOCK)."""
    if player is None:
        return
    with STATE_LOCK:
        _handle_departure_locked(player)


# --------------------------------------------------------------------------
# Matchmaking: T2 / T4 / T5
# --------------------------------------------------------------------------
def _send_game_start_locked(session: GameSession) -> bool:
    """Personalised GAME_START to both seats.  False if a send failed."""
    failed = []
    for player_id, player in session.players.items():
        opponent = session.players[other(player_id)]
        message = build_game_start(player_id, player.symbol, opponent.alias,
                                   session.board_rows())
        if not send_locked(player, message):
            failed.append(player)
    if failed:
        _handle_departure_locked(failed[0])  # T12: peer vanished in setup
        return False
    return True


def register_player(conn: socket.socket, addr,
                    alias: str) -> Optional[PlayerConn]:
    """Seat a client that sent a valid CONNECT (takes STATE_LOCK).

    Whichever thread gets the lock first with an empty lobby becomes
    Player_1; the next becomes Player_2 and starts the game.
    """
    with STATE_LOCK:
        if server.session is not None:  # a game is already running
            _send_quiet(conn, build_error(
                "GAME_FULL", "A game is already in progress."))
            log.info("refused %s: GAME_FULL", alias)
            return None

        if server.lobby is None:  # T2: first player waits in the lobby
            player = PlayerConn(conn, addr, alias, "Player_1")
            server.lobby = player
            log_transition("WAITING_FOR_PLAYERS", "WAITING_FOR_PLAYERS",
                           "T2")
            log.info("%s joined the lobby", player)
            if not send_locked(player, build_lobby_wait()):
                server.lobby = None
                return None
            return player

        # T4: second player arrives -> create a NEW session and start
        first = server.lobby
        server.lobby = None
        second = PlayerConn(conn, addr, alias, "Player_2")
        session = GameSession(first, second)
        first.session = second.session = session
        server.session = session
        log_transition("WAITING_FOR_PLAYERS", "GAME_START", "T4")
        log.info("match: %s vs %s", first, second)
        if _send_game_start_locked(session):
            session.transition(State.PLAYER_TURN, "T5")
        return second


# --------------------------------------------------------------------------
# Move handling: T6 - T10 (the check-then-act is one critical section)
# --------------------------------------------------------------------------
def broadcast_locked(session: GameSession, message: dict) -> None:
    """Send one snapshot, encoded once, to Player_1 then Player_2.

    Both sends finish before the lock is released, so no other event can
    reach either client in between.  A failed send forfeits that player.
    """
    data = encode_frame(message)
    failed = [player for player in session.players.values()
              if not send_bytes_locked(player, data)]
    if failed:
        _handle_departure_locked(failed[0])


def handle_move(player: PlayerConn, msg: dict) -> None:
    """Validate and apply a MOVE (state-dependent checks 5-9, then commit).

    Order of checks follows docs/fsm_specification.md Section 5.1.  Errors
    never change the board and never pass the turn.
    """
    with STATE_LOCK:
        session = player.session
        if session is None:  # still in the lobby
            return send_error_locked(player, "WRONG_STATE",
                                     "No game in progress yet.")
        if session.is_over():
            return  # game already ended; ignore late input
        if session.state is not State.PLAYER_TURN:
            return send_error_locked(player, "WRONG_STATE",
                                     "Moves are not accepted right now.")
        if msg["player_id"] != player.player_id:
            session.transition(State.PLAYER_TURN, "T7")
            return send_error_locked(player, "ID_MISMATCH",
                                     f"You are {player.player_id}.")
        if session.active != player.player_id:
            session.transition(State.PLAYER_TURN, "T7")
            return send_error_locked(player, "OUT_OF_TURN",
                                     "It is not your turn.")

        session.transition(State.EVALUATE_MOVE, "T6")
        col = msg["payload"]["col"]
        row = None
        if not 0 <= col < COLS:
            error = ("INVALID_COLUMN", "Column must be 1-7.")
        else:
            row = session.lowest_empty_row(col)
            error = (("COLUMN_FULL", "That column is full.")
                     if row is None else None)
        if error:  # T8: same player must choose again
            session.transition(State.PLAYER_TURN, "T8")
            return send_error_locked(player, *error)

        # ---- commit point: every check passed ----
        session.drop_disc(row, col, player.symbol)
        session.move_number += 1
        log.info("move %d: %s -> column %d (row %d)", session.move_number,
                 player, col + 1, row)
        outcome = session.evaluate(row, col)
        if outcome is not None:  # T10: win or draw
            finish_game_locked(session, outcome, winner=(
                player.player_id if outcome == "WIN" else None))
            return
        session.active = other(player.player_id)  # swap in same section
        broadcast_locked(session, build_state_update(
            session.board_rows(), player.player_id, row, col,
            session.active, session.move_number))
        if not session.is_over():  # broadcast may have forfeited the game
            session.transition(State.PLAYER_TURN, "T9")


def dispatch(player: PlayerConn, msg: dict) -> bool:
    """Route one validated message.  Returns False to end the thread."""
    msg_type = msg["msg_type"]
    if msg_type == MOVE:
        handle_move(player, msg)
        return True
    if msg_type == DISCONNECT:  # graceful quit counts as a forfeit
        log.info("%s sent DISCONNECT", player)
        on_client_disconnected(player)
        return False
    with STATE_LOCK:  # CONNECT after registration
        send_error_locked(player, "WRONG_STATE", "Already connected.")
    return True


# --------------------------------------------------------------------------
# Per-connection thread
# --------------------------------------------------------------------------
def read_message(conn: socket.socket) -> Optional[dict]:
    """Read, parse and validate one client message (NO lock held).

    Returns None on EOF.  Raises FrameError (fatal framing problem) or
    ProtocolError (bad content).
    """
    body = read_frame(conn)
    if body is None:
        return None
    msg = parse_message(body)
    validate_client_message(msg)
    return msg


def handshake(conn: socket.socket, addr) -> Optional[PlayerConn]:
    """Wait up to HANDSHAKE_TIMEOUT_S for a valid CONNECT, then seat it."""
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise socket.timeout()
            conn.settimeout(remaining)
            msg = read_message(conn)
        except socket.timeout:
            _send_quiet(conn, build_error(
                "HANDSHAKE_TIMEOUT", "No valid CONNECT within 10 s."))
            return None
        except FrameError as exc:
            _send_quiet(conn, build_error(exc.code, exc.detail))
            return None
        except ProtocolError as exc:
            _send_quiet(conn, build_error(exc.code, exc.detail))
            if is_fatal(exc.code):
                return None
            continue  # non-fatal: keep waiting for a valid CONNECT
        if msg is None or msg["msg_type"] == DISCONNECT:
            return None
        if msg["msg_type"] == CONNECT:
            return register_player(conn, addr, msg["payload"]["alias"])
        _send_quiet(conn, build_error(
            "WRONG_STATE", "Send CONNECT first."))


def client_thread(conn: socket.socket, addr) -> None:
    """Handler for one connection: handshake, then read -> validate -> act."""
    player: Optional[PlayerConn] = None
    try:
        player = handshake(conn, addr)
        if player is None:
            return
        conn.settimeout(None)  # wait indefinitely for the next move
        while True:
            try:
                msg = read_message(conn)  # blocking, NO lock held
            except ProtocolError as exc:  # bad content: reply, carry on
                with STATE_LOCK:
                    send_error_locked(player, exc.code, exc.detail)
                if is_fatal(exc.code):
                    break
                continue
            if msg is None:  # EOF: peer closed (TCP FIN)
                log.info("%s closed the connection (EOF)", player)
                break
            if not dispatch(player, msg):
                break
    except FrameError as exc:  # fatal: stream out of sync
        log.warning("fatal framing error from %s: %s", addr, exc)
        if player is not None:
            with STATE_LOCK:
                send_error_locked(player, exc.code, exc.detail)
    except SOCKET_ERRORS as exc:  # reset, broken pipe, timeout...
        log.warning("connection lost (%s): %s", player or addr, exc)
    except Exception:  # never let one client crash the server
        log.exception("unexpected error in handler for %s", addr)
    finally:
        on_client_disconnected(player)  # idempotent cleanup / forfeit
        try:
            conn.close()  # only the owning thread closes its socket
        except OSError:
            pass


# --------------------------------------------------------------------------
# Listener and accept loop (main thread)
# --------------------------------------------------------------------------
def live_handler_count() -> int:
    return sum(1 for t in threading.enumerate()
               if t.name.startswith("client-"))


def accept_loop(listener: socket.socket) -> None:
    """Accept connections forever.  Never reads or writes game data."""
    while True:
        try:
            conn, addr = listener.accept()
        except OSError:
            break  # listener was closed: shut down
        if live_handler_count() >= MAX_CONNECTIONS:
            log.warning("too many connections; refusing %s", addr)
            conn.close()
            continue
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        thread = threading.Thread(
            target=client_thread, args=(conn, addr),
            name=f"client-{addr[0]}:{addr[1]}", daemon=True)
        thread.start()


def create_listener(port: int, host: str = BIND_HOST) -> socket.socket:
    """TCP listening socket bound to host:port (default 0.0.0.0)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(LISTEN_BACKLOG)
    return listener


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="server.py",
        description="Connect Four game server (C4P v1). Listens on all "
                    "interfaces (0.0.0.0) and hosts one two-player game "
                    "at a time.",
        epilog="examples:\n  python3 server.py -h\n"
               "  python3 server.py -p 8080",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-p", "--port", required=True, type=port_number, metavar="PORT",
        help="TCP port to listen on, 1024-65535 (required)")
    parser.add_argument(
        "--debug", action="store_true",
        help="log every frame (header and body) sent and received")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout)

    try:
        listener = create_listener(args.port)
    except OSError as exc:
        print(f"[ERROR] Cannot listen on port {args.port}: {exc}",
              file=sys.stderr)
        return 1
    log_transition("INIT", "WAITING_FOR_PLAYERS", "T1")
    log.info("Listening on %s:%d (waiting for players)", BIND_HOST,
             args.port)
    try:
        accept_loop(listener)
    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)")
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
