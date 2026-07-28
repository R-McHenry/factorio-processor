#!/usr/bin/env python3
"""vlang — the language for the v10 vector processor (LANGUAGE.md).

The fifth layer, and the only new code between the surface syntax and IR8:

    .fnet  --netlist.py-->  source.json          hardware
    vlang  ------------->   IR8 --schedule8-->   ROM      <-- this file
                            factorio_memory_tb.py         verification

Nothing below it changes. `schedule8` still owns every timing rule, and this
module emits ONLY `IR8.vec_move` / `vec_select` / `vec_read_lane` / `copy_a`,
never a vector control row — so the planned instruction decoder (LANGUAGE.md
§7) is a backend swap that no compiled program can notice.

WHAT THE MACHINE IS, FROM HERE (LANGUAGE.md §1). Not a register machine:

  * Operands live in FIXED SLOTS. `A_*` reads VREG0 x VREG1, `B_*` reads
    VREG2 x VREG3, every `VS_*` reads VREG0. You cannot ask for mul(r7, r9);
    you move operands into place. Instruction selection and register
    allocation are therefore the same problem, and they are the whole compiler.
  * Results are read by SELECTION, not written to a destination. An op output
    is a mux source, always computing; `dst = a*b` is "put a and b in the
    pair, park the mux on the product, commit to dst".
  * TWO SELECTIONS SUM on the write bus. Vector ADD costs no instruction at
    all, and neither does adding a scalar to every lane (via the BCAST
    generator). The matcher has to know that or it emits an op for something
    the wire does for free.

THREE HARDWARE FACTS THAT SHAPE THE OUTPUT (MANDELBROT.md §4):

  * A LANE THAT COMPUTES TO 0 VANISHES, and `each` against a fixed signal
    iterates only the FIRST operand's lanes — so a following vec-scal op can
    never put those lanes back. Offsets are therefore compiled as an ADDEND on
    the second selector against BCAST, never as a VS_SUB after a multiply:
    `a - k` is canonicalised to `a + (-k)` at DAG-build time and lands as a
    dual selection. That is not an optimisation, it is the correctness rule
    that cost a live run.
  * AN INSTRUCTION IMMEDIATE IS 20 BITS SIGNED. Every broadcast constant is
    range-checked here rather than silently truncated in the ROM.
  * A DESTINATION MAY NOT BE AN OPERAND OF ITS OWN UNIT. `vec_move` erases the
    destination and only then pulses the write select, so `V0 <- VS_DIV` would
    read an already-emptied VREG0 and commit zero. The allocator refuses those
    scratch destinations and falls back to a live register — which is exactly
    why the hand-written grid seed needs its temporary.

The surface is a Python DSL (LANGUAGE.md §2, decided): expression trees come
free from operator overloading, so all the effort lands on selection and
allocation. A text front-end onto this same IR is a later, optional bolt-on.
"""
import sys
from dataclasses import dataclass, field

from machine_v8 import IR8

# ---------------------------------------------------------------------------
# The mux index map — the single source of truth for it, mirroring
# modules/v10_op_farm.fnet's blocked layout (registers 1..16, pair A 17..24,
# pair B 25..32, vec-scal 33..44, generators and I/O 45..52). Programs and
# bench builders import these names rather than carrying bare numbers.
# ---------------------------------------------------------------------------


def SRC_VREG(k):
    """Mux index (and write-block id) of VREGk."""
    return k + 1


A_MUL, A_SUB, A_MAX, A_MIN, A_DIV, A_MOD = 17, 18, 19, 20, 21, 22
B_MUL, B_SUB, B_SQ, B_SQG, B_SQDIFF = 25, 26, 27, 28, 29
VS_MUL, VS_ADD, VS_SUB, VS_DIV, VS_MOD, VS_GT, VS_LT = 33, 34, 35, 36, 37, 38, 39
ONES, BCAST, COORD, LANE_IN = 45, 46, 47, 48

UNIT_NAME = {
    A_MUL: "A_MUL", A_SUB: "A_SUB", A_MAX: "A_MAX", A_MIN: "A_MIN",
    A_DIV: "A_DIV", A_MOD: "A_MOD",
    B_MUL: "B_MUL", B_SUB: "B_SUB", B_SQ: "B_SQ", B_SQG: "B_SQG",
    B_SQDIFF: "B_SQDIFF",
    VS_MUL: "VS_MUL", VS_ADD: "VS_ADD", VS_SUB: "VS_SUB", VS_DIV: "VS_DIV",
    VS_MOD: "VS_MOD", VS_GT: "VS_GT", VS_LT: "VS_LT",
    ONES: "ONES", BCAST: "BCAST", COORD: "COORD", LANE_IN: "LANE_IN",
}

# Which VREGs a unit READS. A destination may never be one of them (see the
# module docstring), and this is also what makes the pair-B load shareable:
# B_SQ, B_SQG, B_SQDIFF and B_MUL all read {2, 3}, so one load feeds all four.
UNIT_READS = {
    A_MUL: (0, 1), A_SUB: (0, 1), A_MAX: (0, 1), A_MIN: (0, 1),
    A_DIV: (0, 1), A_MOD: (0, 1),
    B_MUL: (2, 3), B_SUB: (2, 3), B_SQ: (2, 3), B_SQG: (2, 3),
    B_SQDIFF: (2, 3),
    VS_MUL: (0,), VS_ADD: (0,), VS_SUB: (0,), VS_DIV: (0,), VS_MOD: (0,),
    VS_GT: (0,), VS_LT: (0,),
}

