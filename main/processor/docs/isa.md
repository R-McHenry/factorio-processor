# v8 processor — complete brief (written 2026-07-13, all facts verified live)

Self-contained state dump for a fresh session. The v8 machine is the **suite
master** (`modules/processor.source.json` = `modules/processor_v8.source.json`,
imported from `modules/processor_v8.bp.txt`). v7 archived as
`modules/processor_v7.source.json`; the v7-era plan lives in SCHEDULER_PLAN.md
(phases 4–5 still open). Suite: **14/14 green** via
`.\.venv\Scripts\python.exe run_all.py --start-server --regen`.

## Hardware (what changed from v7)

Matrix dests: **out1**=write_addr, **out2**=write_value, **out3**=park port A,
**out4**=pc_inject. Port B lost its matrix dest. 12 route bits + 20-bit imm
unchanged; one imm per slot unchanged.

- **Latched port A** — `matrix_out3(12) → addr_rd_a(17) → a_rd_latch(23)`
  (else-branch hold, `≠` condition) `→ each+0→R (37) → port A decider (56)`.
  One address instruction parks the port; data persists until re-parked.
- **pc_inject** — `matrix_out4(18) → pc_inject(26) → arith 40 (each+0→P) → cell
  red input bus`. Anything routed to out4, from any source, is converted to
  signal-P and SUMS into the PC for one tick. Relative jump, computed jump,
  halt (see timing). P stays unmasked → write-path absolute jumps still work.
- **Memory-mapped port B** — decider 53 gates the green memory bus (cells + ALU
  + ROM) with the `mmap_addr(59)` mask (`shape-circle=1`) → `addr_rd_b(24) →
  each+0→R (38) → port B decider (57)`. Port B's read address IS the value of
  the shape-circle cell; write that cell to re-park the stream.
- **Accumulate cells** — `write_trigger_reset(19)` holds a constant −1 for each
  accumulate signal and the cell condition is `each[G] ≤ 0`: the +1 reset pulse
  sums to 0 → hold survives → writes ADD. Unmasked signals reset normally.
  Accumulate cells cannot be zero-written (reset is blocked).
- **Negatives** — cell (36), injector (22), latch (23), mmap gate (53) use `≠0`;
  negative values write, hold, read, and accumulate correctly.
- **Config combinators are toolchain-owned** (user decision): in-game contents
  of `write_trigger_reset`, `mmap_addr`, `pc_autoincrement` are sample
  placeholders; `tools/import_master_bp.py` populates them from
  `machine_v8.ACCUMULATE_SIGNALS` / `MMAP_B_SIGNAL` / P=1 at import and strips
  ALL named logistic groups. To change the accumulate set or B's register cell:
  edit machine_v8 constants, re-import.

Key v8 entity ids (renumber on every export — resolve by `player_description`):
cell **36** (mem_state probe, red), ports `port_a_rd` **64** / `port_b_rd` **65** /
`intruction_rd` **66** (green), matrix outs **7/11/12/18**, write inputs 1/4,
direct-write trio 15/16/19, addr_rd_a **17**, addr_rd_b **24**, ROM **41**,
tables **6/44/45/46**, port-A decider 56 (alu_bus probe), a_rd_latch 23,
pc_inject 26, mmap_addr 59.

## Measured timing (consumer-slot frame: "m" = ROM slot whose routes consume)

| Rule | Constant | Evidence |
|---|---|---|
| a-> consumer needs park ≥ 4 earlier; park persists until next park+4 | `PARK_A_TO_USE=4` | proc_v8_latched_read |
| b-> consumer needs mmap-write value slot ≥ 7 earlier; streams until re-park | `PARK_B_VALUE_TO_USE=7` | proc_v8_mmap_port_b (cell update → data = +3 ticks) |
| cell freshness: m ≥ writer value slot + 6 | `CELL_TO_USE=6` | = v7 rule in consumer frame |
| ALU output: m ≥ v + 5 + latency (CMP flags L=1, T/S L=2) | `ALU_TO_USE_BASE=5` | = v7 alu_ready in consumer frame |
| same-stage anti (read old value): pending write v ≥ m − 2 | `ANTI_MARGIN=2` | v7-verified, carried over |
| boundary transient: m == v+5+L while another operand settles within 6 | `COHERENCE_WINDOW=6` | the v7 I=H+H bug class |
| write path: const addr at n pairs value n+2; cell lands ~v+5 | unchanged | all write benches |
| jump_rel: inject at n → slots n+1..n+4 execute, resume at n+offset+5 | `SHADOW=4, RESUME=5` | proc_v8_relative_jump (skip count == offset exactly; P trace shows the +offset+1 step) |
| jump_if_zero (write-path absolute, addr=P+flag): value slot v effect at fetch v+7, 6 delay slots | unchanged | proc_v8_fib_cmp_v8 |
| halt: `jump_rel` offset −5 lands on itself; n+1..n+4 reserved NOPs; P cycles n..n+4 | | fib bench poison canary stayed 0 |
| accumulate: exactly ONE add per write instruction (1/tick streams count cleanly) | | relative_jump bench used an accumulate cell as instruction counter |

