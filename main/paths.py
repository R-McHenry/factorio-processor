"""Where things live, in one place.

The repo is split three ways (see the root README): `main/` is the deliverable,
`plan/` holds everything not yet built, `archive/` holds superseded
generations. Inside `main/` the layering follows the import graph exactly —
`signal_space` and `blueprint_codec` depend on nothing, `fnet/` and `bench/`
depend only on those, and `processor/` sits on top. Nothing in `fnet/` or
`bench/` imports `processor/`.

Every script that is not at `main/` root puts MAIN on sys.path before
importing, so imports are absolute from here: `import signal_space`,
`from processor.isa import IR8`, `from bench.processor_tb import run_sc`.
"""
from pathlib import Path

MAIN = Path(__file__).resolve().parent
ROOT = MAIN.parent                      # the three-way split lives here

FNET_DEMO = MAIN / "fnet" / "demo"
MODULES = MAIN / "processor" / "modules"
TESTBENCHES = MAIN / "processor" / "testbenches"
TOOLS = MAIN / "processor" / "tools"
DOCS = MAIN / "processor" / "docs"
RESULTS = MAIN / "results"
