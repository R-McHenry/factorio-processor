#!/usr/bin/env python3
"""Self-tests for vlang.py — the DSL, the pattern table, and the allocator
(plain asserts, run with the venv python).

All offline: no game, no server, no schedule. The live gate is
v10_proc_mandelbrot_dsl in run_all.py, which recompiles the mandelbrot kernel
from the DSL and asserts the SAME 38 expectations as the hand-written one.
These tests pin the three things that gate it (LANGUAGE.md §5 steps 2-4):

  2. the DAG shares common subexpressions — `zx*zx` in two statements is ONE
     node, which is the whole efficiency of the kernel;
  3. maximal-munch selection emits the expected (unit, operands, dest) triples;
  4. the allocator's move count for the mandelbrot body is <= 11, the
     hand-written figure.
"""
import json
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIN))
ROOT = MAIN / "processor"

from processor.isa import config_from_signals_map, schedule8  # noqa: E402
from processor.lang import (A_MAX, A_SUB, B_MUL, B_SQ, B_SQDIFF, B_SQG,  # noqa: E402
                   BCAST, COORD, LIVE_REGS, VS_DIV, VS_GT, VS_MOD, VS_MUL,
                   CompileError, Insn, Machine, VMove, dag_text, vmax, vmin)

_BASE = json.loads((ROOT / "modules" / "v10_processor.source.json")
                   .read_text(encoding="utf-8"))
_SIGNALS = _BASE["signals"]
CFG = config_from_signals_map(_SIGNALS)
ROWS = {k: int(v["address"]) for k, v in _SIGNALS.items()
        if "." not in k and "address" in v}

# the hand-written kernel's constants, so the tests compare like with like
S, HALF, W, DX, OX, DY, OY, ITERS = 360, 180, 64, 14, 720, 23, 450, 20
R2MAX = 4 * S * S


def machine():
    return Machine(CFG, ROWS, "test")


