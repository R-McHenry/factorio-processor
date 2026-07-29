# fnet — the HDL layer

`.fnet` netlist source → `source.json` blueprint. It compiles **any**
combinator circuit; nothing in here knows a processor exists.

```
.\.venv\Scripts\python.exe main\fnet\hdl.py build main\processor\modules\v8_alu.fnet --top alu_bench
.\.venv\Scripts\python.exe main\fnet\test_hdl.py
```

`demo/` is the layer's own proof: `netlist_demo.fnet` compiles to a circuit
entity- and connectivity-equivalent to the hand-written
`demo_circuit.source.json`, and both run the same `memory_basic` testbench
in-game.

## What it replaced, and why

Hand-written Python blueprint generators. Three incidents from one v10
session, all of which this catches mechanically:

1. **A missing wire, twice.** A gated output never fed its cell — found by
   hand, twice. "Every net has a driver and a consumer" is a build error here.
2. **A silent signal collision.** `signal-N` globally substituted for a latch
   role also renamed an unrelated ALU operand. One allocator owning every
   `(name, quality)` refuses the second claim without an explicit alias.
3. **A placeholder promoted without a design pass.** WIP letters copied into a
   Python dict became "official". Explicit `signal x;` (auto) versus
   `signal x = signal-T;` (deliberate pin) forces the decision at the
   declaration.

## The language

```
# comment to end of line

signal read_addr;                  # auto-allocated by the one authority
signal carrier = signal-T;         # or deliberately pinned
exclude carrier;                   # keep it out of the address space

net red state;                     # a net is ONE physical wire network,
net green membus;                  # and it has exactly one color

gate = decider-combinator({ ...verbatim control_behavior JSON with [param0]... });

module mem_cell(in_g membus, out_r state) {   # ports are direction+color typed
  gate<read_addr, 1, "cell0">(in_g: membus, out_r: state);
}
```

Four things are load-bearing:

**Raw JSON passthrough.** A template is an entity prototype name plus its
*verbatim* `control_behavior`, with `[paramN]` substitution slots. The
compiler never validates or interprets the contents — it substitutes, parses,
and emits untouched. Latch, mux and decode idioms are library `.fnet` files,
not compiler code.

**One net, one color.** Every net is `net red x;` or `net green x;`, and
template ports are `in_r` / `in_g` / `out_r` / `out_g`. The connected net's
color must match the port. This is explicit in v1 on purpose — outputs present
the same result on both colors, inputs sum red+green, and decider/arithmetic
configs reference wire color explicitly (`first_signal_networks` etc.). An
auto-color pass is planned (`../../plan/ROADMAP.md` §7); the grammar leaves
room for it.

**One signal authority.** `signal x;` auto-allocates from the pool; `signal x
= signal-T;` pins. Every allocation is published in the compiled payload's
`signals` map, which is how testbenches drive symbols as `"$name"` and how the
ISA layer discovers a design's own addresses instead of being told them.

**Address tables are markers.** A constant combinator carrying a
`signal_table` marker is expanded by the test harness after every paste —
~2451 rows × seven tables is more than anyone wants in checked-in JSON.
`bench/export_bp.py` (or `--export-bp` on a run) inlines them when you want a
blueprint to paste by hand.

## Layout: optimize, then repair

Placement order is **decoupled from entity numbering**. Numbering stays
declaration order — testbench `entity_map`s and the `.debug.json` sidecar
index by it — while geometry follows an optimized order, so a better order can
never change behaviour.

Four orders (declaration, Cuthill-McKee, reverse Cuthill-McKee, net
clustering) are each packed and scored on `(reach violations, total wire
length)`, lowest wins. Declaration order is always a candidate, so the result
is never worse than before.

Any edge still over the 9-tile reach gets an inert constant combinator
anchored on the same net at the edge's midpoint. That is **electrically the
same network** — an empty constant drives nothing and a wire network
propagates within a tick — so a repeater costs **zero latency**. Repeaters go
in the free lanes *between* packed rows, so inserting one never moves an
already-placed entity, which is what makes the loop converge.

`design.wires()` is the strict checker and raises on any over-reach edge;
`design.route()` is the build path that repairs.

## Not a general-purpose HDL

Primitives grow only as idioms recur, and it does not model tick-level timing
or quality arithmetic — it is structural connectivity plus a symbol table.
Timing lives in `../processor/isa.py`. Design history and the remaining phases
are in `../../plan/NETLIST_PLAN.md`.

## Known: 3 of 32 tests fail

Pre-existing, unrelated to the layer's output — both live benches pass. See
`../../plan/ROADMAP.md` §8.
