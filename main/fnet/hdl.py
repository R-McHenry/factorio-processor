#!/usr/bin/env python3
"""Netlist/HDL compiler for combinator circuits (.fnet -> *.source.json).

See ../plan/NETLIST_PLAN.md for the language and the resolved design decisions.
Principles: combinator control_behavior JSON is passed through verbatim
(only [paramN] slots are substituted, never validated); every net is one
color = one physical wire network; validation is structural only (drivers,
consumers, connector sharing, signal ownership, wire reach).
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # main/ on the path
import signal_space as ss
from blueprint_codec import encode_blueprint_string

# PACKING GRID (2026-07-27: combinators stand UP). A combinator's default
# north footprint is 1 wide x 2 tall, so packing it that way makes the grid
# cell 1x2 instead of the 2x2 an east-facing 2x1 needed once the old layout's
# spare inter-row lane is counted — half the area per entity, and the same
# again for a pair of 1x1 constants sharing one column. Rotation is purely
# cosmetic electrically: blueprint wires name CONNECTOR IDS (1/2 in, 3/4 out),
# which do not depend on which way the entity faces.
#
# Repeaters no longer live in a horizontal lane between rows, because there is
# no longer a spare one. They live in a reserved COLUMN down the middle of the
# block, which is also where a repeater most wants to be: every entity in a
# row is within half a row-width of it, so one repeater in the gap bridges the
# two ends of any row.
WIRE_REACH = 9.0
ROW_COLUMNS = 15            # 1-tile columns per row, INCLUDING the gap column
GAP_COLUMN = 7              # reserved for repeaters; centred, so <= 7 either side
ROW_PITCH = 2.0             # a row is exactly one combinator tall now
MAX_REPEATERS = 400
FIRST_ROW_Y = -12.0         # row centre: an integer, since rows are 2 tall
BLUEPRINT_VERSION = 562954248978435

EMPTY_PROBE_CB = {"sections": {"sections": [{"index": 1, "filters": []}]}}

QUALITIES = set(ss.QUALITY_LEVELS)
_VIRTUALS = set(ss.VIRTUAL_SIGNALS) | set(ss.APPENDED_VIRTUALS)
_ITEMS = set(ss.ITEMS)
_FLUIDS = set(ss.FLUIDS)

# Carrier-obscurity ranking (originally modules/v10_address_map.py
# carrier_candidates(), now archived under archive/v10_generator/): auto
# signals draw from everything-else virtuals at mid qualities, most-obscure
# first, deterministically.
_ALPHANUMERIC = {f"signal-{c}" for c in "0123456789"} | {
    f"signal-{chr(c)}" for c in range(ord("A"), ord("Z") + 1)
}
_WILDCARDS = {
    "signal-each", "signal-everything", "signal-anything", "signal-any-quality",
}
_RESERVED_ELSEWHERE = {"signal-no-entry"}
AUTO_QUALITY_POOL = ("uncommon", "rare", "epic")


def auto_signal_pool():
    names = [
        n for n in sorted(_VIRTUALS)
        if n not in _WILDCARDS and n not in _ALPHANUMERIC and n not in _RESERVED_ELSEWHERE
    ]
    for quality in AUTO_QUALITY_POOL:
        for name in names:
            yield (name, quality)


def signal_type(name: str) -> str:
    if name in _VIRTUALS:
        return "virtual"
    if name in _ITEMS:
        return "item"
    if name in _FLUIDS:
        return "fluid"
    raise NetlistError(f"unknown game signal name: {name}")


def signal_json_fragment(name: str, quality: str) -> str:
    # Always fully qualified. Conditions tolerate omitted type/quality, but a
    # constant-combinator FILTER row with no quality imports as quality=nil
    # and emits NOTHING (verified live 2026-07-27: the mmap_addr mask read
    # back filters_count=1 yet its output net was empty; the fully-qualified
    # accumulate mask emitted fine). Same fragment feeds both contexts.
    stype = signal_type(name)
    return f'"type": "{stype}", "name": "{name}", "quality": "{quality}"'


@dataclass(frozen=True)
class EntityInfo:
    width: float
    height: float
    direction: int | None
    inputs: dict    # color -> connector id
    outputs: dict   # color -> connector id


# Combinators are placed in their DEFAULT north orientation: 1 wide, 2 tall.
# `direction: None` means "emit no direction key", which is north.
_COMBINATOR_1X2 = EntityInfo(1.0, 2.0, None, {"red": 1, "green": 2}, {"red": 3, "green": 4})
ENTITY_INFO: dict[str, EntityInfo] = {
    "decider-combinator": _COMBINATOR_1X2,
    "arithmetic-combinator": _COMBINATOR_1X2,
    "selector-combinator": _COMBINATOR_1X2,
    "constant-combinator": EntityInfo(1.0, 1.0, None, {}, {"red": 1, "green": 2}),
}


class NetlistError(Exception):
    pass


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+|\#[^\n]*)
      | (?P<string>"(?:[^"\\]|\\.)*")
      | (?P<num>-?\d+)
      | (?P<ident>[A-Za-z_][A-Za-z0-9_\-]*)
      | (?P<sym>[=(){}<>,;:@\[\]+])
    """,
    re.VERBOSE,
)


class Lexer:
    def __init__(self, text: str, path: str):
        self.text = text
        self.path = path
        self.pos = 0

    def line(self, pos: int | None = None) -> int:
        p = self.pos if pos is None else pos
        return self.text.count("\n", 0, p) + 1

    def where(self) -> str:
        return f"{self.path}:{self.line()}"

    def error(self, msg: str):
        raise NetlistError(f"{self.where()}: {msg}")

    def _skip_ws(self):
        while self.pos < len(self.text):
            m = _TOKEN_RE.match(self.text, self.pos)
            if m and m.lastgroup == "ws":
                self.pos = m.end()
            else:
                break

    def peek(self):
        """Return (kind, value) of the next token without consuming, or (None, None) at EOF."""
        self._skip_ws()
        if self.pos >= len(self.text):
            return (None, None)
        m = _TOKEN_RE.match(self.text, self.pos)
        if not m or m.lastgroup == "ws":
            self.error(f"unexpected character {self.text[self.pos]!r}")
        return (m.lastgroup, m.group())

    def next(self):
        kind, value = self.peek()
        if kind is None:
            self.error("unexpected end of file")
        self.pos += len(value)
        return (kind, value)

    def at_sym(self, ch: str) -> bool:
        kind, value = self.peek()
        return kind == "sym" and value == ch

    def expect_sym(self, ch: str):
        kind, value = self.peek()
        if kind != "sym" or value != ch:
            self.error(f"expected {ch!r}, found {value!r}")
        self.next()

    def expect_ident(self) -> str:
        kind, value = self.peek()
        if kind != "ident":
            self.error(f"expected identifier, found {value!r}")
        self.next()
        return value

    def capture_json(self) -> str:
        """Capture a balanced {...} blob verbatim (strings respected)."""
        self._skip_ws()
        if self.pos >= len(self.text) or self.text[self.pos] != "{":
            self.error("expected '{' starting a JSON block")
        depth = 0
        i = self.pos
        in_string = False
        while i < len(self.text):
            c = self.text[i]
            if in_string:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_string = False
            elif c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    blob = self.text[self.pos:i + 1]
                    self.pos = i + 1
                    return blob
            i += 1
        self.error("unterminated JSON block")


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------

