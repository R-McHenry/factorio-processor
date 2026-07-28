#!/usr/bin/env python3
"""Serve results_viewer.html and expose POST /rerun to re-execute the testbench."""
import argparse
import json
import subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent

CONFIG = {
    "source": "modules/demo_circuit.source.json",
    "testbench": "testbenches/memory_basic.tb.json",
    "results": "results/memory_basic.results.json",
}


def rerun_cmd() -> list[str]:
    return [
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "factorio_memory_tb.py",
        "run",
        "--source", CONFIG["source"],
        "--testbench", CONFIG["testbench"],
        "--results", CONFIG["results"],
    ]


class ViewerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/config":
            body = json.dumps(CONFIG).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/rerun":
            self.send_error(404)
            return
        try:
            proc = subprocess.run(
                rerun_cmd(), cwd=ROOT, capture_output=True, text=True, timeout=600
            )
            payload = {
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "")[-2000:],
            }
        except Exception as exc:
            payload = {"ok": False, "returncode": -1, "stderr": str(exc)}

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Results viewer server with rerun support")
    parser.add_argument("port", nargs="?", type=int, default=8765)
    parser.add_argument("--source", default=CONFIG["source"])
    parser.add_argument("--testbench", default=CONFIG["testbench"])
    parser.add_argument("--results", default=CONFIG["results"])
    args = parser.parse_args()

    CONFIG["source"] = args.source
    CONFIG["testbench"] = args.testbench
    CONFIG["results"] = args.results

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ViewerHandler)
    print(f"Viewer: http://127.0.0.1:{args.port}/results_viewer.html")
    print(f"Rerun target: {CONFIG['testbench']} -> {CONFIG['results']}")
    server.serve_forever()


if __name__ == "__main__":
    main()
