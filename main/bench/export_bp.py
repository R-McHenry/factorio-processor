#!/usr/bin/env python3
"""Export any compiled design as a blueprint string you can paste by hand.

The same thing `processor_tb.py run --export-bp` does, without needing a
server or a run — useful when you just want the machine, or when the design
has no testbench yet.

WHY THIS EXISTS AT ALL. A design's address tables are constant combinators
carrying a `signal_table` MARKER rather than filters: the runner expands them
over RCON after every paste, because ~2451 rows x 7 tables is far more than
anyone wants in a checked-in JSON. Encode that blueprint straight and paste it
in your own game and you get a machine whose address decode is EMPTY, which
fails silently rather than loudly. This inlines every table, so the output is
self-contained: paste it and it runs.

Pass `--testbench` and the tb's own fixtures (the electric-energy-interface
power seed) are folded in on the same offsets, so the result does not even
need a power network.

Usage:
  python main/bench/export_bp.py --source <design.source.json> [--out X.bp.txt]
  python main/bench/export_bp.py --source <design> --testbench <tb> --out X.bp.txt
  python main/bench/export_bp.py --source <design> --no-fixtures
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # main/ on the path

from bench.processor_tb import export_blueprint, load_json  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True,
                    help="a compiled *.source.json")
    ap.add_argument("--testbench", default=None,
                    help="fold in this tb's fixtures (the power seed)")
    ap.add_argument("--out", default=None,
                    help="default: <source>.bp.txt beside the source")
    ap.add_argument("--no-fixtures", action="store_true",
                    help="omit the fixtures even when a testbench is given")
    args = ap.parse_args()

    src = Path(args.source)
    source = load_json(src)
    tb = load_json(Path(args.testbench)) if args.testbench else None
    out = Path(args.out) if args.out else src.with_name(
        src.name.replace(".source.json", "") + ".bp.txt")

    info = export_blueprint(source, tb, out, with_fixtures=not args.no_fixtures)
    print(f"{out.name}: {info['entities']} entities "
          f"({info['fixture_entities']} from fixtures), "
          f"{info['tables_inlined']} address tables inlined "
          f"({info['table_rows']} rows), {info['chars']} chars")
    if info["chars"] > 277_000:
        print(f"  note: {info['chars']} chars is past the ~277KB RCON request "
              f"ceiling, so this one can only be pasted by hand, not driven "
              f"in over RCON", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
