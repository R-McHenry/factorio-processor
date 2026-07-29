# main — the deliverable

Everything that currently works. No plans (those are in `../plan/`), no
superseded generations (`../archive/`). If it is described here, it exists and
a bench proves it.

```
.\.venv\Scripts\python.exe main\run_all.py --start-server --regen   # 22 benches, ~2 min
.\.venv\Scripts\python.exe main\processor\test_isa.py               # 22 scheduler tests
.\.venv\Scripts\python.exe main\processor\test_lang.py              # 23 compiler tests
.\.venv\Scripts\python.exe main\fnet\test_hdl.py                    # HDL tests
```

## Layout, which follows the import graph exactly

```
main/
├── signal_space.py       the Factorio signal address space — depends on nothing
├── blueprint_codec.py    blueprint-string decode/encode — depends on nothing
├── paths.py              where things live
├── run_all.py            the whole suite against one server boot
├── fnet/                 the HDL layer      (imports the two above, never processor/)
├── bench/                the test harness   (imports signal_space, never processor/)
└── processor/            the machine        (sits on top of all of it)
```

Nothing in `fnet/` or `bench/` imports `processor/`. Both are genuinely
general — the HDL compiles any combinator circuit, the harness runs any
blueprint against any testbench.

## The layers

| layer | file | what it does |
|---|---|---|
| HDL | `fnet/hdl.py` | `.fnet` netlist source → `source.json` blueprint. Signals auto-assigned and published in the payload's `signals` map; address tables emitted as markers the runner expands. |
| ISA | `processor/isa.py` | `IR8` → list scheduler → ROM. Owns **every** timing rule — port latch, cell freshness, ALU latency, jump shadows, vector-zone propagation. Nothing above it knows a tick exists. |
| language | `processor/lang.py` | Python DSL → hash-consed DAG → maximal-munch selection → register allocation → `IR8`. |
| harness | `bench/processor_tb.py` | Pastes a blueprint, drives inputs, single-steps ticks, samples probes, checks expectations. |
| viewer | `bench/serve_results.py` + `results_viewer.html` | ILA-style waveform view with Rerun & Reload. |

`processor/docs/` carries the detail: the ISA brief and measured timing table
(`isa.md`), the language (`language.md`), the mandelbrot kernel
(`mandelbrot.md`), the lamp display (`display.md`), and the two design
documents.

## Running one bench

```
.\.venv\Scripts\python.exe main\bench\processor_tb.py run ^
  --source    main\processor\modules\v10_processor_mandelbrot_dsl.source.json ^
  --testbench main\processor\testbenches\v10_proc_mandelbrot_dsl.tb.json ^
  --results   main\results\v10_proc_mandelbrot_dsl.results.json
```

Then view it:

```
.\.venv\Scripts\python.exe main\bench\serve_results.py
# open http://127.0.0.1:8765/results_viewer.html
```

## How a run works

1. **Clear + paste.** Everything on the surface (except characters) is
   destroyed, then the source blueprint is pasted at `paste_origin_x/y` via the
   one placement method: `import_stack` → `build_blueprint` → `silent_revive`
   on each ghost. No other placement path is used anywhere.
2. **Fixtures.** Each `fixture_blueprints` entry is pasted the same way at its
   own origin, with `build_mode.normal` — an `electric-energy-interface`
   silently refuses to ghost-build under `forced`. The `power_seed` fixture is
   a lone EEI; the surface has a global power network, so no poles.
3. **Entity mapping.** Placed entities are matched back to blueprint
   `entity_number`s by name + relative geometry, so unit numbers are never
   hardcoded and survive every rebuild.
4. **Address tables.** Constant combinators carrying a `signal_table` marker
   are filled over RCON in bulk (~2451 rows each, seven of them) rather than
   being checked into JSON.
5. **Timeline execution.** The game is tick-paused and stepped one tick at a
   time (`game.ticks_to_run = 1`), sampling every probe each tick. A tick skip
   is a hard error, so traces are gap-free.
6. **Checks.** The last sample of the expected probe is compared
   signal-by-signal against each step's `expect` map.

## Formats

**`*.tb.json`** — `entity_map.drivers` (role → entity_number), `output_probes`
(`{name, entity_number, wire}`), `expected_probe_name`, and a `timeline` of
`{name, write?, read_addr?, expect?, skip_ticks?, settle_ticks?, trace_ticks?}`.
Expectations may reference the design's own symbols as `"$name"` or
`"$mem[N]"`. Fixtures never go at 0,0 — that overlaps the paste area.

**`*.results.json`** — paste status, the entity mapping, one sample per tick,
per-step checks, and an overall `pass`.

## Factorio API gotchas (2.1 experimental, all learned the hard way)

- `get_circuit_network()` takes `defines.wire_connector_id` (`circuit_red=1`,
  `circuit_green=2`, combinator outputs `3`/`4`) — **not** `defines.wire_type`.
  Blueprint wire lists use the same connector ids.
- `electric-energy-interface` cannot be ghost-built with `build_mode.forced`
  (0 ghosts, no error). Use `normal`.
- `import_stack()` returns a number; `0` = success, not a boolean.
- Combinators need power to compute — check `entity.status` (`working=1`,
  `no_power=58`).
- A constant-combinator filter row pasted **without** `"quality"` imports as
  quality=nil and emits nothing, while reading back as present. Always emit
  fully-qualified type/name/quality.
- **Five signals never transmit**: `signal-unknown` and the four
  blueprint-parameter placeholders. They accept writes and read back as
  present, but never appear on a wire. They are in `signal_space.EXCLUDED`.
- RCON is asymmetric: **responses** corrupt past ~4096 bytes (so entity dumps
  are chunked, and wide nets are never probed), but **requests** accept ~277KB.
- A headless server with no players connected auto-pauses. Advance time with
  `game.ticks_to_run`, never by clearing `game.tick_paused`.
