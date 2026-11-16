"""Integration tests: the real threaded server over real TCP sockets.

These exercise the Sprint 1 protocol/FSM and the Sprint 2 concurrency
design: simultaneous clients, turn enforcement, validation order,
forfeits, scoring, and race conditions.
"""
import socket
import sys
import threading
import time
import unittest

import framing
import server as srv
from framing import encode_frame
from tests.helpers import ServerTestCase
from tests.test_game_fsm import DRAW_MOVES


def play_move(mover, other, col):
    """Send a move and read the resulting STATE_UPDATE on both sockets."""
    mover.move(col)
    a = mover.expect("STATE_UPDATE")
    b = other.expect("STATE_UPDATE")
    assert a == b, "clients received different updates"
    return a["payload"]


class HandshakeTests(ServerTestCase):
    def test_first_player_waits_in_lobby(self):
        c1 = self.client("Alice")
        c1.connect()
        msg = c1.expect("LOBBY_WAIT")
        self.assertEqual(msg["payload"], {"players_connected": 1,
                                          "players_needed": 2})
        self.assertEqual(msg["player_id"], "SERVER")

    def test_second_player_starts_game_with_personalised_messages(self):
        p1, p2 = self.start_game("Alice", "Bob")
        self.assertEqual(p1.player_id, "Player_1")
        self.assertEqual(p2.player_id, "Player_2")

    def test_game_start_payloads(self):
        p1, p2 = self.client("Alice"), self.client("Bob")
        p1.connect()
        p1.expect("LOBBY_WAIT")
        p2.connect()
        g1, g2 = p1.expect("GAME_START"), p2.expect("GAME_START")
        self.assertEqual(g1["payload"]["you"], "Player_1")
        self.assertEqual(g1["payload"]["symbol"], "X")
        self.assertEqual(g1["payload"]["opponent_alias"], "Bob")
        self.assertEqual(g2["payload"]["you"], "Player_2")
        self.assertEqual(g2["payload"]["symbol"], "O")
        self.assertEqual(g2["payload"]["opponent_alias"], "Alice")
        self.assertEqual(g1["payload"]["first_turn"], "Player_1")
        self.assertEqual(g1["payload"]["board"], ["......."] * 6)

    def test_simultaneous_connects_get_distinct_roles(self):
        # Race R3: two CONNECTs at the same instant.
        clients = [self.client("A"), self.client("B")]
        barrier = threading.Barrier(2)

        def go(c):
            barrier.wait()
            c.connect()

        threads = [threading.Thread(target=go, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        roles = set()
        for c in clients:
            msgs = c.drain(0.5)
            start = [m for m in msgs if m["msg_type"] == "GAME_START"]
            self.assertEqual(len(start), 1)
            roles.add(start[0]["payload"]["you"])
        self.assertEqual(roles, {"Player_1", "Player_2"})

    def test_third_client_is_refused_with_game_full(self):
        self.start_game()
        c3 = self.client("Carl")
        c3.connect()
        c3.expect_error("GAME_FULL", fatal=True)
        c3.expect_eof()

    def test_unsupported_protocol_version(self):
        c = self.client()
        c.send("CONNECT", {"alias": "A", "protocol_version": 9},
               "UNASSIGNED")
        c.expect_error("VERSION_UNSUPPORTED", fatal=True)
        c.expect_eof()

    def test_handshake_timeout(self):
        old = srv.HANDSHAKE_TIMEOUT_S
        srv.HANDSHAKE_TIMEOUT_S = 0.4
        self.addCleanup(setattr, srv, "HANDSHAKE_TIMEOUT_S", old)
        c = self.client()  # connects but never sends CONNECT
        c.expect_error("HANDSHAKE_TIMEOUT", fatal=True)
        c.expect_eof()

    def test_silent_client_does_not_block_other_players(self):
        # Non-blocking accept loop: a mute connection must not delay a game.
        self.client("Silent")  # connects, sends nothing
        p1, p2 = self.start_game("Alice", "Bob")
        self.assertEqual((p1.player_id, p2.player_id),
                         ("Player_1", "Player_2"))

    def test_move_before_connect_is_wrong_state(self):
        c = self.client()
        c.move(3, "Player_1")
        c.expect_error("WRONG_STATE")

    def test_duplicate_connect_is_wrong_state(self):
        c = self.client("Alice")
        c.connect()
        c.expect("LOBBY_WAIT")
        c.connect()
        c.expect_error("WRONG_STATE")

    def test_move_in_lobby_is_wrong_state(self):
        c = self.client("Alice")
        c.connect()
        c.expect("LOBBY_WAIT")
        c.move(0, "Player_1")
        c.expect_error("WRONG_STATE")

    def test_lobby_player_leaving_frees_the_slot(self):
        c1 = self.client("Alice")
        c1.connect()
        c1.expect("LOBBY_WAIT")
        c1.close()
        self.assertTrue(self.wait_until(lambda: srv.server.lobby is None))
        c2 = self.client("Bob")
        c2.connect()
        c2.expect("LOBBY_WAIT")  # Bob is the new first player, no game yet


class MoveTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.p1, self.p2 = self.start_game()

    def test_valid_move_broadcast_is_byte_identical(self):
        self.p1.move(3)
        body1, body2 = self.p1.recv_body(), self.p2.recv_body()
        self.assertEqual(body1, body2)  # same frame, encoded once
        import json
        payload = json.loads(body1)["payload"]
        self.assertEqual(payload["board"][5], "...X...")
        self.assertEqual(payload["last_move"],
                         {"player_id": "Player_1", "row": 5, "col": 3})
        self.assertEqual(payload["next_turn"], "Player_2")
        self.assertEqual(payload["move_number"], 1)

    def test_gravity_stacks_discs(self):
        play_move(self.p1, self.p2, 3)
        payload = play_move(self.p2, self.p1, 3)
        self.assertEqual(payload["last_move"]["row"], 4)
        self.assertEqual(payload["board"][4], "...O...")

    def test_out_of_turn_move_rejected_and_state_unchanged(self):
        self.p2.move(3)
        self.p2.expect_error("OUT_OF_TURN")
        payload = play_move(self.p1, self.p2, 0)  # Player 1 still to move
        self.assertEqual(payload["move_number"], 1)
        self.assertEqual(payload["board"][5], "X......")

    def test_invalid_column_does_not_pass_turn(self):
        for bad in (-1, 7, 99):
            self.p1.move(bad)
            self.p1.expect_error("INVALID_COLUMN")
        play_move(self.p1, self.p2, 2)  # still Player 1's turn

    def test_full_column_rejected(self):
        for i in range(6):  # alternate fill of column 0
            mover, other = ((self.p1, self.p2) if i % 2 == 0
                            else (self.p2, self.p1))
            play_move(mover, other, 0)
        self.p1.move(0)
        self.p1.expect_error("COLUMN_FULL")
        play_move(self.p1, self.p2, 1)  # turn was not passed

    def test_id_mismatch(self):
        self.p1.move(3, player_id="Player_2")
        self.p1.expect_error("ID_MISMATCH")
        play_move(self.p1, self.p2, 3)

    def test_malformed_json_keeps_stream_in_sync(self):
        junk = b"this is not json"
        self.p1.send_raw(len(junk).to_bytes(4, "big") + junk)
        self.p1.expect_error("MALFORMED_JSON")
        play_move(self.p1, self.p2, 3)  # framing still aligned

    def test_schema_violations(self):
        self.p1.send("MOVE", {"col": "3"})
        self.p1.expect_error("SCHEMA_VIOLATION")
        self.p1.send("MOVE", {"col": True})
        self.p1.expect_error("SCHEMA_VIOLATION")
        self.p1.send("MOVE", {})
        self.p1.expect_error("SCHEMA_VIOLATION")

    def test_unknown_and_server_only_types(self):
        self.p1.send("PING")
        self.p1.expect_error("UNKNOWN_MSG_TYPE")
        self.p1.send("STATE_UPDATE")
        self.p1.expect_error("UNKNOWN_MSG_TYPE")

    def test_fragmented_frame_is_reassembled(self):
        frame = encode_frame({"msg_type": "MOVE", "player_id": "Player_1",
                              "payload": {"col": 4}, "timestamp": 1})
        for i in range(len(frame)):  # one byte per send()
            self.p1.send_raw(frame[i:i + 1])
            time.sleep(0.001)
        self.assertEqual(self.p1.expect("STATE_UPDATE")["payload"]
                         ["last_move"]["col"], 4)
        self.p2.expect("STATE_UPDATE")

    def test_coalesced_frames_processed_in_order(self):
        def mv(col):
            return encode_frame({"msg_type": "MOVE",
                                 "player_id": "Player_1",
                                 "payload": {"col": col}, "timestamp": 1})
        self.p1.send_raw(mv(1) + mv(2))  # two moves in ONE send()
        self.assertEqual(self.p1.expect("STATE_UPDATE")["payload"]
                         ["move_number"], 1)
        self.p1.expect_error("OUT_OF_TURN")  # 2nd: turn already passed
        self.p2.expect("STATE_UPDATE")

    def test_frame_too_large_is_fatal_and_forfeits(self):
        self.p1.send_raw(b"\xff\xff\xff\xff")
        self.p1.expect_error("FRAME_TOO_LARGE", fatal=True)
        over = self.p2.expect("GAME_OVER")
        self.assertEqual(over["payload"]["result"], "FORFEIT")
        self.assertEqual(over["payload"]["winner"], "Player_2")

    def test_stalled_body_times_out(self):
        old = framing.BODY_TIMEOUT_S
        framing.BODY_TIMEOUT_S = 0.4
        self.addCleanup(setattr, framing, "BODY_TIMEOUT_S", old)
        self.p1.send_raw((50).to_bytes(4, "big") + b"partial")
        self.p1.expect_error("FRAME_TIMEOUT", fatal=True)
        self.assertEqual(self.p2.expect("GAME_OVER")["payload"]["result"],
                         "FORFEIT")


class GameEndTests(ServerTestCase):
    def play_vertical_win(self, p1, p2):
        """Player 1 stacks column 0; Player 2 plays column 1."""
        for col1, col2 in ((0, 1), (0, 1), (0, 1)):
            play_move(p1, p2, col1)
            play_move(p2, p1, col2)
        p1.move(0)  # 4th disc: four in a row

    def test_win_broadcasts_game_over_and_scores(self):
        p1, p2 = self.start_game("Alice", "Bob")
        self.play_vertical_win(p1, p2)
        o1, o2 = p1.expect("GAME_OVER"), p2.expect("GAME_OVER")
        self.assertEqual(o1["payload"], o2["payload"])  # same table
        payload = o1["payload"]
        self.assertEqual(payload["result"], "WIN")
        self.assertEqual(payload["winner"], "Player_1")
        self.assertEqual(payload["reason"], "FOUR_IN_A_ROW")
        self.assertEqual(payload["winning_cells"],
                         [[2, 0], [3, 0], [4, 0], [5, 0]])
        self.assertEqual(payload["scores"],
                         {"Player_1": 1, "Player_2": 0})
        p1.expect_eof()
        p2.expect_eof()
        self.assertEqual(srv.server.scoreboard["Alice"],
                         {"wins": 1, "losses": 0, "draws": 0})
        self.assertEqual(srv.server.scoreboard["Bob"],
                         {"wins": 0, "losses": 1, "draws": 0})
        self.assertTrue(self.wait_until(lambda: srv.server.session is None))

    def test_draw_game(self):
        p1, p2 = self.start_game("Alice", "Bob")
        for i, col in enumerate(DRAW_MOVES[:-1]):
            mover, other = (p1, p2) if i % 2 == 0 else (p2, p1)
            play_move(mover, other, col)
        p2.move(DRAW_MOVES[-1])  # 42nd move fills the board
        o1, o2 = p1.expect("GAME_OVER"), p2.expect("GAME_OVER")
        self.assertEqual(o1["payload"]["result"], "DRAW")
        self.assertIsNone(o1["payload"]["winner"])
        self.assertEqual(o1["payload"]["reason"], "BOARD_FULL")
        self.assertEqual(o1["payload"], o2["payload"])
        self.assertEqual(srv.server.scoreboard["Alice"]["draws"], 1)
        self.assertEqual(srv.server.scoreboard["Bob"]["draws"], 1)

    def test_scores_accumulate_across_games(self):
        for game in (1, 2):
            p1, p2 = self.start_game("Alice", "Bob")
            self.play_vertical_win(p1, p2)
            over = p1.expect("GAME_OVER")
            p2.expect("GAME_OVER")
            self.assertEqual(over["payload"]["scores"]["Player_1"], game)
            p1.expect_eof()
            p2.expect_eof()
            self.assertTrue(self.wait_until(
                lambda: srv.server.session is None))
            time.sleep(0.1)  # let handler threads finish

    def test_graceful_disconnect_is_a_forfeit(self):
        p1, p2 = self.start_game("Alice", "Bob")
        p2.send("DISCONNECT", {"reason": "QUIT"})
        over = p1.expect("GAME_OVER")
        self.assertEqual(over["payload"]["result"], "FORFEIT")
        self.assertEqual(over["payload"]["winner"], "Player_1")
        self.assertEqual(over["payload"]["reason"],
                         "OPPONENT_DISCONNECTED")
        self.assertEqual(over["payload"]["scores"],
                         {"Player_1": 1, "Player_2": 0})
        self.assertEqual(srv.server.scoreboard["Bob"]["losses"], 1)

    def test_abrupt_close_without_disconnect_message(self):
        p1, p2 = self.start_game("Alice", "Bob")
        play_move(p1, p2, 3)
        p1.close()  # FIN, no DISCONNECT frame (Player 1 is mid-game)
        over = p2.expect("GAME_OVER")
        self.assertEqual(over["payload"]["result"], "FORFEIT")
        self.assertEqual(over["payload"]["winner"], "Player_2")

    def test_connection_reset_is_handled(self):
        p1, p2 = self.start_game("Alice", "Bob")
        p2.abort()  # TCP RST, like kill -9 / pulled cable
        over = p1.expect("GAME_OVER")
        self.assertEqual(over["payload"]["result"], "FORFEIT")
        self.assertEqual(over["payload"]["winner"], "Player_1")

    def test_server_accepts_a_new_game_after_a_forfeit(self):
        p1, p2 = self.start_game("Alice", "Bob")
        p2.send("DISCONNECT", {"reason": "QUIT"})
        p1.expect("GAME_OVER")
        p1.expect_eof()
        self.assertTrue(self.wait_until(lambda: srv.server.session is None))
        time.sleep(0.1)
        a, b = self.start_game("Carol", "Dave")  # fresh match works
        self.assertEqual(a.player_id, "Player_1")

    def test_both_players_dropping_scores_only_once(self):
        # Race R6: two threads notice the end at about the same time.
        p1, p2 = self.start_game("Alice", "Bob")
        barrier = threading.Barrier(2)

        def drop(c):
            barrier.wait()
            c.abort()

        threads = [threading.Thread(target=drop, args=(c,))
                   for c in (p1, p2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(self.wait_until(lambda: srv.server.session is None))
        total = sum(r["wins"] for r in srv.server.scoreboard.values())
        self.assertEqual(total, 1)  # exactly one game was scored


class ConcurrencyTests(ServerTestCase):
    def test_simultaneous_moves_never_corrupt_state(self):
        """Both players fire a MOVE at the same instant, over and over.

        Whichever thread wins STATE_LOCK is processed first; the other is
        evaluated against the updated state.  Every outcome must be some
        valid one-at-a-time ordering, so the stream of STATE_UPDATEs must
        count 1, 2, 3, ... with no gaps or duplicates, each board must hold
        exactly move_number discs, and both clients must see the same.
        """
        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # switch threads very often
        self.addCleanup(sys.setswitchinterval, old)
        p1, p2 = self.start_game("Alice", "Bob")
        seen = {p1: [], p2: []}
        game_over = False
        for round_no in range(25):
            barrier = threading.Barrier(2)
            col = round_no % 7

            def fire(client):
                barrier.wait()
                client.move(col)

            threads = [threading.Thread(target=fire, args=(c,))
                       for c in (p1, p2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            for c in (p1, p2):
                for msg in c.drain(0.1):
                    if msg["msg_type"] == "STATE_UPDATE":
                        seen[c].append(msg["payload"])
                    elif msg["msg_type"] == "GAME_OVER":
                        game_over = True
            if game_over:
                break

        self.assertEqual(seen[p1], seen[p2])  # identical for both clients
        numbers = [p["move_number"] for p in seen[p1]]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        for payload in seen[p1]:
            discs = sum(row.count("X") + row.count("O")
                        for row in payload["board"])
            self.assertEqual(discs, payload["move_number"])
            expected_next = ("Player_2" if payload["move_number"] % 2
                             else "Player_1")
            self.assertEqual(payload["next_turn"], expected_next)
        self.assertGreater(len(numbers), 5)

    def test_many_idle_connections_do_not_block_play(self):
        for i in range(4):
            self.client(f"Idle{i}")  # connected, never send CONNECT
        p1, p2 = self.start_game("Alice", "Bob")
        play_move(p1, p2, 3)

    def test_connection_cap_refuses_extras(self):
        old = srv.MAX_CONNECTIONS
        srv.MAX_CONNECTIONS = 2
        self.addCleanup(setattr, srv, "MAX_CONNECTIONS", old)
        self.client("one")
        self.client("two")
        self.assertTrue(self.wait_until(
            lambda: srv.live_handler_count() == 2))
        extra = self.client("three")
        extra.sock.settimeout(2)
        try:
            self.assertEqual(extra.sock.recv(10), b"")  # closed at once
        except (ConnectionResetError, socket.timeout):
            pass


if __name__ == "__main__":
    unittest.main()
