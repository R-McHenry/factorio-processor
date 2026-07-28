# The language — BUILT (plan written 2026-07-28, §5 steps 2–6 done same day)

The goal, in the designer's words: write `zx' = (zx² − zy²)/S + cx` and have it
run. That is the step that takes this from a machine you can demonstrate to a
machine you can *use* — specifically, to control a factory.

**It runs.** `vlang.py` is the compiler, `test_vlang.py` its 23 offline tests,
and the acceptance test of §6 is live in the suite as `v10_proc_mandelbrot_dsl`
— the mandelbrot kernel recompiled from the DSL, carrying the **identical 38
expectations** as the hand-written one. Suite is **34/34**.

```
.\.venv\Scripts\python.exe run_all.py --start-server --regen   # 34 benches, ~2 min
.\.venv\Scripts\python.exe test_vlang.py                       # 23 compiler tests
.\.venv\Scripts\python.exe test_machine_v8.py                  # 15 scheduler tests
```

The source it compiles is the whole kernel:

```python
zx, zy   = m.vec("zx"), m.vec("zy")
e, esum  = m.vec("e"),  m.vec("esum")
i  = m.coord()
cy = (i // W) * DY - OY
cx = (i %  W) * DX - OX

with m.loop(20):
    r2  = zx*zx + zy*zy
    esc = r2 > 4*S*S
    e   = vmax(e, esc)
    esum = esum + e
    zx, zy = (zx*zx - zy*zy)//S + cx, (zx*zy)//(S//2) + cy
```

**Move for move it matches the hand-written kernel** — 8 in the grid seed, 11
per pass — and uses one register fewer (6, not 7): linear scan spots that the
seed's intermediate dies before its own result is written, where the hand
version kept a dedicated temp. 473 ROM words against 477, the four saved being
a redundant final re-park the compiler's park tracking elides.

The rest of this file is the plan as written, annotated with what actually
happened. §5 records the state; §8 is new, and is what the next session needs.

---

## 0. Where the boundaries are

Four layers exist and work. The language is a fifth, on top:

```
   .fnet  ──netlist.py──>  source.json          hardware
   DSL    ──?──> IR8 ──schedule8──> ROM         programs      <— this file
                 factorio_memory_tb.py          verification
```

**The only new code is between the DSL and IR8.** `schedule8` already owns
every timing rule, `netlist.py` already owns the hardware, and the bench runner
already owns verification. Nothing below the language needs to change for the
language to exist — which is the reason it is worth doing now rather than after
the other open items.

**IR8 is the contract, and it was widened for this on 2026-07-28.**
`IR8.vec_move` / `vec_select` / `vec_read_lane` are the *only* places that know
a vector action is currently realized as writes to memory-mapped control rows.
The language emits `vec_move`; it never writes a control row. That matters
because the planned instruction-decoder rework (§7) changes the expansion and
nothing else — no program, no compiler pass, no bench.

---

## 1. What the machine actually is, from a compiler's point of view

Not a general register machine. Three facts shape every decision below:

**Operands live in fixed slots.** `A_*` ops read VREG0 × VREG1, `B_*` ops read
VREG2 × VREG3, and every `VS_*` op reads VREG0 only. You cannot ask for
`mul(r7, r9)`; you must *move operands into place*. This is the x86 `div`
situation (EDX:EAX), not RISC.

**Results are read by selection, not written to a destination.** An op output
is a mux source. `dst = a*b` is "put a and b in the pair, park the mux on the
product, commit to dst" — a load/compute/store shape with the compute part
free and always-on.

**Two selections sum.** `vec_move(dst, X, src2=Y)` commits `X + Y`. So vector
ADD costs zero instructions, and — with the `BCAST` generator — so does adding
a scalar constant to every lane. The pattern matcher must know this or it will
emit an op for something the wire does for free.

The consequence: **instruction selection and register allocation are the same
problem here**, and they are the whole compiler. Everything else is bookkeeping.

---

## 2. Surface syntax: a Python DSL first, a parser later (decided)

Build the middle-end against operator overloading, not a grammar:

```python
m = Machine(v10)
zx, zy, cx, cy = m.vec(), m.vec(), m.vec(), m.vec()

with m.loop(20):
    r2 = zx*zx + zy*zy
    esc = r2 > 4*S*S
    zx, zy = (zx*zx - zy*zy)//S + cx, (zx*zy)//(S//2) + cy
```

Reasons this is the right first move, not a shortcut:

- expression trees come for free, so **all the effort lands on selection and
  allocation**, which is where the risk is;
- it lives in the same files as the bench builders, so the acceptance test
  (§6) can be written before the compiler;
- `netlist.py`'s hand-rolled tokenizer is ~150 lines and can be reused later
  for a text front-end onto the *same* IR — the parser is the cheap part, and
  this project has already learned that lesson once.

A text syntax is a later, optional bolt-on. Nothing in §3–§5 depends on which
front-end produced the tree.

---

## 3. The type system (small on purpose)

| type | lives in | notes |
|---|---|---|
| `vec` | a vector register (block id 1..12) | 2451 lanes |
| `int` | a scalar memory row | the v8 machine |
| `mask` | a vector register | `vec` restricted to 0/1; distinct only so the compiler can pick mask-aware ops |

**Fixed point stays explicit.** `//S` is written by hand, as in the designer's
own example. A `fixed<S>` type that inserts the divide is a later convenience
and a source of surprises; integers with an explicit scale are honest and
match how the kernel was hand-written.

`vec op int` is a vec-scal op (broadcast). `vec op vec` is a pair op. `int op
int` is the scalar ALU. Mixed assignment is an error, not a coercion.

---

## 4. The compiler, pass by pass

### 4a. Lower to a DAG
Expression trees per statement, common subexpressions shared. `zx*zx` appearing
in both `r2` and the `zx'` update must be ONE node — the mandelbrot kernel's
entire efficiency comes from `B_SQ`, `B_SQG`, `B_SQDIFF` and `B_MUL` all being
live off one pair load, and a tree-walker would reload the pair four times.

### 4b. Instruction selection with placement constraints
Pattern-match DAG nodes to units. Each pattern names the unit, its mux index,
and **which register slots its operands must occupy**:

| pattern | unit | operand placement |
|---|---|---|
| `a*b` | B_MUL | a→VREG2, b→VREG3 |
| `a*a` | B_SQ | a→VREG2 |
| `a*a - b*b` | B_SQDIFF | a→VREG2, b→VREG3 |
| `a*a + b*b` | B_SQ ∥ B_SQG | a→VREG2, b→VREG3, dual select |
| `a - b` | A_SUB / B_SUB | pair A or B |
| `max(a,b)` / `min(a,b)` | A_MAX / A_MIN | pair A |
| `a op k` | VS_* | a→VREG0 |
| `a > k` / `a < k` | VS_GT / VS_LT | a→VREG0 |
| `a + b` | *(none)* | dual select — **free** |
| `a*k1 + k2` | VS_MUL ∥ BCAST | a→VREG0, dual select — **one move** |
| `k` (constant frame) | BCAST | — |

Bigger patterns must be tried first (`a*a - b*b` before `a*a`), which is
ordinary maximal-munch. The table is data, so adding a unit to the op farm is
adding a row.

### 4c. Register allocation
Two register classes with very different pressure:

- **VREG0..VREG3 are scratch**, clobbered constantly by operand placement.
  Never allocate a user variable there.
- **VREG4..VREG11 (8 slots) hold live variables.** Linear-scan over the
  statement order is sufficient at this size; spilling goes to a scalar row via
  `vec_read_lane`, which is catastrophically slow (one lane at a time), so
  **spilling should be a hard error in v1** with a message naming the variables
  that overflowed. Eight is enough for real kernels and the failure should be
  loud.

Note `VREG0`'s special role: it is the accumulator for every `VS_*` op, so a
chain like `(x*k1)*k2` reloads it each step. The allocator should recognise
`VREG0` already holding the needed value and skip the move — the single
highest-value peephole, worth ~2 moves per statement in the mandelbrot loop.

### 4d. Scheduling
Emit `vec_move` / `copy_a` / `write_imm` in order and hand the whole thing to
`schedule8`. **No new timing code.** `check_vector_hazards` and
`check_pulse_widths` already re-audit the result.

### 4e. Control flow
`for` and `while` compile to `label()` + body + `jump_if_zero`, exactly as the
mandelbrot loop does by hand today (counter through the ALU, not the accumulate
cell — see the ROM-shadowing note in MANDELBROT.md §4).

**`if` IS available, via a conditional RELATIVE jump** (designer, 2026-07-28 —
correcting an earlier claim here that forward jumps did not exist). Two facts
make it work, and neither needs new hardware:

- **pc_inject SUMS into the PC.** Anything routed to out4, *from any source*,
  becomes signal-P for one tick. A positive offset therefore jumps FORWARD, and
  that is already measured — `proc_v8_relative_jump` recorded "skip count ==
  offset exactly". `_op_jump_rel` emits a signed offset with no sign check; the
  "backward jumps only" error is `_label_slot` needing its target already
  scheduled, i.e. a **backpatching gap in IR8**, not an ISA limit.
- **A 0-or-offset value is a multiply.** Put a 0/1 flag in `mul_a`, the offset
  in `mul_b`, and route `mul_res` through port A to out4: `PC += flag*offset`.
  Flag 0 injects nothing and execution falls through; flag 1 skips the body.
  Selecting the CMP sense picks which way round.

So `if cond: BODY` is: compute the flag, scale it by `len(BODY)` plus the
shadow, `a->out4`, four shadow slots (they execute on BOTH paths, so they are
NOPs or code valid either way), then BODY. This is strictly better than the
write-path `jump_if_zero` the loops use — it does not occupy the write latch,
and V8.md estimates ~5 ticks off a loop tail.

**Both halves are now measured** (`v8_proc_computed_jump`, 2026-07-28), which
closes V8.md TODO #1 and with it the last gate on `if`:

1. `a->out4` works — the jump distance can be a memory row, so it can be a
   computed value, so it can be `flag * skip`.
2. **P = 0 is self-healing.** The inject fires with zero, the PC cell emits
   nothing, the autoincrement re-seeds, execution continues. This is the
   not-taken path — the one that runs most often — and it was previously only
   a suspicion.

`IR8.jump_rel_a(src)` exists and `jump_rel_a_window(op, distance)` reports
exactly which slots a taken jump skips, off the finished schedule.

What IR8 still owes the front-end is `jump_rel_if(flag_row, target)` — which
assembles the `flag * skip` multiply itself instead of taking a pre-scaled row
— **and forward labels**. The latter is a backpatch pass, and the pattern is
already proven: `computed_jump` patches its own distance into the ROM after
scheduling, because the distance depends on where the scheduler put things. The masks remain the better idiom for *lane-wise* choice (`esc = r2 >
k`, `e |= esc`); `if` is for whole-program control, where `vred_count` reducing
a mask to a scalar is what feeds the condition.

---

## 5. Build order — steps 0–6 DONE (2026-07-28)

Each step ends green on the suite; none of them can regress hardware, because
none of them touch it. That held: the hardware benches are byte-identical
before and after, and the only file below the language that changed at all is
`tools/build_v10_tests.py`, which now *imports* the mux index map from
`vlang.py` instead of keeping a second copy of it.

0. **ISA groundwork** — DONE, and it went further than planned.
   `vec_move`/`vec_select`/`vec_read_lane` are the vector isolation layer.
   `add_imm_a`, `add_ab`, `copy_a_indexed`, `write_indexed` give ALU-free
   arithmetic and ARRAYS. `jump_rel_a` gives computed and conditional jumps,
   with the P=0 fall-through measured.
2. **`Machine` / `Vec` value types** producing a hash-consed DAG — DONE.
   `zx*zx` written in two statements is ONE node
   (`test_common_subexpression_is_one_node`), and `dag_text()` renders a
   let-form that makes sharing visible so the tests can assert on it.
3. **Pattern table + maximal munch** — DONE. `Insn.triple()` is the abstract
   `(unit, operands, dest)` triple, and `test_mandelbrot_body_selects_the_
   expected_units` pins the whole loop body against hand-written expectations.
4. **Register allocator** — DONE. Fixed operand slots, the VREG0-reuse
   peephole, linear scan over VREG4..VREG11, spilling a hard error naming the
   variables. `test_mandelbrot_body_is_at_most_eleven_moves` asserts the
   acceptance figure; it comes out at exactly 11.
5. **Emit IR8 + schedule** — DONE, and the separate two-line smoke bench turned
   out to be unnecessary: mandelbrot itself was the first thing that ran, and
   the offline tests cover the small cases faster than a bench could.
6. **Recompile mandelbrot from the DSL** — DONE. `v10_proc_mandelbrot_dsl`,
   38/38, beside the hand-written bench in the suite.
7. **Loops** — DONE (`with m.loop(n)`, counter through the ALU). **The scalar
   half and the text front-end are not built** — see §8.

### What the compiler actually is

Four passes, ~700 lines, in `vlang.py`:

| pass | what it decides |
|---|---|
| `Machine._bin` / `_node` | the DAG, hash-consed, constants canonicalised |
| `_Selector` | units, broadcasts, and which slot each operand must occupy |
| `_assign_dests` | destination = the operand slot of the single consumer, when legal |
| `_emit_block` / `_colour` | operand moves, then linear scan over the eight variable registers |

`_audit()` then re-walks the finished move list and fails if any register is
overwritten between a value's definition and its last read — the same
belt-and-braces shape as `check_vector_hazards`, because that failure would
otherwise be silent in-game.

### Three things learned building it

**A value from an earlier block is OPAQUE to the pattern matcher.** This was
the one real bug, and it was silent-shaped: `cx = x*DX − OX` is an `add` of a
constant, so when `zx' = …/S + cx` was matched, the matcher happily munched
*into* `cx`, emitted `VS_ADD` with `cx`'s own addend as the broadcast, and then
asked for a register holding `x*DX` — which nothing had computed. Across a
block a value exists ONLY as a register; `_Selector._opaque` is the rule and
`test_earlier_block_values_are_opaque` is the guard.

**A destination may not be an operand of its own unit.** `vec_move` erases the
destination and only then pulses W, so `V0 <- VS_DIV` reads an already-emptied
VREG0 and commits zero. This is exactly why the hand-written grid seed needs
its temporary, and the allocator now derives that rather than being told.

**Scratch state must not cross the back edge.** At the top of the loop body
VREG2 physically holds the *previous* pass's `zx`, so a state tracker that
carried across the boundary would skip the pair load and every pass but the
first would read stale lanes. State is cleared per block; the pair load is
re-emitted every pass, which is what the hand-written kernel does too.

Two smaller ones worth keeping: `x = x + y` cannot be a dual selection (the
erase would clear x first) and compiles to an **accumulate** — which is how the
per-lane counter works with no adder; and every constant is range-checked at
**DAG-build time**, not at selection, because a 20-bit overflow is only useful
as an error if it points at the line that wrote the number.

---

## 6. Acceptance test — PASSED

**Recompile the mandelbrot kernel from the DSL and get the same 38
expectations**: 1749 escaped lanes, escape-iteration sum 26963, the same
32-lane scanline, the same palette value.

Done, and green: `tools/build_v10_tests.py:mandelbrot_dsl` builds
`testbenches/v10_proc_mandelbrot_dsl.tb.json`, and both benches take their
expectations from **one** function (`mandelbrot_expectations`) off the same
reference model — so a divergence between what the designer wrote by hand and
what the compiler emits is a failing bench, not a number nobody looks at.
`test_dsl_bench_matches_the_hand_written_expectations` asserts the two dicts
are equal offline as well, which catches it in a second rather than in 14.

Secondary target met: **11 moves per pass**, equal to the hand-written figure,
and 8 for the grid seed. Register pressure came out one lower (6 against 7).

---

## 6a. Capabilities the interconnect already has (measured 2026-07-28)

**The philosophy is programmable interconnect, not instructions** — so before
adding hardware, ask what the crossbar can already be talked into. It is a full
3x4 (three sources x four dests) and HALF of it had never been driven.
`v8_decoder_matrix` now covers all twelve routes plus multi-source summation,
24/24. What that unlocks, none of it needing a new combinator:

| capability | route | status |
|---|---|---|
| indirect store (`*p = x`) | `a->out1` | matrix-tested |
| indirect load (`x = *p`) | `b->out3` | matrix-tested |
| **indexed load** (`arr[i]`) | `const->out3 + a->out3` | sums, 1000+7=1007 |
| **indexed store** (`arr[i] = x`) | `const->out1 + a->out1` | sums |
| **add-immediate in flight** (`dst = src + k`) | `const->out2 + a->out2` | sums, 7+5=12 |
| **mem+mem add, no ALU** (`dst = m1 + m2`) | `a->out2 + b->out2` | sums |
| conditional / computed jump | `a->out4` | matrix-tested |
| computed port-B address (pointer) | `a->out2` into `b_reg` | already possible |

Three consequences worth stating plainly:

**Arrays are free.** Indexed load and store are the difference between named
variables and data structures, and both are one route-bit pair. The language
should have subscripting in v1.

**The ALU is optional for `+`.** A bus sum costs one slot; an ALU round trip
costs `ALU_TO_USE_BASE + latency` = 6. Loop counters, pointer bumps and
accumulator updates should all compile to summation, not to `add_res`.

**Predication needs no branch at all.** `write_addr = trash + flag*(real-trash)`
sends a write to a scratch row or the real one depending on a flag — a
conditional assignment with *zero* jump shadow, which is often better than the
`if` of §4e. The compiler should prefer it for single-assignment conditionals
and reserve the jump for blocks.

Also unlocked: **clearing an accumulate cell**, which the ISA notes list as
impossible (the reset pulse is blocked). Route a computed `-current` to out2
targeting that row and the accumulate adds it to zero.

**Proven end to end** by `v8_proc_bus_summation`, and IR8 exposes all four:
`add_imm_a`, `add_ab`, `copy_a_indexed`, `write_indexed`. The convention that
makes it work is the designer's: a matrix dest may hold one value SPLIT ACROSS
SIGNALS, the value is their sum, and **sanitising happens at the output, not
the input** — every consumer already ends in `each+0 -> carrier`.

So the front-end can emit subscripting and ALU-free arithmetic from day one.
Two notes for whoever writes the selection pass:

- **The index streams through port B**, so an indexed access needs `park_b` on
  the index row, and consecutive accesses at the same index share that park.
  Batch them: re-parking costs `PARK_B_VALUE_TO_USE` = 7.
- **Writing the index row is an anti-dependency on every indexed access already
  emitted** — the scheduler now enforces this (`_stream_consumers`), and it is
  a real hazard, not a theoretical one: it silently wrote `arr[0]` instead of
  `arr[3]` the first time this bench ran.

## 7. What this is NOT waiting for

Recorded so nobody re-opens them:

**The vector-zone decoder** (memory-mapped control → direct drive) is worth
~5× on vector code and is deliberately AFTER the language, because `vec_move`
already hides it. Note the philosophy-consistent shape when it comes: not an
instruction set for the vector zone, but **more interconnect** — out5/out6/out7
matrix dests driving `vec_rsel` / `vec_wsel` / `vec_erase` directly, one more
column of gates per dest. That collapses a move from three write-path
traversals to one or two slots, is purely additive (the memory-mapped path
keeps working), and adds no opcodes.

**Delay-slot filling** — the jump shadow costs ~15–20%. Accepted for now.

**A tick simulator** — explicitly not wanted. The bench scaffolding is the
answer; make it faster or more thorough in-game if it becomes the bottleneck.

**External I/O** is the one genuinely missing capability for the stated goal.
Radar gives a **zero-tick wireless link**, so the intended shape is: memory-map
external devices onto the same bus, with a select line each device compares
against its own id — the identical pattern the vector zone's `vec_wsel`/
`vec_erase` block ids already use. More commands are then rows, not rework.
That work is independent of the language and can land in parallel.

---

## 8. What the language does NOT have yet (2026-07-28)

Written down deliberately, because the compiler is small enough that each of
these is an evening rather than a project, and because §5 step 7's "the scalar
half" was one line hiding four things.

**One loop per program, and no `if`.** `Machine.loop` refuses a second loop
rather than silently mis-scheduling one, and there is no conditional at all.
Both are gated on the same missing piece and it is *not* hardware: IR8 owes the
front-end **`jump_rel_if(flag_row, target)` and FORWARD LABELS**. §4e records
that the ISA half is measured (`v8_proc_computed_jump`: `a->out4` works, and
P = 0 falls through self-healingly); what is missing is a backpatch pass, and
the pattern is already proven — `computed_jump` patches its own distance into
the ROM after scheduling. This is the highest-value next item.

**No scalar (`int`) values.** §3's type table has three rows and only `vec` is
implemented. Scalar rows are written directly through IR8 by the bench
builders, and the loop counter is emitted by `Machine.loop` itself. Adding
`int` means a second, much easier allocator over memory rows, and it is what
`vred_count`/`vred_sum` results want to flow into.

**No subscripting**, though §6a says the hardware has had it since the
interconnect matrix was fully driven — `copy_a_indexed` / `write_indexed` are
sitting in IR8 unused by any front-end.

**No predication.** §6a's `write_addr = trash + flag*(real-trash)` is a
conditional assignment with zero jump shadow, and the compiler should prefer it
over a branch for single-assignment conditionals. Nothing emits it yet.

**Spilling is a hard error**, by design (§4c), and eight variables has been
enough so far — mandelbrot fits in six. When it stops being enough the answer
is more registers (three combinators each, and indices 13..16 are already
reserved), not a spill path through `vec_read_lane`.

**No text front-end**, which remains the cheap part and is still optional:
nothing in §3–§5 depends on which front-end produced the tree.
