# Roadmap — everything not yet built (consolidated 2026-07-28)

This is the single forward-looking list. It was gathered out of four documents
during the archive/main/plan split, so that `main/` records only what exists
and this file records only what does not:

| came from | now |
|---|---|
| `V8.md` §TODO | §1, §2, §3, §6 below |
| `MANDELBROT.md` §7 "Not built" | §4 below |
| `LANGUAGE.md` §7 "not waiting for" + §8 "does not have yet" | §1, §5 below |
| `NETLIST_PLAN.md` "Later / deferred" | §7 below |

Reference material for all of it lives in `main/processor/docs/` and
`main/fnet/README.md`. Items are ordered by what unblocks the most.

**§3, the vector-zone decoder, was BUILT on 2026-07-28** and its record moved
into `main/` — see `main/processor/docs/isa.md` "Vector control: two planes,
one set of gates" and `main/processor/modules/v10_vec_decoder.fnet`. A vector
move went from 15.5 ticks to 3.9 and the mandelbrot loop body from 171 ticks a
pass to 43, with byte-identical expectations. What is left of it is §3 below,
which is now only the follow-on tuning.

**Next up: §1**, the top blocker — with vector code no longer the bottleneck,
what limits programs is control flow.

---

## 1. `jump_rel_if` + forward labels — the top blocker

**Nothing about this is hardware.** The ISA half is measured
(`v8_proc_computed_jump`, 2026-07-28): `IR8.jump_rel_a(src)` routes port A to
out4, so the jump distance is a memory row, and `mem[JV] = flag * skip` falls
through at 0 and skips at 1. Both halves are verified, and the not-taken one
matters more — **P = 0 is self-healing**: the inject fires with zero, the PC
cell emits nothing, the autoincrement re-seeds, execution continues.

What is missing is a **backpatch pass in the ISA layer**. `_label_slot` needs
its target already scheduled, which is why `jump_rel` says "backward jumps
only". The pattern is already proven — `computed_jump` patches its own
distance into the ROM after scheduling, because the distance depends on where
the scheduler put things.

Two things fall out the moment it lands:

- **`lang.Machine.loop` currently refuses a second loop** and there is no `if`
  at all. Both are this, and only this.
- `if cond: BODY` becomes: compute the flag, scale it by `len(BODY)` plus the
  shadow, `a->out4`, four shadow slots (they execute on BOTH paths, so they
  must be NOPs or valid either way), then BODY. Strictly better than the
  write-path `jump_if_zero` the loops use — it does not occupy the write latch,
  and it saves ~5 ticks off a loop tail.

Also still open from the original item: `b->out4`, and an `IR8.jump_rel_if`
that assembles the `flag * skip` multiply itself instead of taking a
pre-scaled row.

**Prefer predication where it fits.** `write_addr = trash + flag*(real-trash)`
sends a write to a scratch row or the real one depending on a flag — a
conditional assignment with *zero* jump shadow. The compiler should reach for
that on single-assignment conditionals and keep the jump for blocks.

---

## 2. External I/O — the one genuinely missing capability

The stated goal is to control a factory, and this is what stands between the
machine and that. Radar gives a **zero-tick wireless link**, so the intended
shape is: memory-map external devices onto the same bus, with a select line
each device compares against its own id — the identical pattern the vector
zone's `vec_wsel` / `vec_erase` block ids already use. More commands are then
rows, not rework.

Independent of everything else here; can land in parallel. Design notes and
the lane conventions are in `SIMD.md`.

---

## 3. Vector-move cost — what is left after the decoder

The decoder itself is **built** (2026-07-28): word 2 of each instruction drives
`vec_rsel` / `vec_ssel` / `vec_wsel` / `vec_erase` out of a private ROM2 read at
the same fetch address, every gate in the zone ORs the two control planes, and
a move went from 15.5 ticks to 3.9 with byte-identical mandelbrot expectations.
The record lives in `main/`: `main/processor/docs/isa.md` for the measured
table and the word-2 layout, `main/processor/modules/v10_vec_decoder.fnet` for
the hardware and why each depth is what it is.

Three things were deliberately left:

- **Source-aware move depth — the biggest remaining win.** What costs 3.9 ticks
  a move is no longer the control plane; it is the scheduler applying the
  `vec_wsel -> VEC_BUS` LATEST depth of 4 to every move, because the op farm's
  square block can be that deep. A move whose source is a plain register needs
  2, a one-deep op needs 3, and only B_SQDIFF needs 4 — and the move op already
  knows its own source index. Summing the real depths over the mandelbrot loop
  gives 30 rather than 44, so the body should land near 30 ticks. Not attempted
  yet because the flat bound is the table the zone was verified against, and a
  wrong depth here is a silently wrong frame, not a crash.

