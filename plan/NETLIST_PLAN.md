# Netlist/HDL layer — plan (updated 2026-07-27)

A text HDL for combinator circuits: named signals, colored nets, raw-JSON
combinator templates, modules, debug probes. Compiles to the existing
`*.source.json` format (and a pasteable BP string), replacing raw
`(entity, connector, entity, connector)` wire tuples and ad hoc signal dicts.
End state: the project's blueprint-generation workflow routes through this
layer instead of hand-built BP JSON.

Design decisions below were resolved with the designer on 2026-07-27; the
original motivation (three v10 incidents this would have caught mechanically)
is preserved at the bottom.

## Core decisions (resolved)

- **File format, not a Python API.** Modules are `.fnet` source files with
  their own grammar, parsed by `netlist.py`. Generator scripts like
  `generate_v10_processor.py` are what this replaces.
- **Raw JSON passthrough, minimal wrapping.** A combinator template is the
  entity prototype name plus its *verbatim* `control_behavior` JSON, with
  `[paramN]` substitution slots. The compiler never validates or interprets
  `control_behavior` contents — it substitutes params, parses the JSON, and
  emits it untouched. Latch/mux/decode idioms become library `.fnet` files,
  not compiler code.
- **One net = one color.** Every net is declared `net red x;` or
  `net green x;`. A net is one physical wire network; the compiler knows every
  emitted wire's color from the net itself.
- **v1 coloring is fully explicit** — template ports are `in_r`, `in_g`,
  `out_r`, `out_g` and the connected net's color must match the port.
  Rationale (designer): outputs present the same result on both colors
  (2 channels of one output); inputs sum red+green; deciders/arithmetic/
  selector configs reference wire color explicitly
  (`first_signal_networks` etc.). **Plan for a later auto-color pass** that
  assigns colors to unconstrained nets and rewrites `*_networks` config
  fields to match — the grammar must not paint us out of that (color on a
  net declaration is optional syntax later, mandatory in v1).
- **Layout: optimizer + repeater fallback** (superseded the original
  "auto-grid + fail on reach errors"; implemented 2026-07-27 when v10 scale
  made hand-packing untenable). Three stages:
  1. **Placement order is decoupled from entity numbering.** Numbering stays
     declaration order — testbench `entity_map`s and the `.debug.json`
     sidecar index by it — while geometry follows an optimized order. A
     better order therefore can never change behavior.
  2. **Try a few orders, keep the best.** Declaration, Cuthill-McKee,
     reverse Cuthill-McKee, and net-clustering are each packed and scored on
     `(reach violations, total wire length)`, lower wins. Declaration order
     is always a candidate, so the result is never worse than before.
  3. **Repeaters as fallback.** Any edge still over reach gets an inert
     constant combinator anchored on the same net at the edge's midpoint.
     This is **electrically the same network** — an empty constant drives
     nothing and a wire network propagates within a tick — so a repeater
     costs **zero latency** (designer, 2026-07-27). It is the mechanical
     form of the `br_*` bridge stims `v8_processor.fnet` places by hand.
     Repeaters live in the free lanes *between* packed rows (rows are 1 tile
     tall at pitch 2), so inserting one never moves an already-placed
     entity — which is what makes the loop converge.

  `design.wires()` remains the strict checker (raises on any over-reach
  edge); `design.route()` is the build path that repairs. Verified
  output-neutral: after the change all eight existing fnet modules were
  entity-, signal-, and connectivity-identical (only positions moved), and
  the suite stayed green.
- **First real target: fresh small module, live-tested.** Re-express
  `demo_circuit` in the DSL with identical entity numbering, compile, paste,
  and run the existing `memory_basic` testbench. Proves the whole pipeline
  (DSL → source.json → paste → bench) before anything v10-sized. The v10
  register-bank diff against `WIRE_TEMPLATE` remains available as a later
  equivalence check but is not the gate.

## The language (v1)

```
# comment to end of line
import "lib/gates.fnet";

# Signals: named channels, allocated by one authority.
signal wsel;                    # auto-assigned from the obscurity-ranked pool
signal read_addr = signal-R;    # explicit pin
signal boost = signal-W@rare;   # pin with quality

# Nets: named single-color wire networks.
net red data_bus;
net green write_en;
debug net green read_out;       # or later:  debug read_out;

# Template: entity prototype + verbatim control_behavior with [paramN] slots.
read_gate = decider-combinator({
  "decider_conditions": {
    "conditions": [
      { "first_signal": { "type": "virtual", "name": "signal-each" },
        "second_signal": { [param0] },
        "comparator": "=",
        "first_signal_networks": { "red": true, "green": false } }
    ],
    "outputs": [ ... ]
  }
});

# Instantiation: <...> binds params, (...) connects ports.
read_gate<read_addr>(in_r: data_bus, in_g: mem_state, out_g: read_out);

# Sugar: assignment target binds to the output port matching the net's color;
# a positional input binds to in_r/in_g by the net's color.
read_out = read_gate<read_addr>(data_bus, in_g: mem_state);

# Modules: ports are direction+color-typed nets.
module mem_cell(in_g write_in, out_g q) {
  net red loop;
  ...
}
mem_cell(write_in: stim_net, q: cell_out);
```

