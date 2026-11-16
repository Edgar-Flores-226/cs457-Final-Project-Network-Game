# Sprint 3 Run Guide - Connect Four Client and Server
 
- **Student:** Edgar Flores
- **Course:** CS 457 - Computer Networks and the Internet
- **Sprint:** 3
- **Language:** Python 3.9+ (standard library only, no `pip install` needed)
## Contents
 
- [1. Project Layout](#1-project-layout)
- [2. How to Run](#2-how-to-run)
- [3. How to Test](#3-how-to-test)
- [4. How the Code Maps to the Design Documents](#4-how-the-code-maps-to-the-design-documents)
- [5. Sprint 3 Rubric Checklist](#5-sprint-3-rubric-checklist)
- [6. Deliberate Implementation Notes](#6-deliberate-implementation-notes)
## 1. Project Layout
 
```text
client.py          Client entry point (CLI args, DNS resolution, game UI)
server.py          Server entry point (threads, STATE_LOCK, matchmaking, moves)
framing.py         4-byte big-endian length-prefix framing (recv_exact, read_frame)
protocol.py        Constants, error codes, parse + validate messages
messages.py        Builders for all 8 message types
game_fsm.py        Connect Four rules and the FSM (State enum, transition table)
cli.py             Shared argparse validators (port 1024-65535, alias, host)
scripts/run_demo.py   Plays a scripted 2-client game and writes the execution log
tests/             106 automated tests (unit, integration, CLI, end-to-end)
logs/gameplay_log.txt Intermediate gameplay execution log (generated)
docs/              Sprint 1 and 2 design documents plus this guide
```
 
## 2. How to Run
 
Server (binds to `0.0.0.0`):
 
```bash
python3 server.py -h
python3 server.py -p 8080
```
 
Client (open two terminals, one per player):
 
```bash
python3 client.py -h
python3 client.py -i localhost -p 8080
python3 client.py -i localhost -p 8080 -a Alice
```
 
- `-i` accepts a DNS hostname (resolved with `socket.gethostbyname()`) or an IP address.
- `-a` is optional; without it the client asks for a name before connecting.
- Add `--debug` to either program to log every frame (4-byte header plus JSON body).
- During your turn type a column `1`-`7`, or `q` to quit (quitting forfeits).
- `Ctrl+C` on the server shuts it down; `Ctrl+C` on a client disconnects cleanly.
DNS failure example (graceful, exit code 1, no traceback):
 
```bash
python3 client.py -i nonexistent.invalid -p 8080
```
 
## 3. How to Test
 
Run everything (about 10 seconds):
 
```bash
python3 -m unittest discover -s tests -t .
```
 
| Test file            | What it covers                                                                     |
| :------------------- | :--------------------------------------------------------------------------------- |
| `test_framing.py`    | Length header, 1-byte fragmentation, coalescing, EOF, bad lengths, body timeout    |
| `test_protocol.py`   | JSON/schema validation, `bool` rejected as `int`, aliases, message builders        |
| `test_game_fsm.py`   | Gravity, all 4 win directions, draw, FSM transition table                          |
| `test_server.py`     | Real sockets: handshake, turn enforcement, errors, win/draw/forfeit, scores, races |
| `test_cli.py`        | `-h`, required `-i`/`-p`, port range, DNS failure, connection refused              |
| `test_client_e2e.py` | Real `client.py` processes playing against the real server                         |
 
Generate the gameplay execution log (starts the server and two clients, plays 7 turns):
 
```bash
python3 scripts/run_demo.py
```
 
Or capture it by hand for a screenshot: run the server in one terminal and a client in each of two others, then play at least three turns each.
 
## 4. How the Code Maps to the Design Documents
 
| Design document                                          | Implemented in                                                                      |
| :------------------------------------------------------- | :---------------------------------------------------------------------------------- |
| Protocol: framing, 8 message types, error codes          | `framing.py`, `protocol.py`, `messages.py`                                          |
| FSM: states, T1-T15, validation order                    | `game_fsm.py` (transition table), `server.py` (`handle_move`, `finish_game_locked`) |
| Concurrency: one thread per connection, one `STATE_LOCK` | `server.py` (`accept_loop`, `client_thread`, `STATE_LOCK`)                          |
| Race analysis R1-R9                                      | `tests/test_server.py` (`ConcurrencyTests`, `GameEndTests`)                         |
| Scoreboard and score broadcast                           | `record_result_locked`, `finish_game_locked`, `GAME_OVER.scores`                    |
 
The server logs every FSM transition with its ID, for example `EVALUATE_MOVE -> PLAYER_TURN (T9)`, so the running server can be compared directly with `fsm_specification.md`.
 
## 5. Sprint 3 Rubric Checklist
 
| Rubric item                                                                                             | Where                                                                    |
| :------------------------------------------------------------------------------------------------------ | :----------------------------------------------------------------------- |
| CLI parsing: client `-h -i -p`, server `-h -p`, binds `0.0.0.0`, port range 1024-65535, usage on errors | `client.py`, `server.py`, `cli.py`; `test_cli.py`                        |
| DNS support and graceful `socket.gaierror` handling                                                     | `client.py` `resolve_host()`; `test_cli.py`                              |
| TCP stream framing and serialization matching Sprint 1                                                  | `framing.py`, `protocol.py`; `test_framing.py`                           |
| 2 simultaneous clients, session isolation, FSM turn enforcement, broadcasts                             | `server.py`; `test_server.py`                                            |
| PEP 8, modular, commented, execution log, Git                                                           | `flake8 --max-line-length=79` reports no issues; `logs/gameplay_log.txt` |
 
## 6. Deliberate Implementation Notes
 
- `MAX_CONNECTIONS` counts live handler threads (named `client-...`) rather than `threading.active_count()`, so unrelated threads (for example in the test runner) are not counted.
- A `GAME_FULL` refusal is sent after the third client's `CONNECT` is validated, which keeps the main accept loop free of any client I/O.
- Player 1 leaving the lobby is detected as soon as its thread sees EOF. In the rare instant before that, a second player could be paired with a vanished first player; the server then follows FSM transition T12 and awards the forfeit.
 