@dataclass
class SignalDecl:
    name: str
    pin: tuple[str, str] | None   # (game_name, quality) or None
    src: str
    mem: tuple[str | None, int] | None = None   # (region or None, offset/address)
    resolved: tuple[str, str] | None = None
    address: int | None = None
    auto: bool = False
    accumulate: bool = False


@dataclass
class TemplateDef:
    name: str
    prototype: str
    body_text: str
    src: str


@dataclass
class Port:
    name: str
    direction: str   # "in" | "out"
    color: str       # "red" | "green"


@dataclass
class Conn:
    port: str | None
    net: str


@dataclass
class Inst:
    callee: str
    params: list          # ("signal", name) | ("int", value) | ("str", text)
    conns: list[Conn]
    assign: str | None
    src: str


@dataclass
class NetDecl:
    name: str
    color: str
    debug: bool
    src: str


@dataclass
class ModuleDef:
    name: str
    ports: list[Port]
    signals: list[SignalDecl] = field(default_factory=list)
    regions: dict[str, int] = field(default_factory=dict)
    excludes: list[tuple[str, str]] = field(default_factory=list)  # (signal name, src)
    accumulates: list[tuple[str, str]] = field(default_factory=list)  # (signal name, src)
    nets: list[NetDecl] = field(default_factory=list)
    debugs: list[tuple[str, str]] = field(default_factory=list)  # (net name, src)
    insts: list[Inst] = field(default_factory=list)
    src: str = ""


@dataclass
class FileAst:
    signals: list[SignalDecl] = field(default_factory=list)
    regions: dict[str, int] = field(default_factory=dict)
    excludes: list[tuple[str, str]] = field(default_factory=list)  # (signal name, src)
    accumulates: list[tuple[str, str]] = field(default_factory=list)  # (signal name, src)
    templates: dict[str, TemplateDef] = field(default_factory=dict)
    modules: dict[str, ModuleDef] = field(default_factory=dict)


PORT_KEYS = ("in_r", "in_g", "out_r", "out_g")


def _port_key_parts(key: str) -> tuple[str, str]:
    direction = "in" if key.startswith("in") else "out"
    color = "red" if key.endswith("_r") else "green"
    return direction, color


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_file(path: Path, _visited: set | None = None,
               _loaded: set | None = None) -> FileAst:
    visited = _visited if _visited is not None else set()
    loaded = _loaded if _loaded is not None else set()
    rp = path.resolve()
    if rp in loaded:
        # Diamond import (e.g. every component file imports lib/v8.fnet):
        # already merged once — contribute nothing the second time.
        return FileAst()
    if rp in visited:
        raise NetlistError(f"import cycle involving {path}")
    visited.add(rp)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise NetlistError(f"cannot read {path}: {exc}") from exc

    lx = Lexer(text, str(path))
    ast = FileAst()

    def merge(other: FileAst, src: str):
        for sig in other.signals:
            if any(s.name == sig.name for s in ast.signals):
                raise NetlistError(f"{src}: imported signal {sig.name!r} already defined")
            ast.signals.append(sig)
        for rname, base in other.regions.items():
            if rname in ast.regions:
                raise NetlistError(f"{src}: imported region {rname!r} already defined")
            ast.regions[rname] = base
        ast.excludes.extend(other.excludes)
        ast.accumulates.extend(other.accumulates)
        for name, tdef in other.templates.items():
            if name in ast.templates:
                raise NetlistError(f"{src}: imported template {name!r} already defined")
            ast.templates[name] = tdef
        for name, mdef in other.modules.items():
            if name in ast.modules:
                raise NetlistError(f"{src}: imported module {name!r} already defined")
            ast.modules[name] = mdef

    while True:
        kind, value = lx.peek()
        if kind is None:
            break
        if kind != "ident":
            lx.error(f"expected declaration, found {value!r}")
        if value == "import":
            src = lx.where()
            lx.next()
            skind, sval = lx.next()
            if skind != "string":
                lx.error("import expects a quoted path")
            lx.expect_sym(";")
            merge(parse_file(path.parent / json.loads(sval), visited, loaded), src)
        elif value == "signal":
            ast.signals.append(_parse_signal_decl(lx))
        elif value == "region":
            rname, base = _parse_region_decl(lx)
            if rname in ast.regions:
                lx.error(f"region {rname!r} already defined")
            ast.regions[rname] = base
        elif value == "exclude":
            ast.excludes.extend(_parse_exclude(lx))
        elif value == "accumulate":
            ast.accumulates.extend(_parse_exclude(lx))
        elif value == "module":
            mdef = _parse_module(lx)
            if mdef.name in ast.modules:
                lx.error(f"module {mdef.name!r} already defined")
            ast.modules[mdef.name] = mdef
        else:
            # file scope:  name = entity-prototype({ ...json... });
            src = lx.where()
            name = lx.expect_ident()
            lx.expect_sym("=")
            prototype = lx.expect_ident()
            if prototype not in ENTITY_INFO:
                raise NetlistError(
                    f"{src}: unknown entity prototype {prototype!r} "
                    f"(known: {', '.join(sorted(ENTITY_INFO))}; extend ENTITY_INFO in netlist.py)"
                )
            lx.expect_sym("(")
            body = lx.capture_json()
            lx.expect_sym(")")
            if lx.at_sym(";"):
                lx.next()
            if name in ast.templates:
                raise NetlistError(f"{src}: template {name!r} already defined")
            ast.templates[name] = TemplateDef(name, prototype, body, src)
    visited.discard(rp)
    loaded.add(rp)
    return ast


def _parse_signal_decl(lx: Lexer) -> SignalDecl:
    src = lx.where()
    lx.next()  # 'signal'
    name = lx.expect_ident()
    pin = None
    mem = None
    if lx.at_sym("="):
        lx.next()
        kind, value = lx.peek()
        if kind == "ident" and value == "memory":
            # signal x = memory[region + offset];  /  memory[addr];
            lx.next()
            lx.expect_sym("[")
            k2, v2 = lx.next()
            if k2 == "num":
                mem = (None, int(v2))
            elif k2 == "ident":
                offset = 0
                if lx.at_sym("+"):
                    lx.next()
                    k3, v3 = lx.next()
                    if k3 != "num":
                        lx.error(f"expected numeric offset, found {v3!r}")
                    offset = int(v3)
                mem = (v2, offset)
            else:
                lx.error(f"expected region name or address, found {v2!r}")
            lx.expect_sym("]")
        else:
            game_name = lx.expect_ident()
            quality = "normal"
            if lx.at_sym("@"):
                lx.next()
                quality = lx.expect_ident()
                if quality not in QUALITIES:
                    lx.error(f"unknown quality {quality!r}")
            pin = (game_name, quality)
    lx.expect_sym(";")
    return SignalDecl(name, pin, src, mem=mem)


