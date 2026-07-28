#!/usr/bin/env python3
"""v8 ISA compiler: IR + list scheduler + schedule checks for the v8 processor
(the v8 processor design — latched port A, memory-mapped port B,
pc_inject on out4, accumulate cells, negative numbers).

Matrix dests in v8: out1=write_addr, out2=write_value, out3=park A (LATCHED
read address), out4=pc_inject (sums into the PC cell for one tick). Port B has
no matrix dest: its read address is the VALUE of the shape-circle cell.

Measured timing (2026-07-13 benches, consumer-slot frame — "slot m" means the
ROM slot whose routes consume the value):

  a-> consumer:   m >= park_slot + 4; park persists until the next park + 4
  b-> consumer:   m >= mmap-write value slot + 7; stream persists until re-park
  cell freshness: m >= writer value slot + 6 (equals the v7 pulse rule m=p+3,
                  p >= v+3); ALU outputs: m >= v + 5 + latency (L=1 -> same 6)
  same-stage anti (read old value): pending write v >= m - 2 (v7-verified rule)
  write path:     const addr at n pairs value n+2; cell lands ~value+5 (unchanged)
  jump_rel:       inject at n -> n+1..n+4 execute (4 delay slots), fetch resumes
                  at n + offset + 5 (measured: skip count == offset)
  jump_if_zero:   v7 write-path conditional (addr = P + flag), value slot v
                  takes effect at fetch v+7; 6 delay slots (unchanged, P unmasked)
  halt:           jump_rel offset -5 lands on itself; slots n+1..n+4 reserved NOPs

Accumulate cells (reset-blocked: writes ADD instead of replace):
shape-vertical / shape-horizontal / shape-curve / shape-curve-2. write_imm
refuses them (use add_imm); they cannot be zero-write-cleared.
"""
from dataclasses import dataclass, field
from typing import Callable

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # main/ on the path
from processor.assembler import Program, assemble_program, encode
from signal_space import ALU_MAP, address_of, signal_at, total_addresses

# consumer-frame constants (see module docstring)
PARK_A_TO_USE = 4
PARK_B_VALUE_TO_USE = 7
CELL_TO_USE = 6
ALU_TO_USE_BASE = 5          # + latency
ANTI_MARGIN = 2              # same-stage pending write: v >= m - ANTI_MARGIN
ADDR_TO_VALUE = 2
WRITE_TO_CELL = 5
JUMP_REL_SHADOW = 4
JUMP_REL_RESUME = 5          # resume fetch at n + offset + JUMP_REL_RESUME
JUMP_ABS_DELAY_SLOTS = 6
COHERENCE_WINDOW = 6

# Canonical machine configuration (single source of truth — the import tool
# populates the write_trigger_reset / mmap_addr combinators from these; values
# in a pasted BP are treated as sample placeholders and overwritten):
MMAP_B_SIGNAL = "shape-circle"
ACCUMULATE_SIGNALS = ("shape-vertical", "shape-horizontal", "shape-curve", "shape-curve-2")

MMAP_B = address_of(MMAP_B_SIGNAL)
ACCUMULATE_CELLS = {address_of(s) for s in ACCUMULATE_SIGNALS}

ALU_OUTPUT = {}
for _op, _spec in ALU_MAP.items():
    for _out in _spec["out"]:
        _lat = 1 if (_op == "CMP" and _out in ("O", "Q")) else _spec["latency"]
        ALU_OUTPUT[f"signal-{_out}"] = {
            "op": _op, "latency": _lat,
            "operands": [address_of(f"signal-{s}") for s in _spec["in"]],
        }


_CHUNK1_WIDTH = total_addresses()  # 2480: also v10's per-chunk width (processor/modules/v10_addr_map.fnet)


def _name(addr):
    if addr > _CHUNK1_WIDTH:
        # v10 unified address space: chunk (2+) is one vector register's
        # fine-grained lane space, same per-signal layout as chunk1 just
        # offset by _CHUNK1_WIDTH per register (processor/modules/v10_addr_map.fnet
        # register_chunk_base). v8 programs never pass addresses this large,
        # so this branch is dead for pure-v8 use -- display only, for v10
        # program manifests/error messages.
        rel = (addr - 1) % _CHUNK1_WIDTH
        reg = (addr - 1) // _CHUNK1_WIDTH - 1
        s = signal_at(rel + 1)
        base = s["name"] if s["quality"] == "normal" else f"{s['name']}~{s['quality']}"
        return f"VREG{reg}.{base}"
    s = signal_at(addr)
    return s["name"] if s["quality"] == "normal" else f"{s['name']}~{s['quality']}"


def _alu_spec(addr):
    if addr > _CHUNK1_WIDTH:
        return None  # v10 vector-chunk addresses are plain memory, never ALU outputs
    s = signal_at(addr)
    if s["quality"] == "normal" and s["name"] in ALU_OUTPUT:
        return ALU_OUTPUT[s["name"]]
    return None


@dataclass(frozen=True)
class MachineConfig:
    """The machine-specific addresses the scheduler needs. The default is the
    v8 master (built from signal_space's canonical letters/shapes); an
    fnet-compiled processor supplies its own via config_from_signals_map —
    the design file is the source of truth (plan/NETLIST_PLAN.md 'config combinators
    are fnet-owned')."""
    pc_addr: int
    mmap_b: int
    accumulate_cells: frozenset
    alu_by_addr: dict            # result addr -> {"op", "latency", "operands": [addr, ...]}
    namer: Callable[[int], str]
    # v10 vector zone. `vec_reach` maps a CONTROL row address to
    # {consumer: (soonest, latest) combinational stages between them}; a
    # control absent from a consumer's entry simply does not reach it.
    # Consumers are the out-port rows a program can `copy_a` from, plus the
    # VEC_BUS sentinel — the shared vector write bus, which is not addressable
    # but IS consumed, by the write head every time `vec_wsel` fires. Empty for
    # v8, which has no vector zone, so every vector rule below is inert there.
    #
    # TWO numbers, not one, because a control can reach a consumer by paths of
    # different depth: `vec_wsel` reaches the bus in 2 stages through a plain
    # register select, but in 4 through the op farm's square block (square,
    # then subtract, then the gate). The two rules the scheduler enforces pull
    # in opposite directions, so each takes the number that is safe for it:
    #   forward  ("has my control arrived yet?")   -> LATEST, wait out the
    #                                                 slowest path;
    #   backward ("could a later write already be
    #             visible at an earlier read?")    -> SOONEST, assume the
    #                                                 fastest path.
    # A single number would be wrong for one of them the moment the zone stops
    # being a uniform-depth chain.
    vec_reach: dict = field(default_factory=dict)
    vec_bus_reader: int | None = None   # the control row whose settle reads VEC_BUS
    vec_rows: dict = field(default_factory=dict)   # row name -> address

    def alu_spec(self, addr):
        return self.alu_by_addr.get(addr)

    @property
    def vec_ports(self) -> frozenset:
        """Addressable rows whose value the vector zone produces (not cells)."""
        return frozenset(port for reach in self.vec_reach.values()
                         for port in reach if port != VEC_BUS)

    def vec_stages(self, control: int, port: int):
        """SOONEST stages carrying `control` to `port` — the anti-dependency
        number: if a read is this far past a later write's settle, it is
        already contaminated. None if that control never reaches the port."""
        span = self.vec_reach.get(control, {}).get(port)
        return None if span is None else span[0]

    def vec_stages_latest(self, control: int, port: int):
        """LATEST stages carrying `control` to `port` — the freshness number:
        a read must be this far past the control's settle to see it on every
        path. None if that control never reaches the port."""
        span = self.vec_reach.get(control, {}).get(port)
        return None if span is None else span[1]


