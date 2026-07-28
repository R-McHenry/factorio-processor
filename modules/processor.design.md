# Processor design notes

Source of truth: `modules/processor.source.json` (decoded blueprint v3, 60 entities, 89 wires).
Architecture as explained by the designer, cross-referenced against the decoded combinator logic.

## Big picture

Fully pipelined, full-rate design: no multi-stage instruction decode. Each instruction
directly encodes the state of the 3×4 switch matrix for that moment; the compiler/bytecode
layer is responsible for turning virtual instructions into sequences of matrix-state
instructions. Latency of every path = number of combinators in the path (1 tick each),
memory included.

## Memory (v7 — set/reset trigger write, zero-write capable)

- The memory cell is a **single decider combinator** (entity 34, v7 numbering):
  `IF each[R] > 0 AND each[G] = 0 → each[R]`, red output looped back to red input
  (hold). **No else branch** (v6 had one): a nonzero signal on the green input now
  *clears* that signal's hold instead of substituting a value — that is what makes
  writing 0 possible. Output greens feed the read ports; output red feeds the ALU
  input bus.
- **Write mechanism** (three inputs instead of the old write_raw):
  `write_trigger_set` (15, red into injector 21), `write_value` (16, green into 21),
  `write_trigger_reset` (18, green into the cell). The injector decider 21
  (`anything[R]>0 → everything[G]`) has its output connectors tied to the cell's
  output connectors: while the set trigger is present it feeds the value onto the
  cell's red hold loop, while the reset signal on the cell's green input drops the
  old held value. Release both → the loop re-latches whatever is on it, **including
  nothing (= 0)**. The reset signal's type must match the signal being cleared;
  set/reset conventionally carry 1.
- **Address/value encoder** (write path, same input timing as v6): `write_adress`
  (1) → arith 2 (`each+0→N`) → decider 3 (latch, red self-loop holds the address) →
  decider 8 (`each[R] = N[G] → each=1[G]`, one-hot vs `addres_table` 6). The one-hot
  splits into two products: arith 14 (`each[R] × N[G]`, value from `write_value` 4 →
  arith 9) feeding the injector's value input (16), and arith 13 (`each[R] × W[G]`)
  feeding trigger set (red out → 15) and reset (green out → 18). **W comes from the
  matrix select, not the data**: the three out2 bit-test combinators (AND 2/32/512)
  chain their green outputs into a select bus → decider 5 → decider 10 → `W=1`.
  Any instruction that routes to write_value fires the trigger even when the value
  is 0 — that is the ISA-level zero-write. Trigger and value paths are both 2
  combinators deep from the bit test, so they meet at the injector in step.
  (v7.0 bug found live: decider 5 tested its unwired red input instead of green —
  values flowed, triggers never fired, no ROM-driven write ever landed. Fixed in
  v7.1, `modules/processor_v7.1.bp.txt`.) **Write address latches and holds
  indefinitely**; the triggers pulse per write instruction.
- **Three read ports**: address-compare deciders `each[R] = R[R] → each[G]`
  (52 = port A, 53 = port B, 54 = program), each fed by an `addres_table` copy
  (41/42/43) and an address register (`addr_rd_a` 22 → arith 35, `addr_rd_b` 23 →
  arith 36, PC → arith 37 `P+0→R`). Outputs land on `port_a_rd` (59),
  `port_b_rd` (60), `intruction_rd` (61).
- The v7 master (`modules/processor_v7.bp.txt` → imported via
  `tools/import_master_bp.py`) ships **closed-loop**: the four matrix_out→input
  wires are green and the program-port data wire is included. Open-loop benches
  rely on the master ROM being empty (cleared at import). v6 kept as
  `modules/processor_v6.source.json`.

## ALU + ROM (shared address space / summing bus)

- Memory red output, `ROM` (32), `pc_autoincrement` (21) and the ALU all sit on shared
  red-input / green-output summing buses, so ROM and ALU registers occupy one address
  space. Planned: address map grows to thousands of entries; ALU addresses move to the
  high end, away from program ROM (stored from low addresses up).