def mandelbrot_machine():
    """The acceptance-test kernel, exactly as tools/build_v10_tests.py writes
    it. Returns the compiled Machine."""
    m = machine()
    zx, zy = m.vec("zx"), m.vec("zy")
    e, esum = m.vec("e"), m.vec("esum")

    i = m.coord()
    y = (i // W).named("y")
    cy = (y * DY - OY).named("cy")
    x = (i % W).named("x")
    cx = (x * DX - OX).named("cx")

    with m.loop(ITERS):
        r2 = (zx * zx + zy * zy).named("r2")
        esc = (r2 > R2MAX).named("esc")
        e = vmax(e, esc)
        esum = esum + e
        zx, zy = ((zx * zx - zy * zy) // S + cx, (zx * zy) // HALF + cy)

    m.reduce_sum(e, 2200)
    m.reduce_sum(esum, 2201)
    m.read_lane(cx, 1248, 2202)
    colour = (esum * 12 * 0x010101).named("colour")
    m.read_lane(colour, 1248, 2204)
    m.park(colour)
    return m.compile()


def body_triples(m):
    blk = m.loop_block()
    return [it.triple() for it in blk.stream if isinstance(it, Insn)]


# ===========================================================================
# step 2 — the DAG
# ===========================================================================

def test_common_subexpression_is_one_node():
    """`zx*zx` in the escape test and in the zx update must be ONE node. The
    kernel's whole efficiency is that B_SQ, B_SQG, B_SQDIFF and B_MUL are all
    live off a single pair load, and a tree-walker would reload it four times."""
    m = machine()
    zx, zy = m.vec("zx"), m.vec("zy")
    r2 = zx * zx + zy * zy
    upd = (zx * zx - zy * zy) // S
    sq_in_r2 = r2.node.args[0]
    sq_in_upd = upd.node.args[0].args[0]
    assert sq_in_r2.op == "mul" and sq_in_r2.args[0] is zx.node
    assert sq_in_r2 is sq_in_upd, "zx*zx was rebuilt instead of shared"
    # ...and the whole DAG names it exactly once
    text = dag_text([r2, upd])
    assert sum(1 for line in text.splitlines() if line.startswith(
        f"{sq_in_r2.label()} =")) == 1, text


def test_commutative_operands_hash_cons_either_way():
    m = machine()
    a, b = m.vec("a"), m.vec("b")
    assert (a * b).node is (b * a).node
    assert (a + b).node is (b + a).node
    assert vmax(a, b).node is vmax(b, a).node
    assert (a - b).node is not (b - a).node          # sub is not commutative


def test_constant_subtraction_becomes_an_addend():
    """THE CORRECTNESS RULE, not an optimisation: a lane that computed to 0
    vanishes, and `each` against a fixed signal iterates only the first
    operand's lanes — so an offset must ride the SECOND selector as an addend.
    `a - k` is therefore an `add` of a negative constant at DAG-build time."""
    m = machine()
    a = m.vec("a")
    n = (a * DX - OX).node
    assert n.op == "add", "a - k must not survive as a subtraction"
    assert n.args[1].is_const and n.args[1].k == -OX
    assert (OX - a).node.op == "add"                  # k - a is a*-1 + k


def test_non_integer_operand_is_refused():
    m = machine()
    try:
        m.vec("a") * 1.5
    except CompileError as exc:
        assert "fixed point is explicit" in str(exc)
    else:
        raise AssertionError("a float operand should not compile")


# ===========================================================================
# step 3 — the pattern table and maximal munch
# ===========================================================================

def test_mandelbrot_body_selects_the_expected_units():
    """Hand-written expectations for the loop body, in order. Each entry is
    the (unit, operands, dest) triple of LANGUAGE.md §5 step 3."""
    m = mandelbrot_machine()
    assert body_triples(m) == [
        # a*a + b*b -> B_SQ || B_SQG off ONE pair load; the add is free
        ("B_SQ|B_SQG", ("V2=zx", "V3=zy"), "r2"),
        (f"VS_GT[{R2MAX}]", ("V0=r2",), "esc"),
        ("A_MAX", ("V0=e", "V1=esc"), "e'"),
        # x = x + y compiles to an ACCUMULATE, not a dual selection
        ("e'+=", (), "esum'"),
        # a*a - b*b -> B_SQDIFF, same pair, no reload
        ("B_SQDIFF", ("V2=zx", "V3=zy"), "t26"),
        (f"VS_DIV|cx[{S}]", ("V0=t26",), "zx'"),
        ("B_MUL", ("V2=zx", "V3=zy"), "t30"),
        (f"VS_DIV|cy[{HALF}]", ("V0=t30",), "zy'"),
    ]


def test_grid_seed_uses_the_lane_resurrecting_shape():
    """`x*k1 + k2` must land as VS_MUL || BCAST — the vec-scal unit and the
    constant frame summed on the write bus — not as a multiply followed by a
    VS_SUB, which could never reach the lanes the modulo dropped."""
    m = mandelbrot_machine()
    seed = [it.triple() for it in m.blocks[0].stream if isinstance(it, Insn)]
    assert seed == [
        (f"VS_DIV[{W}]", ("V0=COORD",), "y"),
        (f"VS_MUL|BCAST[{DY}]{{{-OY}}}", ("V0=y",), "cy"),
        (f"VS_MOD[{W}]", ("V0=COORD",), "x"),
        (f"VS_MUL|BCAST[{DX}]{{{-OX}}}", ("V0=x",), "cx"),
    ]


def test_bigger_patterns_win():
    """Maximal munch: `a*a` alone is B_SQ, but inside `a*a - b*b` it is
    absorbed into B_SQDIFF and never materialised on its own."""
    m = machine()
    a, b = m.vec("a"), m.vec("b")
    (a * a - b * b).named("d")
    m.read_lane(a * a - b * b, 1, 900)
    m.compile()
    units = [it.triple()[0] for it in m.blocks[0].stream if isinstance(it, Insn)]
    assert units == ["B_SQDIFF"], units

    m2 = machine()
    c = m2.vec("c")
    m2.read_lane(c * c, 1, 900)
    m2.compile()
    assert [it.triple()[0] for it in m2.blocks[0].stream
            if isinstance(it, Insn)] == ["B_SQ"]


def test_vector_add_costs_no_instruction():
    """Two selections sum on the write bus, so `a + b` is a dual selection with
    NO unit and no operand placement at all."""
    m = machine()
    a, b = m.vec("a"), m.vec("b")
    m.read_lane(a + b, 1, 900)
    m.compile()
    insns = [it for it in m.blocks[0].stream if isinstance(it, Insn)]
    assert len(insns) == 1 and insns[0].units() == () and not insns[0].reads
    assert len(m.moves(0)) == 1


def test_scalar_addend_is_free_too():
    """`a + k` rides BCAST on the second selector: one move, no operand
    placement, and every lane present in the result."""
    m = machine()
    a = m.vec("a")
    m.read_lane(a + 7, 1, 900)
    m.compile()
    insn = next(it for it in m.blocks[0].stream if isinstance(it, Insn))
    assert insn.sel2 == ("src", BCAST) and insn.bcast2 == 7
    assert not insn.reads and len(m.moves(0)) == 1


def test_immediate_is_range_checked():
    """20 bits SIGNED is what caps a fixed-point scale, and a silently
    truncated ROM word is the worst possible way to find that out."""
    m = machine()
    a = m.vec("a")
    try:
        (a > 4 * 400 * 400)                     # S = 400 needs 640000
    except CompileError as exc:
        assert "20-bit signed" in str(exc)
    else:
        raise AssertionError("an out-of-range broadcast should not compile")


def test_vector_comparison_is_refused_with_advice():
    m = machine()
    a, b = m.vec("a"), m.vec("b")
    m.read_lane(a > b, 1, 900)
    try:
        m.compile()
    except CompileError as exc:
        assert "vmax/vmin" in str(exc)
    else:
        raise AssertionError("vec > vec has no unit and must not compile")


def test_earlier_block_values_are_opaque():
    """A value from an earlier block exists only as a REGISTER, so no pattern
    may look inside its defining expression. Getting this wrong rebuilt
    `cx = x*DX - OX` as the outer half of the zx update and then asked for a
    register holding `x*DX`, which nothing had computed."""
    m = mandelbrot_machine()
    zx_update = next(t for t in body_triples(m) if t[2] == "zx'")
    assert zx_update[0] == f"VS_DIV|cx[{S}]"
    assert zx_update[1] == ("V0=t26",), "the matcher munched into cx"


# ===========================================================================
# step 4 — register allocation
# ===========================================================================

def test_mandelbrot_body_is_at_most_eleven_moves():
    """The acceptance figure of LANGUAGE.md §6: the compiled loop body must be
    no worse than the hand-written 11 moves per pass. If it is, the VREG0-reuse
    peephole of §4c is missing."""
    m = mandelbrot_machine()
    moves = m.moves(m.loop_block().index)
    assert len(moves) <= 11, "\n".join(mv.describe(m.resolve) for mv in moves)
    assert len(moves) == 11


def test_one_pair_load_feeds_four_units():
    """The point of the shared DAG: VREG2/VREG3 are loaded ONCE and B_SQ,
    B_SQG, B_SQDIFF and B_MUL all read them across four instructions."""
    m = mandelbrot_machine()
    moves = m.moves(m.loop_block().index)
    pair_loads = [mv for mv in moves if mv.dest in (("slot", 2), ("slot", 3))]
    assert len(pair_loads) == 2, [mv.describe(m.resolve) for mv in pair_loads]


def test_results_are_computed_straight_into_their_operand_slot():
    """The peephole that pays: `r2` is used only as VS_GT's VREG0 operand, so
    it is COMPUTED into VREG0 and the move disappears."""
    m = mandelbrot_machine()
    blk = m.loop_block()
    r2 = next(it for it in blk.stream if isinstance(it, Insn) and it.value.label() == "r2")
    esc = next(it for it in blk.stream if isinstance(it, Insn) and it.value.label() == "esc")
    assert r2.dest == ("slot", 0)
    assert esc.dest == ("slot", 1)          # A_MAX wants it in VREG1


def test_destination_never_shadows_its_own_operand():
    """`vec_move` erases the destination before pulsing W, so `V0 <- VS_DIV`
    would read an emptied VREG0 and commit zero. The seed's `y` therefore goes
    to a live register even though its only use is VREG0."""
    m = mandelbrot_machine()
    y = next(it for it in m.blocks[0].stream
             if isinstance(it, Insn) and it.value.label() == "y")
    assert y.dest[0] == "stor", "y must not be computed into its own operand slot"
    assert y.dest[1].reg in LIVE_REGS


def test_seed_temporary_is_recycled():
    """Linear scan should notice the seed's intermediate dies before its own
    result is written, so `y` and `cy` share a register — one fewer than the
    hand-written kernel, which kept a dedicated temp."""
    m = mandelbrot_machine()
    regs = {st.reg for uid, st in m._storage.items() if st.reg is not None}
    assert len(regs) <= 7, sorted(regs)


def test_loop_carried_update_writes_the_variable_register():
    """A carried value shares ONE storage with the variable it updates, so the
    copy over the back edge costs nothing."""
    m = mandelbrot_machine()
    blk = m.loop_block()
    zx_new = next(it for it in blk.stream
                  if isinstance(it, Insn) and it.value.label() == "zx'")
    zx_load = next(mv for mv in m.moves(blk.index) if mv.dest == ("slot", 2))
    assert zx_new.dest[0] == "stor"
    assert zx_new.dest[1] is zx_load.sel1[1], "zx' did not land in zx's register"


def test_self_add_becomes_an_accumulate():
    """`esum = esum + e` cannot be a dual selection — vec_move erases esum
    first, so the bus would show 0 + e. It is a write with no erase, which is
    how a per-lane counter works with no adder anywhere in the machine."""
    m = mandelbrot_machine()
    acc = next(it for it in m.loop_block().stream
               if isinstance(it, Insn) and it.value.label() == "esum'")
    assert acc.accumulate and acc.sel2 is None
    assert not acc.reads, "an accumulate needs no operand placement"


def test_scratch_state_does_not_cross_the_back_edge():
    """VREG2 physically holds the PREVIOUS pass's zx at the top of the body, so
    the pair load must be re-emitted every pass or every iteration but the
    first reads stale lanes."""
    m = mandelbrot_machine()
    first = m.moves(m.loop_block().index)[0]
    assert first.dest == ("slot", 2)


def test_running_out_of_registers_is_a_hard_error():
    """Spilling goes through vec_read_lane one lane at a time, so it is a loud
    failure naming the variables rather than a silent catastrophe."""
    m = machine()
    live = [m.vec(f"v{n}") for n in range(len(LIVE_REGS) + 2)]
    acc = live[0] + live[1]
    for v in live[2:]:
        acc = acc + v                    # every variable live at once
    m.read_lane(acc, 1, 900)
    for v in live:
        m.read_lane(v, 1, 901)
    try:
        m.compile()
    except CompileError as exc:
        assert "out of vector registers" in str(exc) and "v0" in str(exc)
    else:
        raise AssertionError("ten live variables must not fit eight registers")


# ===========================================================================
# end to end (still offline): the DSL reaches a schedule
# ===========================================================================

def test_mandelbrot_compiles_to_a_schedule():
    """Steps 2-4 hand off to IR8 and schedule8 unchanged — no timing code in
    the compiler at all."""
    m = mandelbrot_machine()
    ir = m.emit(label="iter")
    prog, sched = schedule8(ir, name="test_mandelbrot_dsl")
    assert prog.end() > 300 and "iter" in ir.labels
    assert sched._label_slot("iter") < prog.end()
    assert [op.kind for op in ir.ops[-1:]] == ["halt"]
    # deterministic: the same source compiles to the same ROM
    again = mandelbrot_machine().emit(label="iter")
    assert schedule8(again)[0].rom_entries() == prog.rom_entries()


def test_dsl_bench_matches_the_hand_written_expectations():
    """The acceptance test, minus the game: both builders must emit the SAME 38
    expectations, from the same reference model."""
    from processor.tools.build_v10_tests import BENCHES  # noqa: E402

    lane_count = 2451
    hand = BENCHES["mandelbrot"](CFG, ROWS, lane_count)[1][0]["expect"]
    dsl = BENCHES["mandelbrot_dsl"](CFG, ROWS, lane_count)[1][0]["expect"]
    assert hand == dsl and len(dsl) == 38


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} lang tests passed")
