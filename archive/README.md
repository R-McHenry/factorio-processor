# archive — superseded, kept whole

Three earlier generations, each retired because something in `../main/`
replaced it wholesale. Nothing here is imported by `main/`; nothing here is
maintained. It is kept because each one was verified live at the time and the
measurements in it are still the evidence behind rules `main/` now relies on.

| directory | what it was | replaced by |
|---|---|---|
| `v7/` | the v7 ISA: a list scheduler with a separate post-hoc schedule linter, plus the hand-built v7 processor masters and their 7 benches | `main/processor/isa.py`, which folds the linter's rules into scheduling |
| `pre_fnet_v8/` | the v8 machine as hand-pasted blueprint strings, imported and patched by tool, with 6 benches pinning ISA behaviour | `main/fnet/` — the same machine described in `.fnet` and compiled |
| `v10_generator/` | the v10 vector zone emitted by hand-written Python blueprint generators | the same, in `.fnet` |
| `dead/` | orphans: two fib program variants nothing referenced, three testbenches not in any suite, a scratch paste buffer | — |

## What is worth reading here

**`v7/SCHEDULER_PLAN.md`** is the origin of the scheduling model that
`main/processor/isa.py` still uses — staged IR, barriers, labels, and the
consumer-slot frame. Phases 4 and 5 in it were superseded (a tick simulator is
now explicitly not wanted; see `../plan/ROADMAP.md` §6).

**`v7/verifier.py`** replays latch/port/cell/ALU/jump timelines over a finished
`Program` and flags violations. Its rules live inside the scheduler now, but it
is the only standalone post-hoc linter that has ever existed, and
`../plan/ROADMAP.md` §6 wants one again for hand-written ROMs.

**`pre_fnet_v8/`** is the evidence for the measured timing table. Each of its
six benches isolates one ISA feature — relative jump, latched read, accumulate
cells, memory-mapped port B, the fib loop, the spin counter — and the numbers
in `main/processor/docs/isa.md` came from them.

## Running any of it

These were removed from the suite in the 2026-07-28 split, but nothing about
them is broken — they were all green when archived. They need their imports
repointed at `main/` (the modules were renamed: `machine_v8` → `processor.isa`,
`factorio_memory_tb` → `bench.processor_tb`, `netlist` → `fnet.hdl`,
`factorio_blueprint_codec` → `blueprint_codec`) and their `--source` /
`--testbench` paths pointed here. That was not done as part of the split,
because doing it would have meant maintaining them.