- ALU ops (24–27) on fixed signal triplets:
  `SUB: J−K→L`, `ADD: G+H→I`, `DIV: D/E→F`, `MUL: A×B→C`.
- **PC**: memory-mapped autoincrement — `pc_autoincrement` (21) holds `signal-P=1` on
  the ALU/memory input bus; arith 31 (`P+0→R`) turns the running P into the program
  read address. Always on, full rate. Writing the PC's memory location = jump.

## CMP unit (v6)

- Four deciders + two arithmetics beside the ALU, on the same red-in/green-out buses.
  Inputs M, N (memory-mapped cells); outputs on the green bus:
  `O = 1 if M > N` (1 tick), `Q = 1 if M ≥ N` (1 tick), `T = max(M,N)` and
  `S = min(M,N)` (2 ticks — select decider plus an `each+0→T/S` arithmetic).
  O/Q together distinguish >, =, <: (1,1) greater, (0,1) equal, (0,0) less.
- All ALU ops live in `ALU_MAP` in `signal_space.py` (operands, results, latency);
  the address-table ordering derives from it. T is port-readable at its address.
- **History**: v5 used signal-R for max and had Q duplicate O's `M>N` condition.
  The R choice caused a measured carrier leak — the port-read deciders compare
  `each[R]=R[R]` and the carrier always self-matches, so every port read emitted
  `signal-R = max` whenever CMP output was nonzero. v6 keeps signal-R strictly as
  the read-address carrier (never on the green bus → no leak, stays excluded from
  the table) and moved max to signal-T. The v6 changes were made in the source
  first (`modules/processor_v6.bp.txt` is the import string for the game); the
  file is master now that the in-game BP is unwieldy.
- **Zero-write**: still deferred; plan is a memory-mapped zeroing function
  (write to a dedicated address that clears a target cell) rather than a value
  sentinel.

## Instruction decoder / 3×4 routing matrix

- Instruction value arrives on `intruction_rd` (49) green from the program read port.
- **Bits 0–11 are the matrix state**; 12 decoder columns, each a pair:
  arith `each AND <2^n> → each` (bit test on the instruction) + decider
  `anything[R]>0 → everything[G]` (switch passing its data input to its output rail).
- Entity 60 (`each >> 12 → each`) is the **constant decoder**: bits 12+ form a ~20-bit
  immediate, fed as the data input of the constant switch column.
- Switch map (bit → route):

  | Bit | 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 | 256 | 512 | 1024 | 2048 |
  |---|---|---|---|---|---|---|---|---|---|---|---|---|
  | Source | portA | portA | portA | portA | portB | portB | portB | portB | const | const | const | const |
  | Dest | out1 | out2 | out3 | out4 | out1 | out2 | out3 | out4 | out1 | out2 | out3 | out4 |

  (deciders 14/19/22/33 = port A column, 38/40/45/50 = port B, 52/54/56/58 = constant;
  output rails are shared via green-out to green-out wiring.)
- **Matrix outputs** drive: `matrix_out1` → `write_adress`, `matrix_out2` → `write_value`,
  `matrix_out3` → `addr_rd_a`, `matrix_out4` → `addr_rd_b`.
- ⚠️ The matrix_out→input connections are deliberately wired in **red** (the wrong
  color): they document the intended green hookup while keeping the feedback loop
  broken so the parts can be tested open-loop.

## Named constant combinators (entity numbers, v7)