def _master_config() -> MachineConfig:
    alu = {address_of(name): spec for name, spec in ALU_OUTPUT.items()}
    return MachineConfig(
        pc_addr=address_of("signal-P"),
        mmap_b=MMAP_B,
        accumulate_cells=frozenset(ACCUMULATE_CELLS),
        alu_by_addr=alu,
        namer=_name,
    )


def config_from_signals_map(signals_map: dict) -> MachineConfig:
    """Build a MachineConfig from a compiled fnet source's "signals" map.
    Expects the v8 component naming convention: memory rows pc, b_reg,
    sub/add/div/mul_{a,b,res}, cmp_m/cmp_n, cmp_min/cmp_max,
    flag_ge/flag_gt; accumulate rows carry "accumulate": true."""
    rows = {k: v for k, v in signals_map.items()
            if "." not in k and "address" in v}

    def addr(name):
        if name not in rows:
            raise KeyError(f"design declares no memory row named {name!r} "
                           f"(have: {', '.join(sorted(rows)) or 'none'})")
        return int(rows[name]["address"])

    alu = {}
    for op, (a, b, res) in {
        "SUB": ("sub_a", "sub_b", "sub_res"),
        "ADD": ("add_a", "add_b", "add_res"),
        "DIV": ("div_a", "div_b", "div_res"),
        "MUL": ("mul_a", "mul_b", "mul_res"),
    }.items():
        alu[addr(res)] = {"op": op, "latency": 1,
                          "operands": [addr(a), addr(b)]}
    cmp_operands = [addr("cmp_m"), addr("cmp_n")]
    for out, lat in (("cmp_min", 2), ("cmp_max", 2),
                     ("flag_ge", 1), ("flag_gt", 1)):
        alu[addr(out)] = {"op": "CMP", "latency": lat, "operands": cmp_operands}

    display = {int(e["address"]): k for k, e in rows.items()}
    return MachineConfig(
        pc_addr=addr("pc"),
        mmap_b=addr("b_reg"),
        accumulate_cells=frozenset(
            int(e["address"]) for e in rows.values() if e.get("accumulate")),
        alu_by_addr=alu,
        namer=lambda a: display.get(a, f"mem[{a}]"),
        vec_reach=_vec_reach_from_rows(rows),
        vec_bus_reader=(int(rows["vec_wsel"]["address"])
                        if "vec_wsel" in rows else None),
        vec_rows={name: int(entry["address"]) for name, entry in rows.items()
                  if name.startswith(("vec_", "vred_", "vqual_"))},
    )


# v10 vector zone timing, measured live 2026-07-27 (v10_vec_io.fnet /
# v10_op_farm.fnet). Stages are counted from the tick a control row SETTLES on
# the memory bus, and count the combinational stages the vector zone runs
# afterwards. Named by the design's row convention so a design without a
# vector zone (v8) simply produces an empty table.
#
# Each entry is `(soonest, latest)` — see MachineConfig.vec_reach for why one
# number is not enough. Everything downstream of the shared write bus adds a
# fixed tail: vred_sum is bus + 1 (the reduce), vec_rlane_data is bus + 2
# (lane_read, relabel), so a control's whole row follows from how it reaches
# the bus:
#
#   rsel/ssel        bus +1   the select gate, and nothing else
#   wsel/erase       bus +2   write head, then the register's own select gate
#                    bus +4   ...or, if the SQUARE block is the selected
#                             source, head, square, subtract, gate
#   bcast            bus +2   the vec-scal unit, then its select gate
#   wlane_addr/value bus +3   one-hot, multiply, then the select gate
#   rlane_addr                steers the lane extractor ONLY — it never
#                             reaches the bus and never reaches vred_sum
#
# VEC_BUS is the shared vector write bus. It has no address — a program can
# never read it — but it is consumed: the write head samples it every time
# `vec_wsel` fires, so a register write is a BUS READ at that instant. Without
# this a move silently captures whatever the mux happens to be showing, which
# is the single most likely way to get a wrong answer out of the vector zone.
VEC_BUS = -1

#   vred_count       bus +2   the popcount mask, then the same sum reducer,
#                             so it is always vred_sum + 1
_VEC_REACH_BY_NAME = {
    "vec_rsel":        {"vec_rlane_data": (3, 3), "vred_sum": (2, 2),
                        "vred_count": (3, 3), VEC_BUS: (1, 1)},
    "vec_ssel":        {"vec_rlane_data": (3, 3), "vred_sum": (2, 2),
                        "vred_count": (3, 3), VEC_BUS: (1, 1)},
    # a register write changes a mux SOURCE, so it reaches the bus through the
    # register's own frame and then the select gate — or, two stages deeper,
    # through the op farm's square block
    "vec_wsel":        {"vec_rlane_data": (3, 6), "vred_sum": (2, 5),
                        "vred_count": (3, 6), VEC_BUS: (2, 4)},
    "vec_erase":       {"vec_rlane_data": (3, 6), "vred_sum": (2, 5),
                        "vred_count": (3, 6), VEC_BUS: (2, 4)},
    # the broadcast feeds a vec-scal unit, whose output then passes a select
    # gate; it never reaches the square block
    "vec_bcast":       {"vec_rlane_data": (3, 4), "vred_sum": (2, 3),
                        "vred_count": (3, 4), VEC_BUS: (2, 2)},
    # the second broadcast feeds the BCAST generator, same depth
    "vec_bcast2":      {"vec_rlane_data": (3, 4), "vred_sum": (2, 3),
                        "vred_count": (3, 4), VEC_BUS: (2, 2)},
    "vec_wlane_addr":  {"vec_rlane_data": (5, 5), "vred_sum": (4, 4),
                        "vred_count": (5, 5), VEC_BUS: (3, 3)},
    "vec_wlane_value": {"vec_rlane_data": (5, 5), "vred_sum": (4, 4),
                        "vred_count": (5, 5), VEC_BUS: (3, 3)},
    # steers the lane extractor only — never the bus, never the reductions
    "vec_rlane_addr":  {"vec_rlane_data": (3, 3)},
}


