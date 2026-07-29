# v10 processor — design draft (started 2026-07-21)

The narrative companion to the `.fnet` sources, which are the build:
`modules/v10_addr_map.fnet` (**the** authority on addresses — every vector
control value and reduction output is a real `memory[...]` row),
`modules/lib/v10.fnet` (templates + the colour discipline), and the five live
components `v10_vec_reg` / `v10_vec_bank` / `v10_op_farm` / `v10_vec_io` /
`v10_vec_decoder`.

The Python generator that preceded them — `v10_address_map.py`,
`generate_v10_processor.py`, `generate_v10_vector_zone.py` and their
blueprints — is **archived under `archive/v10_generator/`** and is not part of
the build. It never got the register bank working; see the status note below
and that directory's README. Its one surviving idea is the
carrier-obscurity ranking, reimplemented as `netlist.auto_signal_pool()`.

Cross-reference: V8.md (scalar core being extended), SIMD.md (superseded —
the separate vPC/vFetch/vector-word design is replaced by v10's single
unified PC; the VALU-farm idioms it describes carry forward).

**Status (2026-07-27): the zone is COMPLETE ENOUGH TO COMPUTE — the whole
machine runs a mandelbrot kernel over all 2451 lanes.** v10 is expressed as
`.fnet` modules with per-component testbenches (the method that took the v8
rebuild to 25/25); `tools/generate_v10_processor.py` and its `WIRE_TEMPLATE`
are superseded. Live:

- `modules/v10_addr_map.fnet` — `region vec = 2130`: every vector control
  value and reduction output as a memory-mapped row.
- `modules/v10_vec_reg.fnet` — the **one-combinator** vector register,
  **16/16** (`testbenches/v10_vec_reg.tb.json`).
- `modules/v10_vec_bank.fnet` — **12** registers + write heads + the read mux,
  **34/34** (`testbenches/v10_vec_bank.tb.json`), including the free vector
  ADD, the one-tick move primitive, and pair B's opposite operand faces.
  Registers own mux indices **1..16** (four reserved) so ops never renumber.
- `modules/v10_op_farm.fnet` — the always-on op farm: two hardwired vec-vec
  operand pairs (A with MUL/SUB/MAX/MIN/DIV/MOD, B with MUL/SUB and the
  SQUARE block) plus the vec-scal block (MUL/ADD/SUB/DIV/MOD and the GT/LT
  masks against `vec_bcast`), **27/27** (`testbenches/v10_op_farm.tb.json`).
  Mux indices are BLOCKED with reserves — registers 1..16, pair A 17..24,
  pair B 25..32, vec-scal 33..44, generators and I/O 45..52 — so adding a
  unit renumbers nothing, which the first two revisions both failed to do.
