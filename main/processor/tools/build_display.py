#!/usr/bin/env python3
"""Lamp-matrix display for the v10 vector machine: one lamp per vector lane.

THE IDEA. A small lamp in `packed_rgb` colour mode reads ONE named signal off
its circuit network and colours itself by that signal's value. A vector frame
IS a set of one value per (signal, quality) row. So give every lane of the
design's address space its own lamp, wire the whole matrix into one network,
and hang that network on the vector write bus: the lamp at lane `i` shows lane
`i`, all 2451 of them, every tick, from a single wire. There is no scanout, no
addressing, no per-pixel work — the display is the address space, drawn.

Lane `i` sits at column `i % width`, row `i / width`, which is exactly the
mapping `tools/build_v10_tests.mandelbrot` uses to turn COORD into a grid, so
the picture comes out the right way up with no transform anywhere.

WHAT DRIVES IT. `vec_rsel` picks what the bus shows, so the display shows
whichever register or op output the program last parked the mux on. The
mandelbrot program ends parked on its colour register and then halts, so the
image stays up for as long as the machine is running.

COLOUR. `color_mode: 2` is packed RGB — one integer per lamp, 0xRRGGBB. The
mandelbrot palette is deliberately a GREY ramp (`level * 0x010101`): if the
packing is what we think it is the picture is greyscale, and if Factorio wants
an alpha byte in there instead the whole image comes out tinted, which is a
much easier thing to notice than a subtly wrong shade.

Outputs (both pasteable):
  modules/v10_display.bp.txt           lamps only — wire it to the machine
  modules/v10_display_selftest.bp.txt  lamps + a constant combinator holding
                                       the last mandelbrot frame, pre-wired,
                                       so it draws the picture with no
                                       processor attached at all

Usage:
  python -m tools.build_display
  python -m tools.build_display --width 64 --origin-x 22.5 --origin-y -17.5
"""
import argparse
import json
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAIN))
ROOT = MAIN / "processor"

from blueprint_codec import encode_blueprint_string  # noqa: E402
from bench.processor_tb import design_address_space  # noqa: E402

BLUEPRINT_VERSION = 562954248978435

# Small lamp: circuit connectors are red = 1, green = 2 (the same numbering the
# sample display blueprint used, which wired everything on 2).
WIRE_ID = {"red": 1, "green": 2}
CONST_OUT = {"red": 1, "green": 2}      # constant combinator OUTPUT connectors
SECTION_SLOTS = 1000                    # as used by the address tables


def lamp(entity_number, x, y, row):
    """One pixel. `row` is the address-space entry this lamp displays.

    `always_on` is NOT optional and is a top-level entity key, not a
    control_behavior one: without it a lamp only lights when it is dark, so
    the display works at night and quietly does nothing in daylight."""
    return {
        "entity_number": entity_number,
        "name": "small-lamp",
        "position": {"x": x, "y": y},
        "always_on": True,
        "control_behavior": {
            "use_colors": True,
            "color_mode": 2,
            "rgb_signal": {"type": row["type"], "name": row["name"],
                           "quality": row["quality"]},
        },
    }


def frame_combinator(entity_number, x, y, space, values, description):
    """A constant combinator holding one value per lane — a frozen frame.

    Same shape as the address tables: 1000 slots per section, fully-qualified
    type/name/quality on every filter (a filter row with no quality imports as
    quality=nil and emits NOTHING — silently, since it reads back as present)."""
    sections = {}
    for addr, value in sorted(values.items()):
        if not value:
            continue                      # 0 is absent on a wire anyway
        row = space[addr]
        index = (addr - 1) // SECTION_SLOTS + 1
        sections.setdefault(index, []).append({
            "index": (addr - 1) % SECTION_SLOTS + 1,
            "type": row["type"], "name": row["name"], "quality": row["quality"],
            "comparator": "=", "count": int(value),
        })
    return {
        "entity_number": entity_number,
        "name": "constant-combinator",
        "position": {"x": x, "y": y},
        "player_description": description,
        "control_behavior": {
            "sections": {"sections": [{"index": i, "filters": f}
                                      for i, f in sorted(sections.items())]}
        },
    }


