# Factorio processor

A programmable vector processor built out of Factorio combinators, with the
whole toolchain that produces it: an HDL, an instruction scheduler, a compiler
for a small vector language, and a testbench harness that verifies every layer
against the running game over RCON.

It computes. The headline artifact is a **mandelbrot set over 2451 lanes**,
compiled from four lines of source, running on hardware described in a netlist
language, verified lane by lane against a Python model — and drawn on a
2451-lamp display.

```
zx, zy = (zx*zx - zy*zy)//S + cx, (zx*zy)//(S//2) + cy
```

---

## The three sections

| directory | what it is |
|---|---|
| **`main/`** | The deliverable. Everything that currently works, and nothing else. Self-contained: compile the hardware, schedule a program, run the suite. |
| **`plan/`** | Everything not yet built. One roadmap plus the two live design documents. Nothing here describes something that exists. |
| **`archive/`** | Superseded generations, kept whole and runnable: the v7 ISA, the pre-fnet hand-imported v8 masters, the pre-HDL v10 generator. |

The split is deliberate and load-bearing: `main/` contains no plans, so
anything you read there is true of the code as it stands, and `plan/` contains
no completed work, so nothing there is stale by construction.

## Quick start

The venv lives here at the root and is shared by all three sections.

```
.\.venv\Scripts\python.exe main\run_all.py --start-server --regen   # 21 benches
.\.venv\Scripts\python.exe main\processor\test_isa.py               # 15 scheduler tests
.\.venv\Scripts\python.exe main\processor\test_lang.py              # 23 compiler tests
.\.venv\Scripts\python.exe main\fnet\test_hdl.py                    # HDL tests
```

`main/README.md` is the real entry point — what each layer does, how a run
works, the file formats, and the Factorio API gotchas that cost real time.

## Layers

```
 .fnet  --main/fnet/hdl.py-->  source.json         hardware
 DSL    --main/processor/lang.py--> IR8            programs
        --main/processor/isa.py--> ROM             scheduling
        main/bench/processor_tb.py                 verification
```

Each layer is verified independently in the game before the one above it is
built, which is why the stack holds together: `main/processor/testbenches/`
has a bench per hardware component, and the whole-machine benches run real
programs and check exact numbers.
