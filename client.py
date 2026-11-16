#!/usr/bin/env python3
"""Connect Four network client (C4P v1) - Sprint 3 implementation.

The client is deliberately simple and single-threaded: the game is strictly
turn-based, so it either prompts for a move (your turn) or blocks waiting
for the server's next message.  It never updates its own board; it only
redraws from messages sent by the server, which is the single authority.

Usage:
  python3 client.py -h
  python3 client.py -i server.flores.edu -p 8080
  python3 client.py -i localhost -p 8080 -a Alice
"""
import argparse
import logging
import socket
import sys
from typing import Dict, List, Optional

from cli import alias_arg, hostname_arg, port_number
from framing import FrameError, read_frame, send_frame
from messages import build_connect, build_disconnect, build_move
from protocol import (COLS, ERROR, GAME_OVER, GAME_START, LOBBY_WAIT,
                      STATE_UPDATE, ProtocolError, parse_message,
                      validate_server_message)

CONNECT_TIMEOUT_S = 10

# Everything that can go wrong on a socket when the server disappears.
SOCKET_ERRORS = (ConnectionResetError, BrokenPipeError,
                 ConnectionAbortedError, TimeoutError, socket.timeout,
                 OSError)


def say(text: str = "") -> None:
    """print() that flushes, so output appears immediately when piped."""
    print(text, flush=True)


# --------------------------------------------------------------------------
# Command-line handling and connection setup
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="Connect Four network client (C4P v1).",
        epilog="examples:\n  python3 client.py -h\n"
               "  python3 client.py -i server.flores.edu -p 8080\n"
               "  python3 client.py -i localhost -p 8080 -a Alice",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-i", "--host", dest="hostname", required=True, type=hostname_arg,
        metavar="HOSTNAME",
        help="server DNS hostname or IP address (required)")
    parser.add_argument(
        "-p", "--port", required=True, type=port_number, metavar="PORT",
        help="server TCP port, 1024-65535 (required)")
    parser.add_argument(
        "-a", "--alias", type=alias_arg, metavar="ALIAS", default=None,
        help="your player name, 1-16 letters/digits/underscores "
             "(asked interactively if omitted)")
    parser.add_argument(
        "--debug", action="store_true",
        help="log every frame (header and body) sent and received")
    return parser


def resolve_host(hostname: str) -> str:
    """Resolve a DNS name (or IP literal) to an IPv4 address.

    The client MUST fail gracefully if DNS resolution fails: a clear
    message and exit code 1, never a raw traceback.
    """
    try:
        return socket.gethostbyname(hostname)
    except (socket.gaierror, UnicodeError):
        # UnicodeError: names such as an over-long label cannot even be
        # encoded for DNS, which is also a resolution failure.
        print(f"[ERROR] Unable to resolve DNS hostname '{hostname}'.")
        print("Please check your Cisco router DNS configuration "
              "('ip dns server' / 'ip host') or verify domain spelling.")
        sys.exit(1)