| Entity | Name | Role |
|---|---|---|
| 1 | `write_adress` | memory write address input (latching) |
| 4 | `write_value` | memory write value input (encoded path) |
| 15 | `write_trigger_set` | direct write: enables the injector (red into decider 21) |
| 16 | `write_value` | direct write: value into the injector (green into 21) |
| 18 | `write_trigger_reset` | direct write: clears the old hold (green into cell 34; signal type must match) |
| 6, 41, 42, 43 | `addres_table` | address decode tables (write, port A, port B, program) |
| 7, 11, 12, 17 | `matrix_out1`–`4` | routing-matrix output taps |
| 22 | `addr_rd_a` | port A read address register |
| 23 | `addr_rd_b` | port B read address register |
| 24 | `addr_rd_pc` | program counter read address tap |
| 27 | `pc_autoincrement` | PC increment constant (signal-P = 1) on the summing bus |
| 38 | `ROM` | program ROM (shared summing bus) |
| 59 | `port_a_rd` | port A read output / matrix data input |
| 60 | `port_b_rd` | port B read output / matrix data input |
| 61 | `intruction_rd` | instruction read output → decoder + constant (>>12) |

(v6→v7 renumbering: cell 28→34, ports 47/48/49→59/60/61, ROM 32→38, tables
5/35/36/37→6/41/42/43, matrix outs 6/9/10/13→7/11/12/17, addr_rd 16/17→22/23.)

## Full address space (verified live 2026-07-12)

- `signal_space.py` defines the canonical 2100-slot address space: every usable signal
  × 5 quality levels, qualities interleaved fastest. Wildcards, `signal-R` (reserved
  read-address carrier) and blueprint parameters are excluded; the 13 ALU/PC signals
  (`signal-A`–`L`, `signal-P`) sit at the end (P = addr 2096), away from program ROM
  at low addresses. `signal-heart`/normal = addr 236.
- Source entities carry `"signal_table": {"table": "full_address_space"}` instead of
  2100 inline filters; the runner expands it post-paste via chunked Lua `set_slot`
  writes (all 4 tables: 2100/2100 slots accepted, quality-variant virtuals included).
- **Write-zero**: resolved in v7.1 — the write trigger comes from the matrix out2
  select, not the value, so an instruction with `const->write_value` and imm 0
  clears the target cell (verified live: proc_memory_raw direct-drive +
  proc_zero_write ROM-level).

## Verified closed-loop execution (proc_hello_world)

- `tools/build_closed_loop.py` generates `modules/processor_full.source.json`:
  the four red matrix_out→input wires become green. The program-port data wire
  (decider 43 green-in → decider 44 green-in) was originally missing from the master
  BP — without it `intruction_rd` reads 0 forever; the generator patched it in for the
  first hello-world run, and the master BP (v4) now includes it in-game.
- Hello world ROM (assembled by `assembler.py`): addr 1 `const->write_addr imm=236`,
  addr 2 NOP (address path is 2 combinators deeper than the value path), addr 3–7
  `const->write_value` with 'h','e','l','l','o'.
- Observed: instruction fetch latency 2 ticks from PC; fetch→write-bus latency 4 more
  ticks; memory latches 1 tick later; port readback +1 tick. **Throughput is 1
  write/tick** — all five letters landed on consecutive ticks (104,101,108,108,111)
  and 'o' (111) holds indefinitely. Fully pipelined as designed.

## Offset array write (verified: proc_array_write)

- Park port A on the PC's own slot (signal-P, addr 2096) → `port_a_rd` streams the
  live P value. Routing `a->write_addr` makes the write address track the PC; adding
  `const->write_value` per instruction streams data. Consecutive instructions write
  **consecutive addresses** — an array write with no address per element, courtesy of
  the additive bus + free-running PC.