SCRATCH = (0, 1, 2, 3)              # VREG0..VREG3: operand slots, never variables
LIVE_REGS = tuple(range(4, 12))     # VREG4..VREG11: the eight variable homes

IMM_MIN, IMM_MAX = -(1 << 19), (1 << 19) - 1


class CompileError(Exception):
    pass


def _check_imm(k, what):
    if not (IMM_MIN <= k <= IMM_MAX):
        raise CompileError(
            f"{what} {k} does not fit a 20-bit signed instruction immediate "
            f"([{IMM_MIN}, {IMM_MAX}]) — this is what caps a fixed-point scale, "
            "not precision appetite (MANDELBROT.md §4)")
    return k


# ---------------------------------------------------------------------------
# 1. The DAG (LANGUAGE.md §4a)
# ---------------------------------------------------------------------------
# Hash-consed, so `zx*zx` written in two statements is ONE node. That sharing
# is the whole efficiency of the mandelbrot kernel: B_SQ, B_SQG, B_SQDIFF and
# B_MUL are four different units reading one pair load, and only a shared DAG
# lets the allocator see that the pair is already in place. A tree-walker would
# reload the pair four times.

_COMMUTATIVE = frozenset({"add", "mul", "max", "min"})
_LEAF_OPS = frozenset({"var", "coord", "ones", "const"})


class Node:
    __slots__ = ("op", "args", "k", "name", "uid", "block")

    def __init__(self, op, args, k, name, uid, block):
        self.op, self.args, self.k = op, args, k
        self.name, self.uid, self.block = name, uid, block

    @property
    def is_const(self):
        return self.op == "const"

    def label(self):
        if self.op == "const":
            return f"#{self.k}"
        if self.op in ("coord", "ones"):
            return self.op.upper()
        return self.name or f"t{self.uid}"

    def __repr__(self):
        if self.op in _LEAF_OPS:
            return self.label()
        return f"{self.op}({', '.join(a.label() for a in self.args)})"


def dag_text(roots):
    """A let-form rendering that makes SHARING VISIBLE — which is what the DAG
    tests actually assert. Every interior node appears exactly once, named, so
    reuse shows up as a repeated name rather than a repeated subtree."""
    order, seen = [], set()

    def walk(n):
        if n.uid in seen or n.op in _LEAF_OPS:
            return
        seen.add(n.uid)
        for a in n.args:
            walk(a)
        order.append(n)

    for r in roots:
        walk(r.node if isinstance(r, Vec) else r)
    return "\n".join(
        f"{n.label()} = {n.op}({', '.join(a.label() for a in n.args)})"
        for n in order)


class Vec:
    """A vector value: 2451 lanes, one DAG node. Immutable — an operator makes
    a new Vec, and rebinding the Python name is what an assignment is."""

    __slots__ = ("m", "node")

    def __init__(self, m, node):
        self.m, self.node = m, node

    def named(self, name):
        """Give the node a name, for listings and error messages."""
        if self.node.name is None:
            self.node.name = name
        return self

    def __mul__(self, o):
        return self.m._bin("mul", self, o)
    __rmul__ = __mul__

    def __add__(self, o):
        return self.m._bin("add", self, o)
    __radd__ = __add__

    def __sub__(self, o):
        # `a - k` becomes `a + (-k)` HERE, not in the matcher: a subtrahend
        # compiles to VS_SUB, which cannot reach a lane that computed to zero,
        # while an addend rides the second selector and can.
        return self.m._bin("sub", self, o)

    def __rsub__(self, o):
        # `k - a` = `a*-1 + k`: one move through VS_MUL || BCAST, and it
        # resurrects vanished lanes for the same reason.
        return self.m._bin("add", self.m._bin("mul", self, -1), o)

    def __neg__(self):
        return self.m._bin("mul", self, -1)

    def __floordiv__(self, o):
        return self.m._bin("div", self, o)

    def __mod__(self, o):
        return self.m._bin("mod", self, o)

    def __gt__(self, o):
        return self.m._bin("gt", self, o)

    def __lt__(self, o):
        return self.m._bin("lt", self, o)

    def __repr__(self):
        return f"Vec({self.node!r})"


def vmax(a, b):
    """Elementwise max — A_MAX. Also the sticky-OR idiom for masks."""
    return a.m._bin("max", a, b)


def vmin(a, b):
    """Elementwise min — A_MIN."""
    return a.m._bin("min", a, b)


# ---------------------------------------------------------------------------
# 2. Instruction selection (LANGUAGE.md §4b)
# ---------------------------------------------------------------------------
# A pattern names the unit(s), the broadcast immediates, and WHICH REGISTER
# SLOTS its operands must occupy. Bigger patterns are tried first — ordinary
# maximal munch — so `a*a - b*b` beats `a*a`, and `a*k1 + k2` beats `a*k1`.