## Vector control: two planes, one set of gates (measured 2026-07-28)

The v10 vector zone's select/erase lines have **two drivers**, and every gate
in the zone tests both — the read-selects and write heads OR their two
conditions, the register cell ANDs its two erase tests. Adding the second plane
cost one more condition on combinators that already had one — not a single new
entity among the zone's own gates — and the first plane still works.

| plane | how a control gets there | cost of a move |
|---|---|---|
| memory-mapped rows (`vec_rsel`/`vec_ssel`/`vec_wsel`/`vec_erase`) | scalar `write_imm` / `pulse_imm`, so `value_slot + WRITE_TO_CELL` | **15.5 ticks** |
| decoder (`vdec_*`, `modules/v10_vec_decoder.fnet`) | a field of the slot's SECOND instruction word | **3.9 ticks** |

**Word 2 comes from a second ROM at the same address.** The instruction word is
full — 12 route bits plus a 20-bit signed immediate, and the immediate is what
caps the fixed-point scale (`4S² ≤ 524287`) — so the decoder hangs a second
read port off `pc_select`, the fetch-address net, and reads a private ROM2.
One PC, one fetch address, every jump offset unchanged. ROM2 rides a private
net, so unlike ROM it shadows no memory row, and a scalar-only slot simply has
no ROM2 row: word 2 reads as absent, every field is 0, no gate matches.

```
bits  0..5   R      mux read-select     latched — a park persists
bits  6..11  S      second read-select  latched
bits 12..16  W      commit register     one tick, by construction
bits 17..21  erase  zero register       one tick, same tick as W
```

**The decode depths ARE the timing rule.** The bank's park-then-commit rule
(select, let the bus settle, then pulse W) is met by building the W/erase
chains one stage deeper than R/S, so both halves of a move ride one word and
still land a tick apart:

| Rule | Constant | Evidence |
|---|---|---|
| R/S of the word at slot n reach the control plane at slot n+2 | `DEC_SEL_SETTLE=2` | mask, collapse, latch |
| W/erase at slot n+3 — one tick later, always | `DEC_WRITE_SETTLE=3` | mask, shift, collapse, relabel |
| settle is the same quantity a write reaches at `value_slot + WRITE_TO_CELL`, so the whole `_VEC_REACH_BY_NAME` table applies unchanged | | v10_vec_decoder (7/7), and both mandelbrots identical |
| back-to-back dependent moves: 4 slots, the `vec_wsel → VEC_BUS` latest depth (through the square block) | | mandelbrot loop, 11 moves in 43 ticks |

R and S are **latched** and W and erase are **not**, which is exactly the
park/pulse distinction the memory-mapped path had to spell out. A park has to
persist — a lane read walks the bus with port A long after the instruction that
steered it — so a scalar instruction leaves R/S standing. A commit must be one
tick, and it is: the next instruction's field is zero. **That is the entire bug
class `check_pulse_widths` exists for, gone by construction.** Keep the check
anyway; the memory-mapped path is live hardware and a design without a decoder
still drives the zone that way (`IR8.vec_move(..., via="memory")`).

Measured on `v10_proc_mandelbrot_dsl`, which produces **byte-identical
expectations** on both paths — all 38 of them, same picture, same numbers:

| | memory-mapped | decoder |
|---|---|---|
| loop body | 171 ticks/pass | **43** |
| per move (11 moves/pass) | 15.5 | **3.9** |
| whole program | 535 slots, 473 ROM words | **238 slots, 152 ROM + 28 word-2 rows** |
| ticks to halt | 5119 | **1571** |

What is left in the 43 is the zone's own depth, not the control plane: a move
whose source is a plain register only needs 2 stages, but the scheduler applies
the 4 the square block can need to every move. Making that depth source-aware
is the next thing worth doing here and is recorded in `plan/ROADMAP.md`; it has
not been measured, so no number for it is claimed.

Unmeasured / untested: `a->out4` (conditional/computed relative jump),
`b->out4`, P=0 mid-jump behavior (suspected self-healing: cell emits nothing at
0, autoincrement re-seeds next tick), port-A park from a port source
(`a/b->out3`), mem+mem summation (`a->x + b->x` same slot).

## Compiler: machine_v8.py (IR8 → schedule8 → assembler.Program → ROM)