def build_matrix(space, width, origin_x, origin_y, wire):
    """Lamps for every lane, row-major, chained into ONE circuit network.

    Chaining is per-row plus one vertical link down column 1. Consecutive
    lanes are adjacent within a row but a row wrap is `width` tiles, far past
    a lamp's 9-tile wire reach — column 1 exists in every row (row 0 starts at
    lane 1, i.e. column 1), so it is the one column that can carry the seam."""
    lanes = sorted(space)
    entities, at = [], {}
    for n, addr in enumerate(lanes, start=1):
        col, row = addr % width, addr // width
        at[(col, row)] = n
        entities.append(lamp(n, origin_x + col, origin_y + row, space[addr]))

    wid = WIRE_ID[wire]
    wires = []
    for (col, row), n in at.items():
        right = at.get((col + 1, row))
        if right is not None:
            wires.append([n, wid, right, wid])
    rows = sorted({r for _c, r in at})
    for row in rows[:-1]:
        here, below = at.get((1, row)), at.get((1, row + 1))
        if here is not None and below is not None:
            wires.append([here, wid, below, wid])
    return entities, wires, at


def relay(entity_number, x, y):
    """A wire post: an EMPTY constant combinator.

    Wire reach is 9 tiles and the machine's write bus is further than that from
    the lamp matrix, so the run needs one. A constant combinator is the right
    entity for the job — 1x1, needs no power, and with no filters it drives
    nothing onto the net it extends. (Its output connectors are red 1 / green
    2, the numbering the netlist compiler already uses for every probe.)"""
    return {
        "entity_number": entity_number,
        "name": "constant-combinator",
        "position": {"x": x, "y": y},
        "player_description": "display_bus_relay",
        "control_behavior": {"sections": {"sections": [{"index": 1, "filters": []}]}},
    }


