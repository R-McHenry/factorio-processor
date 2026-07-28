#!/usr/bin/env python3
"""Shared helpers for the v10 processor generator: signal/carrier lookups,
op-shape builders, the BlueprintBuilder, and the wire-connector constants.

modules/v10_address_map.py is the source of truth for ops, read-select
order, and carrier-signal assignment; modules/processor_v10.design.md is the
narrative writeup. tools/generate_v10_processor.py is the actual entry point
(scalar core + vector zone, one unified flow) and imports this module for
its building blocks -- this file has no CLI of its own and produces no
blueprint by itself (an earlier standalone `build()`/`main()` pair here was
superseded 2026-07-21 by the unified generator and removed 2026-07-22, since
it wasn't kept in sync with later carrier-ownership fixes and had drifted to
producing structurally-broken output).

Entity JSON shapes below are copied from decoded live combinators (the WIP
BP + two patches reviewed 2026-07-21), not guessed -- only the carrier
SIGNAL CHOICE is substituted (v10_address_map.py's ranked carriers replace
the WIP BP's placeholder alphanumeric picks; nothing about signal identity
is taken from any BP).

Coverage:
  - Op farm (vec-vec arith/conditional, vec-scal-vec arith/conditional) + the
    R/S read-select gate chain that reads it: CONFIRMED, real carriers.
  - Reduction farm (vec-scal-scal reduce, count/time/max/min/argmax/argmin,
    CMP-vs-broadcast): CONFIRMED, real carriers.
  - Register bank write-select (W) + read-select (R/S) gates: CONFIRMED.
  - Register bank write-address-decode scaffold (x=-10.5/-9/-7) and the
    register hold-cells themselves (x=-3): STRUCTURAL ONLY. The WIP BP's own
    demo leaves these as uniform placeholders (literal "signal-N" on every
    row, "signal-no-entry != N" defaults) -- there's no decided per-register
    identity scheme yet, so this generator reproduces the placeholder shape
    rather than inventing one. Marked PLACEHOLDER in the output.
  - Port-A beyond-chunk1 read extension: CONFIRMED chunk-boundary compare,
    proposed (not live-verified) final gate `each[G] > 0 -> each[R]`.
  - VVEC_QUALITY_SET: proposed (not live-verified) fill-in per the design
    doc.

Usage: none directly -- see tools/generate_v10_processor.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import modules.v10_address_map as vam
import signal_space as ss

IN_R, IN_G, OUT_R, OUT_G = 1, 2, 3, 4


def sig(name: str, quality: str = "normal") -> dict:
    d = {"type": "virtual", "name": name}
    if quality != "normal":
        d["quality"] = quality
    return d


def carrier_sig(role: str) -> dict:
    name, quality = vam.CARRIER_SIGNALS[role]
    return sig(name, quality)


def output_sig(op_name: str) -> dict:
    name, quality = vam.REDUCTION_OUTPUT_SIGNALS[op_name]
    return sig(name, quality)


NET_R = {"red": True, "green": False}
NET_G = {"red": False, "green": True}


class BlueprintBuilder:
    def __init__(self):
        self.entities: list[dict] = []
        self.wires: list[list[int]] = []
        self._next = 1

    # Combinators default to north-facing (1 wide x 2 tall). The whole layout
    # (row pitch 1.0, column pitch ~2.0) is calibrated for the rotated,
    # east-facing footprint (2 wide x 1 tall) -- confirmed live: every
    # decider/arithmetic/selector-combinator in the designer's layout demo
    # carries direction=4. Constant-combinators are 1x1 and don't need it.
    ROTATED_TYPES = {"decider-combinator", "arithmetic-combinator", "selector-combinator"}

    def add(self, name: str, x: float, y: float, control_behavior: dict | None = None,
            signal_table: str | None = None, player_description: str | None = None) -> int:
        num = self._next
        self._next += 1
        ent = {"entity_number": num, "name": name, "position": {"x": x, "y": y}}
        if name in self.ROTATED_TYPES:
            ent["direction"] = 4
        if player_description is not None:
            ent["player_description"] = player_description
        if signal_table is not None:
            ent["signal_table"] = {"table": signal_table}
        elif control_behavior is not None:
            ent["control_behavior"] = control_behavior
        self.entities.append(ent)
        return num

    def wire(self, a: int, ca: int, b: int, cb: int) -> None:
        self.wires.append([a, ca, b, cb])

    def to_dict(self) -> dict:
        return {
            "blueprint": {
                "icons": [{"signal": {"name": "decider-combinator"}, "index": 1}],
                "entities": self.entities,
                "wires": self.wires,
                "item": "blueprint",
                "version": 562954249175042,
                "label": "v10 vector zone (generated)",
            }
        }


# ---------------------------------------------------------------------------
# Entity-shape helpers (copied from decoded live combinators)
# ---------------------------------------------------------------------------


def vec_vec_arith(operation: str) -> dict:
    """each[R] op each[G] -> each. Confirmed live, e.g. old-BP entities 74-81."""
    return {"arithmetic_conditions": {
        "first_signal": sig("signal-each"), "second_signal": sig("signal-each"),
        "operation": operation, "output_signal": sig("signal-each"),
        "first_signal_networks": NET_R, "second_signal_networks": NET_G,
    }}


def vec_vec_gate(comparator: str | None) -> dict:
    """each[R] <cmp> each[G] -> each[R], else nothing. GT_GATE/LT_GATE.
    comparator=None reproduces the confirmed live default (in-game shows '<')."""
    cond = {"first_signal": sig("signal-each"), "second_signal": sig("signal-each"),
            "first_signal_networks": NET_R, "second_signal_networks": NET_G}
    if comparator is not None:
        cond["comparator"] = comparator
    return {"decider_conditions": {
        "conditions": [cond],
        "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
        "else_outputs": [],
    }}


def vec_vec_selmaxmin(comparator: str | None) -> dict:
    """each[R] <cmp> each[G] -> each[R] (true) else each[G] (false, opposite
    color). MAX/MIN: red+green sum on the shared vector bus -> true elementwise
    max/min. Confirmed live, old-BP entities 72/73."""
    cond = {"first_signal": sig("signal-each"), "second_signal": sig("signal-each"),
            "first_signal_networks": NET_R, "second_signal_networks": NET_G}
    if comparator is not None:
        cond["comparator"] = comparator
    return {"decider_conditions": {
        "conditions": [cond],
        "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
        "else_outputs": [{"signal": sig("signal-each"), "networks": NET_G}],
    }}


def vec_scal_vec_arith(operation: str, broadcast: dict) -> dict:
    """each[R] op BROADCAST[G] -> each. Confirmed live, old-BP entities 62-65."""
    return {"arithmetic_conditions": {
        "first_signal": sig("signal-each"), "second_signal": broadcast,
        "operation": operation, "output_signal": sig("signal-each"),
        "first_signal_networks": NET_R, "second_signal_networks": NET_G,
    }}


def vec_scal_vec_mask(comparator: str | None, broadcast: dict) -> dict:
    """each[R] <cmp> BROADCAST[G] -> 1 (mask, not the value). GT_MASK/LT_MASK.
    Confirmed live, old-BP entities 66/67."""
    cond = {"first_signal": sig("signal-each"), "second_signal": broadcast,
            "first_signal_networks": NET_R, "second_signal_networks": NET_G}
    if comparator is not None:
        cond["comparator"] = comparator
    return {"decider_conditions": {
        "conditions": [cond],
        "outputs": [{"signal": sig("signal-each"), "copy_count_from_input": False, "networks": NET_R}],
        "else_outputs": [],
    }}


def vec_scal_vec_select(comparator: str | None, broadcast: dict) -> dict:
    """each[R] <cmp> BROADCAST[G] -> each[R] (the value). GT_SELECT/LT_SELECT.
    Confirmed live, old-BP entities 68/69."""
    cond = {"first_signal": sig("signal-each"), "second_signal": broadcast,
            "first_signal_networks": NET_R, "second_signal_networks": NET_G}
    if comparator is not None:
        cond["comparator"] = comparator
    return {"decider_conditions": {
        "conditions": [cond],
        "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
        "else_outputs": [],
    }}


def select_gate(index: int, carrier_a: dict, carrier_b: dict) -> dict:
    """R == N OR S == N -> everything(red). Confirmed live shape; compare_type
    explicitly set to "or" (designer-confirmed semantics: two independent
    selectors, not the implicit-AND my first read of the JSON wrongly assumed)."""
    return {"decider_conditions": {
        "conditions": [
            {"first_signal": carrier_a, "constant": index, "comparator": "=",
             "first_signal_networks": NET_G},
            {"first_signal": carrier_b, "constant": index, "comparator": "=",
             "first_signal_networks": NET_G, "compare_type": "or"},
        ],
        "outputs": [{"signal": sig("signal-everything"), "networks": NET_R}],
        "else_outputs": [],
    }}


def write_select_gate(index: int, carrier_w: dict) -> dict:
    """W == N -> everything(red). Confirmed live, old-BP entities 46-53."""
    return {"decider_conditions": {
        "conditions": [{"first_signal": carrier_w, "constant": index, "comparator": "=",
                         "first_signal_networks": NET_G}],
        "outputs": [{"signal": sig("signal-everything"), "networks": NET_R}],
        "else_outputs": [],
    }}


def reduce_scal_arith(operation: str, broadcast: dict, output: dict) -> dict:
    """each[R] op BROADCAST[G] -> OUTPUT (Factorio sums all matching lanes
    into the single fixed output signal). Confirmed live, old-BP 142/148/152/156."""
    return {"arithmetic_conditions": {
        "first_signal": sig("signal-each"), "second_signal": broadcast,
        "operation": operation, "output_signal": output,
        "first_signal_networks": NET_R, "second_signal_networks": NET_G,
    }}


def relabel_each(output: dict) -> dict:
    """each+0 -> OUTPUT. Confirmed live (M/N/U/V relabeling, argmax patch)."""
    return {"arithmetic_conditions": {
        "first_signal": sig("signal-each"), "second_constant": 0, "operation": "+",
        "output_signal": output,
    }}


def cmp_vs_broadcast(comparator: str | None, broadcast: dict, output: dict) -> dict:
    """each <cmp> BROADCAST -> 1 (mask, on green). CMP-style GT/LT/EQ.
    Confirmed live, old-BP entities 172/175/176."""
    cond = {"first_signal": sig("signal-each"), "second_signal": broadcast,
            "first_signal_networks": NET_R, "second_signal_networks": NET_G}
    if comparator is not None:
        cond["comparator"] = comparator
    return {"decider_conditions": {
        "conditions": [cond],
        "outputs": [{"signal": output, "copy_count_from_input": False, "networks": NET_G}],
        "else_outputs": [],
    }}


def full_offset_address_table(offset: int) -> dict:
    """A complete, fully-populated address table (every entry from
    signal_space's canonical table), each value shifted by `offset` --
    i.e. a chunk-relative address table, generated inline rather than via
    the signal_table post-paste marker.

    DECIDED 2026-07-21 (correcting an earlier misreading): the WIP BP's
    single-value `addres_table_ext1`/`ext2` constants (`signal-0=2481`,
    `signal-0=4961`) were never meant to be permanent chunk-boundary
    markers -- they were placeholder stand-ins for exactly this: a full
    address table whose values are offset by the target chunk's base, so a
    one-hot match against it works directly off the raw (global) incoming
    address with no separate subtraction stage needed. Chunked ~150-row
    signal_table population isn't used here -- the generator emits the
    complete content inline, so nothing needs a post-paste step.

    Drops exactly one row: SCALAR_READ_ADDR_CARRIER's assigned (name,
    quality) pair, which must not be valid addressable data (2026-07-22,
    see modules/v10_address_map.py). Filtered here, after the full
    2480-entry table is built, rather than by shrinking signal_space's
    canonical table -- so this removes that one quality tier only, not all
    5 tiers of the name."""
    excluded_name, excluded_quality = vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]
    entries = [
        e for e in ss.full_table()
        if not (e["name"] == excluded_name and e["quality"] == excluded_quality)
    ]
    sections: list[dict] = []
    current: list[dict] = []
    section_index = 1
    for i, entry in enumerate(entries):
        slot = i % 1000
        if slot == 0 and current:
            sections.append({"index": section_index, "filters": current})
            section_index += 1
            current = []
        current.append({
            "index": slot + 1, "type": entry["type"], "name": entry["name"],
            "quality": entry["quality"], "comparator": "=",
            "count": entry["address"] + offset,
        })
    if current:
        sections.append({"index": section_index, "filters": current})
    return {"sections": {"sections": sections}}


def addr_table_onehot_decode() -> dict:
    """each[G] != 0 -> each[R] (address-table one-hot match). Confirmed live,
    argmax/argmin patch entities 9/14, and port-extension entities."""
    return {"decider_conditions": {
        "conditions": [{"first_signal": sig("signal-each"), "comparator": "≠",
                         "first_signal_networks": NET_G}],
        "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
        "else_outputs": [],
    }}


def selector(operation_dict: dict) -> dict:
    return dict(operation_dict)


# ---------------------------------------------------------------------------
# Layout (x per role; confirmed from the 2026-07-21 layout demo)
# ---------------------------------------------------------------------------

X_REG_CONST = -10.5   # placeholder: per-register address-table constant
X_REG_MATCH = -9      # placeholder: per-register one-hot match decider
X_REG_VALUE = -7      # placeholder: per-register value-multiply arith
X_REG_WSEL = -5       # write-select gate (W == index), CONFIRMED
X_REG_CELL = -3       # register hold-cell, PLACEHOLDER (unconfigured stub live)
X_REG_RSEL = -1       # read-select gate (R or S == index), CONFIRMED
X_QUAL_TABLE = -2.5   # quality-level reference table
X_QUAL_DECODE = -1    # quality-level decode gate (shares the reg-rsel column, y=-23.5)

X_OPFARM = 1          # op source combinators (vec-vec, vec-scal-vec) + quality selector
X_OPFARM_RSEL = 3     # read-select gate for op-farm sources

X_PORTEXT_CONST = 4.5
X_PORTEXT_MATCH = 6
X_PORTEXT_FINAL = 8   # proposed, not live-verified: each[G] > 0 -> each[R]

X_REDUCE = 10          # reduction farm
X_ARGVAL = 12          # MAX/MIN value relabel
X_ARGDECODE = 14       # ARGMAX/ARGMIN one-hot decode gate
X_ARGTABLE = 15.5      # address table feeding the decode gate
X_ARGOUT = 17          # ARGMAX/ARGMIN relabel

ROW = 1.0  # vertical spacing
Y0 = -3.5  # row 1 (bottom-most) y coordinate


def row_y(index_1based: int) -> float:
    return Y0 - (index_1based - 1) * ROW


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def op_control_behavior(op: vam.VectorOp) -> dict:
    broadcast = carrier_sig("VSVEC_BROADCAST_OPERAND")
    n, cat = op.name, op.category
    if cat == "vec_vec":
        if op.op in ("XOR", "OR", "AND", "^", "%", "-", "/", "*"):
            return vec_vec_arith(op.op)
        if n.endswith("_GATE"):
            comparator = ">" if n.startswith("VVEC_GT") else None
            return vec_vec_gate(comparator)
        if n in ("VVEC_MAX", "VVEC_MIN"):
            comparator = ">" if n == "VVEC_MAX" else None
            return vec_vec_selmaxmin(comparator)
    if cat == "vec_scal_vec":
        if op.op in ("-", "+", "/", "*"):
            return vec_scal_vec_arith(op.op, broadcast)
        if n.endswith("_MASK"):
            comparator = ">" if "GT" in n else None
            return vec_scal_vec_mask(comparator, broadcast)
        if n.endswith("_SELECT"):
            comparator = ">" if "GT" in n else None
            return vec_scal_vec_select(comparator, broadcast)
    raise ValueError(f"no op_control_behavior rule for {n} ({cat})")


OP_ARITH_OPS = {"XOR", "OR", "AND", "^", "%", "-", "/", "*", "+"}


def op_entity_type(op: vam.VectorOp) -> str:
    """vec-vec/vec-scal-vec arithmetic ops are arithmetic-combinators; the
    conditional families (GATE/MAX/MIN/MASK/SELECT) are decider-combinators."""
    if op.op in OP_ARITH_OPS:
        return "arithmetic-combinator"
    return "decider-combinator"


