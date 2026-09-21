# Statement of Work (SOW) - CS Final Project

**Student Name:** Edgar Flores
**Course:** CS 457 - Computer Networks and the Internet
**Date:** September 20, 2026
**Repository URL:** https://github.com/Edgar-Flores-226/cs457-Final-Project-Network-Game.git

---
## 1. Project Overview & Selection

* **Selected Game:** Connect Four (CLI-based)
* **Programming Language:** Python 3.9.25
* **Target Domain Name:** `server.flores.edu`

---

## 2. Game Rules & Mechanics

### Turn Mechanics
* **Player Assignment:** The game server manages connection order. The first player to connect is assigned **Player 1 ('X')**, and the second player is assigned **Player 2 ('O')**.
* **Turn Sequence:** Gameplay follows strict alternating turns starting with Player 1.
* **Input Validation:** Players select a column number (1-7) via command-line input. If a player enters an invalid column (out of range or full), the server responds with an error message and prompts that same player to choose again without passing the turn.

### Victory Conditions
* A player wins immediately upon connecting **4 of their disc symbols ('X' or 'O')** in a continuous line.
* Valid winning connections include:
    * **Horizontal:** 4 adjacent symbols across any row.
    * **Vertical:** 4 adjacent symbols down any column.
    * **Diagonal:** 4 adjacent symbols along any positive or negative slope.

### Draw / Tie Conditions
* A draw is declared if all **42 grid positions (6 rows x 7 columns)** are filled without either player achieving a 4-in-a-row alignment.
* In the event of a tie, the server announces a draw to both clients and gracefully terminates or prompts for a rematch.

---

## 3. Network Architecture & Target Domain Setup

* **Transport Protocol:** TCP Sockets (stream-based communication for reliable message delivery).
* **Authoritative DNS Target Domain:** `server.flores.edu`
* **Network Integration Plan:** The CLI client will establish a TCP connection resolving to `server.flores.edu` to send moves and receive updated game state payloads.

---

## 4. Development Roadmap & Milestones

| Sprint Phase | Milestone Focus | Deliverables |
| :--- | :--- | :--- |
| **Sprint 0** | Toolchain & SOW | Git repo setup, tool configuration, SOW submission (`server.flores.edu`). |