# AI Prompting & Constraint Strategy

- **Student:** Edgar Flores
- **Course:** CS 457 - Computer Networks and the Internet
- **Sprint:** 1 and 2
- **Governing documents:** `protocol_blueprint.md`, `fsm_specification.md` (Sprint 1); `concurrency_design.md`, `sequence_diagram.md` (Sprint 2)

## Contents

- [1. Strategy Overview](#1-strategy-overview)
- [2. Master System Prompt (Paste at the Start of Every AI Session)](#2-master-system-prompt-paste-at-the-start-of-every-ai-session)
- [3. Task Prompts (Each Used After the Master Prompt)](#3-task-prompts-each-used-after-the-master-prompt)
- [4. Anti-Boilerplate Rejection Rules](#4-anti-boilerplate-rejection-rules)
- [5. Human Review Checklist (Applied to Every Generated File Before Commit)](#5-human-review-checklist-applied-to-every-generated-file-before-commit)
- [6. Sprint 2 Update: Concurrency-Constrained Prompting](#6-sprint-2-update-concurrency-constrained-prompting)
- [7. Prompt Log (Fill In As You Work)](#7-prompt-log-fill-in-as-you-work)

## 1. Strategy Overview

Unconstrained AI tools default to generic socket boilerplate: `recv(1024)`, `json.loads` on whatever arrived, newline splitting, no EOF check, and an invented message format. That code fails against this project's protocol. My strategy is to make the **blueprint the specification and the AI the typist**:

| Principle                            | How it is applied                                                                                                                                                                 |
| :----------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Blueprint is the source of truth** | The exact schema, framing rule, constants, and error codes are pasted into every session. The AI is told that the documents win over its own preferences.                         |
| **One small unit per prompt**        | One function or one module at a time (framing, then validation, then builders, then state handlers), never "write the whole server".                                              |
| **Explicit negative constraints**    | Each prompt lists forbidden patterns (e.g. "no single `recv()` assumed to be a full message").                                                                                    |
| **Closed vocabulary**                | The AI may only use the 8 message types, 14 error codes, and 8 FSM states defined in the docs. Inventing a new one is a failure.                                                  |
| **Tests before trust**               | Each prompt ships with acceptance tests (fragmentation, coalescing, EOF, bad schema) that I run myself. Output that fails is rejected and re-prompted, not hand-patched silently. |
| **Human review checklist**           | Section 5 is applied to every generated file before it is committed.                                                                                                              |
| **Log everything**                   | Section 6 records each prompt, the result, and the correction, so Git history shows the constraint loop.                                                                          |

---

## 2. Master System Prompt (Paste at the Start of Every AI Session)

```text
You are implementing code for a two-player Connect Four game over TCP in Python 3.9
(standard library only: socket, struct, json, enum, logging, select, re).
You are NOT designing the protocol. The protocol is fixed below. If anything in my request
conflicts with this specification, the specification wins and you must tell me about the conflict
instead of silently changing it. If something is not specified, ask me; do not invent it.

=== FRAMING (C4P v1) ===
- Every message is ONE frame: a 4-byte unsigned big-endian length N, then N bytes of UTF-8 JSON.
- Pack with struct.pack("!I", len(body)). Unpack with struct.unpack("!I", header).
- N must satisfy 1 <= N <= 4096. Otherwise it is a FATAL framing error (FRAME_EMPTY / FRAME_TOO_LARGE).
- You must read exactly 4 header bytes and then exactly N body bytes using a recv_exact() loop.
  A single recv() call must NEVER be assumed to return a complete header or message.
- recv() returning b"" is EOF (peer closed). Every receive path must check it and treat it as a
  disconnect. EOF in the middle of a frame is also a disconnect.
- Compact JSON on send: json.dumps(msg, separators=(",", ":")).
- Do NOT use newline delimiters, readline(), makefile(), split("\n"), or recv(1024)-as-a-message.

=== ENVELOPE (exactly 4 keys) ===
{"msg_type": str, "player_id": str, "payload": object, "timestamp": int (Unix epoch seconds)}
- Client->Server player_id: "UNASSIGNED" in CONNECT, afterwards "Player_1" or "Player_2" (must match
  the id assigned to that socket). Server->Client player_id: always "SERVER".
- Extra unknown keys are ignored. Missing/wrong-typed keys => SCHEMA_VIOLATION.
- Integer fields must be validated with type(x) is int (reject bool, since bool subclasses int).

=== MESSAGE TYPES (closed set of 8; do not add or rename) ===
CONNECT      C->S  payload {"alias": str matching ^[A-Za-z0-9_]{1,16}$, "protocol_version": int == 1}
LOBBY_WAIT   S->C  payload {"players_connected": int, "players_needed": int}
GAME_START   S->C  payload {"you": "Player_1"|"Player_2", "symbol": "X"|"O", "opponent_alias": str,
                            "first_turn": "Player_1", "board": [6 strings of 7 chars]}
MOVE         C->S  payload {"col": int, 0..6}      (NO "row" field; server applies gravity)
STATE_UPDATE S->C  payload {"board": [6 strings of 7 chars], "last_move": {"player_id": str,
                            "row": int, "col": int}, "next_turn": str, "move_number": int}
ERROR        S->C  payload {"code": str, "message": str, "fatal": bool}
DISCONNECT   C->S  payload {"reason": "QUIT"}
GAME_OVER    S->C  payload {"result": "WIN"|"DRAW"|"FORFEIT", "winner": str|null, "reason":
                            "FOUR_IN_A_ROW"|"BOARD_FULL"|"OPPONENT_DISCONNECTED",
                            "winning_cells": [[row,col],...], "board": [...], "scores":
                            {"Player_1": int, "Player_2": int}}

=== BOARD ===
6 rows x 7 cols. Row 0 = TOP, row 5 = BOTTOM. Wire columns 0..6. Cells: "X" (Player_1),
"O" (Player_2), "." empty. Board on the wire = list of 6 strings, row 0 first.

=== ERROR CODES (closed set) ===
Non-fatal: MALFORMED_JSON, SCHEMA_VIOLATION, UNKNOWN_MSG_TYPE, WRONG_STATE, ID_MISMATCH,
           OUT_OF_TURN, INVALID_COLUMN, COLUMN_FULL
Fatal:     VERSION_UNSUPPORTED, GAME_FULL, FRAME_EMPTY, FRAME_TOO_LARGE, FRAME_TIMEOUT,
           HANDSHAKE_TIMEOUT

=== FSM (closed set of states) ===
INIT, WAITING_FOR_PLAYERS, GAME_START, PLAYER_TURN, EVALUATE_MOVE, FORFEIT_HANDLING,
GAME_OVER, CLEANUP.
- Exactly one self.state variable, changed ONLY inside transition(new_state, transition_id).
- Handlers return the next state; they never assign self.state directly.
- Errors never pass the turn. Disconnect in any in-game state => FORFEIT_HANDLING.

=== SOCKET RULES ===
- All reads go through read_message(sock); all writes go through send_message(sock, msg).
- Catch ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError (socket.timeout)
  and convert them into the CLIENT_DISCONNECTED event. Never let them crash the process.
- Each send to a client inside GAME_OVER/CLEANUP is wrapped in its own try/except.

=== OUTPUT RULES ===
- Standard library only. Python 3.9 compatible (no match statements, no X | Y type unions).
- Type hints and short docstrings. Use logging, not print, for diagnostics.
- Define the constants (MAX_FRAME_BYTES = 4096, HEADER_FORMAT = "!I", etc.) once at module top.
- Return ONLY the requested function/module. No example servers, no extra features, no
  protocol "improvements". After the code, list which spec rules each part implements.
```

---

## 3. Task Prompts (Each Used After the Master Prompt)

### Prompt P1 - Framing Module (`framing.py`)

```text
Using the C4P v1 specification above, write framing.py containing exactly:
  1. encode_frame(message: dict) -> bytes
  2. recv_exact(sock, n_bytes: int) -> Optional[bytes]   (None on EOF)
  3. read_frame(sock) -> Optional[bytes]  -> reads 4-byte header via recv_exact, validates
     1 <= N <= MAX_FRAME_BYTES, then reads N bytes via recv_exact. Returns None on EOF at any point.
     Raises FrameError(code) with code "FRAME_EMPTY" or "FRAME_TOO_LARGE" for bad lengths.
  4. class FrameError(Exception) with a .code attribute.
Constraints: no newline logic, no recv(1024), no buffering library, no threading. Every recv path
handles b"". Do not decode or parse JSON in this module; it deals in bytes only.
```

**Acceptance tests I run:**

- Send a valid frame one byte at a time through a `socketpair()`; `read_frame` returns the identical body.
- Send three frames in one `sendall()`; three consecutive `read_frame` calls return three correct bodies.
- Header `00 00 00 00` raises `FrameError("FRAME_EMPTY")`; header `FF FF FF FF` raises `FrameError("FRAME_TOO_LARGE")`.
- Close the peer mid-body; `read_frame` returns `None` and the test does not hang.

### Prompt P2 - Message Validation (`protocol.py`)

```text
Using the C4P v1 specification above, write protocol.py containing:
  1. parse_message(body: bytes) -> dict  -> decode UTF-8, json.loads, ensure the result is a dict.
     Raises ProtocolError("MALFORMED_JSON") on any failure.
  2. validate_envelope(msg: dict) -> None -> checks the 4 envelope keys and their types.
     Raises ProtocolError("SCHEMA_VIOLATION") otherwise.
  3. validate_client_message(msg: dict) -> None -> checks msg_type is one of the three
     client-originated types (CONNECT, MOVE, DISCONNECT) and validates that type's payload exactly
     as specified (alias regex, protocol_version == 1, col type is int and not bool, reason == "QUIT").
     Raises ProtocolError("UNKNOWN_MSG_TYPE") for unknown or server-only types, and
     ProtocolError("SCHEMA_VIOLATION") for payload problems.
     NOTE: range checking col (0..6) is NOT done here; the FSM does it in EVALUATE_MOVE.
  4. class ProtocolError(Exception) with .code.
Constraints: no third-party validators (no pydantic, jsonschema). Reject bool for int fields.
Use only error codes from the closed list.
```

**Acceptance tests:** `{"col": true}` rejected; `{"col": "3"}` rejected; `{"col": 9}` **accepted** here (range is FSM's job); missing `timestamp` rejected; array body rejected as `MALFORMED_JSON`; `msg_type:"STATE_UPDATE"` from a client rejected as `UNKNOWN_MSG_TYPE`.

### Prompt P3 - Message Builders (`messages.py`)

```text
Using the C4P v1 specification above, write messages.py with one builder function per
SERVER->CLIENT message type: build_lobby_wait, build_game_start, build_state_update, build_error,
build_game_over. Each returns the full envelope dict (msg_type, player_id="SERVER", payload,
timestamp=int(time.time())). Field names, nesting, and types must match the spec character for
character. build_error must accept only codes from the closed error list and derive "fatal" from
the code (not from a caller argument). Do not add any field that is not in the specification.
```

**Acceptance tests:** compare each builder's output against the exact example JSON in `protocol_blueprint.md` (ignoring `timestamp`); `build_error("MADE_UP")` raises `ValueError`.

### Prompt P4 - FSM Core (`game_fsm.py`)

```text
Using the C4P v1 specification above and the FSM transition table T1-T15 below, write game_fsm.py
with: the State enum; a Game class holding board, players, active, move_number, scores; a single
transition(new_state, tid) method that logs "old -> new (Tn)"; and one handler per state that
returns the next State. Implement the move pipeline in this order and stop at the first failure:
  OUT_OF_TURN (checked in PLAYER_TURN), then INVALID_COLUMN, then COLUMN_FULL (in EVALUATE_MOVE).
An error NEVER changes the active player. Win detection must check the 4 axes through the last
placed disc and return the 4 winning cells. A full board with no win is a DRAW.
Do not touch sockets in this module: it takes events in and returns outgoing messages as
(recipient, message_dict) tuples so it can be unit-tested without a network.
[Paste Section 3 of fsm_specification.md here]
```

**Acceptance tests:** the 13 scenarios in `fsm_specification.md` Section 8 that do not require a real socket (1-6, 8, 10), run as pure unit tests.

### Prompt P5 - Server Socket Loop & Disconnect Handling (`server.py`)

```text
Using the C4P v1 specification above, write server.py that connects the sockets to the Game class
from game_fsm.py. Requirements:
  - Listening socket with SO_REUSEADDR; accept up to 2 players; a third connection receives a
    fatal ERROR GAME_FULL and is closed.
  - A 10-second handshake timeout per new socket and a 10-second body timeout after a header.
  - All input via framing.read_frame + protocol.parse_message + validation; all output via
    send_message (encode_frame + sendall).
  - EOF (read_frame returns None) and ConnectionResetError / BrokenPipeError /
    ConnectionAbortedError / TimeoutError all produce the CLIENT_DISCONNECTED event, which drives
    the FSM to FORFEIT_HANDLING during a game or removes the player from the lobby.
  - GAME_OVER sends are each wrapped in their own try/except; sockets are always closed in CLEANUP.
Do not add chat, rematch, reconnect, or any feature not in the specification.
```

**Acceptance tests:** scenarios 7, 8, 9, 10, 11, 12, 13 from `fsm_specification.md` Section 8, including a manual `kill -9` of a client process and a CML link-down test.

---

## 4. Anti-Boilerplate Rejection Rules

I reject (and re-prompt) any generated code that contains the following:

| Red flag                                                                      | Why it violates the blueprint                                                      |
| :---------------------------------------------------------------------------- | :--------------------------------------------------------------------------------- |
| `data = sock.recv(1024)` followed by `json.loads(data)`                       | Assumes one `recv()` equals one message; breaks under coalescing and fragmentation |
| `.split("\n")`, `.readline()`, `sock.makefile()`                              | Newline framing is not this protocol                                               |
| `struct.pack("<I", ...)` or `"I"` without `!`                                 | Wrong byte order or native alignment; must be network byte order                   |
| No `if not data` / `is None` EOF check                                        | Infinite loop on disconnect                                                        |
| `"row"` in a `MOVE` payload                                                   | `MOVE` carries `col` only                                                          |
| Any `msg_type` outside the 8 defined (e.g. `CHAT`, `PING`, `REMATCH`, `JOIN`) | Closed vocabulary                                                                  |
| `self.state = ...` outside `transition()`                                     | Breaks single-source-of-truth FSM                                                  |
| An error path that calls `swap_turn()`                                        | Errors must not pass the turn                                                      |
| Bare `except:` or `except Exception: pass`                                    | Hides disconnects; only the named socket exceptions are caught                     |
| Third-party imports                                                           | Standard library only                                                              |

**Example of a corrective re-prompt** (template, used when a generation violates the rules):

```text
Your last output reads the socket with a single recv(1024) and parses it as one JSON message.
That violates the framing rule in the specification: every message is a 4-byte big-endian
length header followed by exactly N body bytes, read with a recv_exact() loop, and b"" means EOF.
Regenerate only read_frame() so that it follows that rule. Do not change any other function.
```

---

## 5. Human Review Checklist (Applied to Every Generated File Before Commit)

- [ ] Every `msg_type`, field name, and error code appears in the blueprint verbatim.
- [ ] Length header uses `"!I"`; max frame constant is 4096; zero length rejected.
- [ ] Every `recv`/`recv_exact` path handles EOF (`b""` / `None`).
- [ ] The named socket exceptions are caught and converted to `CLIENT_DISCONNECTED`.
- [ ] Errors do not pass the turn; out-of-turn is checked before column validity.
- [ ] `self.state` is only assigned in `transition()`.
- [ ] I can explain every line (I do not commit code I cannot explain).
- [ ] The acceptance tests for that prompt pass and the test file is committed.

## 6. Sprint 2 Update: Concurrency-Constrained Prompting

**Governing documents for Sprint 2:** `concurrency_design.md`, `sequence_diagram.md` (in addition to the Sprint 1 documents).

Concurrency bugs are easy for AI tools to introduce and hard to spot, because the code often looks reasonable and passes a single-client test. The Sprint 1 master prompt (Section 2) still applies unchanged. Sprint 2 **adds** the rules below so that AI-generated server code follows the locking design exactly.

### 6,1 Common AI Concurrency Mistakes This Update Prevents

| Typical AI output                                                       | Why it breaks this design                                                    |
| :---------------------------------------------------------------------- | :--------------------------------------------------------------------------- |
| Spawns a thread per client but shares game state with no lock           | Race conditions R1-R9 in `concurrency_design.md` Section 6                   |
| Adds several locks (`board_lock`, `socket_lock`, `score_lock`)          | Introduces lock-ordering and deadlock risk; the design uses exactly one      |
| Calls `recv()` while holding the lock                                   | One silent client freezes the other player                                   |
| Uses `time.sleep()` loops or `Condition` variables to wait for Player 2 | The design is event-driven; no thread waits for another                      |
| Checks whose turn it is, releases the lock, then applies the move       | Breaks the atomic check-then-act (Section 5.4)                               |
| Updates scores in several places                                        | Double-counting; scoring must happen once, inside `finish_game_locked()`     |
| Calls `sock.close()` on the opponent's socket from another thread       | May not wake the blocked `recv()`; only `shutdown()` is allowed cross-thread |

### 6.2 Master Prompt Extension

```text
=== CONCURRENCY MODEL (Sprint 2, FIXED - do not redesign) ===
- Model: multi-threading. The main thread runs ONLY an accept() loop and spawns one daemon
  handler thread per accepted connection. There is no game-loop thread, timer thread, or pool.
- Exactly ONE lock exists: STATE_LOCK = threading.Lock(). Do NOT create any other lock, RLock,
  Condition, Event, Semaphore, or Queue. Do NOT poll shared state or call time.sleep() to wait.
- STATE_LOCK protects: server.lobby, server.session, every GameSession field (board, active,
  move_number, state, players), server.scoreboard, AND every write to a registered client socket.
- Always use `with STATE_LOCK:`. Never call acquire()/release() manually. Never nest acquisition.
  Functions whose names end in _locked assume the lock is held and must NEVER acquire it.
  Only entry points (register_player, dispatch, handle_move, on_client_disconnected) acquire it.
- Never call recv(), recv_exact(), or read_message() while holding STATE_LOCK.
  Framing, UTF-8/JSON parsing, schema validation, and msg_type checks run BEFORE acquiring.
- Inside the lock, validate in this exact order and stop at the first failure:
  session exists and state is PLAYER_TURN (WRONG_STATE), player_id matches the socket's assigned
  id (ID_MISMATCH), session.active == player_id (OUT_OF_TURN), 0 <= col <= 6 (INVALID_COLUMN),
  column has space (COLUMN_FULL). Validate first, then commit. Error paths NEVER mutate state and
  NEVER pass the turn.
- The board mutation, move_number increment, win/draw evaluation, and turn swap happen in the SAME
  critical section.
- finish_game_locked() must be idempotent (return immediately if session.is_over()), update the
  scoreboard exactly once, build ONE GAME_OVER message, send it to each recipient in its own
  try/except, call shutdown(SHUT_RDWR) on both sockets, and set server.session = None.
- A NEW GameSession object is created for every match. Threads keep a reference to their own session.
- Only the owning thread may call recv(), settimeout(), or close() on its socket. Other threads may
  only send (while holding STATE_LOCK) or call shutdown().
- Broadcast: build the message once, call encode_frame once, send the identical bytes to Player_1 and
  then Player_2 inside one critical section. A failed send means that player disconnected (forfeit).
- Scoreboard is a dict keyed by alias: {"wins": int, "losses": int, "draws": int}. GAME_OVER.scores =
  {"Player_1": wins of Player_1's alias, "Player_2": wins of Player_2's alias}. STATE_UPDATE carries
  NO scores.
- Do not add message types, error codes, states, payload fields, or features.
- Standard library only; Python 3.9 compatible.
```

### 6.3 Task Prompts 

Prompt P5 from Section 3 still defines socket options and timeouts; prompts P6-P9 below refine the threaded server and replace P5's loop structure.

#### Prompt P6 - Accept Loop and Client Thread (`server.py`)

```text
Using the C4P v1 and Concurrency specifications above, write accept_loop(listener) and
client_thread(conn). accept_loop only calls accept() and starts a daemon Thread running
client_thread; if threading.active_count() > MAX_CONNECTIONS (8) it closes the new socket instead.
client_thread: settimeout(10) and read CONNECT (no lock), call register_player(), then loop
read_message -> dispatch. Catch Disconnected, ConnectionResetError, BrokenPipeError,
ConnectionAbortedError, TimeoutError and call on_client_disconnected(player). Close the socket
in a finally block. Do not touch STATE_LOCK in accept_loop. Return only the requested functions.
```

**Acceptance tests:** two clients connecting within the same millisecond both get a response; a client that connects and never sends `CONNECT` does not delay a second client; `grep` shows `accept_loop` never references game state.

#### Prompt P7 - Matchmaking and Disconnect (`register_player`, `on_client_disconnected`)

```text
Using the specifications above, write register_player(conn, alias) and
on_client_disconnected(player). register_player acquires STATE_LOCK and: if server.session is not
None, sends fatal ERROR GAME_FULL and returns None; if server.lobby is None, registers Player_1 and
sends LOBBY_WAIT; otherwise creates a NEW GameSession, registers Player_2, sends the personalised
GAME_START to both, and transitions to PLAYER_TURN. on_client_disconnected acquires STATE_LOCK,
removes a lobby player, or (if the session is not over) transitions to FORFEIT_HANDLING and calls
finish_game_locked(session, "FORFEIT", survivor). All sends go through send_locked(). Do not
acquire the lock inside any *_locked helper.
```

**Acceptance tests:** simultaneous `CONNECT`s yield exactly one `Player_1` and one `Player_2`; a third client gets `GAME_FULL`; calling `on_client_disconnected` twice for the same player has no second effect.

#### Prompt P8 - Move Handling and Broadcast (`handle_move`, `broadcast_locked`)

```text
Using the specifications above, write handle_move(player, msg) and broadcast_locked(session, msg).
handle_move acquires STATE_LOCK once, runs the ordered validations (WRONG_STATE, ID_MISMATCH,
OUT_OF_TURN, INVALID_COLUMN, COLUMN_FULL), then commits: drop disc, move_number += 1, evaluate; if no
result swap active and broadcast STATE_UPDATE, otherwise call finish_game_locked(). broadcast_locked
encodes the frame ONCE and sends the same bytes to Player_1 then Player_2, each in its own
try/except; if any send failed, call finish_game_locked(session, "FORFEIT", survivor) after the loop.
Error paths must not mutate state or swap the turn. [Paste concurrency_design.md Section 7.2 here]
```

**Acceptance tests:** out-of-turn `MOVE` leaves board, `move_number`, and `active` unchanged; two clients each send a `MOVE` simultaneously 1000 times (with `sys.setswitchinterval(1e-6)`) and the disc count always equals `move_number`; both clients receive byte-identical `STATE_UPDATE` frames.

#### Prompt P9 - Game Completion and Scoring (`finish_game_locked`, `record_result_locked`)

```text
Using the specifications above, write finish_game_locked(session, result, winner) and
record_result_locked(session, result, winner). finish_game_locked returns immediately if
session.is_over(). Otherwise it transitions to GAME_OVER, calls record_result_locked, builds ONE
GAME_OVER message (scores = wins of each seat's alias from server.scoreboard), sends it to each
still-connected player in its own try/except, transitions to CLEANUP, calls shutdown(SHUT_RDWR) on
both sockets inside try/except OSError, and sets server.session = None. record_result_locked updates
the scoreboard: WIN or FORFEIT gives the winner wins += 1 and the other losses += 1; DRAW gives both
draws += 1. Do not close sockets in this function.
```

**Acceptance tests:** calling `finish_game_locked` twice changes the scoreboard only once; a forfeit sends `GAME_OVER` only to the survivor; scores accumulate across two consecutive games with the same aliases; both clients receive identical `scores`.

### 6.4 Concurrency Rejection Rules

I reject (and re-prompt) generated code containing any of the following:

| Red flag                                                                                | Violated rule                                                      |
| :-------------------------------------------------------------------------------------- | :----------------------------------------------------------------- |
| `recv`, `recv_exact`, or `read_message` inside a `with STATE_LOCK:` block               | Never block on a read while holding the lock                       |
| More than one `threading.Lock()`, or any `RLock`, `Condition`, `Event`, `Queue`         | Exactly one lock; event-driven design                              |
| `STATE_LOCK.acquire()` without `with`                                                   | Lock must be released automatically on exceptions                  |
| A `*_locked` function that contains `with STATE_LOCK:`                                  | Self-deadlock (the lock is not reentrant)                          |
| `while ...: time.sleep(...)` waiting for a player or a turn                             | No polling                                                         |
| Mutable game state stored in module-level globals outside `ServerState` / `GameSession` | All shared state must be in the documented, lock-protected objects |
| `sendall()` to a registered client outside `STATE_LOCK`                                 | Concurrent writes can interleave frame bytes                       |
| `sock.close()` on another player's socket                                               | Only the owner closes; others call `shutdown()`                    |
| Turn swap before validation completes, or inside an error path                          | Errors never pass the turn                                         |
| Scoreboard updates anywhere except `record_result_locked()`                             | Exactly-once scoring                                               |
| Bare `except:` or `except Exception: pass`                                              | Hides disconnects and bugs                                         |
| Non-daemon handler threads                                                              | A stuck thread would prevent server exit                           |

**Corrective re-prompt template (concurrency):**

```text
Your last output calls read_message() inside `with STATE_LOCK:`. That violates the specification:
reads must never happen while holding the lock, because a silent client would block the other player.
Regenerate only handle_move so that framing, JSON parsing, and schema validation happen before the
lock is acquired, and only the state-dependent checks and the commit happen inside it. Do not add
any other lock and do not change any other function.
```

### 6.5 Concurrency Review Checklist (Add to Section 5 for Every Sprint 2 File)

- [ ] `STATE_LOCK` is the only synchronization primitive in the file.
- [ ] Every acquisition uses `with`, and no `*_locked` function acquires the lock.
- [ ] No blocking read occurs inside a locked block.
- [ ] Every write to a registered client socket goes through `send_locked()`.
- [ ] The turn check and the board update are in one critical section; errors return before the commit point.
- [ ] `finish_game_locked()` is idempotent and is the only place scores change.
- [ ] Cross-thread teardown uses `shutdown()`, and only the owner calls `close()`.
- [ ] The race test (1000 simultaneous-move iterations with `sys.setswitchinterval(1e-6)`) passes.
- [ ] I can explain why each lock-protected line needs the lock.

---

## 7. Prompt Log (Fill In As I Work)

Record each real interaction here so the repository history demonstrates the constrained-generation loop. Commit this file after each AI session.

| Date                 | Tool / Model | Prompt used                                                                                                                                    | Result                                                                          | Violations found                                                                   | Corrective action                           | Commit      |
| :------------------- | :----------- | :--------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------ | :--------------------------------------------------------------------------------- | :------------------------------------------ | :---------- |
| 2026-10-14 | Claude       | Sprint 1 documents: asked Claude to draft `protocol_blueprint.md`, `fsm_specification.md`, and this file from the assignment text              | Drafted; I reviewed and committed                                               | Wire-example lengths and Mermaid layout were checked by running tools, not trusted | Mermaid labels shortened after render check | _short SHA_ |
| 2026-10-17 and 2026-10-18           | Claude       | Sprint 2 documents: asked Claude to draft `concurrency_design.md` and `sequence_diagram.md` from the assignment text and my Sprint 1 documents | Drafted; all 5 Mermaid diagrams rendered successfully; I reviewed and committed | Actor labels with underscores rendered incorrectly                                 | Removed underscores from actor labels       | _short SHA_ |
|                      |              |                                                                                                                                                |                                                                                 |                                                                                    |                                             |             |