def _parse_exclude(lx: Lexer) -> list[tuple[str, str]]:
    """exclude sig_a, sig_b;  — remove these declared signals' rows from the
    design's address space BEFORE numbering (design-wide: every signal_table
    in the design shares the resulting compact numbering).

    Also reused for `accumulate sig_a, sig_b;` — same `<keyword> name, ...;`
    shape (the leading keyword is skipped, whichever it is)."""
    src = lx.where()
    lx.next()  # 'exclude' / 'accumulate'
    names = []
    while True:
        names.append((lx.expect_ident(), src))
        if lx.at_sym(","):
            lx.next()
        else:
            break
    lx.expect_sym(";")
    return names


def _parse_region_decl(lx: Lexer) -> tuple[str, int]:
    lx.next()  # 'region'
    name = lx.expect_ident()
    lx.expect_sym("=")
    kind, value = lx.next()
    if kind != "num" or int(value) < 1:
        lx.error(f"region base must be a positive address, found {value!r}")
    lx.expect_sym(";")
    return name, int(value)


def _parse_module(lx: Lexer) -> ModuleDef:
    src = lx.where()
    lx.next()  # 'module'
    name = lx.expect_ident()
    lx.expect_sym("(")
    ports: list[Port] = []
    while not lx.at_sym(")"):
        key = lx.expect_ident()
        if key not in PORT_KEYS:
            lx.error(f"port direction must be one of {PORT_KEYS}, found {key!r}")
        direction, color = _port_key_parts(key)
        pname = lx.expect_ident()
        ports.append(Port(pname, direction, color))
        if lx.at_sym(","):
            lx.next()
    lx.expect_sym(")")
    lx.expect_sym("{")
    mdef = ModuleDef(name, ports, src=src)
    while not lx.at_sym("}"):
        kind, value = lx.peek()
        if kind != "ident":
            lx.error(f"expected statement, found {value!r}")
        if value == "signal":
            mdef.signals.append(_parse_signal_decl(lx))
        elif value == "region":
            rname, base = _parse_region_decl(lx)
            if rname in mdef.regions:
                lx.error(f"region {rname!r} already defined")
            mdef.regions[rname] = base
        elif value == "exclude":
            mdef.excludes.extend(_parse_exclude(lx))
        elif value == "accumulate":
            mdef.accumulates.extend(_parse_exclude(lx))
        elif value == "net":
            mdef.nets.extend(_parse_net_decl(lx, debug=False))
        elif value == "debug":
            dsrc = lx.where()
            lx.next()
            nkind, nvalue = lx.peek()
            if nkind == "ident" and nvalue == "net":
                mdef.nets.extend(_parse_net_decl(lx, debug=True))
            else:
                while True:
                    mdef.debugs.append((lx.expect_ident(), dsrc))
                    if lx.at_sym(","):
                        lx.next()
                    else:
                        break
                lx.expect_sym(";")
        else:
            mdef.insts.append(_parse_inst(lx))
    lx.expect_sym("}")
    return mdef


def _parse_net_decl(lx: Lexer, debug: bool) -> list[NetDecl]:
    src = lx.where()
    lx.next()  # 'net'
    color = lx.expect_ident()
    if color not in ("red", "green"):
        lx.error(f"net color must be red or green, found {color!r} "
                 "(colorless nets are reserved for the future auto-color pass)")
    decls = []
    while True:
        decls.append(NetDecl(lx.expect_ident(), color, debug, src))
        if lx.at_sym(","):
            lx.next()
        else:
            break
    lx.expect_sym(";")
    return decls


def _parse_inst(lx: Lexer) -> Inst:
    src = lx.where()
    first = lx.expect_ident()
    assign = None
    if lx.at_sym("="):
        lx.next()
        assign = first
        callee = lx.expect_ident()
    else:
        callee = first
    params: list = []
    if lx.at_sym("<"):
        lx.next()
        while not lx.at_sym(">"):
            kind, value = lx.next()
            if kind == "ident":
                params.append(("signal", value))
            elif kind == "num":
                params.append(("int", int(value)))
            elif kind == "string":
                params.append(("str", json.loads(value)))
            else:
                lx.error(f"bad template parameter {value!r}")
            if lx.at_sym(","):
                lx.next()
        lx.expect_sym(">")
    lx.expect_sym("(")
    conns: list[Conn] = []
    while not lx.at_sym(")"):
        ident = lx.expect_ident()
        if lx.at_sym(":"):
            lx.next()
            conns.append(Conn(ident, lx.expect_ident()))
        else:
            conns.append(Conn(None, ident))
        if lx.at_sym(","):
            lx.next()
    lx.expect_sym(")")
    lx.expect_sym(";")
    return Inst(callee, params, conns, assign, src)


# ---------------------------------------------------------------------------
# Signal allocation
# ---------------------------------------------------------------------------

class SignalAllocator:
    def __init__(self):
        self.owners: dict[tuple[str, str], str] = {}

    def pin(self, key: tuple[str, str], owner: str, src: str):
        signal_type(key[0])  # validates the name exists
        if key in self.owners:
            raise NetlistError(
                f"{src}: signal {key[0]}@{key[1]} already owned by {self.owners[key]} "
                f"(explicit aliasing not implemented yet; pick another signal)"
            )
        self.owners[key] = owner

    def auto(self, owner: str, src: str) -> tuple[str, str]:
        for key in auto_signal_pool():
            if key not in self.owners:
                self.owners[key] = owner
                return key
        raise NetlistError(f"{src}: auto signal pool exhausted")


def _signal_scopes(ast: FileAst):
    return [("file", ast.signals, dict(ast.regions))] + [
        (mdef.name, mdef.signals, {**ast.regions, **mdef.regions})
        for mdef in ast.modules.values()
    ]


