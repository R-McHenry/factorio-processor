#!/usr/bin/env python3
"""Generates the FULL v10 processor (scalar core + vector zone) as one
blueprint, from one flow.

Earlier drafts treated the vector zone as a separate generator to be spliced
into the scalar core afterward. That was wrong: the register bank's write-
address decode taps directly into the scalar core's own write-address-latch
wire (v7/v8's `signal-N`), so the two were never separable subsystems. This
script produces one blueprint, in one pass:

  1. Load the scalar core from modules/processor.source.json (the proven,
     14/14-green v8 master) -- NOT re-derived from scratch. Every signal in
     it is still just a placeholder per the designer (2026-07-21): control-
     plane carriers (write-address latch, write strobe, read-address
     carrier, accumulate masks, mmap mask) are reassigned via
     v10_address_map.CARRIER_SIGNALS, same as everything else in this
     project. ALU/CMP/PC operand signals (the G+H->I family) are explicitly
     KEPT at v8's own letters via v10_address_map.ALU_CORE_SIGNALS -- a
     recorded decision, not untouched leftover; see that module for why.
  2. Generate the vector zone (op farm, R/S-select, reduction farm,
     register bank, port-A extension, quality op) from
     modules/v10_address_map.py, positioned to not collide with the scalar
     core's footprint.
  3. Wire the register bank's write-address decode into the scalar core's
     write-address-latch (entity 2's role) and write-value-latch (entity 9's
     role) outputs, so a chunk2+ write actually routes through the real
     write path rather than sitting next to it unconnected.
  4. Every address-table-role constant (scalar core's 4 + one per vector
     register + the argmax/argmin pair) carries a `signal_table` marker
     (the existing project convention, V8.md) so nothing is left blank --
     no combinator in the output has an empty/unconfigured condition.

The register hold-cell (x=-3) uses a simplified v7-style hold (`each[R]>0
AND each[G]=0 -> each[R]`, no separate reset/injector overlay) rather than
replicating v8's more intricate shared-output-connector injector trick per
register -- flagged as the most speculative piece, worth focusing live
review on.

Usage:
    python tools/generate_v10_processor.py --out modules/v10_full.bp.json --out-string modules/v10_full.bp.txt
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import factorio_blueprint_codec as codec
import modules.v10_address_map as vam
import tools.generate_v10_vector_zone as vz

sig = vz.sig
carrier_sig = vz.carrier_sig
output_sig = vz.output_sig
NET_R = vz.NET_R
NET_G = vz.NET_G
IN_R, IN_G, OUT_R, OUT_G = vz.IN_R, vz.IN_G, vz.OUT_R, vz.OUT_G
BlueprintBuilder = vz.BlueprintBuilder

SCALAR_CORE_SOURCE = Path(__file__).resolve().parent.parent / "modules" / "processor.source.json"

# Structural signals: Factorio's own wildcard operators, not data or control-
# plane carriers. Never substituted, never need a registry entry.
_STRUCTURAL_SIGNALS = {"signal-each", "signal-everything", "signal-anything"}

# Legacy literal signal -> new (name, quality), scoped by the ORIGINAL v8
# entity_number in modules/processor.source.json (stable -- that file is a
# frozen reference, never regenerated).
#
# MUST be entity-scoped, not a flat {(name, quality): role} lookup applied
# everywhere: v8's own design legitimately reuses some literal names for two
# different, electrically-isolated purposes. Found 2026-07-22 in a full
# signal audit: "signal-N" is BOTH entities 2/8/9/14's write-address-latch
# carrier AND entities 49/50's own ALU CMP "N" operand (see
# v10_address_map.ALU_CORE_SIGNALS). A flat substitution renamed BOTH to
# SCALAR_WRITE_ADDR_LATCH's carrier together, silently breaking the CMP
# block's M/N compare -- nothing on that operand's network actually produced
# the new name, so the comparison saw M against an absent signal. Entity-
# scoping makes that class of bug structurally impossible: each entity only
# gets the substitutions listed for its own old entity_number.
#
# Every signal that appears anywhere in the scalar core's control_behavior
# must have an entry here (enforced by load_scalar_core below) -- including
# the ALU/CMP/PC letters, which v10 deliberately KEEPS at v8's own names
# (see v10_address_map.ALU_CORE_SIGNALS) rather than leaving them as
# unowned survivors of "this part of the loaded JSON wasn't touched."
def _entity_subs(*pairs: tuple[str, str]) -> dict[tuple[str, str], tuple[str, str]]:
    return {(name, quality): (name, quality) for name, quality in pairs}


SCALAR_SIGNAL_SUBSTITUTIONS: dict[int, dict[tuple[str, str], tuple[str, str]]] = {
    # write-address latch: v8's own "each+0->N" canonicalizer (2), one-hot
    # match (8), value-latch canonicalizer (9), value*address product (14).
    2: {("signal-N", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_ADDR_LATCH"]},
    8: {("signal-N", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_ADDR_LATCH"]},
    9: {("signal-N", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_ADDR_LATCH"]},
    14: {("signal-N", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_ADDR_LATCH"]},
    # write-enable strobe pulse (separate role from the address latch above,
    # despite the WIP-era "N"/"W" naming looking related): entities 5/10 emit
    # the strobe, 13 gates the write value by it.
    5: {("signal-W", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_STROBE"]},
    10: {("signal-W", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_STROBE"]},
    13: {("signal-W", "normal"): vam.CARRIER_SIGNALS["SCALAR_WRITE_STROBE"]},
    # port-A read-address carrier: unifies with the vector zone's port-ext
    # decode condition (PORTEXT_MATCH below), which checks for this same
    # carrier. Without this substitution entity 37's actual output
    # (literal "signal-R") never matched PORTEXT_MATCH's condition at all --
    # a real, previously-undetected functional bug, not just a stray literal.
    37: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]},
    38: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]},
    39: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"],
         ("signal-P", "normal"): vam.ALU_CORE_SIGNALS["PC"]},
    40: {("signal-P", "normal"): vam.ALU_CORE_SIGNALS["PC"]},
    56: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]},
    57: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]},
    58: {("signal-R", "normal"): vam.CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"]},
    # ALU/CMP operand+result letters: identity (kept as v8's own names, see
    # v10_address_map.ALU_CORE_SIGNALS), recorded explicitly per entity so
    # nothing here is an unowned leftover.
    32: _entity_subs(("signal-J", "normal"), ("signal-K", "normal"), ("signal-L", "normal")),
    33: _entity_subs(("signal-G", "normal"), ("signal-H", "normal"), ("signal-I", "normal")),
    34: _entity_subs(("signal-D", "normal"), ("signal-E", "normal"), ("signal-F", "normal")),
    35: _entity_subs(("signal-A", "normal"), ("signal-B", "normal"), ("signal-C", "normal")),
    49: _entity_subs(("signal-M", "normal"), ("signal-N", "normal")),
    50: _entity_subs(("signal-M", "normal"), ("signal-N", "normal")),
    51: _entity_subs(("signal-M", "normal"), ("signal-N", "normal"), ("signal-Q", "normal")),
    52: _entity_subs(("signal-M", "normal"), ("signal-N", "normal"), ("signal-O", "normal")),
    62: _entity_subs(("signal-S", "normal")),
    63: _entity_subs(("signal-T", "normal")),
    # accumulate-cell mask set (write_trigger_reset)
    19: {
        ("shape-vertical", "normal"): vam.CARRIER_SIGNALS["ACCUMULATE_MASK_1"],
        ("shape-horizontal", "normal"): vam.CARRIER_SIGNALS["ACCUMULATE_MASK_2"],
        ("shape-curve", "normal"): vam.CARRIER_SIGNALS["ACCUMULATE_MASK_3"],
        ("shape-curve-2", "normal"): vam.CARRIER_SIGNALS["ACCUMULATE_MASK_4"],
    },
    # port-B memory-mapped address mask (mmap_addr)
    59: {("shape-circle", "normal"): vam.CARRIER_SIGNALS["MMAP_PORT_B_MASK"]},
    # pc_autoincrement's own config table (kept as PC, no rename)
    29: {("signal-P", "normal"): vam.ALU_CORE_SIGNALS["PC"]},
}


def _collect_signals(obj, found: set[tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        if obj.get("type") == "virtual" and "name" in obj and obj["name"] not in _STRUCTURAL_SIGNALS:
            found.add((obj["name"], obj.get("quality", "normal")))
        for v in obj.values():
            _collect_signals(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _collect_signals(v, found)


def _substitute_signals(obj, subs: dict[tuple[str, str], tuple[str, str]]):
    """Deep-walk a decoded-JSON fragment, replacing any {"type":"virtual",
    "name": X, "quality": Y (or omitted = normal), ...} matching `subs`
    (this entity's own substitution map -- see SCALAR_SIGNAL_SUBSTITUTIONS)
    with the new (name, quality).

    BUG FIXED 2026-07-21: this used to return a bare {"type","name","quality"}
    dict, which is correct for nested signal objects (first_signal/
    second_signal/output_signal) but silently DESTROYED constant-combinator
    filter objects, which are flat and carry index/comparator/count as
    siblings of type/name -- e.g. write_trigger_reset and mmap_addr's
    filters lost their `count`, which is why those two entities failed to
    build even though nothing overlapped them. Now preserves every other
    key on the object, only touching name/quality."""
    if isinstance(obj, dict):
        if obj.get("type") == "virtual" and "name" in obj:
            key = (obj["name"], obj.get("quality", "normal"))
            target = subs.get(key)
            if target is not None:
                name, quality = target
                new = dict(obj)
                new["name"] = name
                if quality != "normal":
                    new["quality"] = quality
                elif "quality" in new:
                    del new["quality"]
                return new
        return {k: _substitute_signals(v, subs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_signals(v, subs) for v in obj]
    return obj


def load_scalar_core(bp: BlueprintBuilder) -> dict[str, int]:
    """Appends the scalar core (renumbered, carriers substituted) into `bp`.
    Returns anchor entity numbers the vector zone's register bank needs to
    wire into."""
    src = json.loads(SCALAR_CORE_SOURCE.read_text(encoding="utf-8"))
    src_bp = src["blueprint"]
    src_entities = src_bp["entities"]
    src_wires = src_bp["wires"]

    # player_description survives the round trip in the source file (unlike
    # the toolchain-populated address tables, which drop control_behavior in
    # favor of signal_table -- handled below).
    old_to_new: dict[int, int] = {}
    desc_by_old_num = {e["entity_number"]: e.get("player_description") for e in src_entities}
    anchors: dict[str, int] = {}

    for e in src_entities:
        old_num = e["entity_number"]
        raw_cb = e.get("control_behavior")
        if raw_cb is not None:
            found: set[tuple[str, str]] = set()
            _collect_signals(raw_cb, found)
            subs = SCALAR_SIGNAL_SUBSTITUTIONS.get(old_num, {})
            unowned = found - set(subs.keys())
            if unowned:
                raise AssertionError(
                    f"scalar core entity {old_num} ({e['name']}) uses signal(s) "
                    f"{sorted(unowned)} with no SCALAR_SIGNAL_SUBSTITUTIONS[{old_num}] "
                    f"entry -- every signal in the loaded scalar core must trace to an "
                    f"explicit v10_address_map.py decision, see generate_v10_processor.py "
                    f"docstring.")
            cb = _substitute_signals(raw_cb, subs)
        else:
            cb = None
        new_e_kwargs = {}
        num = bp._next
        entity = {"entity_number": num, "name": e["name"], "position": dict(e["position"])}
        if e.get("direction") is not None:
            entity["direction"] = e["direction"]
        if e.get("player_description"):
            entity["player_description"] = e["player_description"]
        if e.get("signal_table"):
            entity["signal_table"] = dict(e["signal_table"])
        elif cb is not None:
            entity["control_behavior"] = cb
        bp.entities.append(entity)
        bp._next += 1
        old_to_new[old_num] = num

    for w in src_wires:
        a, ca, b, cb_ = w
        bp.wires.append([old_to_new[a], ca, old_to_new[b], cb_])

    # Anchor points the vector zone needs to tap. Identified by wiring shape
    # (verified live in modules/processor.source.json), not
    # player_description -- most of these entities aren't named in the
    # source file.
    def find_by_shape(old_num_hint: int) -> int:
        return old_to_new[old_num_hint]

    # CORRECTED 2026-07-21: entity 3 is the write-address LATCH (else-branch
    # hold: true->green, else->red -- the classic hold idiom), fed BY entity
    # 2 (`each+0->N`) but NOT the same network -- entity 2's output is a raw
    # transient pass-through, entity 3's output is the held/stable value that
    # entity 8 (the one-hot address match) actually reads. The designer's
    # write/read patch confirmed REG_MATCH needs entity 3's output, not
    # entity 2's -- tapping entity 2 (this generator's original guess) would
    # have fed the register bank a transient signal instead of the latched one.
    anchors["write_addr_latch"] = find_by_shape(3)
    anchors["write_value_latch"] = find_by_shape(9)
    anchors["port_a_read_carrier"] = find_by_shape(37)
    # port_a_rd: the scalar core's own port-A read OUTPUT (named in the
    # source file). PORTEXT_FINAL needs to land its gated read result here.
    anchors["port_a_rd"] = next(
        old_to_new[e["entity_number"]] for e in src_entities if e.get("player_description") == "port_a_rd")
    # "The chunk1 green line that goes everywhere" (designer's phrase): the
    # shared green summing bus carrying ALU results + ROM content, found by
    # tracing wire connectivity (union-find over the wire list) -- the
    # largest green-family network in the scalar core, 15 entities including
    # all four ALU units' out-G and ROM's in-G. Tap point is ROM (entity 41)
    # specifically on its IN-G connector -- that's the connector actually on
    # this network (ROM's out-G is a different, unrelated network).
    anchors["chunk1_green_bus"] = find_by_shape(41)
    return anchors


def build_register_bank(bp: BlueprintBuilder, roles: dict[tuple[str, int], int], y_offset: float = 0.0) -> None:
    """6-column register bank (matches the designer's original demo layout):
    address table -> one-hot match -> value product -> [write-select gate] ->
    hold-cell -> [read-select gate]. Follows v8's proven write-encoder shape
    (entities 2/8/9/14 in modules/processor.source.json).

    CORRECTED 2026-07-21 (address values): no runtime offset-subtraction
    stage. Each register's address table is generated with its own values
    already shifted by the chunk base (full_offset_address_table), so the
    one-hot match compares the raw (global) write-address-latch value
    directly -- this is what the WIP BP's addres_table_ext1/ext2 constants
    were always placeholder stand-ins for, per the designer, not permanent
    boundary markers.

    Entities are created here (registered into `roles` by (ROLE, row)) but
    NOT wired -- wiring is applied uniformly afterward via WIRE_TEMPLATE
    (see apply_wire_template), which encodes the designer's hand-corrected
    live layout exactly."""
    carrier_w = carrier_sig("VEC_WRITE_SELECT")
    carrier_r = carrier_sig("VEC_READ_SELECT_PORT_A")
    carrier_s = carrier_sig("VEC_READ_SELECT_PORT_B")
    addr_latch = carrier_sig("SCALAR_WRITE_ADDR_LATCH")

    x_const, x_match, x_value, x_wsel, x_cell, x_rsel = (-10.5, -9, -7, -5, -3, -1)

    for i in range(vam.NUM_VECTOR_REGISTERS):
        index = i + 1  # 1-based, matches W/R/S convention
        y = vz.row_y(index) + y_offset
        chunk_base = vam.register_chunk_base(i) - 1

        roles[("REG_CONST", i)] = bp.add(
            "constant-combinator", x_const, y,
            vz.full_offset_address_table(chunk_base), player_description=f"addres_table_reg{i}")

        # e_match mirrors v8 entity 8 exactly: each[R, table] = addr_latch[G, fixed] -> each[G] (one-hot).
        roles[("REG_MATCH", i)] = bp.add("decider-combinator", x_match, y, {
            "decider_conditions": {
                "conditions": [{"first_signal": sig("signal-each"), "second_signal": addr_latch,
                                 "comparator": "=", "first_signal_networks": NET_R,
                                 "second_signal_networks": NET_G}],
                "outputs": [{"signal": sig("signal-each"), "copy_count_from_input": False, "networks": NET_G}],
                "else_outputs": [],
            }
        })
        roles[("REG_VALUE", i)] = bp.add("arithmetic-combinator", x_value, y, {
            "arithmetic_conditions": {
                "first_signal": sig("signal-each"), "second_signal": addr_latch,
                "operation": "*", "output_signal": sig("signal-each"),
                "first_signal_networks": NET_R, "second_signal_networks": NET_G,
            }
        })
        roles[("REG_WSEL", i)] = bp.add("decider-combinator", x_wsel, y, vz.write_select_gate(index, carrier_w))
        roles[("REG_CELL", i)] = bp.add("decider-combinator", x_cell, y, {
            "decider_conditions": {
                "conditions": [{"first_signal": sig("signal-each"), "comparator": ">",
                                 "first_signal_networks": NET_R}],
                "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
                "else_outputs": [],
            }
        })
        roles[("REG_RSEL", i)] = bp.add("decider-combinator", x_rsel, y, vz.select_gate(index, carrier_r, carrier_s))


def build_vector_zone_minus_registers(bp: BlueprintBuilder, roles: dict[tuple[str, int], int],
                                       y_offset: float = 0.0) -> None:
    """Op farm, reduction farm, quality op, and port-A extension (everything
    in the vector zone except the register bank, built separately above by
    build_register_bank). Entities only -- wiring applied afterward by
    apply_wire_template."""
    carrier_r = carrier_sig("VEC_READ_SELECT_PORT_A")
    carrier_s = carrier_sig("VEC_READ_SELECT_PORT_B")

    # op farm rows 0..19 = R9..R28 (op_rows order), row 20 = quality-transfer
    # selector. Row numbering matches the designer's corrected export
    # (0-based, top = R9) -- WIRE_TEMPLATE depends on this exact convention.
    op_rows = [row[1] for row in vam.READ_SELECT_TABLE if row[0] >= 9]
    for i, op_name in enumerate(op_rows):
        op = next(o for o in vam.ALL_OPS if o.name == op_name)
        y = vz.row_y(1 + i) + y_offset
        roles[("OPFARM", i)] = bp.add(vz.op_entity_type(op), vz.X_OPFARM, y, vz.op_control_behavior(op))
        roles[("OPFARM_RSEL", i)] = bp.add(
            "decider-combinator", vz.X_OPFARM_RSEL, y,
            vz.select_gate(vam.READ_SELECT_INDEX[op_name], carrier_r, carrier_s))

    quality_request = carrier_sig("VQUALITY_REQUEST_LEVEL")
    quality_marker_name, _ = vam.CARRIER_SIGNALS["VQUALITY_TRANSFER_MARKER"]
    y21 = vz.row_y(21) + y_offset
    # CORRECTED 2026-07-22: was hardcoded literal "signal-W" (both here and
    # in quality_source_signal below) -- now owned via CARRIER_SIGNALS like
    # everything else. Only the NAME is reused across all 5 rows (matching
    # quality_source_signal's requirement); qualities still span all 5 tiers
    # since that's the actual lookup table content, not a carrier pick.
    roles[("QUAL_TABLE", 0)] = bp.add("constant-combinator", vz.X_QUAL_TABLE, y21, {
        "sections": {"sections": [{"index": 1, "filters": [
            {"index": q + 1, "type": "virtual", "name": quality_marker_name, "quality": quality,
             "comparator": "=", "count": q + 1}
            for q, quality in enumerate(["normal", "uncommon", "rare", "epic", "legendary"])
        ]}]}
    })
    # CORRECTED 2026-07-21 ("flip the coloring"): each on RED, carrier on
    # GREEN -- opposite of the original proposal. Matches the designer's
    # live-tested fix (entity 42 in the corrected export) exactly.
    roles[("QUAL_DECODE", 0)] = bp.add("decider-combinator", vz.X_QUAL_DECODE, y21, {
        "decider_conditions": {
            "conditions": [{"first_signal": sig("signal-each"), "second_signal": quality_request,
                             "comparator": "=", "first_signal_networks": NET_R,
                             "second_signal_networks": NET_G}],
            "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
            "else_outputs": [],
        }
    })
    roles[("OPFARM", 20)] = bp.add("selector-combinator", vz.X_OPFARM, y21, vz.selector({
        "operation": "quality-transfer", "select_quality_from_signal": True,
        "quality_source_static": {"name": "legendary"},
        # full (name, quality) pair, not just the name -- consistent with
        # every other carrier reference (2026-07-22 signal audit).
        "quality_source_signal": carrier_sig("VQUALITY_TRANSFER_MARKER"),
        "quality_destination_signal": sig("signal-each"),
    }))
    # OPFARM_RSEL row 20 (aligned with the quality-selector row) is a
    # duplicate of the R=28 gate in the corrected export -- not a real 29th
    # read-select value, just chain continuity at that row. Replicated as-is.
    carrier_r_dup = carrier_sig("VEC_READ_SELECT_PORT_A")
    carrier_s_dup = carrier_sig("VEC_READ_SELECT_PORT_B")
    roles[("OPFARM_RSEL", 20)] = bp.add(
        "decider-combinator", vz.X_OPFARM_RSEL, y21, vz.select_gate(28, carrier_r_dup, carrier_s_dup))

    b1 = carrier_sig("VSSCAL_BROADCAST_OPERAND_1")
    b2 = carrier_sig("VSSCAL_BROADCAST_OPERAND_2")
    cmp_thresh = carrier_sig("VREDUCE_CMP_THRESHOLD")
    reduce_rows = [
        ("VSSCAL_MUL_REDUCE", lambda: vz.reduce_scal_arith("*", b1, output_sig("VSSCAL_MUL_REDUCE"))),
        ("VSSCAL_DIV_REDUCE", lambda: vz.reduce_scal_arith("/", b2, output_sig("VSSCAL_DIV_REDUCE"))),
        ("VSSCAL_ADD_REDUCE", lambda: vz.reduce_scal_arith("+", b2, output_sig("VSSCAL_ADD_REDUCE"))),
        ("VSSCAL_SUB_REDUCE", lambda: vz.reduce_scal_arith("-", b2, output_sig("VSSCAL_SUB_REDUCE"))),
        ("VREDUCE_COUNT", lambda: vz.selector({"operation": "count", "count_signal": output_sig("VREDUCE_COUNT")})),
        ("VREDUCE_TIME", lambda: vz.selector({"operation": "time",
                                               "game_tick_signal": output_sig("VREDUCE_TIME_TICK"),
                                               "day_tick_signal": output_sig("VREDUCE_TIME_DAY_TICK"),
                                               "day_length_signal": output_sig("VREDUCE_TIME_DAY_LENGTH")})),
        ("VREDUCE_MAX", lambda: vz.selector({"operation": "select", "select_max": True, "index_constant": 0})),
        ("VREDUCE_MIN", lambda: vz.selector({"operation": "select", "select_max": False, "index_constant": 0})),
        ("VREDUCE_CMP_GT", lambda: vz.cmp_vs_broadcast(">", cmp_thresh, output_sig("VREDUCE_CMP_GT"))),
        ("VREDUCE_CMP_LT", lambda: vz.cmp_vs_broadcast(None, cmp_thresh, output_sig("VREDUCE_CMP_LT"))),
        ("VREDUCE_CMP_EQ", lambda: vz.cmp_vs_broadcast("=", cmp_thresh, output_sig("VREDUCE_CMP_EQ"))),
    ]
    entity_types = {
        "VSSCAL_MUL_REDUCE": "arithmetic-combinator", "VSSCAL_DIV_REDUCE": "arithmetic-combinator",
        "VSSCAL_ADD_REDUCE": "arithmetic-combinator", "VSSCAL_SUB_REDUCE": "arithmetic-combinator",
        "VREDUCE_COUNT": "selector-combinator", "VREDUCE_TIME": "selector-combinator",
        "VREDUCE_MAX": "selector-combinator", "VREDUCE_MIN": "selector-combinator",
        "VREDUCE_CMP_GT": "decider-combinator", "VREDUCE_CMP_LT": "decider-combinator",
        "VREDUCE_CMP_EQ": "decider-combinator",
    }
    for i, (name, cb_fn) in enumerate(reduce_rows):
        y = vz.row_y(i + 1) + y_offset
        roles[("REDUCE", i)] = bp.add(entity_types[name], vz.X_REDUCE, y, cb_fn())

    y7 = vz.row_y(7) + y_offset
    y8 = vz.row_y(8) + y_offset
    roles[("ARGVAL", 6)] = bp.add("arithmetic-combinator", vz.X_ARGVAL, y7, vz.relabel_each(output_sig("VREDUCE_MAX")))
    roles[("ARGVAL", 7)] = bp.add("arithmetic-combinator", vz.X_ARGVAL, y8, vz.relabel_each(output_sig("VREDUCE_MIN")))

    roles[("ARGDECODE", 6)] = bp.add("decider-combinator", vz.X_ARGDECODE, y7, vz.addr_table_onehot_decode())
    roles[("ARGDECODE", 7)] = bp.add("decider-combinator", vz.X_ARGDECODE, y8, vz.addr_table_onehot_decode())
    roles[("ARGTABLE", 6)] = bp.add("constant-combinator", vz.X_ARGTABLE, y7, signal_table="full_address_space")
    roles[("ARGTABLE", 7)] = bp.add("constant-combinator", vz.X_ARGTABLE, y8, signal_table="full_address_space")
    roles[("ARGOUT", 6)] = bp.add("arithmetic-combinator", vz.X_ARGOUT, y7, vz.relabel_each(output_sig("VREDUCE_ARGMAX")))
    roles[("ARGOUT", 7)] = bp.add("arithmetic-combinator", vz.X_ARGOUT, y8, vz.relabel_each(output_sig("VREDUCE_ARGMIN")))

    # Port A beyond-chunk1 read extension, scope confirmed as port-A-only for
    # v10 (2 chunks: register 0's and register 1's). addres_table_ext1/ext2
    # are full offset address tables (labeled to match), not single
    # boundary-value placeholders -- a one-hot match against the live port-A
    # read-address carrier (tapped from the scalar core's own entity 37)
    # rather than a threshold compare.
    for i in range(2):
        chunk_base = vam.register_chunk_base(i) - 1
        y = vz.row_y(i + 1) + y_offset
        roles[("PORTEXT_CONST", i)] = bp.add(
            "constant-combinator", vz.X_PORTEXT_CONST, y,
            vz.full_offset_address_table(chunk_base), player_description=f"addres_table_ext{i + 1}")
        roles[("PORTEXT_MATCH", i)] = bp.add("decider-combinator", vz.X_PORTEXT_MATCH, y, {
            "decider_conditions": {
                # CORRECTED 2026-07-22: SCALAR_READ_ADDR_CARRIER is now a
                # normal auto-ranked carrier_sig() draw like every other
                # role -- no special case at assignment time. The
                # non-addressable requirement is enforced the other way
                # around: v10_address_map.py adds whatever this resolves to
                # into signal_space.EXCLUDED *after* the draw, so
                # full_offset_address_table() above (and chunk1's own
                # signal_table population) both exclude it automatically.
                "conditions": [{"first_signal": sig("signal-each"),
                                 "second_signal": carrier_sig("SCALAR_READ_ADDR_CARRIER"),
                                 "comparator": "=", "first_signal_networks": NET_R,
                                 "second_signal_networks": NET_G}],
                "outputs": [{"signal": sig("signal-each"), "copy_count_from_input": False, "networks": NET_R}],
                "else_outputs": [],
            }
        })
        roles[("PORTEXT_FINAL", i)] = bp.add("decider-combinator", vz.X_PORTEXT_FINAL, y, {
            "decider_conditions": {
                "conditions": [{"first_signal": sig("signal-each"), "comparator": ">",
                                 "constant": 0, "first_signal_networks": NET_G}],
                "outputs": [{"signal": sig("signal-each"), "networks": NET_R}],
                "else_outputs": [],
            }
        })


# ---------------------------------------------------------------------------
# Wire template: the designer's hand-corrected live layout (2026-07-21),
# extracted mechanically from a cleared+re-exported blueprint (role/row
# assignment verified by position; see tools/ chat history for the
# extraction script). Applied uniformly instead of hand-written per-section
# wiring, so it can't silently drift from what was actually verified live.
# Connector ids: 1=in-R, 2=in-G, 3=out-R, 4=out-G.
# ---------------------------------------------------------------------------
WIRE_TEMPLATE: list[tuple[tuple[str, int, int], tuple[str, int, int]]] = [
    (('REG_CONST', 7, 1), ('REG_MATCH', 7, 1)),
    (('REG_CONST', 6, 1), ('REG_MATCH', 6, 1)),
    (('REG_CONST', 5, 1), ('REG_MATCH', 5, 1)),
    (('REG_CONST', 4, 1), ('REG_MATCH', 4, 1)),
    (('REG_CONST', 3, 1), ('REG_MATCH', 3, 1)),
    (('REG_CONST', 2, 1), ('REG_MATCH', 2, 1)),
    (('REG_CONST', 1, 1), ('REG_MATCH', 1, 1)),
    (('REG_CONST', 0, 1), ('REG_MATCH', 0, 1)),
    (('REG_MATCH', 7, 2), ('REG_MATCH', 6, 2)),
    (('REG_MATCH', 7, 3), ('REG_VALUE', 7, 1)),
    (('REG_MATCH', 6, 2), ('REG_MATCH', 5, 2)),
    (('REG_MATCH', 6, 3), ('REG_VALUE', 6, 1)),
    (('REG_MATCH', 5, 2), ('REG_MATCH', 4, 2)),
    (('REG_MATCH', 5, 3), ('REG_VALUE', 5, 1)),
    (('REG_MATCH', 4, 2), ('REG_MATCH', 3, 2)),
    (('REG_MATCH', 4, 3), ('REG_VALUE', 4, 1)),
    (('REG_MATCH', 3, 2), ('REG_MATCH', 2, 2)),
    (('REG_MATCH', 3, 3), ('REG_VALUE', 3, 1)),
    (('REG_MATCH', 2, 2), ('REG_MATCH', 1, 2)),
    (('REG_MATCH', 2, 3), ('REG_VALUE', 2, 1)),
    (('REG_MATCH', 1, 2), ('REG_MATCH', 0, 2)),
    (('REG_MATCH', 1, 3), ('REG_VALUE', 1, 1)),
    (('REG_MATCH', 0, 3), ('REG_VALUE', 0, 1)),
    (('REG_VALUE', 7, 2), ('REG_VALUE', 6, 2)),
    (('REG_VALUE', 7, 3), ('REG_CELL', 7, 1)),
    (('REG_VALUE', 6, 2), ('REG_VALUE', 5, 2)),
    (('REG_VALUE', 6, 3), ('REG_CELL', 6, 1)),
    (('REG_VALUE', 5, 2), ('REG_VALUE', 4, 2)),
    (('REG_VALUE', 5, 3), ('REG_CELL', 5, 1)),
    (('REG_VALUE', 4, 2), ('REG_VALUE', 3, 2)),
    (('REG_VALUE', 4, 3), ('REG_CELL', 4, 1)),
    (('REG_VALUE', 3, 2), ('REG_VALUE', 2, 2)),
    (('REG_VALUE', 3, 3), ('REG_CELL', 3, 1)),
    (('REG_VALUE', 2, 2), ('REG_VALUE', 1, 2)),
    (('REG_VALUE', 2, 3), ('REG_CELL', 2, 1)),
    (('REG_VALUE', 1, 2), ('REG_VALUE', 0, 2)),
    (('REG_VALUE', 1, 3), ('REG_CELL', 1, 1)),
    (('REG_VALUE', 0, 3), ('REG_CELL', 0, 1)),
    (('REG_WSEL', 7, 2), ('REG_WSEL', 6, 2)),
    (('REG_WSEL', 6, 2), ('REG_WSEL', 5, 2)),
    (('REG_WSEL', 5, 2), ('REG_WSEL', 4, 2)),
    (('REG_WSEL', 4, 2), ('REG_WSEL', 3, 2)),
    (('REG_WSEL', 3, 2), ('REG_WSEL', 2, 2)),
    (('REG_WSEL', 2, 2), ('REG_WSEL', 1, 2)),
    (('REG_WSEL', 1, 2), ('REG_WSEL', 0, 2)),
    (('REG_WSEL', 0, 2), ('REG_CELL', 0, 2)),
    (('REG_CELL', 7, 1), ('REG_CELL', 7, 3)),
    (('REG_CELL', 7, 2), ('REG_CELL', 6, 2)),
    (('REG_CELL', 7, 3), ('REG_RSEL', 7, 1)),
    (('REG_CELL', 6, 1), ('REG_CELL', 6, 3)),
    (('REG_CELL', 6, 2), ('REG_CELL', 5, 2)),
    (('REG_CELL', 6, 3), ('REG_RSEL', 6, 1)),
    (('REG_CELL', 5, 1), ('REG_CELL', 5, 3)),
    (('REG_CELL', 5, 2), ('REG_CELL', 4, 2)),
    (('REG_CELL', 5, 3), ('REG_RSEL', 5, 1)),
    (('REG_CELL', 4, 1), ('REG_CELL', 4, 3)),
    (('REG_CELL', 4, 2), ('REG_CELL', 3, 2)),
    (('REG_CELL', 4, 3), ('REG_RSEL', 4, 1)),
    (('REG_CELL', 3, 1), ('REG_CELL', 3, 3)),
    (('REG_CELL', 3, 2), ('REG_CELL', 2, 2)),
    (('REG_CELL', 3, 3), ('REG_RSEL', 3, 1)),
    (('REG_CELL', 2, 1), ('REG_CELL', 2, 3)),
    (('REG_CELL', 2, 2), ('REG_CELL', 1, 2)),
    (('REG_CELL', 2, 3), ('REG_RSEL', 2, 1)),
    (('REG_CELL', 1, 1), ('REG_CELL', 1, 3)),
    (('REG_CELL', 1, 2), ('REG_CELL', 0, 2)),
    (('REG_CELL', 1, 3), ('PORTEXT_FINAL', 1, 3)),
    (('REG_CELL', 1, 3), ('REG_RSEL', 1, 1)),
    (('REG_CELL', 1, 4), ('OPFARM', 0, 2)),
    (('REG_CELL', 0, 1), ('REG_CELL', 0, 3)),
    (('REG_CELL', 0, 2), ('REG_RSEL', 0, 2)),
    (('REG_CELL', 0, 3), ('PORTEXT_FINAL', 0, 3)),
    (('REG_CELL', 0, 3), ('REG_RSEL', 0, 1)),
    (('REG_CELL', 0, 3), ('OPFARM', 0, 1)),
    (('QUAL_TABLE', 0, 1), ('QUAL_DECODE', 0, 1)),
    (('QUAL_DECODE', 0, 2), ('OPFARM', 19, 2)),
    (('QUAL_DECODE', 0, 4), ('OPFARM', 20, 2)),
    (('REG_RSEL', 7, 2), ('REG_RSEL', 6, 2)),
    (('REG_RSEL', 7, 3), ('REG_RSEL', 6, 3)),
    (('REG_RSEL', 6, 2), ('REG_RSEL', 5, 2)),
    (('REG_RSEL', 6, 3), ('REG_RSEL', 5, 3)),
    (('REG_RSEL', 5, 2), ('REG_RSEL', 4, 2)),
    (('REG_RSEL', 5, 3), ('REG_RSEL', 4, 3)),
    (('REG_RSEL', 4, 2), ('REG_RSEL', 3, 2)),
    (('REG_RSEL', 4, 3), ('REG_RSEL', 3, 3)),
    (('REG_RSEL', 3, 2), ('REG_RSEL', 2, 2)),
    (('REG_RSEL', 3, 3), ('REG_RSEL', 2, 3)),
    (('REG_RSEL', 2, 2), ('REG_RSEL', 1, 2)),
    (('REG_RSEL', 2, 3), ('REG_RSEL', 1, 3)),
    (('REG_RSEL', 1, 2), ('REG_RSEL', 0, 2)),
    (('REG_RSEL', 1, 3), ('REG_RSEL', 0, 3)),
    (('REG_RSEL', 0, 2), ('OPFARM_RSEL', 0, 2)),
    # CORRECTED 2026-07-22 (write-vector patch): REG_WSEL's IN_R was
    # completely unwired before this -- a write-select gate needs SOMETHING
    # on its IN_R to gate through when W matches, and nothing fed it. Chained
    # across all 8 rows like every other shared bus here, then bridged
    # directly onto OPFARM_RSEL's OUT_R chain (the "ALU output selector
    # bus"): writing a register isn't only a scalar-address write anymore --
    # it can also pull from whatever vector source R/S currently has
    # selected onto the shared bus, gated through by W.
    (('REG_WSEL', 7, 1), ('REG_WSEL', 6, 1)),
    (('REG_WSEL', 6, 1), ('REG_WSEL', 5, 1)),
    (('REG_WSEL', 5, 1), ('REG_WSEL', 4, 1)),
    (('REG_WSEL', 4, 1), ('REG_WSEL', 3, 1)),
    (('REG_WSEL', 3, 1), ('REG_WSEL', 2, 1)),
    (('REG_WSEL', 2, 1), ('REG_WSEL', 1, 1)),
    (('REG_WSEL', 1, 1), ('REG_WSEL', 0, 1)),
    (('REG_WSEL', 0, 1), ('OPFARM_RSEL', 0, 3)),
    # MISSED IN THE FIRST PASS (2026-07-22): the actual per-row gated output
    # -- WSEL[i]'s OUT_R feeding CELL[i]'s IN_R -- confirmed from the same
    # patch (col1.row_i.c3 -> col2.row_i.c1 for all 8 rows). Without this,
    # WSEL's IN_R bus above has nowhere to go and the gate does nothing.
    # Coexists with REG_VALUE[i].OUT_R -> REG_CELL[i].IN_R (unchanged,
    # below): the scalar-address write path already self-gates via its own
    # address-table match, so both sources can safely feed the same cell.
    (('REG_WSEL', 7, 3), ('REG_CELL', 7, 1)),
    (('REG_WSEL', 6, 3), ('REG_CELL', 6, 1)),
    (('REG_WSEL', 5, 3), ('REG_CELL', 5, 1)),
    (('REG_WSEL', 4, 3), ('REG_CELL', 4, 1)),
    (('REG_WSEL', 3, 3), ('REG_CELL', 3, 1)),
    (('REG_WSEL', 2, 3), ('REG_CELL', 2, 1)),
    (('REG_WSEL', 1, 3), ('REG_CELL', 1, 1)),
    (('REG_WSEL', 0, 3), ('REG_CELL', 0, 1)),
    # REG_RSEL's own OUT_R chain (0-7, already wired above) was never
    # bridged to OPFARM_RSEL's OUT_R chain -- meaning register-select and
    # op-select formed two SEPARATE output networks despite the design
    # calling for one shared vector bus ("the two selected frames sum onto
    # one shared vector bus"). Same patch, same fix: merge them.
    (('REG_RSEL', 0, 3), ('OPFARM_RSEL', 0, 3)),
    (('OPFARM', 20, 1), ('OPFARM', 19, 1)),
    (('OPFARM', 20, 3), ('OPFARM_RSEL', 20, 1)),
    (('OPFARM', 19, 1), ('OPFARM', 18, 1)),
    (('OPFARM', 19, 2), ('OPFARM', 18, 2)),
    (('OPFARM', 19, 3), ('OPFARM_RSEL', 19, 1)),
    (('OPFARM', 18, 1), ('OPFARM', 17, 1)),
    (('OPFARM', 18, 2), ('OPFARM', 17, 2)),
    (('OPFARM', 18, 3), ('OPFARM_RSEL', 18, 1)),
    (('OPFARM', 17, 1), ('OPFARM', 16, 1)),
    (('OPFARM', 17, 2), ('OPFARM', 16, 2)),
    (('OPFARM', 17, 3), ('OPFARM_RSEL', 17, 1)),
    (('OPFARM', 16, 1), ('OPFARM', 15, 1)),
    (('OPFARM', 16, 2), ('OPFARM', 15, 2)),
    (('OPFARM', 16, 3), ('OPFARM_RSEL', 16, 1)),
    (('OPFARM', 15, 1), ('OPFARM', 14, 1)),
    (('OPFARM', 15, 2), ('OPFARM', 14, 2)),
    (('OPFARM', 15, 3), ('OPFARM_RSEL', 15, 1)),
    (('OPFARM', 14, 1), ('OPFARM', 13, 1)),
    (('OPFARM', 14, 2), ('OPFARM', 13, 2)),
    (('OPFARM', 14, 3), ('OPFARM_RSEL', 14, 1)),
    (('OPFARM', 13, 1), ('OPFARM', 12, 1)),
    (('OPFARM', 13, 2), ('OPFARM', 12, 2)),
    (('OPFARM', 13, 3), ('OPFARM_RSEL', 13, 1)),
    (('OPFARM', 12, 1), ('OPFARM', 11, 1)),
    (('OPFARM', 12, 2), ('OPFARM_RSEL', 12, 2)),
    (('OPFARM', 12, 3), ('OPFARM_RSEL', 12, 1)),
    (('OPFARM', 11, 1), ('OPFARM', 10, 1)),
    (('OPFARM', 11, 2), ('OPFARM', 10, 2)),
    (('OPFARM', 11, 3), ('OPFARM_RSEL', 11, 1)),
    (('OPFARM', 10, 1), ('OPFARM', 9, 1)),
    (('OPFARM', 10, 2), ('OPFARM', 9, 2)),
    (('OPFARM', 10, 3), ('OPFARM_RSEL', 10, 1)),
    (('OPFARM', 9, 1), ('OPFARM', 8, 1)),
    (('OPFARM', 9, 2), ('OPFARM', 8, 2)),
    (('OPFARM', 9, 3), ('OPFARM_RSEL', 9, 1)),
    (('OPFARM', 8, 1), ('OPFARM', 7, 1)),
    (('OPFARM', 8, 2), ('OPFARM', 7, 2)),
    (('OPFARM', 8, 3), ('OPFARM_RSEL', 8, 1)),
    (('OPFARM', 7, 1), ('OPFARM', 6, 1)),
    (('OPFARM', 7, 2), ('OPFARM', 6, 2)),
    (('OPFARM', 7, 3), ('OPFARM_RSEL', 7, 1)),
    (('OPFARM', 6, 1), ('OPFARM', 5, 1)),
    (('OPFARM', 6, 2), ('OPFARM', 5, 2)),
    (('OPFARM', 6, 3), ('OPFARM_RSEL', 6, 1)),
    (('OPFARM', 5, 1), ('OPFARM', 4, 1)),
    (('OPFARM', 5, 2), ('OPFARM', 4, 2)),
    (('OPFARM', 5, 3), ('OPFARM_RSEL', 5, 1)),
    (('OPFARM', 4, 1), ('OPFARM', 3, 1)),
    (('OPFARM', 4, 2), ('OPFARM', 3, 2)),
    (('OPFARM', 4, 3), ('OPFARM_RSEL', 4, 1)),
    (('OPFARM', 3, 1), ('OPFARM', 2, 1)),
    (('OPFARM', 3, 2), ('OPFARM', 2, 2)),
    (('OPFARM', 3, 3), ('OPFARM_RSEL', 3, 1)),
    (('OPFARM', 2, 1), ('OPFARM', 1, 1)),
    (('OPFARM', 2, 2), ('OPFARM', 1, 2)),
    (('OPFARM', 2, 3), ('OPFARM_RSEL', 2, 1)),
    (('OPFARM', 1, 1), ('OPFARM', 0, 1)),
    (('OPFARM', 1, 2), ('OPFARM', 0, 2)),
    (('OPFARM', 1, 3), ('OPFARM_RSEL', 1, 1)),
    (('OPFARM', 0, 3), ('OPFARM_RSEL', 0, 1)),
    (('OPFARM_RSEL', 20, 2), ('OPFARM_RSEL', 19, 2)),
    (('OPFARM_RSEL', 20, 3), ('OPFARM_RSEL', 19, 3)),
    (('OPFARM_RSEL', 19, 2), ('OPFARM_RSEL', 18, 2)),
    (('OPFARM_RSEL', 19, 3), ('OPFARM_RSEL', 18, 3)),
    (('OPFARM_RSEL', 18, 2), ('OPFARM_RSEL', 17, 2)),
    (('OPFARM_RSEL', 18, 3), ('OPFARM_RSEL', 17, 3)),
    (('OPFARM_RSEL', 17, 2), ('OPFARM_RSEL', 16, 2)),
    (('OPFARM_RSEL', 17, 3), ('OPFARM_RSEL', 16, 3)),
    (('OPFARM_RSEL', 16, 2), ('OPFARM_RSEL', 15, 2)),
    (('OPFARM_RSEL', 16, 3), ('OPFARM_RSEL', 15, 3)),
    (('OPFARM_RSEL', 15, 2), ('OPFARM_RSEL', 14, 2)),
    (('OPFARM_RSEL', 15, 3), ('OPFARM_RSEL', 14, 3)),
    (('OPFARM_RSEL', 14, 2), ('OPFARM_RSEL', 13, 2)),
    (('OPFARM_RSEL', 14, 3), ('OPFARM_RSEL', 13, 3)),
    (('OPFARM_RSEL', 13, 2), ('OPFARM_RSEL', 12, 2)),
    (('OPFARM_RSEL', 13, 3), ('OPFARM_RSEL', 12, 3)),
    (('OPFARM_RSEL', 12, 2), ('OPFARM_RSEL', 11, 2)),
    (('OPFARM_RSEL', 12, 3), ('OPFARM_RSEL', 11, 3)),
    (('OPFARM_RSEL', 11, 2), ('OPFARM_RSEL', 10, 2)),
    (('OPFARM_RSEL', 11, 3), ('OPFARM_RSEL', 10, 3)),
    (('OPFARM_RSEL', 10, 2), ('OPFARM_RSEL', 9, 2)),
    (('OPFARM_RSEL', 10, 3), ('OPFARM_RSEL', 9, 3)),
    (('OPFARM_RSEL', 9, 2), ('OPFARM_RSEL', 8, 2)),
    (('OPFARM_RSEL', 9, 3), ('OPFARM_RSEL', 8, 3)),
    (('OPFARM_RSEL', 8, 2), ('OPFARM_RSEL', 7, 2)),
    (('OPFARM_RSEL', 8, 3), ('OPFARM_RSEL', 7, 3)),
    (('OPFARM_RSEL', 7, 2), ('OPFARM_RSEL', 6, 2)),
    (('OPFARM_RSEL', 7, 3), ('OPFARM_RSEL', 6, 3)),
    (('OPFARM_RSEL', 6, 2), ('OPFARM_RSEL', 5, 2)),
    (('OPFARM_RSEL', 6, 3), ('OPFARM_RSEL', 5, 3)),
    (('OPFARM_RSEL', 5, 2), ('OPFARM_RSEL', 4, 2)),
    (('OPFARM_RSEL', 5, 3), ('OPFARM_RSEL', 4, 3)),
    (('OPFARM_RSEL', 4, 2), ('OPFARM_RSEL', 3, 2)),
    (('OPFARM_RSEL', 4, 3), ('OPFARM_RSEL', 3, 3)),
    (('OPFARM_RSEL', 3, 2), ('OPFARM_RSEL', 2, 2)),
    (('OPFARM_RSEL', 3, 3), ('OPFARM_RSEL', 2, 3)),
    (('OPFARM_RSEL', 2, 2), ('OPFARM_RSEL', 1, 2)),
    (('OPFARM_RSEL', 2, 3), ('OPFARM_RSEL', 1, 3)),
    (('OPFARM_RSEL', 1, 2), ('OPFARM_RSEL', 0, 2)),
    (('OPFARM_RSEL', 1, 3), ('OPFARM_RSEL', 0, 3)),
    (('PORTEXT_CONST', 1, 1), ('PORTEXT_MATCH', 1, 1)),
    (('PORTEXT_CONST', 0, 1), ('PORTEXT_MATCH', 0, 1)),
    (('PORTEXT_MATCH', 1, 4), ('PORTEXT_FINAL', 1, 2)),
    (('PORTEXT_MATCH', 0, 4), ('PORTEXT_FINAL', 0, 2)),
    (('PORTEXT_FINAL', 1, 4), ('PORTEXT_FINAL', 0, 4)),
    (('REDUCE', 10, 1), ('REDUCE', 9, 1)),
    (('REDUCE', 10, 2), ('REDUCE', 9, 2)),
    (('REDUCE', 10, 4), ('REDUCE', 9, 4)),
    (('REDUCE', 9, 1), ('REDUCE', 8, 1)),
    (('REDUCE', 9, 2), ('REDUCE', 8, 2)),
    (('REDUCE', 9, 4), ('REDUCE', 8, 4)),
    (('REDUCE', 8, 1), ('REDUCE', 7, 1)),
    (('REDUCE', 8, 2), ('REDUCE', 7, 2)),
    (('REDUCE', 8, 4), ('ARGVAL', 7, 4)),
    (('REDUCE', 7, 1), ('REDUCE', 6, 1)),
    (('REDUCE', 7, 2), ('REDUCE', 6, 2)),
    (('REDUCE', 7, 4), ('ARGVAL', 7, 2)),
    (('REDUCE', 7, 4), ('ARGDECODE', 7, 2)),
    (('REDUCE', 6, 1), ('REDUCE', 5, 1)),
    (('REDUCE', 6, 2), ('REDUCE', 5, 2)),
    (('REDUCE', 6, 4), ('ARGVAL', 6, 2)),
    (('REDUCE', 6, 4), ('ARGDECODE', 6, 2)),
    (('REDUCE', 5, 1), ('REDUCE', 4, 1)),
    (('REDUCE', 5, 2), ('REDUCE', 4, 2)),
    (('REDUCE', 5, 4), ('REDUCE', 4, 4)),
    (('REDUCE', 5, 4), ('ARGVAL', 6, 4)),
    (('REDUCE', 4, 1), ('REDUCE', 3, 1)),
    (('REDUCE', 4, 2), ('REDUCE', 3, 2)),
    (('REDUCE', 4, 4), ('REDUCE', 3, 4)),
    (('REDUCE', 3, 1), ('REDUCE', 2, 1)),
    (('REDUCE', 3, 2), ('REDUCE', 2, 2)),
    (('REDUCE', 3, 4), ('REDUCE', 2, 4)),
    (('REDUCE', 2, 1), ('REDUCE', 1, 1)),
    (('REDUCE', 2, 2), ('REDUCE', 1, 2)),
    (('REDUCE', 2, 4), ('REDUCE', 1, 4)),
    (('REDUCE', 1, 1), ('REDUCE', 0, 1)),
    (('REDUCE', 1, 2), ('REDUCE', 0, 2)),
    (('REDUCE', 1, 4), ('REDUCE', 0, 4)),
    (('REDUCE', 0, 2), ('REDUCE', 0, 4)),
    (('ARGVAL', 7, 4), ('ARGVAL', 6, 4)),
    (('ARGVAL', 7, 4), ('ARGOUT', 7, 4)),
    (('ARGVAL', 6, 4), ('ARGOUT', 6, 4)),
    (('ARGDECODE', 7, 1), ('ARGTABLE', 7, 1)),
    (('ARGDECODE', 7, 4), ('ARGOUT', 7, 2)),
    (('ARGDECODE', 6, 1), ('ARGTABLE', 6, 1)),
    (('ARGDECODE', 6, 4), ('ARGOUT', 6, 2)),
]


def apply_wire_template(bp: BlueprintBuilder, roles: dict[tuple[str, int], int]) -> None:
    for (role_a, row_a, conn_a), (role_b, row_b, conn_b) in WIRE_TEMPLATE:
        a = roles[(role_a, row_a)]
        b = roles[(role_b, row_b)]
        bp.wire(a, conn_a, b, conn_b)


def wire_external_anchors(bp: BlueprintBuilder, roles: dict[tuple[str, int], int], anchors: dict[str, int]) -> None:
    """Connections to the scalar core that WIRE_TEMPLATE can't encode (the
    designer's corrected export excluded the scalar core, so these never
    appear in it). Tap points chosen as the natural "row 0 / start of chain"
    entity for each bus WIRE_TEMPLATE builds -- REDUCE's tap to
    chunk1_green_bus and to the register-0 cell (for its primary vector
    operand) are this generator's own inference, not directly confirmed in
    the correction; flagged for review."""
    bp.wire(roles[("REG_MATCH", 0)], IN_G, anchors["write_addr_latch"], OUT_G)
    bp.wire(roles[("REG_VALUE", 0)], IN_G, anchors["write_value_latch"], OUT_G)
    # REG_WSEL[0] is the entry point of the big merged W/R/S/broadcast bus
    # (WSEL -> CELL -> RSEL -> OPFARM_RSEL -> OPFARM[12:20] -> QUAL_DECODE,
    # all one network per WIRE_TEMPLATE) -- one tap here reaches all of it.
    bp.wire(roles[("REG_WSEL", 0)], IN_G, anchors["chunk1_green_bus"], IN_G)
    # REDUCE's own broadcast-operand green chain is a separate network (no
    # link to the WSEL/RSEL bus was observed) -- tap it independently.
    bp.wire(roles[("REDUCE", 0)], IN_G, anchors["chunk1_green_bus"], IN_G)
    # INFERENCE (not in the correction): REDUCE needs a primary vector
    # operand the same way OPFARM does; wiring it to the same source
    # (register 0's cell) for consistency. Confirm intent before trusting.
    bp.wire(roles[("REDUCE", 0)], IN_R, roles[("REG_CELL", 0)], OUT_R)
    bp.wire(roles[("PORTEXT_MATCH", 0)], IN_G, anchors["port_a_read_carrier"], OUT_G)
    bp.wire(roles[("PORTEXT_MATCH", 1)], IN_G, anchors["port_a_read_carrier"], OUT_G)
    # CORRECTED 2026-07-21 (write/read patch): PORTEXT_FINAL's gated read
    # result needs to actually land on the scalar core's port-A read output,
    # or nothing downstream ever sees it.
    bp.wire(roles[("PORTEXT_FINAL", 0)], OUT_G, anchors["port_a_rd"], IN_G)
    bp.wire(roles[("PORTEXT_FINAL", 1)], OUT_G, anchors["port_a_rd"], IN_G)


def build_full(bp: BlueprintBuilder) -> None:
    anchors = load_scalar_core(bp)
    # Scalar core spans x=-10.5..10.5, y=-6.5..6 (modules/processor.source.json).
    # Shift the vector zone's y so its footprint clears that with margin.
    # -4.0 confirmed clear live by the designer (tighter than the original -10.0).
    y_offset = -4.0
    roles: dict[tuple[str, int], int] = {}
    build_register_bank(bp, roles, y_offset)
    build_vector_zone_minus_registers(bp, roles, y_offset)
    apply_wire_template(bp, roles)
    wire_external_anchors(bp, roles, anchors)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="Write decoded-JSON blueprint here")
    ap.add_argument("--out-string", help="Write encoded blueprint STRING here")
    args = ap.parse_args()

    bp = BlueprintBuilder()
    build_full(bp)
    decoded = bp.to_dict()
    decoded["blueprint"]["label"] = "v10 processor (generated, scalar+vector unified)"

    if args.out:
        Path(args.out).write_text(json.dumps(decoded, indent=1), encoding="utf-8")
        print(f"wrote {args.out} ({len(bp.entities)} entities, {len(bp.wires)} wires)")
    if args.out_string:
        s = codec.encode_blueprint_string(decoded)
        Path(args.out_string).write_text(s, encoding="utf-8")
        print(f"wrote {args.out_string} ({len(s)} chars)")
    if not args.out and not args.out_string:
        print(json.dumps(decoded, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
