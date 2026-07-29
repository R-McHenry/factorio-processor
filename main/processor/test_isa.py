#!/usr/bin/env python3
"""Self-tests for processor/isa.py (plain asserts, run with the venv python).

The live gate is v8_proc_fib_cmp / v8_proc_computed_jump in run_all.py; these
tests pin the scheduler's structural guarantees offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # main/ on the path

from signal_space import address_of
from processor.assembler import (assemble_program, assemble_word2,
                                 decode_word2, encode_word2)
from processor.isa import (ACCUMULATE_CELLS, CELL_TO_USE, IR8,
                        JUMP_ABS_DELAY_SLOTS, JUMP_REL_RESUME,
                        JUMP_REL_SHADOW, MMAP_B, MachineConfig, VEC_BUS,
                        WRITE_TO_CELL, check_pulse_widths, vec_control_settles,
                        PARK_A_TO_USE, ScheduleError, schedule8)
from processor.tools.build_fnet_v8_tests import fib_cmp  # noqa: F401  (import proves it builds)

H, G, M = address_of("signal-H"), address_of("signal-G"), address_of("signal-M")
N_ = address_of("signal-N")
I_, Q_ = address_of("signal-I"), address_of("signal-Q")
ACC = address_of("shape-vertical")


def fib_ir():
    ir = IR8()
    ir.write_imm(N_, 100, warm=True)
    ir.write_imm(H, 1)
    ir.park_b(I_)
    ir.barrier()
    ir.label("loop")
    ir.copy_b(H)
    ir.copy_a(H, G)
    ir.barrier()
    ir.copy_a(H, M)
    ir.barrier()
    ir.jump_if_zero(Q_, "loop")
    ir.write_imm(address_of("signal-heart"), 999)
    ir.halt()
    return ir


def test_fib_schedules_and_is_deterministic():
    a, sa = schedule8(fib_ir())
    b, _ = schedule8(fib_ir())
    assert a.rom_entries() == b.rom_entries()
    # loop re-enters at the body's MIN footprint slot (the park), not an anchor
    label = sa._label_slot("loop")
    body_ops = sa.ir.ops[sa.ir.labels["loop"]:]
    assert label == min(o.first_slot for o in body_ops if o.first_slot is not None)


def test_label_floors_past_prelude_footprints():
    """A prelude value slot captured inside a loop pairs the PREVIOUS pass's
    latch (measured: spin counter gained +2/pass) — labels must floor past it."""
    ir = IR8()
    ir.write_imm(H, 1)
    ir.barrier()
    ir.label("top")
    ir.add_imm(ACC, 1)
    ir.jump_rel("top")
    prog, sched = schedule8(ir)
    label = sched._label_slot("top")
    prelude = ir.ops[0]
    assert prelude.last_slot < label, (prelude.last_slot, label)


def test_jump_rel_geometry():
    ir = IR8()
    ir.write_imm(H, 1)
    ir.barrier()
    ir.label("top")
    ir.add_imm(ACC, 1)
    ir.jump_rel("top")
    prog, sched = schedule8(ir)
    jump = ir.ops[-1]
    label = sched._label_slot("top")
    offset = prog.slots[jump.slot]["imm"]
    assert jump.slot + offset + JUMP_REL_RESUME == label
    # delay slots stay empty
    assert not any(s in prog.slots
                   for s in range(jump.slot + 1, jump.slot + 1 + JUMP_REL_SHADOW))


def test_halt_is_self_jump_with_empty_shadow():
    ir = IR8()
    ir.write_imm(H, 1)
    ir.halt()
    prog, _ = schedule8(ir)
    halt = ir.ops[-1]
    assert prog.slots[halt.slot]["imm"] == -JUMP_REL_RESUME
    assert not any(s in prog.slots
                   for s in range(halt.slot + 1, halt.slot + 1 + JUMP_REL_SHADOW))


def test_park_reuse_and_spacing():
    """Two copies from the same cell share one park; the consumer sits at least
    PARK_A_TO_USE after it."""
    ir = IR8()
    ir.write_imm(H, 7)
    ir.barrier()
    ir.copy_a(H, G)
    ir.barrier()
    ir.copy_a(H, M)
    prog, sched = schedule8(ir)
    assert len(sched.parks_a) == 1
    park_slot, park_addr = sched.parks_a[0]
    assert park_addr == H
    for op in ir.ops[1:]:
        assert op.slot >= park_slot + PARK_A_TO_USE


def test_accumulate_guards():
    ir = IR8()
    try:
        ir.write_imm(ACC, 5)
    except ValueError as exc:
        assert "accumulate" in str(exc)
    else:
        raise AssertionError("write_imm on an accumulate cell must raise")
    try:
        ir.add_imm(H, 5)
    except ValueError as exc:
        assert "not an accumulate" in str(exc)
    else:
        raise AssertionError("add_imm on a plain cell must raise")


def test_copy_b_requires_park():
    ir = IR8()
    ir.copy_b(H)
    try:
        schedule8(ir)
    except ScheduleError as exc:
        assert "park_b" in str(exc)
    else:
        raise AssertionError("copy_b before park_b must raise")


def test_forward_jump_rejected():
    ir = IR8()
    ir.jump_rel("later")
    ir.label("later")
    ir.write_imm(H, 1)
    try:
        schedule8(ir)
    except ScheduleError:
        pass
    else:
        raise AssertionError("forward jump_rel must raise")


def test_pulse_imm_is_exactly_one_tick_wide():
    """The pulse must stay one tick wide with real ops crowding it.

    Two independent write_imm calls schedule adjacently in isolation but widen
    once surrounding stages' footprints compete for the slot — which is why
    pulse_imm places both writes atomically. A wide pulse is silent and
    expensive: v10 reads its vector write-select off a memory row, so three
    ticks means three accumulate copies of the vector bus.
    """
    ir = IR8()
    for i in range(4):
        ir.write_imm(H, i + 1)
        ir.barrier()
        ir.pulse_imm(G, 7)
        ir.barrier()
        ir.copy_a(H, M)
        ir.barrier()
    schedule8(ir)
    pulses = [op for op in ir.ops if op.kind == "pulse_imm"]
    assert len(pulses) == 4
    for i, op in enumerate(pulses):
        width = op.clear_value_slot - op.value_slot
        assert width == 1, f"pulse {i} widened to {width} ticks"
        assert op.value_slot == op.slot + 2


def test_pulse_imm_refuses_accumulate_cells():
    ir = IR8()
    try:
        ir.pulse_imm(ACC, 1)
    except ValueError as exc:
        assert "accumulate" in str(exc)
    else:
        raise AssertionError("pulse on an accumulate cell must raise — it adds twice")


def _vec_config():
    """A v10-shaped config: two control rows, one out-port, one bus reader.

    `wsel` deliberately carries a SPLIT depth (soonest 2, latest 4), the shape
    the real zone has once the op farm's square block is in play — a register
    write reaches the bus in 2 stages through a plain select and in 4 through
    square-then-subtract."""
    rsel, wsel, rlane_addr, rlane_data = 900, 901, 902, 903
    reach = {
        rsel:       {rlane_data: (3, 3), VEC_BUS: (1, 1)},
        wsel:       {rlane_data: (3, 6), VEC_BUS: (2, 4)},
        rlane_addr: {rlane_data: (3, 3)},
    }
    names = {rsel: "vec_rsel", wsel: "vec_wsel",
             rlane_addr: "vec_rlane_addr", rlane_data: "vec_rlane_data"}
    cfg = MachineConfig(
        pc_addr=address_of("signal-P"), mmap_b=MMAP_B,
        accumulate_cells=frozenset(), alu_by_addr={},
        namer=lambda a: names.get(a, f"mem[{a}]"),
        vec_reach=reach, vec_bus_reader=wsel,
    )
    return cfg, rsel, wsel, rlane_addr, rlane_data


def test_vector_read_waits_for_the_zone_to_propagate():
    """A vector out-port is combinational, so a read must clear the control's
    cell landing AND the zone's own depth."""
    cfg, rsel, _wsel, _ra, rlane_data = _vec_config()
    ir = IR8(cfg)
    ir.write_imm(rsel, 21)
    ir.barrier()
    ir.copy_a(rlane_data, H)
    schedule8(ir)
    write, read = ir.ops[0], ir.ops[1]
    assert read.slot >= write.value_slot + CELL_TO_USE + 3, (
        f"read at {read.slot} but control settles at {write.value_slot}")


