#!/usr/bin/env python3
"""Export a compiled source.json as a blueprint string you can paste by hand.

WHY THIS IS NOT JUST `encode_blueprint_from_source`. A design's address tables
are constant combinators carrying a `signal_table` MARKER, not filters: the
test runner expands them over RCON after every paste, because ~2451 rows x 7
tables is far more than anyone wants to keep in a checked-in JSON. That is
fine for a bench and useless for a human — paste the runner's blueprint into
your own game and you get a processor whose address decode is empty, which
fails silently rather than loudly.

This tool inlines every table, so the output is self-contained: paste it, and
the machine runs. `--with-power` folds in the same electric-energy-interface
fixture the benches use, on the same offset, so it does not even need a power
network (the test surface's is global anyway).

Usage:
  python -m tools.export_bp --source modules/v10_processor_mandelbrot.source.json
  python -m tools.export_bp --source modules/v8_processor.source.json --no-power
"""
import argparse
import json
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(MAIN))
ROOT = MAIN / "processor"

from blueprint_codec import (decode_blueprint_string,  # noqa: E402
                                      encode_blueprint_string)
from bench.processor_tb import signal_table_rows  # noqa: E402

SECTION_SLOTS = 1000


def inline_signal_tables(blueprint):
    """Replace every `signal_table` marker with the rows it stands for.

    Identical tables share one exclusion set, so the rows are computed once
    per (exclude, offset) key — the same caching the runner does, for the same
    reason (2451 rows x 7 tables is not free to build)."""
    cache, filled = {}, []
    for entity in blueprint["entities"]:
        marker = entity.pop("signal_table", None)
        if marker is None:
            continue
        exclude = tuple((e["name"], e.get("quality", "normal"))
                        for e in marker.get("exclude", []))
        offset = int(marker.get("offset", 0))
        key = (exclude, offset)
        if key not in cache:
            cache[key] = signal_table_rows(list(exclude), offset=offset)
        sections = {}
        for row in cache[key]:
            sections.setdefault(row["section"], []).append({
                "index": row["slot"], "type": row["type"], "name": row["name"],
                "quality": row["quality"], "comparator": "=",
                "count": row["count"],
            })
        entity["control_behavior"] = {
            "sections": {"sections": [{"index": i, "filters": f}
                                      for i, f in sorted(sections.items())]}
        }
        filled.append((entity["entity_number"], len(cache[key])))
    return filled


def merge_fixture(blueprint, fixture_string, dx, dy):
    """Append another blueprint's entities, shifted and renumbered.

    Entity numbers are rewritten past the host's highest, and the fixture's
    own wires are rewritten with them — a fixture with no wires (the power
    seed) simply contributes none."""
    fixture = decode_blueprint_string(fixture_string)["blueprint"]
    base = max((e["entity_number"] for e in blueprint["entities"]), default=0)
    remap = {}
    for entity in fixture.get("entities", []):
        old = entity["entity_number"]
        remap[old] = base + len(remap) + 1
        moved = json.loads(json.dumps(entity))
        moved["entity_number"] = remap[old]
        moved["position"] = {"x": entity["position"]["x"] + dx,
                             "y": entity["position"]["y"] + dy}
        blueprint["entities"].append(moved)
    for a, ca, b, cb in fixture.get("wires", []):
        blueprint.setdefault("wires", []).append([remap[a], ca, remap[b], cb])
    return len(remap)


def main() -> None:
    import processor.tools.build_v10_tests as v10

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="modules/v10_processor_mandelbrot.source.json")
    ap.add_argument("--out", default=None, help="default: <source stem>.bp.txt")
    ap.add_argument("--no-power", action="store_true",
                    help="omit the electric-energy-interface fixture")
    args = ap.parse_args()

    src_path = ROOT / args.source
    source = json.loads(src_path.read_text(encoding="utf-8"))
    blueprint = source["blueprint"]
    blueprint.setdefault("item", "blueprint")
    blueprint.setdefault("label", source.get("name", src_path.stem))
    if source.get("description"):
        blueprint["description"] = source["description"][:490]

    filled = inline_signal_tables(blueprint)
    added = 0
    if not args.no_power:
        fixture = v10.TB_COMMON["fixture_blueprints"][0]
        added = merge_fixture(blueprint, fixture["blueprint_string"],
                              fixture["origin_x"], fixture["origin_y"])

    stem = args.out or src_path.name.replace(".source.json", "")
    payload = {"blueprint": blueprint}
    (ROOT / "modules" / f"{stem}.bp.json").write_text(
        json.dumps(payload, indent=1), encoding="utf-8")
    string = encode_blueprint_string(payload)
    (ROOT / "modules" / f"{stem}.bp.txt").write_text(string, encoding="utf-8")

    rows = sum(n for _e, n in filled)
    print(f"{stem}: {len(blueprint['entities'])} entities "
          f"({added} from the power fixture), {len(filled)} address tables "
          f"inlined ({rows} rows), {len(string)} chars "
          f"-> modules/{stem}.bp.txt")


if __name__ == "__main__":
    main()
