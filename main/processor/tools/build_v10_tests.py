#!/usr/bin/env python3
"""End-to-end program benches for the composed v10 machine
(modules/v10_processor.source.json).

The point of these is that a vector operation is NOT a new instruction. The
vector zone's control plane is chunk1's memory bus, so every vector action is
an ordinary `write_imm` (or `pulse_imm`) to a memory row, and results come
back through `copy_a` — the same latched port A the scalar core already had —
because the out-ports deposit their results as ordinary scalar rows.

processor.isa now knows the vector zone's timing (`MachineConfig.vec_reach`), so
programs here contain NO hand-inserted settle stages: the scheduler spaces
vector reads behind vector control writes by itself. `check_vector_hazards`
below re-verifies the finished schedule against the same table — belt and
braces, not a second source of truth.

Every machine fact comes from the compiled artifact: the MachineConfig from
the source's signals map, vector row addresses from the same map, ROM row
signals from the design's own exclusion-compacted address space, and bench
expectations as symbolic "$name" / "$mem[N]" references.

Entity numbers: ROM=66 and the driver trio 46/47/48 are stable because the
scalar core is declared first, so v8's numbering is preserved. PROBE numbers
are NOT stable — they are appended last, so any component added to the vector
zone shifts them. They are read from the compiled `.debug.json` sidecar
instead of being written down here, because writing them down cost a suite
run: entity 179 stopped being the state loop and started being a combinator
carrying the whole memory bus, and the bench did not fail, it TIMED OUT on a
corrupted RCON response.
"""
import json
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAIN))
ROOT = MAIN / "processor"

from processor.assembler import assemble_program  # noqa: E402
from bench.processor_tb import design_address_space  # noqa: E402
from processor.isa import (IR8, WRITE_TO_CELL, check_pulse_widths,  # noqa: E402
                        config_from_signals_map, schedule8)
# The mux index map lives in vlang.py — one table, shared by the hand-written
# programs here and by the compiler's pattern table (v10_op_farm.fnet is where
# the numbers come from).
from processor.lang import (A_MAX, A_MIN, A_MUL, A_SUB, B_MUL, B_SQ,  # noqa: E402,F401
                   B_SQDIFF, B_SQG, B_SUB, BCAST, COORD, LANE_IN, ONES,
                   SRC_VREG, VS_DIV, VS_GT, VS_LT, VS_MOD, VS_MUL, VS_SUB,
                   Machine, vmax)

ROM_ENTITY = 66

# Scratch rows the programs copy vector results into. Clear of every region:
# acc=300/301, pc=2096, alu=2110..2127, vec=2130..2148.
#
# ROM ROW COLLISION, the constraint that decides where these can live. The ROM
# constant drives the memory bus and its rows are the SLOT ADDRESSES it
# occupies, so a program spanning N slots puts junk on rows 1..N. That is
# harmless for a row the program only WRITES (a memory cell holds its own
# value on the state loop, which is what the probe samples) but fatal for one
# it PORT-READS, since port A reads the bus. The mandelbrot program is long
# enough to matter, so its scratch rows sit above the PC (2096) — past any
# address a PC can ever reach — and its loop counter runs through the ALU
# rather than the accumulate cell at 300, which a long ROM would shadow.
DST_COORD_LANE_7 = 500
DST_COORD_LANE_250 = 501
DST_LANE_IN_READBACK = 502
DST_LANE_IN_SUM = 503
DST_COORD_SUM = 504
DST_VREG0_SUM = 505
DST_VREG0_LANE_7 = 506
DST_MOD_LANE_7 = 507
DST_MOD_LANE_8 = 508

def check_vector_hazards(ir, cfg):
    """Fail the build if a vector read lands inside a control transition.

    The zone is a fixed-delay chain: a port at tick S reflects its controls as
    of tick S - stages, so a control settling at T (its value slot +
    WRITE_TO_CELL) becomes visible at the port from T + stages onward. Two
    things must hold for every read:

      forward  — the control the read WANTS must already be visible;
      backward — no control written LATER in the program may be visible yet.

    The scheduler enforces both while placing ops (`_Sched8._vec_read_lo` and
    `_vec_write_disturbs_read`). This walks the finished schedule as an
    independent audit, off the SAME table on the MachineConfig — one source of
    truth, checked twice.
    """
    settles = []          # (order, settle_slot, control addr)
    reads = []            # (order, read_slot, out-port addr)
    for order, op in enumerate(ir.ops):
        if op.kind in ("write_imm", "pulse_imm") and op.dst in cfg.vec_reach:
            settles.append((order, op.value_slot + WRITE_TO_CELL, op.dst))
            if op.kind == "pulse_imm":
                settles.append((order, op.clear_value_slot + WRITE_TO_CELL, op.dst))
        elif op.kind == "copy_a" and op.src in cfg.vec_ports:
            reads.append((order, op.slot, op.src))

    problems = []
    for read_order, read_slot, port in reads:
        for write_order, settle_slot, control in settles:
            stages = cfg.vec_stages(control, port)
            if stages is None or write_order < read_order:
                continue
            if read_slot >= settle_slot + stages:
                problems.append(
                    f"slot {read_slot}: read of {cfg.namer(port)} would see "
                    f"{cfg.namer(control)}, which the program writes LATER "
                    f"(settles {settle_slot}, visible from "
                    f"{settle_slot + stages})"
                )
    if problems:
        raise SystemExit("vector hazard check failed:\n  " + "\n  ".join(problems))