def test_later_vector_write_cannot_reach_an_earlier_read():
    """The anti-dependency direction: the greedy scheduler leaves a wide gap
    behind a vector read, and a later control write must not drop into it and
    become visible at that read."""
    cfg, _rsel, _wsel, rlane_addr, rlane_data = _vec_config()
    ir = IR8(cfg)
    ir.write_imm(rlane_addr, 7)
    ir.barrier()
    ir.copy_a(rlane_data, H)      # must still see lane 7
    ir.barrier()
    ir.write_imm(rlane_addr, 8)
    ir.barrier()
    ir.copy_a(rlane_data, G)      # must see lane 8
    schedule8(ir)
    first_read, second_write, second_read = ir.ops[1], ir.ops[2], ir.ops[3]
    visible = second_write.value_slot + WRITE_TO_CELL + 3
    assert first_read.slot < visible, (
        f"read at {first_read.slot} would already see the lane-8 write "
        f"(visible from {visible})")
    assert second_read.slot >= visible


def test_register_write_waits_for_the_mux_to_present_its_source():
    """A `vec_wsel` write samples the shared bus at the instant it settles, so
    the mux must already be showing the intended source — otherwise a move
    silently captures whatever happens to be there."""
    cfg, rsel, wsel, _ra, _rd = _vec_config()
    ir = IR8(cfg)
    ir.write_imm(rsel, 21)
    ir.barrier()
    ir.pulse_imm(wsel, 1)
    schedule8(ir)
    steer, write = ir.ops[0], ir.ops[1]
    bus_read = write.value_slot + WRITE_TO_CELL
    assert bus_read >= steer.value_slot + WRITE_TO_CELL + 1, (
        f"bus read at {bus_read} precedes the mux settling")