def jump_rel_a_window(op, distance: int) -> range:
    """Slots a taken `jump_rel_a` skips: the four shadow slots after the inject
    execute either way, then fetch resumes at n + distance + JUMP_REL_RESUME."""
    start = op.slot + JUMP_REL_SHADOW + 1
    return range(start, op.slot + distance + JUMP_REL_RESUME)


def check_pulse_widths(ir) -> None:
    """Every pulsed control row must be high for EXACTLY one tick.

    A machine invariant rather than a bench one: the v10 vector zone reads its
    write-select off a memory row, so a pulse that widened to three ticks
    commits three accumulate copies of the write bus instead of one — a wrong
    answer, silently. `pulse_imm` places the pair atomically to make that
    impossible; this re-asserts the result on the finished schedule."""
    problems = []
    for op in ir.pulses:
        width = op.clear_value_slot - op.value_slot
        if width != 1:
            problems.append(
                f"{ir.config.namer(op.dst)} pulse is {width} tick(s) wide "
                f"(value slots {op.value_slot} -> {op.clear_value_slot}); "
                f"a register write would accumulate {width} copies of the bus")
    if problems:
        raise ScheduleError("pulse width check failed:\n  " + "\n  ".join(problems))


def _vec_reach_from_rows(rows: dict) -> dict:
    """Resolve _VEC_REACH_BY_NAME against a design's own addresses."""
    reach = {}
    for control, ports in _VEC_REACH_BY_NAME.items():
        if control not in rows:
            continue
        resolved = {}
        for port, span in ports.items():
            if port == VEC_BUS:
                resolved[VEC_BUS] = span
            elif port in rows:
                resolved[int(rows[port]["address"])] = span
        if resolved:
            reach[int(rows[control]["address"])] = resolved
    return reach


DEFAULT_CONFIG = _master_config()


@dataclass
class Op:
    kind: str
    stage: int
    src: int | None = None
    dst: int | None = None
    value: int | None = None
    warm: bool = False
    target: str | None = None
    slot: int | None = None
    value_slot: int | None = None
    clear_value_slot: int | None = None   # pulse_imm: slot of the trailing zero
    first_slot: int | None = None   # earliest footprint slot incl. parks — what
                                    # a loop label actually re-enters
    last_slot: int | None = None    # latest footprint slot (labels floor past it)

    def describe(self, namer=_name):
        return {
            "write_imm": lambda: f"{namer(self.dst)} <- {self.value}" + (" (warm)" if self.warm else ""),
            "pulse_imm": lambda: f"{namer(self.dst)} <- {self.value} for ONE tick, then 0",
            "add_imm": lambda: f"{namer(self.dst)} += {self.value}",
            "copy_a": lambda: f"{namer(self.dst)} <- {namer(self.src)} (port A)",
            "add_imm_a": lambda: f"{namer(self.dst)} <- {namer(self.src)} + {self.value} (A+const on out2)",
            "add_ab": lambda: f"{namer(self.dst)} <- {namer(self.src)} + B (A+B on out2)",
            "copy_a_indexed": lambda: f"{namer(self.dst)} <- mem[{namer(self.src)} + B] (const+B on out3)",
            "write_indexed": lambda: f"mem[{namer(self.src)} + B] <- {self.value} (const+B on out1)",
            "park_b": lambda: f"park B on {namer(self.src)}",
            "copy_b": lambda: f"{namer(self.dst)} <- B stream",
            "jump_rel": lambda: f"jump_rel to '{self.target}'",
            "jump_rel_a": lambda: f"PC += {namer(self.src)} (computed, a->out4)",
            "jump_if_zero": lambda: f"jump to '{self.target}' while {namer(self.src)} == 0",
            "halt": lambda: "halt (self-jump spin)",
        }[self.kind]()


