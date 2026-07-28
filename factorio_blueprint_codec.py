#!/usr/bin/env python3
import argparse
import base64
import json
import sys
import zlib
from pathlib import Path
from typing import Any


DROP_ENTITY_FIELDS_DEFAULT = {
    "position",
    "entity_id",
    "health",
    "items",
    "request_filters",
    "tags",
}


def decode_blueprint_string(blueprint_string: str) -> dict[str, Any]:
    if not blueprint_string:
        raise RuntimeError("Empty blueprint string")
    if blueprint_string[0] != "0":
        raise RuntimeError("Unsupported blueprint version prefix")
    compressed = base64.b64decode(blueprint_string[1:])
    raw_json = zlib.decompress(compressed)
    return json.loads(raw_json.decode("utf-8"))


def encode_blueprint_string(decoded_obj: dict[str, Any]) -> str:
    raw = json.dumps(decoded_obj, separators=(",", ":")).encode("utf-8")
    return "0" + base64.b64encode(zlib.compress(raw)).decode("ascii")


def get_value_by_path(payload: dict[str, Any], dotted_path: str) -> Any:
    current: Any = payload
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise RuntimeError(f"Path not found: {dotted_path}")
    return current


def load_blueprint_string_from_json(path: Path, key_path: str) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    value = get_value_by_path(data, key_path)
    if not isinstance(value, str):
        raise RuntimeError(f"Value at {key_path} is not a string")
    return value


def normalize_blueprint(decoded_obj: dict[str, Any]) -> dict[str, Any]:
    if "blueprint" not in decoded_obj:
        raise RuntimeError("Decoded payload does not contain blueprint key")

    blueprint = decoded_obj["blueprint"]
    entities = blueprint.get("entities", [])
    wires = blueprint.get("wires", [])

    normalized_entities: list[dict[str, Any]] = []
    for entity in entities:
        compact = {
            key: value
            for key, value in entity.items()
            if key not in DROP_ENTITY_FIELDS_DEFAULT
        }

        cb = compact.get("control_behavior")
        if isinstance(cb, dict):
            sections = (
                cb.get("sections", {}).get("sections", [])
                if isinstance(cb.get("sections"), dict)
                else []
            )
            for section in sections:
                filters = section.get("filters", []) if isinstance(section, dict) else []
                for filt in filters:
                    if isinstance(filt, dict):
                        filt.pop("quality", None)
                        filt.pop("comparator", None)

        normalized_entities.append(compact)

    normalized_wires: list[dict[str, Any]] = []
    for wire in wires:
        if isinstance(wire, list) and len(wire) == 4:
            normalized_wires.append(
                {
                    "from_entity": wire[0],
                    "from_connector": wire[1],
                    "to_entity": wire[2],
                    "to_connector": wire[3],
                }
            )
        else:
            normalized_wires.append({"raw": wire})

    return {
        "format": "factorio-circuit-module-v1",
        "item": blueprint.get("item"),
        "version": blueprint.get("version"),
        "icons": blueprint.get("icons", []),
        "entities": normalized_entities,
        "wires": normalized_wires,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Decode and normalize Factorio blueprint strings"
    )

    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--blueprint-string", help="Raw encoded Factorio blueprint string")
    source.add_argument("--from-json", help="JSON file containing a blueprint string field")

    parser.add_argument(
        "--key-path",
        default="blueprint_string",
        help="Dotted key path when using --from-json (example: blueprint.string)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    decode_cmd = sub.add_parser("decode", help="Decode to raw blueprint JSON")
    decode_cmd.add_argument("--out", required=True, help="Output JSON path")

    normalize_cmd = sub.add_parser(
        "normalize",
        help="Export custom circuit-focused format with physical noise removed",
    )
    normalize_cmd.add_argument("--out", required=True, help="Output JSON path")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.blueprint_string:
        encoded = args.blueprint_string
    else:
        encoded = load_blueprint_string_from_json(Path(args.from_json), args.key_path)

    decoded = decode_blueprint_string(encoded)

    if args.command == "decode":
        write_json(Path(args.out), decoded)
        print(json.dumps({"written": str(Path(args.out)), "mode": "decode"}, indent=2))
        return 0

    if args.command == "normalize":
        normalized = normalize_blueprint(decoded)
        write_json(Path(args.out), normalized)
        print(json.dumps({"written": str(Path(args.out)), "mode": "normalize"}, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