@dataclass
class Insn:
    """One committed value: `dest <- sel1 + sel2`, with `reads` naming the
    operand slots the units require. `dest` is filled in by the allocator."""
    value: "Node"
    sel1: tuple                     # ('unit', idx) | ('src', idx) | ('val', Node)
    sel2: tuple | None
    bcast: int | None
    bcast2: int | None
    reads: dict                     # slot -> Node
    accumulate: bool = False
    dest: tuple | None = None       # ('slot', s) | ('stor', Storage)

    def units(self):
        return tuple(s[1] for s in (self.sel1, self.sel2)
                     if s is not None and s[0] in ("unit", "src"))

    def unit_text(self):
        def one(sel):
            kind, x = sel
            return UNIT_NAME.get(x, str(x)) if kind in ("unit", "src") else x.label()
        text = "|".join(one(s) for s in (self.sel1, self.sel2) if s is not None)
        if self.bcast is not None:
            text += f"[{self.bcast}]"
        if self.bcast2 is not None:
            text += f"{{{self.bcast2}}}"
        if self.accumulate:
            text += "+="
        return text

    def triple(self):
        """The abstract `(unit, operands, dest)` triple of LANGUAGE.md §5 step
        3 — what the selection tests assert against."""
        return (self.unit_text(),
                tuple(f"V{s}={self.reads[s].label()}" for s in sorted(self.reads)),
                self.value.label())


_VS_UNIT = {"mul": VS_MUL, "add": VS_ADD, "sub": VS_SUB, "div": VS_DIV,
            "mod": VS_MOD, "gt": VS_GT, "lt": VS_LT}
_A_UNIT = {"mul": A_MUL, "sub": A_SUB, "div": A_DIV, "mod": A_MOD,
           "max": A_MAX, "min": A_MIN}


def _sel_of(n):
    """How a materialised value names itself to the mux."""
    if n.op == "coord":
        return ("src", COORD)
    if n.op == "ones":
        return ("src", ONES)
    if n.is_const:
        raise CompileError("a bare constant has no mux source; the matcher "
                           "folds constants into BCAST")
    return ("val", n)


class _Selector:
    """Maximal-munch selection over the DAG, walked in DAG order."""

    def __init__(self, block_index):
        self.block = block_index
        self.insns: list[Insn] = []
        self.done: set = set()

    def value(self, node):
        """Materialise `node`: after this it is nameable to the mux, either as
        a generator source, a live-in register, or a fresh Insn."""
        if node.uid in self.done:
            return node
        if node.is_const:
            raise CompileError("a constant is not a vector on its own; combine "
                               "it with a vector (`v + k`, `v*k + j`)")
        self.done.add(node.uid)
        if node.op in _LEAF_OPS or node.block < self.block:
            return node                     # generator, variable, or live-in
        sel1, sel2, bcast, bcast2, reads = self._match(node)
        # SELECTOR operands first, then SLOT operands. A slot operand computed
        # afterwards would clobber the very slot this Insn is about to read:
        # `zx' = sqdiff/S + cx` must have cx in place BEFORE sqdiff lands in
        # VREG0, or evaluating the second selection destroys the first.
        for sel in (sel1, sel2):
            if sel is not None and sel[0] == "val":
                self.value(sel[1])
        for slot in sorted(reads):
            self.value(reads[slot])
        self.insns.append(Insn(node, sel1, sel2, bcast, bcast2, reads))
        return node

    # -- opacity ------------------------------------------------------------
    def _opaque(self, n):
        """A node a pattern may NOT look inside.

        Generators and variables obviously, but also anything from an EARLIER
        BLOCK: across a block a value exists only as a register, so its
        defining expression is not available to munch. Getting this wrong is
        not a missed optimisation — it silently rebuilt `cx = x*DX - OX` as
        the outer half of `zx' = .../S + cx` and then asked for a register
        holding `x*DX`, which nothing had computed."""
        return n.op in _LEAF_OPS or n.block < self.block

    def _square_of(self, n):
        """The squared operand if `n` is `a*a`, else None."""
        if not self._opaque(n) and n.op == "mul" and n.args[0] is n.args[1] \
                and not n.args[0].is_const:
            return n.args[0]
        return None

    def _vs_form(self, n):
        """(unit, operand, immediate) if `n` is a vec-scal op, else None."""
        if not self._opaque(n) and n.op in _VS_UNIT and len(n.args) == 2 \
                and n.args[1].is_const and not n.args[0].is_const:
            return _VS_UNIT[n.op], n.args[0], n.args[1].k
        return None

    # -- the pattern table --------------------------------------------------
    def _match(self, n):
        op, args = n.op, n.args

        if op == "add":
            a, b = args
            for x, y in ((a, b), (b, a)):
                # a*a + b*b -> B_SQ || B_SQG. The escape radius for free: two
                # selections sum, so the add costs no instruction at all.
                sx, sy = self._square_of(x), self._square_of(y)
                if sx is not None and sy is not None and sx is not sy:
                    return (("unit", B_SQ), ("unit", B_SQG), None, None,
                            {2: sx, 3: sy})
            for x, y in ((a, b), (b, a)):
                # vsop(v,k) + w -> the vec-scal unit || w. One move for a
                # multiply-accumulate; this is the mandelbrot z update.
                form = self._vs_form(x)
                if form and not y.is_const:
                    unit, operand, imm = form
                    return (("unit", unit), _sel_of(y),
                            _check_imm(imm, "broadcast operand"), None,
                            {0: operand})
            for x, y in ((a, b), (b, a)):
                # vsop(v,k1) + k2 -> the vec-scal unit || BCAST. THE LANE
                # RESURRECTION SHAPE: a lane the op dropped is restored by the
                # summed constant frame. Every coordinate seed has this form.
                form = self._vs_form(x)
                if form and y.is_const:
                    unit, operand, imm = form
                    return (("unit", unit), ("src", BCAST),
                            _check_imm(imm, "broadcast operand"),
                            _check_imm(y.k, "broadcast addend"), {0: operand})
            for x, y in ((a, b), (b, a)):
                # v + k -> dual selection against BCAST. ZERO operand
                # placement: v never has to be moved into a slot at all.
                if y.is_const and not x.is_const:
                    return (_sel_of(x), ("src", BCAST), None,
                            _check_imm(y.k, "broadcast addend"), {})
            # v + w -> dual selection. Vector ADD is free; no unit exists.
            return (_sel_of(a), _sel_of(b), None, None, {})

        if op == "sub":
            a, b = args
            # a*a - b*b -> B_SQDIFF, off the same pair load as B_SQ/B_SQG
            sa, sb = self._square_of(a), self._square_of(b)
            if sa is not None and sb is not None and sa is not sb:
                return (("unit", B_SQDIFF), None, None, None, {2: sa, 3: sb})
            if a.is_const or b.is_const:
                raise CompileError("constant subtraction should have been "
                                   "canonicalised to an addend — selector bug")
            return (("unit", A_SUB), None, None, None, {0: a, 1: b})

        if op == "mul":
            a, b = args
            if b.is_const:
                return (("unit", VS_MUL), None,
                        _check_imm(b.k, "broadcast multiplier"), None, {0: a})
            if a is b:
                return (("unit", B_SQ), None, None, None, {2: a})
            return (("unit", B_MUL), None, None, None, {2: a, 3: b})

        if op in ("div", "mod", "gt", "lt"):
            a, b = args
            if b.is_const:
                return (("unit", _VS_UNIT[op]), None,
                        _check_imm(b.k, f"broadcast operand of `{op}`"), None,
                        {0: a})
            if op in ("gt", "lt"):
                raise CompileError(
                    "vector-vs-vector comparison has no unit; compare against a "
                    "constant, or use vmax/vmin for the elementwise choice")
            return (("unit", _A_UNIT[op]), None, None, None, {0: a, 1: b})

        if op in ("max", "min"):
            return (("unit", _A_UNIT[op]), None, None, None,
                    {0: args[0], 1: args[1]})

        raise CompileError(f"no pattern for `{op}`")


