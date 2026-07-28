#!/usr/bin/env python3
"""Self-tests for netlist.py (plain asserts, run with the venv python).

Covers NETLIST_PLAN.md migration step 1: parsing, template substitution,
signal allocation, module nesting, structural validation, reach checking,
debug probes — plus step 2's static half: the compiled netlist_demo module
must be entity- and connectivity-equivalent to modules/demo_circuit.source.json.
"""
import json
import tempfile
from pathlib import Path

from netlist import (
    Design, NetlistError, auto_signal_pool, build, parse_file, wire_partition,
)

ROOT = Path(__file__).resolve().parent

GATE = """
gate = decider-combinator({
  "decider_conditions": {
    "conditions": [
      { "first_signal": { "type": "virtual", "name": "signal-each" }, "comparator": ">" }
    ],
    "outputs": [ { "signal": { "type": "virtual", "name": "signal-each" } } ]
  }
});
"""

CONST = """
const = constant-combinator({
  "sections": { "sections": [ { "index": 1, "filters": [] } ] }
});
"""


def compile_text(text: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.fnet"
        path.write_text(text, encoding="utf-8")
        ast = parse_file(path)
        top = "main" if "main" in ast.modules else next(iter(ast.modules))
        design = Design(ast, top)
        design.validate()
        design.layout()
        return design, design.wires()


def expect_error(text: str, fragment: str):
    try:
        compile_text(text)
    except NetlistError as exc:
        assert fragment in str(exc), f"expected {fragment!r} in error, got: {exc}"
        return
    raise AssertionError(f"expected NetlistError containing {fragment!r}")


def test_demo_matches_demo_circuit_topology():
    design = build(ROOT / "modules" / "netlist_demo.fnet", None, None, None, None,
                   check_only=True)
    wires = design.wires()
    payload = design.emit_source("netlist_demo", wires)
    reference = json.loads(
        (ROOT / "modules" / "demo_circuit.source.json").read_text(encoding="utf-8")
    )
    ref_entities = reference["blueprint"]["entities"]
    got_entities = payload["blueprint"]["entities"]
    assert len(got_entities) == len(ref_entities) == 6
    for got, ref in zip(got_entities, ref_entities):
        assert got["entity_number"] == ref["entity_number"]
        assert got["name"] == ref["name"], (got["entity_number"], got["name"], ref["name"])
        assert got.get("direction") == ref.get("direction")
    # the memory gate is signal-independent and must match demo_circuit verbatim
    assert got_entities[1]["control_behavior"] == ref_entities[1]["control_behavior"]
    # wire lists may differ (MST vs hand chaining) but connectivity must not
    assert wire_partition(payload["blueprint"]["wires"]) == \
        wire_partition(reference["blueprint"]["wires"])
    # the debug mark produced the probe on entity 6, green — what the tb probes
    assert design.debug_probes == [
        {"net": "read_out", "entity_number": 6, "wire": "green"}
    ]


def test_demo_auto_carrier_flows_everywhere():
    design = build(ROOT / "modules" / "netlist_demo.fnet", None, None, None, None,
                   check_only=True)
    payload = design.emit_source("netlist_demo", design.wires())
    carrier_name, carrier_quality = next(iter(auto_signal_pool()))
    signals = payload["signals"]
    assert signals["read_addr"]["name"] == carrier_name
    assert signals["read_addr"]["quality"] == carrier_quality
    assert signals["read_addr"]["auto"] is True
    assert signals["read_addr"]["display"] == f"{carrier_name}~{carrier_quality}"
    ents = payload["blueprint"]["entities"]
    # read stim filter (entity 3) carries the allocated carrier
    filt = ents[2]["control_behavior"]["sections"]["sections"][0]["filters"][0]
    assert filt["name"] == carrier_name and filt["quality"] == carrier_quality
    # address table (entity 4) has the marker + ONLY the explicitly excluded
    # carrier row — exclusion is stated in the fnet, never compiler-injected
    table = ents[3]["signal_table"]
    assert table["table"] == "full_address_space"
    assert table["exclude"] == [
        {"type": "virtual", "name": carrier_name, "quality": carrier_quality}
    ]
    # read gate (entity 5) compares against the carrier
    cond = ents[4]["control_behavior"]["decider_conditions"]["conditions"][0]
    assert cond["second_signal"]["name"] == carrier_name
    assert cond["second_signal"]["quality"] == carrier_quality
    # the marker never reaches control_behavior
    assert "signal_table" not in ents[3]["control_behavior"]


def test_lift_out_player_description():
    design, _ = compile_text("""
labeled = constant-combinator({
  "player_description": "my_block",
  "sections": { "sections": [ { "index": 1, "filters": [] } ] }
});
module main() {
  net red out;
  labeled(out_r: out);
  debug out;
}
""")
    payload = design.emit_source("t", [])
    ent = payload["blueprint"]["entities"][0]
    assert ent["player_description"] == "my_block"
    assert "player_description" not in ent["control_behavior"]


def test_undriven_net_errors():
    expect_error(GATE + """
module main() {
  net red a;
  net green sink;
  gate(in_r: a, out_g: sink);
  debug sink;
}
""", "has no driver")


def test_unconsumed_net_errors():
    expect_error(GATE + """
module main() {
  net red a;
  gate(out_r: a);
}
""", "has no consumer")


def test_unconnected_net_errors():
    expect_error(GATE + """
module main() {
  net red a, b;
  net green sink;
  gate(in_r: a, out_r: a, out_g: sink);
  debug sink;
}
""", "never connected")


def test_top_ports_waive_requirements():
    design, wires = compile_text(GATE + """
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""")
    assert len(design.entities) == 1
    assert wires == []  # single-pin nets emit no wires


def test_debug_waives_consumer_and_adds_probe():
    design, wires = compile_text(GATE + """
module main(in_r a) {
  debug net green q;
  gate(in_r: a, out_g: q);
}
""")
    assert design.entities[1].prototype == "constant-combinator"
    assert wires == [[1, 4, 2, 2]]  # gate out_g -> probe green connector
    assert design.emit_debug_map()["probes"][0]["net"] == "q"


def test_color_mismatch_errors():
    expect_error(GATE + """
module main(in_r a) {
  net green g;
  gate(in_r: g, out_r: a);
}
""", "is green")


def test_constant_has_no_inputs():
    expect_error(CONST + """
module main(in_r a) {
  const(in_r: a);
}
""", "no in_r connector")


def test_duplicate_port_fill_errors():
    expect_error(GATE + """
module main(in_r a, in_r b, out_g q) {
  gate(in_r: a, in_r: b, out_g: q);
}
""", "connected twice")


def test_signal_pin_collision_errors():
    expect_error(GATE + """
signal x = signal-W;
signal y = signal-W;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "already owned")


def test_unknown_signal_name_errors():
    expect_error(GATE + """
signal x = signal-ZZZ;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "unknown game signal")


def test_auto_allocation_deterministic_and_skips_pins():
    first, second = list(auto_signal_pool())[:2]
    src = GATE + f"""
signal pinned = {first[0]}@{first[1]};
signal auto1;
module main(in_r a, out_g q) {{
  gate(in_r: a, out_g: q);
}}
"""
    d1, _ = compile_text(src)
    d2, _ = compile_text(src)
    autos1 = [s.resolved for s in d1.ast.signals if s.auto]
    autos2 = [s.resolved for s in d2.ast.signals if s.auto]
    assert autos1 == autos2 == [second]  # auto skipped the pinned candidate


def test_param_substitution_signal_int_quality():
    design, _ = compile_text("""
sel = selector-combinator({
  "operation": "quality-transfer",
  "quality_source_signal": { [param0] },
  "index_constant": [param1]
});
signal w = signal-W@rare;
module main(in_r a, out_g q) {
  sel<w, 7>(in_r: a, out_g: q);
}
""")
    cb = design.entities[0].control_behavior
    assert cb["quality_source_signal"] == \
        {"type": "virtual", "name": "signal-W", "quality": "rare"}
    assert cb["index_constant"] == 7


def test_param_count_mismatch_errors():
    expect_error("""
sel = selector-combinator({ "x": { [param0] }, "y": { [param1] } });
signal w = signal-W;
module main(in_r a, out_g q) {
  sel<w>(in_r: a, out_g: q);
}
""", "got 1 argument")


def test_module_nesting_two_instances():
    design, wires = compile_text(GATE + """
module cell(in_g w, out_g q) {
  net red loop;
  gate(in_g: w, in_r: loop, out_r: loop, out_g: q);
}
module main(in_g stim) {
  debug net green q0, q1;
  cell(w: stim, q: q0);
  cell(w: stim, q: q1);
}
""")
    # 2 cells + 2 probes; each cell got its own hierarchical loop net
    assert len(design.entities) == 4
    assert "cell0.loop" in design.nets and "cell1.loop" in design.nets
    # self-loop wire on each gate: input red 1 <-> output red 3
    assert [1, 1, 1, 3] in wires and [2, 1, 2, 3] in wires


def test_module_port_color_mismatch_errors():
    expect_error(GATE + """
module cell(in_r w, out_g q) {
  gate(in_r: w, out_g: q);
}
module main(in_g stim) {
  debug net green q0;
  cell(w: stim, q: q0);
}
""", "bound net")


def test_module_recursion_errors():
    expect_error("""
module main(in_r a) {
  main(a: a);
}
""", "recursive")


def test_positional_binds_by_color_and_assignment_by_color():
    design, wires = compile_text(GATE + """
module main(out_g result) {
  net red data;
  net green enable;
  gate(out_r: data);
  gate(out_g: enable);
  result = gate(data, in_g: enable);
}
""")
    net = design.nets["data"]
    assert (2, 1) in net.consumer_pins   # positional red -> in_r (connector 1)
    assert design.nets["result"].driver_pins == [(2, 4)]  # assignment -> out_g


# A long net stretched apart by 35 unconnected fillers. The layout optimizer
# clusters net-mates, so this only exceeds reach under DECLARATION order —
# which is exactly what the two tests below pin down: wires() is the strict
# checker at a given placement, route() is the repairing build path.
REACH_FIXTURE = GATE + CONST + """
module main() {{
  net red a;
  net green s;
  gate(out_r: a);
  {fillers}
  gate(in_r: a, out_g: s);
  debug s;
}}
""".format(fillers="\n".join("const();" for _ in range(35)))


def declaration_order_design(text: str):
    """Compile, then force the un-optimized packing (worst case on purpose)."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.fnet"
        path.write_text(text, encoding="utf-8")
        design = Design(parse_file(path), "main")
    design.validate()
    design._pack(list(range(len(design.entities))))
    return design


def test_wire_reach_error():
    design = declaration_order_design(REACH_FIXTURE)
    try:
        design.wires()
    except NetlistError as exc:
        assert "> reach" in str(exc), exc
        return
    raise AssertionError("expected a reach violation under declaration order")


def test_layout_optimizer_avoids_the_reach_violation():
    design, wires = compile_text(REACH_FIXTURE)
    assert design.layout_choice != "declaration"
    assert not design.repeaters, "clustering alone should have sufficed"
    assert wires


def test_route_repairs_reach_with_inert_repeaters():
    design = declaration_order_design(REACH_FIXTURE)
    before = len(design.entities)
    wires = design.route()
    assert design.repeaters, "route() should have inserted repeaters"
    assert len(design.entities) == before + len(design.repeaters)
    for repeater in design.repeaters:
        entity = design.entities[repeater["entity_number"] - 1]
        assert entity.prototype == "constant-combinator"
        # inert: no filters means it drives nothing, so the repeater is a pure
        # reach anchor on the same electrical network — zero added latency
        assert entity.control_behavior["sections"]["sections"][0]["filters"] == []
    # the two gate pins still share one network, now via the repeaters
    partition = wire_partition(wires)
    driver = next(p for p in partition if (1, 3) in p)
    assert (37, 1) in driver


def test_no_exclusion_leaves_marker_untouched():
    design, _ = compile_text("""
tbl = constant-combinator({
  "signal_table": { "table": "full_address_space" },
  "sections": { "sections": [ { "index": 1, "filters": [] } ] }
});
signal carrier;
module main() {
  net red bus;
  tbl(out_r: bus);
  debug bus;
}
""")
    payload = design.emit_source("t", design.wires())
    assert payload["blueprint"]["entities"][0]["signal_table"] == \
        {"table": "full_address_space"}


def test_exclusion_compacts_the_address_space():
    import signal_space as ss
    carrier_name, carrier_quality = next(iter(auto_signal_pool()))
    raw = {int(e["address"]): e for e in ss.full_table()}
    excluded_addr = next(
        a for a, e in raw.items()
        if (e["name"], e["quality"]) == (carrier_name, carrier_quality)
    )
    design, _ = compile_text(GATE + f"""
signal carrier;
exclude carrier;
signal shifted = memory[{excluded_addr}];
module main(in_r a, out_g q) {{
  gate(in_r: a, out_g: q);
}}
""")
    # no gap: the excluded row's address now belongs to the NEXT raw row
    assert design.address_space[excluded_addr]["name"] == raw[excluded_addr + 1]["name"]
    assert (carrier_name, carrier_quality) not in {
        (r["name"], r["quality"]) for r in design.address_space.values()
    }
    assert len(design.address_space) == len(raw) - 1
    assert max(design.address_space) == len(raw) - 1  # contiguous 1..n-1
    shifted = next(d for d in design.ast.signals if d.name == "shifted")
    assert shifted.resolved == (raw[excluded_addr + 1]["name"],
                                raw[excluded_addr + 1]["quality"])
    # the marker-driven runner path regenerates the identical numbering
    from factorio_memory_tb import signal_table_rows
    rows = signal_table_rows([(carrier_name, carrier_quality)])
    assert len(rows) == len(raw) - 1
    assert rows[excluded_addr - 1]["name"] == raw[excluded_addr + 1]["name"]
    assert rows[excluded_addr - 1]["count"] == excluded_addr


def test_exclusion_flags_and_errors():
    design, _ = compile_text(GATE + """
signal carrier;
exclude carrier;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""")
    payload = design.emit_source("t", [])
    assert payload["signals"]["carrier"]["excluded"] is True
    expect_error(GATE + """
exclude ghost;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "undeclared signal")
    expect_error(GATE + """
signal p = memory[206];
exclude p;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "cannot exclude memory-mapped")
    expect_error("""
tbl = constant-combinator({
  "signal_table": { "table": "full_address_space", "exclude": [] },
  "sections": { "sections": [ { "index": 1, "filters": [] } ] }
});
module main() {
  net red bus;
  tbl(out_r: bus);
  debug bus;
}
""", "design-wide")


def test_memory_mapped_signal_resolves_via_address_table():
    import signal_space as ss
    by_addr = {int(e["address"]): e for e in ss.full_table()}
    base, offset = 2090, 3
    entry = by_addr[base + offset]
    design, _ = compile_text(GATE + f"""
region alu = {base};
signal operand = memory[alu + {offset}];
signal direct = memory[206];
module main(in_r a, out_g q) {{
  gate(in_r: a, out_g: q);
}}
""")
    ops = {d.name: d for d in design.ast.signals}
    assert ops["operand"].resolved == (entry["name"], entry["quality"])
    assert ops["operand"].address == base + offset
    assert ops["direct"].resolved == \
        (by_addr[206]["name"], by_addr[206]["quality"])
    payload = design.emit_source("t", [])
    assert payload["signals"]["operand"]["address"] == base + offset
    assert payload["signals"]["operand"]["auto"] is False
    # memory-mapped rows own their (name, quality): an auto can't land on it
    assert (payload["signals"]["operand"]["name"],
            payload["signals"]["operand"]["quality"]) != \
        (payload["signals"]["direct"]["name"],
         payload["signals"]["direct"]["quality"])


def test_memory_mapped_errors():
    expect_error(GATE + """
signal x = memory[999999];
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "no signal at address")
    expect_error(GATE + """
signal x = memory[nowhere + 4];
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "unknown region")
    # two declarations landing on the same address collide like any pin
    expect_error(GATE + """
region r = 100;
signal x = memory[r + 6];
signal y = memory[106];
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "already owned")


