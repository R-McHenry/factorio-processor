#!/usr/bin/env python3
"""Self-tests for verifier.py (plain asserts, style of assembler.py's self-test).

Run: .\\.venv\\Scripts\\python.exe test_verifier.py

The key acceptance case from SCHEDULER_PLAN.md Phase 1: the verifier must pass
the current hand-built fib/fib_cmp unchanged and must flag the original
"D <- I races G's update, reads I=H+H" bug when reintroduced.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))

from assembler import Program
from signal_space import address_of
from verifier import verify
from build_closed_loop import fib_program, fib_cmp_program, FIB_THRESHOLD, DONE_MARKER


def codes(report, severity=None):
    return {f.code for f in report.findings
            if severity is None or f.severity == severity}


def test_hand_programs_pass():
    for builder in (fib_program, fib_cmp_program):
        rep = verify(builder(), name=builder.__name__)
        assert rep.ok, f"{builder.__name__} should verify clean:\n{rep.render()}"
        assert not rep.warnings, f"{builder.__name__} unexpectedly warns:\n{rep.render()}"
        # one conditional jump back to slot 9, fetch resuming at the done marker
        [jump] = rep.jumps
        assert jump["conditional"] and jump["dest"] == 9 and jump["resume_time"] == 36
        # done marker present in the manifest
        assert any(w.value == DONE_MARKER for w in rep.manifest)


def buggy_fib_program() -> Program:
    """fib with the original race reintroduced: D <- I (ADD output) at LOOP+8
    instead of D <- H. G's cell settles ~2 ticks before the sample while H sits
    exactly at the ready boundary -> measured live as reading I = H+H."""
    G, H, D, E, F_ = (address_of(f"signal-{s}") for s in "GHDEF")
    I_ = address_of("signal-I")
    P = address_of("signal-P")
    W = address_of("signal-heart")
    LOOP = 10

    p = Program()
    p.warm_latch(1, E)
    p.at(5, ["const->write_value"], FIB_THRESHOLD, "seed E")
    p.write_imm(4, H, 1, "seed H=1")
    p.copy(LOOP + 0, H, G, port="a", why="G <- H")
    p.copy(LOOP + 2, I_, H, port="b", why="H <- I")
    d_value = p.copy(LOOP + 8, I_, D, port="b", why="D <- I (the bug)")
    f_read = Program.alu_ready_pulse(d_value, op_latency=1)
    jump_value = p.jump_offset(f_read, F_, P, LOOP - 1, why="loop while F == 0")
    p.write_imm(jump_value + Program.JUMP_DELAY_SLOTS + 1, W, DONE_MARKER, why="done")
    return p


def test_flags_reintroduced_ihh_bug():
    rep = verify(buggy_fib_program(), name="fib_buggy")
    assert not rep.ok, "reintroduced I=H+H bug must fail verification"
    transients = [f for f in rep.errors if f.code == "alu-read-transient"]
    assert transients, f"expected alu-read-transient error:\n{rep.render()}"
    assert "signal-I" in transients[0].message and transients[0].slot == 18


def test_alu_read_too_early():
    H = address_of("signal-H")
    G = address_of("signal-G")
    I_ = address_of("signal-I")
    p = Program()
    p.write_imm(1, G, 5, "seed G")
    p.write_imm(2, H, 7, "seed H")
    # value slot 4 -> earliest safe I pulse is 4+2+1=7; pulse at 5 is too early
    p.copy(5, I_, H, port="a", why="read I too early")
    rep = verify(p)
    assert "alu-read-early" in codes(rep, "error"), rep.render()


def test_port_consumer_without_pulse():
    p = Program()
    p.at(1, ["const->write_addr"], address_of("signal-G"), "addr")
    p.at(3, ["a->write_value"], why="consume port A with no pulse at slot 0")
    rep = verify(p)
    assert "port-no-pulse" in codes(rep, "error"), rep.render()
    # parking port A makes the same program legal (array mode)
    rep = verify(p, parked_ports={"a": address_of("signal-P")})
    assert "port-no-pulse" not in codes(rep), rep.render()


def test_unused_port_pulse_warns():
    p = Program()
    p.at(1, ["const->addr_a"], address_of("signal-G"), "pulse nobody consumes")
    rep = verify(p)
    assert "port-unused" in codes(rep, "warning"), rep.render()


def test_value_with_no_address():
    p = Program()
    p.at(1, ["const->write_value"], 42, "value before any address")
    rep = verify(p)
    assert "value-no-addr" in codes(rep, "error"), rep.render()


def test_zero_write_allowed_missing_imm_rejected():
    # explicit 0 is a legal zero-write since v7.1 (trigger fires off the matrix select)
    p = Program()
    p.warm_latch(1, address_of("signal-G"))
    p.at(5, ["const->write_value"], 0, "clear G")
    rep = verify(p)
    assert rep.ok, rep.render()
    assert rep.manifest[-1].value == 0
    # ...but a const value slot with no imm ever set is a forgotten immediate
    p2 = Program()
    p2.warm_latch(1, address_of("signal-G"))
    p2.at(5, ["const->write_value"], why="forgot the imm")
    rep2 = verify(p2)
    assert "value-missing-imm" in codes(rep2, "error"), rep2.render()


def test_latch_collision():
    p = Program()
    # const addr at 1 pairs at 3; port addr at 2 pairs at 3 -> collision
    p.at(1, ["const->write_addr"], address_of("signal-G"), "const addr")
    p.at(2, ["b->write_addr"], why="port addr colliding in the latch")
    rep = verify(p, parked_ports={"b": address_of("signal-P")})
    assert "latch-collision" in codes(rep, "error"), rep.render()


def test_cold_latch_warns():
    p = Program()
    p.write_imm(1, address_of("signal-G"), 5, "no warm pulses at all")
    rep = verify(p)
    assert "cold-latch" in codes(rep, "warning"), rep.render()


def test_stale_cell_read_warns():
    H = address_of("signal-H")
    p = Program()
    p.warm_latch(1, H)
    p.at(5, ["const->write_value"], 7, "H=7 lands ~10")
    p.at(6, ["const->addr_a"], H, "pulse samples at 9, before the write lands")
    p.at(9, ["a->write_value"], why="consume")
    p.at(7, ["const->write_addr"], address_of("signal-G"), "addr for the copy")
    rep = verify(p)
    assert "read-stale" in codes(rep, "warning"), rep.render()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} verifier tests passed")
