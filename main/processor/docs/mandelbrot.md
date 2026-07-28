# Mandelbrot on the v10 vector processor — DONE (2026-07-27)

The kernel is built, live, and in the suite (**34/34** as of 2026-07-28)
(`testbenches/v10_proc_mandelbrot.tb.json`, built by
`tools/build_v10_tests.py:mandelbrot`). This file is now both the record of
what it is and the handoff for whatever comes next.

**It has a compiled twin.** `v10_proc_mandelbrot_dsl` is the same kernel
recompiled from the vlang DSL (LANGUAGE.md), carrying the identical 38
expectations off the same reference model, and matching this hand-written
version move for move — 8 in the seed, 11 per pass. The two run side by side,
so any divergence between hand and compiler is a failing bench. If you change
the kernel here, change it there: `mandelbrot_expectations()` is shared, so the
expectations follow automatically, but the two programs are written twice on
purpose.

```
.\.venv\Scripts\python.exe run_all.py --start-server --regen     # 34 benches, ~2 min
.\.venv\Scripts\python.exe test_vlang.py                         # 23 compiler tests
.\.venv\Scripts\python.exe test_machine_v8.py                    # 15 scheduler tests
```

---

## 1. What runs

The mandelbrot set over the **whole vector** — 2451 lanes, 64 columns × 38
rows, 20 iterations of `z = z² + c` in 360ths fixed point — computed by a
scalar program that contains **no vector instruction at all**. Every line of
it is a `write_imm` or `pulse_imm` to a memory row; `machine_v8` schedules the
lot with no idea it is driving a vector unit.

539 ROM words, ~5200 ticks, **14s wall** including the paste and the
lane-by-lane readback. The finished frame is also a picture: see DISPLAY.md
for the lamp matrix that shows all 2451 lanes at once, and the pasteable
blueprints of machine, display, and the two wired together.

Checked against a Python model lane by lane, all 38 expectations exact:

| readout | value |
|---|---|
| `vred_sum` of the sticky escape mask | 1749 of 2451 lanes escape |
| `vred_sum` of the escape-iteration counter | 26963 |
| 32-lane scanline through image row 19 | exact, lane by lane |
| `cx`, `cy` at a probe lane | exact |
| the display palette at a probe lane | exact |

The scanline, read out of the machine one lane at a time (`#` = still inside
after 20 iterations, `@` = escaped immediately):

```
@%*:+++=######################=*
```

and the full frame the same program holds, from the reference model:

```
 @@@@@@@@@@@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%@@@@@@@@@@@@
@@@@@@@@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%@@@@@@@@
@@@@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%+*%%%%%%%%%%%%@@@@
@@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**-***%%%%%%%%%%%@@
@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**+.+-+*%%%%%%%%%%%
@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**++=#+**%%%%%%%%%%%
@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%*+-=-##-+-*%%%%%%%%%%
@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%****+-######=**%%%%%%%%%
@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%*******++:######=+*****%%%%%
@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%*+--#++=.=--#####:-=+#+***+.*%
@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%**+:###-.##############-+.#=-#%
@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%****++########################+*%
@@@%%%%%%%%%%%%%%%%%%%%%%%%%%****+---#######################-+**
@@%%%%%%%%%%%%%%%%*+************++-##########################+++
@@%%%%%%%%%%%%%%%*+#+++++:++**+++##############################-
@%%%%%%%%%%%%%%%***+=##--##--+++=#############################=+
@%%%%%%%%%%%%%%%***+=#########.--##############################+
@%%%%%%%%%%%%%*++++-##########################################=*
@%%%%%%%******++---.##########################################**
@%%***:*+++++-=:############################################=+**
@%%***-++++++==:############################################++**
@%%%%%%%******++#-############################################**
@%%%%%%%%%%%%**++++-##########################################:*
@%%%%%%%%%%%%%%%***+=##########--##############################+
@%%%%%%%%%%%%%%%***+=:#::##:.=++=#############################=*
@@%%%%%%%%%%%%%%%*+-+++++-+++++++###############################
@@%%%%%%%%%%%%%%%**=************++-##########################=+#
@@@%%%%%%%%%%%%%%%%%%%%%%%%%%****+.::#######################=+**
@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%***++=########################+*%
@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%***+=###-###############-=-:-:=%
@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%*+##:++=#-#:#####:-#=#++**+-*%
@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%*******++#######+++*****%%%%
@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%*****+-######=***%%%%%%%%
@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**#--:##-=-*%%%%%%%%%%
@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**++-#++*%%%%%%%%%%%
@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**+#=#+*%%%%%%%%%%%
@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%**#+***%%%%%%%%%%%@
@@@@@@@@@@@@@@@@@@%%%%%%%%%%%%%%%%%%%%%%%%%%%%-*%%%%%%%%%%%%@@@@
@@@@@@@@@@@@@@@@@@@@
```