class IR8:
    """v8 program builder. Stage semantics as in the archived v7 scheduler: within a stage
    reads sample pre-stage state; barrier() commits."""

    def __init__(self, config: MachineConfig | None = None):
        self.config = config or DEFAULT_CONFIG
        self.ops: list[Op] = []
        self.stage = 0
        self.labels: dict[str, int] = {}
        self.pulses: list[Op] = []      # checked by check_pulse_widths

    def barrier(self):
        self.stage += 1

    def label(self, name):
        if name in self.labels:
            raise ValueError(f"duplicate label '{name}'")
        self.labels[name] = len(self.ops)

    def _add(self, op):
        self.ops.append(op)
        return op

    def write_imm(self, addr, value, warm=False):
        if addr in self.config.accumulate_cells:
            raise ValueError(f"{self.config.namer(addr)} is an accumulate cell — use add_imm "
                             "(writes there sum; they cannot be replaced or zeroed)")
        return self._add(Op("write_imm", self.stage, dst=addr, value=value, warm=warm))

    def pulse_imm(self, addr, value):
        """Hold `addr` at `value` for EXACTLY one tick, then zero it.

        The write path is pipelined — a write_imm's address at slot n pairs
        its value at n+2 and the cell lands five later — so two writes to the
        same row placed on consecutive slots land on consecutive ticks. That
        makes a one-tick pulse fall out of the existing hardware with no new
        combinators (designer, 2026-07-27).

        It is a single op rather than two write_imm calls because the pair
        must be ATOMIC: scheduled independently, surrounding stages' footprints
        can steal the adjacent slot and silently widen the pulse. A wide pulse
        is not a small error — the v10 vector zone reads its write-select off a
        memory row, so a 3-tick pulse commits three accumulate copies of the
        vector bus instead of one.
        """
        if addr in self.config.accumulate_cells:
            raise ValueError(f"{self.config.namer(addr)} is an accumulate cell — "
                             "a pulse there would add twice, not pulse")
        op = self._add(Op("pulse_imm", self.stage, dst=addr, value=value))
        self.pulses.append(op)
        return op

    def add_imm(self, addr, value):
        if addr not in self.config.accumulate_cells:
            raise ValueError(f"{self.config.namer(addr)} is not an accumulate cell — writes there "
                             "replace; use write_imm")
        return self._add(Op("add_imm", self.stage, dst=addr, value=value))

    def copy_a(self, src, dst):
        return self._add(Op("copy_a", self.stage, src=src, dst=dst))

    def park_b(self, src):
        return self._add(Op("park_b", self.stage, src=src, dst=self.config.mmap_b, value=src))

    def copy_b(self, dst):
        return self._add(Op("copy_b", self.stage, dst=dst))

    # -- v10 vector zone ------------------------------------------------------
    # THE ISOLATION LAYER. Everything above is scalar v8 and always will be.
    # The three methods below are the ONLY place that knows how a vector action
    # is realized on this machine — today, as ordinary writes to memory-mapped
    # control rows, because the zone's control plane IS chunk1's memory bus.
    #
    # That costs three traversals of the scalar write path per move (~15 ticks;
    # measured, and about 90% of vector-code time). The planned fix is a second
    # instruction decoder driving the select/erase/write lines directly, which
    # would make a move ~3 ticks. Programs must not have to change when that
    # lands, so they call `vec_move` and never write a control row by hand:
    # the decoder becomes a different expansion here plus new entries in
    # _VEC_REACH_BY_NAME, and every bench re-verifies it unchanged.

    def _vec_row(self, name):
        rows = self.config.vec_rows
        if name not in rows:
            raise ValueError(
                f"this machine has no vector zone (no {name!r} row); "
                f"vector ops need a v10-class design")
        return rows[name]

    def vec_select(self, src, src2=None, bcast=None, bcast2=None):
        """Park the read mux, and optionally the broadcast scalars.

        S is written EVERY time, never left standing: two selections SUM on the
        write bus, so a stale S is not a no-op, it is a silent extra addend.
        Pass `src2` to use that deliberately — it is how vector ADD costs zero
        combinators, and how a frame generator resurrects lanes an op dropped.
        """
        if bcast is not None:
            self.write_imm(self._vec_row("vec_bcast"), bcast)
        if bcast2 is not None:
            self.write_imm(self._vec_row("vec_bcast2"), bcast2)
        self.write_imm(self._vec_row("vec_rsel"), src)
        self.write_imm(self._vec_row("vec_ssel"), src if src2 is None else src2)

    def vec_move(self, dst, src, src2=None, bcast=None, bcast2=None,
                 accumulate=False):
        """`dst` (a register block id) <- whatever the mux presents.

        Registers are accumulate-only, so a replace is erase-then-write: the
        erase leaves the register empty AND holding, and into an empty register
        a single accumulate pulse IS a replace. `accumulate=True` skips the
        erase, which is how a per-lane counter works with no adder anywhere.

        Both pulses must be exactly one tick — `pulse_imm` places each pair
        atomically on consecutive slots, because two independent writes widen
        to three ticks once neighbouring stages compete for the slot, and a
        3-tick pulse commits three accumulate copies of the bus.
        """
        self.vec_select(src, src2, bcast, bcast2)
        self.barrier()
        if not accumulate:
            self.pulse_imm(self._vec_row("vec_erase"), dst)
            self.barrier()
        self.pulse_imm(self._vec_row("vec_wsel"), dst)
        self.barrier()

    def vec_read_lane(self, lane, dst):
        """Extract one lane of whatever the mux presents into a scalar row."""
        self.write_imm(self._vec_row("vec_rlane_addr"), lane)
        self.barrier()
        self.copy_a(self._vec_row("vec_rlane_data"), dst)
        self.barrier()

    # -- summing routes ------------------------------------------------------
    # THE MATRIX IS A CROSSBAR, NOT AN INSTRUCTION SET. Any subset of the 12
    # source->dest routes can fire in one slot, and two sources aimed at the
    # SAME dest both land on that dest's net. Values are not normalised inside
    # the matrix - port A's rides its parked row's own signal, port B's rides
    # its streamed row's, const's rides the immediate carrier - so a dest can
    # be holding one value SPLIT ACROSS SIGNALS. By convention the value is
    # their sum, and every consumer sanitises with `each+0 -> carrier` on the
    # way in (designer, 2026-07-28: sanitise at the output, not the input).
    # Measured at the matrix by v8_decoder_matrix (24/24) and end to end by
    # `fnet_bus_summation`.
    #
    # None of these four is a new combinator. Each is a different subset of the
    # same route bits, which is the whole point of a programmable interconnect.

    def add_imm_a(self, src, dst, k):
        """dst <- mem[src] + k in ONE slot (`a->out2` + `const->out2`).

        The ALU route costs a write, ALU_TO_USE_BASE + latency, and a read.
        This costs the read alone, which is why counters and pointer bumps
        should never touch the ALU."""
        return self._add(Op("add_imm_a", self.stage, src=src, dst=dst, value=k))

    def add_ab(self, src, dst):
        """dst <- mem[src] + <port B stream> (`a->out2` + `b->out2`).

        Two memory rows added with no ALU at all. B must already be parked."""
        return self._add(Op("add_ab", self.stage, src=src, dst=dst))

    def copy_a_indexed(self, base, dst):
        """dst <- mem[base + <port B stream>] (`const->out3` + `b->out3`).

        The READ ADDRESS is computed on the interconnect, so this is `arr[i]`
        with i streaming through port B. The park is private: a computed
        address cannot be reused by the park tracker, which keys on a literal."""
        return self._add(Op("copy_a_indexed", self.stage, src=base, dst=dst))

    def write_indexed(self, base, value):
        """mem[base + <port B stream>] <- value (`const->out1` + `b->out1`).

        `arr[i] = k`, the write address computed the way the read address is,
        on the other matrix dest."""
        return self._add(Op("write_indexed", self.stage, src=base, value=value))

    def jump_rel_a(self, src):
        """PC += mem[src] — a COMPUTED relative jump (`a->out4`).

        pc_inject sums whatever reaches out4 into the PC for one tick, and it
        does not care which source sent it. Routing port A there makes the jump
        distance DATA, which is the whole of conditional and computed control
        flow on this machine and needs no new hardware:

          if      — `mem[src] = flag * skip`, so 0 falls through and 1 skips;
          switch  — `mem[src]` read from a jump table;
          return  — `mem[src]` holding a saved distance.

        The scheduler cannot know where this lands (the distance is data), so
        it reserves the four shadow slots and floors everything after at
        n + 5 — which is exactly the first slot a taken jump skips. A caller
        that wants to know what got skipped reads it off the finished schedule;
        `jump_rel_a_window()` does that.
        """
        op = self._add(Op("jump_rel_a", self.stage, src=src))
        self.stage += 1
        return op

    def jump_rel(self, target):
        op = self._add(Op("jump_rel", self.stage, target=target))
        self.stage += 1
        return op

    def jump_if_zero(self, flag, target):
        op = self._add(Op("jump_if_zero", self.stage, src=flag, target=target))
        self.stage += 1
        return op

    def halt(self):
        op = self._add(Op("halt", self.stage))
        self.stage += 1
        return op


class ScheduleError(Exception):
    pass


