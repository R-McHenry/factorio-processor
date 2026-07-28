#!/usr/bin/env python3
"""Self-tests for scheduler.py (plain asserts, run with the venv python).

Phase 2/3 acceptance (SCHEDULER_PLAN.md): auto-scheduled fib_cmp verifies
clean with no literal slot numbers in its source, slot count within ~20% of
the hand version; behavioral equality is gated by the live suite
(proc_fib_cmp_auto in run_all.py).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from signal_space import address_of
from scheduler import IR, ScheduleError, schedule
from build_closed_loop import fib_cmp_auto_ir, fib_cmp_program
from verifier import verify

HAND_END = 38   # hand-scheduled fib_cmp end slot


def test_fib_cmp_auto_schedules_clean():
    ir = fib_cmp_auto_ir()
    prog, report = schedule(ir, name="fib_cmp_auto")
    assert report.ok and not report.warnings, report.render()
    assert prog.end() <= HAND_END * 1.2, f"end slot {prog.end()} vs hand {HAND_END}"
    # same write intents (addresses + immediate values) in the same order as the
    # hand-built program; port-sourced values are None in both manifests
    hand = verify(fib_cmp_program(), name="fib_cmp")
    assert [w.addr_expr for w in report.manifest] == [w.addr_expr for w in hand.manifest]
    jump_slots = {j["value_slot"] for j in report.jumps} | {j["value_slot"] for j in hand.jumps}
    assert [w.value for w in report.manifest if w.value_slot not in jump_slots] == \
           [w.value for w in hand.manifest if w.value_slot not in jump_slots]
    # loop closes on itself: jump dest + 1 == the anchor slot of the first loop op
    [jump] = report.jumps
    assert jump["conditional"]
    first_loop_op = ir.ops[ir.labels["loop"]]
    assert jump["dest"] + 1 == first_loop_op.slot


def test_deterministic():
    a, _ = schedule(fib_cmp_auto_ir())
    b, _ = schedule(fib_cmp_auto_ir())
    assert a.rom_entries() == b.rom_entries()


def test_zero_write_schedules():
    """write_imm(_, 0) is legal since v7.1: clear a cell, then keep writing."""
    G, H, W = address_of("signal-G"), address_of("signal-H"), address_of("signal-heart")
    ir = IR()
    ir.write_imm(W, 42, warm=True)
    ir.barrier()
    ir.write_imm(G, 7)
    ir.barrier()
    ir.write_imm(W, 0)
    ir.barrier()
    ir.write_imm(H, 9)
    prog, report = schedule(ir)
    assert report.ok, report.render()
    zero = [w for w in report.manifest if w.value == 0]
    assert len(zero) == 1 and zero[0].addr == W


def test_forward_jump_rejected():
    ir = IR()
    ir.write_imm(address_of("signal-H"), 1)
    ir.barrier()
    ir.jump_if_zero(address_of("signal-Q"), "later")
    ir.label("later")
    ir.write_imm(address_of("signal-G"), 5)
    try:
        schedule(ir)
    except ScheduleError as exc:
        assert "backward" in str(exc)
    else:
        raise AssertionError("forward jump must raise")


def test_same_stage_write_then_read_infeasible():
    H, G = address_of("signal-H"), address_of("signal-G")
    ir = IR()
    ir.write_imm(H, 1)
    ir.barrier()
    ir.write_imm(H, 7)      # placed first, lands early
    ir.copy(H, G)           # must sample pre-stage H -> before that write lands
    try:
        schedule(ir)
    except ScheduleError as exc:
        assert "reorder" in str(exc)
    else:
        raise AssertionError("read after same-stage write to the same cell must raise")


def test_read_before_write_same_stage_ok():
    """The legal ordering of the same stage: read first, then overwrite."""
    H, G = address_of("signal-H"), address_of("signal-G")
    ir = IR()
    ir.write_imm(H, 1)
    ir.barrier()
    ir.copy(H, G)           # reads old H
    ir.write_imm(H, 7)      # lands after the read sampled
    prog, report = schedule(ir)
    assert report.ok, report.render()
    reads = [op for op in ir.ops if op.kind == "copy"]
    writes = [op for op in ir.ops if op.kind == "write_imm" and op.value == 7]
    assert writes[0].value_slot >= reads[0].slot + 1


def test_port_forcing_and_round_robin():
    H, G, D = address_of("signal-H"), address_of("signal-G"), address_of("signal-D")
    ir = IR()
    ir.write_imm(H, 1)
    ir.barrier()
    ir.copy(H, G, port="b")
    ir.copy(H, D)
    schedule(ir)
    assert ir.ops[1].port == "b"
    assert ir.ops[2].port in ("a", "b")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} scheduler tests passed")