def test_split_depth_uses_latest_forward_and_soonest_backward():
    """A control that reaches a port by paths of different depth must be read
    with the LATEST (or a deep source still shows the old frame) and guarded
    against with the SOONEST (contamination needs only one short path)."""
    cfg, _rsel, wsel, _ra, rlane_data = _vec_config()
    assert cfg.vec_stages(wsel, rlane_data) == 3          # soonest
    assert cfg.vec_stages_latest(wsel, rlane_data) == 6   # latest

    ir = IR8(cfg)
    ir.copy_a(rlane_data, G)      # must NOT see the register write below
    ir.barrier()
    ir.pulse_imm(wsel, 1)
    ir.barrier()
    ir.copy_a(rlane_data, H)      # must see it, on every path
    schedule8(ir)
    early, write, late = ir.ops[0], ir.ops[1], ir.ops[2]
    settle = write.value_slot + WRITE_TO_CELL
    assert early.slot < settle + 3, (
        f"read at {early.slot} is past the SOONEST visibility {settle + 3}")
    assert late.slot >= write.value_slot + CELL_TO_USE + 6, (
        f"read at {late.slot} does not clear the LATEST path "
        f"(control value slot {write.value_slot})")


def test_v8_config_has_no_vector_rules():
    """Every vector rule must be inert on a machine without a vector zone."""
    assert not IR8().config.vec_reach
    assert IR8().config.vec_bus_reader is None
    assert IR8().config.vec_ports == frozenset()


# ---------------------------------------------------------------------------
# the vector-zone decoder (plan/ROADMAP §3): control out of a second
# instruction word instead of three traversals of the scalar write path
# ---------------------------------------------------------------------------

def _decoder_config():
    """`_vec_config` plus the four decoder rows, i.e. a v10 machine that has
    the decoder. Same reach table — the zone downstream of the control plane
    is the SAME hardware, so what the decoder changes is only how fast a
    control gets there."""
    cfg, rsel, wsel, rlane_addr, rlane_data = _vec_config()
    ssel, erase = 904, 905
    reach = dict(cfg.vec_reach)
    reach[ssel] = reach[rsel]
    reach[erase] = reach[wsel]
    rows = {"vec_rsel": rsel, "vec_ssel": ssel, "vec_wsel": wsel,
            "vec_erase": erase, "vec_rlane_addr": rlane_addr,
            "vec_rlane_data": rlane_data,
            "vdec_rsel": 910, "vdec_ssel": 911, "vdec_wsel": 912,
            "vdec_erase": 913}
    names = {v: k for k, v in rows.items()}
    return (MachineConfig(pc_addr=address_of("signal-P"), mmap_b=MMAP_B,
                          accumulate_cells=frozenset(), alu_by_addr={},
                          namer=lambda a: names.get(a, f"mem[{a}]"),
                          vec_reach=reach, vec_bus_reader=wsel, vec_rows=rows),
            rows, rlane_data)


def test_decoder_move_is_one_instruction_and_no_rom_word():
    """The whole point: a move stops being a write_imm pair plus two pulses and
    becomes one slot's second word — which carries no route bits, so it does
    not even consume a ROM word."""
    cfg, rows, _rd = _decoder_config()
    ir = IR8(cfg)
    ir.vec_move(3, 27, src2=28)
    prog, _ = schedule8(ir)
    assert [op.kind for op in ir.ops] == ["vec_ctl"]
    move = ir.ops[0]
    assert prog.word2[move.slot] == encode_word2(rsel=27, ssel=28, wsel=3, erase=3)
    assert assemble_program(prog.rom_entries()) == []          # no ROM word at all
    assert assemble_word2(prog.rom_entries()) == [(move.slot, prog.word2[move.slot])]