TB_COMMON = {
    "pre_stimulus_dead_ticks": 0,
    "settle_ticks": 1,
    "poll_seconds": 0.002,
    "tick_wait_timeout_seconds": 3.0,
    "trace_ticks": 3,
    "rebuild_before_run": True,
    "paste_origin_x": 0.0,
    "paste_origin_y": 0.0,
    "fixture_blueprints": [
        {
            "name": "power_seed",
            "origin_x": 0.0,
            "origin_y": -24.0,
            "blueprint_string": "0eNqFz8EKgzAMBuB3ybnCdNVpX2UM0S5KQKO0dcxJ333RwXZcLyEh+X66QTssODviAGYDshN7MNcNPPXcDPuMmxHBAA5ogyObIKPr10Qu0HWNRYgKiO/4BJPGmwLkQIHwwxzNWvMytuhkQf3nFMyTF2HiPV3URCtYpRQS1C5dh6729BIkPX3fHkwBR5F/H1LwQOcPKC+yKteZLqtLqc95jG8gJlKg"
        }
    ],
    "restore_pause_state": True,
    "entity_map": {
        "drivers": {"trig_set": 46},   # inert: the runner requires >=1 driver
        "expected_probe_name": "mem_state",
        "output_probes": [
            # ONLY the cell's red state loop is probed, and that is a real
            # constraint rather than minimalism. Two nets in this machine are
            # far too wide to sample:
            #   vbus   — selecting COORD puts ~2451 lanes on it.
            #   membus — the ROM constant drives it, so it carries ONE SIGNAL
            #            PER INSTRUCTION plus every ALU and out-port row. At 45
            #            ROM words that sampled fine; at 99 it pushed each
            #            per-tick response past the ~4096-byte limit where RCON
            #            responses corrupt, and the run timed out (measured
            #            2026-07-27).
            # The state loop carries only what the program actually wrote, so
            # it stays small however long the program gets.
            # entity number resolved at build time — see probe_entity()
            {"name": "mem_state", "entity_number": None, "wire": "red"},
        ]
    },
}


def probe_entity(net_name: str) -> int:
    """Entity number of a debug probe, from the compiler's own sidecar."""
    debug = json.loads((ROOT / "modules" / "v10_processor.debug.json")
                       .read_text(encoding="utf-8"))
    for probe in debug["probes"]:
        if probe["net"] == net_name:
            return int(probe["entity_number"])
    raise SystemExit(f"no debug probe on net {net_name!r}; "
                     f"have {[p['net'] for p in debug['probes']]}")


def rom_filters_for_design(instructions, space):
    """ROM filter rows via the DESIGN's address space (slot addr -> its row
    signal under this design's exclusion set)."""
    filters = []
    for index, (addr, word) in enumerate(instructions, start=1):
        row = space[addr]
        filters.append({
            "index": index,
            "type": row["type"],
            "name": row["name"],
            "quality": row["quality"],
            "comparator": "=",
            "count": word,
        })
    return filters


