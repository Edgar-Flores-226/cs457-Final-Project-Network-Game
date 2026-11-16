"""End-to-end: the real client.py processes against the real server."""
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest

import server as srv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        srv.reset_state()
        self.listener = srv.create_listener(0, "127.0.0.1")
        self.port = self.listener.getsockname()[1]
        self.procs = []
        threading.Thread(target=srv.accept_loop, args=(self.listener,),
                         daemon=True).start()

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()

    def start_client(self, alias, moves):
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.join(ROOT, "client.py"),
             "-i", "localhost", "-p", str(self.port), "-a", alias],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        proc.stdin.write("".join(m + "\n" for m in moves))
        proc.stdin.close()
        proc.stdin = None  # we closed it ourselves; communicate() must not
        self.procs.append(proc)
        return proc

    def play(self, alice_moves, bob_moves):
        alice = self.start_client("Alice", alice_moves)
        end = time.time() + 5
        while srv.server.lobby is None and time.time() < end:
            time.sleep(0.02)  # make sure Alice is seated first
        bob = self.start_client("Bob", bob_moves)
        out_a = alice.communicate(timeout=20)[0]
        out_b = bob.communicate(timeout=20)[0]
        return alice, bob, out_a, out_b

    def test_full_game_two_clients(self):
        alice, bob, out_a, out_b = self.play(["4"] * 4, ["5"] * 3)
        self.assertEqual((alice.returncode, bob.returncode), (0, 0))
        for out in (out_a, out_b):
            self.assertIn("Game started!", out)
            self.assertIn("SCORES", out)
        self.assertIn("Waiting for Player 2 to connect", out_a)
        self.assertIn("You are Player_1 (X)", out_a)
        self.assertIn("You are Player_2 (O)", out_b)
        self.assertGreaterEqual(out_a.count(">>> Your Turn <<<"), 4)
        self.assertGreaterEqual(out_b.count(">>> Your Turn <<<"), 3)
        self.assertIn("Waiting for Player_2 (Bob) to move", out_a)
        self.assertIn("Waiting for Player_1 (Alice) to move", out_b)
        self.assertIn("You WIN!", out_a)
        self.assertIn("You lose", out_b)
        self.assertIn("Alice (Player_1, X) : 1 win", out_a)
        self.assertNotIn("Traceback", out_a + out_b)

    def test_invalid_input_is_reprompted(self):
        alice, bob, out_a, out_b = self.play(
            ["abc", "9", "0", "4"], ["q"])
        self.assertEqual(out_a.count("Invalid input"), 3)
        self.assertEqual((alice.returncode, bob.returncode), (0, 0))
        self.assertIn("by forfeit", out_a)

    def test_quitting_forfeits_to_the_opponent(self):
        alice, bob, out_a, out_b = self.play(["4", "q"], ["5", "5"])
        self.assertEqual((alice.returncode, bob.returncode), (0, 0))
        self.assertIn("you forfeit", out_a)
        self.assertIn("Opponent disconnected. You win by forfeit", out_b)
        self.assertNotIn("Traceback", out_a + out_b)

    def test_stdin_eof_counts_as_quit(self):
        alice, bob, out_a, out_b = self.play([], ["5"])  # Alice: no input
        self.assertEqual(alice.returncode, 0)
        self.assertIn("by forfeit", out_b)

    def run_client_against(self, server_behaviour):
        """Run client.py against a fake server whose behaviour is given by
        server_behaviour(conn); return the finished process."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        def serve():
            conn, _ = listener.accept()
            server_behaviour(conn)

        threading.Thread(target=serve, daemon=True).start()
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "client.py"), "-i",
             "127.0.0.1", "-p", str(port), "-a", "Alice"],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL)

    def test_server_closing_connection_is_handled(self):
        def read_connect_then_close(conn):
            conn.recv(4096)  # consume CONNECT first so the close is a
            conn.close()     # clean FIN, not a reset (RST)

        r = self.run_client_against(read_connect_then_close)
        self.assertEqual(r.returncode, 1)
        self.assertIn("server closed the connection", r.stdout.lower())
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_server_connection_reset_is_handled(self):
        def reset_connection(conn):
            conn.recv(4096)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
            conn.close()  # linger 0 = send RST, like a crashed server

        r = self.run_client_against(reset_connection)
        self.assertEqual(r.returncode, 1)
        self.assertIn("lost connection to the server", r.stdout.lower())
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_closing_with_unread_data_gives_a_clean_error(self):
        """Closing while the client's CONNECT is unread makes TCP send a
        reset instead of a normal close; the client must cope either way
        (this is the timing case that made the old test flaky)."""
        def close_without_reading(conn):
            time.sleep(0.4)  # let CONNECT arrive and sit unread
            conn.close()

        r = self.run_client_against(close_without_reading)
        self.assertEqual(r.returncode, 1)
        self.assertRegex(r.stdout.lower(),
                         "server closed the connection|lost connection")
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_garbage_from_server_is_handled(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        def send_garbage():
            conn, _ = listener.accept()
            conn.sendall(b"\x00\x00\x00\x05hello")  # valid frame, bad JSON
            time.sleep(1)
            conn.close()

        threading.Thread(target=send_garbage, daemon=True).start()
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "client.py"), "-i",
             "127.0.0.1", "-p", str(port), "-a", "Alice"],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bad data from the server", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