def allocate_signals(ast: FileAst, only: set | None = None) -> SignalAllocator:
    """Phase 1+2 of allocation: name pins first, then autos — pins always
    win regardless of order.

    Memory-mapped declarations (`signal x = memory[region + k]`) are NOT
    resolved here: their (name, quality) depends on the design's address
    space, which depends on the design's `exclude` set (exclusion happens
    before numbering). And because memory rows must own their (name,
    quality), remaining autos are held back until AFTER
    resolve_memory_signals() — `only` restricts this call's auto pass to
    the excluded signal names (they shape the space, so they must resolve
    first); allocate_remaining_autos() finishes the job."""
    alloc = SignalAllocator()
    for scope, decls, _ in _signal_scopes(ast):
        seen = set()
        for decl in decls:
            if decl.name in seen:
                raise NetlistError(f"{decl.src}: duplicate signal {decl.name!r} in {scope}")
            seen.add(decl.name)
            if decl.mem is None and decl.pin is not None:
                alloc.pin(decl.pin, f"{scope}.{decl.name}", decl.src)
                decl.resolved = decl.pin
    for scope, decls, _ in _signal_scopes(ast):
        for decl in decls:
            if decl.resolved is None and decl.mem is None and \
                    (only is None or decl.name in only):
                decl.resolved = alloc.auto(f"{scope}.{decl.name}", decl.src)
                decl.auto = True
    return alloc


def allocate_remaining_autos(ast: FileAst, alloc: SignalAllocator):
    """Phase 4 (after memory rows pinned): the rest of the autos, drawn
    from the pool skipping everything now owned — including memory rows,
    so an auto can never squat on an addressable row's signal."""
    for scope, decls, _ in _signal_scopes(ast):
        for decl in decls:
            if decl.resolved is None and decl.mem is None:
                decl.resolved = alloc.auto(f"{scope}.{decl.name}", decl.src)
                decl.auto = True


def resolve_memory_signals(ast: FileAst, alloc: SignalAllocator,
                           space_by_addr: dict[int, dict]):
    """Pass 3: pin memory-mapped declarations against the design's generated
    (exclusion-compacted) address space. The address is the chosen thing;
    the game signal is derived from it via the same numbering the hardware
    tables will be populated with."""
    for scope, decls, regions in _signal_scopes(ast):
        for decl in decls:
            if decl.mem is None:
                continue
            region, offset = decl.mem
            if region is None:
                addr = offset
            else:
                if region not in regions:
                    raise NetlistError(
                        f"{decl.src}: unknown region {region!r} "
                        f"(defined: {', '.join(sorted(regions)) or 'none'})"
                    )
                addr = regions[region] + offset
            entry = space_by_addr.get(addr)
            if entry is None:
                raise NetlistError(
                    f"{decl.src}: no signal at address {addr} "
                    f"(design address space is 1..{max(space_by_addr)})"
                )
            decl.resolved = (entry["name"], entry["quality"])
            decl.address = addr
            alloc.pin(decl.resolved, f"{scope}.{decl.name}", decl.src)


# ---------------------------------------------------------------------------
# Elaboration
# ---------------------------------------------------------------------------

@dataclass
class Net:
    name: str
    color: str
    src: str
    debug: bool = False
    ext_driven: bool = False
    ext_consumed: bool = False
    driver_pins: list = field(default_factory=list)    # (entity_index, connector)
    consumer_pins: list = field(default_factory=list)
    repeater_pins: list = field(default_factory=list)  # inert reach anchors


@dataclass
class DesignEntity:
    prototype: str
    control_behavior: dict
    label: str
    position: tuple[float, float] | None = None
    signal_table: dict | None = None
    player_description: str | None = None
    accumulate_mask: bool = False


# Keys allowed at a template body's top level that belong on the entity
# record, not inside control_behavior. "signal_table" is the existing
# populate-at-paste marker (bench.processor_tb.write_signal_table). Its
# "exclude" list is design-wide state (the address space is numbered AFTER
# dropping excluded rows, so every table must share one exclusion set) —
# it comes from `exclude <signal>;` statements and is injected at emission;
# writing "exclude" inside a template blob is an error.
# "player_description" is the project's entity-identification convention.
LIFT_OUT_KEYS = ("signal_table", "player_description")


_PARAM_RE = re.compile(r"\[param(\d+)\]")


