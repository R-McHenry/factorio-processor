#!/usr/bin/env python3
"""Program benches for the fnet-compiled v8 processor
(modules/v8_processor.source.json).

Unlike build_v8_tests.py (master entity numbers, signal_space letters), all
machine facts come from the compiled artifact itself: the MachineConfig
(PC/mmap/accumulate/ALU addresses) from the source's signals map, ROM row
signals from the design's exclusion-compacted address space, and testbench
expectations as symbolic "$name"/"$mem[N]" references — zero game-signal
literals, valid under any exclusion set (NETLIST_PLAN "regenerate everything
together").

Entity numbers (fnet processor, stable under v8_processor.fnet's layout
order): ROM=66, trio 46/47/48, probes: mem_state=101 (red), mem_bus=100,
port_a=77, port_b=83, instr_port=99 (green).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assembler import assemble_program  # noqa: E402
from factorio_memory_tb import design_address_space  # noqa: E402
from machine_v8 import (IR8, JUMP_REL_RESUME, config_from_signals_map,  # noqa: E402
                        jump_rel_a_window, schedule8)

ROM_ENTITY = 66

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
            "origin_y": -16.0,
            "blueprint_string": "0eNqFz8EKgzAMBuB3ybnCdNVpX2UM0S5KQKO0dcxJ333RwXZcLyEh+X66QTssODviAGYDshN7MNcNPPXcDPuMmxHBAA5ogyObIKPr10Qu0HWNRYgKiO/4BJPGmwLkQIHwwxzNWvMytuhkQf3nFMyTF2HiPV3URCtYpRQS1C5dh6729BIkPX3fHkwBR5F/H1LwQOcPKC+yKteZLqtLqc95jG8gJlKg"
        }
    ],
    "restore_pause_state": True,
    "entity_map": {
        "drivers": {"trig_set": 46},   # inert: runner requires >=1 driver
        "expected_probe_name": "mem_state",
        "output_probes": [
            {"name": "mem_state", "entity_number": 101, "wire": "red"},
            {"name": "mem_bus", "entity_number": 100, "wire": "green"},
            {"name": "port_a", "entity_number": 77, "wire": "green"},
            {"name": "port_b", "entity_number": 83, "wire": "green"},
            {"name": "instr_port", "entity_number": 99, "wire": "green"},
        ]
    },
}


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


def accumulate_halt(cfg, rows):
    """Deterministic accumulate + halt: a plain write, exactly three add_imm
    pulses into `counter`, halt; a post-halt add_imm into `poison_ctr` is
    only reachable if the halt spin fails."""
    ir = IR8(cfg)
    ir.write_imm(50, 7)
    ir.barrier()
    ir.add_imm(rows["counter"], 1)
    ir.barrier()
    ir.add_imm(rows["counter"], 1)
    ir.barrier()
    ir.add_imm(rows["counter"], 1)
    ir.barrier()
    ir.halt()
    ir.add_imm(rows["poison_ctr"], 111)

    prog, _ = schedule8(ir, name="fnet_accumulate_halt", verbose=True)
    timeline = [
        {"name": "run_to_halt", "skip_ticks": 120, "settle_ticks": 1,
         "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": {"$mem[50]": 7, "$counter": 3, "$poison_ctr": 0}},
    ]
    return prog, timeline, ("write_imm + three add_imm pulses + halt on the fnet "
                            "machine: counter lands exactly at 3, the plain row "
                            "holds 7, and the post-halt poison stays 0 (halt spin "
                            "traps the PC).")


def fib_cmp(cfg, rows):
    """The v8 fib_cmp program (machine_v8 self-test) scheduled against the
    fnet machine's addresses: port B streams add_res, port A latch-copies,
    CMP-steered absolute jump, halt spin with poison canary."""
    G, H = rows["add_a"], rows["add_b"]
    I_, M, N_ = rows["add_res"], rows["cmp_m"], rows["cmp_n"]
    Q_ = rows["flag_ge"]
    W = 206
    ir = IR8(cfg)
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
    ir.add_imm(rows["poison_ctr"], 111)   # only reachable if the halt fails

    prog, _ = schedule8(ir, name="fnet_fib_cmp", verbose=True)
    timeline = [
        {"name": "trace_first_passes", "settle_ticks": 1, "trace_ticks": 40,
         "expect_probe": "mem_state", "expect": {}},
        {"name": "skip_to_completion", "skip_ticks": 320, "settle_ticks": 1,
         "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": {"$add_a": 89, "$add_b": 144, "$cmp_m": 144,
                    "$cmp_n": 100, "$mem[206]": 999, "$poison_ctr": 0}},
    ]
    return prog, timeline, ("machine_v8-scheduled fib_cmp against the fnet "
                            "machine's own addresses (config from the signals "
                            "map): B streams add_res, latched-A copies, CMP-"
                            "steered absolute jump, halt spin with poison canary.")


def bus_summation(cfg, rows):
    """The interconnect summing, end to end — four capabilities, no new hardware.

    v8_decoder_matrix proved that two sources aimed at one matrix dest both
    land on its net (24/24). This proves the other half: that the CONSUMERS
    sanitise correctly, i.e. that a dest holding one value split across
    signals is read as their sum. Port A's value rides its parked row's own
    signal and const's rides the immediate carrier, so every case below is a
    genuinely split value — nothing here would work if sanitising happened on
    the input side instead of the output side.

    1. ADD-IMMEDIATE IN FLIGHT. `a->out2 + const->out2` writes src + k in one
       slot. The ALU route costs a write, ALU_TO_USE_BASE + latency and a read;
       this costs the read. Loop counters should never touch the ALU again.
    2. MEM + MEM, NO ALU. `a->out2 + b->out2` adds two memory rows, using the
       latched port and the streaming port at once.
    3. INDEXED LOAD. `const->out3 + b->out3` computes port A's PARK ADDRESS on
       the interconnect: base + a streamed index, i.e. `arr[i]`. The array here
       is four rows holding 11/22/33/44 and the index walks them.
    4. INDEXED STORE. `const->out1 + b->out1` computes the WRITE address the
       same way, on the other dest: `arr[i] = k`.

    3 and 4 together are what turn named variables into data structures, and
    they are the reason this bench matters more than its size suggests.
    """
    BASE = 700          # arr[0..3] at 700..703
    IDX = 610           # the index variable, streamed through port B
    D_ADDK, D_MEMADD = 620, 621
    D_LOAD0, D_LOAD2, D_LOAD3 = 622, 623, 624
    SEED = 611

    ir = IR8(cfg)
    ir.write_imm(BASE + 0, 11)
    ir.write_imm(BASE + 1, 22)
    ir.write_imm(BASE + 2, 33)
    ir.write_imm(BASE + 3, 44)
    ir.write_imm(SEED, 35)
    ir.write_imm(IDX, 2)
    ir.barrier()

    # 1. add-immediate in flight
    ir.add_imm_a(SEED, D_ADDK, 7)                 # 35 + 7 = 42
    ir.barrier()

    # 2. mem + mem, with B streaming the seed
    ir.park_b(SEED)
    ir.barrier()
    ir.add_ab(D_ADDK, D_MEMADD)                   # 42 + 35 = 77
    ir.barrier()

    # 3. indexed load, index 2 -> arr[2] = 33
    ir.park_b(IDX)
    ir.barrier()
    ir.copy_a_indexed(BASE, D_LOAD2)
    ir.barrier()

    # 4. indexed store at the same index: arr[2] <- 555
    ir.write_indexed(BASE, 555)
    ir.barrier()

    # the index is a VARIABLE: bump it with the very add it is testing, then
    # read and write again, so nothing here can pass by accident of a constant
    ir.add_imm_a(IDX, IDX, 1)                     # i = 3
    ir.barrier()
    ir.park_b(IDX)
    ir.barrier()
    ir.copy_a_indexed(BASE, D_LOAD3)              # arr[3] = 44
    ir.barrier()
    ir.write_indexed(BASE, 666)                   # arr[3] <- 666
    ir.barrier()

    # index 0 proves the base is not being ignored
    ir.write_imm(IDX, 0)
    ir.barrier()
    ir.park_b(IDX)
    ir.barrier()
    ir.copy_a_indexed(BASE, D_LOAD0)              # arr[0] = 11
    ir.barrier()

    ir.halt()
    ir.add_imm(rows["poison_ctr"], 111)

    prog, _ = schedule8(ir, name="fnet_bus_summation", verbose=True)
    timeline = [
        {"name": "run_to_halt", "skip_ticks": 260, "settle_ticks": 1,
         "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": {
             f"$mem[{D_ADDK}]": 42,        # a->out2 + const->out2
             f"$mem[{D_MEMADD}]": 77,      # a->out2 + b->out2
             f"$mem[{D_LOAD2}]": 33,       # arr[2] via const->out3 + b->out3
             f"$mem[{D_LOAD3}]": 44,       # arr[3], index bumped by the adder
             f"$mem[{D_LOAD0}]": 11,       # arr[0]: the base is real
             f"$mem[{BASE + 2}]": 555,     # arr[2] = 555 via const->out1 + b->out1
             f"$mem[{BASE + 3}]": 666,     # arr[3] = 666
             f"$mem[{BASE + 0}]": 11,      # untouched neighbours
             f"$mem[{BASE + 1}]": 22,
             f"$mem[{IDX}]": 0,
             "$poison_ctr": 0,
         }},
    ]
    return prog, timeline, (
        "The interconnect summing, end to end. Two sources aimed at one matrix "
        "dest both land on its net carrying DIFFERENT signals, and the "
        "consumer sanitises with each+0 -> carrier, so the dest's value is "
        "their sum: add-immediate in flight (a+const on out2, 35+7=42), "
        "mem+mem with no ALU at all (a+b on out2, 42+35=77), an indexed LOAD "
        "with the read address computed on out3 (const base + streamed index: "
        "arr[2]=33, arr[3]=44, arr[0]=11) and an indexed STORE with the write "
        "address computed the same way on out1 (arr[2]=555, arr[3]=666, "
        "neighbours untouched). The index is a variable bumped by the very "
        "adder under test, so no case passes by accident of a constant. None "
        "of it is a new combinator - each is a different subset of the same 12 "
        "route bits, which is what a programmable interconnect buys instead of "
        "an instruction set.")


def computed_jump(cfg, rows):
    """`a->out4`: the jump distance as DATA, and the P=0 fall-through.

    This is the last untested corner of the interconnect and the one an `if`
    rides on. pc_inject sums whatever reaches out4 into the PC for one tick and
    does not care which source sent it, so routing PORT A there makes the jump
    distance a memory row. `mem[JV] = flag * skip` is then an if-statement:
    flag 0 injects nothing and execution falls through, flag 1 skips the block.

    Both halves are exercised, in one program:

      PHASE 1  JV = 0. The inject fires with a zero value. The PC cell must
               emit nothing at 0 and the autoincrement must re-seed — recorded
               in V8.md as "suspected self-healing", and this is the evidence.
               M_FALLTHRU proves execution continued.
      PHASE 2  JV = K, K chosen so the landing slot is exactly M_LAND's first
               cell. Everything between is skipped: M_SKIP1..3 stay 0 while
               M_LAND is written.

    The not-taken path is the one that runs most often, so proving it is worth
    more than proving the jump. K is BACKPATCHED after scheduling, because the
    distance depends on where the scheduler put things — the same backpatch a
    forward label will need when the language grows one.
    """
    JV = 630
    M_FALLTHRU, M_LAND = 631, 635
    SKIPS = [632, 633]

    ir = IR8(cfg)
    ir.write_imm(JV, 0, warm=True)
    ir.barrier()
    ir.jump_rel_a(JV)                       # phase 1: must fall through
    ir.write_imm(M_FALLTHRU, 111)
    ir.barrier()

    patch_me = ir.write_imm(JV, 0)          # phase 2 distance, backpatched
    ir.barrier()
    taken = ir.jump_rel_a(JV)
    skip_ops = []
    for row in SKIPS:
        skip_ops.append(ir.write_imm(row, 222))
        ir.barrier()
    land = ir.write_imm(M_LAND, 444)
    ir.barrier()
    ir.halt()
    ir.add_imm(rows["poison_ctr"], 111)

    prog, _ = schedule8(ir, name="fnet_computed_jump", verbose=True)

    # Land exactly on M_LAND's first cell: resume = n + K + JUMP_REL_RESUME.
    distance = land.slot - taken.slot - JUMP_REL_RESUME
    prog.slots[patch_me.value_slot]["imm"] = distance
    window = jump_rel_a_window(taken, distance)
    # A write_imm occupies [slot, value_slot]; a marker straddling the window
    # edge would have its address executed and its value skipped, which is
    # neither outcome and would make the bench meaningless rather than red.
    straddling = [op for op in skip_ops
                  if not (op.slot in window and op.value_slot in window)]
    if straddling:
        raise SystemExit(
            f"marker at slots {[ (o.slot, o.value_slot) for o in straddling ]} "
            f"straddles the skip window {window.start}..{window.stop - 1}; "
            f"use fewer markers so they fit wholly inside")

    timeline = [
        {"name": "run_to_halt", "skip_ticks": 200, "settle_ticks": 1,
         "trace_ticks": 3, "expect_probe": "mem_state",
         "expect": {
             f"$mem[{M_FALLTHRU}]": 111,          # P=0 fell through
             f"$mem[{SKIPS[0]}]": 0,              # ...and K skipped these
             f"$mem[{SKIPS[1]}]": 0,
             f"$mem[{M_LAND}]": 444,              # landing on the far side
             f"$mem[{JV}]": distance,
             "$poison_ctr": 0,
         }},
    ]
    return prog, timeline, (
        "A COMPUTED relative jump: `a->out4` puts the jump distance in a "
        "memory row instead of the instruction word, which is how this machine "
        "gets an if-statement without an if-instruction - `mem[JV] = flag * "
        "skip`, so 0 falls through and 1 skips. Both halves run here. First "
        "the inject fires with JV = 0 and execution must CONTINUE: the PC cell "
        "emits nothing at zero and the autoincrement re-seeds, which V8.md had "
        "only ever recorded as 'suspected self-healing' and which is the path "
        "a not-taken branch takes every time. Then JV = " + str(distance) + ", "
        "chosen so fetch resumes exactly on the far marker: the markers "
        "in between stay 0 and the far one is written. The distance is "
        "backpatched after scheduling, since it depends on where the scheduler "
        "put things - the same backpatch a forward label will need.")


BENCHES = {
    "accumulate_halt": accumulate_halt,
    "fib_cmp": fib_cmp,
    "bus_summation": bus_summation,
    "computed_jump": computed_jump,
}


def main() -> None:
    base = json.loads((ROOT / "modules" / "v8_processor.source.json").read_text(encoding="utf-8"))
    signals = base["signals"]
    cfg = config_from_signals_map(signals)
    rows = {k: int(v["address"]) for k, v in signals.items()
            if "." not in k and "address" in v}
    space = design_address_space(signals)
    for name, builder in BENCHES.items():
        prog, timeline, description = builder(cfg, rows)
        instructions = assemble_program(prog.rom_entries())
        src = json.loads(json.dumps(base))
        src["name"] = f"v8_processor_{name}"
        src["description"] = f"fnet v8 program: {description}"
        patched = 0
        for entity in src["blueprint"]["entities"]:
            if entity["entity_number"] == ROM_ENTITY:
                assert entity.get("player_description") == "ROM", entity
                entity["control_behavior"] = {
                    "sections": {"sections": [{"index": 1,
                                               "filters": rom_filters_for_design(instructions, space)}]}
                }
                patched += 1
        assert patched == 1
        tb = {"name": f"v8_proc_{name}", "description": description, **TB_COMMON,
              "timeline": timeline}
        (ROOT / "modules" / f"v8_processor_{name}.source.json").write_text(
            json.dumps(src, indent=2), encoding="utf-8")
        (ROOT / "testbenches" / f"v8_proc_{name}.tb.json").write_text(
            json.dumps(tb, indent=2), encoding="utf-8")
        print(f"{name}: {len(instructions)} ROM words -> "
              f"v8_processor_{name}.source.json + v8_proc_{name}.tb.json")


if __name__ == "__main__":
    main()
