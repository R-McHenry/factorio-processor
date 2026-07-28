# Project

A programmable vector processor built out of Factorio combinators, plus the whole
toolchain that produces it. Split three ways, deliberately:

- **`main/`** — the deliverable. Everything that works, nothing else. **No plans
  live here**, so anything you read in it is true of the code as it stands.
- **`plan/`** — everything not yet built. Start at `plan/ROADMAP.md`.
- **`archive/`** — superseded generations (v7, pre-fnet v8, the pre-HDL v10
  generator), kept whole but unmaintained. Nothing in `main/` imports it.

When you finish something from `plan/`, move the record into `main/` and delete
the plan entry. That invariant is the whole point of the split.

## Environment

- Factorio **2.1 experimental**, headless server, save `claude.zip`.
- The test surface uses a **global power network** — no poles; one
  electric-energy-interface powers all combinators.
- Python venv at the repo root: always use `.\.venv\Scripts\python.exe`.
- `C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe`
- `factorio --start-server claude.zip --rcon-port 25575 --rcon-password claude`

## Layout inside main/

It follows the import graph exactly, and the layering is enforced by nothing
but discipline — keep it:

```
main/
├── signal_space.py     the Factorio signal address space  (no local deps)
├── blueprint_codec.py  blueprint-string decode/encode      (no local deps)
├── paths.py  run_all.py
├── fnet/       hdl.py, test_hdl.py, demo/     .fnet -> source.json
├── bench/      processor_tb.py, serve_results.py, results_viewer.html
└── processor/  isa.py, lang.py, assembler.py, modules/, testbenches/,
                tools/, docs/
```

**Nothing in `fnet/` or `bench/` may import `processor/`.** Both are general —
the HDL compiles any circuit, the harness runs any blueprint.

Imports are absolute from `main/`: `import signal_space`,
`from processor.isa import IR8`, `from bench.processor_tb import run_sc`. Every
script below `main/` root puts `main/` on `sys.path` first.

Modules were renamed in the 2026-07-28 split — old names appear in `archive/`
and in git history: `machine_v8`→`processor.isa`, `vlang`→`processor.lang`,
`netlist`→`fnet.hdl`, `factorio_memory_tb`→`bench.processor_tb`,
`factorio_blueprint_codec`→`blueprint_codec`.

## Hard rules

- **One placement method only**: `import_stack` → `build_blueprint` →
  `silent_revive`. Never `create_entity` or any other path.
- Never paste fixture blueprints at 0,0 — it overlaps the module paste area.
- **Never probe `vbus` or `membus`** — they carry thousands of signals and RCON
  responses corrupt past ~4096 bytes. Probe the memory cell's red state loop.

## API gotchas (verified live on this server)

- `get_circuit_network()` takes `defines.wire_connector_id` (circuit_red=1,
  circuit_green=2, combinator_output_red=3, combinator_output_green=4), NOT
  `defines.wire_type`. Blueprint wire lists use the same connector ids.
- `electric-energy-interface` cannot be ghost-built with `build_mode.forced`
  (0 ghosts, no error) — use `build_mode.normal`.
- `import_stack()` returns a number; 0 = success.
- Unpowered combinators compute nothing: check `entity.status` (working=1,
  no_power=58).
- Unit numbers change on every clear+paste; map blueprint entity_numbers to
  live unit_numbers by geometry (the runner does this).
- A constant-combinator filter row pasted WITHOUT `"quality"` imports as
  quality=nil and emits NOTHING, while reading back as present — silent.
  Always emit fully-qualified type/name/quality.
- **Five signals NEVER transmit**: `signal-unknown` and the four
  blueprint-parameter placeholders. They accept writes and read back as
  present but never appear on a wire. They are in `signal_space.EXCLUDED`;
  before that they silently swallowed every write (found because a whole-space
  reduction came out 5250 short of N(N+1)/2).
- **A headless server with no players connected AUTO-PAUSES.** Advance time
  with `game.ticks_to_run`, never by clearing `game.tick_paused` — the latter
  crawls at ~0.2 UPS and looks like a hardware problem. This cost a session.

