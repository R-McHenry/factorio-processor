#!/usr/bin/env python3
"""Generate the closed-loop processor source + hello-world testbench.

From modules/processor.source.json:
1. Convert the four intentionally-red matrix_out -> input wires to green (closes the loop).
2. Add the missing program-port data wire (43.Gin -- 44.Gin): without it intruction_rd
   always reads 0 and the ROM can never reach the decoder.
3. Clear stimulus from the input constants (write_adress, write_value).
4. Replace the four addres_table combinators' contents with a `signal_table` field —
   the runner expands it to the full ~2k address space post-paste.
5. Assemble the proto hello-world program into ROM: set write address once (latched),
   then stream 'h','e','l','l','o' into the same slot via const->write_value.

Outputs: modules/processor_full.source.json, testbenches/proc_hello_world.tb.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assembler import Program, assemble_program  # noqa: E402
from signal_space import address_of, signal_at, total_addresses  # noqa: E402
from verifier import verify_or_die  # noqa: E402
from scheduler import IR, schedule  # noqa: E402

# v8 entity numbers (master is closed-loop: matrix_out wires green, program-port
# data wire present; nothing to patch, only to verify)
ADDRESS_TABLE_ENTITIES = [6, 44, 45, 46]
MATRIX_LOOP_WIRES = [
    [1, 2, 7, 2],     # write_adress  -- matrix_out1 (green)
    [4, 2, 11, 2],    # write_value   -- matrix_out2 (green)
    [12, 2, 17, 2],   # matrix_out3   -- addr_rd_a   (green, into the latch)
    [18, 2, 26, 2],   # matrix_out4   -- pc_inject   (green)
]
PROGRAM_PORT_WIRES = [[56, 2, 57, 2], [57, 2, 58, 2]]  # read-data green chain incl. program port
CLEAR_STIMULUS_ENTITIES = [1, 4, 15, 16]  # write_adress/value + trigger set/value
                                          # (19 = trigger reset carries the accumulate mask)
ROM_ENTITY = 41

TARGET_SLOT = ("signal-heart", "normal")
HELLO = [ord(c) for c in "hello"]


def build_rom_program() -> tuple[list[tuple[int, int]], int]:
    target_addr = address_of(*TARGET_SLOT)
    program = [{"addr": 1, "routes": ["const->write_addr"], "imm": target_addr}]
    # addr 2 = NOP: the address path is 2 combinators deeper than the value path,
    # so give the latch a tick before the first value flows.
    for offset, char in enumerate(HELLO):
        program.append({"addr": 3 + offset, "routes": ["const->write_value"], "imm": char})
    return assemble_program(program), target_addr


def build_array_write_program() -> list[tuple[int, int]]:
    """Offset array write: port A is parked on the PC's own slot (signal-P), so
    routing a->write_addr makes the write address track P itself. Each instruction
    also routes const->write_value with one character — consecutive instructions
    write consecutive addresses via the additive bus. No address per character.

    Measured rule (proc_array_write first run): value of instruction n lands at the
    address latched by instruction n-1, and a burst loses its first two values to
    address-latch pipeline fill. Two priming a->write_addr instructions fix both:
    values at instructions 3..7 land at addresses 2..6."""
    program = [
        {"addr": 1, "routes": ["a->write_addr"]},
        {"addr": 2, "routes": ["a->write_addr"]},
    ]
    program += [
        {"addr": 3 + i, "routes": ["a->write_addr", "const->write_value"], "imm": char}
        for i, char in enumerate(HELLO)
    ]
    return assemble_program(program)


FIB_THRESHOLD = 100
FIB_FINAL = (89, 144)  # (G, H) when H first reaches >= 100
DONE_MARKER = 999


def fib_program() -> Program:
    """Fibonacci loop with a conditional exit, no comparator needed.

    Cells: G=prev, H=curr (ADD computes I=G+H continuously), D=copy of curr for
    the compare, E=threshold. DIV computes F = D/E: 0 below threshold, >=1 at exit.
    The jump instruction writes the loop-start PC value to address 2096+F — the PC
    slot while F=0 (jump taken), a harmless P-quality neighbor slot once F>=1
    (jump bypassed, PC runs on into the exit marker).

    Built with the assembler's Program scheduler — the measured pipeline rules
    (addr/value pairing, port pulse timing, ALU readiness, jump delay slots)
    live in assembler.Program, not in hand-placed slot numbers.
    """
    G, H, D, E, F_ = (address_of(f"signal-{s}") for s in "GHDEF")
    I_ = address_of("signal-I")
    P = address_of("signal-P")
    W = address_of(*TARGET_SLOT)
    LOOP = 10

    p = Program()
    p.warm_latch(1, E)                          # slots 1-3; slot 3 doubles as E's address
    p.at(5, ["const->write_value"], FIB_THRESHOLD, "seed E=threshold (pairs slot 3)")
    p.write_imm(4, H, 1, "seed H=1 (G stays 0 unwritten)")
    # loop body: interleaved port-A/port-B copies, then DIV-as-comparator jump
    p.copy(LOOP + 0, H, G, port="a", why="G <- H")
    p.copy(LOOP + 2, I_, H, port="b", why="H <- I (old G+H)")
    d_value = p.copy(LOOP + 8, H, D, port="b",
                     why="D <- H after H's cell update (reading I here races G: I=H+H)")
    f_read = Program.alu_ready_pulse(d_value, op_latency=1)
    jump_value = p.jump_offset(f_read, F_, P, LOOP - 1, why="loop while F = D/E == 0")
    # exit marker: first slot never fetched during looping passes
    p.write_imm(jump_value + Program.JUMP_DELAY_SLOTS + 1, W, DONE_MARKER, why="done marker")
    return p


def build_fib_program() -> list[tuple[int, int]]:
    return assemble_program(verify_or_die(fib_program(), name="fib").rom_entries())


def fib_expectations() -> dict[str, int]:
    return {
        "signal-G": FIB_FINAL[0],
        "signal-H": FIB_FINAL[1],
        "signal-D": FIB_FINAL[1],
        "signal-E": FIB_THRESHOLD,
        TARGET_SLOT[0]: DONE_MARKER,
    }


def fib_cmp_program() -> Program:
    """Fibonacci loop, exit steered by the CMP unit instead of DIV.

    Cells: G=prev, H=curr (ADD computes I=G+H), M=copy of curr, N=threshold.
    CMP's Q flag (1 if M >= N) offsets the jump's write address off the PC slot
    exactly like DIV's quotient did: addr = P + Q. Flags are latency 1.
    """
    G, H, M, N_ = (address_of(f"signal-{s}") for s in "GHMN")
    I_ = address_of("signal-I")
    Q_ = address_of("signal-Q")
    P = address_of("signal-P")
    W = address_of(*TARGET_SLOT)
    LOOP = 10

    p = Program()
    p.warm_latch(1, N_)                         # slots 1-3; slot 3 doubles as N's address
    p.at(5, ["const->write_value"], FIB_THRESHOLD, "seed N=threshold (pairs slot 3)")
    p.write_imm(4, H, 1, "seed H=1 (G stays 0 unwritten)")
    p.copy(LOOP + 0, H, G, port="a", why="G <- H")
    p.copy(LOOP + 2, I_, H, port="b", why="H <- I (old G+H)")
    m_value = p.copy(LOOP + 8, H, M, port="b", why="M <- H after H's cell update")
    q_read = Program.alu_ready_pulse(m_value, op_latency=1)
    jump_value = p.jump_offset(q_read, Q_, P, LOOP - 1, why="loop while Q = (M >= N) == 0")
    p.write_imm(jump_value + Program.JUMP_DELAY_SLOTS + 1, W, DONE_MARKER, why="done marker")
    return p


def build_fib_cmp_program() -> list[tuple[int, int]]:
    return assemble_program(verify_or_die(fib_cmp_program(), name="fib_cmp").rom_entries())


def fib_cmp_auto_ir() -> IR:
    """Same fib_cmp semantics, but as scheduler IR: no slot numbers anywhere.
    Stage 1 runs G <- H and H <- I in parallel (both read pre-stage values);
    M <- H then reads the committed new H; the jump loops while Q = (M>=N) == 0."""
    G, H, M, N_ = (address_of(f"signal-{s}") for s in "GHMN")
    I_ = address_of("signal-I")
    Q_ = address_of("signal-Q")
    W = address_of(*TARGET_SLOT)

    ir = IR()
    ir.write_imm(N_, FIB_THRESHOLD, warm=True)
    ir.write_imm(H, 1)
    ir.barrier()
    ir.label("loop")
    ir.copy(H, G)
    ir.copy(I_, H)
    ir.barrier()
    ir.copy(H, M)
    ir.barrier()
    ir.jump_if_zero(Q_, "loop")
    ir.write_imm(W, DONE_MARKER)
    return ir


def build_fib_cmp_auto_program() -> list[tuple[int, int]]:
    prog, report = schedule(fib_cmp_auto_ir(), name="fib_cmp_auto")
    print(report.render())
    return assemble_program(prog.rom_entries())


def zero_write_ir() -> IR:
    """ROM-level zero-write proof (v7.1 trigger fires off the matrix out2 select):
    write 42 to the target, canary G=7, clear the target with an imm-0 write,
    canary H=9. Final memory heart=0/G=7/H=9 — if the trigger never fired the
    heart cell would still read 42, if clearing broke the machine the canaries fail."""
    G, H = address_of("signal-G"), address_of("signal-H")
    W = address_of(*TARGET_SLOT)

    ir = IR()
    ir.write_imm(W, 42, warm=True)
    ir.barrier()
    ir.write_imm(G, 7)
    ir.barrier()
    ir.write_imm(W, 0)
    ir.barrier()
    ir.write_imm(H, 9)
    return ir


def build_zero_write_program() -> list[tuple[int, int]]:
    prog, report = schedule(zero_write_ir(), name="zero_write")
    print(report.render())
    return assemble_program(prog.rom_entries())


def zero_write_expectations() -> dict[str, int]:
    return {TARGET_SLOT[0]: 0, "signal-G": 7, "signal-H": 9}


def fib_cmp_expectations() -> dict[str, int]:
    return {
        "signal-G": FIB_FINAL[0],
        "signal-H": FIB_FINAL[1],
        "signal-M": FIB_FINAL[1],
        "signal-N": FIB_THRESHOLD,
        TARGET_SLOT[0]: DONE_MARKER,
    }


def array_write_expectations() -> dict[str, int]:
    """Letters land at addresses 2..6 (value at instr n -> slot n-1)."""
    expect = {}
    for i, char in enumerate(HELLO):
        slot = signal_at(2 + i)
        key = slot["name"] if slot["quality"] == "normal" else f"{slot['name']}~{slot['quality']}"
        expect[key] = char
    return expect


def rom_filters(instructions: list[tuple[int, int]]) -> list[dict]:
    filters = []
    for index, (addr, word) in enumerate(instructions, start=1):
        slot = signal_at(addr)
        entry = {
            "index": index,
            "name": slot["name"],
            "quality": slot["quality"],
            "comparator": "=",
            "count": word,
        }
        if slot["type"] != "item":
            entry["type"] = slot["type"]
        filters.append(entry)
    return filters


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--program",
                        choices=["hello", "array", "fib", "fib_cmp", "fib_cmp_auto", "zerow"],
                        default="hello")
    args = parser.parse_args()

    src = json.loads((ROOT / "modules" / "processor.source.json").read_text(encoding="utf-8"))
    bp = src["blueprint"]

    def has_wire(wire):
        return wire in bp["wires"] or [wire[2], wire[3], wire[0], wire[1]] in bp["wires"]

    missing = [w for w in MATRIX_LOOP_WIRES + PROGRAM_PORT_WIRES if not has_wire(w)]
    if missing:
        raise RuntimeError(f"master source is missing closed-loop wires: {missing}")

    if args.program == "hello":
        instructions, target_addr = build_rom_program()
    elif args.program == "fib":
        instructions, target_addr = build_fib_program(), None
    elif args.program == "fib_cmp":
        instructions, target_addr = build_fib_cmp_program(), None
    elif args.program == "fib_cmp_auto":
        instructions, target_addr = build_fib_cmp_auto_program(), None
    elif args.program == "zerow":
        instructions, target_addr = build_zero_write_program(), None
    else:
        instructions, target_addr = build_array_write_program(), None
    for entity in bp["entities"]:
        num = entity["entity_number"]
        if num in CLEAR_STIMULUS_ENTITIES:
            entity.pop("control_behavior", None)
        elif num in ADDRESS_TABLE_ENTITIES:
            entity.pop("control_behavior", None)
            entity["signal_table"] = {"table": "full_address_space"}
        elif num == ROM_ENTITY:
            entity["control_behavior"] = {
                "sections": {"sections": [{"index": 1, "filters": rom_filters(instructions)}]}
            }

    tb_common = {
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
            "drivers": {
                "addr_a": 17
            },
            "expected_probe_name": "port_a",
            "output_probes": [
                {"name": "mem_state", "entity_number": 36, "wire": "red"},
                {"name": "port_a", "entity_number": 64, "wire": "green"},
                {"name": "port_b", "entity_number": 65, "wire": "green"},
                {"name": "instr_port", "entity_number": 66, "wire": "green"},
                {"name": "enc_write", "entity_number": 16, "wire": "green"},
                {"name": "out1", "entity_number": 7, "wire": "green"},
                {"name": "out2", "entity_number": 11, "wire": "green"}
            ]
        },
    }

    if args.program == "hello":
        src["name"] = "processor_full"
        src["description"] = (
            "Closed-loop processor: matrix_out wires green, full "
            f"{total_addresses()}-slot address space via signal_table, hello-world ROM "
            f"writing to {TARGET_SLOT[0]}/{TARGET_SLOT[1]} (addr {target_addr}). "
            "Generated by tools/build_closed_loop.py"
        )
        out_src = ROOT / "modules" / "processor_full.source.json"
        tb = {
            "name": "proc_hello_world",
            "description": (
                "Closed-loop hello world: PC free-runs from paste, ROM latches the write address "
                f"(addr {target_addr} = {TARGET_SLOT[0]}) then streams h,e,l,l,o into it via "
                "const->write_value. port A is parked on the target address for live readback. "
                "Final memory value must be 'o' (111)."
            ),
            **tb_common,
            "timeline": [
                {
                    "name": "run_program",
                    "drive": {"addr_a": {"signal-R": target_addr}},
                    "settle_ticks": 1,
                    "trace_ticks": 34,
                    "expect_probe": "port_a",
                    "expect": {TARGET_SLOT[0]: HELLO[-1]}
                },
                {
                    "name": "final_mem_check",
                    "settle_ticks": 1,
                    "trace_ticks": 2,
                    "expect_probe": "mem_state",
                    "expect": {TARGET_SLOT[0]: HELLO[-1]}
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_hello_world.tb.json"
    elif args.program == "fib":
        src["name"] = "processor_fib"
        src["description"] = (
            "Closed-loop processor, Fibonacci-loop ROM: ADD computes G+H->I, port reads "
            "copy G<-H, H<-I, D<-I each pass; DIV F=D/E acts as threshold comparator; "
            "jump writes loop-start to address 2096+F (PC slot while F=0, bypass slot "
            "once F>=1). Exits when fib >= "
            f"{FIB_THRESHOLD}, then writes {DONE_MARKER} to {TARGET_SLOT[0]}. "
            "Generated by tools/build_closed_loop.py --program fib"
        )
        out_src = ROOT / "modules" / "processor_fib.source.json"
        tb = {
            "name": "proc_fib_loop",
            "description": (
                "Fibonacci loop with conditional exit via DIV-as-comparator and a "
                "PC-write jump whose target address is offset by the quotient. "
                f"Expect G={FIB_FINAL[0]}, H=D={FIB_FINAL[1]}, E={FIB_THRESHOLD}, "
                f"{TARGET_SLOT[0]}={DONE_MARKER} when done."
            ),
            **tb_common,
            "save_after_run": "claude_fib_test",
            "timeline": [
                {
                    "name": "run_program",
                    "settle_ticks": 1,
                    "trace_ticks": 330,
                    "expect_probe": "mem_state",
                    "expect": fib_expectations()
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_fib_loop.tb.json"
    elif args.program == "fib_cmp":
        src["name"] = "processor_fib_cmp"
        src["description"] = (
            "Closed-loop processor, Fibonacci-loop ROM with CMP-steered exit: "
            "per pass G<-H, H<-I, M<-H; CMP's Q flag (M >= N threshold) offsets the "
            "PC-write jump address. Exits when fib >= "
            f"{FIB_THRESHOLD}, then writes {DONE_MARKER} to {TARGET_SLOT[0]}. "
            "Generated by tools/build_closed_loop.py --program fib_cmp"
        )
        out_src = ROOT / "modules" / "processor_fib_cmp.source.json"
        tb = {
            "name": "proc_fib_cmp",
            "description": (
                "Fibonacci with CMP-unit exit (Q = M>=N steers the jump) and "
                "fast-forward: traces the first passes at 1-tick resolution, then "
                "skips ahead and checks the final state. "
                f"Expect G={FIB_FINAL[0]}, H=M={FIB_FINAL[1]}, N={FIB_THRESHOLD}, "
                f"{TARGET_SLOT[0]}={DONE_MARKER}."
            ),
            **tb_common,
            "save_after_run": "claude_fib_cmp_test",
            "timeline": [
                {
                    "name": "trace_first_passes",
                    "settle_ticks": 1,
                    "trace_ticks": 40,
                    "expect_probe": "mem_state",
                    "expect": {}
                },
                {
                    "name": "skip_to_completion",
                    "skip_ticks": 300,
                    "settle_ticks": 1,
                    "trace_ticks": 3,
                    "expect_probe": "mem_state",
                    "expect": fib_cmp_expectations()
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_fib_cmp.tb.json"
    elif args.program == "fib_cmp_auto":
        src["name"] = "processor_fib_cmp_auto"
        src["description"] = (
            "Closed-loop processor, auto-scheduled fib_cmp ROM: same semantics as "
            "processor_fib_cmp but slots, ports and the loop-back target assigned by "
            "scheduler.py from a stage/label IR with no literal slot numbers. "
            "Generated by tools/build_closed_loop.py --program fib_cmp_auto"
        )
        out_src = ROOT / "modules" / "processor_fib_cmp_auto.source.json"
        tb = {
            "name": "proc_fib_cmp_auto",
            "description": (
                "Auto-scheduled Fibonacci with CMP-unit exit: the ROM comes from "
                "scheduler.py (list scheduler over a staged IR), verified by verifier.py. "
                f"Expect G={FIB_FINAL[0]}, H=M={FIB_FINAL[1]}, N={FIB_THRESHOLD}, "
                f"{TARGET_SLOT[0]}={DONE_MARKER}."
            ),
            **tb_common,
            "save_after_run": "claude_fib_cmp_auto_test",
            "timeline": [
                {
                    "name": "trace_first_passes",
                    "settle_ticks": 1,
                    "trace_ticks": 40,
                    "expect_probe": "mem_state",
                    "expect": {}
                },
                {
                    "name": "skip_to_completion",
                    "skip_ticks": 300,
                    "settle_ticks": 1,
                    "trace_ticks": 3,
                    "expect_probe": "mem_state",
                    "expect": fib_cmp_expectations()
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_fib_cmp_auto.tb.json"
    elif args.program == "zerow":
        src["name"] = "processor_zerow"
        src["description"] = (
            "Closed-loop processor, zero-write proof ROM (auto-scheduled): write 42 to "
            f"{TARGET_SLOT[0]}, canary G=7, clear it with an imm-0 write (v7.1 trigger "
            "fires off the matrix out2 select, so an empty value product clears the "
            "cell), canary H=9. Generated by tools/build_closed_loop.py --program zerow"
        )
        out_src = ROOT / "modules" / "processor_zerow.source.json"
        tb = {
            "name": "proc_zero_write",
            "description": (
                "ROM-level zero-write: the target cell is written to 42 then cleared by "
                "an instruction whose value immediate is 0; canary writes before and "
                f"after prove the machine kept running. Expect {TARGET_SLOT[0]}=0, "
                "G=7, H=9 in final memory."
            ),
            **tb_common,
            "save_after_run": "claude_zero_write_test",
            "timeline": [
                {
                    "name": "run_program",
                    "settle_ticks": 1,
                    "trace_ticks": 30,
                    "expect_probe": "mem_state",
                    "expect": zero_write_expectations()
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_zero_write.tb.json"
    else:
        pc_addr = address_of("signal-P")
        src["name"] = "processor_array"
        src["description"] = (
            "Closed-loop processor, offset-array-write ROM: port A parked on the PC slot "
            f"(addr {pc_addr}), each instruction routes a->write_addr (address := P) and "
            "const->write_value (one character). Consecutive instructions write consecutive "
            "addresses via the additive bus. Generated by tools/build_closed_loop.py --program array"
        )
        out_src = ROOT / "modules" / "processor_array.source.json"
        tb = {
            "name": "proc_array_write",
            "description": (
                "Offset array write: PC free-runs, port A reads the PC slot "
                f"(addr {pc_addr}) so a->write_addr makes the write address track P. "
                "ROM streams h,e,l,l,o via const->write_value on 5 consecutive instructions; "
                "the letters land on 5 consecutive memory addresses. Expectations pinned "
                "from the observed landing offset."
            ),
            **tb_common,
            "save_after_run": "claude_processor_test",
            "timeline": [
                {
                    "name": "run_program",
                    "drive": {"addr_a": {"signal-R": pc_addr}},
                    "settle_ticks": 1,
                    "trace_ticks": 34,
                    "expect_probe": "mem_state",
                    "expect": array_write_expectations()
                }
            ]
        }
        out_tb = ROOT / "testbenches" / "proc_array_write.tb.json"

    out_src.write_text(json.dumps(src, indent=2), encoding="utf-8")
    out_tb.write_text(json.dumps(tb, indent=2), encoding="utf-8")

    print(f"address space: {total_addresses()} slots")
    print("ROM program:")
    for addr, word in instructions:
        slot = signal_at(addr)
        print(f"  addr {addr} ({slot['name']}/{slot['quality']}): {word}")
    print(f"written: {out_src}")
    print(f"written: {out_tb}")


if __name__ == "__main__":
    main()