- `modules/v10_vec_io.fnet` — the scalar↔vector ports: COORD (a "nothing →
  vec" lane-index ramp), LANE_IN (single addressed lane in), LANE_OUT and
  VRED_SUM (single lane / whole-frame out), plus the frame generators ONES
  and BCAST and the VRED_COUNT popcount, **14/14**
  (`testbenches/v10_vec_io.tb.json`).
- `modules/v10_vec_decoder.fnet` — the vector-zone instruction decoder
  (2026-07-28): a second read port on the fetch-address net pulls word 2 of
  each instruction out of a private ROM2 and drives the four select/erase
  lines directly. **7/7** (`testbenches/v10_vec_decoder.tb.json`). Purely
  additive — every gate ORs it with the memory-mapped row it already tested.
- `modules/v10_processor.fnet` — **the composed machine**, 221 entities: the
  v8 scalar core with the vector zone hung off its memory bus. Two real
  programs run on it: `v10_proc_vector_roundtrip` and `v10_proc_mandelbrot`
  (both built by `tools/build_v10_tests.py`). Neither contains a vector
  OPCODE: a move is a field of the instruction's second word, so `processor.
  isa` schedules them with no new instruction kind in the matrix at all, and
  `IR8.vec_move` remains the only code that knows how a vector action is
  realised.

**THE SQUARE BLOCK** (2026-07-27). The bank exports both faces of each pair-B
register — `a2g` (VREG2 on green) and `b2r` (VREG3 on red) — so a register can
be squared with the ordinary `each[red] × each[green]` shape, no
each-against-itself semantics required. B_SQ (24), B_SQG (25) and B_SQDIFF
(26) then make one pair load yield `zx·zy`, `zx²−zy²` and — since two mux
selections sum — `zx²+zy²`, which is the entire mandelbrot iteration including
its escape test. B_SQDIFF is the zone's only two-deep unit, and that is why
`machine_v8._VEC_REACH_BY_NAME` now carries a `(soonest, latest)` pair per
control instead of one number: the forward rule wants the slowest path and the
anti-dependency rule wants the fastest.

**The mandelbrot kernel is done — `MANDELBROT.md`** records what it computes,
what was added to the machine for it, the four idioms it is written in, and
the three things that bit (absent-vs-zero at the grid seed, the 20-bit
immediate setting the fixed-point scale, the ROM shadowing low memory rows).

**Register moves: erase pulse, then a one-tick W pulse** (designer,
2026-07-27). Holding both `vec_wsel` and `vec_erase` cannot work — leaving
"mirror" mode passes through either *cleared* or *accumulating*, whichever
row lands first. The answer is not to hold W at all. The write path is
pipelined, so writing a row and then zeroing it lands on consecutive ticks;
an erase pulse leaves the register empty and holding, and into an empty
register a single accumulate pulse IS a replace. No atomicity between the two
rows is needed and no hardware changes.

The pulse must be exactly one tick, so `machine_v8.IR8.pulse_imm` places both
writes atomically on consecutive slots. Two independent `write_imm` calls
schedule adjacently in isolation but widen to three ticks once surrounding
stages compete for the slot, and a 3-tick pulse silently commits three
accumulate copies of the vector bus.

This closes the 2026-07-22 open question below. The old failure — a
194-entity generated blueprint that showed no signs of life on any probe —
is now explained: the register hold cells were never configured (six of the
eight still carried Factorio's unconfigured placement default,
`signal-no-entry != N`, as this file already recorded). Given a real hold
cell, the write path works first try. The infrastructure bugs fixed back
then (an RCON hang on large entity dumps, a fixture overlap that left the
processor unpowered) were real but were not the cause.

**Status of the pre-2026-07-27 material below**: the address map, op
inventory, and read-select table were traced from WIP blueprints and remain
a *sketch*. Per the designer, the fnet rebuild proves op *shapes* first and
fills the inventory afterwards.

**Nothing in any pasted BP's signal/quality choices is authoritative.** Every
BP reviewed so far (`modules/processor_v10.bp.txt` main draft, plus a small
patch) is WIP scaffolding: several combinators are still literal Factorio
placement defaults (`signal-no-entry ≠ N` conditions, blank `{}` conditions),
and even the "real" signal choices in the BP are templates standing in for
whatever `v10_address_map.py` ultimately assigns. v10 does not owe v7/v8 (or
the WIP BP) compatibility.

## Datapath (designer-resolved 2026-07-27) — read this before the tables below

The earlier wording ("the two selected frames sum onto one shared vector
bus", alongside vec-vec ops described as `each[R] op each[G]` with two full
vector operands) read as a contradiction: one wire cannot present two
distinguishable operands. It is resolved, and the resolution makes the zone
*simpler* than the sketch:

- **The op farm never touches the mux.** Vec-vec op operands are **hardwired
  to the first two vector registers** — VREG0's red state loop and VREG1's
  green frame. The farm is always-on and needs no selection at all.
- **The 28-way mux feeds the WRITE path only.** R and S each name a source;
  both drive the one shared write bus, so two selections **sum** — which is
  exactly why vector ADD costs zero combinators. `R = S = k` selects k once,
  not twice (the gate ORs).
- **W then picks which register commits.** Registers are accumulate-only, so
  a commit always adds.

**The register is one combinator** (designer, 2026-07-27). An accumulating
memory needs no write state at all: the register is purely a hold loop whose
only condition is the erase test, and **write heads output directly onto that
loop** so wire summation performs the accumulate. Heads are separate
combinators and a loop may carry several — they sum. Consequences worth
knowing:

- **Erase is a real operation.** Writing a register's block id to the
  memory-mapped `vec_erase` row makes its cell emit nothing for a tick and
  the loop empties. This replaces the earlier clear-by-writing-the-negation
  hack.
- **Move = erase + write in the same tick.** The cell emits nothing while the
  head drops the new frame, so the loop's next value is the new frame alone —
  REPLACE, in one tick, no separate erase step. Verified live. This is the
  workhorse of any loop feeding the hardwired VREG0/VREG1 operands.
- A decider could not have expressed erase + write + hold anyway: three
  states, two branches.

**Control is chunk1's green memory bus, wired straight in** (designer,
2026-07-27). W, R, S, erase, the broadcast operand, and every reduction
output are ordinary memory-mapped rows (`modules/v10_addr_map.fnet`,
`region vec = 2130`), so a scalar `write_imm` *is* the vector control action.
There is no mirror, no copy, and no private control wiring anywhere, and
control arrives with **zero added latency** — control and data are in phase,
so a direct frame write drives W and the write bus in the same tick.

An earlier revision of this had the colors the other way round (data green,
control red) and needed a mirror combinator to move the scalar bus across,
costing a tick on every control change. That tick was an artifact of the
colour choice, not a requirement; flipping the discipline removed both the
combinator and the timing rule that came with it.

**And it now has a second driver** (2026-07-28, built —
`modules/v10_vec_decoder.fnet`, measured in `isa.md`). The prediction above
held exactly: the decoder became **another driver of the same select/erase
lines**, one extra condition on gates that already had one, and nothing built
for the memory-mapped plane was wasted. What it dropped was the write path —
a control now arrives as a field of the slot's second instruction word instead
of five ticks after a `write_imm` — taking a vector move from 15.5 ticks to
3.9 and the mandelbrot loop body from 171 ticks a pass to 43, with
byte-identical expectations.

Two details worth keeping here, because they are properties of the zone rather
than of the decoder:

- The **second PC** half of the 2026-07-27 sketch was NOT built and was
  rejected again. Vector compute is combinational and always-on, so there is
  no long-running vector operation for a second sequencer to overlap with; all
  a vector "program" does is sequence moves, and one PC sequences them fine.
  What the decoder fetches is a second WORD at the same address, not a second
  stream.
- The "park R/S, let the bus settle, then pulse W" rule below is now met by
  **construction** rather than by scheduling: the W/erase decode chain is built
  one combinator deeper than the R/S chain, so both halves of a move ride one
  instruction word and still land a tick apart. R and S are latched (a park has
  to persist while port A walks the bus); W and erase are not (a commit must be
  one tick, and the next instruction's field is zero).

**Color discipline** (makes every gate in the zone the same shape): RED is
control carriers only — W, R, S, the broadcast scalar — on the `vctl` net,
plus each register's own private state self-loop. GREEN is vector data:
every register frame, every op output, and the shared write bus. So each
gate reads its condition off red and passes a green frame through, and the
two input connectors never compete. See `modules/lib/v10.fnet`.

**Timing rule (measured live 2026-07-27):** the bus is one tick behind the
carriers, in both directions. Park R/S, let the bus settle, *then* pulse W
for one tick. Changing a selection and pulsing W in the same tick commits
the **previous** selection — the bank bench caught VREG7 capturing VREG0's
stale frame summed with the intended one. Same class as v8's
`PARK_A_TO_USE`.

## Big picture — what changes from v8

- **One unified address space, one program counter.** No separate vector
  program/PC (SIMD.md's rejected-then-reconsidered "own sequencer" idea is
  dropped again, permanently this time — vector *control* is memory-mapped,
  but vector *compute* stays an always-on combinational farm, so a
  memory-mapped issue only costs a write, not a stall).
- **Chunk 1** (addresses 1..2480): scalar space, structurally the same as
  v8's `signal_space.py` table.
- **Chunks 2..9**: one 2480-wide fine-grained block per vector register (8
  registers), addressed the same way chunk1 addresses scalar signals, just
  offset. Total unified space: 22,320 addresses.
- **Vector bus**: a separate, coarse, whole-frame addressing scheme layered
  on top of the fine-grained chunks. Two independent read-port selectors
  (`R`, `S`) each pick one of 28 full-vector sources (8 registers + 12
  vec-vec op outputs + 8 vec-scal→vec op outputs); the two selected frames
  **sum** onto one shared vector bus. A separate write-select (`W`, 1..8)
  picks which register a write commits to.
- **Mixed vector×scalar → vector** ops read their scalar operand off a
  dedicated broadcast carrier (a chunk1 address wired straight in, not routed
  through the vector bus) — same idiom SIMD.md proposed, now concrete.
- **Reductions (vector → scalar)** write straight onto chunk1's own
  scalar-readable space via a dedicated green line — no vector-bus
  involvement, same shape as v8's CMP flags (O/Q/T/S).
- **Standard scalar port A** gains the ability to read addresses beyond
  chunk1 (in progress — see Open questions). Scope for v10: **port A only**,
  a small (~4-combinator) chunk-boundary-select extension; port B and
  instruction fetch stay chunk1-only for now.

## Address map

Generated by `v10_address_map.py`; reproduced here for reference (regenerate
if the module changes — this table is not hand-maintained):

| Chunk | Range | Contents |
|---|---|---|
| 1 | 1–2480 | Scalar space (unchanged shape from v8) |
| 2 | 2481–4960 | Vector register 0, fine-grained |
| 3 | 4961–7440 | Vector register 1 |
| 4 | 7441–9920 | Vector register 2 |
| 5 | 9921–12400 | Vector register 3 |
| 6 | 12401–14880 | Vector register 4 |
| 7 | 14881–17360 | Vector register 5 |
| 8 | 17361–19840 | Vector register 6 |
| 9 | 19841–22320 | Vector register 7 |

## Op inventory

### vec-vec (always-on, `each[R] op each[G] → each`, two full vector operands)

| Op | Underlying op | Entities | Notes |
|---|---|---|---|
| VVEC_XOR / OR / AND / POW / MOD / SUB / DIV / MUL | XOR, OR, AND, `^`, `%`, `-`, `/`, `*` | 74–81 | Combinators still carry the stale copy-pasted label `ALU_MUL` except MUL (81) |
| VVEC_GT_GATE / LT_GATE | `>`, `<` (default) | 70, 71 | `each[R]` where condition holds, else 0 — no else branch, a value-preserving gate |
| VVEC_MAX / MIN | `>`/`<` + else-branch | 72, 73 | **True elementwise max/min**: true case → `each[R]` on RED, else case → `each[G]` on GREEN (else_outputs on the opposite color). Red+green sum once both land on the shared vector bus, so the two branches combine into a real per-lane max/min |

All built live (traced via their read-select wiring to entities 112–115, R=17..20). No dedicated vector ADD — planned to come free from two-read-port bus
summation rather than a combinator (carried over from V8.md TODO #4).

### vec-scal→vec (`each[R] op BROADCAST[G] → each`, one contiguous block)

| Op | Underlying op | Output shape |
|---|---|---|
| VSVEC_SUB / ADD / DIV / MUL | `-`, `+`, `/`, `*` | full vector |
| VSVEC_GT_MASK / LT_MASK | `>`, `<` (default) | 1 where condition holds (mask) |
| VSVEC_GT_SELECT / LT_SELECT | `>`, `<` (default) | lane's own value where condition holds (max/min against the broadcast) |

All built live, one shared broadcast carrier per block.

### vec-scal→scal (reduction: vector combined with a broadcast scalar, then
Factorio sums all matching lanes into one output signal)

| Op | Underlying op |
|---|---|
| VSSCAL_MUL_REDUCE / DIV_REDUCE / ADD_REDUCE / SUB_REDUCE | `*`, `/`, `+`, `-` |

Built live; coexists as a **selectable alternative** to the plain scalar ALU,
not a replacement of it.

### vec→scal (pure reduction, no scalar operand)

| Op | Mechanism | Output |
|---|---|---|
| VREDUCE_SUM | `each+0 → signal` full-lane sum | — |
| VREDUCE_COUNT | selector-combinator, `count` mode — candidate popcount | — |
| VREDUCE_MAX | selector-combinator, `select`, `select_max: true` | `signal-M~rare` |
| VREDUCE_MIN | selector-combinator, `select`, `select_max: false` | `signal-N~rare` |
| VREDUCE_ARGMAX | address-table one-hot decode of VREDUCE_MAX's winning signal | `signal-U~rare` |
| VREDUCE_ARGMIN | address-table one-hot decode of VREDUCE_MIN's winning signal | `signal-V~rare` |
| VREDUCE_CMP | compare-vs-broadcast, `>` / `<` (default) / `=` → 3 flags, CMP-style | — |
| VREDUCE_TIME | selector-combinator, `time` mode — doesn't consume vector inputs, kept intentionally per designer | — |

**Correction from the argmax/argmin patch**: the main WIP BP's entities 167
*and* 169 both showed `select_max: true` — a copy-paste bug, not two real
ops. The patch confirms the intended pair is `select_max: true` (MAX, entity
167 in the main BP / entity 11 in the patch) and `select_max: false` (MIN,
entity 169 needs its flag flipped / entity 16 in the patch shows the correct
config).

**ARGMAX/ARGMIN mechanism**: the patch adds a decoder stage on top of both
selects — an `addres_table` constant (signal→index reference table, same
shape used throughout the write path) feeds a decider testing
`each[G] ≠ 0 → each[R]`: for whichever signal the selector picked (present as
a nonzero lane on green, fed from the select output), pass through the
table's RED value for that *same* signal identity. That substitutes "which
signal won" for "that signal's table address" — i.e. the winning lane's
*index*, not just its value. Same one-hot-match idiom v7/v8 use for write-
address decode, just run against a selector's output instead of a write
address. A final `each+0 → signal-U/V(~rare)` relabels the decoded index onto
a dedicated output.

### vec-vec unary (single vector → vector, request-driven)

**VVEC_QUALITY_SET** (sample op, partially left as an exercise; my proposed
fill-in, not live-verified): rewrite every lane's quality tier to a
runtime-selected level 1..5, values unchanged. Use case: bulk quality
retagging in one tick ("instant DMA"), or feeding quality-aware game
mechanics directly from vector state.

- Entity 3 (given): selector-combinator, `operation: quality-transfer`,
  `select_quality_from_signal: true`, `quality_source_signal: signal-W` —
  transfers every input lane to whatever quality `signal-W` carries on its
  green input.
- Entity 2 (given): constant combinator, a reference table — `signal-A` at
  each of the 5 qualities, valued 1..5 (quality level encoded as data).
- Entity 1 (blank, filled in here): `each[G] = VQUALITY_REQUEST_LEVEL[R] →
  each[G]` — `each` iterates entity 2's 5 rows on green, compared against the
  new `VQUALITY_REQUEST_LEVEL` broadcast carrier (chunk1, holds 1..5) on red;
  only the matching row passes through, preserving its quality tag, onto
  entity 3's `quality_source_signal` input.
- **Mismatch to fix**: entity 3 reads `signal-W`, but entity 2's table is
  built from `signal-A` — these need to match (a decider can't rename a
  matched `each` signal to a different fixed name while preserving its
  quality). Simplest fix: rebuild entity 2's table using `signal-W` instead
  of `signal-A` — no real cost, both are placeholders.
- **Needs a bench check**: selector-combinator quality-transfer semantics
  (whether `quality_source_signal` keys off the matched signal's quality tag
  vs. its numeric value) aren't something I can verify without running the
  game — this is a design proposal, not a confirmed-live fact.

## Read-select enumeration (R = 1..28)

8 registers + 12 vec-vec ops + 8 vec-scal→vec ops = 28. Full R↔source table
is wire-traced (not guessed) through the select-gate chain, entities 104–131,
each gated on `signal-R~uncommon == N` OR `signal-S~uncommon == N` (confirmed
by designer: two genuinely independent selectors, enabling bus summation of
whichever two sources each port names); see `v10_address_map.py`'s
`READ_SELECT_TABLE`. Order is **not** clean ascending
op order — each category block is wired in reverse op order, and category
order is registers → vec-vec arith → vec-vec conditional → vec-scal-vec
conditional → vec-scal-vec arith:

| R | Source | R | Source | R | Source | R | Source |
|---|---|---|---|---|---|---|---|
| 1 | VREG0 | 8 | VREG7 | 15 | VVEC_OR | 22 | VSVEC_GT_SELECT |
| 2 | VREG1 | 9 | VVEC_MUL | 16 | VVEC_XOR | 23 | VSVEC_LT_MASK |
| 3 | VREG2 | 10 | VVEC_DIV | 17 | VVEC_MIN | 24 | VSVEC_GT_MASK |
| 4 | VREG3 | 11 | VVEC_SUB | 18 | VVEC_MAX | 25 | VSVEC_MUL |
| 5 | VREG4 | 12 | VVEC_MOD | 19 | VVEC_LT_GATE | 26 | VSVEC_DIV |
| 6 | VREG5 | 13 | VVEC_POW | 20 | VVEC_GT_GATE | 27 | VSVEC_ADD |
| 7 | VREG6 | 14 | VVEC_AND | 21 | VSVEC_LT_SELECT | 28 | VSVEC_SUB |

Two independent selectors (Port A = `R`, Port B = `S`) each index this same
list; selected frames sum onto the shared vector bus.

The 8 register slots are entities 82–89 (gated by the write-select chain
46–53 at the same index) — confirmed as the actual register locations, not
entities 90/91, which turned out to be unrelated carried-over v8 scalar-core
entities (write injector + hold cell) laid out nearby. Their own decider
condition is still Factorio's unconfigured-combinator default
(`signal-no-entry != N`) — real hold-cell logic isn't built yet for 6 of the
8; only the select/gate wiring is complete for all 8.

## Standard scalar port A: beyond-chunk1 read extension

Scope for v10: **port A only** (~4 combinators); port B and instruction
fetch stay chunk1-only. From the read patch: two gate entities (previously
blank) should implement `each[G] > 0 → each[R]` — condition tests the GREEN
network (the chunk-boundary compare result), value comes from the RED
network. Same green-enable/red-data split as the vec-vec MAX/MIN else-branch
idiom above.

## Carrier-signal allocation

Not hand-picked — `v10_address_map.py` ranks every (signal, quality) combo by
how likely a real program would want it as data, then allocates from the
least-likely end. Type dominates quality:

- **Type**, most→least likely to be real data: item/fluid signals →
  alphanumeric virtuals (`signal-0`..`9`, `signal-A`..`Z`) → everything-else
  virtuals (shapes/colors/arrows/utility + the 76 census-appended virtuals).
- **Quality**, most→least likely to be real data: normal → legendary →
  {uncommon, rare, epic} (tied).

Carriers are drawn from (everything-else virtual) × (uncommon/rare/epic),
deterministically (alphabetical), excluding only genuine non-signals
(wildcards) and one practical-noise exclusion (`signal-no-entry`, which is
Factorio's own default filter on a freshly-placed unconfigured combinator —
not a "reserved meaning" exclusion). Run the module for the current
allocation; it will shift if `CARRIER_ROLES` grows (currently 8 roles,
including `VQUALITY_REQUEST_LEVEL` for VVEC_QUALITY_SET).

Reduction *outputs* (VREDUCE_MAX/MIN/ARGMAX/ARGMIN — see op inventory) are
handled differently: they're results a program deliberately reads, not
control-plane inputs, so they follow v8's CMP-flag convention (a convenient,
well-known address) instead of the obscurity ranking — kept at `rare` quality
only to stay clear of v8's own reserved ALU signals (`M`..`T` at normal
quality).

## Resolved (2026-07-21)

1. ~~Do op outputs need their own fine-grained RO chunk?~~ **No** — decided:
   op outputs are reachable only via the R/S-select vector bus. Reductions
   (and future dedicated vector I/O) are the intended scalar-facing path.
   Address space stays at 22,320, does not grow per op.
2. ~~Vec-vec conditional op entity IDs~~ — found: 70 (GT_GATE), 71 (LT_GATE),
   72 (MAX), 73 (MIN). All built live.
3. ~~Patch entities 9/10 semantics~~ — `each[G] > 0 → each[R]`.
4. ~~R/S select-gate AND vs OR~~ — confirmed OR: two genuinely independent
   selectors, as intended. (Earlier read of the JSON as an implicit-AND was
   wrong — deferring to the designer's live in-game view over my inference
   from the exported condition shape.)
5. ~~VREDUCE_MAX/MIN~~ — main BP's duplicate `select_max: true` on entities
   167/169 was a copy-paste bug; correct pair is true (MAX) / false (MIN),
   per the argmax/argmin patch. Added VREDUCE_ARGMAX/ARGMIN (the patch's
   address-table decoder stage on top of both selects) and VVEC_QUALITY_SET
   (quality-transfer unary op, entity 1's condition proposed but not
   live-verified) to the op inventory.

## Open questions / TODO

1. **Register write-through has no confirmed live evidence yet** (2026-07-22).
   A test that writes two operand vectors into registers 0/1 and reads back
   both the raw cells and `VVEC_MUL`'s output showed nothing on any probe —
   not the register cells, not the op's raw always-on output, not the gated
   vector bus. Confirmed NOT an infrastructure artifact (power and RCON
   pipeline both verified working correctly for this same paste). Needs
   isolated, bottom-up live debugging: confirm the write-address-latch and
   write-value-latch actually reach the register bank's `REG_MATCH`/
   `REG_VALUE` stage before assuming the hold-cell itself is at fault.
2. **The reduction-output signal assignments were promoted from the WIP
   blueprint's literal placeholders without a real design pass** — e.g.
   `VREDUCE_TIME`'s `signal-T`/`D`/`L` are exactly the letters that happened
   to be in the original patch, just now recorded in `REDUCTION_OUTPUT_SIGNALS`
   rather than actually decided. A few single letters (`A`..`V`) are already
   reused across multiple, semantically-unrelated result roles (distinguished
   only by quality tier). Revisit the whole reduction/ALU output address
   scheme deliberately rather than one-off patching individual entries as
   they're noticed.
3. Vec-vec ADD: still pending the two-read-port bus-summation mechanism
   (shared dependency with V8.md TODO #4) rather than a dedicated combinator.
4. VVEC_QUALITY_SET entity 1's fill-in and the entity-2/entity-3 signal-name
   mismatch are proposals, not confirmed live — need a bench check, and
   selector-combinator quality-transfer semantics in general are new
   territory for this project.
5. See `NETLIST_PLAN.md` for a proposed higher-level IR (named nets, a real
   signal allocator, gate/latch/mux primitives) to replace the current raw
   `(entity, connector, entity, connector)` wire-tuple + ad hoc dict style —
   motivated directly by how much of this session went into wiring
   archaeology and signal bookkeeping that a validating IR would catch
   structurally.

## Generator (ARCHIVED — `archive/v10_generator/generate_v10_processor.py`)

**Historical only.** This whole section describes the Python generator that
the fnet rebuild replaced on 2026-07-27; it is kept because the reasoning
(especially the entity-scoped signal substitution and why a flat name-based
one corrupted the CMP block) is worth not relearning. The files it names now
live under `archive/v10_generator/`, their imports intentionally broken. To
build v10 today, compile the `.fnet` sources — `run_all.py --regen` does it.

Superseded the standalone `tools/generate_v10_vector_zone.py` generator
(2026-07-21): the register bank's write-address decode taps directly into
the scalar core's own write-address-latch/write-value-latch wires, so the
scalar core and vector zone were never actually separable subsystems.
`generate_v10_vector_zone.py` is now a shared-helpers module only (signal/
carrier lookups, op-shape builders, `BlueprintBuilder`) — no CLI, no `build()`
of its own (that function was deleted 2026-07-22, having drifted out of
sync with later carrier-ownership fixes to the point of producing
structurally broken output).

`generate_v10_processor.py` produces one blueprint in one pass:
1. Loads the scalar core from `modules/processor.source.json` (the proven
   v8 master) with entity numbers preserved 1:1. Every control-plane signal
   in it — write-address latch, write-enable strobe, read-address carrier,
   accumulate masks, mmap mask — is substituted via
   `SCALAR_SIGNAL_SUBSTITUTIONS`, an entity-number-scoped map (not a flat
   name-based one: v8's own design legitimately reuses some literal names,
   e.g. `signal-N`, for two different, electrically-isolated purposes, so a
   flat substitution silently corrupted the ALU CMP block once — see
   `v10_address_map.ALU_CORE_SIGNALS`'s docstring). The ALU/CMP/PC letters
   are deliberately kept at v8's own names — an explicit decision recorded
   in `ALU_CORE_SIGNALS`, not an untouched leftover.
2. Generates the vector zone (register bank, op farm, reduction farm,
   port-A extension, quality op) from `v10_address_map.py`, positioned
   clear of the scalar core's footprint.
3. Wires everything together via a `WIRE_TEMPLATE` (role/row-indexed tuples,
   extracted mechanically from hand-corrected live exports) plus
   `wire_external_anchors()` for the scalar-core hookups the template can't
   encode on its own.
4. Every address-table role (scalar core's 4 + one per vector register +
   the argmax/argmin pair) gets real content — either a `signal_table`
   marker (scalar core + argmax/argmin, expanded post-paste by the test
   runner) or a fully inline offset table (`full_offset_address_table()`,
   used for the 8 register chunks and the port-A extension) — nothing is
   left blank.

**Signal ownership is enforced, not just aspirational**: `load_scalar_core`
raises immediately if any signal in the loaded scalar core isn't covered by
an explicit `SCALAR_SIGNAL_SUBSTITUTIONS` entry (structural wildcards
`signal-each`/`everything`/`anything` excepted). A full audit of the
generated blueprint (every operand/output signal in every entity, address
tables excluded) currently shows zero unaccounted signals.

**Usage**:
```
python -m tools.generate_v10_processor --out modules/v10_full.bp.json --out-string modules/v10_full.bp.txt
```
The `--out-string` form is directly pasteable in Factorio. Regenerate after
any change to `v10_address_map.py` or the generator itself — there's no
independent state to drift, unlike a hand-edited BP.