- **Compiler timing rules (measured)**:
  1. The value of instruction *n* lands at the address latched by instruction *n−1*
     (value path reaches the write multiplier one instruction before that
     instruction's own address does).
  2. A write burst loses its first two values to address-latch pipeline fill —
     prepend two priming `a->write_addr`-only instructions.
  3. With priming at ROM addr 1–2 and values at 3..7, letters land at addr 2..6.
- Trace keys are quality-aware: non-normal quality slots appear as
  `<signal>~<quality>` (e.g. `signal-0~rare`) in probes and `expect` maps.
- `save_after_run: "<name>"` in a tb saves the post-run map (e.g.
  `claude_processor_test.zip`) for in-game inspection.

## Conditional jump + loop (verified: proc_fib_loop)

- **Jump** = write the loop-start PC value to the PC slot (signal-P, addr 2096).
  Resume is clean: fetch reflects the new PC 7 slots after the jump's value slot,
  so a jump has **6 delay slots** that still fetch and execute (fill with NOPs).
- **Conditional bypass** (DIV as threshold comparator): F = D/E is 0 below the
  threshold and >=1 above. Routing `b->write_addr + const->write_addr imm=2096` in
  one instruction makes the jump's write address `2096 + F` via the additive rail —
  the PC slot while F=0 (jump taken), a harmless P-quality-neighbor slot once F>=1
  (jump bypassed). No MUL scaling needed while F stays small.
- **Fibonacci demo**: cells G=prev, H=curr, ADD computes I=G+H continuously;
  per pass: G<-H, H<-I (port A/B copies), D<-H, F=D/E steers the jump.
  Ran 11 passes (H: 1,2,3,5,8,13,21,34,55,89,144), exited at 144 >= 100, wrote the
  done marker. Loop period 26 ticks. Save: `claude_fib_test.zip`.

## Compiler scheduling rules (measured)

1. A const write ADDRESS at slot n pairs with the write VALUE at slot n+2 — the
   address path (arith->latch->one-hot) is 2 slots deeper than the value path.
   (A port-sourced address is 1 slot shallower: it pairs with the value at n+1.)
2. A port read pulse (`const->addr_a/b` at slot p) is live for exactly one tick;
   its consumer (`a->...`/`b->...` route) must sit at slot **p+3**. Port addresses
   are NOT latched — only the write address latches.
3. A PC write's value slot n takes effect at fetch n+7 (6 delay slots).
4. **Read-after-write hazard**: a cell write lands ~5 slots after its value slot;
   the ALU adds 1 more slot. Reading a derived ALU output (e.g. I=G+H) while one
   of its operand cells is mid-update returns a transient (the fib D<-I race read
   I=H+H). Read sources only after their inputs have settled, or read the cell
   itself post-update.
5. A cold write-address latch pipeline drops leading values — warm it with 2-3
   address-only instructions in the program prelude.

## v8 (measured 2026-07-13 — **suite master** since the same day; v7 archived as `modules/processor_v7.source.json`)

The v7-ISA fib programs (port pulses, `const->addr_b`) are not v8-compatible —
on v8 every out4 route is a PC injection. The fib lineage continues as
`proc_v8_fib_cmp_v8`, scheduled by `machine_v8.py` (IR8: `write_imm`/`add_imm`/
`copy_a`/`park_b`/`copy_b`/`jump_rel`/`jump_if_zero`/`halt`, staged parallel
semantics, port-A park intervals, jump shadows, label floors). Compiler
subtleties found live:
- a loop label re-enters at the body's MIN footprint slot (parks/addr slots
  precede a consumer's anchor), and must floor PAST every prelude footprint —
  a prelude value slot captured by the loop pairs the previous pass's latch
  (measured: spin counter gained +2/pass).
- **Named logistic groups are poison in pasted sources**: a grouped section
  resolves against the FORCE's shared registry (inline filters ignored), and a
  testbench driver writing such a section edits the group force-wide — the
  accumulate mask vanished server-wide after proc_memory_raw drove
  trig_reset. `tools/import_master_bp.py` inlines all groups as local sections
  (v8 BP had two: reest_mask on 19, mmap_addr on 59).

Circuit changes (BP `modules/processor_v8.bp.txt`, benches `tools/build_v8_tests.py`,
all 4 measurement benches green live):

- **Latched port A** (`a_rd_latch` 23): out3 address persists until replaced.
  Measured: data valid on `port_a_rd` **one tick later** than the old pulsed path
  (consumer slot ≥ addr_slot + 4 vs == +3), then holds indefinitely. The +3
  pulse/consumer alignment constraint is gone for port A.
- **pc_inject** (out4 → `each+0→P` (40) → cell red bus): anything routed to out4
  sums into the PC for one tick. Measured with an accumulate-cell instruction
  counter: inject at ROM slot n → **4 delay slots** (n+1..n+4 execute), skipped
  count = offset exactly, fetch resumes at **n + offset + 5**. One instruction
  replaces the old 5-slot + 6-shadow write-path jump. Inject −1 = HALT (untested).
- **Memory-mapped port B address** (`mmap_addr` 59 masks the green bus with
  shape-circle=1 → decider 53 → `addr_rd_b`): port B's read address is the VALUE
  of the shape-circle cell. Measured: register cell update → port B data = **+3
  ticks**, streams until re-parked.
- **Accumulate cells** (reset-block): `write_trigger_reset` (19) holds a constant
  −1 for shape-vertical/horizontal/curve/curve-2 and the cell condition is
  `each[G] ≤ 0`, so the +1 reset pulse sums to 0 → no reset → writes ADD.
  Measured: 5, 5, −3 → 7; plain cells still replace; one add per write
  instruction (a 1/tick stream counts cleanly). P is unmasked → absolute jumps
  preserved. Caveat: benches must not drive `write_trigger_reset` without
  re-including the −1 mask entries (a dedicated mask combinator is planned).
- **Negatives**: cell/injector/latch conditions are `≠ 0`; −9 through the
  encoder holds correctly.
- **Signal space**: live prototype census added 76 missing virtual signals
  (`APPENDED_VIRTUALS`, addresses 2101–2480) after the ALU block so every
  existing address is unchanged; `shape-curve-2` (4th accumulate register) is
  addressable at 2116.

## Test strategy (modular, open-loop first)

1. **proc_memory_raw** — drive `write cmd` directly, probe memory state and read ports. ✅
2. **proc_write_encoder** — drive `write_adress` + `write_value`, probe the encoded
   write-raw product (arith 11 output / `write cmd` green). ✅
3. **proc_decoder_matrix** — drive `port_a_rd`, `port_b_rd`, `intruction_rd` as inputs,
   probe `matrix_out1`–`4`. ✅
4. **proc_hello_world** — closed loop, full address space, ROM program. ✅
5. **proc_array_write** — PC-indexed offset array write via the additive bus. ✅
6. **proc_fib_loop** — ALU round trip, mem-to-mem copies, DIV comparator,
   conditional PC-write jump, 11-pass loop. ✅
7. **proc_cmp** — CMP unit: O/Q flags, T=max/S=min, port-read of T, no carrier leak. ✅
8. **proc_fib_cmp** — fib with the exit steered by CMP's Q flag (M ≥ N) instead of
   DIV; also exercises the runner's fast-forward mode. ✅
9. **proc_fib_cmp_auto** — same semantics, but slots/ports/loop target assigned by
   `scheduler.py` from a staged IR (no literal slot numbers); gated by `verifier.py`. ✅
10. **proc_zero_write** — ROM-level zero-write on the v7.1 memory (write 42, clear
   with imm 0, canary writes before/after). ✅
11. Next: display output device, tick simulator (SCHEDULER_PLAN.md Phase 4).

## Runner notes

- Programs are built with `assembler.Program` (slot scheduler): `write_imm`,
  `copy`, `jump_offset`, `warm_latch`, `alu_ready_pulse` encode the measured
  timing rules; slot merging with imm-conflict detection allows interleaving.
- Testbench steps accept `"skip_ticks": N` — after the step's drives are written,
  the runner advances N ticks in one unsampled burst (fast-forward), then resumes
  gap-checked single-tick tracing. Use it to run long programs to completion
  (proc_fib_cmp: 43 sampled ticks instead of ~345).
