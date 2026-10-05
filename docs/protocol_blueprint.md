# Application Protocol Blueprint - Connect Four over TCP

- **Student:** Edgar Flores
- **Course:** CS 457
- **Sprint:** 1
- **Protocol:** C4P (Connect Four Protocol), version `1`
- **Target domain:** `server.flores.edu`
- **Language:** Python 3.9

This document is the single source of truth for every byte exchanged between the Connect Four client and server. All code (and all AI-generated code, see `ai_prompts.md`) must conform to it. The server-side state machine is specified in `fsm_specification.md`.

Keywords **MUST**, **MUST NOT**, and **MAY** are used in the RFC 2119 sense.

## Contents

- [1. Transport Layer](#1-transport-layer)
- [2. Framing Rule (Length-Prefix, Network Byte Order)](#2-framing-rule-length-prefix-network-byte-order)
- [3. Message Envelope (Common to All Messages)](#3-message-envelope-common-to-all-messages)
- [4. Message Types](#4-message-types)
- [5. Message Specifications](#5-message-specifications)
- [6. Error Codes](#6-error-codes)
- [7. Sample Session Trace](#7-sample-session-trace)
- [8. Connection Termination & Socket Lifecycle](#8-connection-termination--socket-lifecycle)
- [9. Protocol Constants Summary](#9-protocol-constants-summary)

---

## 1. Transport Layer

| Property       | Value                                              |
| :------------- | :------------------------------------------------- |
| Transport      | TCP (`SOCK_STREAM`), IPv4                          |
| Server address | Resolved from `server.flores.edu`                  |
| Server port    | `5457` (configurable via CLI flag, default `5457`) |
| Serialization  | JSON, UTF-8 encoded                                |
| Framing        | **Length-prefixed (4-byte big-endian header)**     |
| Topology       | One server, exactly 2 players per game             |

---

## 2. Framing Rule (Length-Prefix, Network Byte Order)

TCP is a byte stream with no message boundaries. Messages sent back-to-back can be **coalesced** into one `recv()` chunk, and a single message can be **fragmented** across several chunks. C4P uses a deterministic length-prefix rule so the receiver never has to guess.

### 2.1 Frame Layout

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+---------------------------------------------------------------+
|                 Length N (uint32, big-endian)                 |
+---------------------------------------------------------------+
|              Payload: N bytes of UTF-8 encoded JSON           |
|                              ...                              |
+---------------------------------------------------------------+
```

| Field    | Size           | Type                                                     | Description                                                                                   |
| :------- | :------------- | :------------------------------------------------------- | :-------------------------------------------------------------------------------------------- |
| `length` | 4 bytes        | unsigned 32-bit int, **Network Byte Order (big-endian)** | Number of bytes in the body that follows. Does **not** include the 4 header bytes themselves. |
| `body`   | `length` bytes | UTF-8 JSON text                                          | Exactly one JSON object (the message envelope, Section 3).                                    |

### 2.2 Rules

1. The sender MUST encode with `struct.pack("!I", len(body))`. The `!` flag forces big-endian regardless of whether the host is x86 (little-endian) or ARM.
2. The receiver MUST read **exactly 4 bytes** (looping on `recv()` until 4 bytes are collected), unpack with `struct.unpack("!I", ...)`, then read **exactly N bytes**. A single `recv()` call is never assumed to return a whole header or a whole message.
3. `length` MUST satisfy `1 <= length <= 4096` (`MAX_FRAME_BYTES = 4096`). The largest legitimate message in this protocol is under 300 bytes, so 4096 leaves ample headroom while preventing a bad or malicious peer from forcing a huge allocation.
4. If `length == 0` or `length > 4096`, the stream can no longer be trusted to be in sync. This is a **fatal framing error**: the receiver sends `ERROR` (`FRAME_EMPTY` / `FRAME_TOO_LARGE`, `fatal: true`) if it still can, then closes the connection (Section 8).
5. Exactly one JSON object per frame. Whitespace inside the JSON is permitted (the length prefix makes it harmless), but senders SHOULD use compact encoding: `json.dumps(msg, separators=(",", ":"))`.
6. Frame-level timeouts: after the 4-byte header is read, the full body MUST arrive within **10 seconds** (`BODY_TIMEOUT_S = 10`) or the connection is treated as dead (`FRAME_TIMEOUT`, fatal). This defends against a peer that sends a header and then stalls.
7. A clean EOF (`recv()` returns `b""`) **in the middle of a frame** (after the header started, before the body finished) is an abrupt termination, never a message.

### 2.3 Why Length-Prefixing (Design Decision)

| Concern                                            | Newline framing         | **Length-prefix (chosen)**                   |
| :------------------------------------------------- | :---------------------- | :------------------------------------------- |
| Delimiter collision (a `\n` inside a string value) | Must escape / forbid    | Not possible, body is opaque bytes           |
| Receiver buffer sizing                             | Unknown until `\n` seen | Known up front (`N`)                         |
| Oversized-message defense                          | Must cap while scanning | One integer comparison before reading body   |
| Human debuggability with `nc`                      | Easy                    | Harder (mitigated by a `--debug` hex logger) |
| Demonstrates network byte order                    | No                      | Yes (`!I`)                                   |

### 2.4 Reference Framing Code (Python)

```python
import json
import struct

MAX_FRAME_BYTES = 4096

def encode_frame(message: dict) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    return struct.pack("!I", len(body)) + body

def recv_exact(sock, n_bytes: int):
    """Return exactly n_bytes, or None if the peer closed (EOF) first."""
    buf = bytearray()
    while len(buf) < n_bytes:
        chunk = sock.recv(n_bytes - len(buf))
        if not chunk:          # b"" == EOF (TCP FIN received)
            return None
        buf.extend(chunk)
    return bytes(buf)
```

### 2.5 Wire Stream Examples (Exact Bytes)

All lengths below were computed from the actual compact-JSON bytes, not estimated.

**Example A - Server to Player 1, three frames back-to-back on one continuous stream**

Each `[hex]` is the 4-byte header; the JSON follows immediately with no separator.

```text
[00 00 00 7A]{"msg_type":"LOBBY_WAIT","player_id":"SERVER","payload":{"players_connected":1,"players_needed":2},"timestamp":1791100001}
[00 00 00 E4]{"msg_type":"GAME_START","player_id":"SERVER","payload":{"you":"Player_1","symbol":"X","opponent_alias":"Bob","first_turn":"Player_1","board":[".......",".......",".......",".......",".......","......."]},"timestamp":1791100030}
[00 00 00 F5]{"msg_type":"STATE_UPDATE","player_id":"SERVER","payload":{"board":[".......",".......",".......",".......",".......","...X..."],"last_move":{"player_id":"Player_1","row":5,"col":3},"next_turn":"Player_2","move_number":1},"timestamp":1791100036}
```

(Line breaks above are for readability only. On the wire the three frames are one unbroken run of 607 bytes: `4+122 + 4+228 + 4+245`.)

| Frame          | Header (hex)  | Body length (decimal) |
| :------------- | :------------ | :-------------------- |
| `LOBBY_WAIT`   | `00 00 00 7A` | 122                   |
| `GAME_START`   | `00 00 00 E4` | 228                   |
| `STATE_UPDATE` | `00 00 00 F5` | 245                   |

**Example B - Coalescing.** All 607 bytes of Example A arrive in a single `recv(4096)` chunk. The receiver parses the first header (`0x7A` = 122), extracts 122 bytes, then sees the next header immediately after, and so on. It MUST loop until its buffer holds no complete frame.

**Example C - Fragmentation.** The 249-byte `STATE_UPDATE` frame (4 + 245) arrives in three chunks:

```text
chunk 1:  00 00                      (2 bytes - header split in half)
chunk 2:  00 F5 7B 22 6D ... (100 bytes: rest of header + first 98 body bytes)
chunk 3:  ... (147 bytes: the remaining body)
```

`recv_exact(sock, 4)` loops until it has `00 00 00 F5`, then `recv_exact(sock, 245)` loops until the whole body is present. No byte is ever lost or misattributed.

**Example D - Client to Server, a `MOVE` frame**

```text
[00 00 00 55]{"msg_type":"MOVE","player_id":"Player_1","payload":{"col":3},"timestamp":1791100035}
```

`0x55` = 85 body bytes.

---

## 3. Message Envelope (Common to All Messages)

Every frame body is one JSON object with exactly these four top-level keys:

```json
{
  "msg_type": "MOVE",
  "player_id": "Player_1",
  "payload": { "col": 3 },
  "timestamp": 1791100035
}
```

| Key         | JSON Type | Required | Rules                                                                                                                                                                        |
| :---------- | :-------- | :------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `msg_type`  | string    | Yes      | One of the 8 values in Section 4. Case-sensitive, upper-case.                                                                                                                |
| `player_id` | string    | Yes      | **Client to Server:** `"UNASSIGNED"` in `CONNECT`; afterwards MUST equal the id the server assigned (`"Player_1"` or `"Player_2"`). **Server to Client:** always `"SERVER"`. |
| `payload`   | object    | Yes      | Message-specific fields (Sections 5.x). Use `{}` when a message has no fields.                                                                                               |
| `timestamp` | integer   | Yes      | Unix epoch **seconds** at send time. Informational and for logging; the server MUST NOT reject messages for clock skew.                                                      |

**General validation rules**

- The body MUST decode as valid UTF-8 and parse as a JSON **object** (not an array, string, or number).
- Unknown extra keys in the envelope or payload are **ignored** (forward compatibility). Missing or wrong-typed required keys are a `SCHEMA_VIOLATION`.
- In Python, `bool` is a subclass of `int`. Integer fields MUST be validated with `type(x) is int` (or `isinstance(x, int) and not isinstance(x, bool)`) so that `true` is not accepted as `1`.

---

## 4. Message Types

| #   | `msg_type`     | Direction              | Purpose                                                      |
| :-- | :------------- | :--------------------- | :----------------------------------------------------------- |
| 1   | `CONNECT`      | Client -> Server       | Client requests to join the game with a player alias.        |
| 2   | `LOBBY_WAIT`   | Server -> Client       | Server tells the first player it is waiting for an opponent. |
| 3   | `GAME_START`   | Server -> Both Clients | Game begins; roles and symbols assigned.                     |
| 4   | `MOVE`         | Client -> Server       | Active player drops a disc in a column.                      |
| 5   | `STATE_UPDATE` | Server -> Both Clients | Updated board and whose turn is next.                        |
| 6   | `ERROR`        | Server -> Client       | Out-of-turn move, invalid column, malformed message, etc.    |
| 7   | `DISCONNECT`   | Client -> Server       | Client announces intentional departure (quit / forfeit).     |
| 8   | `GAME_OVER`    | Server -> Both Clients | Final outcome: `WIN`, `DRAW`, or `FORFEIT`.                  |

---

## 5. Message Specifications

### Board and Coordinate Conventions (used by several messages)

- The grid is **6 rows x 7 columns** (42 cells), matching the SOW.
- **Wire columns are zero-indexed `0`-`6`.** The CLI shows players columns `1`-`7` (as in the SOW); the client converts with `col = user_input - 1` before sending. The server only ever sees `0`-`6`.
- **Rows are zero-indexed `0`-`5`, with row `0` at the TOP and row `5` at the BOTTOM.** A disc dropped in a column occupies the lowest empty row (gravity). The **server** computes the row; the client never sends it.
- `board` is an array of **6 strings** (row 0 first), each exactly **7 characters**: `"X"` (Player 1), `"O"` (Player 2), or `"."` (empty).

### 5.1 `CONNECT` (Client -> Server)

Sent once, immediately after the TCP connection is established. The server MUST receive it within **10 seconds** (`HANDSHAKE_TIMEOUT_S = 10`) or close the connection.

| Payload Field      | Type    | Required | Constraints                    |
| :----------------- | :------ | :------- | :----------------------------- |
| `alias`            | string  | Yes      | Matches `^[A-Za-z0-9_]{1,16}$` |
| `protocol_version` | integer | Yes      | Must equal `1`                 |

```json
{
  "msg_type": "CONNECT",
  "player_id": "UNASSIGNED",
  "payload": { "alias": "Alice", "protocol_version": 1 },
  "timestamp": 1791100000
}
```

Wire: `[00 00 00 77]` + the 119-byte JSON above.

### 5.2 `LOBBY_WAIT` (Server -> Client)

Sent only to the first player after a valid `CONNECT` while the server waits for a second player.

| Payload Field       | Type    | Description   |
| :------------------ | :------ | :------------ |
| `players_connected` | integer | Currently `1` |
| `players_needed`    | integer | Always `2`    |

```json
{
  "msg_type": "LOBBY_WAIT",
  "player_id": "SERVER",
  "payload": { "players_connected": 1, "players_needed": 2 },
  "timestamp": 1791100001
}
```

Wire: `[00 00 00 7A]` + 122 bytes.

### 5.3 `GAME_START` (Server -> Both Clients)

Sent once to each client when the second player connects. The payload is personalised per recipient (`you`, `symbol`, `opponent_alias` differ).

| Payload Field    | Type               | Description                                                                            |
| :--------------- | :----------------- | :------------------------------------------------------------------------------------- |
| `you`            | string             | Recipient's assigned id: `"Player_1"` or `"Player_2"` (first to connect is `Player_1`) |
| `symbol`         | string             | `"X"` for `Player_1`, `"O"` for `Player_2`                                             |
| `opponent_alias` | string             | Other player's alias                                                                   |
| `first_turn`     | string             | Always `"Player_1"` (per SOW)                                                          |
| `board`          | array of 6 strings | Empty board (all `"."`)                                                                |

```json
{
  "msg_type": "GAME_START",
  "player_id": "SERVER",
  "payload": {
    "you": "Player_1",
    "symbol": "X",
    "opponent_alias": "Bob",
    "first_turn": "Player_1",
    "board": [".......", ".......", ".......", ".......", ".......", "......."]
  },
  "timestamp": 1791100030
}
```

Wire: `[00 00 00 E4]` + 228 bytes.

### 5.4 `MOVE` (Client -> Server)

Only legal while the sender is the active player. **Connect Four needs only a column**, because gravity determines the row. (The generic template in the assignment shows `row` and `col`; sending `row` here would be redundant and is a spoofing surface, so it is intentionally omitted.)

| Payload Field | Type    | Required | Constraints                                     |
| :------------ | :------ | :------- | :---------------------------------------------- |
| `col`         | integer | Yes      | `0 <= col <= 6`; target column must not be full |

```json
{
  "msg_type": "MOVE",
  "player_id": "Player_1",
  "payload": { "col": 3 },
  "timestamp": 1791100035
}
```

Wire: `[00 00 00 55]` + 85 bytes.

### 5.5 `STATE_UPDATE` (Server -> Both Clients)

Broadcast after every **valid, non-terminal** move.

| Payload Field | Type               | Description                                                                  |
| :------------ | :----------------- | :--------------------------------------------------------------------------- |
| `board`       | array of 6 strings | Full board after the move                                                    |
| `last_move`   | object             | `{"player_id": string, "row": int, "col": int}` - where the last disc landed |
| `next_turn`   | string             | `"Player_1"` or `"Player_2"` - who must move next                            |
| `move_number` | integer            | Count of valid moves so far (starts at `1`)                                  |

```json
{
  "msg_type": "STATE_UPDATE",
  "player_id": "SERVER",
  "payload": {
    "board": [".......", ".......", ".......", ".......", ".......", "...X..."],
    "last_move": { "player_id": "Player_1", "row": 5, "col": 3 },
    "next_turn": "Player_2",
    "move_number": 1
  },
  "timestamp": 1791100036
}
```

Wire: `[00 00 00 F5]` + 245 bytes.

### 5.6 `ERROR` (Server -> Client)

Sent only to the offending client. A **non-fatal** error does not change game state and, if it was the active player's move, does **not** pass the turn. A **fatal** error is followed by the server closing that connection.

| Payload Field | Type    | Description                                                          |
| :------------ | :------ | :------------------------------------------------------------------- |
| `code`        | string  | One of the codes in Section 6                                        |
| `message`     | string  | Human-readable explanation (display only; clients MUST NOT parse it) |
| `fatal`       | boolean | `true` if the server will close the connection after sending         |

```json
{
  "msg_type": "ERROR",
  "player_id": "SERVER",
  "payload": {
    "code": "OUT_OF_TURN",
    "message": "It is not your turn.",
    "fatal": false
  },
  "timestamp": 1791100040
}
```

Wire: `[00 00 00 90]` + 144 bytes.

### 5.7 `DISCONNECT` (Client -> Server)

Graceful application-layer departure. After sending it, the client calls `sock.shutdown(socket.SHUT_WR)` then `sock.close()`. Legal in any state after the TCP connection is open.

| Payload Field | Type   | Required | Constraints                           |
| :------------ | :----- | :------- | :------------------------------------ |
| `reason`      | string | Yes      | `"QUIT"` (v1 defines only this value) |

```json
{
  "msg_type": "DISCONNECT",
  "player_id": "Player_2",
  "payload": { "reason": "QUIT" },
  "timestamp": 1791100090
}
```

Wire: `[00 00 00 63]` + 99 bytes.

**Server effect:** during a game, the departing player **forfeits** and the opponent is declared winner (Section 8). In the lobby, the player is simply removed.

### 5.8 `GAME_OVER` (Server -> Both Clients)

Sent once when the game ends by `WIN`, `DRAW`, or `FORFEIT`. It carries the final board, so no separate `STATE_UPDATE` is sent for the terminal move.

| Payload Field   | Type                  | Description                                                        |
| :-------------- | :-------------------- | :----------------------------------------------------------------- |
| `result`        | string                | `"WIN"`, `"DRAW"`, or `"FORFEIT"`                                  |
| `winner`        | string or null        | `"Player_1"` / `"Player_2"`; `null` when `result` is `"DRAW"`      |
| `reason`        | string                | `"FOUR_IN_A_ROW"`, `"BOARD_FULL"`, or `"OPPONENT_DISCONNECTED"`    |
| `winning_cells` | array of `[row, col]` | The 4 winning cells for `WIN`; `[]` otherwise                      |
| `board`         | array of 6 strings    | Final board                                                        |
| `scores`        | object                | `{"Player_1": int, "Player_2": int}` - wins in this server session |

**Win example**

```json
{
  "msg_type": "GAME_OVER",
  "player_id": "SERVER",
  "payload": {
    "result": "WIN",
    "winner": "Player_1",
    "reason": "FOUR_IN_A_ROW",
    "winning_cells": [
      [5, 0],
      [5, 1],
      [5, 2],
      [5, 3]
    ],
    "board": [".......", ".......", ".......", ".......", "..OO...", "XXXXO.."],
    "scores": { "Player_1": 1, "Player_2": 0 }
  },
  "timestamp": 1791100100
}
```

Wire: `[00 00 01 21]` + 289 bytes.

**Forfeit example** (Player 2 disconnected; sent only to the surviving Player 1)

```json
{
  "msg_type": "GAME_OVER",
  "player_id": "SERVER",
  "payload": {
    "result": "FORFEIT",
    "winner": "Player_1",
    "reason": "OPPONENT_DISCONNECTED",
    "winning_cells": [],
    "board": [".......", ".......", ".......", ".......", ".......", "...X..."],
    "scores": { "Player_1": 1, "Player_2": 0 }
  },
  "timestamp": 1791100091
}
```

Wire: `[00 00 01 16]` + 278 bytes.

**Draw:** `result:"DRAW"`, `winner:null`, `reason:"BOARD_FULL"`, `winning_cells:[]`.

---

## 6. Error Codes

| `code`                | Fatal | Trigger                                                                                                   |
| :-------------------- | :---- | :-------------------------------------------------------------------------------------------------------- |
| `MALFORMED_JSON`      | No    | Body is not valid UTF-8, not valid JSON, or not a JSON object                                             |
| `SCHEMA_VIOLATION`    | No    | Missing envelope/payload key, wrong type, or field fails its constraint (e.g. bad alias)                  |
| `UNKNOWN_MSG_TYPE`    | No    | `msg_type` is not one of the 8 defined values, or is a server-only type sent by a client                  |
| `WRONG_STATE`         | No    | Valid message type but illegal in the server's current state (e.g. `MOVE` in the lobby, second `CONNECT`) |
| `ID_MISMATCH`         | No    | `player_id` does not match the id assigned to this socket                                                 |
| `OUT_OF_TURN`         | No    | `MOVE` from the player who is not the active player                                                       |
| `INVALID_COLUMN`      | No    | `col` outside `0`-`6`                                                                                     |
| `COLUMN_FULL`         | No    | Target column has no empty cell                                                                           |
| `VERSION_UNSUPPORTED` | Yes   | `protocol_version` in `CONNECT` is not `1`                                                                |
| `GAME_FULL`           | Yes   | A third client connects while a game is in progress                                                       |
| `FRAME_EMPTY`         | Yes   | Length header is `0`                                                                                      |
| `FRAME_TOO_LARGE`     | Yes   | Length header exceeds 4096                                                                                |
| `FRAME_TIMEOUT`       | Yes   | Body did not fully arrive within 10 s of its header                                                       |
| `HANDSHAKE_TIMEOUT`   | Yes   | No valid `CONNECT` within 10 s of accept                                                                  |

**Why some are fatal:** errors inside a well-framed message (bad JSON, bad schema, bad column) leave the byte stream in sync, so the server can reject that one message and carry on. Errors in the framing itself (zero/oversized length, stalled body) mean the receiver can no longer tell where the next message begins, so the only safe recovery is to close.

---

## 7. Sample Session Trace

| Step | Direction       | Message                  | Notes                                             |
| :--- | :-------------- | :----------------------- | :------------------------------------------------ |
| 1    | Alice -> S      | `CONNECT` alias=Alice    | Alice becomes `Player_1` (`X`)                    |
| 2    | S -> Alice      | `LOBBY_WAIT`             | 1 of 2 connected                                  |
| 3    | Bob -> S        | `CONNECT` alias=Bob      | Bob becomes `Player_2` (`O`)                      |
| 4    | S -> Alice, Bob | `GAME_START`             | Personalised to each; `first_turn` = Player_1     |
| 5    | Bob -> S        | `MOVE` col=2             | Bob is not the active player                      |
| 6    | S -> Bob        | `ERROR` `OUT_OF_TURN`    | Non-fatal; turn unchanged                         |
| 7    | Alice -> S      | `MOVE` col=3             | Valid                                             |
| 8    | S -> Alice, Bob | `STATE_UPDATE`           | `next_turn` = Player_2                            |
| 9    | Bob -> S        | `MOVE` col=9             | Out of range                                      |
| 10   | S -> Bob        | `ERROR` `INVALID_COLUMN` | Non-fatal; Bob must choose again, turn not passed |
| ...  | ...             | ...                      | Play continues                                    |
| N    | S -> Alice, Bob | `GAME_OVER` (`WIN`)      | Server then closes both sockets                   |

---

## 8. Connection Termination & Socket Lifecycle

### 8.1 Graceful vs. Abrupt Termination

| Kind                          | What the peer does                                       | What the other side observes                                                                       | Layer                 |
| :---------------------------- | :------------------------------------------------------- | :------------------------------------------------------------------------------------------------- | :-------------------- |
| **Graceful (application)**    | Sends `DISCONNECT`, then `shutdown(SHUT_WR)` + `close()` | A valid `DISCONNECT` frame, followed by EOF                                                        | Application + TCP FIN |
| **Graceful (transport only)** | `close()` / normal process exit without `DISCONNECT`     | `recv()` returns `b""` (EOF) at a frame boundary                                                   | TCP FIN               |
| **Abrupt**                    | `kill -9`, power loss, CML link severed                  | `ConnectionResetError` (RST), `BrokenPipeError` on next send, `TimeoutError`, or EOF **mid-frame** | TCP RST / timeout     |

All three are handled by the **same** recovery path in the server: the player is treated as departed (Section 8.3). The distinction matters only for logging.

### 8.2 The EOF (0-Byte) Rule

`recv()` returning `b""` is end-of-file: the peer has closed its write half (TCP FIN). It does **not** raise an exception. Every receive loop MUST test for it; otherwise the loop spins at 100% CPU because `recv()` returns `b""` instantly forever. `recv_exact()` (Section 2.4) returns `None` on EOF, and callers MUST treat `None` as "peer disconnected".

### 8.3 Server Behavior Table

| Trigger                                                | Classification     | Server action                                              |
| :----------------------------------------------------- | :----------------- | :--------------------------------------------------------- |
| `DISCONNECT` frame received                            | Graceful           | Same as a departure below; log `reason`                    |
| `recv_exact()` returns `None` (EOF)                    | Graceful or abrupt | Treat as departure                                         |
| `ConnectionResetError`, `ConnectionAbortedError`       | Abrupt             | Treat as departure                                         |
| `BrokenPipeError` on `sendall()`                       | Abrupt             | Treat as departure of the **recipient**                    |
| `socket.timeout` / `TimeoutError` (header or body)     | Abrupt             | Send fatal `FRAME_TIMEOUT` if possible, treat as departure |
| Fatal framing error (`FRAME_EMPTY`, `FRAME_TOO_LARGE`) | Protocol violation | Send fatal `ERROR`, close, treat as departure              |

**"Treat as departure" means, by server state:**

| Server state when detected                               | Action                                                                                                                                                                                                                                                          |
| :------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `WAITING_FOR_PLAYERS`                                    | Remove that player from the lobby. If they were Player 1, the lobby is empty again. No `GAME_OVER` is sent.                                                                                                                                                     |
| In a game (`GAME_START`, `PLAYER_TURN`, `EVALUATE_MOVE`) | **Forfeit.** Opponent is declared winner. Send `GAME_OVER` (`result:"FORFEIT"`, `reason:"OPPONENT_DISCONNECTED"`) to the survivor, **wrapping that send in its own `try/except`** because the survivor may have dropped too. Then close both sockets and reset. |
| Both players gone                                        | No messages possible; skip `GAME_OVER`, close sockets, reset.                                                                                                                                                                                                   |

### 8.4 Reference Exception Handling Pattern

```python
try:
    header = recv_exact(sock, 4)
    if header is None:
        trigger_state_transition("CLIENT_DISCONNECTED", player_id)   # EOF
        return
    (length,) = struct.unpack("!I", header)
    if length == 0 or length > MAX_FRAME_BYTES:
        send_error_and_close(sock, "FRAME_TOO_LARGE" if length else "FRAME_EMPTY")
        trigger_state_transition("CLIENT_DISCONNECTED", player_id)
        return
    body = recv_exact(sock, length)
    if body is None:                                                  # EOF mid-frame
        trigger_state_transition("CLIENT_DISCONNECTED", player_id)
        return
except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError) as e:
    logger.warning("Connection lost abruptly: %s", e)
    trigger_state_transition("CLIENT_DISCONNECTED", player_id)
```

### 8.5 Orderly Close Sequence

1. **Client quit:** send `DISCONNECT` -> `shutdown(SHUT_WR)` -> drain until EOF (optional) -> `close()`.
2. **Server after game end:** send `GAME_OVER` to each client -> `shutdown(SHUT_WR)` -> `close()` -> reset game state (`CLEANUP`).
3. The server's listening socket uses `SO_REUSEADDR` and is never closed because of a single client failure.

---

## 9. Protocol Constants Summary

| Constant                  | Value                        |
| :------------------------ | :--------------------------- |
| `PROTOCOL_VERSION`        | `1`                          |
| `DEFAULT_PORT`            | `5457`                       |
| `HEADER_BYTES`            | `4` (`struct` format `"!I"`) |
| `MAX_FRAME_BYTES`         | `4096`                       |
| `HANDSHAKE_TIMEOUT_S`     | `10`                         |
| `BODY_TIMEOUT_S`          | `10`                         |
| `ROWS` x `COLS`           | `6` x `7`                    |
| `EMPTY / P1 / P2 symbols` | `"."` / `"X"` / `"O"`        |