def vector_roundtrip(cfg, rows, lane_count):
    """Scalar program drives the vector zone end to end and reads it back.

    Four things are proven, none of which needed a new instruction:

    1. COORD is a real 'nothing -> vec' source. Park R on it, name a lane,
       and `copy_a` the extracted value into a scalar row. Two widely
       separated lanes (7 and 250) rule out a constant.
    2. The whole-frame reduction actually spans the vector. With COORD
       selected, `vred_sum` is the sum of every lane index, i.e. N(N+1)/2 over
       the design's own address space — a number only reachable by summing
       ~2478 lanes in one tick.
    3. LANE_IN is a working data-in port: the program writes an address and a
       value, selects the resulting one-lane frame, and reads the same value
       back out through LANE_OUT.
    4. The reduction of that one-lane frame is the value itself, confirming
       LANE_IN produces exactly one lane and not a smear.

    Then the register path, which is the interesting half:

    5. A register MOVE works without W and erase ever changing atomically.
       Pulse erase (register cleared and holding empty), then pulse W for
       exactly one tick — into an empty register a single accumulate pulse IS
       a replace. Both pulses come free from the pipeline: the write path
       pairs a write_imm's value two slots after its address and lands it five
       later, so two back-to-back writes to the same row land on consecutive
       ticks (designer, 2026-07-27).
    6. VREG0 really holds the whole ramp: selecting it as a mux source and
       reducing gives the same N(N+1)/2, and lane 7 still reads 7.
    7. The op farm computes on it. `VS_MOD` = VREG0 % 3 reads back 1 at lane 7
       and 2 at lane 8 — real per-lane arithmetic over ~2451 lanes, which is
       the mandelbrot grid step (`x = i % width`).
    """
    coord_sum = lane_count * (lane_count + 1) // 2

    ir = IR8(cfg)
    # -- COORD: park the mux on the lane-index ramp, extract lane 7 ---------
    ir.vec_select(COORD)
    ir.write_imm(rows["vec_rlane_addr"], 7)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_COORD_LANE_7)
    ir.barrier()
    # -- the whole-frame reduction of that same ramp -----------------------
    ir.copy_a(rows["vred_sum"], DST_COORD_SUM)
    ir.barrier()
    # -- a second, far-apart lane of the ramp: a stale read would return 7 --
    ir.write_imm(rows["vec_rlane_addr"], 250)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_COORD_LANE_250)
    ir.barrier()
    # -- LANE_IN: the program writes a vector lane, then reads it back -----
    ir.vec_select(LANE_IN)
    ir.write_imm(rows["vec_wlane_addr"], 60)
    ir.write_imm(rows["vec_wlane_value"], 4242)
    ir.write_imm(rows["vec_rlane_addr"], 60)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_LANE_IN_READBACK)
    ir.barrier()
    ir.copy_a(rows["vred_sum"], DST_LANE_IN_SUM)
    ir.barrier()

    # -- MOVE the COORD ramp into VREG0 ------------------------------------
    # Re-park the mux on COORD (LANE_IN was selected above), let the bus
    # settle, then erase-pulse and write-pulse the register.
    ir.vec_select(COORD)
    ir.barrier()
    ir.vec_move(SRC_VREG(0), COORD)

    # -- prove VREG0 holds the whole ramp, by reading it back as a source --
    ir.vec_select(SRC_VREG(0))
    ir.write_imm(rows["vec_rlane_addr"], 7)
    ir.barrier()
    ir.copy_a(rows["vred_sum"], DST_VREG0_SUM)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_VREG0_LANE_7)
    ir.barrier()

    # -- the op farm computes on it: VS_MOD = VREG0 % 3 --------------------
    ir.vec_select(VS_MOD, bcast=3)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_MOD_LANE_7)
    ir.barrier()
    ir.write_imm(rows["vec_rlane_addr"], 8)
    ir.barrier()
    ir.copy_a(rows["vec_rlane_data"], DST_MOD_LANE_8)
    ir.barrier()
    ir.halt()

    prog, _ = schedule8(ir, name="v10_vector_roundtrip", verbose=True)
    check_vector_hazards(ir, cfg)
    check_pulse_widths(ir)
    timeline = [
        {
            "name": "run_to_halt",
            "skip_ticks": 240,
            "settle_ticks": 1,
            "trace_ticks": 3,
            "expect_probe": "mem_state",
            "expect": {
                f"$mem[{DST_COORD_LANE_7}]": 7,
                f"$mem[{DST_COORD_LANE_250}]": 250,
                f"$mem[{DST_LANE_IN_READBACK}]": 4242,
                f"$mem[{DST_LANE_IN_SUM}]": 4242,
                f"$mem[{DST_COORD_SUM}]": coord_sum,
                # the moved register holds the identical frame
                f"$mem[{DST_VREG0_SUM}]": coord_sum,
                f"$mem[{DST_VREG0_LANE_7}]": 7,
                # VS_MOD = VREG0 % 3, per lane
                f"$mem[{DST_MOD_LANE_7}]": 1,
                f"$mem[{DST_MOD_LANE_8}]": 2,
                "$poison_ctr": 0,
            },
        },
    ]
    description = (
        "End-to-end v10 program: the scalar core drives the vector zone with "
        "nothing but write_imm to memory rows, and reads results back with "
        "copy_a. Parks the mux on COORD and extracts lanes 7 and 250 (both "
        "return their own index); reduces the whole ramp to "
        f"{coord_sum} = N(N+1)/2 over {lane_count} lanes, a number only "
        "reachable by summing the entire vector in one tick; then writes lane "
        "60 = 4242 through LANE_IN and reads it back, with the frame "
        "reduction confirming exactly one lane. Then it MOVES the ramp into "
        "VREG0 - erase pulse, then a one-tick W pulse, both free from the "
        "write path's pipelining - and proves the register holds the whole "
        f"frame by re-reading it as a mux source ({coord_sum} again, lane 7 "
        "still 7). Finally the op farm computes VS_MOD = VREG0 % 3 over all "
        f"{lane_count} lanes, reading back 1 at lane 7 and 2 at lane 8 - the "
        "mandelbrot grid step. No vector-specific instruction exists; "
        "processor.isa scheduled all of it with no vector awareness at all."
    )
    return prog, timeline, description


