# AI Prompting & Constraint Strategy

- **Student:** Edgar Flores
- **Course:** CS 457
- **Sprint:** 1
- **Governing documents:** `protocol_blueprint.md`, `fsm_specification.md`

## Contents

- [1. Strategy Overview](#1-strategy-overview)
- [2. Master System Prompt (Paste at the Start of Every AI Session)](#2-master-system-prompt-paste-at-the-start-of-every-ai-session)
- [3. Task Prompts (Each Used After the Master Prompt)](#3-task-prompts-each-used-after-the-master-prompt)
- [4. Anti-Boilerplate Rejection Rules](#4-anti-boilerplate-rejection-rules)
- [5. Human Review Checklist (Applied to Every Generated File Before Commit)](#5-human-review-checklist-applied-to-every-generated-file-before-commit)
- [6. Prompt Log (Fill In As You Work)](#6-prompt-log-fill-in-as-you-work)

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

---

## 6. Prompt Log (Fill In As You Work)

Record each real interaction here so the repository history demonstrates the constrained-generation loop. Commit this file after each AI session.

| Date         | Tool / Model  | Prompt used  | Result                  | Violations found       | Corrective action              | Commit      |
| :----------- | :------------ | :----------- | :---------------------- | :--------------------- | :----------------------------- | :---------- |
| _2026-10-14_ | _e.g. Claude_ | _P1 framing_ | _pass / partial / fail_ | _e.g. used recv(1024)_ | _re-prompt text or manual fix_ | _short SHA_ |
|              |               |              |                         |                        |                                |             |