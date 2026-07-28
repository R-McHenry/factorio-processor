#!/usr/bin/env python3
"""Measurement benches for the v8 processor (modules/processor_v8.source.json).

v8 changes under test (hand-scheduled ROMs; the compiler learns the constants
only after these measure green):
1. relative_jump — out4 -> pc_inject sums into the PC cell. An accumulate cell
   counts executed instructions, so the final count + the P trace on mem_state
   give the jump's effect tick and shadow length directly.
2. latched_read — out3 -> a_rd_latch: port A address persists until replaced
   (+1 latency vs the old 1-tick pulse).
3. accumulate — reset-blocked cells (shape-vertical et al.) sum writes instead
   of replacing; plain cells still replace; negative values hold (!=0 cells).
4. mmap_port_b — port B's read address is the VALUE of the shape-circle cell
   (mmap_addr mask on the green bus); write that cell to re-park the stream.

Entity numbers (v8): ROM=41, mem cell=36, port_a_rd=64, port_b_rd=65,
intruction_rd=66, matrix outs 7/11/12/18.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assembler import assemble_program  # noqa: E402
from signal_space import address_of  # noqa: E402
from build_closed_loop import rom_filters  # noqa: E402

ROM_ENTITY = 41
ACC = address_of("shape-vertical")     # reset-blocked (accumulate) cell
MMAP = address_of("shape-circle")      # port B address register cell
H = address_of("signal-H")
G = address_of("signal-G")

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
            "origin_y": -8.0,
            "blueprint_string": "0eNqFz8EKgzAMBuB3ybnCdNVpX2UM0S5KQKO0dcxJ333RwXZcLyEh+X66QTssODviAGYDshN7MNcNPPXcDPuMmxHBAA5ogyObIKPr10Qu0HWNRYgKiO/4BJPGmwLkQIHwwxzNWvMytuhkQf3nFMyTF2HiPV3URCtYpRQS1C5dh6729BIkPX3fHkwBR5F/H1LwQOcPKC+yKteZLqtLqc95jG8gJlKg"
        }
    ],
    "restore_pause_state": True,
    "entity_map": {
        "drivers": {"trig_set": 15},   # inert: runner requires >=1 driver; never driven
        "expected_probe_name": "mem_state",
        "output_probes": [
            {"name": "mem_state", "entity_number": 36, "wire": "red"},
            {"name": "port_a", "entity_number": 64, "wire": "green"},
            {"name": "port_b", "entity_number": 65, "wire": "green"},
            {"name": "instr_port", "entity_number": 66, "wire": "green"},
            {"name": "out4_inject", "entity_number": 18, "wire": "green"}
        ]
    },
}


def relative_jump():
    """Counter counts executed instructions; the +8 injection at addr 15 skips
    some of 16..23 (minus the shadow). Observational expectations — the P ramp
    on mem_state shows the effect tick, the final count shows slots skipped."""
    rom = [{"addr": 1, "routes": ["const->write_addr"], "imm": ACC}]
    rom += [{"addr": a, "routes": ["const->write_value"], "imm": 1}
            for a in range(3, 31) if a != 15]
    rom.append({"addr": 15, "routes": ["const->out4"], "imm": 8})
    timeline = [{
        "name": "run_and_observe",
        "settle_ticks": 1,
        "trace_ticks": 45,
        "expect_probe": "mem_state",
        "expect": {}
    }]
    return rom, timeline, ("Relative jump via pc_inject: instruction counter in an accumulate "
                           "cell + P trace measure the jump's effect tick and shadow.")


def latched_read():
    rom = [
        {"addr": 1, "routes": ["const->write_addr"], "imm": H},
        {"addr": 3, "routes": ["const->write_value"], "imm": 42},
        {"addr": 5, "routes": ["const->write_addr"], "imm": G},
        {"addr": 7, "routes": ["const->write_value"], "imm": 77},
        {"addr": 12, "routes": ["const->out3"], "imm": H},
        {"addr": 22, "routes": ["const->out3"], "imm": G},
    ]
    timeline = [
        {"name": "port_a_latched_on_H", "settle_ticks": 1, "trace_ticks": 19,
         "expect_probe": "port_a", "expect": {"signal-H": 42}},
        {"name": "port_a_reparked_on_G", "settle_ticks": 1, "trace_ticks": 15,
         "expect_probe": "port_a", "expect": {"signal-G": 77, "signal-H": 0}},
    ]
    return rom, timeline, ("Latched port A: one out3 address instruction, data persists on "
                           "port_a_rd until re-parked (no +3 consumer pulse dance).")


def accumulate():
    rom = [
        {"addr": 1, "routes": ["const->write_addr"], "imm": ACC},
        {"addr": 3, "routes": ["const->write_value"], "imm": 5},
        {"addr": 4, "routes": ["const->write_value"], "imm": 5},
        {"addr": 5, "routes": ["const->write_value"], "imm": -3},
        {"addr": 7, "routes": ["const->write_addr"], "imm": H},
        {"addr": 9, "routes": ["const->write_value"], "imm": 5},
        {"addr": 10, "routes": ["const->write_value"], "imm": 5},
        {"addr": 12, "routes": ["const->write_addr"], "imm": G},
        {"addr": 14, "routes": ["const->write_value"], "imm": -9},
    ]
    timeline = [{
        "name": "acc_sums_plain_replaces_neg_holds",
        "settle_ticks": 1,
        "trace_ticks": 30,
        "expect_probe": "mem_state",
        "expect": {"shape-vertical": 7, "signal-H": 5, "signal-G": -9}
    }]
    return rom, timeline, ("Accumulate cells: writes to reset-blocked shape-vertical sum "
                           "(5+5-3=7); plain H replaces (5); negative value holds in G (-9).")


def mmap_port_b():
    rom = [
        {"addr": 1, "routes": ["const->write_addr"], "imm": H},
        {"addr": 3, "routes": ["const->write_value"], "imm": 77},
        {"addr": 5, "routes": ["const->write_addr"], "imm": MMAP},
        {"addr": 7, "routes": ["const->write_value"], "imm": H},
        {"addr": 9, "routes": ["const->write_addr"], "imm": G},
        {"addr": 11, "routes": ["const->write_value"], "imm": 33},
        {"addr": 13, "routes": ["const->write_addr"], "imm": MMAP},
        {"addr": 15, "routes": ["const->write_value"], "imm": G},
    ]
    timeline = [
        {"name": "port_b_streams_H", "settle_ticks": 1, "trace_ticks": 20,
         "expect_probe": "port_b", "expect": {"signal-H": 77}},
        {"name": "port_b_reparked_on_G", "settle_ticks": 1, "trace_ticks": 14,
         "expect_probe": "port_b", "expect": {"signal-G": 33, "signal-H": 0}},
    ]
    return rom, timeline, ("Memory-mapped port B address: write the shape-circle cell with a "
                           "target address; port_b_rd streams that cell until re-parked.")


def fib_cmp_v8():
    """Compiler-scheduled fib in the v8 ISA (machine_v8.schedule8): port B
    streams I, port A latch-copies, absolute conditional jump, halt spin.
    A poison add_imm AFTER the halt proves the spin traps the PC: if the halt
    failed, shape-horizontal would read 111."""
    from machine_v8 import IR8, schedule8
    G, H, M, N_ = (address_of(f"signal-{s}") for s in "GHMN")
    I_, Q_ = address_of("signal-I"), address_of("signal-Q")
    W = address_of("signal-heart")
    POISON = address_of("shape-horizontal")

    ir = IR8()
    ir.write_imm(N_, 100, warm=True)
    ir.write_imm(H, 1)
    ir.park_b(I_)
    ir.barrier()
    ir.label("loop")
    ir.copy_b(H)                  # H <- old I (stream reads go first in a stage)
    ir.copy_a(H, G)               # G <- old H
    ir.barrier()
    ir.copy_a(H, M)               # M <- new H
    ir.barrier()
    ir.jump_if_zero(Q_, "loop")
    ir.write_imm(W, 999)
    ir.halt()
    ir.add_imm(POISON, 111)       # only reachable if the halt spin fails

    prog, _ = schedule8(ir, name="fib_cmp_v8", verbose=True)
    rom = [{"addr": a, "routes": e["routes"], "imm": e["imm"] or 0}
           for a, e in sorted(prog.slots.items())]
    timeline = [
        {"name": "trace_first_passes", "settle_ticks": 1, "trace_ticks": 40,
         "expect_probe": "mem_state", "expect": {}},
        {"name": "skip_to_completion", "skip_ticks": 320, "settle_ticks": 1,
         "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": {"signal-G": 89, "signal-H": 144, "signal-M": 144,
                    "signal-N": 100, "signal-heart": 999, "shape-horizontal": 0}},
    ]
    return rom, timeline, ("Auto-scheduled v8 fib_cmp: B streams I, latched-A copies, "
                           "CMP-steered absolute jump, halt spin with poison canary.")


def spin_counter():
    """jump_rel backward loop emitted by the compiler: add_imm counts passes.
    Observational — the trace shows the loop period and steady accumulation."""
    from machine_v8 import IR8, schedule8
    COUNT = address_of("shape-vertical")

    ir = IR8()
    ir.write_imm(address_of("signal-H"), 1)   # something in the prelude
    ir.barrier()
    ir.label("top")
    ir.add_imm(COUNT, 1)
    ir.jump_rel("top")

    prog, _ = schedule8(ir, name="spin_counter", verbose=True)
    rom = [{"addr": a, "routes": e["routes"], "imm": e["imm"] or 0}
           for a, e in sorted(prog.slots.items())]
    timeline = [{"name": "spin", "settle_ticks": 1, "trace_ticks": 45,
                 "expect_probe": "mem_state", "expect": {}}]
    return rom, timeline, ("Compiler-emitted jump_rel loop accumulating a pass counter "
                           "(observational: period + steady increments in the trace).")


BENCHES = {
    "relative_jump": relative_jump,
    "latched_read": latched_read,
    "accumulate": accumulate,
    "mmap_port_b": mmap_port_b,
    "fib_cmp_v8": fib_cmp_v8,
    "spin_counter": spin_counter,
}


def main() -> None:
    base = json.loads((ROOT / "modules" / "processor_v8.source.json").read_text(encoding="utf-8"))
    for name, builder in BENCHES.items():
        rom, timeline, description = builder()
        instructions = assemble_program(rom)
        src = json.loads(json.dumps(base))  # deep copy
        src["name"] = f"processor_v8_{name}"
        src["description"] = f"v8 measurement: {description}"
        for entity in src["blueprint"]["entities"]:
            if entity["entity_number"] == ROM_ENTITY:
                entity["control_behavior"] = {
                    "sections": {"sections": [{"index": 1, "filters": rom_filters(instructions)}]}
                }
        tb = {"name": f"proc_v8_{name}", "description": description, **TB_COMMON,
              "timeline": timeline}
        (ROOT / "modules" / f"processor_v8_{name}.source.json").write_text(
            json.dumps(src, indent=2), encoding="utf-8")
        (ROOT / "testbenches" / f"proc_v8_{name}.tb.json").write_text(
            json.dumps(tb, indent=2), encoding="utf-8")
        print(f"{name}: {len(instructions)} ROM words -> "
              f"processor_v8_{name}.source.json + proc_v8_{name}.tb.json")


if __name__ == "__main__":
    main()
