# plan — everything not yet built

Nothing in this directory describes something that exists. Anything that gets
built moves out of here and into `../main/`, so this stays short and stays
true.

| file | what it is |
|---|---|
| **`ROADMAP.md`** | The single forward-looking list. Start here. Consolidated from four documents in the 2026-07-28 split; ordered by what unblocks the most. |
| `NETLIST_PLAN.md` | The HDL layer's design record and remaining phases. Mostly a completed-work log — the language reference it grew out of now lives in `../main/fnet/`. |
| `SIMD.md` | The SIMD + radar-IO design, still at brainstorm state. Lane conventions, the memory-mapped device pattern, and the bench plan for external I/O (`ROADMAP.md` §2). |

The top three items, in order:

1. **`jump_rel_if` + forward labels** — a backpatch pass in the ISA layer, not
   hardware. It is the only reason the language compiles one loop per program
   and has no `if`.
2. **External I/O** — the one genuinely missing capability for the stated goal
   of controlling a factory. Radar is a zero-tick wireless link; memory-map
   devices onto the same bus with a per-device select line.
3. **The vector-zone decoder** — ~5× on vector code, and a pure backend swap
   because `vec_move` already hides how a vector action is realised.