Against a float model over the same grid, 15 of 2451 lanes disagree on whether
they escape and 101 on the exact iteration — 0.6% and 4%, all on the boundary,
which is what 360ths and 20 iterations buys.

---

## 2. What was added to the machine to get there

Three hardware changes, all small, all live-verified by their component
benches before the kernel ran.

**Twelve registers, not eight** (`modules/v10_vec_bank.fnet`, bench 34/34).
The kernel needs eleven live at once. Registers now own mux indices **1..16**
— four more than are built — so the next widening renumbers nothing. Op
outputs start at 17.

**The square block** (`modules/v10_op_farm.fnet`, bench 24/24). The bank now
exports BOTH faces of each pair-B register (`a2g` = VREG2 on green, `b2r` =
VREG3 on red), so a register can be squared with the ordinary `each[red] ×
each[green]` shape — no each-against-itself semantics that nothing here has
measured. Three new units:

| # | source | |
|---|---|---|
| 24 | B_SQ | VREG2² |
| 25 | B_SQG | VREG3² |
| 26 | B_SQDIFF | VREG2² − VREG3² |

which is what makes the whole iteration cost **one pair load**. With `zx` in
VREG2 and `zy` in VREG3, all of `zx·zy` (B_MUL), `zx²−zy²` (B_SQDIFF) and —
because two mux selections sum — `zx²+zy²` (B_SQ with B_SQG) are on tap at
once. The `(zx+zy)(zx−zy)` identity is not needed and neither is pair A.

**A min and a max in the reach table** (`machine_v8._VEC_REACH_BY_NAME`).
B_SQDIFF is two combinators deep, so `vec_wsel` now reaches the write bus in
2 stages through a plain register select and in 4 through the square block.
One number could not serve both scheduler rules, which pull opposite ways:

- forward, "has my control arrived?" → **latest**, wait out the slowest path;
- backward, "could a later write already be visible at an earlier read?" →
  **soonest**, contamination needs only one short path.

Entries are `(soonest, latest)` now. Add hardware, add its depth, everything
downstream follows.

---

## 3. The kernel, in the four idioms

Full source: `tools/build_v10_tests.py:mandelbrot`. The DSL source that
compiles to the same thing is `mandelbrot_dsl` in the same file — four
statements in the loop body, and the compiler derives every move below. Per
pass, eleven moves:

```python
_move(V2, ZX); _move(V3, ZY)                       # ONE pair load
_move(V0, B_SQ, s=B_SQG)                           # r2 = zx^2+zy^2, free ADD
_move(V1, VS_GT, bcast=4*S*S)                      # escaped THIS pass
_move(V0, E); _move(E, A_MAX)                      # sticky OR
_move(ESUM, E, accumulate=True)                    # ESUM += E, no adder
_move(V0, B_SQDIFF)
_move(ZX, VS_DIV, s=CX, bcast=S)                   # zx' = (zx^2-zy^2)/S + cx
_move(V0, B_MUL)
_move(ZY, VS_DIV, s=CY, bcast=S//2)                # zy' = 2*zx*zy/S + cy
```

The doubling in `2·zx·zy` is folded into the divisor rather than costing a
multiply. The escape test runs on the CURRENT z, before the update, so the
count is the true escape iteration.

**Seeding costs no scalar writes.** COORD gives every lane its own index and
two vec-scal ops turn it into `x = i % 64`, `y = i / 64`.

**The counter needs no adder.** `ESUM += E` is a write with no erase.

**Stickiness is what makes the answer defined.** Escaped lanes keep iterating
and their z overflows within a couple of passes — but the mask is already 1
and `max(1, x) = 1`, so nothing they do afterwards can reach either output.
The reference model can therefore stop iterating a lane the moment it escapes,
and never has to know what Factorio does on 32-bit overflow. Nothing that IS
asserted ever exceeds ~7.5e7.

---