Param substitution is textual, then `json.loads`: a declared signal expands
to `"type": ..., "name": ..., "quality": ...` object-body form (matching the
`{ [param0] }` call-site shape); an int literal expands to the number; a
quoted string to itself. Signal type (virtual/item/fluid) is looked up via
`signal_space.py` lists.

Entity connector table (extensible dict in `netlist.py`; unknown prototype =
error asking to extend it): decider/arithmetic/selector-combinator have
in 1/2, out 3/4, footprint 1x2 in the default north orientation (no
`direction` key emitted); constant-combinator has out-only connectors 1/2,
footprint 1x1.

## Compiler passes

1. Parse (hand-rolled tokenizer; template JSON bodies captured by
   balanced-brace scan, kept as text until param substitution).
2. Elaborate: inline module instances recursively, hierarchical net names
   (`inst.net`), bind ports (direction + color checked).
3. Allocate signals: pins first, then autos drawn from
   `carrier_candidates()`-style obscurity ranking, skipping anything pinned.
   Same `(name, quality)` claimed twice without an explicit alias = error
   (incident #2's class of bug). Allocation is per module *definition* —
   two instances of a module share its internal signal assignments
   (electrically isolated nets, same config), which is intended.
4. Validate: every net needs ≥1 driver pin and ≥1 consumer pin; a top-level
   input port waives the driver requirement, an output port waives the
   consumer requirement, a debug mark waives the consumer requirement.
   Multiple drivers are legal Factorio (summing) — reported, not an error.
5. Layout: column-packed grid — rows are `ROW_COLUMNS` (15) one-tile
   columns wide and exactly one combinator (2 tiles) tall. **Combinators
   stand up** (2026-07-27): a north-facing combinator is 1x2, so the grid
   cell is 1x2 rather than the 2x2 that an east-facing 2x1 needed once the
   old layout's spare inter-row repeater lane was counted — half the area per
   combinator, and the same again for a pair of 1x1 constants sharing one
   column (only CONSECUTIVE constants pair, so pairing never drags an entity
   away from its net-mates). Rotation is electrically inert: blueprint wires
   name connector IDS, not sides. Measured on the v10 processor: 14x43 tiles
   and 199 entities became 15x25 and 190 — denser wiring needs fewer
   repeaters. `GAP_COLUMN` (7) is skipped in every row and reserved for them,
   which is also where a repeater most wants to sit: every entity in a row is
   within half a row-width of it. Entity numbering = declaration order, debug
   probes appended last.
6. Wire: per net, minimum-spanning-tree over pin positions; every edge
   checked against 9.0-tile reach; emit `[en, conn, en, conn]` tuples.
7. Emit: `*.source.json` (existing format, consumed unchanged by
   `factorio_memory_tb.py`), optional BP string via
   `factorio_blueprint_codec.py`, and a `*.debug.json` sidecar mapping each
   debug net → probe entity_number / wire color / position (probe = empty
   constant combinator, the same pattern as `demo_circuit` entity 6), ready
   to paste into a testbench's `output_probes`.

## Migration plan

1. ~~Compiler + unit tests~~ **DONE 2026-07-27**: `netlist.py` +
   `test_netlist.py` (20 tests: template/inst emission, undriven/unconsumed
   errors, port waivers, signal collision + determinism, nested modules,
   param substitution, reach checker).
2. ~~Demo module live~~ **DONE 2026-07-27**: `modules/netlist_demo.fnet`
   re-creates `demo_circuit` with identical entity numbering; compiled output
   is entity- and connectivity-equivalent (`wire_partition`) to
   `modules/demo_circuit.source.json`, and passes `memory_basic` live
   12/12 checks unchanged.
3. ~~Signals/tables infrastructure~~ **DONE 2026-07-27** (designer decision:
   *every* signal auto-assigned, especially the read carrier; signal_space
   integrated):
   - Template bodies may carry top-level `signal_table` /
     `player_description` — lifted onto the entity record, not
     control_behavior. **Exclusion happens during address-space
     generation** (designer, 2026-07-27): an `exclude <signal>;` statement
     drops that signal's one (name, quality) row — never all qualities of
     the name — from the space BEFORE numbering, so the space stays compact
     with no gap at the would-be address. It is design-wide state (every
     signal_table shares the resulting numbering, `memory[...]` resolves
     against it); the compiler stamps the exclusion set onto every emitted
     signal_table marker and the runner regenerates the identical numbering
     through the same authority, `signal_space.full_table(exclude)`.
     Typically the only exclusion is the read carrier. A per-table exclude
     list inside a template blob is a compile error.
   - Compiled source.json embeds a `signals` map (declared name →
     type/name/quality/display); testbenches drive and expect carriers
     symbolically as `"$name"` — `factorio_memory_tb.py` resolves them,
     drives typed + non-normal-quality filters, and honors table exclusion.
   - `netlist_demo` migrated: carrier auto-assigned (no `signal-R`
     anywhere), full 2480-row signal_space table populated at paste,
     live 13/13 (`testbenches/netlist_demo.tb.json`), in the run_all suite.

## v8 baseline (the current goal, set 2026-07-27)

Rebuild the v8 processor as fnet-generated modules with per-component
testbenches, until the full suite runs green against an fnet-generated
master. Equivalence is **behavioral** (the existing benches), never
wire-diff — carrier signals deliberately change when auto-assigned.

Component decomposition follows the existing open-loop benches (they
already isolate the blocks) plus V8.md's chains:

1. ~~`mem_cell`~~ **DONE 2026-07-27** — `modules/v8_mem_cell.fnet`
   (templates in `modules/lib/v8.fnet`, verbatim from the master:
   injector 22, cell 36), live 18/18 (`testbenches/v8_mem_cell.tb.json`),
   in the run_all suite; `--regen` now also recompiles fnet modules.
   Bench wisdom: the runner single-steps a paused game, so a
   `settle_ticks:1, trace_ticks:1` step holds a write for exactly one
   tick — deterministic open-loop accumulate pulses (one add per pulse),
   checked on the *following* release step. The accumulate mask is a
   dedicated combinator on the reset bus (V8.md TODO 7's cleaner split),
   not part of the trig_reset driver.
2. ~~`write_encoder`~~ **DONE 2026-07-27** — `modules/v8_write_encoder.fnet`,
   live 11/11 (`testbenches/v8_write_encoder.tb.json`), in the suite.
   Structure note: the set and reset buses are the two output-color faces
   of ONE product combinator (master 13) — out_r and out_g to different
   nets. The write trigger (master: decode-row entity 5) is a stim port
   here; gating is tested explicitly.
3. ~~`decoder_matrix`~~ **DONE 2026-07-27** — `modules/v8_decoder_matrix.fnet`,
   live 11/11 (`testbenches/v8_decoder_matrix.tb.json`), in the suite.
   Includes the write-trigger sensor (master 5, fires on route bit 2 via
   the bit-2 mask's green output face) — its out_r is the component's
   `write_trig` port, feeding write_encoder's `trig_in` at composition.
   Layout lesson (first reach-limit encounter, solved by ordering): the
   master's matrix_out anchor constants + the 2-tile shifter/sensor are
   interleaved as phase adjusters so no 4-tile mask→gate pair straddles a
   14-tile row wrap; the .fnet documents the constraint.
4. ~~`alu`~~ **DONE 2026-07-27** — `modules/v8_alu.fnet`, live 8/8
   (`testbenches/v8_alu.tb.json`), in the suite. First real use of
   `region`/`memory[...]`: `region alu = 2090` + 18 rows (operands,
   results, S/T min/max, Q/O flags) replace the master's letter signals;
   the bench drives the red state bus directly and reads the green result
   bus — no memory cell in the loop.
5. ~~`port_a`~~ **DONE 2026-07-27** — `modules/v8_port_a.fnet`, live 6/6.
   First excluded-carrier component (`read_a` rides the table net).
6. ~~`port_b_mmap`~~ **DONE 2026-07-27** — `modules/v8_port_b_mmap.fnet`,
   live 6/6. `b_reg = memory[310]` is the register-row config declaration;
   the mask references it. Found the **fully-qualified-filter gotcha**
   (below).
7. ~~`pc`~~ **DONE 2026-07-27** — `modules/v8_pc.fnet`, live 5/5.
   `pc = memory[2096]`, autoincrement stated inline as `<pc> = 1`
   (v8_mask_const), inject/carrier offsets bench-verified open loop.
8. ~~`processor_v8.fnet`~~ **DONE 2026-07-27** — `modules/v8_processor.fnet`
   (`--top processor`, 101 entities), composed from the seven component
   modules + ROM + driver stims. Live: `v8_processor_smoke` 9/9 (direct
   writes, port A/B reads, closed-loop 2-instruction program via
   ROM-driven-by-bench + PC zero-write restart), and machine_v8-scheduled
   programs via `tools/build_fnet_v8_tests.py`: `v8_proc_accumulate_halt`
   (exact counter, halt spin, poison canary 0) and `v8_proc_fib_cmp`
   (full fib: B-stream, latched-A copies, ALU, CMP-steered jumps — exact
   finals). The fib schedule is slot-identical to the master's, confirming
   the timing model carries over to the fnet build.
   - **machine_v8 integration done**: `MachineConfig` (pc/mmap/accumulate/
     ALU addresses + namer) with the master as `DEFAULT_CONFIG`;
     `config_from_signals_map()` builds one from a compiled source's
     signals map (row-name convention: pc, b_reg, sub/add/div/mul_{a,b,res},
     cmp_m/n, cmp_min/max, flag_ge/gt, accumulate-flagged rows).
   - **Composition lessons**: (a) allocation is now phased — explicit pins
     → autos for excluded names → space → memory rows → remaining autos —
     so an auto can never squat on an addressable row (first hit: alu rows
     vs a component's stim carrier). (b) Regions can't overlap: alu moved
     2090→2110, clear of pc=2096. (c) Diamond imports (every component
     imports lib/v8.fnet) are deduped by a loaded-set in parse_file.
     (d) The write-trigger sensor taps ALL THREE out2 masks' green faces
     (bits 2/32/512) — the component originally tapped only bit 2 and its
     bench only tested bit 2; composition review caught it. (e) 101
     entities row-pack with ~15 inert bridge/anchor stims (`br_*`)
     carrying buses down the layout; declaration order in
     v8_processor.fnet is load-bearing and documented there.
   - Bench trick worth keeping: a testbench can DRIVE the ROM constant
     with `$mem[N]` keys (no source variant needed) and restart execution
     deterministically by zero-writing `$pc` through the trio — while
     held, P pins at 1; on release it ramps from 2.

**Filter gotcha (verified live 2026-07-27, fixed in
`signal_json_fragment`):** a constant-combinator filter row without an
explicit `"quality"` imports as quality=nil and the combinator **emits
nothing** (read-back shows the filter present — silent). Conditions
tolerate omission; filters don't. The compiler now always emits fully
qualified `type/name/quality` fragments everywhere.

Each component: its own `.fnet` (shared templates in `modules/lib/v8.fnet`),
its own `*.tb.json` using `$name` symbolic signals, live-tested standalone
before composition. **Convention (2026-07-27): component files export a
NAMED module (`module mem_cell`, `module write_encoder`, …) plus a
`<name>_bench` harness module** — harnesses can't all be `main` because
composition imports every component file and module names collide on
import. Build with `--top <name>_bench`; `run_all.py --regen` passes the
tops from its `FNET_MODULES` list.

**Config combinators are fnet-owned** (designer, 2026-07-27):

- `mmap_addr` is **memory-mapped config**: its content designates which
  address-space row serves as port B's read-address register (today
  `shape-circle` = `machine_v8.MMAP_B_SIGNAL`; the cell at that row holds
  port B's read address). In fnet the configuration act is the declaration
  itself — `signal port_b_addr = memory[...];` chooses the row, and the
  mask template references it — so the config lives in the design file and
  `machine_v8` reads the chosen row from the signals map.
- `pc_autoincrement`: `<pc> = 1` stated inline (the constant is the
  per-tick increment; the PC row is likewise a `memory[...]` declaration).
- `write_trigger_reset`'s accumulate mask (−1 per accumulate cell)
  *derives* from which cells the design declares accumulate, not from a
  Python constant table. **Implemented 2026-07-27**: `accumulate <sig>;`
  statement (memory-mapped rows only) + an `{"accumulate_mask": true}`
  marker template whose filters the compiler GENERATES from the declared
  set (hand-written filters in a mask body are a compile error — one
  source of truth). The signals map stamps `"accumulate": true` on the
  rows for machine_v8 to read at schedule time; declaring accumulate
  without a mask entity, or a mask without declarations, fails validation.

`machine_v8` reads whatever it needs (port-B register row, PC row,
accumulate set, addresses) from the compiled artifacts at schedule time.
One source of truth: the design file. `import_master_bp.py`'s
populate-at-import role retires with the fnet baseline (it stays for
importing chat-pasted BPs).

## Phase 2 notes — wisdom from the infrastructure session (2026-07-27)

- **Benches reference memory by address through the address table, never
  by signal literal** (designer, 2026-07-27 — implemented). The space is
  contiguous but its *numbering* is design-relative (exclusion compacts
  it), so a game-signal literal in a timeline is only correct for one
  exclusion set. `"$mem[N]"` in drives/expects resolves to the signal at
  row N of the design's own space at run time (the runner reconstructs it
  from the signals map's excluded entries via `full_table(exclude)` — the
  same authority that populates the tables), so a bench means "row N"
  under any exclusion set, which is what a memory bench means anyway.
  `netlist_demo.tb.json` is the reference example: `$mem[N]` for rows,
  `$read_addr` for the carrier, zero game-signal literals.
- **Regenerate everything together.** Auto-allocation shifts when the
  declaration set changes; the address space shifts when exclusions change.
  Never cache a numeric address, carrier name, or ROM image across builds —
  source.json, benches, and machine_v8-scheduled ROMs derive from one
  compile or not at all.
- **Byte-diff rebuilds.** When a refactor should be output-neutral, diff
  the freshly compiled source.json against the previous build: identical
  bytes mean the previous live verification still stands. Caught two
  no-live-run-needed cases this session.
- **Probe only debug-marked nets in new benches.** A probe constant has a
  single connection point, so "entity N, wire green" is unambiguous;
  probing a combinator terminal directly leaves input-vs-output ambiguity.
  The `.debug.json` sidecar is shaped to paste into `output_probes`.
- **Stamp `player_description`** (the lift-out key) on component entities
  using V8.md's vocabulary — live debugging and tooling identify entities
  by description, never by entity number.
- **Layout will trip at v8 scale.** 77 entities in naive row packing will
  likely exceed the 9-tile reach on some net; the failure is a clear build
  error naming the net and pins. Respond with module-block grouping or
  placement hints *then* — don't pre-build layout machinery.
- **The runner's `read_addr` shorthand still hardcodes `signal-R`** —
  legacy benches only. New benches use `drive` maps with `$` references;
  retire (or symbolize) the shorthand when the fnet baseline replaces the
  master.

**Memory-mapped operands (resolved 2026-07-27, implemented in netlist.py).**
ALU operand/result slots, the PC, and the mmap marker are not free
carriers — they are *addressable rows*. Their design flow is a memory-space
setup step: choose the **numeric addresses** first (deliberately — e.g. far
from program ROM and user memory), then derive each row's game signal from
the address via the same table the hardware decodes with. In fnet:

```
region alu = 2090;                 # deliberately chosen base address
signal mul_a = memory[alu + 3];    # -> signal_space row at address 2093
signal pc    = memory[2096];       # direct address form
```

`memory[...]` declarations are pins (they register ownership, so autos and
other pins collide loudly), carry their `address` in the emitted `signals`
map, are **never auto-assigned** like control-plane carriers, and cannot be
excluded — being addressable is their whole point. They resolve against the
**design's** address space, i.e. after the `exclude` set has compacted the
numbering (generation order: allocate carriers → generate space with
exclusions → resolve memory addresses → populate tables).
The `machine_v8` integration reads addresses from the signals map when the
fnet v8 rebuild reaches the ALU/PC (regions replace `ALU_MAP`'s hardcoded
letters as the source of truth).

Free control-plane carriers (read/write selects, latch carriers) go auto
immediately.

## v10 vector zone (started 2026-07-27)

The v8 baseline above is complete, so v10 is being rebuilt the same way:
`.fnet` components, each live-benched standalone before composition. Design
decisions and the resolved datapath live in `modules/processor_v10.design.md`;
shared templates in `modules/lib/v10.fnet`. Designer decisions taken
2026-07-27: registers are **accumulate-only** (no reset hardware; clear a
lane by writing its negation), the **full 22,320-address unified space** is
in scope, and the op inventory grows from a **minimal set of proven shapes**
rather than being built out from the WIP-blueprint tables.

0. ~~`v10_addr_map`~~ **DONE** — `modules/v10_addr_map.fnet`, `region vec =
   2130`. Every vector control value and reduction output is a
   `memory[...]` row, declared up front so addresses are stable before the
   ops exist (designer, 2026-07-27: assign them now "so that later flow
   steps can use them when compiling alu operations"). Chunk1's green memory
   bus is wired STRAIGHT into every gate in the zone as its control plane —
   no mirror, no copy, zero added latency — so a scalar `write_imm` to
   `vec_wsel` IS the vector control action.

   **Colour discipline: green = control (the scalar bus), red = vector
   data** (hold loops, write bus, op outputs). Each gate has one red and one
   green input, needs data on one and control on the other, so the two
   assignments are forced together. An earlier revision had them swapped and
   needed a mirror combinator plus a "data lags control by a tick" rule;
   both vanished on the flip.
1. ~~`vec_reg`~~ **DONE** — `modules/v10_vec_reg.fnet`, live **16/16**.
   **The register is ONE combinator** (designer, 2026-07-27): an
   accumulating memory needs no write state, so the register is purely a
   hold loop whose only condition is the erase test —
   `vec_erase != <block id> → everything, from green` — and WRITE HEADS
   output directly onto that same loop, letting Factorio's wire summation do
   the accumulate. Heads are ordinary combinators and a loop may carry
   several (the mux head today, the scalar frame-write path later); they
   simply sum. A decider cannot express erase + write + hold anyway (three
   states, two branches) and with summation writing, it needn't.
   Zeroing is now a real operation rather than the clear-by-negation hack.
   Settles the design doc's open question #1.
2. ~~`vec_bank`~~ **DONE** — `modules/v10_vec_bank.fnet`, live **34/34**.
   **12** registers (widened from 8 for mandelbrot; registers own mux indices
   1..16 with four reserved, so ops never renumber again) + write heads +
   the read mux (`R == k OR S == k` per source),
   all driven through memory-mapped control. Proves external frame write,
   register-to-register copy, the free vector ADD, `R = S = k` selecting
   once not twice, per-block erase, and the **move primitive**: erase and
   write the same register in the same tick and you get REPLACE in one tick,
   because the cell emits nothing while the head drops the new frame. That
   is the shuffle operation any hardwired-operand op farm leans on.
   Exports `op_a`/`op_b` (VREG0's red face, VREG1's hold loop) — the
   hardwired op-farm operands.

   **Color discipline**: red is control only (the mirrored `ctl` net), green
   is vector data only (hold loops, write bus, op outputs). Every gate is
   then the same shape — condition off red, pass a green frame through — and
   the two input connectors never compete.
3. ~~`op_farm`~~ **DONE** — `modules/v10_op_farm.fnet`, live **24/24**.
   The always-on farm with **two hardwired operand pairs** (designer,
   2026-07-27): pair A = VREG0 × VREG1 carries the fuller set (MUL, SUB, and
   MAX as the else-branch shape), pair B = VREG2 × VREG3 the core set (MUL,
   SUB). A second pair halves the shuffling for anything computing more than
   one product per pass. Ops never touch the mux; each result gets its own
   read-select gate, so an op output is just another mux source
   (17 A_MUL, 18 A_SUB, 19 A_MAX, 20 A_DIV, 21 A_MOD, 22 B_MUL, 23 B_SUB —
   deliberate contiguous indices, not the WIP blueprint's reverse-order
   wiring). No ADD unit anywhere: two selections sum on the write bus.

   **Lane semantics, measured not assumed**: `each` on a two-network op
   iterates the UNION of both inputs, a lane missing on one side reading 0.
   So MUL is self-cleaning (drops unmatched lanes, n*0 = 0) but SUB is not —
   it manufactures a negative lane for anything present only on the green
   operand. Operand lane sets must be kept aligned, which is another reason
   the one-tick MOVE matters: it makes the destination's lane set exactly the
   source's.

   Extended 2026-07-27 with **DIV and MOD** on pair A and a **vec-scal
   block** (VREG0 against the memory-mapped `vec_bcast` row: MUL, SUB, DIV,
   MOD, and a GT escape mask). DIV/MOD earn their place immediately — they
   turn the COORD ramp into a 2D grid, `x = i % width` and `y = i / width`.

   **Second lane rule, also measured**: `each` vs a FIXED signal iterates only
   the FIRST signal's networks — unlike `each` vs `each`, which unions them.
   That is why a vec-scal unit can read the whole scalar memory bus for its
   broadcast without dragging every control row in as a junk lane, and why
   v8's own `v8_onehot` has always worked. Generalising the union rule to
   this case would wrongly predict both are broken.

4. ~~`vec_io`~~ **DONE** — `modules/v10_vec_io.fnet`, live **10/10**. The
   ports that let data cross between scalar and vector land; until they
   existed the zone was a closed loop that could only shuffle between its own
   registers. All three plug into the EXISTING mux rather than inventing
   plumbing, so loading a source is just `R = <index>` then `W = <register>`:

   - **COORD** (index 32) — "nothing → vec": an address table wired onto a
     red net, so every lane holds its own index. A whole coordinate ramp in
     one selection with zero scalar writes.
   - **LANE_IN** (index 33) — data in, one addressed lane: a one-hot decode
     of `vec_wlane_addr` times `vec_wlane_value`. It decodes rather than
     copying the scalar bus wholesale, because copying would drag the PC, ALU
     rows and control plane in as junk lanes — and registers accumulate, so
     the junk would compound.
   - **LANE_OUT / VRED_SUM** — data out: extract the lane named by
     `vec_rlane_addr` onto the scalar row `vec_rlane_data`, or collapse the
     whole bus into `vred_sum`. This is what lets a program branch on vector
     state.

   Bench note worth keeping: selecting COORD puts ~2478 lanes on the vector
   bus, and probing that net would blow the ~4096-byte RCON response limit.
   The bench probes only the scalar side and reads vector state through the
   out-ports — which is both the realistic access pattern and the only safe
   one at full width.

5. ~~`v10_processor` + a real program~~ **DONE** —
   `modules/v10_processor.fnet` (203 entities, auto-inserted repeaters)
   composes the v8 scalar core with the whole vector zone, and
   `tools/build_v10_tests.py` produces a live program bench, **1/1**
   (`testbenches/v10_proc_vector_roundtrip.tb.json`).

   **The join is one net.** `v8_processor.fnet`'s `membus` became a port, and
   composition is nothing but handing it to each vector component. So a
   vector operation is an ordinary `write_imm` to a memory row and results
   come back through `copy_a` — machine_v8 scheduled the whole program with
   **no vector awareness at all** and no new instruction exists.

   The program parks the mux on COORD and extracts lanes 7 and 250 (each
   returns its own index), reduces the whole ramp to N(N+1)/2 over 2451
   lanes, then writes lane 60 = 4242 through LANE_IN and reads it back with
   the frame reduction confirming exactly one lane.

   **A build-time hazard checker replaces the scheduler's missing model.**
   `check_vector_hazards()` in the builder knows what machine_v8 does not:
   a control row settles at `value_slot + 5`, and the vector zone then needs
   2-5 more slots to propagate it depending on which port is read
   (`CONTROL_REACH`). A read landing inside that window fails the build. It
   caught three real hazards in the first schedule and is the specification
   for a vector-aware machine_v8 when that lands.

   **Lane discipline composition adds**: `vec_io`'s out-ports DRIVE membus
   and a memory cell may hold the same row, so a program must never write
   `vec_rlane_data` or `vred_sum` — hardware outputs, read-only by
   convention, exactly like v8's CMP flags.

   **vbus is never probed**: selecting COORD puts ~2451 lanes on it, and an
   RCON response corrupts past ~4096 bytes, so probing it would break the run
   rather than fail it. Vector state is observed through the out-ports.

   **Register writes work, via a one-tick pulse** (designer, 2026-07-27 — the
   fix for what looked like a hardware gap). I had concluded that moving a
   register was impossible because `vec_wsel` and `vec_erase` are independent
   HELD rows and leaving "mirror" mode passes through either *cleared* or
   *accumulating*. That framing was wrong: **don't hold W at all**. The write
   path is pipelined, so writing a row and then zeroing it lands on
   consecutive ticks:

   - **erase pulse** — the register clears and is left empty AND holding.
   - **W pulse, one tick** — into an empty register, a single accumulate
     pulse IS a replace.

   So a move needs no atomicity between the two rows, and no new hardware.
   It does need the pulse to be exactly one tick, which is why
   `IR8.pulse_imm` (new) places both writes ATOMICALLY on consecutive slots:
   two independent `write_imm` calls schedule adjacently in isolation but
   widen to three ticks once surrounding stages compete for the slot — and a
   3-tick pulse silently commits three accumulate copies of the bus.
   `check_pulse_widths` re-asserts it per program, and
   `test_machine_v8.test_pulse_imm_is_exactly_one_tick_wide` pins it with
   crowding ops.

   The program now moves the whole COORD ramp into VREG0 and proves it: the
   register reduces to the same N(N+1)/2, lane 7 still reads 7, and the op
   farm computes `VS_MOD = VREG0 % 3` reading back 1 at lane 7 and 2 at lane
   8 — real per-lane arithmetic over 2451 lanes, which is the mandelbrot grid
   step.

6. ~~vector hazard model in `machine_v8`~~ **DONE 2026-07-27**. The zone's
   timing now lives in `MachineConfig.vec_reach`, resolved from the design's
   own row names, so **programs contain no hand-inserted settle stages** — the
   scheduler derives the spacing. Three rules, all in `_Sched8`:

   - `_vec_read_lo` — a vector read waits for the control it wants to
     propagate (`value_slot + CELL_TO_USE + stages`: the ordinary cell rule
     with the zone's depth added).
   - `_vec_write_disturbs_read` — a control written LATER must not already be
     visible at an earlier read. This is the direction that bites: the greedy
     scheduler leaves a wide gap behind a vector read and drops the next
     write into it. Measured — `vec_rlane_addr <- 8` landed at slot 58 and
     became visible at 68, exactly where the preceding lane-7 read sat.
   - `_vec_bus_write_floor` + the `VEC_BUS` sentinel — a `vec_wsel` write
     SAMPLES the shared bus at the instant it settles, so the mux must
     already present the intended source. Without it a move silently captures
     whatever the mux happened to be showing.

   Removing the hand-tuned settle stages cut the program from 99 ROM words to
   52. Fixing a latent halt-placement bug fell out of the same work: `halt` is
   terminal and must sit after every placed slot, which the stage floor alone
   did not guarantee once reads could be pushed far past it.

   Everything is inert on v8 (`vec_reach` is empty there), pinned by
   `test_machine_v8` (15 tests, five of them vector-specific).

7. ~~mandelbrot~~ **DONE 2026-07-27** — `tools/build_v10_tests.py:mandelbrot`,
   live **1/1** (`testbenches/v10_proc_mandelbrot.tb.json`). The set over all
   2451 lanes, 64x38, 20 iterations in 360ths fixed point, 539 ROM words, 14s
   wall; checked lane by lane against a Python model. **MANDELBROT.md** is the
   record. Three additions to the zone paid for it:

   - **the bank widened 8 -> 12** (the kernel needs eleven live registers) and
     registers took mux indices 1..16 so future widenings renumber nothing;
   - **the square block** — the bank exports both faces of each pair-B
     register (`a2g`, `b2r`), so `VREG2^2` is the ordinary `each[red] x
     each[green]` shape and needs no unmeasured each-against-itself
     semantics. B_SQ (24), B_SQG (25), B_SQDIFF (26) make ONE pair load yield
     zx*zy, zx^2-zy^2 and — since two selections sum — zx^2+zy^2, i.e. the
     entire iteration including its escape test;
   - **`(soonest, latest)` in the reach table**, because B_SQDIFF is the
     zone's first two-deep unit. The forward rule (has my control arrived?)
     wants the SLOWEST path; the anti-dependency rule (could a later write
     already be visible?) wants the FASTEST. One number cannot serve both the
     moment the zone stops being a uniform-depth chain.

   What the kernel taught, beyond the zone: a lane computing to 0 VANISHES, so
   a grid offset must be an addend on the second selector rather than a
   VS_SUB after a multiply (this cost a run — 34 of 37 expectations, the three
   misses being exactly the x=0 column and y=0 row); an instruction immediate
   is 20 bits SIGNED, which is what caps the fixed-point scale; and the ROM
   shadows every memory row inside its own slot span, so a long program's
   port-read rows must sit above it.

8. **Next**: reductions beyond `vred_sum` — `vred_count` (popcount) would have
   read "how many lanes are still inside" directly instead of costing a sticky
   mask and a whole-frame sum. Also unbuilt: the multi-chunk port-A read, the
   quality-transfer op, a conditional relative jump (V8.md TODO #1). Deferred
   by the designer: a UPS measurement of the always-on farm.

**Probe width is a hard constraint at this scale.** Two nets in the composed
machine cannot be sampled at all: `vbus` (COORD puts ~2451 lanes on it) and
`membus` — the ROM constant drives it, so it carries one signal per
instruction plus every ALU and out-port row. At 45 ROM words that sampled
fine; at 99 it pushed each per-tick response past the ~4096-byte limit where
RCON responses corrupt, and the run timed out rather than failing. Program
benches probe the cell's red state loop, which carries only what the program
wrote and so stays small however long the program gets.

**Address-space correction found by that program** (2026-07-27): the
whole-space reduction came out 5250 short of N(N+1)/2. A live diff of a
populated address table against its own output net showed 25 rows missing —
`signal-unknown` and the four blueprint-parameter placeholders, at all five
qualities. Factorio accepts them as constant-combinator filters and reads
them back as present, but **never puts them on a wire**: the same silent
failure shape as the missing-quality gotcha. They are now in
`signal_space.EXCLUDED`, which shrank the space 2480 -> 2455. Before this
they were addressable rows that swallowed every write and read back nothing,
in v8 as much as v10. The shift also broke one legacy bench that had cached
a numeric address (`proc_cmp`, 2091 -> 2066) — the "never cache an address
across builds" rule, demonstrated.

**Future direction** (designer, 2026-07-27, not built): a second PC and
instruction-decoder block feeding the interconnect directly instead of
driving it through memory-mapped config. Nothing built now is wasted — a
decoder becomes another driver of the same select/erase lines.

**Target demo: mandelbrot** (designer, 2026-07-27 — "we are very close").
Worth recording because it pins down which ops earn their combinators first.
Per-lane `z = z² + c` needs, for `zx' = zx² − zy² + cx` and
`zy' = 2·zx·zy + cy`:

- vec-vec MUL, for `zx²`, `zy²`, `zx·zy` — all three are the same op with
  different operands shuffled into VREG0/VREG1, so the **move primitive**
  above is the inner loop's workhorse.
- Addition is free (bus summation), so `+ cx` costs no combinator.
- Subtraction: either a vec-vec SUB unit or add a pre-negated register.
- Escape test: vec-scal→vec GT mask of `zx² + zy²` against a broadcast 4.
- Iteration count: commit the still-inside mask to a counter register —
  **registers accumulate natively**, so the histogram is a plain write, no
  adder involved.

That is the minimal op set almost exactly, which is a good sign the set was
chosen right. The cost model is shuffling, not arithmetic: with operands
hardwired to VREG0/VREG1 every op needs its inputs moved in first, so the
inner loop is dominated by moves. Worth measuring before adding ops.

**Offset address tables** (compiler support added 2026-07-27 for the unified
space): a `signal_table` marker may carry `"offset": N`, which shifts every
emitted address by N while leaving the slot *layout* identical. Chunk 1 is
the scalar space at offset 0; chunks 2..9 are the vector registers' lane
blocks at `CHUNK_WIDTH * k`. Because addresses are globally unique, a port-A
read decider comparing a parked address against all nine tables matches in
exactly one chunk — which is the whole beyond-chunk1 read extension, one
table plus one decider per chunk, with no boundary-compare stage.
`modules/lib/v10.fnet`'s `v10_addr_table_chunk` is the template;
`factorio_memory_tb.signal_table_rows(exclude, offset)` populates it and
caches rows per `(exclusions, offset)`.

## Later / deferred

- Retire `tools/generate_v10_processor.py`'s `WIRE_TEMPLATE` /
  `SCALAR_SIGNAL_SUBSTITUTIONS` once the fnet v10 covers the same ground;
  wire-list diffing against them is available per piece in the meantime.
- **Auto-color pass**: assign colors to unconstrained nets, rewrite
  `*_networks` config fields to match. Needed before modules compose freely
  without fixed-color port friction.

## Non-goals

- Not a general-purpose HDL; primitives grow only as idioms recur.
- Not modeling tick-level timing or quality arithmetic — structural
  connectivity + a symbol table only; timing stays in `machine_v8.py`.
- v7 stays archived as-is. (The original "don't touch v8" non-goal is
  superseded by the v8-baseline goal above — but the *existing* v8 master
  and its benches stay green throughout; the fnet rebuild runs alongside
  until it replaces them wholesale.)

## Original motivation (2026-07-22, still the point)

Three v10-session incidents this catches mechanically:

1. **A missing wire, twice.** `REG_WSEL[i]`'s gated output never fed
   `REG_CELL[i]` — found by hand twice. "Every net has a driver and a
   consumer" is a build error here.
2. **A silent signal collision.** `signal-N` globally substituted for a
   latch role also renamed the ALU CMP block's unrelated `N` operand.
   One allocator owning every `(name, quality)` refuses the second claim
   without an explicit alias.
3. **A placeholder promoted without a design pass.** WIP-blueprint letters
   copied into a Python dict became "official". Explicit `signal x;` (auto)
   vs `signal x = signal-T;` (deliberate pin) forces the decision at the
   declaration.
