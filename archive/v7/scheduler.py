#!/usr/bin/env python3
"""List scheduler for the switch-matrix processor (Phases 2-3 of SCHEDULER_PLAN.md).

Programs are written as dependency-declared ops in an IR; the scheduler assigns
slot numbers, ports, and jump targets, honouring every measured pipeline rule
(the same model verifier.py checks). The output is an assembler.Program, so the
existing encode -> ROM path is unchanged.

Execution model of the IR:
- Ops live in *stages*. Within a stage, reads sample the PRE-stage machine
  state (register/parallel semantics): the scheduler keeps each read's sample
  ahead of every same-stage write that could disturb it (anti-dependencies).
  `barrier()` commits the stage's writes; later stages read them (RAW edges).
- `label(name)` marks the next op as a jump target. `jump_if_zero(flag, name)`
  writes `label_slot - 1` to the PC slot offset by mem[flag] — jump taken while
  flag == 0, fall-through once flag >= 1 (the measured DIV/CMP steering trick).
  A jump ends its stage and floors every later op past the 6 delay slots.

Timing constraints applied (all from processor.design.md, verified live):
- RAW cell:     read pulse >= writer value slot + 3 (measured working margin)
- RAW ALU out:  read pulse >= writer value slot + 2 + latency (alu_ready_pulse),
                bumped off the ready boundary if another operand of the same op
                settles within the coherence window (the fib I=H+H transient)
- anti (same stage): a write's value slot must land after every earlier
                same-stage read of that cell/operand has sampled
- WAW:          value slots to one cell keep IR order
- write-address latch: no other address may enter the latch between an op's
                address arrival and its value slot (both directions)
- ports:        pulse at p is consumed at exactly p+3; slot route/imm merging
                is checked against the whole footprint
- jumps:        value slot v takes effect at fetch v+7; delay slots stay empty

schedule() returns the filled Program and the verifier Report (already gated).
"""
from dataclasses import dataclass, field

from assembler import Program
from signal_space import address_of, signal_at
from verifier import (ALU_OUTPUT, WRITE_TO_CELL, COHERENCE_WINDOW,
                      PORT_READ_TO_USE, JUMP_DELAY_SLOTS, verify)

CELL_READ_MARGIN = 3   # pulse >= v+3 measured working (fib); v+2 is unproven


def _name(addr: int) -> str:
    s = signal_at(addr)
    return s["name"] if s["quality"] == "normal" else f"{s['name']}~{s['quality']}"


@dataclass
class Op:
    kind: str                    # write_imm | copy | jump_if_zero
    stage: int
    src: int | None = None       # read address (copy src / jump flag)
    dst: int | None = None       # write address
    value: int | None = None     # immediate value / jump dest (resolved late)
    port: str | None = None      # forced port, or None = auto
    warm: bool = False           # write_imm: prepend 2 latch-warming pulses
    target: str | None = None    # jump label
    after: list = field(default_factory=list)
    # filled by the scheduler:
    slot: int | None = None      # anchor slot
    value_slot: int | None = None

    def describe(self) -> str:
        if self.kind == "write_imm":
            return f"{_name(self.dst)} <- {self.value}" + (" (warm)" if self.warm else "")
        if self.kind == "copy":
            return f"{_name(self.dst)} <- {_name(self.src)}"
        return f"jump to '{self.target}' while {_name(self.src)} == 0"