class Design:
    def __init__(self, ast: FileAst, top: str):
        self.ast = ast
        self.entities: list[DesignEntity] = []
        self.nets: dict[str, Net] = {}
        self.debug_probes: list[dict] = []
        self.multi_driver_notes: list[str] = []
        self.repeaters: list[dict] = []
        self.placement_order: list[int] = []
        self.layout_choice = "declaration"
        self.layout_score: tuple[int, float] = (0, 0.0)
        self._row_ys: list[float] = [FIRST_ROW_Y]
        self._occupied_lane_slots: set[tuple[float, float]] = set()
        if top not in ast.modules:
            raise NetlistError(f"module {top!r} not found (have: {', '.join(ast.modules) or 'none'})")
        self.top = top
        excluded_names = {name for name, _src in ast.excludes}
        for mdef in ast.modules.values():
            excluded_names.update(name for name, _src in mdef.excludes)
        self.alloc = allocate_signals(ast, only=excluded_names)
        self.excluded = self._resolve_excludes()
        self.address_space = {
            int(row["address"]): row
            for row in ss.full_table([(e["name"], e["quality"]) for e in self.excluded])
        }
        resolve_memory_signals(ast, self.alloc, self.address_space)
        allocate_remaining_autos(ast, self.alloc)
        self.accumulate_rows = self._resolve_accumulates()
        self._elaborate(ast.modules[top], "", {}, stack=[top])
        self._attach_debug_probes()

    def _resolve_excludes(self) -> list[dict]:
        """Design-wide exclusions: declared signals whose (name, quality) rows
        are dropped from the address space BEFORE numbering. One compact
        space per design — every signal_table entity shares it."""
        out: list[dict] = []
        refs = [(None, name, src) for name, src in self.ast.excludes]
        for mdef in self.ast.modules.values():
            refs.extend((mdef, name, src) for name, src in mdef.excludes)
        for mdef, name, src in refs:
            decl = None
            if mdef is not None:
                decl = next((d for d in mdef.signals if d.name == name), None)
            if decl is None:
                decl = next((d for d in self.ast.signals if d.name == name), None)
            if decl is None:
                raise NetlistError(f"{src}: exclude of undeclared signal {name!r}")
            if decl.mem is not None:
                raise NetlistError(
                    f"{src}: cannot exclude memory-mapped signal {name!r} — "
                    f"memory rows exist to be addressable"
                )
            game, quality = decl.resolved
            entry = {"type": signal_type(game), "name": game, "quality": quality}
            if entry not in out:
                out.append(entry)
        return out

    def _resolve_accumulates(self) -> list[dict]:
        """Design-wide accumulate set: memory-mapped rows whose cell condition
        is defeated by a standing -1 on the reset bus, so writes ADD instead
        of replacing (v8 `write_trigger_reset` mask semantics). The physical
        mask combinator's contents are GENERATED from this set — declaring a
        row accumulate here is the configuration act, the one source of truth
        processor.isa reads back through the signals map."""
        out: list[dict] = []
        refs = [(None, name, src) for name, src in self.ast.accumulates]
        for mdef in self.ast.modules.values():
            refs.extend((mdef, name, src) for name, src in mdef.accumulates)
        for mdef, name, src in refs:
            decl = None
            if mdef is not None:
                decl = next((d for d in mdef.signals if d.name == name), None)
            if decl is None:
                decl = next((d for d in self.ast.signals if d.name == name), None)
            if decl is None:
                raise NetlistError(f"{src}: accumulate of undeclared signal {name!r}")
            if decl.mem is None:
                raise NetlistError(
                    f"{src}: accumulate requires a memory-mapped signal "
                    f"({name!r} is a control-plane carrier — accumulate is a "
                    f"property of an addressable row)"
                )
            decl.accumulate = True
            game, quality = decl.resolved
            entry = {"type": signal_type(game), "name": game, "quality": quality}
            if entry not in out:
                out.append(entry)
        return out

    def _accumulate_mask_cb(self) -> dict:
        filters = [
            {
                "index": i + 1,
                "type": row["type"],
                "name": row["name"],
                "quality": row["quality"],
                "comparator": "=",
                "count": -1,
            }
            for i, row in enumerate(self.accumulate_rows)
        ]
        return {"sections": {"sections": [{"index": 1, "filters": filters}]}}

    # -- helpers ------------------------------------------------------------

    def _lookup_signal(self, name: str, mdef: ModuleDef, src: str) -> SignalDecl:
        for decl in mdef.signals:
            if decl.name == name:
                return decl
        for decl in self.ast.signals:
            if decl.name == name:
                return decl
        raise NetlistError(f"{src}: unknown signal {name!r}")

    def _render_param(self, param, mdef: ModuleDef, src: str) -> str:
        kind, value = param
        if kind == "signal":
            decl = self._lookup_signal(value, mdef, src)
            return signal_json_fragment(*decl.resolved)
        if kind == "int":
            return str(value)
        return value  # "str": inserted verbatim (escape hatch for raw JSON)

    def _instantiate_template(self, tdef: TemplateDef, params, mdef: ModuleDef, src: str) -> dict:
        needed = {int(m) for m in _PARAM_RE.findall(tdef.body_text)}
        if needed and max(needed) + 1 != len(params) or len(needed) != len(params):
            raise NetlistError(
                f"{src}: template {tdef.name!r} uses params {sorted(needed)}, "
                f"got {len(params)} argument(s)"
            )
        text = tdef.body_text
        for i, param in enumerate(params):
            text = text.replace(f"[param{i}]", self._render_param(param, mdef, src))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise NetlistError(
                f"{src}: template {tdef.name!r} body is not valid JSON after "
                f"substitution: {exc}"
            ) from exc

    # -- elaboration --------------------------------------------------------

    def _elaborate(self, mdef: ModuleDef, prefix: str, bindings: dict[str, Net], stack: list):
        netmap: dict[str, Net] = {}
        for port in mdef.ports:
            if port.name in bindings:
                net = bindings[port.name]
                if net.color != port.color:
                    raise NetlistError(
                        f"{mdef.src}: port {port.name!r} is {port.color} but bound "
                        f"net {net.name!r} is {net.color}"
                    )
            else:  # top-level: the port becomes an externally-visible net
                net = Net(prefix + port.name, port.color, mdef.src,
                          ext_driven=(port.direction == "in"),
                          ext_consumed=(port.direction == "out"))
                self.nets[net.name] = net
            netmap[port.name] = net

        for decl in mdef.nets:
            if decl.name in netmap:
                raise NetlistError(f"{decl.src}: net {decl.name!r} shadows a port")
            net = Net(prefix + decl.name, decl.color, decl.src, debug=decl.debug,
                      ext_consumed=decl.debug)
            self.nets[net.name] = net
            netmap[decl.name] = net

        for dbg_name, dbg_src in mdef.debugs:
            if dbg_name not in netmap:
                raise NetlistError(f"{dbg_src}: debug of unknown net {dbg_name!r}")
            netmap[dbg_name].debug = True
            netmap[dbg_name].ext_consumed = True

        child_counts: dict[str, int] = {}
        for inst in mdef.insts:
            if inst.callee in self.ast.templates:
                self._elab_template_inst(inst, mdef, netmap, prefix)
            elif inst.callee in self.ast.modules:
                if inst.assign is not None:
                    raise NetlistError(
                        f"{inst.src}: assignment sugar is for templates; connect "
                        f"module ports by name"
                    )
                if inst.callee in stack:
                    raise NetlistError(f"{inst.src}: recursive module instantiation of {inst.callee!r}")
                child = self.ast.modules[inst.callee]
                n = child_counts.get(inst.callee, 0)
                child_counts[inst.callee] = n + 1
                child_bindings = self._bind_module_ports(inst, child, netmap)
                self._elaborate(child, f"{prefix}{inst.callee}{n}.", child_bindings,
                                stack + [inst.callee])
            else:
                raise NetlistError(f"{inst.src}: unknown template or module {inst.callee!r}")

    def _bind_module_ports(self, inst: Inst, child: ModuleDef, netmap) -> dict[str, Net]:
        if inst.params:
            raise NetlistError(f"{inst.src}: modules take no <...> params (v1)")
        bindings: dict[str, Net] = {}
        positional = 0
        port_names = [p.name for p in child.ports]
        for conn in inst.conns:
            if conn.port is None:
                if positional >= len(child.ports):
                    raise NetlistError(f"{inst.src}: too many positional connections")
                pname = port_names[positional]
                positional += 1
            else:
                pname = conn.port
                if pname not in port_names:
                    raise NetlistError(
                        f"{inst.src}: module {child.name!r} has no port {pname!r} "
                        f"(ports: {', '.join(port_names)})"
                    )
            if pname in bindings:
                raise NetlistError(f"{inst.src}: port {pname!r} connected twice")
            if conn.net not in netmap:
                raise NetlistError(f"{inst.src}: unknown net {conn.net!r}")
            bindings[pname] = netmap[conn.net]
        for port in child.ports:
            if port.name not in bindings:
                raise NetlistError(f"{inst.src}: module port {port.name!r} left unconnected")
        return bindings

    def _elab_template_inst(self, inst: Inst, mdef: ModuleDef, netmap, prefix: str):
        tdef = self.ast.templates[inst.callee]
        info = ENTITY_INFO[tdef.prototype]
        cb = self._instantiate_template(tdef, inst.params, mdef, inst.src)
        lifted = {key: cb.pop(key) for key in LIFT_OUT_KEYS if key in cb}
        if isinstance(lifted.get("signal_table"), dict) and "exclude" in lifted["signal_table"]:
            raise NetlistError(
                f"{inst.src}: signal_table exclusions are design-wide (they change "
                f"the space's numbering) — use an `exclude <signal>;` statement, "
                f"not a per-table list"
            )
        is_mask = bool(cb.pop("accumulate_mask", False))
        if is_mask:
            if tdef.prototype != "constant-combinator":
                raise NetlistError(
                    f"{inst.src}: accumulate_mask must be a constant-combinator, "
                    f"not {tdef.prototype}"
                )
            if cb:
                raise NetlistError(
                    f"{inst.src}: an accumulate_mask template body must contain "
                    f"only the marker — its filters are generated from the "
                    f"design's `accumulate` declarations (found: {', '.join(cb)})"
                )
            cb = self._accumulate_mask_cb()
        entity_index = len(self.entities)
        self.entities.append(DesignEntity(
            tdef.prototype, cb, f"{prefix}{tdef.name}@{inst.src}",
            signal_table=lifted.get("signal_table"),
            player_description=lifted.get("player_description"),
            accumulate_mask=is_mask,
        ))

        slots: dict[str, Net] = {}

        def fill(key: str, net: Net):
            if key in slots:
                raise NetlistError(
                    f"{inst.src}: port {key!r} connected twice (use named ports)"
                )
            _, color = _port_key_parts(key)
            if net.color != color:
                raise NetlistError(
                    f"{inst.src}: port {key!r} is {color} but net {net.name!r} is {net.color}"
                )
            slots[key] = net

        for conn in inst.conns:
            if conn.net not in netmap:
                raise NetlistError(f"{inst.src}: unknown net {conn.net!r}")
            net = netmap[conn.net]
            if conn.port is not None:
                if conn.port not in PORT_KEYS:
                    raise NetlistError(
                        f"{inst.src}: template ports are {PORT_KEYS}, found {conn.port!r}"
                    )
                fill(conn.port, net)
            else:
                fill("in_r" if net.color == "red" else "in_g", net)

        if inst.assign is not None:
            if inst.assign not in netmap:
                raise NetlistError(f"{inst.src}: unknown net {inst.assign!r}")
            net = netmap[inst.assign]
            fill("out_r" if net.color == "red" else "out_g", net)

        for key, net in slots.items():
            direction, color = _port_key_parts(key)
            table = info.inputs if direction == "in" else info.outputs
            if color not in table:
                raise NetlistError(
                    f"{inst.src}: {tdef.prototype} has no {key} connector"
                )
            pin = (entity_index, table[color])
            (net.consumer_pins if direction == "in" else net.driver_pins).append(pin)

    def _attach_debug_probes(self):
        for name in sorted(n for n, net in self.nets.items() if net.debug):
            net = self.nets[name]
            info = ENTITY_INFO["constant-combinator"]
            entity_index = len(self.entities)
            self.entities.append(DesignEntity(
                "constant-combinator",
                json.loads(json.dumps(EMPTY_PROBE_CB)),
                f"debug-probe:{name}",
            ))
            net.consumer_pins.append((entity_index, info.outputs[net.color]))
            self.debug_probes.append({
                "net": name,
                "entity_number": entity_index + 1,
                "wire": net.color,
            })

    # -- validation ---------------------------------------------------------

    def validate(self):
        errors = []
        pin_owner: dict[tuple[int, int], str] = {}
        for name in sorted(self.nets):
            net = self.nets[name]
            pins = net.driver_pins + net.consumer_pins
            if not pins:
                errors.append(f"net {name!r} ({net.src}) is declared but never connected")
                continue
            if not net.driver_pins and not net.ext_driven:
                errors.append(f"net {name!r} ({net.src}) has no driver")
            if not net.consumer_pins and not net.ext_consumed:
                errors.append(f"net {name!r} ({net.src}) has no consumer")
            for pin in pins:
                other = pin_owner.get(pin)
                if other is not None and other != name:
                    errors.append(
                        f"nets {other!r} and {name!r} share connector {pin[1]} of "
                        f"entity {pin[0] + 1} ({self.entities[pin[0]].label}) — "
                        f"they would merge into one network"
                    )
                pin_owner[pin] = name
            if len({p for p in net.driver_pins}) > 1:
                self.multi_driver_notes.append(
                    f"net {name!r} has {len(set(net.driver_pins))} drivers (values sum)"
                )
        masks = [e for e in self.entities if e.accumulate_mask]
        if self.accumulate_rows and not masks:
            errors.append(
                "accumulate signals declared but no accumulate_mask entity — "
                "the -1 mask must physically ride the reset bus"
            )
        if masks and not self.accumulate_rows:
            errors.append(
                "accumulate_mask entity present but no `accumulate` declarations "
                "— its filters would be empty"
            )
        if errors:
            raise NetlistError("validation failed:\n  " + "\n  ".join(errors))

    # -- layout + wiring ----------------------------------------------------

    def layout(self):
        """Place entities, choosing the best of a few packing orders.

        Placement order is decoupled from entity NUMBERING (which stays
        declaration order, because testbench entity_maps and the .debug.json
        sidecar index by it). Only geometry moves, so a better order can
        never change behavior — it just shortens wires."""
        candidates = [
            ("declaration", list(range(len(self.entities)))),
            ("cuthill-mckee", self._cuthill_mckee(reverse=False)),
            ("reverse-cuthill-mckee", self._cuthill_mckee(reverse=True)),
            ("net-cluster", self._net_cluster_order()),
        ]
        best = None
        for label, order in candidates:
            score = self._score_order(order)
            if best is None or score < best[0]:
                best = (score, label, order)
        self.layout_choice = best[1]
        self.layout_score = best[0]
        self.placement_order = best[2]
        self._pack(best[2])

    def _pack(self, order: list[int]) -> None:
        """Column-pack entities into rows of 1-tile columns, two tiles tall.

        A full-height entity (any combinator) takes a whole column. A 1x1
        constant takes HALF a column, and the next 1x1 in placement order
        takes the other half — only the next one, so pairing never drags an
        entity away from its net-mates to fill a hole further back.

        GAP_COLUMN is skipped in every row and left for repeaters."""
        column = 0
        row_y = FIRST_ROW_Y
        self._row_ys = [row_y]
        half_open: int | None = None      # column with a free bottom half

        def advance():
            nonlocal column, row_y, half_open
            column += 1
            if column == GAP_COLUMN:
                column += 1
            if column >= ROW_COLUMNS:
                column = 0
                row_y += ROW_PITCH
                self._row_ys.append(row_y)
                half_open = None          # never pair across a row break

        for index in order:
            ent = self.entities[index]
            info = ENTITY_INFO[ent.prototype]
            if info.height >= ROW_PITCH:
                if half_open is not None:
                    half_open = None
                    advance()
                ent.position = (column + 0.5, row_y)
                advance()
            elif half_open is not None:
                ent.position = (half_open + 0.5, row_y + 0.5)
                half_open = None
                advance()
            else:
                ent.position = (column + 0.5, row_y - 0.5)
                half_open = column

    def _adjacency(self) -> list[set[int]]:
        adj: list[set[int]] = [set() for _ in self.entities]
        for net in self.nets.values():
            members = {pin[0] for pin in net.driver_pins + net.consumer_pins}
            for i in members:
                adj[i] |= members - {i}
        return adj

    def _cuthill_mckee(self, reverse: bool) -> list[int]:
        """Bandwidth-reduction order: BFS from the least-connected entity,
        neighbours visited lowest-degree first. Pulls net-mates adjacent."""
        adj = self._adjacency()
        degree = [len(a) for a in adj]
        order: list[int] = []
        visited: set[int] = set()
        while len(order) < len(self.entities):
            start = min((i for i in range(len(self.entities)) if i not in visited),
                        key=lambda i: (degree[i], i))
            visited.add(start)
            queue = [start]
            while queue:
                current = queue.pop(0)
                order.append(current)
                for nb in sorted(adj[current] - visited, key=lambda j: (degree[j], j)):
                    visited.add(nb)
                    queue.append(nb)
        return order[::-1] if reverse else order

    def _net_cluster_order(self) -> list[int]:
        """Emit each net's entities contiguously, tightest nets first."""
        order: list[int] = []
        placed: set[int] = set()
        nets = sorted(self.nets.values(),
                      key=lambda n: (len(set(n.driver_pins + n.consumer_pins)), n.name))
        for net in nets:
            for pin in net.driver_pins + net.consumer_pins:
                if pin[0] not in placed:
                    placed.add(pin[0])
                    order.append(pin[0])
        order.extend(i for i in range(len(self.entities)) if i not in placed)
        return order

    def _score_order(self, order: list[int]) -> tuple[int, float]:
        """(reach violations, total wire length) — lexicographic, lower wins."""
        self._pack(order)
        violations = 0
        total = 0.0
        for net in self.nets.values():
            pins = list(dict.fromkeys(net.driver_pins + net.consumer_pins))
            if len(pins) < 2:
                continue
            for a, b in self._mst_edges(pins):
                dist = self._pin_distance(a, b)
                total += dist
                if dist > WIRE_REACH:
                    violations += 1
        return (violations, round(total, 3))

    def route(self) -> list[list[int]]:
        """Wire every net, inserting repeaters until nothing exceeds reach."""
        for _ in range(MAX_REPEATERS + 1):
            wires, violations = self._wire_pass()
            if not violations:
                return wires
            if len(self.repeaters) >= MAX_REPEATERS:
                break
            # One repeater per offending net per pass: each halves that net's
            # worst edge and moves nothing else, so the loop converges.
            for net_name in sorted({v[0] for v in violations}):
                worst = max((v for v in violations if v[0] == net_name),
                            key=lambda v: v[3])
                self._add_repeater(net_name, worst[1], worst[2])
        _, violations = self._wire_pass()
        raise NetlistError(
            "wire reach exceeded and repeater insertion did not converge:\n  "
            + "\n  ".join(self._describe_violation(v) for v in violations[:10])
        )

    def wires(self) -> list[list[int]]:
        """Wire every net at the current placement, failing on any over-reach
        edge. `route()` is the build path (it repairs instead of failing);
        this stays the strict checker."""
        out, violations = self._wire_pass()
        if violations:
            raise NetlistError(
                "wire reach exceeded:\n  "
                + "\n  ".join(self._describe_violation(v) for v in violations)
            )
        return out

    def _wire_pass(self):
        """(wires, violations) for the current placement. Pure — no mutation."""
        out: list[list[int]] = []
        violations = []
        for name in sorted(self.nets):
            net = self.nets[name]
            pins = list(dict.fromkeys(
                net.driver_pins + net.consumer_pins + net.repeater_pins))
            if len(pins) < 2:
                continue
            for a, b in self._mst_edges(pins):
                dist = self._pin_distance(a, b)
                if dist > WIRE_REACH:
                    violations.append((name, a, b, dist))
                    continue
                pa = (a[0] + 1, a[1])
                pb = (b[0] + 1, b[1])
                if pb < pa:
                    pa, pb = pb, pa
                out.append([pa[0], pa[1], pb[0], pb[1]])
        out.sort()
        return out, violations

    def _describe_violation(self, violation) -> str:
        name, a, b, dist = violation
        ea, eb = self.entities[a[0]], self.entities[b[0]]
        return (f"net {name!r}: wire {ea.label} {ea.position} -> "
                f"{eb.label} {eb.position} spans {dist:.1f} > reach {WIRE_REACH}")

    def _add_repeater(self, net_name: str, a, b) -> None:
        """Anchor an inert constant on `net` between two out-of-reach pins.

        Electrically this is the SAME network — a constant combinator with no
        filters drives nothing and a wire network propagates within a tick, so
        a repeater costs zero latency. It is the mechanical form of the br_*
        bridge stims that v8_processor.fnet places by hand. Repeaters live in
        the reserved GAP COLUMN, so inserting one never moves an already-placed
        entity — which is what makes the loop converge."""
        net = self.nets[net_name]
        (ax, ay), (bx, by) = self.entities[a[0]].position, self.entities[b[0]].position
        position = self._free_lane_slot((ax + bx) / 2.0, (ay + by) / 2.0)
        index = len(self.entities)
        self.entities.append(DesignEntity(
            "constant-combinator",
            json.loads(json.dumps(EMPTY_PROBE_CB)),
            f"repeater:{net_name}",
            position=position,
            player_description=f"repeater_{net_name.replace('.', '_')}",
        ))
        net.repeater_pins.append((index, ENTITY_INFO["constant-combinator"].outputs[net.color]))
        self.repeaters.append({"net": net_name, "entity_number": index + 1,
                               "position": {"x": position[0], "y": position[1]}})

    def _free_lane_slot(self, x: float, y: float) -> tuple[float, float]:
        """Nearest unused 1x1 slot in the reserved gap column.

        Two per row (the column is two tiles tall), ordered by distance to the
        midpoint the caller wants. If the column fills, spill into columns to
        the RIGHT of the block, which are unpacked by construction — better a
        long repeater than a failed build."""
        candidates = []
        for row in self._row_ys:
            for half in (-0.5, 0.5):
                candidates.append((GAP_COLUMN + 0.5, row + half))
        for extra in range(ROW_COLUMNS, ROW_COLUMNS + 8):
            for row in self._row_ys:
                for half in (-0.5, 0.5):
                    candidates.append((extra + 0.5, row + half))
        for slot in sorted(candidates, key=lambda s: (math.hypot(s[0] - x, s[1] - y), s)):
            if slot not in self._occupied_lane_slots:
                self._occupied_lane_slots.add(slot)
                return slot
        raise NetlistError("no free repeater slot left in the gap column")

    def _pin_distance(self, a, b) -> float:
        (ax, ay) = self.entities[a[0]].position
        (bx, by) = self.entities[b[0]].position
        return math.hypot(ax - bx, ay - by)

    def _mst_edges(self, pins):
        connected = [pins[0]]
        remaining = list(pins[1:])
        edges = []
        while remaining:
            best = min(
                ((a, b) for a in connected for b in remaining),
                key=lambda ab: self._pin_distance(*ab),
            )
            edges.append(best)
            connected.append(best[1])
            remaining.remove(best[1])
        return edges

    # -- emission -----------------------------------------------------------

    def _scoped_signal_decls(self) -> list[tuple[str, SignalDecl]]:
        out = [("file", decl) for decl in self.ast.signals]
        for mdef in self.ast.modules.values():
            out.extend((mdef.name, decl) for decl in mdef.signals)
        return out

    def signals_map(self) -> dict:
        """Declared-name -> allocated-signal map, embedded in the source
        payload so testbenches can drive/expect '$name' symbolically.
        Bare names resolve when unambiguous; 'scope.name' always works."""
        out: dict[str, dict] = {}
        bare_counts: dict[str, int] = {}
        for scope, decl in self._scoped_signal_decls():
            bare_counts[decl.name] = bare_counts.get(decl.name, 0) + 1
        for scope, decl in self._scoped_signal_decls():
            game, quality = decl.resolved
            entry = {
                "type": signal_type(game),
                "name": game,
                "quality": quality,
                "display": game if quality == "normal" else f"{game}~{quality}",
                "auto": decl.auto,
            }
            if decl.address is not None:
                entry["address"] = decl.address
            if decl.accumulate:
                entry["accumulate"] = True
            if {"type": entry["type"], "name": game, "quality": quality} in self.excluded:
                entry["excluded"] = True
            out[f"{scope}.{decl.name}"] = entry
            if bare_counts[decl.name] == 1:
                out[decl.name] = entry
        return out

    def emit_source(self, name: str, wires: list[list[int]]) -> dict:
        entities = []
        for i, ent in enumerate(self.entities):
            info = ENTITY_INFO[ent.prototype]
            record = {
                "entity_number": i + 1,
                "name": ent.prototype,
                "position": {"x": ent.position[0], "y": ent.position[1]},
            }
            if info.direction is not None:
                record["direction"] = info.direction
            if ent.player_description is not None:
                record["player_description"] = ent.player_description
            record["control_behavior"] = ent.control_behavior
            if ent.signal_table is not None:
                record["signal_table"] = (
                    {**ent.signal_table, "exclude": self.excluded}
                    if self.excluded else ent.signal_table
                )
            entities.append(record)
        icons = [{"signal": {"name": self.entities[0].prototype}, "index": 1}] if entities else []
        return {
            "name": name,
            "surface": "nauvis",
            "description": f"compiled by netlist.py from {name}.fnet (module {self.top})",
            "signals": self.signals_map(),
            "blueprint": {
                "icons": icons,
                "entities": entities,
                "wires": wires,
                "item": "blueprint",
                "version": BLUEPRINT_VERSION,
            },
        }

    def emit_debug_map(self) -> dict:
        probes = []
        for probe in self.debug_probes:
            ent = self.entities[probe["entity_number"] - 1]
            probes.append({**probe, "position": {"x": ent.position[0], "y": ent.position[1]}})
        return {"module": self.top, "probes": probes}

    def signal_report(self) -> list[str]:
        lines = []
        for scope, decl in self._scoped_signal_decls():
            game, quality = decl.resolved
            if decl.address is not None:
                kind = f"memory addr {decl.address}"
            elif decl.auto:
                kind = "auto"
            else:
                kind = "pinned"
            lines.append(f"{scope}.{decl.name} = {game}@{quality} ({kind})")
        return lines