# ---------------------------------------------------------------------------
# Mandelbrot
# ---------------------------------------------------------------------------
#
# Fixed point at scale S: a real value v is carried as round-to-zero(v * S).
# The grid is the lane index itself — COORD hands every lane its own address,
# and the vec-scal MOD/DIV units turn that into (x, y) with no scalar writes.
#
# Iteration, with zx/zy the state and cx/cy the per-lane constant:
#
#     r2  = zx^2 + zy^2                escape test, BEFORE the update
#     zx' = (zx^2 - zy^2)/S + cx
#     zy' = (zx*zy)/(S/2) + cy         (the doubling folded into the divisor)
#
# One pair-B load per pass (VREG2 = zx, VREG3 = zy) makes all three available
# at once: B_SQ + B_SQG summed on the bus is r2, B_SQDIFF is zx^2-zy^2, B_MUL
# is zx*zy. That is why the square block exists.
# THE SCALE IS SET BY THE IMMEDIATE FIELD, not by precision appetite. The
# escape test compares r2 (which lives at scale S^2) against 4*S^2 on the
# vec_bcast row, and an instruction's immediate is 20 bits SIGNED — so
# 4*S^2 <= 524287 and S <= 362. 360 is the largest round value that also
# leaves S/2 an integer, which the zy update needs (the doubling in
# 2*zx*zy is folded into the divisor rather than costing a multiply).
# Measured against a float model over all 2451 lanes: 15 lanes disagree on
# whether they escape and 101 on the exact iteration, i.e. 0.6% / 4%.
MB_SCALE = 360                  # fixed-point scale S
MB_WIDTH = 64                   # lanes per image row
MB_DX, MB_OX = 14, 720          # cx = x*DX - OX   ->  [-2.000, +0.450]
MB_DY, MB_OY = 23, 450          # cy = y*DY - OY   ->  [-1.250, +1.178]
MB_ITERS = 20
MB_R2MAX = 4 * MB_SCALE * MB_SCALE      # escape when |z|^2 > 4
MB_SCAN_ROW = 19                # image row read back lane by lane
MB_SCAN_STEP = 2                # ...every other column, to halve the readback

# THE PALETTE, for the lamp-matrix display (tools/build_display.py). A lamp in
# packed-RGB mode reads one signal and colours itself by its value as 0xRRGGBB,
# so the frame the display shows has to BE colours — which is two more vec-scal
# multiplies on the escape counter and nothing else. Grey on purpose: the ramp
# is `level * 0x010101`, so a correct packing renders greyscale and a wrong one
# (an alpha byte in the low bits, say) renders tinted, which is far easier to
# spot than a subtly wrong shade. Both multipliers fit the 20-bit immediate;
# one combined multiplier would not.
MB_LEVEL_STEP = 12              # esum 0..19  ->  level 0..228
MB_GREY = 0x010101              # level -> packed grey (65793)


def mandelbrot_colour(esum):
    """Packed RGB for a lane, from its escape-counter value. Shared by the
    program (as two VS_MUL broadcasts) and the display's frozen frame."""
    return esum * MB_LEVEL_STEP * MB_GREY

# Registers. 1..4 are the hardwired operand slots, 5..11 the live state — the
# widening from eight is what made the kernel fit, and the ONES/BCAST
# generators then handed one of them back.
MB_V0, MB_V1, MB_V2, MB_V3 = SRC_VREG(0), SRC_VREG(1), SRC_VREG(2), SRC_VREG(3)
MB_ZX, MB_ZY = SRC_VREG(4), SRC_VREG(5)
MB_CX, MB_CY = SRC_VREG(6), SRC_VREG(7)
MB_E, MB_ESUM, MB_T = SRC_VREG(8), SRC_VREG(9), SRC_VREG(10)
# VREG11 is free: the all-ones frame it used to hold is hardware now (ONES/BCAST).

# Scratch rows, above the PC so no ROM row can ever shadow one (see the note
# on DST_COORD_LANE_7). 2200 + n.
MB_DST_ESCAPED = 2200           # vred_sum(E)    = how many lanes escaped
MB_DST_ESUM = 2201              # vred_sum(ESUM) = sum over lanes of K - iter
MB_DST_CX = 2202                # cx at the probe lane  (grid setup check)
MB_DST_CY = 2203                # cy at the probe lane
MB_DST_COLOUR = 2204            # the display palette at the probe lane
MB_DST_SCAN = 2210              # 2210 + n: the scanline, one row per lane read


def _tdiv(a, b):
    """Factorio integer division: truncate toward zero (pinned by the op-farm
    bench's negative_division_truncates_toward_zero step)."""
    q = abs(a) // abs(b)
    return -q if (a < 0) != (b < 0) else q