class IR:
    """Program builder: ops + stages + labels, no slot numbers anywhere."""

    def __init__(self):
        self.ops: list[Op] = []
        self.stage = 0
        self.labels: dict[str, int] = {}   # name -> index of the op it precedes

    def barrier(self) -> None:
        """Commit this stage's writes; later reads observe them."""
        self.stage += 1

    def label(self, name: str) -> None:
        if name in self.labels:
            raise ValueError(f"duplicate label '{name}'")
        self.labels[name] = len(self.ops)

    def write_imm(self, addr: int, value: int, warm: bool = False, after=()) -> Op:
        """value 0 is legal since v7.1: the write trigger fires off the matrix
        out2 select, not the data, so an empty value product clears the cell."""
        return self._add(Op("write_imm", self.stage, dst=addr, value=value,
                            warm=warm, after=list(after)))

    def copy(self, src: int, dst: int, port: str | None = None, after=()) -> Op:
        return self._add(Op("copy", self.stage, src=src, dst=dst, port=port,
                            after=list(after)))

    def jump_if_zero(self, flag: int, target: str, port: str | None = None, after=()) -> Op:
        """Loop while mem[flag] == 0 (backward jumps only for now)."""
        op = self._add(Op("jump_if_zero", self.stage, src=flag, target=target,
                          port=port, after=list(after)))
        self.stage += 1   # a jump ends its stage
        return op

    def _add(self, op: Op) -> Op:
        self.ops.append(op)
        return op


class ScheduleError(Exception):
    pass


