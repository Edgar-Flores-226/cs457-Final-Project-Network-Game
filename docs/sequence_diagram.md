# State, Turn & Score Broadcast Sequence Diagrams - Connect Four Server

**Student:** Edgar Flores
**Course:** CS 457 - Computer Networks and the Internet
**Sprint:** 2
**Companion documents:** `concurrency_design.md` (lock rules, race analysts), `protocol_blueprint.md` (messages), `fsm_specification.md` (states)

All diagrams use Mermaid `sequenceDiagram` syntax and render natively on GitHub.

**How to read them:** `Client 1` and `Client 2` are the two players. `Handler Thread 1/2` are the per-connection server threads from `concurrency_design.md`. `Game State` stands for the shared data guarded by `STATE_LOCK` (board, active player, scoreboard). A message arrow from a handler thread to a client is a write on that client's socket, always performed while holding `STATE_LOCK`.

**Difference from the assignment's example diagram:** the example shows `MOVE (row: 0, col: 2)`. This project's protocol (Sprint 1) sends only `col`, because gravity determines the row and the server computes it. The diagrams below use the real protocol messages.

## Contents

- [1. Connection Setup and Matchmaking (Two Simultaneous Clients)](#1-connection-setup-and-matchmaking-two-simultaneous-clients)
- [2. Move, Server Validation, and State Broadcast](#2-move-server-validation-and-state-broadcast)
- [3. Simultaneous Requests: Out-of-Turn Move Racing a Valid Move](#3-simultaneous-requests-out-of-turn-move-racing-a-valid-move)
- [4. Game-Ending Move, Score Update, and Score Broadcast](#4-game-ending-move-score-update-and-score-broadcast)
- [5. Abrupt Disconnect and Forfeit](#5-abrupt-disconnect-and-forfeit)
- [6. Traceability to the Sprint 2 Acceptance Criteria](#6-traceability-to-the-sprint-2-acceptance-criteria)

## 1. Connection Setup and Matchmaking (Two Simultaneous Clients)

This shows that the main thread never blocks on a client and that the lock decides who becomes Player 1 and who becomes Player 2.

```mermaid
sequenceDiagram
    autonumber
    actor C1 as Client 1
    participant M as Main Thread (accept loop)
    participant T1 as Handler Thread 1
    participant GS as Game State (STATE_LOCK)
    participant T2 as Handler Thread 2
    actor C2 as Client 2

    C1->>M: TCP connect
    M->>T1: spawn thread, return to accept
    C2->>M: TCP connect
    M->>T2: spawn thread, return to accept
    Note over M: Main thread never reads or writes game data
    C1->>T1: CONNECT alias=Alice
    Note over T1: read frame, parse, validate (no lock held)
    T1->>GS: acquire STATE_LOCK
    Note over GS: lobby empty, Alice becomes Player_1 (X)
    T1-->>C1: LOBBY_WAIT (1 of 2 connected)
    T1->>GS: release STATE_LOCK
    C2->>T2: CONNECT alias=Bob
    T2->>GS: acquire STATE_LOCK
    Note over GS: lobby has Alice, create new GameSession<br/>Bob becomes Player_2 (O), active = Player_1
    par GAME_START to Player 1
        T2-->>C1: GAME_START (you=Player_1, symbol=X, first_turn=Player_1)
    and GAME_START to Player 2
        T2-->>C2: GAME_START (you=Player_2, symbol=O, first_turn=Player_1)
    end
    T2->>GS: release STATE_LOCK
    Note over T1,T2: Both threads now block in recv, waiting for their client
```

**Narrative**

1. The main thread accepts each TCP connection and immediately hands it to a new handler thread, so it is always free to accept the next one.
2. Each handler thread reads and validates `CONNECT` on its own, without any lock (FSM steps 1-4).
3. Registration happens under `STATE_LOCK`. Whichever thread takes the lock first with an empty lobby becomes `Player_1` (race R3 in `concurrency_design.md`).
4. The second `CONNECT` creates a fresh `GameSession`, assigns roles, and sends a personalised `GAME_START` to both clients (FSM T4, T5). Player 2's thread performs both sends, so Player 1's thread does not need to be woken.

## 2. Move, Server Validation, and State Broadcast

This is the core turn-synchronization flow, matching the assignment's example but with the real lock and protocol details. It covers a valid move and both rejection paths.

```mermaid
sequenceDiagram
    autonumber
    actor C1 as Client 1 (Player 1, X)
    participant T1 as Handler Thread 1
    participant GS as Game State (STATE_LOCK)
    actor C2 as Client 2 (Player 2, O)

    C1->>T1: MOVE (col=3)
    Note over T1: read_frame + parse + schema check<br/>no lock held
    T1->>GS: acquire STATE_LOCK
    Note over GS: Server authority checks<br/>state is PLAYER_TURN, player_id matches,<br/>active is Player_1, col in 0..6, column not full
    alt Valid move
        Note over GS: Drop disc (gravity gives the row)<br/>move_number += 1, no win or draw<br/>active = Player_2 (same critical section)
        par STATE_UPDATE to Player 1
            T1-->>C1: STATE_UPDATE (board, last_move, next_turn=Player_2, move_number)
        and STATE_UPDATE to Player 2
            T1-->>C2: STATE_UPDATE (board, last_move, next_turn=Player_2, move_number)
        end
    else Out of turn or player_id mismatch
        T1-->>C1: ERROR (OUT_OF_TURN or ID_MISMATCH, fatal=false)
        Note over GS: Board and turn unchanged
    else Invalid column or column full
        T1-->>C1: ERROR (INVALID_COLUMN or COLUMN_FULL, fatal=false)
        Note over GS: Board unchanged, Player_1 must choose again
    end
    T1->>GS: release STATE_LOCK
    Note over C1,C2: Only the client named in next_turn may move
```

**Narrative**

1. Parsing and schema validation use no shared state, so they run before the lock is taken. This keeps the critical section short.
2. Inside the lock, the checks run in the Sprint 1 order: state, `player_id`, turn, column range, column full. The first failure produces a non-fatal `ERROR` to the sender only, and **the turn is not passed**.
3. A valid move is applied, `move_number` is incremented, and `active` flips, all in one critical section (invariants I2 and I3).
4. The `STATE_UPDATE` is built once and serialized once, so both clients receive byte-identical frames. Both writes complete before the lock is released, so no other thread can emit another message to either client in between. `par` is used to show the two sends are one atomic step from the game's point of view; on the wire they are two consecutive `sendall()` calls in a fixed order (Player 1, then Player 2).

## 3. Simultaneous Requests: Out-of-Turn Move Racing a Valid Move

This shows two handler threads contending for the lock at the same instant. It is the scenario that proves out-of-turn moves cannot corrupt state.

```mermaid
sequenceDiagram
    autonumber
    actor C1 as Client 1 (Player 1, X)
    participant T1 as Handler Thread 1
    participant GS as Game State (STATE_LOCK)
    participant T2 as Handler Thread 2
    actor C2 as Client 2 (Player 2, O)

    Note over GS: active = Player_1, move_number = 4
    par Player 1 sends a legal move
        C1->>T1: MOVE (col=3)
    and Player 2 sends too early
        C2->>T2: MOVE (col=5)
    end
    Note over T1,T2: Both parse and validate in parallel, no lock held
    T2->>GS: acquire STATE_LOCK (obtained first)
    T1->>GS: acquire STATE_LOCK (blocks, waits)
    Note over GS: Thread 2 critical section<br/>active is Player_1, not Player_2
    T2-->>C2: ERROR (OUT_OF_TURN, fatal=false)
    T2->>GS: release STATE_LOCK
    GS-->>T1: lock granted
    Note over GS: Thread 1 critical section<br/>move is valid, move_number = 5, active = Player_2
    par STATE_UPDATE to Player 1
        T1-->>C1: STATE_UPDATE (next_turn=Player_2, move_number=5)
    and STATE_UPDATE to Player 2
        T1-->>C2: STATE_UPDATE (next_turn=Player_2, move_number=5)
    end
    T1->>GS: release STATE_LOCK
```

**Narrative**

1. Both messages arrive at nearly the same time and are parsed in parallel because parsing needs no lock.
2. `STATE_LOCK` admits one thread at a time. Here Thread 2 wins the lock, finds it is not Player 2's turn, replies `OUT_OF_TURN`, and changes nothing.
3. Thread 1 then runs its full check-then-act and the game proceeds normally.
4. If Thread 1 had won the lock instead, its move would apply first. Player 2's message would then be evaluated against the **new** state, where it is Player 2's turn, and would be accepted as a legitimate move. Both outcomes are valid serial orderings (serializability principle), and neither can corrupt the board. **Server state, not network arrival order, defines whose turn it is.**

## 4. Game-Ending Move, Score Update, and Score Broadcast

This shows how round scores are stored and pushed to both clients on completion (assignment section 2.3).

```mermaid
sequenceDiagram
    autonumber
    actor C1 as Client 1 (Player 1, X, Alice)
    participant T1 as Handler Thread 1
    participant GS as Game State (STATE_LOCK)
    participant T2 as Handler Thread 2
    actor C2 as Client 2 (Player 2, O, Bob)

    C1->>T1: MOVE (col=3)
    T1->>GS: acquire STATE_LOCK
    Note over GS: Checks pass, disc dropped<br/>evaluate finds four in a row for X
    Note over GS: finish_game_locked (idempotent)<br/>state = GAME_OVER<br/>scoreboard: Alice wins += 1, Bob losses += 1<br/>build ONE GAME_OVER message with scores
    par GAME_OVER to Player 1
        T1-->>C1: GAME_OVER (WIN, winner=Player_1, winning_cells, board, scores)
    and GAME_OVER to Player 2
        T1-->>C2: GAME_OVER (WIN, winner=Player_1, winning_cells, board, scores)
    end
    Note over GS: state = CLEANUP<br/>shutdown(SHUT_RDWR) on both sockets<br/>server.session = None, scoreboard kept
    T1->>GS: release STATE_LOCK
    Note over T2: Blocked recv wakes with EOF<br/>session.is_over() is true, so no forfeit
    T2->>C2: close own socket (finally)
    T1->>C1: close own socket (finally)
    Note over GS: Back in WAITING_FOR_PLAYERS (T15)<br/>ready for a new match, scores retained
```

**Narrative**

1. The winning move is detected inside the same critical section that applied it. The thread that placed the winning disc runs `finish_game_locked()`.
2. Scores are updated **exactly once** in server memory (`wins`, `losses`, `draws` per alias), because `finish_game_locked()` returns immediately if the session is already over.
3. A single `GAME_OVER` message is built from the committed scoreboard (`scores = {"Player_1": wins, "Player_2": wins}`) and sent identically to both clients. This is the score-table broadcast. No scores are sent in `STATE_UPDATE` because they cannot change mid-game.
4. The finishing thread calls `shutdown(SHUT_RDWR)` on both sockets after writing `GAME_OVER`. This wakes the other thread's blocking `recv()` so it can exit; it sees `session.is_over()` and ends quietly instead of treating the EOF as a forfeit.
5. The server returns to `WAITING_FOR_PLAYERS` with the scoreboard intact (FSM T14, T15).

## 5. Abrupt Disconnect and Forfeit

This shows connection loss being detected by one thread and resolved safely while the other thread is blocked waiting.

```mermaid
sequenceDiagram
    autonumber
    actor C1 as Client 1 (Player 1, X, Alice)
    participant T1 as Handler Thread 1
    participant GS as Game State (STATE_LOCK)
    participant T2 as Handler Thread 2
    actor C2 as Client 2 (Player 2, O, Bob)

    Note over GS: PLAYER_TURN, active = Player_1
    Note over C2: Process killed (kill -9) or link severed
    C2--xT2: TCP RST or FIN, no DISCONNECT message
    Note over T2: recv returns EOF or raises ConnectionResetError<br/>no lock held yet
    T2->>GS: acquire STATE_LOCK
    Note over GS: on_client_disconnected<br/>session not over, so FORFEIT_HANDLING (T11)<br/>survivor = Player_1
    Note over GS: finish_game_locked (idempotent)<br/>scoreboard: Alice wins += 1, Bob losses += 1
    T2-->>C1: GAME_OVER (FORFEIT, winner=Player_1, reason=OPPONENT_DISCONNECTED, scores)
    Note over T2,C2: Send to Bob is skipped or fails inside its own try/except
    Note over GS: shutdown(SHUT_RDWR) both sockets<br/>server.session = None
    T2->>GS: release STATE_LOCK
    Note over T1: Blocked recv wakes with EOF<br/>session.is_over() is true, so no second forfeit
    T1->>C1: close own socket (finally)
    T2->>C2: close own socket (finally)
```

**Narrative**

1. A crash produces no `DISCONNECT` frame. The server learns of it only from EOF (`b""`) or a socket exception in the dead player's own thread (`protocol_blueprint.md` Section 8). A graceful `DISCONNECT` frame follows the same path from the lock onward.
2. The thread that detects the loss takes `STATE_LOCK`, so it cannot interleave with a move being processed by the other thread (race R5).
3. `finish_game_locked()` records the forfeit, sends `GAME_OVER` (`FORFEIT`) to the survivor inside its own `try/except` (the survivor may also be gone), and shuts down both sockets.
4. If both players drop at once, whichever thread gets the lock first finishes the game and the other finds the session already over, so there is no double `GAME_OVER` and no double scoring (race R6).

## 6. Traceability to the Sprint 2 Acceptance Criteria

| Acceptance criterion                                                              | Where it is shown                                                                   |
| :-------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------- |
| At least 2 simultaneous clients without blocking the main loop                    | Diagram 1 (accept loop spawns threads; handlers block independently)                |
| Thread safety / mutex locking documented                                          | `concurrency_design.md` Sections 4-6; lock acquire/release visible in every diagram |
| Turn enforcement prevents out-of-turn corruption                                  | Diagrams 2 and 3; invariants I1-I5 in `concurrency_design.md` Section 7.3           |
| Diagram shows move requests, server validation, and simultaneous state broadcasts | Diagrams 2, 3, and 4 (`par` blocks for both clients)                                |
| Both clients receive state updates synchronously                                  | Diagrams 2 and 4; guarantee analysis in `concurrency_design.md` Section 8.2         |
| Score storage and broadcast on round completion                                   | Diagram 4 and `concurrency_design.md` Section 9                                     |
| Abrupt and orderly disconnects handled                                            | Diagram 5                                                                           |
