"""Connect Four rules and the server-side finite state machine.

Specification: docs/fsm_specification.md.  This module never touches a
socket or a lock; it is pure game logic so it can be unit-tested directly.
The threaded server (server.py) calls it while holding STATE_LOCK.
"""
import logging
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from protocol import COLS, EMPTY, PLAYER_IDS, ROWS, SYMBOLS

log = logging.getLogger("fsm")


class State(Enum):
    """The eight FSM states from docs/fsm_specification.md."""

    INIT = auto()
    WAITING_FOR_PLAYERS = auto()
    GAME_START = auto()
    PLAYER_TURN = auto()
    EVALUATE_MOVE = auto()
    FORFEIT_HANDLING = auto()
    GAME_OVER = auto()
    CLEANUP = auto()


class IllegalTransition(Exception):
    """Raised if code tries a transition the FSM does not allow."""


# (from_state, to_state) -> the transition IDs (T1-T15) that may use it.
# T1-T4 and T15 involve the server-level lobby and are logged by server.py.
ALLOWED_TRANSITIONS: Dict[Tuple[State, State], FrozenSet[str]] = {
    (State.GAME_START, State.PLAYER_TURN): frozenset({"T5"}),
    (State.GAME_START, State.FORFEIT_HANDLING): frozenset({"T12"}),
    (State.PLAYER_TURN, State.EVALUATE_MOVE): frozenset({"T6"}),
    (State.PLAYER_TURN, State.PLAYER_TURN): frozenset({"T7"}),
    (State.PLAYER_TURN, State.FORFEIT_HANDLING): frozenset({"T11"}),
    (State.EVALUATE_MOVE, State.PLAYER_TURN): frozenset({"T8", "T9"}),
    (State.EVALUATE_MOVE, State.GAME_OVER): frozenset({"T10"}),
    (State.EVALUATE_MOVE, State.FORFEIT_HANDLING): frozenset({"T12"}),
    (State.FORFEIT_HANDLING, State.GAME_OVER): frozenset({"T13"}),
    (State.GAME_OVER, State.CLEANUP): frozenset({"T14"}),
}

# (row step, column step) for the four line directions through a disc.
DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


def log_transition(old: str, new: str, tid: str) -> None:
    """One log line per FSM transition, e.g. 'PLAYER_TURN -> ... (T9)'."""
    log.info("%s -> %s (%s)", old, new, tid)


def find_winning_cells(board: List[List[str]], row: int,
                       col: int) -> List[List[int]]:
    """Return 4 winning [row, col] cells through (row, col), or [].

    Only lines through the *newly placed* disc can be new wins, so we count
    contiguous same-symbol discs along each of the four axes.
    """
    symbol = board[row][col]
    if symbol == EMPTY:
        return []
    for d_row, d_col in DIRECTIONS:
        run = [(row, col)]
        for sign in (-1, 1):  # extend backwards, then forwards
            r, c = row + sign * d_row, col + sign * d_col
            while 0 <= r < ROWS and 0 <= c < COLS and board[r][c] == symbol:
                if sign == -1:
                    run.insert(0, (r, c))
                else:
                    run.append((r, c))
                r, c = r + sign * d_row, c + sign * d_col
        if len(run) >= 4:
            index = run.index((row, col))
            start = min(index, len(run) - 4)  # window of 4 containing disc
            return [[r, c] for r, c in run[start:start + 4]]
    return []


class GameSession:
    """One match between two players.

    A *new* object is created for every match, so a stale thread from a
    finished game can never affect the next one (race R7).
    """

    def __init__(self, player1: Any, player2: Any) -> None:
        self.players = {"Player_1": player1, "Player_2": player2}
        self.board = [[EMPTY] * COLS for _ in range(ROWS)]
        self.active = "Player_1"  # Player 1 always moves first
        self.move_number = 0
        self.state = State.GAME_START  # created by transition T4
        self.winning_cells: List[List[int]] = []

    # --- FSM engine -------------------------------------------------------
    def transition(self, new_state: State, tid: str) -> None:
        """The ONLY place self.state changes; illegal moves raise."""
        allowed = ALLOWED_TRANSITIONS.get((self.state, new_state))
        if allowed is None or tid not in allowed:
            raise IllegalTransition(
                f"{self.state.name} -> {new_state.name} ({tid})")
        log_transition(self.state.name, new_state.name, tid)
        self.state = new_state

    def is_over(self) -> bool:
        return self.state in (State.GAME_OVER, State.CLEANUP)

    # --- board rules ------------------------------------------------------
    def lowest_empty_row(self, col: int) -> Optional[int]:
        """Gravity: the lowest empty row in a column (row 5 = bottom)."""
        for row in range(ROWS - 1, -1, -1):
            if self.board[row][col] == EMPTY:
                return row
        return None  # column is full

    def drop_disc(self, row: int, col: int, symbol: str) -> None:
        self.board[row][col] = symbol

    def evaluate(self, row: int, col: int) -> Optional[str]:
        """After a move: 'WIN', 'DRAW', or None (game continues)."""
        cells = find_winning_cells(self.board, row, col)
        if cells:
            self.winning_cells = cells
            return "WIN"
        if self.move_number >= ROWS * COLS:
            return "DRAW"
        return None

    def board_rows(self) -> List[str]:
        """Board in wire format: 6 strings, row 0 (top) first."""
        return ["".join(row) for row in self.board]

    def symbol_of(self, player_id: str) -> str:
        return SYMBOLS[player_id]

    def player_ids(self) -> Tuple[str, ...]:
        return PLAYER_IDS