## Timing: the cost is RCON, NOT the game

The game is nearly free — sim ~3000 ticks/s, paste ~33ms. Three fixes took the
suite from ~5-6 min to under a minute, all correctness-neutral:

1. **game.speed = 30 for the run** (restored in `finally`; override via the
   `"sim_speed"` tb key or `TB_SIM_SPEED`). One RCON round-trip == one server
   update frame == **16.67 / game.speed ms**, payload-independent, same while
   paused. Speed 30 → 0.55ms/round-trip.
2. **Persistent RCON connection** (`_RCON_POOL`, reconnect-and-retry-once).
3. **Bulk signal-table writes.** Per-slot `set_slot` costs ~0.6ms/row and
   game.speed does not help (`/sc` runs synchronously in-frame). Bulk
   `sec.filters = {...}` is ~0.01ms/row (~60x). A whole ~229KB table goes in
   one command; identical tables compute their rows once.

Expected wall-times (past ~2x, something is wrong — single-instance lock, hung
RCON, crashed server, block-buffered pipe hiding progress — investigate, don't
wait):

- Headless boot to RCON-ready: **~15-25s**
- Any single bench: **0.5-1.5s**, except the two mandelbrots at **~14s** each
  (~5200 ticks plus a lane-by-lane readback — real compute, not a stall)
- `--regen` (pure Python, no RCON): **~90s**
- Full 21-bench suite: **~50s**; incl. `--regen`: **~2 min**

`run_all.py` streams a flushed per-bench progress line — an empty background
output file does NOT mean the run is hung.

## Common commands

```
.\.venv\Scripts\python.exe main\run_all.py --start-server --regen   # 21 benches
.\.venv\Scripts\python.exe main\processor\test_isa.py               # 15 tests
.\.venv\Scripts\python.exe main\processor\test_lang.py              # 23 tests
.\.venv\Scripts\python.exe main\fnet\test_hdl.py                    # HDL tests
```

One bench, and the viewer:

```
.\.venv\Scripts\python.exe main\bench\processor_tb.py run ^
  --source    main\processor\modules\v10_processor_mandelbrot_dsl.source.json ^
  --testbench main\processor\testbenches\v10_proc_mandelbrot_dsl.tb.json ^
  --results   main\results\v10_proc_mandelbrot_dsl.results.json
.\.venv\Scripts\python.exe main\bench\serve_results.py   # :8765/results_viewer.html
```

## Where the detail is

- `main/README.md` — how a run works, the file formats, the layering
- `main/processor/docs/isa.md` — v8 hardware, the measured timing table, the
  compiler's rules
- `main/processor/docs/language.md` — the DSL, the pattern table, the allocator
- `main/processor/docs/mandelbrot.md` — the kernel, the four idioms, what bit
- `main/processor/docs/display.md` — the 2451-lamp matrix
- `main/processor/docs/processor_design.md`, `processor_v10_design.md`
- `plan/ROADMAP.md` — everything not built
- `archive/README.md` — what each retired generation was, and what is still
  worth reading in it

## Three facts that decide how a program is written

- **A lane that computes to 0 vanishes**, and `each` against a fixed signal
  iterates only the first operand's lanes — so an offset must be applied as an
  ADDEND on the second mux selector, never as a VS_SUB after a multiply. The
  language enforces this by canonicalising `a - k` to `a + (-k)`.
- **An instruction immediate is 20 bits SIGNED**, which is what caps a
  fixed-point scale (the escape test needs 4*S^2 <= 524287).
- **The ROM shadows low memory rows**: its rows ARE the slot addresses the
  program occupies, so any row a long program PORT-READS must sit above the
  span (scratch above the PC at 2200+; loop counters through the ALU, not the
  accumulate cell at 300).

## Etiquette

The designer plays on `claude.zip` and works in-game interleaved with these
sessions. Stop the server when done (`run_all.py --start-server` does it).
A running client holds the single-instance lock — if the server refuses to
boot, check for a `factorio.exe` process and **wait, don't kill it**.
