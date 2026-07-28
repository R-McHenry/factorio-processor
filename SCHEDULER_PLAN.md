# Plan: auto-scheduler (compiler backend) for the switch-matrix processor

> **2026-07-13: superseded by V8.md for current work.** This file documents the
> v7-era plan; phases 1–3 shipped (verifier.py, scheduler.py), the v7 ISA is
> archived (modules/processor_v7.source.json), and the v8 machine + machine_v8.py
> compiler are the live system. Phases 4 (tick simulator) and 5 (front end)
> remain open and are tracked in V8.md's TODO.

Self-contained brief for a fresh session. Goal: programs written as dependency-declared
operations, with slot assignment done by a scheduler instead of by hand.

## Status (2026-07-13)

- **Phase 1 DONE** — `verifier.py`: replays latch/port/cell/ALU/jump timelines over
  `Program.slots`; passes hand fib/fib_cmp unchanged, flags the reintroduced I=H+H
  bug (`alu-read-transient`). Gates generation in `tools/build_closed_loop.py`.
  Tests: `test_verifier.py` (10).
- **Phase 2+3 DONE** — `scheduler.py`: staged IR (`write_imm`/`copy`/`jump_if_zero`,
  `barrier()`, `label()`), greedy earliest-slot placement with port round-robin,
  latch-interval reservation, RAW/anti/WAW edges, jump-shadow floors. Auto fib_cmp:
  no literal slots, end 37 vs hand 38, loop period 26 (same). Suite variant
  `fib_cmp_auto` added to `run_all.py` (10 benches). Tests: `test_scheduler.py` (7).
- Key semantics discovered: within a stage, reads sample PRE-stage state (parallel
  register semantics — needed so `G<-H; H<-I` reads old values); `barrier()` commits.
  Scheduler uses cell-read margin v+3 (measured working) vs verifier's hard line v+2.
- **Phase 4 (tick simulator) and Phase 5 (front-end DSL) remain.**

## Where things stand

The pipeline already exists from encoding downward, hand-scheduled at the top:

- `assembler.py` — `encode(routes, imm)` packs one instruction word (bits 0–11 =
  3×4 switch-matrix state for one tick, bits 12+ = immediate). `Program` is a
  slot-based builder: `write_imm`, `copy`, `jump_offset`, `warm_latch`,
  `alu_ready_pulse`, with slot merging + immediate-conflict detection. **Slot
  numbers are still chosen by the human** — that's the gap this plan closes.
- `signal_space.py` — 2100-slot address space (signal × 5 qualities), `ALU_MAP`
  (op → operand/result signals + latency), `address_of()/signal_at()`.
- `tools/build_closed_loop.py` — generates closed-loop sources + testbenches for
  programs `hello`, `array`, `fib`, `fib_cmp`. fib/fib_cmp use `Program`.
- `factorio_memory_tb.py` — the live testbench runner (drivers, probes, per-step
  `skip_ticks` fast-forward, `save_after_run`).
- `run_all.py` — full suite (9 benches), `--start-server --regen` supported.
- `modules/processor.design.md` — architecture + all measured rules.
- Everything below is **verified live**; the suite is 9/9 green.

## Measured hardware model (the constraint system)

Machine: 1 instruction/tick, no stalls, no instruction register. An instruction is
the matrix state for exactly one tick; operations exist as pulses that must meet at
the right combinator on the right tick. Scheduling constants (in `Program`):

| Rule | Constant |
|---|---|
| const write-address at slot n pairs the write value from slot n+2 | `ADDR_TO_VALUE = 2` |
| port-sourced write-address pairs value from n+1 (skips the >>12 comb) | (used by array demo) |
| port address pulse at p is consumed at exactly p+3, live 1 tick only | `PORT_READ_TO_USE = 3` |
| write value slot v lands in the memory cell at ~v+5 | (implicit) |
| ALU result readable by a port pulse at ≥ v+2+latency (`ALU_MAP` latency) | `alu_ready_pulse` |
| PC-write value slot n takes effect at fetch n+7; n+1..n+6 execute (delay slots) | `JUMP_DELAY_SLOTS = 6` |
| cold write-address latch drops leading values; warm with 2-3 addr-only slots | `warm_latch` |