class _Sched8:
    def __init__(self, ir, name=""):
        self.ir = ir
        self.cfg = ir.config
        self.name = name
        self.prog = Program()
        self.committed: dict[int, int] = {}     # cell -> last committed value slot
        self.pending: dict[int, int] = {}
        self.stage_reads: list[tuple[int, int]] = []   # (cell/ALU addr read, consumer slot)
        self.latch_pairs: list[tuple[int, int]] = []
        self.latch_events: list[int] = []
        self.parks_a: list[tuple[int, int]] = []       # (park slot, address), sorted
        self._vec_reads: list[tuple[int, int]] = []    # (consumer slot, vector out-port)
        self._vec_bus_reads: list[int] = []            # settle slots of vec_wsel writes
        self.park_b_slot: int | None = None            # mmap write value slot
        # (row, slot) for every read of the port-B stream, kept across stages
        # — see _write_floor.
        self._stream_consumers: list = []
        self.park_b_addr: int | None = None
        self.current_stage = 0
        self.floor = 1
        self.label_slots: dict[str, int] = {}
        self.manifest: list[str] = []

    # -- shared helpers (write latch identical to v7) ------------------------
    def _routes_fit(self, cells):
        for slot, routes, imm in cells:
            entry = self.prog.slots.get(slot)
            if entry is None:
                continue
            if any(r in entry["routes"] for r in routes):
                return False
            if imm is not None and entry["imm"] is not None and entry["imm"] != imm:
                return False
        return True

    def _latch_fits(self, events, pair):
        for pt in events:
            if pt in self.latch_events:
                return False
        pt_new, v_new = pair
        for pt_e, v_e in self.latch_pairs:
            if pt_e < pt_new <= v_e or pt_new < pt_e <= v_new:
                return False
        for pt in events[:-1]:
            for pt_e, v_e in self.latch_pairs:
                if pt_e < pt <= v_e:
                    return False
        return True

    def _park_a_live(self, m):
        """Address port A serves a consumer at slot m, or None."""
        live = None
        for park_slot, addr in self.parks_a:
            if park_slot + PARK_A_TO_USE <= m:
                live = addr
            else:
                break
        return live

    def _read_lo(self, addr):
        """Earliest consumer slot m for a fresh committed read of addr."""
        lo = 1
        spec = self.cfg.alu_spec(addr)
        if spec:
            for cell in spec["operands"]:
                v = self.committed.get(cell)
                if v is not None:
                    lo = max(lo, v + ALU_TO_USE_BASE + spec["latency"])
        else:
            v = self.committed.get(addr)
            if v is not None:
                lo = max(lo, v + CELL_TO_USE)
        return max(lo, self._vec_read_lo(addr))

    def _vec_read_lo(self, port):
        """Earliest consumer slot for a v10 vector out-port row.

        A vector out-port is not a memory cell — its value is produced
        combinationally from control ROWS. A control written at value slot v
        lands in its cell at v + WRITE_TO_CELL, the vector zone then needs
        `stages` more ticks to carry it to this port, and reading a settled
        memory-bus row costs the same one extra slot a cell read does. So the
        consumer floor is v + CELL_TO_USE + stages: the ordinary cell rule
        with the vector zone's depth added.

        The LATEST depth is the one that matters here: the read must wait out
        the slowest path the control can take, or a deep source (the op farm's
        square block) is still showing the previous frame.

        Inert for v8 — `vec_reach` is empty there, so this returns 1."""
        lo = 1
        for control, ports in self.cfg.vec_reach.items():
            span = ports.get(port)
            if span is None:
                continue
            for table in (self.committed, self.pending):
                v = table.get(control)
                if v is not None:
                    lo = max(lo, v + CELL_TO_USE + span[1])
        return lo

    def _vec_write_disturbs_read(self, dst, value_slot):
        """True if landing a control row here would reach an ALREADY-PLACED read.

        This is the vector anti-dependency, and it is the direction that bites.
        The zone is a fixed-delay chain: a port at tick S reflects its controls
        as of tick S - stages, so a control settling at T becomes visible from
        T + stages onward. Ops are placed in IR order, so every read already in
        `_vec_reads` precedes this write in the program — and must therefore
        still see the OLD control. That holds only while read_slot <
        T + stages.

        It matters because the greedy scheduler can hand a later op an earlier
        slot: a vector read waits out the zone's propagation and leaves a wide
        gap behind it, and the next control write drops straight into that gap.
        Measured: `vec_rlane_addr <- 8` landed at slot 58 and became visible at
        68 — exactly the slot where the preceding read of lane 7 sat, which
        would have silently read lane 8 instead.

        The SOONEST depth is the one that matters here: contamination only
        needs ONE path to be short enough, so the fastest route decides."""
        ports = self.cfg.vec_reach.get(dst)
        if not ports:
            return False
        settle = value_slot + WRITE_TO_CELL
        for read_slot, port in self._vec_reads:
            span = ports.get(port)
            if span is not None and read_slot >= settle + span[0]:
                return True
        # the write bus is consumed too — a steering change must not become
        # visible at a register write that the program already committed to
        bus_span = ports.get(VEC_BUS)
        if bus_span is not None:
            for bus_read in self._vec_bus_reads:
                if bus_read >= settle + bus_span[0]:
                    return True
        return False

    def _vec_bus_write_floor(self):
        """Earliest value slot for a `vec_wsel` write, as a BUS READ.

        The write head samples the shared vector bus at the instant W settles,
        so the mux must already be presenting the intended source by then.
        Returns the floor in value-slot space (settle = value slot +
        WRITE_TO_CELL)."""
        settle_lo = 1
        for control, ports in self.cfg.vec_reach.items():
            span = ports.get(VEC_BUS)
            if span is None:
                continue
            for table in (self.committed, self.pending):
                v = table.get(control)
                if v is not None:
                    settle_lo = max(settle_lo, v + WRITE_TO_CELL + span[1])
        return settle_lo - WRITE_TO_CELL

    def _read_hi(self, addr):
        """Latest consumer slot (same-stage pending writes must stay old)."""
        hi = None
        spec = self.cfg.alu_spec(addr)
        cells = spec["operands"] if spec else [addr]
        for cell in cells:
            pv = self.pending.get(cell)
            if pv is not None:
                limit = pv + ANTI_MARGIN
                hi = limit if hi is None else min(hi, limit)
        return hi

    def _transient(self, addr, m):
        spec = self.cfg.alu_spec(addr)
        if not spec:
            return False
        lat = spec["latency"]
        for cell in spec["operands"]:
            v = self.committed.get(cell)
            if v is None or m != v + ALU_TO_USE_BASE + lat:
                continue
            for other in spec["operands"]:
                if other == cell:
                    continue
                v2 = self.committed.get(other)
                if v2 is not None and abs(m - (v2 + ALU_TO_USE_BASE + lat)) < COHERENCE_WINDOW:
                    return True
        return False

    def _write_floor(self, dst):
        v_min = max(self.committed.get(dst, 0), self.pending.get(dst, 0)) + 1
        for cell, m in self.stage_reads:
            spec = self.cfg.alu_spec(cell)
            cells = spec["operands"] if spec else [cell]
            if dst in cells or dst == cell:
                v_min = max(v_min, m - ANTI_MARGIN + 1)
        # CROSS-STAGE ANTI-DEPENDENCY ON THE PORT-B STREAM. `stage_reads` is
        # cleared at every barrier, so it only guards reads in the CURRENT
        # stage — fine for ports whose consumers are placed monotonically, and
        # not fine for the stream, because port B's value is whatever its row
        # holds AT THE READ TICK and the greedy scheduler is free to hand a
        # later op an earlier slot. Measured: an index bump three stages later
        # landed at slot 36 and became visible at 42, one tick before an
        # already-placed indexed store sampled the index at 42 — the store went
        # to arr[0] instead of arr[3], silently. A write must therefore stay
        # invisible to every stream read already placed: land later than
        # m - CELL_TO_USE.
        for row, m in self._stream_consumers:
            if row == dst:
                v_min = max(v_min, m - CELL_TO_USE + 1)
        return v_min

    def _commit_stage(self, stage):
        if stage != self.current_stage:
            self.committed.update(self.pending)
            self.pending.clear()
            self.stage_reads.clear()
            self.current_stage = stage

    def _place_cells(self, cells, events, pair, op=None):
        for slot, routes, imm in cells:
            self.prog.at(slot, routes, imm)
        self.latch_events.extend(events)
        if pair:
            self.latch_pairs.append(pair)
        if op is not None:
            op.first_slot = min(slot for slot, _r, _i in cells)
            op.last_slot = max(slot for slot, _r, _i in cells)

    def _label_slot(self, name):
        """A label re-enters at the MINIMUM footprint slot of the ops after it
        (a consumer's park/addr slots precede its anchor), so every part of the
        loop body re-executes on the next pass."""
        idx = self.ir.labels[name]
        slots = [o.first_slot for o in self.ir.ops[idx:] if o.first_slot is not None]
        if not slots:
            raise ScheduleError(f"label '{name}' has no scheduled ops after it "
                                "(backward jumps only)")
        return min(slots)

    # -- op placement ---------------------------------------------------------
    def place(self, op, index):
        self._commit_stage(op.stage)
        fn = getattr(self, "_op_" + op.kind)
        fn(op)
        self.manifest.append(f"op{index} stage {op.stage} slot {op.slot:>3}: "
                             f"{op.describe(self.cfg.namer)}")

    def _search(self, lo, hi, try_slot):
        s = max(lo, self.floor, 1)
        while True:
            if hi is not None and s > hi:
                raise ScheduleError(f"no feasible slot in [{lo}, {hi}] — reorder the stage")
            if try_slot(s):
                return s
            s += 1
            if s > 10000:
                raise ScheduleError("no slot found")

    def _op_write_imm(self, op):
        value_off = 4 if op.warm else 2
        lo = self._write_floor(op.dst) - value_off
        if op.dst == self.cfg.vec_bus_reader:
            lo = max(lo, self._vec_bus_write_floor() - value_off)

        def attempt(s):
            if op.warm:
                cells = [(s + i, ["const->write_addr"], op.dst) for i in range(3)]
                cells.append((s + 4, ["const->write_value"], op.value))
                events, pair = [s + 2, s + 3, s + 4], (s + 4, s + 4)
            else:
                cells = [(s, ["const->write_addr"], op.dst),
                         (s + 2, ["const->write_value"], op.value)]
                events, pair = [s + 2], (s + 2, s + 2)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            if self._vec_write_disturbs_read(op.dst, s + value_off):
                return False
            self._place_cells(cells, events, pair, op)
            op.slot, op.value_slot = s, s + value_off
            if op.dst == self.cfg.vec_bus_reader:
                self._vec_bus_reads.append(op.value_slot + WRITE_TO_CELL)
            self.pending[op.dst] = op.value_slot
            return True

        self._search(lo, None, attempt)

    _op_add_imm = _op_write_imm   # same footprint; semantics differ only in the cell

    def _op_pulse_imm(self, op):
        """Two writes to one row on CONSECUTIVE slots, placed atomically.

        Footprint: address at s and s+1, values at s+2 (the value) and s+3
        (zero). Both value events pair against the same latched address, so
        the latch is held s+2..s+3. Placing them as one unit is the whole
        point — see IR8.pulse_imm."""
        lo = self._write_floor(op.dst) - 2
        if op.dst == self.cfg.vec_bus_reader:
            lo = max(lo, self._vec_bus_write_floor() - 2)

        def attempt(s):
            cells = [(s, ["const->write_addr"], op.dst),
                     (s + 1, ["const->write_addr"], op.dst),
                     (s + 2, ["const->write_value"], op.value),
                     (s + 3, ["const->write_value"], 0)]
            events, pair = [s + 2, s + 3], (s + 2, s + 3)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            # a pulse settles twice — the value, then the trailing zero
            if (self._vec_write_disturbs_read(op.dst, s + 2)
                    or self._vec_write_disturbs_read(op.dst, s + 3)):
                return False
            self._place_cells(cells, events, pair, op)
            op.slot, op.value_slot = s, s + 2
            if op.dst == self.cfg.vec_bus_reader:
                self._vec_bus_reads.append(op.value_slot + WRITE_TO_CELL)
            op.clear_value_slot = s + 3
            self.pending[op.dst] = s + 3
            return True

        self._search(lo, None, attempt)

    def _op_park_b(self, op):
        self._op_write_imm(op)                 # a plain write to the mmap cell
        self.park_b_slot = op.value_slot
        self.park_b_addr = op.src

    def _op_copy_a(self, op):
        read_lo = self._read_lo(op.src)
        read_hi = self._read_hi(op.src)
        w_floor = self._write_floor(op.dst)

        def attempt(m):
            if read_hi is not None and m > read_hi:
                raise ScheduleError(f"'{op.describe()}': consumer must sample before a "
                                    f"same-stage write lands (m <= {read_hi}); reorder")
            if self._transient(op.src, m):
                return False
            # park: reuse a live park on the same address, else place a new one
            park_cells = []
            if self._park_a_live(m) != op.src:
                pn = m - PARK_A_TO_USE
                if pn < 1:
                    return False
                park_cells = [(pn, ["const->out3"], op.src)]
                for ps, _addr in self.parks_a:
                    if ps == pn:
                        return False
                # park must not cut off between an existing park and its consumers:
                # conservative — a new park's live point (pn+4=m) must not precede
                # any already-scheduled a-> consumer at slot >= m
                for cell, mm in self._a_consumers:
                    if mm >= m:
                        return False
            cells = park_cells + [(m - ADDR_TO_VALUE, ["const->write_addr"], op.dst),
                                  (m, ["a->write_value"], None)]
            if m - ADDR_TO_VALUE < 1:
                return False
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            if m < w_floor:
                return False
            self._place_cells(cells, events, pair, op)
            if park_cells:
                self.parks_a.append((m - PARK_A_TO_USE, op.src))
                self.parks_a.sort()
            self._a_consumers.append((op.src, m))
            if op.src in self.cfg.vec_ports:
                self._vec_reads.append((m, op.src))
            op.slot, op.value_slot = m, m
            self.stage_reads.append((op.src, m))
            self.pending[op.dst] = m
            return True

        lo = max(read_lo, w_floor, self.floor + PARK_A_TO_USE
                 if self._park_a_live(max(read_lo, w_floor, self.floor)) != op.src else read_lo)
        self._search(max(lo, self.floor), None, attempt)

    def _op_copy_b(self, op):
        if self.park_b_slot is None:
            raise ScheduleError("copy_b before park_b: port B has no stream address")
        src = self.park_b_addr
        read_lo = max(self._read_lo(src), self.park_b_slot + PARK_B_VALUE_TO_USE)
        read_hi = self._read_hi(src)
        w_floor = self._write_floor(op.dst)

        def attempt(m):
            if read_hi is not None and m > read_hi:
                raise ScheduleError(f"'{op.describe()}': consumer must sample before a "
                                    f"same-stage write lands (m <= {read_hi}); reorder")
            if self._transient(src, m):
                return False
            if m < w_floor or m - ADDR_TO_VALUE < 1:
                return False
            cells = [(m - ADDR_TO_VALUE, ["const->write_addr"], op.dst),
                     (m, ["b->write_value"], None)]
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            op.slot, op.value_slot = m, m
            self.stage_reads.append((src, m))
            self.pending[op.dst] = m
            return True

        self._search(max(read_lo, self.floor), None, attempt)

    def _op_add_imm_a(self, op):
        """copy_a's shape, with the immediate riding out2 alongside A."""
        read_lo, read_hi = self._read_lo(op.src), self._read_hi(op.src)
        w_floor = self._write_floor(op.dst)

        def attempt(m):
            if read_hi is not None and m > read_hi:
                raise ScheduleError("consumer must sample before a same-stage write")
            if self._transient(op.src, m) or m < w_floor or m - ADDR_TO_VALUE < 1:
                return False
            park_cells = []
            if self._park_a_live(m) != op.src:
                pn = m - PARK_A_TO_USE
                if pn < 1 or any(ps == pn for ps, _a in self.parks_a):
                    return False
                for _cell, mm in self._a_consumers:
                    if mm >= m:
                        return False
                park_cells = [(pn, ["const->out3"], op.src)]
            cells = park_cells + [
                (m - ADDR_TO_VALUE, ["const->write_addr"], op.dst),
                (m, ["a->write_value", "const->write_value"], op.value)]
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            if park_cells:
                self.parks_a.append((m - PARK_A_TO_USE, op.src))
                self.parks_a.sort()
            self._a_consumers.append((op.src, m))
            op.slot, op.value_slot = m, m
            self.stage_reads.append((op.src, m))
            self.pending[op.dst] = m
            return True

        self._search(max(read_lo, w_floor, self.floor), None, attempt)

    def _op_add_ab(self, op):
        """A and B both aimed at write_value: the two rows sum on the way in."""
        if self.park_b_slot is None:
            raise ScheduleError("add_ab before park_b: port B has no stream address")
        b_src = self.park_b_addr
        read_lo = max(self._read_lo(op.src), self._read_lo(b_src),
                      self.park_b_slot + PARK_B_VALUE_TO_USE)
        w_floor = self._write_floor(op.dst)

        def attempt(m):
            if (self._transient(op.src, m) or self._transient(b_src, m)
                    or m < w_floor or m - ADDR_TO_VALUE < 1):
                return False
            park_cells = []
            if self._park_a_live(m) != op.src:
                pn = m - PARK_A_TO_USE
                if pn < 1 or any(ps == pn for ps, _a in self.parks_a):
                    return False
                for _cell, mm in self._a_consumers:
                    if mm >= m:
                        return False
                park_cells = [(pn, ["const->out3"], op.src)]
            cells = park_cells + [
                (m - ADDR_TO_VALUE, ["const->write_addr"], op.dst),
                (m, ["a->write_value", "b->write_value"], None)]
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            if park_cells:
                self.parks_a.append((m - PARK_A_TO_USE, op.src))
                self.parks_a.sort()
            self._a_consumers.append((op.src, m))
            op.slot, op.value_slot = m, m
            self.stage_reads.append((op.src, m))
            self.stage_reads.append((b_src, m))
            self._stream_consumers.append((b_src, m))
            self.pending[op.dst] = m
            return True

        self._search(max(read_lo, w_floor, self.floor), None, attempt)

    def _op_copy_a_indexed(self, op):
        """Park A at base + B, then read it.

        The park is never shared: its address is DATA, so `_park_a_live` (which
        keys on a literal) must not go on believing it knows where A points."""
        if self.park_b_slot is None:
            raise ScheduleError("copy_a_indexed before park_b: no index stream")
        b_src = self.park_b_addr
        read_lo = max(self._read_lo(b_src),
                      self.park_b_slot + PARK_B_VALUE_TO_USE) + PARK_A_TO_USE
        w_floor = self._write_floor(op.dst)

        def attempt(m):
            pn = m - PARK_A_TO_USE
            if pn < 1 or m < w_floor or m - ADDR_TO_VALUE < 1:
                return False
            if any(ps == pn for ps, _a in self.parks_a):
                return False
            for _cell, mm in self._a_consumers:
                if mm >= m:
                    return False
            cells = [(pn, ["const->out3", "b->out3"], op.src),
                     (m - ADDR_TO_VALUE, ["const->write_addr"], op.dst),
                     (m, ["a->write_value"], None)]
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            self.parks_a.append((pn, None))
            self.parks_a.sort(key=lambda pa: pa[0])
            self._a_consumers.append((None, m))
            op.slot, op.value_slot = m, m
            self.stage_reads.append((b_src, pn))
            self._stream_consumers.append((b_src, pn))
            self.pending[op.dst] = m
            return True

        self._search(max(read_lo, w_floor, self.floor), None, attempt)

    def _op_write_indexed(self, op):
        """Write address computed on out1: const base + streamed index."""
        if self.park_b_slot is None:
            raise ScheduleError("write_indexed before park_b: no index stream")
        b_src = self.park_b_addr
        read_lo = max(self._read_lo(b_src),
                      self.park_b_slot + PARK_B_VALUE_TO_USE) + ADDR_TO_VALUE

        def attempt(m):
            if m - ADDR_TO_VALUE < 1:
                return False
            cells = [(m - ADDR_TO_VALUE,
                      ["const->write_addr", "b->write_addr"], op.src),
                     (m, ["const->write_value"], op.value)]
            events, pair = [m], (m, m)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            op.slot, op.value_slot = m, m
            self.stage_reads.append((b_src, m - ADDR_TO_VALUE))
            self._stream_consumers.append((b_src, m - ADDR_TO_VALUE))
            return True

        self._search(max(read_lo, self.floor), None, attempt)

    def _op_jump_rel_a(self, op):
        """`a->out4`: the PC-inject path driven from memory instead of an
        immediate. Same shadow and same resume rule as jump_rel — only the
        distance moves from the instruction word to a row."""
        read_lo = self._read_lo(op.src)

        def attempt(n):
            park_cells = []
            if self._park_a_live(n) != op.src:
                pn = n - PARK_A_TO_USE
                if pn < 1 or any(ps == pn for ps, _a in self.parks_a):
                    return False
                for _cell, mm in self._a_consumers:
                    if mm >= n:
                        return False
                park_cells = [(pn, ["const->out3"], op.src)]
            cells = park_cells + [(n, ["a->out4"], None)]
            if not self._routes_fit(cells):
                return False
            if any(s in self.prog.slots
                   for s in range(n + 1, n + 1 + JUMP_REL_SHADOW)):
                return False
            self._place_cells(cells, [], None, op)
            if park_cells:
                self.parks_a.append((n - PARK_A_TO_USE, op.src))
                self.parks_a.sort()
            self._a_consumers.append((op.src, n))
            op.slot, op.value_slot = n, n
            self.stage_reads.append((op.src, n))
            self.floor = n + JUMP_REL_SHADOW + 1
            return True

        self._search(max(read_lo, self.floor), None, attempt)

    def _op_jump_rel(self, op):
        if op.target not in self.ir.labels:
            raise ScheduleError(f"unknown jump target '{op.target}'")
        label = self._label_slot(op.target)

        def attempt(n):
            offset = label - n - JUMP_REL_RESUME
            cells = [(n, ["const->out4"], offset)]
            if not self._routes_fit(cells):
                return False
            # shadow n+1..n+4 must be empty and stay empty (floor handles later ops;
            # earlier ops may already sit there)
            if any(s in self.prog.slots for s in range(n + 1, n + 1 + JUMP_REL_SHADOW)):
                return False
            self._place_cells(cells, [], None, op)
            op.slot, op.value_slot = n, n
            self.floor = n + JUMP_REL_SHADOW + 1
            return True

        self._search(self.floor, None, attempt)

    def _op_halt(self, op):
        def attempt(n):
            cells = [(n, ["const->out4"], -JUMP_REL_RESUME)]
            if not self._routes_fit(cells):
                return False
            if any(s in self.prog.slots for s in range(n + 1, n + 1 + JUMP_REL_SHADOW)):
                return False
            self._place_cells(cells, [], None, op)
            op.slot, op.value_slot = n, n
            self.floor = n + JUMP_REL_SHADOW + 1
            return True

        # A halt is terminal: it must sit after EVERY slot placed so far, or
        # the spin traps the PC before earlier work has executed. The stage
        # floor alone is not enough — an op can legitimately be placed far past
        # it (a vector read waits out the zone's propagation), and the halt
        # would then be scheduled into the gap ahead of it.
        self._search(max(self.floor, self.prog.end() + 1), None, attempt)

    def _op_jump_if_zero(self, op):
        if op.target not in self.ir.labels:
            raise ScheduleError(f"unknown jump target '{op.target}'")
        pc = self.cfg.pc_addr
        dest = self._label_slot(op.target) - 1
        read_lo = self._read_lo(op.src)

        def attempt(m):
            if self._transient(op.src, m):
                return False
            park_cells = []
            if self._park_a_live(m) != op.src:
                pn = m - PARK_A_TO_USE
                if pn < 1:
                    return False
                for cell, mm in self._a_consumers:
                    if mm >= m:
                        return False
                park_cells = [(pn, ["const->out3"], op.src)]
            cells = park_cells + [(m, ["a->write_addr", "const->write_addr"], pc),
                                  (m + 2, ["const->write_value"], dest)]
            events, pair = [m + 2], (m + 2, m + 2)
            if not self._routes_fit(cells) or not self._latch_fits(events, pair):
                return False
            self._place_cells(cells, events, pair, op)
            if park_cells:
                self.parks_a.append((m - PARK_A_TO_USE, op.src))
                self.parks_a.sort()
            self._a_consumers.append((op.src, m))
            op.slot, op.value_slot = m, m + 2
            self.stage_reads.append((op.src, m))
            self.floor = m + 2 + JUMP_ABS_DELAY_SLOTS + 1
            return True

        self._search(max(read_lo, self.floor), None, attempt)

    def run(self):
        self._a_consumers: list[tuple[int, int]] = []
        label_indices = set(self.ir.labels.values())
        for i, op in enumerate(self.ir.ops):
            if i in label_indices:
                # a loop re-enters at the body's min footprint slot, so no earlier
                # op's footprint may extend into the body: a prelude value slot
                # captured by the loop would pair the PREVIOUS pass's latch
                # (measured: spin counter gained +2/pass before this floor)
                self.floor = max(self.floor, self.prog.end() + 1)
            self.place(op, i)
        # post: every slot encodes; jump shadows stayed empty by construction
        for slot, entry in self.prog.slots.items():
            encode(entry["routes"], entry["imm"] or 0)
        return self.prog