def test_decoder_accumulate_drops_the_erase_field():
    cfg, _rows, _rd = _decoder_config()
    ir = IR8(cfg)
    ir.vec_move(9, 7, accumulate=True)
    prog, _ = schedule8(ir)
    assert decode_word2(prog.word2[ir.ops[0].slot]) == {
        "rsel": 7, "ssel": 7, "wsel": 9, "erase": 0}


def test_decoder_commit_lands_one_tick_after_its_own_selects():
    """The bank's rule is park, let the bus settle, THEN commit. Both halves
    ride one word here, so the alignment has to come out of the decode chains'
    depths — and if it ever stops holding, every move captures a stale frame."""
    cfg, rows, _rd = _decoder_config()
    ir = IR8(cfg)
    ir.vec_move(1, 27)
    schedule8(ir)
    settles = dict((addr, slot) for slot, addr in
                   vec_control_settles(ir.ops[0], cfg))
    assert settles[rows["vec_wsel"]] == settles[rows["vec_rsel"]] + 1
    assert settles[rows["vec_erase"]] == settles[rows["vec_wsel"]]


def test_decoder_move_waits_for_the_mux_to_present_its_source():
    """A dependent move still pays the zone's own depth — the decoder removes
    the write path, not the hardware. wsel reaches the bus in at most 4 stages
    (through the square block), so back-to-back moves sit 4 slots apart."""
    cfg, _rows, _rd = _decoder_config()
    ir = IR8(cfg)
    for _ in range(4):
        ir.vec_move(1, 27)
    schedule8(ir)
    slots = [op.slot for op in ir.ops]
    assert all(b - a == 4 for a, b in zip(slots, slots[1:])), slots


def test_decoder_park_persists_across_a_read():
    """R and S are LATCHED, so one park instruction steers the mux for as long
    as port A needs to walk the bus — the read still waits out the zone."""
    cfg, rows, rlane_data = _decoder_config()
    ir = IR8(cfg)
    ir.vec_select(21)
    ir.barrier()
    ir.write_imm(rows["vec_rlane_addr"], 7)
    ir.barrier()
    ir.copy_a(rlane_data, H)
    schedule8(ir)
    park, read = ir.ops[0], ir.ops[2]
    assert park.wsel == 0 and park.erase == 0
    # settle at slot + 2, then the zone's 3 stages, then the read's own slot
    assert read.slot >= park.slot + 2 + 3 + 1


def test_a_branch_never_strands_work_placed_before_it():
    """A vec_ctl takes no route bits and no write latch, so its only floor is
    the zone's timing — which routinely lands it LATER than a scalar op that
    follows it in the IR. Measured 2026-07-28: the mandelbrot loop's branch was
    placed at slot 57 while four of its own body moves sat at 66..83, so those
    moves ran once instead of once per pass."""
    cfg, rows, _rd = _decoder_config()
    ir = IR8(cfg)
    ir.write_imm(H, 1)
    ir.barrier()
    ir.label("top")
    for _ in range(4):
        ir.vec_move(1, 27)
    ir.jump_if_zero(Q_, "top")
    prog, sched = schedule8(ir)
    jump = next(op for op in ir.ops if op.kind == "jump_if_zero")
    body = [op for op in ir.ops if op.kind == "vec_ctl"]
    last_executed = jump.value_slot + JUMP_ABS_DELAY_SLOTS
    assert all(op.slot <= last_executed for op in body), (
        [op.slot for op in body], last_executed)


def test_memory_mapped_backend_is_still_reachable():
    """Both control planes are live hardware, and a v10-class design built
    without the decoder must still compile — the pulses (and the check that
    guards their width) are not dead code."""
    cfg, rows, _rd = _decoder_config()
    ir = IR8(cfg)
    ir.vec_move(1, 27, via="memory")
    schedule8(ir)
    check_pulse_widths(ir)
    kinds = [op.kind for op in ir.ops]
    assert kinds == ["write_imm", "write_imm", "pulse_imm", "pulse_imm"]
    # ...and a machine WITHOUT the decoder picks that backend by itself
    plain, _rsel, _wsel, _ra, _rd2 = _vec_config()
    assert not plain.has_vec_decoder


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} isa tests passed")
