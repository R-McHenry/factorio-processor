#!/usr/bin/env python3
"""Run the full testbench suite against one server boot and print a pass/fail table.

Usage:
    .\\.venv\\Scripts\\python.exe run_all.py            # server must be running
    .\\.venv\\Scripts\\python.exe run_all.py --start-server   # boot/stop it automatically
    .\\.venv\\Scripts\\python.exe run_all.py --regen    # rebuild program variants first
    .\\.venv\\Scripts\\python.exe run_all.py --only fib # substring filter on tb name

Exit code = number of failing benches.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
FACTORIO = r"C:\Program Files (x86)\Steam\steamapps\common\Factorio\bin\x64\factorio.exe"

# (source, testbench) — results path derives from the tb file stem.
# The master is the v8 processor. The v7-ISA fib programs (port pulses,
# const->addr_b) are not v8-compatible; the fib lineage lives on as
# proc_v8_fib_cmp_v8 (machine_v8-scheduled). v7 archive: modules/processor_v7.source.json.
SUITE = [
    ("modules/demo_circuit.source.json", "testbenches/memory_basic.tb.json"),
    ("modules/netlist_demo.source.json", "testbenches/netlist_demo.tb.json"),
    ("modules/v8_mem_cell.source.json", "testbenches/v8_mem_cell.tb.json"),
    ("modules/v8_write_encoder.source.json", "testbenches/v8_write_encoder.tb.json"),
    ("modules/v8_decoder_matrix.source.json", "testbenches/v8_decoder_matrix.tb.json"),
    ("modules/v8_alu.source.json", "testbenches/v8_alu.tb.json"),
    ("modules/v8_port_a.source.json", "testbenches/v8_port_a.tb.json"),
    ("modules/v8_port_b_mmap.source.json", "testbenches/v8_port_b_mmap.tb.json"),
    ("modules/v8_pc.source.json", "testbenches/v8_pc.tb.json"),
    ("modules/v10_vec_reg.source.json", "testbenches/v10_vec_reg.tb.json"),
    ("modules/v10_vec_bank.source.json", "testbenches/v10_vec_bank.tb.json"),
    ("modules/v10_op_farm.source.json", "testbenches/v10_op_farm.tb.json"),
    ("modules/v10_vec_io.source.json", "testbenches/v10_vec_io.tb.json"),
    ("modules/v10_processor_vector_roundtrip.source.json", "testbenches/v10_proc_vector_roundtrip.tb.json"),
    ("modules/v10_processor_mandelbrot.source.json", "testbenches/v10_proc_mandelbrot.tb.json"),
    # the same kernel compiled from the vlang DSL, carrying the IDENTICAL 38
    # expectations — any divergence from the hand-written one fails here
    ("modules/v10_processor_mandelbrot_dsl.source.json", "testbenches/v10_proc_mandelbrot_dsl.tb.json"),
    ("modules/v8_processor.source.json", "testbenches/v8_processor_smoke.tb.json"),
    ("modules/v8_processor_accumulate_halt.source.json", "testbenches/v8_proc_accumulate_halt.tb.json"),
    ("modules/v8_processor_fib_cmp.source.json", "testbenches/v8_proc_fib_cmp.tb.json"),
    ("modules/v8_processor_bus_summation.source.json", "testbenches/v8_proc_bus_summation.tb.json"),
    ("modules/v8_processor_computed_jump.source.json", "testbenches/v8_proc_computed_jump.tb.json"),
    ("modules/processor.source.json", "testbenches/proc_memory_raw.tb.json"),
    ("modules/processor.source.json", "testbenches/proc_write_encoder.tb.json"),
    ("modules/processor.source.json", "testbenches/proc_decoder_matrix.tb.json"),
    ("modules/processor.source.json", "testbenches/proc_cmp.tb.json"),
    ("modules/processor_full.source.json", "testbenches/proc_hello_world.tb.json"),
    ("modules/processor_array.source.json", "testbenches/proc_array_write.tb.json"),
    ("modules/processor_zerow.source.json", "testbenches/proc_zero_write.tb.json"),
    ("modules/processor_v8_relative_jump.source.json", "testbenches/proc_v8_relative_jump.tb.json"),
    ("modules/processor_v8_latched_read.source.json", "testbenches/proc_v8_latched_read.tb.json"),
    ("modules/processor_v8_accumulate.source.json", "testbenches/proc_v8_accumulate.tb.json"),
    ("modules/processor_v8_mmap_port_b.source.json", "testbenches/proc_v8_mmap_port_b.tb.json"),
    ("modules/processor_v8_fib_cmp_v8.source.json", "testbenches/proc_v8_fib_cmp_v8.tb.json"),
    ("modules/processor_v8_spin_counter.source.json", "testbenches/proc_v8_spin_counter.tb.json"),
]

PROGRAM_VARIANTS = ["hello", "array", "zerow"]

# (path, top module or None) — .fnet modules rebuilt on --regen (NETLIST_PLAN:
# source.json, benches, and ROMs derive from one compile or not at all).
# v8 component files export a named component module for later composition,
# so their bench harness module is named and needs an explicit top.
FNET_MODULES = [
    ("modules/netlist_demo.fnet", None),
    ("modules/v8_mem_cell.fnet", "mem_cell_bench"),
    ("modules/v8_write_encoder.fnet", "write_encoder_bench"),
    ("modules/v8_decoder_matrix.fnet", "decoder_matrix_bench"),
    ("modules/v8_alu.fnet", "alu_bench"),
    ("modules/v8_port_a.fnet", "port_a_bench"),
    ("modules/v8_port_b_mmap.fnet", "port_b_mmap_bench"),
    ("modules/v8_pc.fnet", "pc_bench"),
    ("modules/v8_processor.fnet", "processor"),
    ("modules/v10_vec_reg.fnet", "vec_reg_bench"),
    ("modules/v10_vec_bank.fnet", "vec_bank_bench"),
    ("modules/v10_op_farm.fnet", "op_farm_bench"),
    ("modules/v10_vec_io.fnet", "vec_io_bench"),
    ("modules/v10_processor.fnet", "v10_processor"),
]


def rcon_alive() -> bool:
    probe = (
        "from rcon.source import Client\n"
        "with Client('127.0.0.1', 25575, passwd='claude') as c: c.run('/sc rcon.print(1)')\n"
    )
    return subprocess.run([PY, "-c", probe], capture_output=True).returncode == 0


def rcon_quit() -> None:
    probe = (
        "from rcon.source import Client\n"
        "with Client('127.0.0.1', 25575, passwd='claude') as c: c.run('/quit')\n"
    )
    subprocess.run([PY, "-c", probe], capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-server", action="store_true",
                        help="boot the headless server if RCON is down; /quit it afterwards")
    parser.add_argument("--regen", action="store_true",
                        help="regenerate program variants (build_closed_loop.py) first")
    parser.add_argument("--only", default="", help="substring filter on testbench name")
    args = parser.parse_args()

    if args.regen:
        for program in PROGRAM_VARIANTS:
            subprocess.run(
                [PY, str(ROOT / "tools" / "build_closed_loop.py"), "--program", program],
                cwd=ROOT, capture_output=True, check=True,
            )
        subprocess.run([PY, str(ROOT / "tools" / "build_v8_tests.py")],
                       cwd=ROOT, capture_output=True, check=True)
        for fnet, top in FNET_MODULES:
            cmd = [PY, str(ROOT / "netlist.py"), "build", fnet]
            if top:
                cmd += ["--top", top]
            subprocess.run(cmd, cwd=ROOT, capture_output=True, check=True)
        subprocess.run([PY, str(ROOT / "tools" / "build_fnet_v8_tests.py")],
                       cwd=ROOT, capture_output=True, check=True)
        subprocess.run([PY, str(ROOT / "tools" / "build_v10_tests.py")],
                       cwd=ROOT, capture_output=True, check=True)
        print(f"regenerated {len(PROGRAM_VARIANTS)} program variants + v8 benches"
              f" + {len(FNET_MODULES)} fnet modules")

    server_proc = None
    if not rcon_alive():
        if not args.start_server:
            print("Server not reachable on RCON 25575. Start it, or pass --start-server.")
            return 1
        server_proc = subprocess.Popen(
            [FACTORIO, "--start-server", "claude.zip",
             "--rcon-port", "25575", "--rcon-password", "claude"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(40):
            if rcon_alive():
                break
            if server_proc.poll() is not None:
                print("Server exited during startup (another instance holding the lock?)")
                return 1
            time.sleep(2)
        else:
            print("Server did not come up within 80s")
            return 1
        print("server booted")

    rows = []
    suite_started = time.time()
    try:
        for source, testbench in SUITE:
            if args.only and args.only not in testbench:
                continue
            results = f"results/{Path(testbench).stem.replace('.tb', '')}.results.json"
            started = time.time()
            proc = subprocess.run(
                [PY, str(ROOT / "factorio_memory_tb.py"), "run",
                 "--source", source, "--testbench", testbench, "--results", results],
                cwd=ROOT, capture_output=True, text=True,
            )
            duration = time.time() - started
            if proc.returncode != 0:
                rows.append((testbench, "ERROR", "-", duration,
                             (proc.stderr or "").strip().splitlines()[-1:] or ["?"]))
                print(f"  {Path(testbench).name}: ERROR in {duration:.1f}s", flush=True)
                continue
            data = json.loads((ROOT / results).read_text(encoding="utf-8"))
            checks = data.get("checks", [])
            failed = [c["step_name"] for c in checks if not c["pass"]]
            result = "PASS" if data.get("pass") else "FAIL"
            rows.append((
                testbench,
                result,
                f"{len(checks) - len(failed)}/{len(checks)}",
                duration,
                failed,
            ))
            # streamed progress (flush: background pipes are block-buffered —
            # without this a suite run looks hung until it exits)
            print(f"  {Path(testbench).name}: {result} in {duration:.1f}s", flush=True)
    finally:
        if server_proc is not None:
            rcon_quit()
            try:
                server_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server_proc.terminate()

    print()
    print(f"{'testbench':44} {'result':7} {'checks':8} {'time':>7}")
    print("-" * 72)
    failures = 0
    for name, result, checks, duration, detail in rows:
        print(f"{Path(name).name:44} {result:7} {checks:8} {duration:6.1f}s")
        if result != "PASS":
            failures += 1
            for item in detail:
                print(f"    ! {item}")
    print("-" * 72)
    print(f"{len(rows) - failures}/{len(rows)} benches passing "
          f"({time.time() - suite_started:.0f}s total)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