Hazards found the hard way (must be modeled, not rediscovered):
- **RAW through ALU**: reading a derived output (I=G+H) while an operand cell is
  mid-update returns a transient (fib bug: read I=H+H). Rule: an ALU output read at
  port-sample tick t requires all operand cells unchanged in a window around t.
- **Write-address latch lifetime**: the latch holds from (addr_slot+5) until the
  next address arrives; every value slot pairs with whichever latch value is live
  at its multiplier-sample tick. Two ops' address latches must not interleave wrong.
- **One immediate per slot**: routes merge freely, immediates cannot differ.
- **Port pulses pipeline** (pulses on consecutive slots are fine) but each pulse's
  consumer is fixed at +3, and a port serves one address per tick.

## Architecture to build

```
ops + deps (IR)  →  dependency DAG  →  list scheduler  →  verifier  →  Program.at()  →  encode  →  ROM
                                        (new)              (new)        (exists)       (exists)
```

### Phase 1 — verifier / linter (build this first)
A pass that takes a filled `Program` and checks every rule above: pairing of each
value slot to its latch value, port pulse/consumer alignment, ALU-read windows
against cell-update times, imm conflicts (already done), jump shadow contents.
- Input: `Program.slots`; simulate latch/cell/port timelines symbolically.
- Acceptance: it passes the current hand-built fib and fib_cmp unchanged, and it
  **flags the original I=H+H bug** if you re-introduce it (regression fixture).
- Value: immediately useful even before auto-scheduling; catches hand errors.

### Phase 2 — list scheduler
IR: `Op` records — `copy(src, dst, port=None)`, `write_imm(addr, val)`,
`jump_offset(flag_cell, base, dest_block)` — plus explicit or inferred deps
(cell reads-after-writes; port assignment can be automatic: 2 ports = 2 resources).
- Greedy earliest-slot list scheduling with resource reservation tables
  (per-slot routes/imm, port-live ticks, latch intervals, cell-update times).
- Acceptance: auto-scheduled fib_cmp passes the live suite; slot count within
  ~20% of the hand version. Byte-equality not required — behavioral equality via
  `run_all.py` is the bar.

### Phase 3 — blocks, labels, loops
- Basic blocks with symbolic names; `jump_offset` targets a block, resolved to
  (block_start − 1) at layout time; delay-slot region auto-reserved (6 slots,
  schedulable with hazard-free ops later, NOP for now); exit code placed past the
  jump shadow (see fib: marker at `jump_value + JUMP_DELAY_SLOTS + 1`).
- Acceptance: fib_cmp source contains no literal slot numbers at all.

### Phase 4 (optional but high leverage) — tick simulator
A Python model of the machine (cells, latch, ports, matrix, ALU, PC) accurate to
the timing table. Lets the scheduler/verifier be developed and unit-tested without
booting Factorio; the live suite stays as the final gate.
- Acceptance: simulator reproduces the recorded traces in `results/*.results.json`
  for fib and fib_cmp (same per-tick cell values).

### Phase 5 — front end sugar (later)
Tiny DSL or text format: `G = H; H = I; M = H; loop while Q == 0; heart = 999`.
Compiles to IR. Only after 2–3 are solid.

## Known open design items (decide during the session)
- **Zero-write**: RESOLVED 2026-07-13 (v7.1 memory redesign). The cell dropped its
  else branch; writes are driven by set/reset triggers derived from the matrix out2
  *select* (not the data), so an imm-0 write clears the cell. `write_imm(addr, 0)`
  is legal in the IR/verifier; a const value slot with no imm at all is still an
  error (forgotten immediate). Proven live by `proc_zero_write`.
- **Port allocation policy** when both ports are free (round-robin vs. minimizing
  latch interleavings).
- Whether the verifier should model the array-write mode (port-sourced write
  addresses, n+1 pairing) — the array demo uses it; fib-style code doesn't.

## Ground rules / environment reminders
- Master hardware description: `modules/processor.source.json` (v6). Programs are
  ROM-only variants generated by `tools/build_closed_loop.py`.
- Verify everything live: `.\.venv\Scripts\python.exe run_all.py --start-server --regen`
  (9 benches; needs Factorio closed — single-instance lock).
- signal-R is reserved (read-address carrier, never an ALU output, excluded from
  the address table). CMP outputs: O = M>N, Q = M≥N, T = max, S = min.
