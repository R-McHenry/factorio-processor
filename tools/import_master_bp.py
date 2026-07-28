#!/usr/bin/env python3
"""Import a pasted blueprint string as the master processor source.

Workflow: the designer pastes a new processor BP string in chat -> save it as
modules/processor_vN.bp.txt -> run this tool. It decodes the string and writes
modules/processor.source.json with the bench-ready transforms applied:

- top-level name/description/surface fields (the runner and generators expect them)
- ROM constant cleared (program variants inject their own ROM via
  tools/build_closed_loop.py; the open-loop benches need an inert machine)
- the four addres_table constants get `"signal_table": {"table":
  "full_address_space"}` (the runner expands the space post-paste)
- write-path stimulus constants cleared
- configuration combinators (write_trigger_reset accumulate mask, mmap_addr
  register mask, pc_autoincrement) POPULATED from canonical toolchain values —
  in-game contents are sample placeholders and named groups are stripped
  everywhere (grouped sections resolve against force state on paste)

Entity numbers are found by player_description, not hardcoded, so renumbered
BPs import cleanly. The previous master is backed up alongside first.

Usage:
    .venv\\Scripts\\python.exe tools\\import_master_bp.py --bp modules\\processor_v7.bp.txt \\
        --description "Processor v7 (...)" [--backup modules\\processor_v6.source.json]
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factorio_blueprint_codec import decode_blueprint_string  # noqa: E402

ADDRESS_TABLE_DESC = "addres_table"
ROM_DESC = "ROM"
STIMULUS_DESCS = ["write_adress", "write_value", "write_trigger_set",
                  "addr_rd_a", "addr_rd_b"]


def _virtual_filters(pairs):
    return [{"index": i, "type": "virtual", "name": name, "quality": "normal",
             "comparator": "=", "count": count}
            for i, (name, count) in enumerate(pairs, start=1)]


def generated_config() -> dict:
    """Machine configuration OWNED BY THE TOOLCHAIN. Whatever a pasted BP
    carries in these combinators (sample values, named-group references) is
    replaced with the canonical values derived from machine_v8/signal_space —
    the same philosophy as the address tables' signal_table expansion."""
    from machine_v8 import ACCUMULATE_SIGNALS, MMAP_B_SIGNAL
    return {
        # accumulate mask: -1 cancels the +1 reset pulse for these signals
        "write_trigger_reset": _virtual_filters([(s, -1) for s in ACCUMULATE_SIGNALS]),
        # port B address register: the cell whose VALUE is B's read address
        "mmap_addr": _virtual_filters([(MMAP_B_SIGNAL, 1)]),
        # PC autoincrement on the summing bus
        "pc_autoincrement": _virtual_filters([("signal-P", 1)]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bp", required=True, help="file containing the blueprint string")
    parser.add_argument("--description", required=True)
    parser.add_argument("--out", default=str(ROOT / "modules" / "processor.source.json"))
    parser.add_argument("--name", default="processor")
    parser.add_argument("--backup", default="", help="copy the old master here first")
    args = parser.parse_args()

    encoded = Path(args.bp).read_text(encoding="utf-8").strip()
    decoded = decode_blueprint_string(encoded)
    bp = decoded["blueprint"]

    out_path = Path(args.out)
    if args.backup and out_path.exists():
        shutil.copyfile(out_path, args.backup)
        print(f"backed up old master -> {args.backup}")

    config = generated_config()
    tables = rom = cleared = degrouped = configured = 0
    for entity in bp["entities"]:
        # Named logistic groups resolve against the FORCE's shared registry on
        # import — the inline filters are ignored, and a testbench driver writing
        # to a grouped section would edit the shared group for the whole force
        # (measured: the reest_mask accumulate mask vanished server-wide after
        # proc_memory_raw drove trig_reset). Inline every section as local.
        sections = ((entity.get("control_behavior") or {}).get("sections") or {})
        for section in (sections.get("sections") or []):
            if section.pop("group", None) is not None:
                degrouped += 1
        desc = entity.get("player_description", "")
        if desc in config:
            entity["control_behavior"] = {
                "sections": {"sections": [{"index": 1, "filters": config[desc]}]}
            }
            configured += 1
        elif desc == ADDRESS_TABLE_DESC:
            entity.pop("control_behavior", None)
            entity["signal_table"] = {"table": "full_address_space"}
            tables += 1
        elif desc == ROM_DESC:
            entity.pop("control_behavior", None)
            rom += 1
        elif desc in STIMULUS_DESCS:
            if entity.pop("control_behavior", None) is not None:
                cleared += 1
    if tables != 4 or rom != 1:
        raise SystemExit(f"expected 4 addres_table + 1 ROM, found {tables} + {rom}")

    src = {
        "name": args.name,
        "description": args.description,
        "surface": "nauvis",
        "blueprint": bp,
    }
    out_path.write_text(json.dumps(src, indent=2), encoding="utf-8")
    print(f"written: {out_path} ({len(bp['entities'])} entities, {len(bp['wires'])} wires, "
          f"{tables} signal tables, ROM cleared, {cleared} stimulus constants cleared, "
          f"{configured} config combinators populated from canonical values, "
          f"{degrouped} named group sections stripped)")


if __name__ == "__main__":
    main()