# ---------------------------------------------------------------------------
# 3. Register allocation (LANGUAGE.md §4c)
# ---------------------------------------------------------------------------

class Storage:
    """A variable home: one of VREG4..VREG11 once coloured."""
    __slots__ = ("name", "reg", "first", "last", "carried", "from_start")

    def __init__(self, name, from_start=False):
        self.name, self.reg = name, None
        self.first = self.last = None
        self.carried, self.from_start = False, from_start

    def touch(self, index):
        self.first = index if self.first is None else min(self.first, index)
        self.last = index if self.last is None else max(self.last, index)

    def __repr__(self):
        return f"<{self.name}{'' if self.reg is None else f'=VREG{self.reg}'}>"


@dataclass
class VMove:
    """One `IR8.vec_move`. Dests and selectors stay abstract until colouring."""
    dest: tuple
    sel1: tuple
    sel2: tuple | None = None
    bcast: int | None = None
    bcast2: int | None = None
    accumulate: bool = False
    value: "Node" = None            # what the destination holds afterwards
    reads: tuple = ()               # (ref, Node) pairs this move consumes

    def describe(self, resolve):
        s2 = "" if self.sel2 is None else f", s={_srcname(resolve(self.sel2))}"
        b1 = "" if self.bcast is None else f", bcast={self.bcast}"
        b2 = "" if self.bcast2 is None else f", bcast2={self.bcast2}"
        acc = ", accumulate" if self.accumulate else ""
        note = f"   # {self.value.label()}" if self.value is not None else ""
        return (f"{_srcname(resolve(self.dest))} <- "
                f"{_srcname(resolve(self.sel1))}{s2}{b1}{b2}{acc}{note}")


def _srcname(idx):
    return UNIT_NAME.get(idx) or f"VREG{idx - 1}"


@dataclass
class IOItem:
    """A scalar-side action inside a block: a reduction, a lane extraction, or
    a bare re-park. `need` is the value whose frame it wants standing on the
    write bus."""
    kind: str          # 'reduce_sum' | 'reduce_count' | 'read_lane' | 'park'
    need: "Node"
    dst: int | None = None
    lane: int | None = None
    order: int = 0


@dataclass
class _Block:
    kind: str                       # 'straight' | 'loop'
    index: int
    count: int = 0                  # loop trip count
    roots: list = field(default_factory=list)
    carries: list = field(default_factory=list)    # (old Node, new Node)
    ios: list = field(default_factory=list)
    stream: list = field(default_factory=list)     # Insn / IOItem, in order
    items: list = field(default_factory=list)      # VMove / IOItem, in order
    span: tuple = (0, 0)                           # index range in the flat list


# ---------------------------------------------------------------------------
# 4. The machine / program object
# ---------------------------------------------------------------------------

