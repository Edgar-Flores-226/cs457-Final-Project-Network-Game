"""Unit tests for protocol.py validation and messages.py builders."""
import json
import unittest

import messages
from protocol import (ProtocolError, is_fatal, parse_message,
                      validate_client_message, validate_server_message)


def good(msg_type="MOVE", player_id="Player_1", payload=None):
    return {"msg_type": msg_type, "player_id": player_id,
            "payload": {"col": 3} if payload is None else payload,
            "timestamp": 1791100000}


def code_of(func, *args):
    """Call func and return the ProtocolError code it raises."""
    try:
        func(*args)
    except ProtocolError as exc:
        return exc.code
    raise AssertionError("ProtocolError was not raised")


class ParseTests(unittest.TestCase):
    def test_valid_json_object(self):
        self.assertEqual(parse_message(b'{"a":1}'), {"a": 1})

    def test_invalid_json(self):
        self.assertEqual(code_of(parse_message, b"not json"),
                         "MALFORMED_JSON")

    def test_invalid_utf8(self):
        self.assertEqual(code_of(parse_message, b"\xff\xfe"),
                         "MALFORMED_JSON")

    def test_array_is_not_an_object(self):
        self.assertEqual(code_of(parse_message, b"[1,2]"), "MALFORMED_JSON")

    def test_deeply_nested_json_does_not_crash(self):
        self.assertEqual(code_of(parse_message, b"[" * 4000),
                         "MALFORMED_JSON")


class ClientValidationTests(unittest.TestCase):
    def test_valid_move(self):
        validate_client_message(good())

    def test_move_range_is_not_checked_here(self):
        validate_client_message(good(payload={"col": 99}))

    def test_missing_envelope_key(self):
        msg = good()
        del msg["timestamp"]
        self.assertEqual(code_of(validate_client_message, msg),
                         "SCHEMA_VIOLATION")

    def test_bool_is_not_an_int(self):
        self.assertEqual(
            code_of(validate_client_message, good(payload={"col": True})),
            "SCHEMA_VIOLATION")
        msg = good()
        msg["timestamp"] = True
        self.assertEqual(code_of(validate_client_message, msg),
                         "SCHEMA_VIOLATION")

    def test_string_column_rejected(self):
        self.assertEqual(
            code_of(validate_client_message, good(payload={"col": "3"})),
            "SCHEMA_VIOLATION")

    def test_unknown_type(self):
        self.assertEqual(code_of(validate_client_message, good("PING")),
                         "UNKNOWN_MSG_TYPE")

    def test_server_only_type_from_client(self):
        for t in ("STATE_UPDATE", "GAME_OVER", "ERROR", "LOBBY_WAIT",
                  "GAME_START"):
            self.assertEqual(code_of(validate_client_message, good(t)),
                             "UNKNOWN_MSG_TYPE", t)

    def test_connect_alias_rules(self):
        ok = good("CONNECT", "UNASSIGNED",
                  {"alias": "Alice_1", "protocol_version": 1})
        validate_client_message(ok)
        for alias in ("", "a" * 17, "bad name", "bad-name", 5):
            bad = good("CONNECT", "UNASSIGNED",
                       {"alias": alias, "protocol_version": 1})
            self.assertEqual(code_of(validate_client_message, bad),
                             "SCHEMA_VIOLATION", repr(alias))

    def test_connect_version(self):
        bad = good("CONNECT", "UNASSIGNED",
                   {"alias": "A", "protocol_version": 2})
        self.assertEqual(code_of(validate_client_message, bad),
                         "VERSION_UNSUPPORTED")

    def test_disconnect_reason(self):
        validate_client_message(good("DISCONNECT", payload={"reason":
                                                            "QUIT"}))
        self.assertEqual(
            code_of(validate_client_message,
                    good("DISCONNECT", payload={"reason": "x"})),
            "SCHEMA_VIOLATION")


class BuilderTests(unittest.TestCase):
    def test_builders_produce_valid_server_messages(self):
        board = ["......."] * 6
        built = [
            messages.build_lobby_wait(),
            messages.build_game_start("Player_1", "X", "Bob", board),
            messages.build_state_update(board, "Player_1", 5, 3,
                                        "Player_2", 1),
            messages.build_error("OUT_OF_TURN", "no"),
            messages.build_game_over("WIN", "Player_1", "FOUR_IN_A_ROW",
                                     [[5, 0]] * 4, board,
                                     {"Player_1": 1, "Player_2": 0}),
        ]
        for msg in built:
            validate_server_message(json.loads(json.dumps(msg)))

    def test_error_fatal_flag_derived_from_code(self):
        self.assertFalse(
            messages.build_error("OUT_OF_TURN", "x")["payload"]["fatal"])
        self.assertTrue(
            messages.build_error("GAME_FULL", "x")["payload"]["fatal"])
        self.assertTrue(is_fatal("FRAME_TOO_LARGE"))

    def test_unknown_error_code_rejected(self):
        with self.assertRaises(ValueError):
            messages.build_error("MADE_UP", "x")

    def test_envelope_key_order_matches_blueprint(self):
        self.assertEqual(list(messages.build_move("Player_1", 3)),
                         ["msg_type", "player_id", "payload", "timestamp"])

    def test_move_message_matches_blueprint_example(self):
        msg = messages.build_move("Player_1", 3)
        self.assertEqual(msg["payload"], {"col": 3})


if __name__ == "__main__":
    unittest.main()
