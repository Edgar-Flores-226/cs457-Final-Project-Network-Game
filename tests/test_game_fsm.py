"""Unit tests for game_fsm.py (rules and FSM transition table)."""
import unittest

from game_fsm import (GameSession, IllegalTransition, State,
                      find_winning_cells)

# A valid 42-move drawn game (column per move, alternating X then O).
DRAW_MOVES = [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 4, 3,
              3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 6, 6, 6, 6,
              6, 6]


def play(session, columns):
    """Apply moves alternately; return the last evaluate() result."""
    outcome = None
    symbols = ("X", "O")
    for i, col in enumerate(columns):
        row = session.lowest_empty_row(col)
        session.drop_disc(row, col, symbols[i % 2])
        session.move_number += 1
        outcome = session.evaluate(row, col)
    return outcome


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.s = GameSession(object(), object())

    def test_gravity(self):
        self.assertEqual(self.s.lowest_empty_row(3), 5)
        self.s.drop_disc(5, 3, "X")
        self.assertEqual(self.s.lowest_empty_row(3), 4)

    def test_full_column(self):
        for _ in range(6):
            self.s.drop_disc(self.s.lowest_empty_row(0), 0, "X")
        self.assertIsNone(self.s.lowest_empty_row(0))

    def test_vertical_win(self):
        self.assertEqual(play(self.s, [0, 1, 0, 1, 0, 1, 0]), "WIN")
        self.assertEqual(self.s.winning_cells,
                         [[2, 0], [3, 0], [4, 0], [5, 0]])

    def test_horizontal_win(self):
        self.assertEqual(play(self.s, [0, 0, 1, 1, 2, 2, 3]), "WIN")
        self.assertEqual(self.s.winning_cells,
                         [[5, 0], [5, 1], [5, 2], [5, 3]])

    def test_diagonal_up_right_win(self):
        # X discs land on (5,0) (4,1) (3,2) (2,3)
        moves = [0, 1, 1, 2, 3, 2, 2, 3, 3, 6, 3]
        self.assertEqual(play(self.s, moves), "WIN")
        self.assertEqual(self.s.winning_cells,
                         [[2, 3], [3, 2], [4, 1], [5, 0]])

    def test_diagonal_up_left_win(self):
        moves = [6, 5, 5, 4, 3, 4, 4, 3, 3, 0, 3]
        self.assertEqual(play(self.s, moves), "WIN")
        self.assertEqual(self.s.winning_cells,
                         [[2, 3], [3, 4], [4, 5], [5, 6]])

    def test_three_in_a_row_is_not_a_win(self):
        self.assertIsNone(play(self.s, [0, 1, 0, 1, 0]))

    def test_win_in_the_middle_of_a_run(self):
        # X at columns 0,1,3,4 then fills the gap at column 2
        board = self.s.board
        for col in (0, 1, 3, 4):
            board[5][col] = "X"
        board[5][2] = "X"
        cells = find_winning_cells(board, 5, 2)
        self.assertEqual(len(cells), 4)
        self.assertIn([5, 2], cells)

    def test_no_result_before_the_board_is_full(self):
        self.assertIsNone(play(self.s, DRAW_MOVES[:-1]))  # 41 moves

    def test_draw_on_full_board_without_a_winner(self):
        self.assertEqual(play(self.s, DRAW_MOVES), "DRAW")
        self.assertEqual(self.s.move_number, 42)
        self.assertEqual(self.s.winning_cells, [])

    def test_board_rows_wire_format(self):
        self.s.drop_disc(5, 3, "X")
        rows = self.s.board_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[5], "...X...")


class FsmTests(unittest.TestCase):
    def setUp(self):
        self.s = GameSession(object(), object())

    def test_normal_lifecycle(self):
        for state, tid in ((State.PLAYER_TURN, "T5"),
                           (State.EVALUATE_MOVE, "T6"),
                           (State.PLAYER_TURN, "T9"),
                           (State.EVALUATE_MOVE, "T6"),
                           (State.GAME_OVER, "T10"),
                           (State.CLEANUP, "T14")):
            self.s.transition(state, tid)
        self.assertTrue(self.s.is_over())

    def test_forfeit_path(self):
        self.s.transition(State.PLAYER_TURN, "T5")
        self.s.transition(State.FORFEIT_HANDLING, "T11")
        self.s.transition(State.GAME_OVER, "T13")
        self.assertTrue(self.s.is_over())

    def test_illegal_transition_rejected(self):
        with self.assertRaises(IllegalTransition):
            self.s.transition(State.EVALUATE_MOVE, "T6")  # skips T5

    def test_wrong_transition_id_rejected(self):
        with self.assertRaises(IllegalTransition):
            self.s.transition(State.PLAYER_TURN, "T9")

    def test_no_transition_out_of_cleanup(self):
        self.s.transition(State.PLAYER_TURN, "T5")
        self.s.transition(State.FORFEIT_HANDLING, "T11")
        self.s.transition(State.GAME_OVER, "T13")
        self.s.transition(State.CLEANUP, "T14")
        with self.assertRaises(IllegalTransition):
            self.s.transition(State.PLAYER_TURN, "T5")

    def test_error_self_loop_keeps_player_turn(self):
        self.s.transition(State.PLAYER_TURN, "T5")
        self.s.transition(State.PLAYER_TURN, "T7")
        self.assertIs(self.s.state, State.PLAYER_TURN)


if __name__ == "__main__":
    unittest.main()