Ops: `write_imm(addr,val,warm=)` (refuses accumulate cells), `add_imm` (only
accumulate cells), `pulse_imm(addr,val)` (holds a row for exactly ONE tick
then zeroes it — two writes placed atomically on consecutive slots; the write
path's pipelining makes the pulse free, and atomicity is required because
independently-scheduled writes widen once neighbouring stages compete for the
slot), `copy_a(src,dst)` (parks A automatically, REUSES a live park
on the same address), `park_b(src)` / `copy_b(dst)`, `jump_rel(label)` /
`jump_if_zero(flag,label)` (backward only), `halt()`, `barrier()`, `label()`.
Stage semantics: within a stage reads sample PRE-stage state (anti-deps keep
consumers ahead of same-stage writes); `barrier()` commits; jumps end stages.

Hard-won scheduler rules (each cost a live debugging session — don't relearn):
0. **The port-B stream needs a CROSS-STAGE anti-dependency.** `stage_reads` is
   cleared at every barrier, which is enough for consumers placed
   monotonically and not enough for the stream, whose value is whatever its row
   holds AT THE READ TICK. Measured 2026-07-28 in `bus_summation`: an index
   bump three stages later was placed at an EARLIER slot, became visible one
   tick before an already-placed indexed store sampled the index, and the store
   went to `arr[0]` instead of `arr[3]` — silently, with the right value at the
   wrong address. `_Sched8._stream_consumers` keeps every stream read and
   `_write_floor` keeps later writes invisible to them.
1. **A loop label re-enters at the body's MIN footprint slot** (a copy's park
   sits 4 slots before its consumer anchor). Binding to an op anchor skips the
   park → the copy never re-executes after pass 1.
2. **`label()` floors placement past every earlier op's LAST footprint slot.**
   A prelude value slot captured inside the loop pairs the PREVIOUS pass's
   latch → measured: spin counter gained +2/pass.
3. Greedy is in-IR-order: put **stream/ALU reads first within a stage** so a
   copy's write doesn't box them in (anti caps the read at pending_v + 2).
4. Write-address latch model identical to v7 (pair times, interleave checks).

Tests: `test_machine_v8.py` (15 — five pin the v10 vector rules, all inert
on v8), plus `test_verifier.py` (10) / `test_scheduler.py`
(7) for the archived v7 ISA. Live gate: `proc_v8_fib_cmp_v8` (auto-scheduled
fib: 26-tick loop, correct finals, halt-poison canary), `proc_v8_spin_counter`
(jump_rel loop, exactly +1 per 8-tick pass).

## Workflow + gotchas

- BP paste in chat → save to `modules/processor_vN.bp.txt` → `tools/import_master_bp.py`
  (finds entities by description; clears ROM + stimulus; signal_table markers on
  the 4 address tables; populates config combinators; strips named groups;
  `--backup` the old master).
- **Named logistic groups are poison in pasted sources**: a grouped section
  resolves against the FORCE's shared registry (inline filters ignored) and a
  testbench driver writing it edits the group force-wide and persistently
  (survives clear+paste — the accumulate mask vanished server-wide mid-suite).
- If a bench drives `trig_reset` (19), the drive replaces its filters for that
  paste — include the −1 mask entries in every drive map if accumulate cells
  matter in that bench (current benches avoid this).
- `signal_space.py`: 2480 addresses. 76 census-discovered virtuals appended
  AFTER the ALU block (2101–2480) so all pre-census addresses are stable
  (P=2096, heart=206, T=2091). Append new signals to the END of
  `APPENDED_VIRTUALS` only. `shape-curve-2` = 2116.
- v7 programs are NOT v8-compatible: `const->addr_b` (out4) is now a PC
  injection; port-A pulses became parks. hello/array/zerow (write-path only)
  carried over; the fib family was rebuilt as IR8.
- Factorio single-instance lock: the headless server can't boot while the
  user's client is open (and vice versa) — wait, don't kill.

## Suite (14)

memory_basic; open-loop: proc_memory_raw (direct set/value/reset writes +
zero-write), proc_write_encoder, proc_decoder_matrix, proc_cmp; write-path
programs: proc_hello_world, proc_array_write, proc_zero_write
(`tools/build_closed_loop.py`, v8 ids); v8: relative_jump, latched_read,
accumulate, mmap_port_b, fib_cmp_v8, spin_counter (`tools/build_v8_tests.py`).

## Pipeline utilization (analysis 2026-07-13)

fib-class loops: UNCHANGED (26-tick period, ~40% slot occupancy in both v7 and
v8) — the critical path is cell/ALU/jump latency, not port plumbing. Improved:
minimum loop period 13→5-8 (`jump_rel`), counters ~6× (`add_imm` vs ALU round
trip), hot-operand reads free after one `park_b`, no +3 alignment padding.

## What is not built

Every forward-looking item that used to live here — conditional jumps and
forward labels, delay-slot filling, operand-cell allocation, a standalone ROM
linter — moved to `plan/ROADMAP.md` in the 2026-07-28 split. This file records
the machine as it IS.
