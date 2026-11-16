#!/usr/bin/env python3
"""Run a scripted local demo and save the Sprint 3 gameplay execution log.

Starts server.py and two client.py processes on localhost, feeds each
client its moves through stdin, and records everything the three programs
print into one time-ordered log (prefixed [SERVER], [ALICE], [BOB]).

Game played: Alice (X) stacks column 4; Bob (O) answers in column 5.
Alice wins on her 4th move - that is 7 turns in total.

Usage (from the repository root):
  python3 scripts/run_demo.py
  python3 scripts/run_demo.py --port 9090 --out logs/gameplay_log.txt
"""
import argparse
import os
import queue
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALICE_MOVES = ["4", "4", "4", "4"]
BOB_MOVES = ["5", "5", "5"]


def pump(stream, label: str, sink: "queue.Queue") -> None:
    """Copy lines from a process's stdout into the shared queue."""
    for line in iter(stream.readline, ""):
        sink.put((time.time(), label, line.rstrip("\n")))


def start(args, label, sink, stdin=None):
    """Launch a Python child process with unbuffered, merged output."""
    proc = subprocess.Popen(
        [sys.executable, "-u"] + args, cwd=ROOT, text=True,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    threading.Thread(target=pump, args=(proc.stdout, label, sink),
                     daemon=True).start()
    if stdin is not None:
        proc.stdin.write("\n".join(stdin) + "\n")
        proc.stdin.flush()
    return proc


def wait_for(sink_lines, text: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(text in line for line in list(sink_lines)):
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--out", default="logs/gameplay_log.txt")
    args = parser.parse_args()

    sink: "queue.Queue" = queue.Queue()
    seen = []  # every line, for wait_for()

    def collect() -> None:
        while True:
            item = sink.get()
            seen.append(item[2])
            records.append(item)

    records = []
    threading.Thread(target=collect, daemon=True).start()

    server = start(["server.py", "-p", str(args.port)], "SERVER", sink)
    if not wait_for(seen, "Listening on"):
        print("Server did not start; is the port already in use?")
        server.kill()
        return 1

    common = ["client.py", "-i", "localhost", "-p", str(args.port)]
    alice = start(common + ["-a", "Alice"], "ALICE", sink, ALICE_MOVES)
    wait_for(seen, "Waiting for Player 2 to connect")  # Alice seated first
    bob = start(common + ["-a", "Bob"], "BOB", sink, BOB_MOVES)

    codes = []
    for proc in (alice, bob):
        try:
            codes.append(proc.wait(timeout=30))
        except subprocess.TimeoutExpired:
            proc.kill()
            codes.append(-1)
    time.sleep(0.3)  # let the final server log lines arrive
    server.terminate()
    server.wait(timeout=5)
    time.sleep(0.2)

    out_path = os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    lines = [
        "Connect Four - Sprint 3 gameplay execution log",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(localhost, port {args.port})",
        "Python " + sys.version.split()[0],
        f"Client exit codes: Alice={codes[0]} Bob={codes[1]}",
        "Moves: Alice (X) plays column 4 four times, Bob (O) plays "
        "column 5 three times (7 turns).",
    ]
    # One section per program, in the order the output was produced.
    for label, title in (("ALICE", "CLIENT 1 SESSION (Alice, Player_1)"),
                         ("BOB", "CLIENT 2 SESSION (Bob, Player_2)"),
                         ("SERVER", "SERVER LOG (FSM transitions T1-T15)")):
        lines += ["", "=" * 70, title, "=" * 70]
        lines += [text for _, who, text in sorted(records,
                                                  key=lambda r: r[0])
                  if who == label and text.strip()]
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved to {out_path}")
    return 0 if codes == [0, 0] else 1


if __name__ == "__main__":
    sys.exit(main())
