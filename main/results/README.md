# Run outputs

`run_all.py` and `processor_tb.py` write `<bench>.results.json` here: the
paste status, the entity mapping, one sample per tick, and the per-step checks.

The contents are gitignored — every suite run rewrites all of them. Serve them
with `main/bench/serve_results.py` and open the viewer at
<http://127.0.0.1:8765/results_viewer.html>.