def test_mem_refs_resolve_through_design_space():
    from factorio_memory_tb import design_address_space, resolve_signal_ref
    design = build(ROOT / "modules" / "netlist_demo.fnet", None, None, None, None,
                   check_only=True)
    payload = design.emit_source("netlist_demo", design.wires())
    space = design_address_space(payload["signals"])
    # runner-reconstructed space == compiler's space, row for row
    assert len(space) == len(design.address_space)
    for addr in (1, 6, 41, 182, 2096, len(space)):
        assert space[addr]["name"] == design.address_space[addr]["name"]
        assert space[addr]["quality"] == design.address_space[addr]["quality"]
    sig = resolve_signal_ref("$mem[6]", payload["signals"], space)
    assert (sig["name"], sig["quality"]) == ("signal-1", "normal")
    # the excluded carrier's row is nowhere in the space
    carrier = payload["signals"]["read_addr"]
    assert (carrier["name"], carrier["quality"]) not in {
        (r["name"], r["quality"]) for r in space.values()
    }
    try:
        resolve_signal_ref(f"$mem[{len(space) + 1}]", payload["signals"], space)
    except RuntimeError as exc:
        assert "outside the design's address space" in str(exc)
    else:
        raise AssertionError("expected out-of-range $mem ref to raise")


MASK = """
mask = constant-combinator({
  "accumulate_mask": true,
  "player_description": "accumulate_mask"
});
"""