def schedule8(ir, name="", verbose=False):
    sched = _Sched8(ir, name)
    prog = sched.run()
    if verbose:
        print(f"schedule {name}: end slot {prog.end()}")
        for line in sched.manifest:
            print("  " + line)
    return prog, sched


if __name__ == "__main__":
    # self-test: fib_cmp in the v8 ISA — no slot numbers, port B streams I
    G, H, M, N_ = (address_of(f"signal-{s}") for s in "GHMN")
    I_, Q_ = address_of("signal-I"), address_of("signal-Q")
    W = address_of("signal-heart")

    ir = IR8()
    ir.write_imm(N_, 100, warm=True)
    ir.write_imm(H, 1)
    ir.park_b(I_)                 # B streams I = G+H for the whole program
    ir.barrier()
    ir.label("loop")
    # parallel stage; the stream read goes first so the greedy scheduler doesn't
    # box it in behind copy_a's G write (anti-dependency caps the I read at +2)
    ir.copy_b(H)                  # H <- old I (stream)
    ir.copy_a(H, G)               # G <- old H
    ir.barrier()
    ir.copy_a(H, M)               # M <- new H (port A still parked on H)
    ir.barrier()
    ir.jump_if_zero(Q_, "loop")   # parks A on Q; absolute write-path jump
    ir.write_imm(W, 999)
    ir.halt()

    prog, sched = schedule8(ir, name="fib_cmp_v8", verbose=True)
    rom = assemble_program(prog.rom_entries())
    print(f"  ROM words: {len(rom)}, loop re-enters at slot {sched._label_slot('loop')}")
