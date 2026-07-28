# Superseded: the pre-fnet v10 generator (archived 2026-07-27)

Everything in this directory is **frozen and not part of the build**. Nothing
in the live toolchain imports it, `run_all.py` does not reference it, and the
relative imports between these files are intentionally left broken by the
move — they are here to be read, not run.

It is archived rather than deleted because the project is not under version
control, so a delete would be unrecoverable.

## What replaced it

| Archived | Replaced by |
| --- | --- |
| `v10_address_map.py` | `modules/v10_addr_map.fnet` |
| `generate_v10_processor.py`, `generate_v10_vector_zone.py` | `modules/v10_vec_reg.fnet`, `v10_vec_bank.fnet`, `v10_op_farm.fnet`, `v10_vec_io.fnet` |
| `v10_full.bp.txt`, `v10_vector_zone.bp.*` | compiled `modules/v10_*.source.json` |
| `build_v10_test_mul.py`, `v10_test_mul.*` | `testbenches/v10_vec_*.tb.json`, `v10_op_farm.tb.json`, `v10_vec_io.tb.json` |

## Why it was superseded

The generator produced a 194-entity blueprint that pasted and powered
correctly but showed **no signs of life on any probe**
(`processor_v10.design.md`, open question #1, 2026-07-22). The fnet rebuild
found the cause: the register hold cells were never configured — six of the
eight still carried Factorio's unconfigured placement default,
`signal-no-entry != N`, which `v10_address_map.py` itself recorded without
connecting it to the dead write path. Rebuilt bottom-up with a live bench per
component, the write path worked first try.

## One specific trap this directory used to set

`v10_address_map.py` opens by declaring itself "the SOLE authority on what any
v10 signal means". **That is no longer true.** The authority is
`modules/v10_addr_map.fnet`, whose rows are real `memory[...]` addresses in
the design's own space, resolved by the netlist compiler and read back by
tooling from the compiled `signals` map. The carrier-obscurity ranking in
`carrier_candidates()` does live on, reimplemented as `auto_signal_pool()` in
`netlist.py` — that is the only idea from this directory still in use.