def mandelbrot_reference(lane):
    """What the hardware must produce for one lane: (escaped, esum).

    `esum` is the running sum of the STICKY escape mask, so with K iterations
    the escape iteration is K - esum. Once a lane escapes, nothing it computes
    afterwards can change either output — the mask is sticky and A_MAX of 1
    with anything is 1 — so this model can stop iterating z at that point. It
    therefore never has to reproduce Factorio's behaviour on 32-bit overflow,
    which is exactly what an escaped lane's z does within a few passes. On the
    part that IS asserted, no value ever exceeds ~7.5e7."""
    x = lane % MB_WIDTH
    y = _tdiv(lane, MB_WIDTH)
    cx = x * MB_DX - MB_OX
    cy = y * MB_DY - MB_OY
    zx = zy = 0
    escaped = 0
    esum = 0
    for _ in range(MB_ITERS):
        if not escaped and zx * zx + zy * zy > MB_R2MAX:
            escaped = 1
        esum += escaped
        if escaped:
            continue
        zx, zy = (_tdiv(zx * zx - zy * zy, MB_SCALE) + cx,
                  _tdiv(zx * zy, MB_SCALE // 2) + cy)
    return escaped, esum


def mandelbrot(cfg, rows, lane_count):
    """The mandelbrot set over the whole 2451-lane vector, 20 iterations.

    No vector instruction exists: every line below is a scalar write_imm or
    pulse_imm to a memory row, and processor.isa schedules the lot.

    SEEDING COSTS NO SCALAR WRITES. COORD is a hardware ramp (every lane holds
    its own address), so `x = i % W` and `y = i / W` are two vec-scal ops and
    the whole 2451-point grid exists after four moves per axis.

    THE ESCAPE COUNTER NEEDS NO ADDER. VS_GT gives the lanes outside radius 2
    this pass; A_MAX against the previous mask makes it STICKY; and a write
    with no erase makes the register accumulate, so `ESUM += E` is one pulse.
    After K passes ESUM is K minus the escape iteration, per lane.

    STICKINESS IS ALSO WHAT MAKES THE ANSWER DEFINED. Escaped lanes keep
    iterating and their z overflows within a couple of passes, but the mask is
    already 1 and max(1, x) = 1, so neither output can be disturbed by it.
    Nothing here has to know what Factorio does on 32-bit overflow.

    The loop counter deliberately runs through the ALU (add_res = add_a + 1)
    rather than the accumulate cell at row 300: this program is long enough
    that the ROM's own rows shadow row 300 on the memory bus, and port A reads
    the bus."""
    ir = IR8(cfg)
    half = MB_SCALE // 2

    # -- loop-control constants ---------------------------------------------
    ir.write_imm(rows["cmp_n"], MB_ITERS, warm=True)
    ir.write_imm(rows["add_b"], 1, warm=True)
    ir.barrier()

    # -- seed the grid straight off the coordinate ramp ---------------------
    # ZERO AND ABSENT ARE THE SAME THING on a Factorio wire, and `each` against
    # a FIXED signal iterates only the first operand's lanes. So the whole
    # x = 0 column and the whole y = 0 row DISAPPEAR at the modulo/divide, and
    # no vec-scal op can put them back — none of them ever sees those lanes.
    # Measured: the first cut of this kernel scored 34 of 37, and the three
    # misses were exactly those lanes, with cx/cy of 0 instead of -OX/-OY.
    #
    # The offset therefore has to arrive as an ADDEND on the SECOND selector,
    # because a sum on the write bus reaches lanes the op did not. BCAST is
    # that addend as hardware — `vec_bcast2` in every lane — so `frame*k1 + k2`
    # is one move, and the whole seed is six. It used to be thirteen, plus a
    # register permanently holding ones.
    ir.vec_move(MB_V0, COORD)
    ir.vec_move(MB_T, VS_DIV, bcast=MB_WIDTH)                  # y = i / W
    ir.vec_move(MB_V0, MB_T)
    ir.vec_move(MB_CY, VS_MUL, src2=BCAST, bcast=MB_DY, bcast2=-MB_OY)

    ir.vec_move(MB_V0, COORD)
    ir.vec_move(MB_T, VS_MOD, bcast=MB_WIDTH)                  # x = i % W
    ir.vec_move(MB_V0, MB_T)
    ir.vec_move(MB_CX, VS_MUL, src2=BCAST, bcast=MB_DX, bcast2=-MB_OX)
    ir.barrier()

    # z starts at 0 and the registers come up empty, so there is nothing to
    # initialise: absent IS zero on a Factorio wire.
    ir.label("iter")
    # one pair-B load, then everything the iteration needs is on tap
    ir.vec_move(MB_V2, MB_ZX)
    ir.vec_move(MB_V3, MB_ZY)
    # escape test on the CURRENT z: r2 = zx^2 + zy^2, free on the bus
    ir.vec_move(MB_V0, B_SQ, src2=B_SQG)
    ir.vec_move(MB_V1, VS_GT, bcast=MB_R2MAX)          # this pass
    ir.vec_move(MB_V0, MB_E)                           # previous
    ir.vec_move(MB_E, A_MAX)                           # sticky OR
    ir.vec_move(MB_ESUM, MB_E, accumulate=True)        # ESUM += E
    # the update itself
    ir.vec_move(MB_V0, B_SQDIFF)
    ir.vec_move(MB_ZX, VS_DIV, src2=MB_CX, bcast=MB_SCALE)
    ir.vec_move(MB_V0, B_MUL)
    ir.vec_move(MB_ZY, VS_DIV, src2=MB_CY, bcast=half)
    # loop control: add_res = add_a + 1, cmp_m = add_a, jump while add_a < K
    ir.copy_a(rows["add_res"], rows["add_a"])
    ir.barrier()
    ir.copy_a(rows["add_a"], rows["cmp_m"])
    ir.barrier()
    ir.jump_if_zero(rows["flag_ge"], "iter")

    # -- readback -----------------------------------------------------------
    scan_lanes = [MB_SCAN_ROW * MB_WIDTH + x
                  for x in range(0, MB_WIDTH, MB_SCAN_STEP)]
    probe_lane = MB_SCAN_ROW * MB_WIDTH + MB_WIDTH // 2

    ir.vec_select(MB_E)
    ir.barrier()
    ir.copy_a(rows["vred_sum"], MB_DST_ESCAPED)
    ir.barrier()

    ir.vec_select(MB_ESUM)
    ir.barrier()
    ir.copy_a(rows["vred_sum"], MB_DST_ESUM)
    ir.barrier()
    for n, lane in enumerate(scan_lanes):
        ir.vec_read_lane(lane, MB_DST_SCAN + n)

    ir.vec_select(MB_CX)
    ir.barrier()
    ir.vec_read_lane(probe_lane, MB_DST_CX)
    ir.vec_select(MB_CY)
    ir.barrier()
    ir.vec_read_lane(probe_lane, MB_DST_CY)

    # -- turn the escape counter into a frame the lamp matrix can show ------
    # Two vec-scal multiplies over all 2451 lanes, then the mux is LEFT PARKED
    # on the result: the program halts with the picture standing on the vector
    # write bus, so the display stays lit for as long as the machine runs.
    ir.vec_move(MB_V0, MB_ESUM)
    ir.vec_move(MB_T, VS_MUL, bcast=MB_LEVEL_STEP)
    ir.vec_move(MB_V0, MB_T)
    ir.vec_move(MB_T, VS_MUL, bcast=MB_GREY)
    ir.vec_select(MB_T)
    ir.barrier()
    ir.vec_read_lane(probe_lane, MB_DST_COLOUR)
    ir.vec_select(MB_T)           # re-park: the readback is the last write
    ir.barrier()
    ir.halt()

    prog, _ = schedule8(ir, name="v10_mandelbrot", verbose=False)
    check_vector_hazards(ir, cfg)
    check_pulse_widths(ir)

    expect, art = mandelbrot_expectations(lane_count, scan_lanes, probe_lane)
    timeline = [
        {"name": "run_to_halt", "skip_ticks": _run_length(ir, MB_ITERS, "iter"),
         "settle_ticks": 1, "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": expect},
    ]
    return prog, timeline, mandelbrot_description(
        lane_count, scan_lanes, probe_lane, art, hand_written=True)


# ---------------------------------------------------------------------------
# The same kernel, COMPILED from the DSL (LANGUAGE.md §6, the acceptance test)
# ---------------------------------------------------------------------------

def mandelbrot_dsl(cfg, rows, lane_count):
    """`build_v10_tests.py:mandelbrot`, recompiled from the DSL.

    THIS IS THE ACCEPTANCE TEST FOR THE LANGUAGE and it is deliberately not a
    weaker one: it carries the SAME 38 expectations as the hand-written kernel,
    off the same reference model, so any divergence between what the designer
    wrote by hand and what the compiler emits is a failing bench rather than a
    number nobody looks at.

    What reproducing it demonstrates, because the hand-written version needed
    all six: instruction selection (B_SQ || B_SQG for the escape radius,
    B_SQDIFF and B_MUL off the same pair load), register allocation under fixed
    operand slots, loops, fixed point, mask accumulation, and the free-ADD
    idiom. The compiler matches it move for move — 8 in the seed, 11 per pass —
    and uses one register fewer, because linear scan spots that the seed's
    intermediate dies before its own result is written.

    Note how little of this is about vectors. `zx*zx` appears in two statements
    and is ONE DAG node; that is what lets the allocator see the pair is
    already loaded and skip three reloads.
    """
    S, half = MB_SCALE, MB_SCALE // 2
    m = Machine(cfg, rows, "mandelbrot")

    # z starts at 0 and registers come up empty, so there is nothing to
    # initialise: absent IS zero on a Factorio wire.
    zx, zy = m.vec("zx"), m.vec("zy")
    e, esum = m.vec("e"), m.vec("esum")

    # The grid, straight off the coordinate ramp. `- OX` is written as a
    # subtraction and compiled as an ADDEND on the second selector, which is
    # what keeps the x = 0 column and the y = 0 row alive (MANDELBROT.md §4).
    i = m.coord()
    y = (i // MB_WIDTH).named("y")
    cy = (y * MB_DY - MB_OY).named("cy")
    x = (i % MB_WIDTH).named("x")
    cx = (x * MB_DX - MB_OX).named("cx")

    with m.loop(MB_ITERS):
        r2 = (zx * zx + zy * zy).named("r2")     # escape test on the CURRENT z
        esc = (r2 > MB_R2MAX).named("esc")
        e = vmax(e, esc)                         # sticky OR
        esum = esum + e                          # accumulate: no adder anywhere
        zx, zy = ((zx * zx - zy * zy) // S + cx,
                  (zx * zy) // half + cy)        # the doubling folded into /2

    scan_lanes = [MB_SCAN_ROW * MB_WIDTH + n
                  for n in range(0, MB_WIDTH, MB_SCAN_STEP)]
    probe_lane = MB_SCAN_ROW * MB_WIDTH + MB_WIDTH // 2

    m.reduce_sum(e, MB_DST_ESCAPED)
    m.reduce_sum(esum, MB_DST_ESUM)
    for n, lane in enumerate(scan_lanes):
        m.read_lane(esum, lane, MB_DST_SCAN + n)
    m.read_lane(cx, probe_lane, MB_DST_CX)
    m.read_lane(cy, probe_lane, MB_DST_CY)

    # the display palette, and the mux left parked on it so the lamp matrix
    # stays lit for as long as the machine runs
    colour = (esum * MB_LEVEL_STEP * MB_GREY).named("colour")
    m.read_lane(colour, probe_lane, MB_DST_COLOUR)
    m.park(colour)

    m.compile()
    body = len(m.moves(m.loop_block().index))
    assert body <= 11, f"loop body compiled to {body} moves, hand-written is 11"
    ir = m.emit(label="iter")

    prog, _ = schedule8(ir, name="v10_mandelbrot_dsl", verbose=False)
    check_vector_hazards(ir, cfg)
    check_pulse_widths(ir)

    expect, art = mandelbrot_expectations(lane_count, scan_lanes, probe_lane)
    timeline = [
        {"name": "run_to_halt", "skip_ticks": _run_length(ir, MB_ITERS, "iter"),
         "settle_ticks": 1, "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": expect},
    ]
    return prog, timeline, mandelbrot_description(
        lane_count, scan_lanes, probe_lane, art, hand_written=False,
        moves=(len(m.moves(0)), body))


def mandelbrot_expectations(lane_count, scan_lanes, probe_lane):
    """The 38 expectations, from the reference model. ONE source of truth,
    shared by the hand-written kernel and the compiled one — which is what
    makes the acceptance test an acceptance test."""
    escaped_total = esum_total = 0
    for lane in range(1, lane_count + 1):
        e, s = mandelbrot_reference(lane)
        escaped_total += e
        esum_total += s
    px, py = probe_lane % MB_WIDTH, _tdiv(probe_lane, MB_WIDTH)
    expect = {
        f"$mem[{MB_DST_ESCAPED}]": escaped_total,
        f"$mem[{MB_DST_ESUM}]": esum_total,
        f"$mem[{MB_DST_CX}]": px * MB_DX - MB_OX,
        f"$mem[{MB_DST_CY}]": py * MB_DY - MB_OY,
        f"$mem[{MB_DST_COLOUR}]": mandelbrot_colour(
            mandelbrot_reference(probe_lane)[1]),
        "$poison_ctr": 0,
    }
    scan_esum = []
    for n, lane in enumerate(scan_lanes):
        _e, s = mandelbrot_reference(lane)
        scan_esum.append(s)
        expect[f"$mem[{MB_DST_SCAN + n}]"] = s
    art = "".join("#" if s == 0 else ".:-=+*%@"[min(s, MB_ITERS - 1) * 8 // MB_ITERS]
                  for s in scan_esum)
    return expect, art


def mandelbrot_description(lane_count, scan_lanes, probe_lane, art,
                           hand_written, moves=None):
    escaped_total = sum(mandelbrot_reference(l)[0] for l in range(1, lane_count + 1))
    esum_total = sum(mandelbrot_reference(l)[1] for l in range(1, lane_count + 1))
    if hand_written:
        head = ("computed by a scalar program that contains no vector "
                "instruction at all - every line of it is a write_imm or "
                "pulse_imm to a memory row. ")
    else:
        seed, body = moves
        head = ("COMPILED FROM THE vlang DSL - the source is the four-line "
                "kernel `r2 = zx*zx + zy*zy; esc = r2 > 4*S*S; e = vmax(e, "
                "esc); zx, zy = (zx*zx - zy*zy)//S + cx, (zx*zy)//(S//2) + cy` "
                "and nothing else. Selection, allocation and lowering to IR8 "
                f"produce {seed} moves of grid seed and {body} per pass, the "
                "same as the hand-written kernel, which is why this bench "
                "carries the identical expectations: any divergence between "
                "the two is a failure here. `zx*zx` appears in two statements "
                "and is ONE DAG node, which is how the allocator knows the "
                "pair-B load already stands and skips three reloads. ")
    return (
        f"The mandelbrot set over the whole vector: {lane_count} lanes, "
        f"{MB_WIDTH} wide, {MB_ITERS} iterations of z = z^2 + c in "
        f"{MB_SCALE}ths fixed point, " + head +
        "Seeding costs zero scalar writes: COORD gives each lane its own index "
        f"and the vec-scal MOD/DIV units turn that into x = i % {MB_WIDTH}, "
        f"y = i / {MB_WIDTH}, then cx = x*DX - OX and cy = y*DY - OY, with the "
        "offset applied as an ADDEND on the second mux selector so the x = 0 "
        "column and y = 0 row survive. One pair-B load per pass then yields "
        "every quantity the iteration needs at once - B_SQ and B_SQG summed on "
        "the write bus give the escape radius zx^2+zy^2 for free, B_SQDIFF "
        "gives zx^2-zy^2 and B_MUL gives zx*zy. The per-lane escape counter "
        "needs no adder either: VS_GT masks the lanes outside radius 2, A_MAX "
        "against the previous mask makes it sticky, and a register write with "
        "no erase accumulates it, so after K passes each lane holds K minus its "
        "own escape iteration. Checked against a Python model lane by lane: "
        f"{escaped_total} of {lane_count} lanes escape, the escape-iteration "
        f"sum over the whole frame is {esum_total}, a {len(scan_lanes)}-lane "
        f"scanline through row {MB_SCAN_ROW} comes back exactly ({art}), and "
        f"the grid itself is spot-checked at lane {probe_lane}. Stickiness is "
        "what makes the answer defined rather than merely large: escaped lanes "
        "go on iterating and overflow within a couple of passes, but "
        "max(1, x) = 1, so nothing they do afterwards can reach either output."
    )


def _run_length(ir, iterations, label):
    """Ticks to run: the straight-line span plus the extra passes of the loop,
    with margin. Overshooting is free — the program ends in a halt spin."""
    last = max(o.last_slot for o in ir.ops if o.last_slot is not None)
    label_at = ir.labels[label]
    body_first = min(o.first_slot for o in ir.ops[label_at:]
                     if o.first_slot is not None)
    jump = next(o for o in ir.ops if o.kind == "jump_if_zero")
    body = jump.slot + 8 - body_first
    return int((last + (iterations - 1) * body) * 1.3) + 200


BENCHES = {
    "vector_roundtrip": vector_roundtrip,
    "mandelbrot": mandelbrot,
    "mandelbrot_dsl": mandelbrot_dsl,
}


def main() -> None:
    base = json.loads((ROOT / "modules" / "v10_processor.source.json").read_text(encoding="utf-8"))
    signals = base["signals"]
    cfg = config_from_signals_map(signals)
    rows = {k: int(v["address"]) for k, v in signals.items()
            if "." not in k and "address" in v}
    space = design_address_space(signals)
    lane_count = len(space)

    state_probe = probe_entity("processor0.state")
    for name, builder in BENCHES.items():
        prog, timeline, description = builder(cfg, rows, lane_count)
        instructions = assemble_program(prog.rom_entries())
        src = json.loads(json.dumps(base))
        src["name"] = f"v10_processor_{name}"
        src["description"] = f"v10 program: {description}"
        patched = 0
        for entity in src["blueprint"]["entities"]:
            if entity["entity_number"] == ROM_ENTITY:
                assert entity.get("player_description") == "ROM", entity
                entity["control_behavior"] = {
                    "sections": {"sections": [{"index": 1,
                                               "filters": rom_filters_for_design(instructions, space)}]}
                }
                patched += 1
        assert patched == 1, f"ROM entity {ROM_ENTITY} not found"
        tb = json.loads(json.dumps(
            {"name": f"v10_proc_{name}", "description": description,
             **TB_COMMON, "timeline": timeline}))
        tb["entity_map"]["output_probes"][0]["entity_number"] = state_probe
        (ROOT / "modules" / f"v10_processor_{name}.source.json").write_text(
            json.dumps(src, indent=2), encoding="utf-8")
        (ROOT / "testbenches" / f"v10_proc_{name}.tb.json").write_text(
            json.dumps(tb, indent=2), encoding="utf-8")
        print(f"{name}: {len(instructions)} ROM words, {lane_count} lanes "
              f"-> v10_processor_{name}.source.json + v10_proc_{name}.tb.json")


if __name__ == "__main__":
    main()
