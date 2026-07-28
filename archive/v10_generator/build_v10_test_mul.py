#!/usr/bin/env python3
"""First live test of the v10 processor: an assembled program (machine_v8's
IR8/schedule8 -- the same compiler v8 uses, since v10 reuses v8's exact
scalar core and write path) writes two operand vectors into register 0 and
register 1, then selects VVEC_MUL's output onto the shared read-select bus.

Registers 0/1 are the FIXED, always-on operand pair for every vec-vec/
vec-scal-vec op (confirmed 2026-07-22 by tracing WIRE_TEMPLATE: REG_CELL[0]'s
OUT_R feeds the op farm's shared IN_R, REG_CELL[1]'s OUT_G feeds its shared
IN_G) -- so writing 6 to register 0's signal-A lane and 7 to register 1's
signal-A lane, then reading the op farm, should show signal-A=42 wherever
VVEC_MUL landed.

This is the first time ANY of the following gets tested live:
  - the register bank's write path (address decode -> hold cell), whose
    hold-cell logic is the single most speculative part of the v10 design
  - the R/S read-select mechanism actually gating an op's output through
  - whether a write to the un-held R/S carrier network persists long enough
    (no dedicated hold cell backs it, unlike the vector registers) for a
    later instruction's write pulse to still be visible when polled

Outputs: modules/v10_test_mul.source.json, testbenches/v10_test_mul.tb.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from assembler import assemble_program  # noqa: E402
from machine_v8 import IR8, schedule8  # noqa: E402
from tools.build_closed_loop import rom_filters  # noqa: E402
import signal_space as ss  # noqa: E402
import modules.v10_address_map as vam  # noqa: E402
import tools.generate_v10_vector_zone as vz  # noqa: E402
import tools.generate_v10_processor as gp  # noqa: E402

ROM_ENTITY = 41          # same as v8 -- scalar core entity numbers preserved 1:1 (verified)
MEM_STATE_ENTITY = 36    # same as v8
REG_CELL_0_ENTITY = 82   # register 0's hold cell (build_register_bank, x_cell column)
REG_CELL_1_ENTITY = 88   # register 1's hold cell
OPFARM_MUL_ENTITY = 126  # VVEC_MUL's always-on combinator (op_rows[0], R index 9)
OPFARM_RSEL_MUL_ENTITY = 127  # VVEC_MUL's read-select gate (gates onto the shared vector bus)

LANE_NAME, LANE_QUALITY = "signal-A", "normal"  # arbitrary lane; ops apply per-signal-name
OPERAND_0, OPERAND_1 = 6, 7
EXPECTED = OPERAND_0 * OPERAND_1

lane_addr = ss.address_of(LANE_NAME, LANE_QUALITY)
REG0_ADDR = vam.register_chunk_base(0) - 1 + lane_addr
REG1_ADDR = vam.register_chunk_base(1) - 1 + lane_addr
R_NAME, R_QUALITY = vam.CARRIER_SIGNALS["VEC_READ_SELECT_PORT_A"]
R_ADDR = ss.address_of(R_NAME, R_QUALITY)
MUL_R_INDEX = vam.READ_SELECT_INDEX["VVEC_MUL"]


def build_program() -> list[tuple[int, int]]:
    ir = IR8()
    # warm writes: hold the address for 3 ticks before the value pulses, giving
    # the register bank's (unconfirmed) hold-cell logic the best chance to
    # actually latch the value rather than missing a 1-tick pulse.
    ir.write_imm(REG0_ADDR, OPERAND_0, warm=True)
    ir.barrier()
    ir.write_imm(REG1_ADDR, OPERAND_1, warm=True)
    ir.barrier()
    # R has no dedicated hold cell (unlike the vector registers) -- this
    # write is expected to be transient; scheduled last, after both operands
    # are already stably held, so its brief pulse gates an already-stable
    # product through rather than racing the operands into place.
    ir.write_imm(R_ADDR, MUL_R_INDEX)

    prog, _ = schedule8(ir, name="v10_test_mul", verbose=True)
    rom = [{"addr": a, "routes": e["routes"], "imm": e["imm"] or 0}
           for a, e in sorted(prog.slots.items())]
    return assemble_program(rom)


def main() -> None:
    instructions = build_program()

    bp = vz.BlueprintBuilder()
    gp.build_full(bp)
    decoded = bp.to_dict()
    rom_baked = False
    for entity in decoded["blueprint"]["entities"]:
        if entity["entity_number"] == ROM_ENTITY:
            entity["control_behavior"] = {
                "sections": {"sections": [{"index": 1, "filters": rom_filters(instructions)}]}
            }
            rom_baked = True
    if not rom_baked:
        raise SystemExit(f"ROM entity {ROM_ENTITY} not found in generated blueprint")
    decoded["blueprint"]["label"] = "v10 test: VVEC_MUL end-to-end"

    src = {
        "name": "v10_test_mul",
        "description": (
            f"v10 first live test: write register0.{LANE_NAME}={OPERAND_0}, "
            f"register1.{LANE_NAME}={OPERAND_1} (warm writes), select R="
            f"{MUL_R_INDEX} (VVEC_MUL), expect {LANE_NAME}={EXPECTED} on the "
            "op farm and (if the read-select gate fires in time) the shared "
            "vector bus. Also checks whether the register bank's hold-cell "
            "actually latches a written value at all -- unconfirmed until now."
        ),
        "surface": "nauvis",
        "blueprint": decoded["blueprint"],
    }
    (ROOT / "modules" / "v10_test_mul.source.json").write_text(
        json.dumps(src, indent=2), encoding="utf-8")

    tb = {
        "name": "v10_test_mul",
        "description": src["description"],
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
                # y=-8 is v8's own TB_COMMON convention (tools/build_v8_tests.py) --
                # safe for the small ~77-entity scalar-only core, but v10's vector
                # zone extends down to y=-14.5, so that position silently overlapped
                # it: 0 ghosts/0 revived, no power anywhere, no error reported (found
                # 2026-07-22 -- every probe read empty for the whole trace). Use a
                # large, module-size-independent margin instead of a small offset
                # tuned for a different, smaller generator's footprint.
                "origin_x": 0.0,
                "origin_y": -100.0,
                "blueprint_string": "0eNqFz8EKgzAMBuB3ybnCdNVpX2UM0S5KQKO0dcxJ333RwXZcLyEh+X66QTssODviAGYDshN7MNcNPPXcDPuMmxHBAA5ogyObIKPr10Qu0HWNRYgKiO/4BJPGmwLkQIHwwxzNWvMytuhkQf3nFMyTF2HiPV3URCtYpRQS1C5dh6729BIkPX3fHkwBR5F/H1LwQOcPKC+yKteZLqtLqc95jG8gJlKg"
            }
        ],
        "restore_pause_state": True,
        "entity_map": {
            "drivers": {"trig_set": 15},  # inert: runner requires >=1 driver; never driven
            "expected_probe_name": "mem_state",
            "output_probes": [
                {"name": "mem_state", "entity_number": MEM_STATE_ENTITY, "wire": "red"},
                {"name": "reg0_cell", "entity_number": REG_CELL_0_ENTITY, "wire": "red"},
                {"name": "reg1_cell", "entity_number": REG_CELL_1_ENTITY, "wire": "red"},
                {"name": "opfarm_mul_raw", "entity_number": OPFARM_MUL_ENTITY, "wire": "red"},
                {"name": "vector_bus", "entity_number": OPFARM_RSEL_MUL_ENTITY, "wire": "red"},
            ],
        },
        "save_after_run": "claude_v10_test_mul",
        "timeline": [
            {
                "name": "observe_write_and_select",
                "settle_ticks": 1,
                "trace_ticks": 60,
                "expect_probe": "mem_state",
                "expect": {},
            },
        ],
    }
    (ROOT / "testbenches" / "v10_test_mul.tb.json").write_text(
        json.dumps(tb, indent=2), encoding="utf-8")

    print(f"ROM: {len(instructions)} words")
    print(f"register0[{LANE_NAME}] addr = {REG0_ADDR}, register1[{LANE_NAME}] addr = {REG1_ADDR}")
    print(f"R carrier = {R_NAME}@{R_QUALITY} (addr {R_ADDR}), VVEC_MUL index = {MUL_R_INDEX}")
    print("wrote modules/v10_test_mul.source.json + testbenches/v10_test_mul.tb.json")


if __name__ == "__main__":
    main()