def connect_to_server(ip: str, port: int) -> socket.socket:
    """Open the TCP connection, or exit(1) with a friendly message."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    try:
        sock.connect((ip, port))
    except ConnectionRefusedError:
        print(f"[ERROR] Connection refused by {ip}:{port}. "
              "Is the server running and the port correct?")
        sys.exit(1)
    except socket.timeout:
        print(f"[ERROR] Timed out connecting to {ip}:{port}. "
              "Check the address, port and any firewall.")
        sys.exit(1)
    except OSError as exc:
        print(f"[ERROR] Could not connect to {ip}:{port}: {exc}")
        sys.exit(1)
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


# --------------------------------------------------------------------------
# Terminal user interface
# --------------------------------------------------------------------------
def render_board(board: List[str]) -> str:
    """Draw the board in ASCII with column numbers 1-7 above it."""
    lines = ["", "    " + " ".join(str(c + 1) for c in range(COLS)),
             "  +---------------+"]
    lines += ["  | " + " ".join(row) + " |" for row in board]
    lines.append("  +---------------+")
    return "\n".join(lines)


def prompt_alias() -> str:
    """Ask for a valid alias (done BEFORE connecting, so the server's
    10-second handshake timer is not running while the user types)."""
    while True:
        try:
            value = input("Enter your player name (1-16 letters, digits "
                          "or _): ").strip()
        except EOFError:
            say("\nNo name entered. Exiting.")
            sys.exit(1)
        try:
            return alias_arg(value)
        except argparse.ArgumentTypeError as exc:
            say(f"  {exc}")


def prompt_move() -> Optional[int]:
    """Ask for a column 1-7.  Returns the 0-based wire column, or None
    if the player chose to quit (or stdin was closed)."""
    while True:
        try:
            raw = input("Enter column (1-7) or q to quit: ")
        except EOFError:
            return None
        if not sys.stdin.isatty():
            say(raw)  # echo scripted input so logs show what was typed
        raw = raw.strip().lower()
        if raw in ("q", "quit"):
            return None
        try:
            column = int(raw)
        except ValueError:
            column = 0
        if 1 <= column <= COLS:
            return column - 1  # the wire protocol uses columns 0-6
        say("  Invalid input. Please enter a number from 1 to 7.")


# --------------------------------------------------------------------------
# Game session
# --------------------------------------------------------------------------
class GameClient:
    """Drives one game: sends CONNECT, then reacts to server messages."""

    def __init__(self, sock: socket.socket, alias: str) -> None:
        self.sock = sock
        self.alias = alias
        self.my_id: Optional[str] = None
        self.symbol = "?"
        self.opponent_id: Optional[str] = None
        self.aliases: Dict[str, str] = {}  # player_id -> alias
        self.my_turn = False
        self.move_pending = False  # sent a MOVE, awaiting the server reply

    # --- network ----------------------------------------------------------
    def receive(self) -> Optional[dict]:
        """Next validated server message, or None if the server closed."""
        body = read_frame(self.sock)
        if body is None:
            return None
        msg = parse_message(body)
        validate_server_message(msg)
        return msg

    def quit_quietly(self) -> None:
        """Best-effort DISCONNECT so the server can award a forfeit."""
        if self.my_id is None:
            return
        try:
            send_frame(self.sock, build_disconnect(self.my_id))
            self.sock.shutdown(socket.SHUT_WR)
        except SOCKET_ERRORS:
            pass

    # --- main loop --------------------------------------------------------
    def run(self) -> int:
        """Play until GAME_OVER.  Returns the process exit code."""
        send_frame(self.sock, build_connect(self.alias))
        while True:
            if self.my_turn:
                say("\n>>> Your Turn <<<")
                column = prompt_move()
                if column is None:
                    say("Quitting - you forfeit the game.")
                    self.quit_quietly()
                    return 0
                send_frame(self.sock, build_move(self.my_id, column))
                self.my_turn = False
                self.move_pending = True

            msg = self.receive()
            if msg is None:
                say("\n[ERROR] The server closed the connection.")
                return 1
            exit_code = self.handle(msg)
            if exit_code is not None:
                return exit_code

    def handle(self, msg: dict) -> Optional[int]:
        """React to one server message.  Returns an exit code to stop."""
        payload = msg["payload"]
        msg_type = msg["msg_type"]
        if msg_type == LOBBY_WAIT:
            say(f"Connected as {self.alias}. Waiting for Player 2 to "
                "connect...")
        elif msg_type == GAME_START:
            self.on_game_start(payload)
        elif msg_type == STATE_UPDATE:
            self.on_state_update(payload)
        elif msg_type == ERROR:
            return self.on_error(payload)
        elif msg_type == GAME_OVER:
            self.on_game_over(payload)
            return 0
        return None

    def on_game_start(self, payload: dict) -> None:
        self.my_id = payload["you"]
        self.symbol = payload["symbol"]
        self.opponent_id = "Player_2" if self.my_id == "Player_1" \
            else "Player_1"
        self.aliases = {self.my_id: self.alias,
                        self.opponent_id: payload["opponent_alias"]}
        say(f"\n=== Game started! You are {self.my_id} ({self.symbol}) "
            f"vs {payload['opponent_alias']} ===")
        say(render_board(payload["board"]))
        self.update_turn(payload["first_turn"])

    def on_state_update(self, payload: dict) -> None:
        self.move_pending = False
        last = payload["last_move"]
        who = ("You" if last["player_id"] == self.my_id
               else self.aliases.get(last["player_id"], "Opponent"))
        say(f"\n{who} played column {last['col'] + 1} "
            f"(move {payload['move_number']}).")
        say(render_board(payload["board"]))
        self.update_turn(payload["next_turn"])

    def update_turn(self, next_turn: str) -> None:
        """Set whose turn it is and print the turn status line."""
        self.my_turn = next_turn == self.my_id
        if not self.my_turn:
            name = self.aliases.get(next_turn, "opponent")
            say(f"Waiting for {next_turn} ({name}) to move...")

    def on_error(self, payload: dict) -> Optional[int]:
        say(f"\n[SERVER ERROR] {payload['code']}: {payload['message']}")
        if payload["fatal"]:
            return 1
        if self.move_pending:  # rejected move: the turn was NOT passed
            self.move_pending = False
            self.my_turn = True
        return None

    def on_game_over(self, payload: dict) -> None:
        say(render_board(payload["board"]))
        result, winner = payload["result"], payload["winner"]
        if result == "DRAW":
            say("\n*** GAME OVER: It's a draw! ***")
        elif result == "FORFEIT":
            outcome = ("You win" if winner == self.my_id
                       else "You lose")
            say(f"\n*** GAME OVER: Opponent disconnected. "
                f"{outcome} by forfeit. ***")
        elif winner == self.my_id:
            say("\n*** GAME OVER: You WIN! ***")
        else:
            say("\n*** GAME OVER: You lose. Better luck next time! ***")
        self.print_scores(payload["scores"])

    def print_scores(self, scores: Dict[str, int]) -> None:
        """Score summary table from the server's cumulative win counts."""
        width = max(len(name) for name in self.aliases.values())
        say("\n================ SCORES ================")
        for player_id in ("Player_1", "Player_2"):
            wins = scores[player_id]
            symbol = "X" if player_id == "Player_1" else "O"
            plural = "win" if wins == 1 else "wins"
            say(f" {self.aliases[player_id]:<{width}} "
                f"({player_id}, {symbol}) : {wins} {plural}")
        say("========================================")


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", stream=sys.stderr)

    server_ip = resolve_host(args.hostname)  # exits 1 if DNS fails
    alias = args.alias or prompt_alias()
    sock = connect_to_server(server_ip, args.port)
    say(f"Connected to {args.hostname} ({server_ip}:{args.port}).")

    client = GameClient(sock, alias)
    try:
        return client.run()
    except KeyboardInterrupt:
        say("\nInterrupted - disconnecting.")
        client.quit_quietly()
        return 130
    except (FrameError, ProtocolError) as exc:
        say(f"\n[ERROR] Bad data from the server: {exc}")
        return 1
    except SOCKET_ERRORS as exc:
        say(f"\n[ERROR] Lost connection to the server: {exc}")
        return 1
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main())
