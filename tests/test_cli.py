"""Command-line behaviour of client.py and server.py (Sprint 3 rubric 1-2).

Runs the real scripts as subprocesses and checks exit codes and output.
"""
import os
import socket
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(script, *args, timeout=30):
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, script), *args],
        capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL)


class ServerCliTests(unittest.TestCase):
    def test_help(self):
        r = run("server.py", "-h")
        self.assertEqual(r.returncode, 0)
        self.assertIn("-p", r.stdout)
        self.assertIn("usage:", r.stdout)
        self.assertIn("0.0.0.0", r.stdout)

    def test_port_is_required(self):
        r = run("server.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage:", r.stderr)
        self.assertIn("-p", r.stderr)

    def test_port_must_be_a_number(self):
        r = run("server.py", "-p", "abc")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid port", r.stderr)

    def test_port_range(self):
        for bad in ("80", "1023", "65536", "-5", "0"):
            r = run("server.py", "-p", bad)
            self.assertEqual(r.returncode, 2, bad)
            self.assertIn("usage:", r.stderr)
        # boundaries are accepted by the parser
        import server
        self.assertEqual(server.build_parser().parse_args(["-p", "1024"])
                         .port, 1024)
        self.assertEqual(server.build_parser().parse_args(["-p", "65535"])
                         .port, 65535)

    def test_port_already_in_use_fails_gracefully(self):
        busy = socket.socket()
        busy.bind(("0.0.0.0", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        port = busy.getsockname()[1]
        if port < 1024:
            self.skipTest("ephemeral port below 1024")
        r = run("server.py", "-p", str(port))
        self.assertEqual(r.returncode, 1)
        self.assertIn("[ERROR] Cannot listen", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class ClientCliTests(unittest.TestCase):
    def test_help(self):
        r = run("client.py", "-h")
        self.assertEqual(r.returncode, 0)
        for flag in ("-i", "-p", "usage:"):
            self.assertIn(flag, r.stdout)

    def test_required_arguments(self):
        for args in ((), ("-i", "localhost"), ("-p", "8080")):
            r = run("client.py", *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("usage:", r.stderr)
            self.assertIn("required", r.stderr)

    def test_bad_port_and_alias(self):
        for args in (("-i", "localhost", "-p", "22"),
                     ("-i", "localhost", "-p", "x"),
                     ("-i", "localhost", "-p", "8080", "-a", "bad name")):
            r = run("client.py", *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("usage:", r.stderr)

    def test_dns_failure_exits_cleanly(self):
        # ".invalid" is reserved (RFC 2606): it can never resolve.
        r = run("client.py", "-i", "nonexistent.invalid", "-p", "8080",
                "-a", "Alice")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[ERROR] Unable to resolve DNS hostname "
                      "'nonexistent.invalid'.", r.stdout)
        self.assertIn("Cisco router DNS configuration", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_unencodable_hostname_is_a_dns_failure_too(self):
        r = run("client.py", "-i", "a" * 80 + ".example", "-p", "8080",
                "-a", "Alice")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Unable to resolve", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_connection_refused_exits_cleanly(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing is listening on this port now
        if port < 1024:
            self.skipTest("ephemeral port below 1024")
        r = run("client.py", "-i", "127.0.0.1", "-p", str(port),
                "-a", "Alice")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_ip_literal_is_accepted_without_dns(self):
        import client
        self.assertEqual(client.resolve_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(client.resolve_host("localhost"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