class Machine:
    """The DSL surface. Build values with operators, delimit a loop body with
    `with m.loop(n):`, read results out with the IO methods, then `emit(ir)`.

    LOOP-CARRIED STATE IS FOUND BY REBINDING. `zx, zy = ..., ...` inside the
    body is an ordinary Python assignment, so the machine snapshots the
    caller's Vec locals on entry and diffs them on exit; a name whose Vec
    object changed is loop-carried, and its register is written at the bottom
    of the body. That keeps the surface exactly as LANGUAGE.md §2 sketched it,
    with no explicit `carry(...)` declarations.
    """

    def __init__(self, cfg, rows, name="vprog"):
        self.cfg, self.rows, self.name = cfg, rows, name
        self._uid = 0
        self._pool: dict = {}
        self.blocks = [_Block("straight", 0)]
        self.cur = 0
        self._storage: dict = {}          # node uid -> Storage
        self._loops = 0
        self._compiled = False

    # -- node construction --------------------------------------------------
    def _tick(self):
        self._uid += 1
        return self._uid

    def _node(self, op, args=(), k=None, name=None):
        if op in _COMMUTATIVE and len(args) == 2:
            # constants last, then creation order: a canonical form, so `zx*zx`
            # written in two statements hash-conses to one node
            args = tuple(sorted(args, key=lambda n: (n.is_const, n.uid)))
        key = (op, tuple(a.uid for a in args), k)
        hit = self._pool.get(key)
        if hit is not None:
            return hit
        node = Node(op, args, k, name, self._tick(), self.cur)
        self._pool[key] = node
        return node

    def _const(self, k):
        if not isinstance(k, int) or isinstance(k, bool):
            raise CompileError(
                f"vector operands are integers; got {k!r} — fixed point is "
                "explicit in this language, so write the //S yourself")
        # Checked HERE rather than at selection, because every constant in this
        # language ends up as a broadcast immediate and the error is only
        # useful if it points at the line that wrote the number.
        _check_imm(k, "constant")
        return self._node("const", (), k)

    def _as_node(self, x):
        return x.node if isinstance(x, Vec) else self._const(x)

    def _bin(self, op, a, b):
        an, bn = self._as_node(a), self._as_node(b)
        if op == "sub" and bn.is_const:
            # THE CANONICALISATION THAT MATTERS: an offset must arrive as an
            # addend on the second selector, never as a VS_SUB after the op
            # that dropped the lane (MANDELBROT.md §4).
            return Vec(self, self._node("add", (an, self._const(-bn.k))))
        return Vec(self, self._node(op, (an, bn)))

    # -- value constructors -------------------------------------------------
    def vec(self, name=None):
        """A live vector variable. Registers come up EMPTY and absent is zero
        on a Factorio wire, so an unassigned `vec()` is a zero frame — which is
        exactly how mandelbrot's z starts, with no initialisation code."""
        node = Node("var", (), None, name, self._tick(), self.cur)
        st = Storage(name or f"var{node.uid}", from_start=True)
        self._storage[node.uid] = st
        return Vec(self, node)

    def coord(self):
        """COORD — every lane holds its own index, 1..N. A whole coordinate
        ramp in one selection, with zero scalar writes."""
        return Vec(self, self._node("coord"))

    def ones(self):
        """ONES — 1 in every lane, including lanes an op dropped."""
        return Vec(self, self._node("ones"))

    def storage_for(self, node):
        st = self._storage.get(node.uid)
        if st is None:
            st = Storage(node.label())
            self._storage[node.uid] = st
        return st

    # -- control flow -------------------------------------------------------
    def loop(self, count):
        """`with m.loop(n):` — the counted loop of LANGUAGE.md §4e.

        The counter runs through the ALU (`add_res = add_a + 1`) rather than
        the accumulate cell at row 300, because a long program's ROM rows
        SHADOW low memory and port A reads the memory bus (MANDELBROT.md §4).
        """
        if self._loops:
            raise CompileError("v1 compiles exactly one loop per program "
                               "(LANGUAGE.md §5 step 7)")
        return _LoopCtx(self, count)

    # -- scalar-side IO -----------------------------------------------------
    def _io(self, item):
        item.order = self._tick()
        self.blocks[self.cur].ios.append(item)

    def reduce_sum(self, v, dst_row):
        """`vred_sum` of a whole frame onto a scalar row."""
        self._io(IOItem("reduce_sum", v.node, dst=dst_row))

    def reduce_count(self, v, dst_row):
        """`vred_count` — how many lanes are present, not how much."""
        self._io(IOItem("reduce_count", v.node, dst=dst_row))

    def read_lane(self, v, lane, dst_row):
        """One lane onto a scalar row. Slow by construction — a whole image row
        is one read per lane — so this is the readback path, not a data path."""
        self._io(IOItem("read_lane", v.node, dst=dst_row, lane=lane))

    def park(self, v):
        """Leave the mux standing on `v`. The frame then stays on the write bus
        for as long as the machine runs, which is what keeps a display lit."""
        self._io(IOItem("park", v.node))

    # =======================================================================
    # compilation
    # =======================================================================
    def compile(self):
        """Select, allocate, audit. Returns self, with `blocks[*].items` filled
        in and every Storage coloured."""
        if self._compiled:
            raise CompileError("compile() is not idempotent; build a new Machine")
        self._compiled = True
        self._resolve_roots()
        for blk in self.blocks:
            self._select_block(blk)
        self._allocate()
        return self

    # -- root discovery ------------------------------------------------------
    def _boundary(self, node, block_index, out):
        """Nodes from an EARLIER block reachable from `node` — the live-ins.
        Descent stops at a boundary: across a block a value lives in a
        register, never as an expression."""
        if node.block < block_index:
            if node.op not in ("coord", "ones", "const"):
                out.add(node.uid)
            return
        for a in node.args:
            self._boundary(a, block_index, out)

    def _resolve_roots(self):
        by_uid = {}

        def index(n):
            if n.uid in by_uid:
                return
            by_uid[n.uid] = n
            for a in n.args:
                index(a)

        for blk in self.blocks:
            for io in blk.ios:
                index(io.need)
            for old, new in blk.carries:
                index(old)
                index(new)

        # Later blocks first: each block's live-ins become roots of whichever
        # earlier block produced them.
        live_in = {b.index: set() for b in self.blocks}
        for blk in reversed(self.blocks):
            wanted = [io.need for io in blk.ios]
            wanted += [new for _old, new in blk.carries]
            wanted += [by_uid[u] for u in sorted(live_in[blk.index])]
            found = set()
            for node in wanted:
                self._boundary(node, blk.index, found)
            for u in found:
                live_in[by_uid[u].block].add(u)

        for blk in self.blocks:
            roots = {new.uid: new for _old, new in blk.carries}
            for u in live_in[blk.index]:
                node = by_uid[u]
                if node.block == blk.index:
                    roots[u] = node
            blk.roots = [roots[u] for u in sorted(roots)]

    # -- selection -----------------------------------------------------------
    def _select_block(self, blk):
        """Roots and IO actions are merged in DAG order, so the emitted body
        follows the order the statements were written: `zx, zy = A, B`
        evaluates A completely before B, and their node uids say so."""
        sel = _Selector(blk.index)
        agenda = ([(r.uid, "root", r) for r in blk.roots]
                  + [(io.order, "io", io) for io in blk.ios])
        for _order, kind, thing in sorted(agenda, key=lambda t: t[0]):
            sel.value(thing if kind == "root" else thing.need)
            blk.stream.extend(sel.insns)
            sel.insns = []
            if kind == "io":
                blk.stream.append(thing)

    # -- allocation ----------------------------------------------------------
    def _allocate(self):
        # A carried value shares ONE storage with the variable it updates, so
        # the update writes the loop's register directly and the copy over the
        # back edge costs nothing at all.
        for blk in self.blocks:
            for old, new in blk.carries:
                st = self.storage_for(old)
                st.carried = True
                self._storage[new.uid] = st

        uses = self._collect_uses()
        for blk in self.blocks:
            self._assign_dests(blk, uses)
        flat = []
        for blk in self.blocks:
            start = len(flat)
            blk.items = self._emit_block(blk)
            flat.extend(blk.items)
            blk.span = (start, len(flat))
        self._colour(flat)
        self._audit()

    def _collect_uses(self):
        """value uid -> [(block index, position in stream, slot or None)]."""
        uses = {}
        for blk in self.blocks:
            for pos, item in enumerate(blk.stream):
                if isinstance(item, IOItem):
                    uses.setdefault(item.need.uid, []).append((blk.index, pos, None))
                    continue
                for sel in (item.sel1, item.sel2):
                    if sel is not None and sel[0] == "val":
                        uses.setdefault(sel[1].uid, []).append((blk.index, pos, None))
                for slot, node in item.reads.items():
                    uses.setdefault(node.uid, []).append((blk.index, pos, slot))
        return uses

    def _assign_dests(self, blk, uses):
        """THE PEEPHOLE THAT PAYS (LANGUAGE.md §4c): a value whose only use is
        an operand slot is computed STRAIGHT INTO that slot, so the operand
        move disappears. Worth ~2 moves per statement in the mandelbrot loop —
        without it every op result lands in a variable and is moved back."""
        claims = []                                   # (slot, from, to)
        insn_at = {pos: it for pos, it in enumerate(blk.stream)
                   if isinstance(it, Insn)}
        for pos, insn in sorted(insn_at.items()):
            st = self._storage.get(insn.value.uid)
            if st is not None and st.carried:
                insn.dest = ("stor", st)              # a carried variable
                self._fix_self_reference(insn, st)
                continue
            slot = self._scratch_dest(blk, insn, pos, uses.get(insn.value.uid, []),
                                      claims, insn_at)
            if slot is not None:
                insn.dest = ("slot", slot)
                claims.append((slot, pos, uses[insn.value.uid][0][1]))
            else:
                dest_st = self.storage_for(insn.value)
                insn.dest = ("stor", dest_st)
                self._fix_self_reference(insn, dest_st)

    def _fix_self_reference(self, insn, st):
        """`X = X + Y` cannot be a dual selection: `vec_move` ERASES X before
        pulsing the write select, so the bus would show 0 + Y. It is an
        ACCUMULATE instead — a write with no erase — which is how a per-lane
        counter works with no adder anywhere in the machine."""
        hits = [sel for sel in (insn.sel1, insn.sel2)
                if sel is not None and sel[0] == "val"
                and self._storage.get(sel[1].uid) is st]
        if not hits:
            return
        if insn.value.op != "add" or len(hits) != 1 or insn.reads:
            raise CompileError(
                f"{st.name} is both the destination and an operand of "
                f"`{insn.value.op}`; only `x = x + y` has a hardware form "
                "(accumulate — a write with no erase)")
        other = insn.sel2 if hits[0] is insn.sel1 else insn.sel1
        insn.sel1, insn.sel2, insn.accumulate = other, None, True

    def _scratch_dest(self, blk, insn, pos, u, claims, insn_at):
        if len(u) != 1:
            return None
        ublock, upos, slot = u[0]
        if slot is None or ublock != blk.index:
            return None
        # A DESTINATION MAY NOT BE AN OPERAND OF ITS OWN UNIT: vec_move erases
        # the destination before pulsing W, so `V0 <- VS_DIV` reads an emptied
        # VREG0 and commits zero. This is what forces the grid seed's temp.
        for unit in insn.units():
            if slot in UNIT_READS.get(unit, ()):
                return None
        for cslot, cfrom, cto in claims:
            if cslot == slot and not (cto <= pos or upos <= cfrom):
                return None
        for between in range(pos + 1, upos):
            other = insn_at.get(between)
            if other is not None and slot in other.reads:
                return None
        return slot

    def _emit_block(self, blk):
        """Forward simulation: place operands, then commit. THE OTHER HALF OF
        THE PEEPHOLE is the `is` test below — a slot already holding the wanted
        value needs no move, which is why ONE pair load feeds B_SQ, B_SQG,
        B_SQDIFF and B_MUL across four instructions.

        Scratch state does NOT survive a block boundary. At the top of the loop
        body VREG2 physically holds the PREVIOUS pass's zx, so carrying the
        state across the back edge would read stale lanes on every pass but the
        first."""
        items, slot_state = [], {}
        for item in blk.stream:
            if isinstance(item, IOItem):
                items.append(item)
                continue
            for slot in sorted(item.reads):
                node = item.reads[slot]
                if slot_state.get(slot) is node:
                    continue
                src = self._source(node)
                items.append(VMove(("slot", slot), src, value=node,
                                   reads=((src, node),)))
                slot_state[slot] = node
            sel1 = self._resolve_sel(item.sel1)
            sel2 = None if item.sel2 is None else self._resolve_sel(item.sel2)
            reads = tuple((ref, sel[1])
                          for ref, sel in ((sel1, item.sel1), (sel2, item.sel2))
                          if sel is not None and sel[0] == "val")
            items.append(VMove(item.dest, sel1, sel2, item.bcast, item.bcast2,
                               item.accumulate, value=item.value, reads=reads))
            if item.dest[0] == "slot":
                slot_state[item.dest[1]] = item.value
        items.extend(self._carry_copies(blk))
        return items

    def _carry_copies(self, blk):
        """A carried update whose producing Insn could not take the loop's
        register directly needs an explicit copy over the back edge."""
        out = []
        for old, new in blk.carries:
            st = self._storage[old.uid]
            if new is old or any(isinstance(it, Insn) and it.value is new
                                 and it.dest == ("stor", st)
                                 for it in blk.stream):
                continue
            src = self._source(new)
            out.append(VMove(("stor", st), src, value=new, reads=((src, new),)))
        return out

    def _resolve_sel(self, sel):
        kind, x = sel
        return ("unit", x) if kind in ("unit", "src") else self._source(x)

    def _source(self, node):
        if node.op == "coord":
            return ("unit", COORD)
        if node.op == "ones":
            return ("unit", ONES)
        st = self._storage.get(node.uid)
        if st is None:
            raise CompileError(
                f"{node.label()} has no register home — it was absorbed into a "
                "pattern and then wanted as a value; selector bug")
        return ("stor", st)

    def _colour(self, flat):
        """Linear scan over the finished move list. VREG0..VREG3 are never
        candidates (operand placement clobbers them constantly), so eight
        registers hold every variable — and SPILLING IS A HARD ERROR, because
        the only spill path is `vec_read_lane`, one lane at a time."""
        loop_span = next((b.span for b in self.blocks if b.kind == "loop"), None)

        seen = []
        for i, item in enumerate(flat):
            if isinstance(item, IOItem):
                st = self._storage.get(item.need.uid)
                if st is not None:
                    self._touch(st, i, seen)
                continue
            for ref in (item.dest, item.sel1, item.sel2):
                if ref is not None and ref[0] == "stor":
                    self._touch(ref[1], i, seen)

        for st in seen:
            if st.from_start:
                # a variable's register is claimed from program start: it holds
                # the empty frame the program relies on, so nothing else may
                # borrow it before the first write
                st.first = 0
        if loop_span:
            lo, hi = loop_span
            for st in seen:
                if st.carried or (st.first < hi and st.last >= lo):
                    st.first, st.last = min(st.first, lo), max(st.last, hi - 1)

        active = []
        for st in sorted(seen, key=lambda s: (s.first, s.last, s.name)):
            active = [a for a in active if a.last >= st.first]
            taken = {a.reg for a in active}
            free = [r for r in LIVE_REGS if r not in taken]
            if not free:
                names = ", ".join(sorted({a.name for a in active} | {st.name}))
                raise CompileError(
                    f"out of vector registers holding {{{names}}} — "
                    f"{len(LIVE_REGS)} live variables is the limit, and the only "
                    "spill path is vec_read_lane one lane at a time, so this is a "
                    "hard error rather than a silent catastrophe (LANGUAGE.md §4c)")
            st.reg = free[0]
            active.append(st)

    @staticmethod
    def _touch(st, i, seen):
        if st.first is None:
            seen.append(st)
        st.touch(i)

    def _audit(self):
        """Independent re-check of the allocation, on the finished move list.

        The failure this catches is the one that would be silent in-game: a
        register overwritten between a value's definition and its last read, so
        the move that looks like `V2 <- zx` actually carries something else.
        Same belt-and-braces shape as `check_vector_hazards` — one source of
        truth, checked twice."""
        for blk in self.blocks:
            holding = {}                    # Storage -> Node it currently holds
            for item in blk.items:
                if isinstance(item, IOItem):
                    st = self._storage.get(item.need.uid)
                    self._audit_read(st, item.need, blk)
                    continue
                for ref, node in item.reads:
                    if ref[0] == "stor":
                        self._audit_read(ref[1], node, blk, holding)
                if item.dest[0] == "stor":
                    holding[item.dest[1]] = item.value
            self._holding = holding

    def _audit_read(self, st, node, blk, holding=None):
        if st is None:
            return
        held = (holding or {}).get(st)
        if held is not None and held is not node:
            raise CompileError(
                f"block {blk.index}: reading {node.label()} out of VREG{st.reg}, "
                f"which now holds {held.label()} — allocation bug")

    # -- resolution ----------------------------------------------------------
    def resolve(self, ref):
        kind, x = ref
        if kind == "unit":
            return x
        if kind == "slot":
            return SRC_VREG(x)
        if x.reg is None:
            raise CompileError(f"{x.name} was never coloured")
        return SRC_VREG(x.reg)

    # =======================================================================
    # emission
    # =======================================================================
    def moves(self, block_index):
        """The VMoves of one block — what the move-count tests count."""
        return [it for it in self.blocks[block_index].items
                if isinstance(it, VMove)]

    def loop_block(self):
        return next((b for b in self.blocks if b.kind == "loop"), None)

    def listing(self):
        out = []
        for blk in self.blocks:
            head = f"-- block {blk.index} ({blk.kind}"
            out.append(head + (f" x{blk.count})" if blk.kind == "loop" else ")"))
            for it in blk.items:
                if isinstance(it, VMove):
                    out.append("   " + it.describe(self.resolve))
                else:
                    lane = "" if it.lane is None else f"[{it.lane}]"
                    tail = "" if it.dst is None else f" -> mem[{it.dst}]"
                    out.append(f"   {it.kind} {it.need.label()}{lane}{tail}")
        return "\n".join(out)

    def emit(self, ir=None, halt=True, label="vloop"):
        """Lower to IR8. NO TIMING CODE LIVES HERE: `vec_move` is the isolation
        layer and `schedule8` places everything."""
        ir = ir or IR8(self.cfg)
        rows = self.rows
        loop = self.loop_block()
        if loop is not None:
            ir.write_imm(rows["cmp_n"], loop.count, warm=True)
            ir.write_imm(rows["add_b"], 1, warm=True)
            ir.barrier()

        park = None
        for blk in self.blocks:
            if blk.kind == "loop":
                ir.barrier()
                ir.label(label)
            for it in blk.items:
                if isinstance(it, VMove):
                    sel1 = self.resolve(it.sel1)
                    sel2 = None if it.sel2 is None else self.resolve(it.sel2)
                    ir.vec_move(self.resolve(it.dest), sel1, src2=sel2,
                                bcast=it.bcast, bcast2=it.bcast2,
                                accumulate=it.accumulate)
                    park = (sel1, sel1 if sel2 is None else sel2)
                else:
                    park = self._emit_io(ir, it, park)
            if blk.kind == "loop":
                # loop counter through the ALU, never the accumulate cell at
                # row 300 — a long program's ROM rows shadow it and port A
                # reads the memory bus (MANDELBROT.md §4)
                ir.copy_a(rows["add_res"], rows["add_a"])
                ir.barrier()
                ir.copy_a(rows["add_a"], rows["cmp_m"])
                ir.barrier()
                ir.jump_if_zero(rows["flag_ge"], label)
                park = None
        if halt:
            ir.halt()
        return ir

    def _emit_io(self, ir, io, park):
        want = self.resolve(self._source(io.need))
        if park != (want, want):
            ir.vec_select(want)
            ir.barrier()
            park = (want, want)
        if io.kind == "reduce_sum":
            ir.copy_a(self.rows["vred_sum"], io.dst)
            ir.barrier()
        elif io.kind == "reduce_count":
            ir.copy_a(self.rows["vred_count"], io.dst)
            ir.barrier()
        elif io.kind == "read_lane":
            ir.vec_read_lane(io.lane, io.dst)
        elif io.kind != "park":
            raise CompileError(f"unknown IO action {io.kind!r}")
        return park