## 4. What bit, and what it cost

**Zero and absent are the same thing** — this one cost a run. `x = i % 64` is
0 for the whole first column and `y = i / 64` is 0 for the whole first row,
and a lane computing to 0 VANISHES from the wire. `each` against a fixed
signal iterates only the first operand's lanes, so the following `VS_SUB`
could not put those lanes back: their `cx`/`cy` came out 0 instead of
−OX/−OY. First run scored 34 of 37, and the three misses were exactly those
lanes. The fix is to apply the offset as an **addend, not a subtrahend**: a
register holding −OX in every lane, summed onto the bus by the second
selector, so a missing product still lands on a present lane. `VS_GT` against
0 builds the all-ones frame in one move, since COORD is 1..N and every lane of
it is positive.

**The immediate field sets the fixed-point scale.** An instruction immediate
is 20 bits SIGNED, and the escape test compares `r2` (at scale S²) against
`4·S²` on `vec_bcast` — so `4S² ≤ 524287` and `S ≤ 362`. 360 is the largest
round value that also leaves `S/2` an integer, which the zy update needs.
Wanting S = 1024 costs two extra moves per pass to scale `r2` down first.

**The ROM shadows low memory rows.** The ROM constant drives the memory bus
and its rows ARE the slot addresses it occupies, so a 523-slot program puts
junk on rows 1..523. Harmless for a row the program only WRITES (the cell
holds its own value on the state loop, which is what the probe samples), fatal
for one it PORT-READS, because port A reads the bus. Hence: scratch rows above
the PC at 2200+, and a loop counter through the ALU (`add_res = add_a + 1`)
rather than the accumulate cell at row 300, which the ROM shadows.

**Deselect lags** — the bank bench caught a new step of mine committing the
PREVIOUS selection because clearing R and pulsing W landed on the same tick.
Same rule as 2026-07-27; it is still true and still easy to forget.

**Never probe `vbus` or `membus`.** Unchanged and still measured: `vbus`
carries 2451 lanes, `membus` carries one signal per ROM word, and RCON
responses corrupt past ~4096 bytes. Probe the cell's red state loop (entity
179 in the composed machine) and read vector state through the out-ports.

---

## 5. The machine as it now stands

**Mux sources** (`vec_rsel` / `vec_ssel`), registers 1..16 reserved:

| # | source | # | source |
|---|---|---|---|
| 1–12 | VREG0…VREG11 | 24 | B_SQ — VREG2² |
| 13–16 | *(reserved for registers)* | 25 | B_SQG — VREG3² |
| 17 | A_MUL — VREG0 × VREG1 | 26 | B_SQDIFF — VREG2² − VREG3² |
| 18 | A_SUB | 27 | VS_MUL — VREG0 × `vec_bcast` |
| 19 | A_MAX — elementwise max | 28 | VS_SUB |
| 20 | A_DIV | 29 | VS_DIV |
| 21 | A_MOD | 30 | VS_MOD |
| 22 | B_MUL — VREG2 × VREG3 | 31 | VS_GT — 1 where VREG0 > `vec_bcast` |
| 23 | B_SUB | 32 | COORD — every lane holds its own index |
| | | 33 | LANE_IN — the one-lane scalar write |

Op operands stay hardwired (pair A = VREG0×VREG1, pair B = VREG2×VREG3) and
never go through the mux, which is what makes two simultaneous selections a
feature rather than an ambiguity. Control rows are unchanged:
`modules/v10_addr_map.fnet`, region `vec = 2130`.

`modules/v10_processor.fnet` is now **203 entities** (`--top v10_processor`),
in a compact block since combinators were rotated to stand up (15 one-tile
columns per row, two tiles tall, with column 7 reserved for repeaters).

---

## 6. Since built

`ONES` (45) and `BCAST` (46) are hardware now — a frame present in every
lane, and the same frame scaled by a second broadcast carrier. They exist
because of §4's absent-vs-zero trap: `frame*k1 + k2` is one move with lane
resurrection built in, so the grid seed went from 13 moves and a register
permanently holding ones to 6 moves and none. `vred_count` answers "how
many lanes satisfy X" in one read. `A_MIN` and `VS_LT` complete the
comparison pairs. The kernel is 477 ROM words again, down from 539.

## 7. Not built

Moved to `plan/ROADMAP.md` §4 (vector hardware) and §3 (the decoder).