def bridge_to_machine(machine, at, origin_x, origin_y, wire, next_number,
                      max_span=8.0):
    """Wire the lamp matrix to the machine's vector write bus.

    The tap is a WRITE HEAD's red input rather than a select gate's output: a
    head is already a pure consumer of the bus, so hanging lamps (which are
    also pure consumers) off the same point cannot perturb anything. Pick the
    head furthest to the right whose row has a lamp in column 0, then step
    relays across the gap."""
    heads = [e for e in machine["entities"]
             if str(e.get("player_description", "")).startswith("whead_")]
    if not heads:
        raise SystemExit("no write-head entity found to tap the vector bus")
    candidates = []
    for head in heads:
        row = int(round(head["position"]["y"] - origin_y))
        if (0, row) in at:
            candidates.append((head["position"]["x"], head, row))
    if not candidates:
        raise SystemExit("no write head lines up with a column-0 lamp")
    _x, head, row = max(candidates, key=lambda c: c[0])

    tap_x, tap_y = head["position"]["x"], head["position"]["y"]
    lamp_x = origin_x
    wid = WIRE_ID[wire]
    entities, wires = [], []
    posts = []
    span = lamp_x - tap_x
    count = max(0, int(-(-span // max_span)) - 1)     # ceil, minus the last hop
    for i in range(1, count + 1):
        x = tap_x + span * i / (count + 1)
        post = relay(next_number + i - 1, round(x) + 0.5, tap_y)
        entities.append(post)
        posts.append(post)

    chain = [(head["entity_number"], 1)]               # decider red INPUT
    chain += [(p["entity_number"], CONST_OUT[wire]) for p in posts]
    chain.append((at[(0, row)], wid))
    for (a, ca), (b, cb) in zip(chain, chain[1:]):
        wires.append([a, ca, b, cb])
    return entities, wires, head, row


def blueprint(entities, wires, label, description):
    return {
        "blueprint": {
            "item": "blueprint",
            "label": label,
            "description": description,
            "icons": [{"signal": {"name": "small-lamp"}, "index": 1}],
            "entities": entities,
            "wires": wires,
            "version": BLUEPRINT_VERSION,
        }
    }


def write_pair(payload, stem):
    (ROOT / "modules" / f"{stem}.bp.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8")
    string = encode_blueprint_string(payload)
    (ROOT / "modules" / f"{stem}.bp.txt").write_text(string, encoding="utf-8")
    return string


def main() -> None:
    import processor.tools.build_v10_tests as mb

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="modules/v10_processor_mandelbrot.source.json",
                    help="design whose address space the lanes come from")
    ap.add_argument("--width", type=int, default=mb.MB_WIDTH,
                    help="lanes per row; must match the kernel's grid width")
    ap.add_argument("--origin-x", type=float, default=22.5)
    ap.add_argument("--origin-y", type=float, default=-17.5)
    ap.add_argument("--wire", choices=("red", "green"), default="red",
                    help="red matches the vector write bus")
    ap.add_argument("--machine", default="modules/v10_processor_mandelbrot.source.json",
                    help="also emit machine + display + bridge as one paste; "
                         "empty string to skip")
    ap.add_argument("--out", default="v10_mandelbrot_machine_display",
                    help="basename for the combined paste, so a variant "
                         "program (the DSL-compiled kernel, say) does not "
                         "overwrite the documented one")
    args = ap.parse_args()

    signals = json.loads((ROOT / args.source).read_text(encoding="utf-8"))["signals"]
    space = design_address_space(signals)

    entities, wires, at = build_matrix(space, args.width,
                                       args.origin_x, args.origin_y, args.wire)
    rows = 1 + max(r for _c, r in at)
    label = f"v10 vector display {args.width}x{rows}"
    description = (
        f"One small lamp per vector lane: {len(entities)} lanes of the v10 "
        f"address space laid out {args.width} wide, all on one {args.wire} "
        "network. Wire it to the machine's vector write bus (any select-gate "
        "output or write-head input) and it shows whichever source vec_rsel "
        "is parked on, as packed RGB, every tick."
    )
    write_pair(blueprint(entities, wires, label, description), "v10_display")

    # -- self-test: the same matrix with the last mandelbrot frame attached --
    colours = {addr: mb.mandelbrot_colour(mb.mandelbrot_reference(addr)[1])
               for addr in space}
    frame_x = args.origin_x - 2.0
    frame_y = args.origin_y
    frame = frame_combinator(len(entities) + 1, frame_x, frame_y, space, colours,
                             "mandelbrot_frame")
    seam = at[(1, 0)]
    st_entities = entities + [frame]
    st_wires = wires + [[frame["entity_number"], CONST_OUT[args.wire],
                         seam, WIRE_ID[args.wire]]]
    lit = sum(1 for v in colours.values() if v)
    write_pair(
        blueprint(st_entities, st_wires,
                  f"v10 mandelbrot display (frozen frame) {args.width}x{rows}",
                  "The mandelbrot display with a constant combinator holding "
                  "the finished frame already wired in, so it draws the picture "
                  "with no processor attached. Paste it to check the wiring and "
                  "the colour packing before hooking up the machine: the image "
                  "should be GREY. A colour cast means the packed-RGB byte "
                  "order is not 0xRRGGBB."),
        "v10_display_selftest")

    print(f"display: {len(entities)} lamps ({args.width} x {rows}), "
          f"{len(wires)} {args.wire} wires -> modules/v10_display.bp.txt")
    print(f"selftest: + 1 frame combinator, {lit} lit lanes "
          f"-> modules/v10_display_selftest.bp.txt")

    if args.machine:
        build_combined(args, space, entities, wires, at, rows)


def build_combined(args, space, lamps, lamp_wires, at, rows):
    """Machine + display + the bridge between them, as ONE pasteable object.

    Everything in here is positioned by this script, so the wire run is a
    solved problem rather than something to do by hand at 9-tile reach."""
    from processor.tools.export_bp import inline_signal_tables, merge_fixture
    import processor.tools.build_v10_tests as v10

    source = json.loads((ROOT / args.machine).read_text(encoding="utf-8"))
    machine = source["blueprint"]
    machine.setdefault("item", "blueprint")
    filled = inline_signal_tables(machine)
    merge_fixture(machine, v10.TB_COMMON["fixture_blueprints"][0]["blueprint_string"],
                  v10.TB_COMMON["fixture_blueprints"][0]["origin_x"],
                  v10.TB_COMMON["fixture_blueprints"][0]["origin_y"])

    base = max(e["entity_number"] for e in machine["entities"])
    shifted = json.loads(json.dumps(lamps))
    for lamp_entity in shifted:
        lamp_entity["entity_number"] += base
    shifted_at = {k: v + base for k, v in at.items()}
    all_wires = list(machine.get("wires", []))
    all_wires += [[a + base, ca, b + base, cb] for a, ca, b, cb in lamp_wires]

    posts, bridge_wires, head, row = bridge_to_machine(
        machine, shifted_at, args.origin_x, args.origin_y, args.wire,
        base + len(shifted) + 1)
    entities = machine["entities"] + shifted + posts
    all_wires += bridge_wires
    check_geometry(entities, all_wires)

    machine["entities"] = entities
    machine["wires"] = all_wires
    machine["label"] = "v10 mandelbrot machine + display"
    machine["description"] = (
        "The whole thing in one paste: the v10 vector processor with the "
        "mandelbrot program in ROM and its address tables inlined, a "
        f"{args.width}-wide lamp matrix with one lamp per vector lane, and the "
        "relay that bridges the two. Unpause and it computes for ~5000 ticks, "
        "then halts with the mux parked on its colour register, so the picture "
        "stays on the write bus. The image should be GREY; a colour cast means "
        "packed RGB is not 0xRRGGBB."
    )
    string = write_pair({"blueprint": machine}, args.out)
    print(f"combined: {len(entities)} entities ({len(filled)} address tables "
          f"inlined), tapped at {head['player_description']} "
          f"(entity {head['entity_number']}) via {len(posts)} relay(s) into "
          f"display row {row}, {len(string)} chars "
          f"-> modules/{args.out}.bp.txt")


def check_geometry(entities, wires):
    """Two ways a generated blueprint fails silently in-game: entities sharing
    a tile (the paste drops one) and a wire past its 9-tile reach (the paste
    drops the wire). Both are cheap to rule out here and expensive to notice
    later."""
    import math
    from fnet.hdl import ENTITY_INFO
    # Footprints come from the compiler's own table, so a change to how it
    # orients combinators cannot leave this check silently checking the old
    # shape (it did exactly that once, when they went from 2x1 to 1x2).
    size = {name: (int(info.width), int(info.height))
            for name, info in ENTITY_INFO.items()}
    size["electric-energy-interface"] = (2, 2)
    footprint = {}
    for entity in entities:
        w, h = size.get(entity["name"], (1, 1))
        x, y = entity["position"]["x"], entity["position"]["y"]
        for dx in range(w):
            for dy in range(h):
                cell = (math.floor(x - w / 2) + dx, math.floor(y - h / 2) + dy)
                if cell in footprint:
                    raise SystemExit(
                        f"entities {footprint[cell]} ({entity['name']}) and "
                        f"{entity['entity_number']} share tile {cell}")
                footprint[cell] = entity["entity_number"]
    pos = {e["entity_number"]: e["position"] for e in entities}
    for a, _ca, b, _cb in wires:
        pa, pb = pos[a], pos[b]
        span = max(abs(pa["x"] - pb["x"]), abs(pa["y"] - pb["y"]))
        if span > 9.0:
            raise SystemExit(f"wire {a}->{b} spans {span} tiles, past 9-tile reach")


if __name__ == "__main__":
    main()