def test_accumulate_mask_generated_from_declarations():
    import signal_space as ss
    by_addr = {int(e["address"]): e for e in ss.full_table()}
    design, _ = compile_text(GATE + MASK + """
region acc = 300;
signal ctr = memory[acc + 0];
signal ctr2 = memory[301];
accumulate ctr, ctr2;
module main(in_r a, out_g q) {
  net green reset_bus;
  mask(out_g: reset_bus);
  gate(in_r: a, in_g: reset_bus, out_g: q);
}
""")
    payload = design.emit_source("t", design.wires())
    ent = payload["blueprint"]["entities"][0]
    assert ent["player_description"] == "accumulate_mask"
    filters = ent["control_behavior"]["sections"]["sections"][0]["filters"]
    assert [(f["name"], f["quality"], f["count"], f["index"]) for f in filters] == [
        (by_addr[300]["name"], by_addr[300]["quality"], -1, 1),
        (by_addr[301]["name"], by_addr[301]["quality"], -1, 2),
    ]
    # the signals map carries the flag machine_v8 will read at schedule time
    assert payload["signals"]["ctr"]["accumulate"] is True
    assert payload["signals"]["ctr"]["address"] == 300
    assert "accumulate" not in payload["signals"].get("ctr2", {}) or \
        payload["signals"]["ctr2"]["accumulate"] is True


