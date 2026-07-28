# Factorio Circuit Testbench

Vector-based testing of Factorio combinator circuits over RCON, with single-tick
resolution and an ILA-style waveform viewer. Think "HDL testbench, but the DUT is
a Factorio blueprint".

## Files

| File | Role |
| --- | --- |
| `factorio_memory_tb.py` | Testbench runner: pastes the circuit, drives inputs, single-steps ticks, samples probes, checks expectations |
| `factorio_blueprint_codec.py` | Standalone blueprint-string decode/encode utility |
| `assembler.py` | Instruction encoder + slot-based `Program` builder (measured timing rules) |
| `verifier.py` | Schedule linter: replays latch/port/cell/ALU/jump timelines over a `Program`, flags rule violations (e.g. the fib I=H+H transient) |
| `scheduler.py` | v7-ISA list scheduler: staged IR (`copy`/`write_imm`/`jump_if_zero` + `barrier`/`label`) → auto slot/port assignment → verified `Program` |
| `machine_v8.py` | v8-ISA compiler: IR8 (`copy_a`/`park_b`/`copy_b`/`add_imm`/`jump_rel`/`halt`) for the latched-port/pc_inject/accumulate machine |
| `vlang.py` | The language: a Python DSL (`Machine`/`Vec`, operator overloading) → hash-consed DAG → maximal-munch selection → register allocation → IR8. Also owns the v10 mux index map |
| `test_vlang.py` | Compiler self-tests (DAG sharing, the pattern table, the ≤11-move allocation target) — all offline |
| `V8.md` | Complete v8 brief: hardware, measured timing table, compiler rules, gotchas, roadmap |
| `LANGUAGE.md` | The language: what was built, what it deliberately lacks (§8), and the acceptance test |
| `test_verifier.py`, `test_scheduler.py` | Self-tests incl. the I=H+H regression fixture |
| `tools/import_master_bp.py` | Import a chat-pasted processor BP string as the bench master (clears ROM, marks address tables, backs up the old master) |
| `serve_results.py` | Local web server for the viewer; `POST /rerun` re-executes the testbench |
| `results_viewer.html` | ILA waveform viewer (served by `serve_results.py`) |
| `modules/demo_circuit.source.json` | Source blueprint (decoded JSON, full game fields) of the circuit under test |
| `testbenches/memory_basic.tb.json` | Testbench definition: entity map, probes, fixtures, timeline |
| `results/memory_basic.results.json` | Run output: rebuild status, mapping, tick-by-tick trace, checks |

## Quick start

1. Start the game server (see `CLAUDE.md` for paths):

       factorio --start-server claude.zip --rcon-port 25575 --rcon-password claude

2. Run the testbench:

       .\.venv\Scripts\python.exe factorio_memory_tb.py run ^
         --source modules/demo_circuit.source.json ^
         --testbench testbenches/memory_basic.tb.json ^
         --results results/memory_basic.results.json

3. View results:

       .\.venv\Scripts\python.exe serve_results.py
       # open http://127.0.0.1:8765/results_viewer.html

## How a run works

1. **Clear + paste.** Everything on the surface (except characters) is destroyed, then
   the source blueprint is pasted at `paste_origin_x/y` via the one placement method:
   `import_stack` → `build_blueprint` → `silent_revive` on each ghost.
2. **Fixtures.** Each entry in `fixture_blueprints` is pasted the same way at its own
   origin (`build_mode.normal` — see gotchas). The `power_seed` fixture is a lone
   electric-energy-interface; the surface uses a global power network, so no poles.
3. **Entity mapping.** Placed entities are matched back to blueprint `entity_number`s
   by name + relative geometry (`find_translation`), so unit numbers never need to be
   hardcoded and survive every rebuild.
4. **Timeline execution.** The game is tick-paused; each timeline step writes constant
   combinator filters, then the runner single-steps ticks (`game.ticks_to_run = 1`),
   sampling every probe each tick. A tick-skip is a hard error, so traces are gap-free.
5. **Checks.** After `settle_ticks` + `trace_ticks`, the last sample of the
   `expected_probe_name` probe is compared signal-by-signal against the step's
   `expect` map. Results (trace + checks) are written as JSON.

## Testbench format (`testbenches/*.tb.json`)

- `entity_map.write_entity_number` — constant combinator driven by each step's `write` map.
- `entity_map.read_entity_number` — constant combinator driven by `read_addr`
  (shorthand for `signal-R = <addr>`).
- `entity_map.output_probes` — list of `{name, entity_number, wire}` (wire: `red`|`green`).
  Every probe is sampled every tick and appears in the trace/viewer.
- `entity_map.expected_probe_name` — which probe `expect` assertions check.
- `timeline` — ordered steps: `{name, write?, read_addr?, expect?, settle_ticks?, trace_ticks?}`.
- `pre_stimulus_dead_ticks`, `settle_ticks`, `trace_ticks` — dead ticks before stimulus,
  ticks to wait after driving, ticks recorded per step.
- `fixture_blueprints` — `[{name, blueprint_string, origin_x, origin_y}]`. Never place
  fixtures at 0,0 — that overlaps the module paste area.

## Results format (`results/*.results.json`)

- `rebuild` / `fixtures` — paste status (`import_ok`, `build_ok`, ghost/revive counts).
- `mapping` — translation, `entity_number → unit_number` map, resolved probe units.
- `timeline` — one sample per tick: `{tick, stimulus: {write, read}, observed_by_probe,
  phase, step_index, step_name}`.
- `checks` — per step: expected vs observed on the expected probe, mismatches, pass.
- `pass` — overall.

## Viewer

- Single ILA view: pinned tick axis on top, pinned signal axis on the left; only the
  plot interior scrolls (two scrollbars total).
- Signals are grouped (`write`, `read`, `out:<probe>`); click a group header to
  minimize it to one aggregate row: empty cell if no member has a value that tick,
  `value·<last char of signal name>` if exactly one, `(...)` if several.
- **Rerun & Reload** re-executes the runner via `POST /rerun` and reloads the JSON
  (requires `serve_results.py`; opening the HTML as a plain file disables rerun).
- `Open Results JSON` / `Load default` view a file without rerunning.

## Blueprint codec

    .\.venv\Scripts\python.exe factorio_blueprint_codec.py decode --in <file/bp-string> ...
    .\.venv\Scripts\python.exe factorio_blueprint_codec.py encode --in <decoded.json> ...

Useful for inspecting/authoring `*.source.json` and fixture strings.

## API gotchas (Factorio 2.1 experimental — learned the hard way)

- `LuaEntity.get_circuit_network()` takes `defines.wire_connector_id`
  (`circuit_red=1`, `circuit_green=2`, combinator outputs `3`/`4`) — **not**
  `defines.wire_type` (red=2, green=3). Passing wire_type silently probes the wrong
  connector or returns nil.
- Blueprint wire lists use the same connector ids: `[ent_a, connector_a, ent_b, connector_b]`.
- `electric-energy-interface` has no placing item: `build_blueprint` with
  `build_mode.forced`/`superforced` silently skips it (0 ghosts). `build_mode.normal`
  ghosts it fine, and `silent_revive` completes the placement.
- `LuaItemStack.import_stack()` returns a number: `0` = success (not a boolean).
- Combinators need electric power to compute (`entity.status == defines.entity_status.working`;
  `no_power` = 58). The test surface has a global power network, so one EEI powers everything.
- Unit numbers change on every clear+paste; always re-map via geometry, never hardcode.
