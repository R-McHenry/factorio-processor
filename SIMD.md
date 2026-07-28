# SIMD extension + radar IO — design (brainstorm state, 2026-07-15)

Companion to V8.md. Nothing here is built or measured yet; decisions marked
DECIDED are locked by the designer, the rest is proposal. Update this file as
the design settles.

## Core insight

A Factorio wire is a ~2480-lane vector (every signal × 5 qualities), and the
v8 memory cell already IS a vector register — the scalar machine is a SIMD
machine plus one-hot lane masking (the write encoder). The SIMD zone is the
machine with the mask taken off.

## SIMD zone (local, tightly coupled — NOT behind the radar)

- **Vector registers** = clones of the v8 cell (`each[R]≠0 AND each[G]≤0 →
  each[R]`, red self-loop) + v8-style set/value/reset write machinery.
  Whole-register reset needs an **all-ones constant** (every signal = 1) gated
  by the commit pulse — toolchain-populated like the address tables.
  - Reset-blocked variant = **vector accumulator** (2480-lane MAC).
- **VALU farm, always-on** (the scalar ALU idiom — functions compute
  continuously, programs choose what to commit): `each[R]+each[G]`,
  `each[R]−each[G]`, `each×each`, `each×N` / `each+N` (scalar broadcast, N
  from the scalar core), mask/predication deciders
  (`each[R]≠0 AND each[G]≠0 → each[R]` = select lanes where mask reg nonzero).
- **Reductions, always-on, port-readable**: `each+0→signal-X` sums ALL lanes
  in one tick (one combinator); decider match-count = popcount; `each×each`
  chained into a sum = **dot product in ~3 ticks**. Outputs join the scalar
  green bus at designated signals → read like CMP flags. Horizontal max: no
  native form (iterate or log-tree of paired deciders) — open.
- **Routing = own sequencer (DECIDED 2026-07-15)** — memory-mapped routing was
  rejected as too slow (~10 scalar ticks per vector op, scalar core blocked).
  The SIMD zone gets its own program memory and program counter (attached-
  array-processor / Amiga-Copper shape), issuing 1 vector op/tick:
  - **vFetch** = vPC cell + own autoincrement + own address-table copy + read
    decider (~5 combinators, scalar fetch stage cloned).
  - **Vector word (32b, no immediates)**: bits 0–7 A-select (one-hot, 8 regs),
    8–15 B-select, 16–23 W-select, 24–28 result source (always-on VALU output
    to commit: add/sub/mul/mask/rx-frame/mem-frame), 29 commit, 30
    signal-scalar (pulse a done-flag lane onto the scalar green bus), 31
    format flag → word is a vjump (bits 0–23 target/offset, pc_inject idiom).
  - Decode cost ≈ 24 bit-test ariths + 24 gate deciders (comparable to the
    scalar switch matrix).
  - **Sync primitives**: (1) run/stop enable cell, scalar-writable, gates
    vPC's autoincrement — kick = write vPC entry + vEN=1; routines self-halt
    (clear vEN or self-spin vjump); (2) done-flag lane, scalar polls like a
    CMP flag; (3) **free-running mode** — vector program loops forever
    (rotating D-selects, maintaining shortage masks/reductions); scalar just
    reads always-current reduction lanes. Preferred for monitoring workloads.
  - Immediates: none in the word — vector constants preloaded into registers
    by the scalar core; scalar-broadcast operands (each×N) ride a designated
    lane the scalar core writes.
  - vPC writes from the scalar side via pc_inject-style injection (relative
    vjumps for free) rather than full write machinery.
  - Compiler phasing: v1 = standalone vector assembler + hand routines,
    scalar kicks/polls via existing IR8; v2 = machine_v8 schedules both
    streams (wants the tick simulator first — two free-running streams).
- **Scalar↔vector**: whole-frame `mem → Vk` copy through one gate = 1 tick;
  lane extract = one-hot × Vk (the write-encoder product stage re-aimed);
  optional bank-select gate to alias a Vk's lanes onto the port-read bus.
- machine_v8 grows IR ops (`vadd(a,b,dst)`, `vmask`, `vcommit`,
  `vload_mem(dst)`, …) that compile to existing write_imm's + settle constants.

## Radar IO

