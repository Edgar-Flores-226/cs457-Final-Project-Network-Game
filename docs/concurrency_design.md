# Concurrency Architecture & Game Behavior Design - Connect Four Server

**Student:** Edgar Flores
**Course:** CS 457 - Computer Networks and the Internet
**Sprint:** 2
**Companion documents:** `protocol_blueprint.md`, `fsm_specification.md`, `sequence_diagram.md`
**Chosen model:** Option A - multi-threading (`threading.Thread`, one handler thread per connection, one `threading.Lock`)

This document explains how the server handles two simultaneous clients without blocking, how shared game state is protected, how turns are enforced, how state updates and scores are broadcast, and why the design is free of race conditions and deadlocks. The Mermaid sequence diagrams that illustrate these flows are in `sequence_diagram.md`.

## Contents

- [1. Purpose and Relationship to Sprint 1](#1-purpose-and-relationship-to-sprint-1)
- [2. Concurrency Model Selection](#2-concurrency-model-selection)
- [3. Thread Architecture](#3-thread-architecture)
- [4. Shared State Inventory](#4-shared-state-inventory)
- [5. Locking Strategy](#5-licking-strategy)
- [6. Race Condition Analysis](#6-race-condition-analysis)
- [7. Turn Enforcement](#7-turn-enforcement)
- [8. Synchronized Broadcast Mechanism](#8-synchronized-broadcast-mechanism)
- [9. Score Storage and Broadcast (Assignment 2.3)](#9-score-storage-and-broadcast-assignment-23)
- [10. Cross-Thread Connection Teardown](#10-cross-thread-connection-teardown)
- [11. Implementation Skeleton (Contract)](#11-implementation-skeleton-contract)
- [12. Concurrency Test Plan](#12-concurrency-test-plan)
- [Appendix A. What Would Change With Option B (`selectors`)](#appendix-a-what-would-change-with-option-b-selectors)

## 1. Purpose and Relationship to Sprint 1

Sprint 1 defined **what** is sent (protocol) and **which states** exist (FSM). Sprint 2 defines **which thread runs each transition** and **under what lock**. Nothing in the Sprint 1 protocol or FSM changes.

| Sprint 1 artifact                    | Sprint 2 relationship                                                                               |
| :----------------------------------- | :-------------------------------------------------------------------------------------------------- |
| 8 message types, framing error codes | Unchanged. `MOVE` still carries `col` only; `GAME_OVER.scores` carries the score table (Section 9). |
| FSM states and transitions T1-T15    | Unchanged. Section 5.3 maps each transition to its thread and lock.                                 |
| `recv_exact()` blocking framing loop | Reused as-is. This is the main reason Option A was chosen (Section 2).                              |
| Validation pipeline (steps 1-10)     | Same order. Steps 1-4 run **outside** the lock; steps 5-10 run **inside** it (Section 7).           |

## 2. Concurrency Model Selection

### 2.1 Options Compared

| Criterion                              | Option A: Threads + `Lock`                          | Option B: `selectors` event loop                                              |
| :------------------------------------- | :-------------------------------------------------- | :---------------------------------------------------------------------------- |
| Blocking `recv_exact()` from Sprint 1  | Works unchanged inside each thread                  | Not usable. Needs an incremental frame parser and per-connection byte buffers |
| Per-connection code shape              | Straight-line loop: read, validate, dispatch        | Callback / state-per-connection bookkeeping                                   |
| Shared-state safety                    | Needs explicit locking (documented in Sections 4-6) | Single thread, so no locks, but a slow handler stalls everyone                |
| Failure isolation                      | An exception kills one thread only                  | An unhandled exception can stop the whole loop                                |
| Scale                                  | Hundreds of connections                             | Thousands (irrelevant for a 2-player game)                                    |
| Demonstrates explicit synchronization  | Yes                                                 | Via event-loop isolation instead                                              |

### 2.2 Decision: Option A (Multi-Threading)

1. **It reuses the Sprint 1 design directly.** The framing rule is "read exactly 4 bytes, then exactly N bytes" with a blocking loop. A thread can block inside `recv_exact()` safely; an event loop cannot.
2. **The workload is tiny and I/O-bound.** Exactly two players are active. Thread count stays under 10, and the Python GIL does not matter because threads spend their time waiting on sockets.
3. **Each thread is easy to reason about.** Every client thread runs the same loop: `read frame -> validate -> dispatch`.
4. **Faults are contained.** The top of every thread catches exceptions and converts them into the `CLIENT_DISCONNECTED` event, so one bad client cannot crash the server.
5. **It makes the synchronization design explicit.** The assignment asks for a lock strategy; Option A requires a rigorous one.

### 2.3 Trade-offs Accepted

| Trade-off                                                 | Mitigation                                                    |
| :-------------------------------------------------------- | :------------------------------------------------------------ |
| Shared state can race                                     | One coarse lock around all mutable state (Section 5)          |
| A `sendall()` under the lock could stall on a frozen peer | Bounded-output argument plus timeouts (Section 8.3)           |
| Threads do not scale to thousands of clients              | Not a requirement; a connection cap is enforced (Section 3.5) |

## 3. Thread Architecture

### 3.1 Thread Inventory

| Thread                    | Count                              | Created by                                | Runs                  | Blocks in                                           |
| :------------------------ | :--------------------------------- | :---------------------------------------- | :-------------------- | :-------------------------------------------------- |
| **Main / accept thread**  | 1                                  | `python server.py`                        | `accept_loop()`       | Only `listener.accept()`                            |
| **Client handler thread** | 1 per accepted connection (daemon) | Main thread, immediately after `accept()` | `client_thread(conn)` | `recv()` while waiting for that client's next frame |

There is **no game-loop thread, no timer thread, and no worker pool**. Game logic runs inside whichever client handler thread received the triggering message.

### 3.2 Why Two Simultaneous Clients Never Block the Server

- The main thread does exactly two things: `accept()` and `Thread(...).start()`. It never reads, writes, or touches game state, so a slow or silent client cannot delay the next `accept()`.
- The handshake (`CONNECT`, up to 10 s) is read **inside the new client's own thread**, not the main thread.
- Each client waits in its own thread. Player 1 waiting for a move does not prevent Player 2 from sending one; whichever thread receives a complete frame runs next.
- Threads never wait for each other except to take the single state lock for a few microseconds.

### 3.3 Handler Thread Lifecycle

```text
accept() -> spawn thread
  1. settimeout(10); read CONNECT frame          (no lock)
  2. register_player()                            (STATE_LOCK: lobby / match creation)
  3. loop:
       frame = read_message(conn)                 (blocking, NO lock held)
       dispatch(player, frame)                    (takes STATE_LOCK inside)
  4. on EOF / socket exception / fatal frame error:
       on_client_disconnected(player)             (STATE_LOCK: lobby removal or forfeit)
  5. finally: close(conn); thread exits
```

### 3.4 Event-Driven, Not Polling
 
No thread polls shared state or sleeps in a loop, and no `threading.Condition` or `Event` is needed. Every state change is triggered by something a thread directly observed: a received message, an EOF, or a socket exception. The thread that observes it takes the lock, performs the transition, and releases the lock. Player 1's thread waiting in the lobby simply stays in `recv()`; it never needs to be "woken up" when Player 2 arrives, because Player 2's thread sends `GAME_START` to both clients itself.

### 3.5 Limits

| Constant              | Value | Purpose                                                                                                    |
| :---------------      | :---- | :--------------------------------------------------------------------------------------------------------- |
| `LISTEN_BACKLOG`      | `5`   | Pending-connection queue length                                                                            |
| `MAX_CONNECTIONS`     | `8`   | If `threading.active_count()` exceeds this, new sockets are closed immediately. Prevents thread exhaustion |
| `HANDSHAKE_TIMEOUT_S` | `10`  | Unchanged from Sprint 1                                                                                    |
| `BODY_TIMEOUT_S`      | `10`  | Unchanged from Sprint 1                                                                                    |

## 4. Shared State Inventory

Every piece of mutable data and who may touch it:

| Data                                                                      | Lives in             | Accessed by                         | Protection                                                |
| :------------------------------------------------------------------------ | :------------------- | :---------------------------------- | :-------------------------------------------------------- |
| `server.lobby` (waiting `PlayerConn` or `None`)                           | `ServerState`        | Any handler thread                  | `STATE_LOCK`                                              |
| `server.session` (current `GameSession` or `None`)                        | `ServerState`        | Any handler thread                  | `STATE_LOCK`                                              |
| `session.board`, `session.active`, `session.move_number`, `session.state` | `GameSession`        | Handler threads of the two players  | `STATE_LOCK`                                              |
| `server.scoreboard` (alias -> record)                                     | `ServerState`        | Any handler thread                  | `STATE_LOCK`                                              |
| **Writes** to a registered client socket                                  | `PlayerConn.sock`    | Either player's thread (broadcasts) | `STATE_LOCK` (serializes frame so bytes never interleave) |
| **Reads** from a client socket                                            | `PlayerConn.sock`    | Owning handler thread only          | Thread-confined, no lock needed                           |
| Parsed message dict, local variables                                      | Handler thread stack | One thread                          | Thread-confined                                           |
| Sockets not yet registered (handshake phase)                              | Handler thread       | Owning thread only                  | Thread-confined                                           |

**Ownership rule:** only the owning thread ever calls `recv()` on, `settimeout()` on, or `close()` on a client socket. Other threads may only `send` (under the lock) or `shutdown()` it (Section 10).

## 5. Locking Strategy

### 5.1 The Lock

```python
STATE_LOCK = threading.Lock()   # ONE coarse-grained, non-reentrant lock for ALL shared game state
```

With two players and messages that arrive at human speed, contention is negligible, so one coarse lock is the safest choice. It makes "which lock protects what" trivially answerable: **all of it**.

### 5.2 Acquisition Rules

1. **Always use `with STATE_LOCK:`.** Never call `acquire()` / `release()` manually, so exceptions cannot leave the lock held.
2. **Never nest.** `threading.Lock` is not reentrant; re-acquiring it in the same thread deadlocks. Functions that assume the lock is already held are named with a `_locked` suffix (e.g. `broadcast_locked`, `finish_game_locked`) and **never** acquire it themselves. Only entry points (`dispatch`, `register_player`, `on_client_disconnected`) acquire it.
3. **Never block on a read while holding the lock.** `recv_exact()` / `read_message()` are called only outside the lock. This guarantees a silent client cannot freeze the other player.
4. **Do syntax checks before acquiring.** UTF-8/JSON parsing, schema validation, and framing (Sprint 1 steps 1-4) use no shared state, so they run unlocked and keep the critical section short.
5. **Every write to a registered socket happens inside the lock.** One function, `send_locked(player, msg)`, performs all such writes and asserts `STATE_LOCK.locked()` in debug builds. Replies to syntax errors therefore take the lock briefly just to send.
6. **Validate, then commit.** All checks that can fail run before any mutation. After the first mutation, the only failures possible are socket errors, which are handled by forfeit (Section 8.3). A move is never half-applied.
7. **Keep critical sections short.** Only validation, board mutation, scoreboard update, and the (tiny) sends happen inside.

### 5.3 Critical Section Map (Which Thread, Which Lock, Which Transition)

| Operation                                                         | Runs in                       | Lock held?  | FSM transition     |
| :---------------------------------------------------------------- | :---------------------------- | :---------- | :----------------- |
| `accept()` and thread spawn                                       | Main thread                   | No          | (pre-FSM)          |
| Read and validate `CONNECT` frame                                 | New client thread             | No          | -                  |
| First `CONNECT`: become `Player_1`, send `LOBBY_WAIT`             | Client thread                 | Yes         | T2                 |
| Second `CONNECT`: create session, send `GAME_START` x2            | Client thread (Player 2's)    | Yes         | T4, T5             |
| Third client: send `ERROR GAME_FULL`                              | Client thread                 | Yes (check) | -                  |
| Wait for a frame (`recv_exact`)                                   | Owning thread                 | **No**      | -                  |
| Frame / JSON / schema validation                                  | Owning thread                 | **No**      | (steps 1-4)        |
| State / id / turn / column checks, apply move, evaluate           | Mover's thread                | Yes         | T6-T10             |
| Broadcast `STATE_UPDATE` to both                                  | Mover's thread                | Yes         | T9                 |
| Build and broadcast `GAME_OVER`, update scores, shut down sockets | Thread that detected the end  | Yes         | T10, T13, T14, T15 |
| Forfeit on disconnect                                             | Thread that detected the drop | Yes         | T3, T11, T12       |
| Close own socket                                                  | Owning thread, in `finally`   | No          | -                  |

### 5.4 Why the GIL Does Not Make This Safe

Python's GIL protects individual bytecode operations, not sequences of them. The turn check and the board update are a _check-then-act_ pair:

```python
if session.active == player_id:      # (1) check
    session.drop_disc(row, col)      # (2) act   <- another thread can run between (1) and (2)
    session.active = other           # (3)
```

Without the lock, Player 2's thread could slip in between steps (1) and (3) and also pass its own check. The lock makes (1)-(3) one indivisible step with respect to every other thread.

### 5.5 Deadlock and Starvation Analysis

| Hazard                                         | Analysis                                                                                                                                                  |
| :--------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Circular wait** (needs 2+ locks)             | There is exactly one lock, so a cycle is impossible.                                                                                                      |
| **Self-deadlock** (re-acquiring a held `Lock`) | Prevented by rule 2: `_locked` helpers never acquire; entry points are never called from inside the lock.                                                 |
| **Lock held across a blocking call**           | Reads are never done under the lock (rule 3). Sends are bounded (Section 8.3).                                                                            |
| **Lost unlock on exception**                   | `with` releases automatically; validate-then-commit keeps state consistent if an exception unwinds mid-critical-section.                                  |
| **Starvation**                                 | `threading.Lock` is not strictly fair, but at most two clients contend, each holding the lock for microseconds, so starvation is not a practical concern. |

## 6. Race Condition Analysis

| #   | Race                                            | What could go wrong without a lock                            | How the design prevents it                                                                                                                                 |
| :-- | :---------------------------------------------- | :------------------------------------------------------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | Both players send `MOVE` at the same instant    | Both pass the turn check; two discs placed; turn corrupted    | The first thread to take `STATE_LOCK` runs the full check-then-act. The second then evaluates against the **updated** state (Section 7.3)                  |
| R2  | A client sends two `MOVE` frames back-to-back   | Second move applied before the turn flips                     | Same thread, so sequential. After the first move `active` is the opponent, so the second gets `OUT_OF_TURN`                                                |
| R3  | Two clients `CONNECT` simultaneously            | Both become `Player_1`, or the lobby is overwritten           | Registration is inside the lock. Lock order decides roles: first through is `Player_1` (`X`), second is `Player_2` (`O`)                                   |
| R4  | A third client connects as the match is created | Third client joins a half-built session                       | The `server.session is not None` check and match creation are in one critical section. Late arrivals get `GAME_FULL`                                       |
| R5  | Opponent drops while a move is being processed  | Move applied against a dead peer; two threads finish the game | Drop handling needs the lock, so it runs after the move completes. Both paths end in `finish_game_locked()`, which is **idempotent**                       |
| R6  | Both players disconnect at once                 | Two `GAME_OVER` broadcasts; scores counted twice              | First thread through the lock finishes the game; the second sees `session.is_over()` and returns                                                           |
| R7  | Stale thread from a finished game fires late    | Forfeits the **next** game                                    | Each match is a fresh `GameSession` object. A thread's `player.session` points to its own old session, which is already over, so the late event is ignored |
| R8  | Two threads write to one socket                 | Interleaved bytes corrupt frame boundaries                    | All writes go through `send_locked()` under `STATE_LOCK`                                                                                                   |
| R9  | Score read while being updated                  | Torn or half-updated score table                              | Scoreboard is read and written only under `STATE_LOCK`, and updated exactly once per game                                                                  |

**Serializability principle:** any interleaving of events produces the same final state as _some_ one-at-a-time ordering of those events. The lock decides the order; the state machine is correct for every order.

## 7. Turn Enforcement

### 7.1 Validation Pipeline vs. Lock Boundary

This is the Sprint 1 pipeline (`fsm_specification.md` Section 5.1), annotated with where the lock is held.

| Step | Check                                             | Error code on failure                                       | Lock    |
| :--- | :------------------------------------------------ | :---------------------------------------------------------- | :------ |
| 1    | Frame header and body valid (`1 <= N <= 4096`)    | `FRAME_EMPTY` / `FRAME_TOO_LARGE` / `FRAME_TIMEOUT` (fatal) | No      |
| 2    | UTF-8 JSON object                                 | `MALFORMED_JSON`                                            | No      |
| 3    | Envelope and payload schema                       | `SCHEMA_VIOLATION`                                          | No      |
| 4    | `msg_type` known and client-originated            | `UNKNOWN_MSG_TYPE`                                          | No      |
| 5    | Session exists and state is `PLAYER_TURN`         | `WRONG_STATE`                                               | **Yes** |
| 6    | `player_id` equals this socket's assigned id      | `ID_MISMATCH`                                               | **Yes** |
| 7    | `session.active == player_id`                     | `OUT_OF_TURN`                                               | **Yes** |
| 8    | `0 <= col <= 6`                                   | `INVALID_COLUMN`                                            | **Yes** |
| 9    | Column has an empty cell                          | `COLUMN_FULL`                                               | **Yes** |
| 10   | Commit: drop disc, evaluate, swap turn, broadcast | -                                                           | **Yes** |

### 7.2 Move Handler (Reference Logic)

```python
def handle_move(player: PlayerConn, msg: dict) -> None:
    """Runs in the mover's own thread. msg already passed steps 1-4 (unlocked)."""
    with STATE_LOCK:
        session = player.session
        if session is None or session.is_over():
            return                                        # game already ended: ignore
        if session.state is not State.PLAYER_TURN:
            return send_error_locked(player, "WRONG_STATE")
        if msg["player_id"] != player.player_id:
            return send_error_locked(player, "ID_MISMATCH")
        if session.active != player.player_id:
            return send_error_locked(player, "OUT_OF_TURN")  # turn NOT passed
        col = msg["payload"]["col"]
        if not 0 <= col <= 6:
            return send_error_locked(player, "INVALID_COLUMN")
        row = session.lowest_empty_row(col)
        if row is None:
            return send_error_locked(player, "COLUMN_FULL")

        # ---- commit point: every validation has passed ----
        session.drop_disc(row, col, player.symbol)
        session.move_number += 1
        outcome = session.evaluate(row, col)              # "WIN", "DRAW", or None
        if outcome is None:
            session.active = other(player.player_id)      # swap in the SAME critical section
            broadcast_locked(session, build_state_update(session, row, col))   # T9
        else:
            finish_game_locked(session, outcome, winner=player.player_id)      # T10
```

### 7.3 Invariants That Out-of-Turn Moves Cannot Break

| #   | Invariant                                                       | Why it holds                                                                           |
| :-- | :-------------------------------------------------------------- | :------------------------------------------------------------------------------------- |
| I1  | Only a validated `MOVE` from `session.active` mutates the board | Steps 5-9 precede any mutation and return early on failure                             |
| I2  | Number of discs on the board equals `move_number`               | Disc placement and `move_number += 1` occur together under the lock                    |
| I3  | `active` flips only together with a board change                | The swap is in the same critical section as `drop_disc`                                |
| I4  | Error paths are read-only                                       | Every `send_error_locked` return occurs before the commit point                        |
| I5  | No mutation after the game ends                                 | Every entry point checks `session.is_over()` first; `finish_game_locked` is idempotent |

**What if the opponent's `MOVE` was sent just before the update arrived?** If Player 2 sends a move while it is still Player 1's turn and wins the lock first, it gets `OUT_OF_TURN` and the state is untouched. If Player 1's move is processed first and Player 2's message is evaluated afterwards, it is _legitimately_ Player 2's turn and the move is valid. Either result is correct because **server state, not network arrival order, defines the turn.**

## 8. Synchronized Broadcast Mechanism

### 8.1 Procedure (`broadcast_locked`)

1. **Snapshot once.** Under the lock, build the `STATE_UPDATE` (or `GAME_OVER`) message from the committed state: board, `last_move`, `next_turn`, `move_number`.
2. **Serialize once.** `frame = encode_frame(message)`. Both clients receive **byte-identical** frames (the server-to-client `player_id` is always `"SERVER"`, so no per-recipient fields differ).
3. **Send in a fixed order.** `Player_1` then `Player_2`, both inside the same critical section.
4. **Release only after both sends return.** No other thread can mutate state or emit another message to either client until both frames are written to the kernel send buffers.
5. **Check results.** If either send raises (`BrokenPipeError`, `ConnectionResetError`, timeout), the recipient is treated as disconnected and the game ends by forfeit (Section 8.3).

### 8.2 What Is and Is Not Guaranteed

TCP cannot deliver bytes to two different machines at the same physical instant, so "synchronous" is defined precisely:

| Guaranteed                                                                                                                                                       | Not guaranteed (and why it does not matter)                                |
| :--------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------- |
| Both clients receive the **same snapshot** (same `move_number`, board, `next_turn`)                                                                              | The exact arrival instant at each client. Delivery skew is network latency |
| The server writes both frames before it processes any other event                                                                                                | That the client application has already rendered it                        |
| Each client sees updates in **strict order** (TCP ordering + one writer at a time)                                                                               | -                                                                          |
| No client can act on a state the other has not been sent: the only legal next mover is the one named by `next_turn`, and anything else is rejected `OUT_OF_TURN` | -                                                                          |

Because the server is the single authority and rejects every action not valid in the current state, small differences in when each client _displays_ the update cannot corrupt the game.

### 8.3 Send Failures and Blocking Risk

- **Failure handling:** each send is wrapped in `try/except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError)`. A failed send never aborts the loop over recipients; failures are collected, then `finish_game_locked(..., "FORFEIT", survivor)` runs once.
- **Why `sendall()` under the lock cannot hang indefinitely:** a full game produces at most about 42 `STATE_UPDATE` frames of roughly 250 bytes plus one `GAME_START` and one `GAME_OVER`, about 11 KB in total per client. This is less than a typical default TCP send buffer (16 KB or more), so even a peer that never reads cannot fill the buffer during one game. The 10-second socket timeout only applies while the owning reader thread is mid-frame, so the design does not rely on it for this argument.
- **Hardening option (not required):** enable `SO_KEEPALIVE` on accepted sockets to detect dead peers sooner.

### 8.4 Client-Side Synchronization Rules

| Rule                                                                                                                    | Reason                                                                 |
| :---------------------------------------------------------------------------------------------------------------------- | :--------------------------------------------------------------------- |
| The client never updates its own board optimistically; it redraws only from `STATE_UPDATE` / `GAME_START` / `GAME_OVER` | The server is the single source of truth                               |
| The client prompts for a move only when the last `next_turn` equals its own `player_id`                                 | Prevents pointless `OUT_OF_TURN` errors                                |
| On `ERROR` (non-fatal) during its own turn, the client re-prompts                                                       | The turn was not passed (Sprint 1 rule)                                |
| The client is single-threaded and blocking                                                                              | The game is strictly turn-based: wait for update, prompt, send, repeat |

## 9. Score Storage and Broadcast (Assignment 2.3)

### 9.1 How Scores Are Stored in Server Memory

Scores live in one in-memory dictionary on the server, keyed by player alias, guarded by `STATE_LOCK`:

```python
# server.scoreboard : Dict[str, Record]
# Record = {"wins": int, "losses": int, "draws": int}
scoreboard = {
    "Alice": {"wins": 2, "losses": 1, "draws": 0},
    "Bob":   {"wins": 1, "losses": 2, "draws": 0},
}
```

Scores persist across games for as long as the server process runs (each game ends with `CLEANUP`, which closes sockets but **keeps** the scoreboard, matching `fsm_specification.md` Section 2). They are lost when the server restarts; persistence to disk is out of scope.

### 9.2 Update Rules (Exactly Once per Game)

The update happens inside `finish_game_locked()`, which is idempotent (R6), so a game can never be scored twice.

| Result                            | Winner record        | Other player's record |
| :-------------------------------- | :------------------- | :-------------------- |
| `WIN` (four in a row)             | `wins += 1`          | `losses += 1`         |
| `FORFEIT` (opponent disconnected) | Survivor `wins += 1` | Leaver `losses += 1`  |
| `DRAW` (board full)               | Both `draws += 1`    | -                     |

### 9.3 Broadcast Logic on Round Completion

1. Thread detecting the end acquires `STATE_LOCK` (or already holds it inside `handle_move`).
2. `finish_game_locked()` checks `session.is_over()`; if already over, return (idempotent).
3. Transition to `GAME_OVER`; update the scoreboard (Section 9.2).
4. Build **one** `GAME_OVER` message containing `result`, `winner`, `reason`, `winning_cells`, final `board`, and `scores`.
5. Serialize once and send to both players, each in its own `try/except`, using the same ordered broadcast as Section 8. A forfeit sends only to the survivor.
6. Transition to `CLEANUP`: `shutdown(SHUT_RDWR)` both sockets (Section 10), set `server.session = None`, and return to `WAITING_FOR_PLAYERS` (T15).

### 9.4 The Score Table on the Wire (No Protocol Change)

The Sprint 1 `GAME_OVER` payload already carries a `scores` object. It maps each seat to that seat's player's cumulative wins from the scoreboard:

```json
"scores": { "Player_1": 3, "Player_2": 1 }
```

Both clients receive the identical table and render it with the aliases they learned from `GAME_START`, for example:

```text
================ SCORES ================
 Alice (Player_1, X) : 3 wins
 Bob   (Player_2, O) : 1 win
========================================
```

Scores are pushed **only** at game completion (in `GAME_OVER`); `STATE_UPDATE` deliberately carries no scores, since they cannot change mid-game.

### 9.5 Known Limitations (Documented Decisions)

- The alias is the scoreboard key, so two different people using the same alias share a record. Acceptable for this project; a unique-alias rule could be added in a later sprint.
- Only the winner count is sent in `scores`. Losses and draws are tracked server-side and could be added to the payload in a later protocol revision.

## 10. Cross-Thread Connection Teardown

When one thread ends the game, the _other_ player's thread may be blocked in `recv()`. Rules:

| Rule                                                                                                                     | Reason                                                                                                                                                             |
| :----------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The thread that ends the game calls `sock.shutdown(socket.SHUT_RDWR)` on **both** sockets (each in `try/except OSError`) | `shutdown()` makes the other thread's blocking `recv()` return `b""` promptly. Calling `close()` from a different thread does not reliably wake a blocked `recv()` |
| Only the **owning** thread calls `sock.close()`, in its `finally` block                                                  | Avoids closing a descriptor another thread is still using                                                                                                          |
| A woken thread checks `player.session.is_over()` before treating EOF as a disconnect                                     | A game that already ended must not trigger a second forfeit (R5, R6, R7)                                                                                           |
| `GAME_OVER` is written **before** `shutdown()`                                                                           | Queued data is still delivered before the FIN                                                                                                                      |

## 11. Implementation Skeleton (Contract)

This fixes the structure; it is not the final implementation.

```python
import socket
import threading
from enum import Enum, auto
from typing import Dict, Optional

STATE_LOCK = threading.Lock()          # the only lock

class State(Enum):
    INIT = auto(); WAITING_FOR_PLAYERS = auto(); GAME_START = auto()
    PLAYER_TURN = auto(); EVALUATE_MOVE = auto(); FORFEIT_HANDLING = auto()
    GAME_OVER = auto(); CLEANUP = auto()

class PlayerConn:
    sock: socket.socket
    alias: str
    player_id: str                     # "Player_1" | "Player_2"
    symbol: str                        # "X" | "O"
    session: Optional["GameSession"]   # set under STATE_LOCK when matched

class GameSession:                     # a NEW object per match (isolates stale threads, R7)
    players: Dict[str, PlayerConn]
    board: list                        # 6 rows x 7 cols
    active: str
    move_number: int
    state: State
    def is_over(self) -> bool: ...
    def lowest_empty_row(self, col: int) -> Optional[int]: ...
    def drop_disc(self, row: int, col: int, symbol: str) -> None: ...
    def evaluate(self, row: int, col: int) -> Optional[str]: ...

class ServerState:
    lobby: Optional[PlayerConn] = None
    session: Optional[GameSession] = None
    scoreboard: Dict[str, Dict[str, int]] = {}

# --- entry points: acquire STATE_LOCK themselves ---
def accept_loop(listener: socket.socket) -> None: ...        # main thread, no lock
def client_thread(conn: socket.socket) -> None: ...          # one per connection
def register_player(conn, alias) -> Optional[PlayerConn]: ...        # T2 / T4 / T5 / GAME_FULL
def dispatch(player: PlayerConn, msg: dict) -> None: ...             # MOVE / DISCONNECT / illegal
def handle_move(player: PlayerConn, msg: dict) -> None: ...          # Section 7.2
def on_client_disconnected(player: Optional[PlayerConn]) -> None: ...  # T3 / T11 / T12

# --- helpers: assume STATE_LOCK is held, never acquire it ---
def send_locked(player: PlayerConn, msg: dict) -> bool: ...
def send_error_locked(player: PlayerConn, code: str) -> None: ...
def broadcast_locked(session: GameSession, msg: dict) -> None: ...
def finish_game_locked(session: GameSession, result: str, winner: Optional[str]) -> None: ...
def record_result_locked(session: GameSession, result: str, winner: Optional[str]) -> None: ...
```

```python
def on_client_disconnected(player):
    with STATE_LOCK:
        if player is None:
            return
        if server.lobby is player:                    # T3: left the lobby
            server.lobby = None
            return
        session = player.session
        if session is None or session.is_over():      # game already ended: nothing to do
            return
        transition(session, State.FORFEIT_HANDLING, "T11")
        finish_game_locked(session, "FORFEIT", winner=other(player.player_id))   # T13-T15
```

## 12. Concurrency Test Plan

| #   | Test                                                              | Expected result                                                                                                                                      |
| :-- | :---------------------------------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Start the server; connect two clients within the same millisecond | Exactly one `Player_1` and one `Player_2`; both receive `GAME_START`                                                                                 |
| 2   | While one client sits idle in the lobby, connect a second         | Main loop still accepts; match starts                                                                                                                |
| 3   | Both clients send `MOVE` simultaneously, repeated 1000 times      | Board always has `move_number` discs; no double move; the out-of-turn sender gets `OUT_OF_TURN` or a valid move (Section 7.3), never a corrupt state |
| 4   | Player 1 sends two `MOVE` frames in one `sendall()`               | First applied; second gets `OUT_OF_TURN`                                                                                                             |
| 5   | Player 2 sends `MOVE` on Player 1's turn                          | `ERROR OUT_OF_TURN`; board and turn unchanged                                                                                                        |
| 6   | Kill Player 2 with `kill -9` mid-game                             | Player 1 receives `GAME_OVER` (`FORFEIT`); scoreboard shows one win and one loss                                                                     |
| 7   | Both clients killed simultaneously                                | Exactly one `finish_game_locked` effect; scores counted once; server keeps accepting                                                                 |
| 8   | Third client connects during a game                               | Fatal `ERROR GAME_FULL`; game unaffected                                                                                                             |
| 9   | A client sends a header and then stalls                           | Thread times out after 10 s; the other player is unaffected                                                                                          |
| 10  | Complete one game, then start a second with the same aliases      | Scores accumulate across games; no stale-thread effects                                                                                              |

**Race amplification tip:** in the test harness call `sys.setswitchinterval(1e-6)` so the interpreter switches threads far more often, making any missing-lock bug appear quickly. Run with `python -X dev` for extra runtime checks.

## Appendix A. What Would Change With Option B (`selectors`)

For completeness, the alternative design would differ as follows. This is documented to justify the decision, not as the implementation plan.

- A single thread runs `selectors.DefaultSelector()` over the listening socket and every client socket, all set with `setblocking(False)`.
- `recv_exact()` is replaced by a per-connection **incremental frame parser** that accumulates bytes in a buffer, extracts a frame once 4 header bytes plus N body bytes are present, and may extract several frames per readable event (coalescing).
- State isolation comes from the single thread: handlers run to completion one at a time, so no lock is needed, but no handler may block or perform long work.
- Writes need per-connection outbound buffers and `EVENT_WRITE` registration for partial sends.
- The 10-second handshake/body timeouts become deadline timestamps checked each loop iteration.