- **The spare mode bit.** Word 2 uses 22 of 32 bits. The intended use for one
  spare was a flag meaning *"this instruction's scalar immediate is
  `vec_bcast`"*, so `frame*k` carries its constant in word 1's existing
  immediate instead of a separate `write_imm` five ticks ahead. In the
  mandelbrot loop that is three writes a pass out of fourteen instructions.
  `vec_bcast` / `vec_bcast2` stay memory-mapped either way — they are 20 bits
  each and there is no room for them as fields.

- **Nothing exercises the memory-mapped move path end to end any more.** The
  v10 program benches all take the decoder now; the component benches
  (`v10_vec_reg`, `v10_vec_bank`) still drive the memory-mapped rows directly,
  and `IR8.vec_move(..., via="memory")` still compiles and is unit-tested, but
  no whole-machine bench runs a program that way. That is a deliberate
  trade — the alternative was a 23rd bench duplicating mandelbrot at 4x the
  ticks — recorded here so it is a decision rather than an oversight.

---

## 4. Vector hardware not built

- **Reductions beyond `vred_sum` / `vred_count`** — max, min, argmax, argmin,
  the CMP flags. Addresses are allocated, hardware is not.
- **Multi-chunk port A** — reading a vector lane by a chunk-2..9 address. Not
  needed so far: `vec_rlane_addr` already reads any lane one at a time, which
  is what makes the scanline the expensive third of the mandelbrot program.
- **The quality-transfer unary op** (`vqual_level`).
- **UPS measurement** — deferred deliberately.

---

## 5. Language features not built

Each is an evening rather than a project; the compiler is ~700 lines.

- **Scalar (`int`) values.** The type table has three rows and only `vec`
  exists. Scalar rows are written directly through the ISA layer by the bench
  builders, and the loop counter is emitted by `Machine.loop` itself. Adding
  `int` means a second, much easier allocator over memory rows — and it is
  what `vred_count` / `vred_sum` results want to flow into.
- **Subscripting.** The hardware has had it since the interconnect matrix was
  fully driven: `copy_a_indexed` / `write_indexed` sit in the ISA layer unused
  by any front-end. Arrays are one route-bit pair.
- **Predication** — see §1.
- **A text front-end.** Still the cheap part, still optional; nothing in the
  compiler depends on which front-end produced the tree, and `fnet`'s
  hand-rolled tokenizer (~150 lines) is reusable onto the same IR.
- **Spilling stays a hard error**, by design. Eight variables has been enough
  (mandelbrot fits in six). When it stops being enough the answer is more
  registers — three combinators each, and mux indices 13..16 are already
  reserved — not a spill path through `vec_read_lane`.

---

## 6. Scheduler / ISA work

- **Delay-slot filling.** Jump shadows execute; the scheduler reserves them as
  NOPs. Hoisting hazard-free ops into the 4–6 shadow slots is pure compiler
  work, worth ~15–20% on jump-heavy code — fib idles 6 of 26 ticks in the jump
  shadow alone.
- **Operand-cell register allocation.** fib's `M ← H` exists only to feed CMP:
  6+ ticks/pass of aliasing overhead. Letting the scheduler assign loop
  variables directly into ALU operand cells (or adding compare-against-cell
  hardware) takes the fib period from ~26 to ~20.
- **A standalone linter for hand-written ROMs.** Scheduling-time checks exist;
  a post-hoc pass like the archived `verifier.py` did for v7 does not.

**A tick simulator is explicitly NOT wanted** (superseding the older
SCHEDULER_PLAN Phase 4 item). The bench scaffolding is the answer; if it
becomes the bottleneck, make it faster or more thorough in-game.

---

## 7. fnet / HDL layer

- **Auto-color pass**: assign colors to unconstrained nets and rewrite the
  `*_networks` config fields to match. Needed before modules compose freely
  without fixed-color port friction.

**Non-goals, recorded so they are not re-opened:** fnet is not a
general-purpose HDL — primitives grow only as idioms recur — and it does not
model tick-level timing or quality arithmetic. It is structural connectivity
plus a symbol table; timing lives in the ISA layer.

---

## 8. Known defects — three failing HDL tests

`main/fnet/test_hdl.py` is **29 pass / 3 fail**, and was exactly that before
the 2026-07-28 restructure too (verified by running the pre-split code from
the snapshot commit). None of the three affects a live bench: `memory_basic`
and `netlist_demo` both pass in-game, so the compiler's output is fine.

- `test_demo_matches_demo_circuit_topology` — `demo_circuit.source.json` is a
  hand-written reference whose combinators carry `direction: 4`; the compiler
  no longer emits a direction, so the comparison trips. Either regenerate the
  reference or drop the assertion.
- `test_route_repairs_reach_with_inert_repeaters` — `route()` is expected to
  insert repeaters and does not.
- `test_wire_reach_error` — a reach violation expected under declaration order
  is not raised.

The last two are the same area (wire reach and repeater insertion) and are
probably one fix. Nobody has looked at them; they may be stale tests rather
than real defects, since the layout code was reworked when combinators were
rotated upright.
