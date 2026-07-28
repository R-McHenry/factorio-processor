# Project

Vector-based testbench for Factorio combinator circuits over RCON. Full workflow,
file formats, and API gotchas: see README.md. Environment facts below are load-bearing.

## Environment

- Factorio **2.1 experimental**, headless server, save `claude.zip`.
- The test surface uses a **global power network** — no poles needed; one
  electric-energy-interface powers all combinators.
- Python venv at `.venv\` (has `rcon` installed). Always use `.\.venv\Scripts\python.exe`.

factorio dir:
C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe

run game cmd:
factorio --start-server claude.zip --rcon-port 25575 --rcon-password claude

example talk to factorio:
#!/bin/bash
# rcon.sh — requires: pip install rcon  (or use the mcrcon binary)
python3 -c "
from rcon.source import Client
import sys
with Client('127.0.0.1', 25575, passwd='claude') as c:
    print(c.run('/sc ' + sys.stdin.read()))
"

## Hard rules

- **One placement method only**: `import_stack` → `build_blueprint` → `silent_revive`.
  Never `create_entity` or other placement paths.
- Never paste fixture blueprints at 0,0 — it overlaps the module paste area.

## API gotchas (verified live on this server)

- `get_circuit_network()` takes `defines.wire_connector_id` (circuit_red=1,
  circuit_green=2, combinator_output_red=3, combinator_output_green=4), NOT
  `defines.wire_type` (red=2, green=3). Blueprint wire lists use the same connector ids.
- `electric-energy-interface` cannot be ghost-built with `build_mode.forced` (0 ghosts,
  no error) — use `build_mode.normal`.
- `import_stack()` returns a number; 0 = success.
- Unpowered combinators compute nothing: check `entity.status` (working=1, no_power=58).
- Unit numbers change on every clear+paste; map blueprint entity_numbers to live
  unit_numbers by geometry (the runner does this).
- A constant-combinator filter row pasted WITHOUT `"quality"` imports as
  quality=nil and emits NOTHING (filter reads back as present — silent).
  Always emit fully-qualified type/name/quality in filters
  (netlist.py `signal_json_fragment` does).
- **Five signals NEVER transmit** (measured 2026-07-27): `signal-unknown` and
  the four blueprint-parameter placeholders `signal-item-parameter`,
  `signal-fluid-parameter`, `signal-fuel-parameter`, `signal-signal-parameter`.
  A constant combinator accepts them as filters and reads them back as
  present — same silent shape as the missing-quality gotcha — but they never
  appear on a wire. They are in `signal_space.EXCLUDED` now; before that they
  were addressable rows that swallowed every write and read back nothing.
  Found because a whole-space vector reduction came out 5250 short of
  N(N+1)/2; a live diff of a populated address table against its own output
  net named all 25 missing rows (5 names x 5 qualities).

## Timing: the cost is RCON, NOT the game (measured 2026-07-27)

The game is nearly free — sim ~3000 ticks/s, paste ~33ms. Bench wall-time was
RCON-bound; three fixes took the full 25-bench suite from ~5-6 min to **~17s**
(bench portion ~230s → ~14s). All in factorio_memory_tb.py, all correctness-
neutral (still paused single-stepping, one tick at a time):

1. **game.speed = 30 for the run** (restored to 1 in finally; override via
   `"sim_speed": N` tb key or `TB_SIM_SPEED=N`). One RCON round-trip == one
   server update frame == **16.67 / game.speed ms**, payload-independent, same
   while paused. So speed 30 → 0.55ms/round-trip (from 16.67).
2. **Persistent RCON connection** (`_RCON_POOL`, reconnect-and-retry-once). A
   fresh Client per call re-paid TCP-connect + RCON-auth every time; once (1)
   removed the frame latency, that handshake was the dominant per-call cost.
3. **Bulk signal-table writes.** The real floor was the four ~2477-row address
   tables: per-slot `set_slot` cost ~0.6ms/row (game.speed does NOT help —
   `/sc` runs synchronously in-frame). Bulk `sec.filters = {...}` is ~0.01ms/row
   (~60x). RCON REQUESTS accept ~277KB, so a whole table (~229KB) goes in one
   command; identical tables (shared exclusion set) compute rows once. This was
   ~94% of processor-bench time — the reason (1)+(2) alone only got processors
   ~1.7x while components got 5-7x.

Expected wall-times **at the current defaults** (past ~2x, something is wrong —
single-instance lock, hung RCON, crashed server, block-buffered pipe hiding
progress — investigate, don't wait):

- Headless server boot to RCON-ready: **~15-25s** (independent of game.speed)
- Any single bench, component or processor-scale: **0.5-1.5s**
- `--regen` step (compiles + program gen, pure Python, no RCON): **~90s**
- Full 34-bench suite (server already up): **~51s**; incl. --regen: **~2 min**
  (the two mandelbrot benches — hand-written and DSL-compiled — are ~14s each
  of that: ~5200 ticks plus a lane-by-lane readback, real compute, not a stall)

RCON asymmetry to remember: RESPONSES corrupt the connection past ~4096 bytes
(entity dumps are chunked for this), but REQUESTS accept ~277KB. run_all.py
streams a flushed per-bench progress line — an empty background output file does
NOT mean the run is hung.

## Common commands

Full suite (34 benches; boots/stops the server itself, regenerates program variants):
.\.venv\Scripts\python.exe run_all.py --start-server --regen

Offline unit tests (no game): test_vlang.py (23, the compiler),
test_machine_v8.py (15, the scheduler). test_netlist.py has one PRE-EXISTING
failure — demo_circuit.source.json is a hand-written reference whose
combinators carry direction=4 and netlist.py no longer emits a direction; the
live netlist_demo bench passes.

The master is the v8 processor (latched port A, pc_inject relative jumps,
memory-mapped port B, accumulate cells, negatives). v8 programs are scheduled by
machine_v8.py (IR8); the v7-era scheduler.py/verifier.py remain for the archived
v7 ISA (modules/processor_v7.source.json). New BPs pasted in chat are imported
with tools/import_master_bp.py (inlines named logistic groups — force-state
hazard — clears ROM, marks address tables).

The fnet rebuild of v8 is complete and runs programs: modules/v8_processor.fnet
(--top processor) composes the seven component modules; machine_v8.MachineConfig
/ config_from_signals_map schedule against the compiled artifact's own
addresses; tools/build_fnet_v8_tests.py generates program variants + benches
(fib_cmp exact finals live). Both masters run side by side in the suite.

Run one testbench:
.\.venv\Scripts\python.exe factorio_memory_tb.py run --source modules/demo_circuit.source.json --testbench testbenches/memory_basic.tb.json --results results/memory_basic.results.json

**The LANGUAGE is built — start at LANGUAGE.md.** `vlang.py` compiles a Python
DSL (operator overloading -> hash-consed DAG -> maximal-munch selection ->
register allocation -> IR8); `test_vlang.py` is its 23 offline tests. The
acceptance test passed: `v10_proc_mandelbrot_dsl` recompiles the mandelbrot
kernel from the four-line source and carries the IDENTICAL 38 expectations as
the hand-written `v10_proc_mandelbrot`, matching it move for move (8 seed, 11
per pass) with one register fewer. Both run in the suite, so any divergence is
a failing bench. **What it does NOT have yet is LANGUAGE.md §8** (one loop per
program, no `if`, no scalar type, no subscripting) — the top item there is
IR8's `jump_rel_if` + forward labels, which is a backpatch pass, not hardware.

Two rules the compiler enforces that are easy to get wrong by hand:
- a value from an EARLIER BLOCK is opaque to the pattern matcher (it exists
  only as a register, so its defining expression must not be munched into);
- a destination may not be an operand of its own unit, because `vec_move`
  erases the destination before pulsing W.

vlang.py also owns the mux index map (A_MUL..LANE_IN); build_v10_tests.py
imports it rather than keeping a second copy.

The isolation layer under it all is unchanged: `IR8.vec_move` / `vec_select` /
`vec_read_lane` are the ONLY code that knows a vector action is currently
memory-mapped writes, so the planned instruction decoder (~5x on vector code)
is a backend swap that no compiled program can notice.

v8 brief for a fresh session (hardware, measured timing, compiler, gotchas, roadmap): V8.md
SIMD + radar-IO extension design (brainstorm state, lane conventions, bench plan): SIMD.md
Processor design + measured timing rules: modules/processor.design.md
v7-era scheduler plan (phases 4-5 still open, superseded details): SCHEDULER_PLAN.md
**The v10 vector machine computes: a mandelbrot kernel runs over all 2451
lanes — start at MANDELBROT.md** (what it computes, the three hardware
additions it needed, the four programming idioms, the gotchas that bit, and
what is deliberately not built). The composed machine is
modules/v10_processor.fnet (--top v10_processor, 203 entities): the fnet v8
scalar core with the vector zone hung off its memory bus. A vector op is an
ordinary write_imm to a memory row — there is no vector instruction set — and
machine_v8 knows the zone's timing, so programs need no hand-inserted settle
stages. Worked examples of every idiom: tools/build_v10_tests.py.

Three v10 facts that decide how a program is written, all learned the hard way
(details in MANDELBROT.md §4):
- **A lane that computes to 0 vanishes**, and `each` against a fixed signal
  iterates only the first operand's lanes — so an offset must be applied as an
  ADDEND on the second mux selector, never as a VS_SUB after a multiply.
- **An instruction immediate is 20 bits SIGNED**, which is what caps a
  fixed-point scale (the escape test needs 4*S^2 <= 524287).
- **The ROM shadows low memory rows**: its rows ARE the slot addresses the
  program occupies, so any row a long program PORT-READS must sit above the
  span (scratch above the PC at 2200+; loop counters through the ALU, not the
  accumulate cell at 300).

Lamp-matrix display (one lamp per vector lane; pasteable bps for the machine,
the display, and the two together; the packed-RGB grey-ramp check): DISPLAY.md
v10 processor design + current status (datapath resolved, 12-register bank and
square block live, whole machine computing):
modules/processor_v10.design.md
Netlist/HDL layer (.fnet DSL -> source.json; compiler netlist.py + test_netlist.py; signals auto-assigned + published in the source payload's "signals" map, testbenches drive them as "$name"; signal_space tables via signal_table markers with carrier exclusion; current goal = fnet-generated v8 baseline, component roadmap inside): NETLIST_PLAN.md

Results viewer (with Rerun & Reload):
.\.venv\Scripts\python.exe serve_results.py
then open http://127.0.0.1:8765/results_viewer.html