Radar relaying = two independent global channels per surface (all radar reds
one network, all greens another). We build every remote end; latency is fixed
(designer: 1 tick, possibly 0 — bench to confirm the constant).

DECIDED (2026-07-15):
- **Green = the CPU scalar/control plane, duplex by lane partition.**
  Radar-green ties into the memory green OUTPUT bus (the one port deciders
  read). Consequences:
  - The world sees CPU memory: peripherals decode commands/addresses straight
    off broadcast cell lanes. Device select = an ordinary cell (`D` on a
    designated IO lane); "call out an address on the bus" = one write_imm.
  - **Peripheral replies land as port-readable memory**: a remote's gated
    output on lane X sums onto the bus → scalar `read(X)` returns it. Remote
    sensors are memory-mapped registers; NO new scalar ISA for scalar IO.
  - A remote end is 2–3 combinators: select decider (`D == my_id →
    everything`), transmitter gate, optional converter.
- **Red = the vector/data plane**, half-duplex bulk frames arbitrated by the
  green control plane (whoever D selects owns red): peripheral→CPU dumps
  (chest/roboport item frames, native) commit into an RX vector register;
  CPU→peripheral pushes via a Vtx gate (train-loader payloads).

Lane discipline (convention, enforce in the compiler):
| Lanes | Owner | Notes |
|---|---|---|
| low signal lanes (ROM region) | CPU exclusive | the PC FETCHES through this bus — remote writes here corrupt instruction fetch; hard keep-out |
| general cells + ALU/P block | CPU exclusive | remote writes sum into program state |
| appended virtuals 2101–2480 (380 lanes) | IO plane | D select, command lanes, peripheral reply lanes |
| item signals | payload | native for chests/roboports; red frames + disciplined green use |

Isolation facts (verified from v8 wiring): ALU operands come from the RED
internal bus (cell red-out) — remote green lanes cannot perturb computation;
values must be explicitly copied into cells first. Internal red buses never
touch radar-red.

Hazards to design around:
- Sums are the collision model. Uplink discipline = exclusivity of D. During a
  D switch, 1–2 ticks of mixed frames: selected device echoes its id on a
  reserved lane; CPU polls the echo before trusting reads / committing red.
- The CPU broadcasts its ROM lanes (harmless — remotes only gate their own
  lanes — but never assign IO meaning to low lanes).
- A peripheral reply lane whose CPU cell is nonzero reads as the sum: keep IO
  block cells at 0, or exploit deliberately (free offset/bias).

## Example flow (outpost resupply, end to end)

1. `write_imm(D, outpost_7)`; poll id-echo lane.
2. Commit radar-red → Vrx (outpost's chest frame, item lanes, native).
3. `Vrx − Vthreshold` (preloaded) → shortage mask; popcount reduction says how
   many item types short; scalar branches on it like a CMP flag.
4. Commit shortage vector → Vtx; `write_imm(D, loader_3)` → train loader reads
   its payload from red and loads.
Per-item scalar work: zero.

## Open questions

- Vector register count (each ≈ 2 combinators + selection compare; 8 proposed).
- RX register: hardwired to radar-red vs commit-gated (gated proposed, safer
  during D switches).
- Whether the CPU's Vtx also drives red permanently-gated-by-D or needs its own
  enable cell.
- Horizontal max strategy.
- Exact IO lane map inside 2101–2480 (D lane, echo lane, command block).

## Measurement benches before the compiler learns constants

0. vFetch stage: vector word fetch→decode→commit latency; vjump shadow length;
   vEN gate (freeze/resume) semantics; scalar→vPC injection constant.

1. Radar relay latency, both colors, both directions (two radars far apart on
   the test surface; driver constant on one, probe on the other). Confirm
   red/green channel isolation.
2. Frame-move gate: 1-tick full-frame copy incl. negatives + quality lanes.
3. Vector register write with all-ones reset; accumulate variant (MAC).
4. Config-settle: selection-cell write → gate stable (predict cell-land +1).
5. Reduction correctness (sum incl. negatives; popcount; dot product) +
   port-readback of reduction signals.
6. UPS sanity: several 2480-lane registers + always-on each×each farm.
7. End-to-end: mock outpost (constant behind a select gate) on a second radar,
   full example flow above.