def test_accumulate_errors():
    # accumulate of a control-plane carrier is refused
    expect_error(GATE + MASK + """
signal free;
accumulate free;
module main(in_r a, out_g q) {
  net green reset_bus;
  mask(out_g: reset_bus);
  gate(in_r: a, in_g: reset_bus, out_g: q);
}
""", "requires a memory-mapped signal")
    expect_error(GATE + """
accumulate ghost;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "undeclared signal")
    # declaring accumulate rows without a physical mask is a build error
    expect_error(GATE + """
signal ctr = memory[300];
accumulate ctr;
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", "no accumulate_mask entity")
    # and a mask with nothing to hold is one too
    expect_error(GATE + MASK + """
module main(in_r a, out_g q) {
  net green reset_bus;
  mask(out_g: reset_bus);
  gate(in_r: a, in_g: reset_bus, out_g: q);
}
""", "no `accumulate` declarations")
    # the mask body is generated — hand-written filters are refused
    expect_error(GATE + """
badmask = constant-combinator({
  "accumulate_mask": true,
  "sections": { "sections": [ { "index": 1, "filters": [] } ] }
});
signal ctr = memory[300];
accumulate ctr;
module main(in_r a, out_g q) {
  net green reset_bus;
  badmask(out_g: reset_bus);
  gate(in_r: a, in_g: reset_bus, out_g: q);
}
""", "only the marker")


def test_import_merges_templates():
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "lib.fnet"
        lib.write_text(GATE, encoding="utf-8")
        top = Path(td) / "top.fnet"
        top.write_text("""
import "lib.fnet";
module main(in_r a, out_g q) {
  gate(in_r: a, out_g: q);
}
""", encoding="utf-8")
        ast = parse_file(top)
        design = Design(ast, "main")
        design.validate()
        assert len(design.entities) == 1


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} netlist tests passed")