class _Scheduler:
    def __init__(self, ir: IR, name: str):
        self.ir = ir
        self.name = name
        self.prog = Program()
        self.committed: dict[int, int] = {}    # cell -> latest committed value slot
        self.pending: dict[int, int] = {}      # same, for the open stage
        self.stage_reads: list[tuple[int, int]] = []   # (cell read, pulse slot)
        self.latch_pairs: list[tuple[int, int]] = []   # (pair_time, value_slot)
        self.latch_events: list[int] = []              # pair times
        self.current_stage = 0
        self.floor = 1                        # first legal anchor (jump shadows)
        self.last_port = "b"                  # round-robin start -> "a" first
        self.label_slots: dict[str, int] = {}

    # -- footprints ---------------------------------------------------------
    def _footprint(self, op: Op, s: int, port: str | None):
        """[(slot, routes, imm)], latch events (pair times), pair, pulse, value slot."""
        if op.kind == "write_imm":
            if op.warm:
                cells = [(s + i, ["const->write_addr"], op.dst) for i in range(3)]
                cells.append((s + 4, ["const->write_value"], op.value))
                return cells, [s + 2, s + 3, s + 4], (s + 4, s + 4), None, s + 4
            cells = [(s, ["const->write_addr"], op.dst),
                     (s + 2, ["const->write_value"], op.value)]
            return cells, [s + 2], (s + 2, s + 2), None, s + 2
        if op.kind == "copy":
            cells = [(s, [f"const->addr_{port}"], op.src),
                     (s + 1, ["const->write_addr"], op.dst),
                     (s + 3, [f"{port}->write_value"], None)]
            return cells, [s + 3], (s + 3, s + 3), s, s + 3
        # jump_if_zero: pulse reads the flag, combined port+const address = P+flag,
        # value = dest (resolved from the label before placement)
        pc = address_of("signal-P")
        cells = [(s, [f"const->addr_{port}"], op.src),
                 (s + 3, [f"{port}->write_addr", "const->write_addr"], pc),
                 (s + 5, ["const->write_value"], op.value)]
        return cells, [s + 5], (s + 5, s + 5), s, s + 5

    # -- feasibility checks --------------------------------------------------
    def _routes_fit(self, cells) -> bool:
        for slot, routes, imm in cells:
            entry = self.prog.slots.get(slot)
            if entry is None:
                continue
            if any(r in entry["routes"] for r in routes):
                return False
            if imm is not None and entry["imm"] is not None and entry["imm"] != imm:
                return False
        return True

    def _latch_fits(self, events, pair) -> bool:
        for pt in events:
            if pt in self.latch_events:
                return False
        pt_new, v_new = pair
        for pt_e, v_e in self.latch_pairs:
            if pt_e < pt_new <= v_e:      # my address breaks an existing pairing
                return False
            if pt_new < pt_e <= v_new:    # an existing address breaks mine
                return False
        # intermediate warm events may not break existing pairs either
        for pt in events[:-1]:
            for pt_e, v_e in self.latch_pairs:
                if pt_e < pt <= v_e:
                    return False
        return True

    def _read_bounds(self, op: Op, addr: int):
        """(earliest pulse, latest pulse) for reading `addr` this stage."""
        lo, hi = self.floor, None
        sig = signal_at(addr)
        if sig["quality"] == "normal" and sig["name"] in ALU_OUTPUT:
            spec = ALU_OUTPUT[sig["name"]]
            for operand in spec["operands"]:
                cell = address_of(operand)
                v = self.committed.get(cell)
                if v is not None:
                    lo = max(lo, Program.alu_ready_pulse(v, spec["latency"]))
                pv = self.pending.get(cell)
                if pv is not None:   # same-stage write: must sample before it lands
                    hi = pv - 1 if hi is None else min(hi, pv - 1)
        else:
            v = self.committed.get(addr)
            if v is not None:
                lo = max(lo, v + CELL_READ_MARGIN)
            pv = self.pending.get(addr)
            if pv is not None:
                hi = pv - 1 if hi is None else min(hi, pv - 1)
        return lo, hi

    def _transient_at(self, op: Op, pulse: int) -> bool:
        """Mirror of the verifier's alu-read-transient rule."""
        sig = signal_at(op.src)
        if not (sig["quality"] == "normal" and sig["name"] in ALU_OUTPUT):
            return False
        spec = ALU_OUTPUT[sig["name"]]
        lat = spec["latency"]
        sample = pulse + PORT_READ_TO_USE
        for operand in spec["operands"]:
            v = self.committed.get(address_of(operand))
            if v is None or pulse != Program.alu_ready_pulse(v, lat):
                continue
            for other in spec["operands"]:
                if other == operand:
                    continue
                v2 = self.committed.get(address_of(other))
                if v2 is not None and abs(sample - (v2 + WRITE_TO_CELL + lat)) < COHERENCE_WINDOW:
                    return True
        return False

    def _write_floor(self, op: Op) -> int:
        """Earliest legal value slot for a write to op.dst."""
        v_min = 0
        last = max(self.committed.get(op.dst, 0), self.pending.get(op.dst, 0))
        v_min = max(v_min, last + 1)                        # WAW keeps IR order
        for cell, pulse in self.stage_reads:                # anti: reads sample first
            if cell == op.dst:
                v_min = max(v_min, pulse + 1)
            sig = signal_at(cell)
            if sig["quality"] == "normal" and sig["name"] in ALU_OUTPUT:
                operands = [address_of(o) for o in ALU_OUTPUT[sig["name"]]["operands"]]
                if op.dst in operands:
                    v_min = max(v_min, pulse + 1)
        return v_min

    # -- placement -----------------------------------------------------------
    def _place(self, op: Op, index: int) -> None:
        if op.stage != self.current_stage:
            self.committed.update(self.pending)
            self.pending.clear()
            self.stage_reads.clear()
            self.current_stage = op.stage

        if op.kind == "jump_if_zero":
            if op.target not in self.label_slots:
                raise ScheduleError(f"jump target '{op.target}' not yet scheduled "
                                    "(only backward jumps are supported)")
            op.value = self.label_slots[op.target] - 1

        lo_pulse, hi_pulse = (self._read_bounds(op, op.src) if op.src is not None
                              else (self.floor, None))
        # anchor offsets: pulse == anchor for reads; value slot offset per kind
        value_off = {"write_imm": 4 if op.warm else 2, "copy": 3, "jump_if_zero": 5}[op.kind]
        lo = max(self.floor, lo_pulse,
                 self._write_floor(op) - value_off if op.dst is not None or op.kind == "jump_if_zero" else 1)
        for dep in op.after:
            if dep.value_slot is None:
                raise ScheduleError(f"op depends on an unscheduled op: {op.describe()}")
            lo = max(lo, dep.value_slot + 1)

        ports = ([op.port] if op.port else
                 (["a", "b"] if self.last_port == "b" else ["b", "a"]))
        s = max(lo, 1)
        while True:
            if hi_pulse is not None and op.src is not None and s > hi_pulse:
                raise ScheduleError(
                    f"no feasible slot for '{op.describe()}': read must sample before a "
                    f"same-stage write lands (pulse <= {hi_pulse}); reorder the stage")
            placed = False
            for port in (ports if op.kind in ("copy", "jump_if_zero") else [None]):
                cells, events, pair, pulse, value_slot = self._footprint(op, s, port)
                if pulse is not None and self._transient_at(op, pulse):
                    continue
                if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                    continue
                for slot, routes, imm in cells:
                    self.prog.at(slot, routes, imm, why=f"op{index}: {op.describe()}")
                self.latch_events.extend(events)
                self.latch_pairs.append(pair)
                op.slot, op.value_slot, op.port = s, value_slot, port
                if pulse is not None:
                    self.stage_reads.append((op.src, pulse))
                if op.dst is not None:
                    self.pending[op.dst] = value_slot
                if port:
                    self.last_port = port
                if op.kind == "jump_if_zero":
                    self.floor = value_slot + JUMP_DELAY_SLOTS + 1
                placed = True
                break
            if placed:
                return
            s += 1
            if s > 10000:
                raise ScheduleError(f"no slot found for '{op.describe()}'")

    def run(self) -> Program:
        index_to_labels = {}
        for name, idx in self.ir.labels.items():
            index_to_labels.setdefault(idx, []).append(name)
        for i, op in enumerate(self.ir.ops):
            pending_labels = index_to_labels.get(i, [])
            self._place(op, i)
            for name in pending_labels:
                self.label_slots[name] = op.slot
        return self.prog