class _LoopCtx:
    def __init__(self, m, count):
        self.m, self.count = m, count

    def __enter__(self):
        m = self.m
        m._loops += 1
        blk = _Block("loop", len(m.blocks), count=self.count)
        m.blocks.append(blk)
        m.cur = blk.index
        self.frame = sys._getframe(1)
        self.entry = {k: v for k, v in self.frame.f_locals.items()
                      if isinstance(v, Vec)}
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            return False
        m = self.m
        blk = m.blocks[m.cur]
        now = self.frame.f_locals
        carries = []
        for name, old in self.entry.items():
            new = now.get(name)
            if isinstance(new, Vec) and new.node is not old.node:
                old.named(name)
                new.node.name = new.node.name or f"{name}'"
                carries.append((old.node, new.node))
        # DAG order, so the body matches the order the statements were written
        blk.carries = sorted(carries, key=lambda p: p[1].uid)
        for old, _new in blk.carries:
            if old.block >= blk.index:
                raise CompileError(
                    f"{old.label()} is rebound in the loop but was not live on "
                    "entry; give it a value (or m.vec()) before the loop")
        tail = _Block("straight", len(m.blocks))
        m.blocks.append(tail)
        m.cur = tail.index
        return False


def compile_program(build, cfg, rows, name="vprog"):
    """`build(m)` writes the program; returns the compiled Machine."""
    m = Machine(cfg, rows, name)
    build(m)
    return m.compile()