# ---------------------------------------------------------------------------
# Wire-partition equivalence (used by tests / diffing against legacy BPs)
# ---------------------------------------------------------------------------

def wire_partition(wires) -> frozenset:
    """Partition of (entity_number, connector) pins into connected components."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a_en, a_conn, b_en, b_conn in wires:
        ra, rb = find((a_en, a_conn)), find((b_en, b_conn))
        if ra != rb:
            parent[ra] = rb
    groups: dict = {}
    for pin in list(parent):
        groups.setdefault(find(pin), set()).add(pin)
    return frozenset(frozenset(g) for g in groups.values())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build(source: Path, top: str | None, out: Path | None, bp_out: Path | None,
          debug_map_out: Path | None, check_only: bool = False) -> Design:
    ast = parse_file(source)
    if top is None:
        if len(ast.modules) == 1:
            top = next(iter(ast.modules))
        elif "main" in ast.modules:
            top = "main"
        else:
            raise NetlistError(
                f"multiple modules ({', '.join(ast.modules)}) — pick one with --top"
            )
    design = Design(ast, top)
    design.validate()
    design.layout()
    wires = design.route()

    stem = source.name[:-len(".fnet")] if source.name.endswith(".fnet") else source.stem
    payload = design.emit_source(stem, wires)

    print(f"module {top}: {len(design.entities)} entities, {len(design.nets)} nets, "
          f"{len(wires)} wires")
    print(f"  layout: {design.layout_choice} order, "
          f"{design.layout_score[1]:.0f} tiles of wire"
          + (f", {len(design.repeaters)} repeaters" if design.repeaters else ""))
    for line in design.signal_report():
        print(f"  signal {line}")
    for note in design.multi_driver_notes:
        print(f"  note: {note}")
    for probe in design.debug_probes:
        print(f"  debug probe: net {probe['net']!r} -> entity {probe['entity_number']} "
              f"({probe['wire']})")

    if not check_only:
        out = out or source.with_name(stem + ".source.json")
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
        if design.debug_probes:
            dm = debug_map_out or source.with_name(stem + ".debug.json")
            dm.write_text(json.dumps(design.emit_debug_map(), indent=2) + "\n",
                          encoding="utf-8")
            print(f"wrote {dm}")
        if bp_out is not None:
            bp = dict(payload["blueprint"])
            bp["entities"] = [
                {k: v for k, v in e.items() if k != "signal_table"}
                for e in bp["entities"]
            ]
            bp_string = encode_blueprint_string({"blueprint": bp})
            bp_out.write_text(bp_string + "\n", encoding="utf-8")
            print(f"wrote {bp_out}")
    return design


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile .fnet netlists to *.source.json")
    sub = parser.add_subparsers(dest="command", required=True)
    for cmd in ("build", "check"):
        p = sub.add_parser(cmd)
        p.add_argument("source", type=Path)
        p.add_argument("--top", default=None)
        if cmd == "build":
            p.add_argument("-o", "--out", type=Path, default=None)
            p.add_argument("--bp", type=Path, default=None)
            p.add_argument("--debug-map", type=Path, default=None)
    args = parser.parse_args()
    try:
        if args.command == "check":
            build(args.source, args.top, None, None, None, check_only=True)
        else:
            build(args.source, args.top, args.out, args.bp, args.debug_map)
    except NetlistError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