def schedule(ir: IR, name: str = "", verbose: bool = False):
    """IR -> (Program, verifier Report). Raises if the result breaks any rule."""
    sched = _Scheduler(ir, name)
    prog = sched.run()
    report = verify(prog, name=name or "scheduled program")
    # delay slots of every jump must have stayed empty (they execute!)
    for jump in report.jumps:
        shadow = [s for s in prog.slots
                  if jump["value_slot"] < s <= jump["value_slot"] + JUMP_DELAY_SLOTS]
        if shadow:
            raise ScheduleError(f"scheduler placed ops {shadow} inside the delay slots "
                                f"of the jump at {jump['value_slot']}")
    if not report.ok:
        raise ScheduleError("scheduled program fails verification:\n" + report.render())
    if verbose:
        print(report.render(verbose=True))
        print("  schedule:")
        for i, op in enumerate(ir.ops):
            port = f" port {op.port}" if op.port else ""
            print(f"    op{i} stage {op.stage} slot {op.slot:>3} "
                  f"(value {op.value_slot}){port}: {op.describe()}")
    return prog, report


if __name__ == "__main__":
    # demo: fib_cmp with zero hand-placed slot numbers (cf. fib_cmp_program())
    G, H, M, N_ = (address_of(f"signal-{s}") for s in "GHMN")
    I_, Q_ = address_of("signal-I"), address_of("signal-Q")
    W = address_of("signal-heart")

    ir = IR()
    ir.write_imm(N_, 100, warm=True)      # threshold
    ir.write_imm(H, 1)                    # fib seed (G stays 0)
    ir.barrier()
    ir.label("loop")
    ir.copy(H, G)                         # parallel: G <- old H
    ir.copy(I_, H)                        # parallel: H <- old I = old G+H
    ir.barrier()
    ir.copy(H, M)                         # M <- new H, feeds CMP against N
    ir.barrier()
    ir.jump_if_zero(Q_, "loop")           # loop while Q = (M >= N) == 0
    ir.write_imm(W, 999)                  # done marker, past the delay slots

    prog, _ = schedule(ir, name="fib_cmp_auto", verbose=True)
    print(f"\n  end slot {prog.end()} (hand-scheduled fib_cmp ends at 38)")
