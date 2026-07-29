# The lamp-matrix display — pasteable blueprints

Written 2026-07-27, alongside the mandelbrot kernel (MANDELBROT.md).

## The idea

A small lamp in **packed-RGB** colour mode reads ONE named signal off its
circuit network and colours itself by that signal's value. A vector frame IS
one value per (signal, quality) row. So: give every lane of the design's
address space its own lamp, wire the whole matrix into one network, and hang
that network on the vector write bus.

The lamp at lane `i` then shows lane `i` — all 2451 of them, every tick, from
a single wire. No scanout, no addressing, no per-pixel work. **The display is
the address space, drawn.**

Lane `i` sits at column `i % 64`, row `i / 64` — exactly the mapping the
mandelbrot kernel uses to turn COORD into a grid, so the picture comes out the
right way up with no transform anywhere.

`vec_rsel` decides what the bus shows, so the display shows whatever the
program last parked the mux on. The mandelbrot program ends parked on its
colour register and then halts, so the image stays up for as long as the
machine runs.

**Every lamp needs `always_on: true`** — a top-level entity key, not a
control_behavior one. Without it a lamp only lights when it is dark, so the
display works at night and quietly does nothing in daylight. (Missed on the
first cut; the designer caught it in game.)

## The files (all in `processor/modules/`, all pasteable as-is)

| file | what it is |
|---|---|
| `v10_mandelbrot_machine_display.bp.txt` | **the whole thing in one paste** — processor with the mandelbrot ROM, address tables inlined, power interface, and the 2451-lamp matrix wired to its write bus. ~2655 entities. |
| `v10_display.bp.txt` | the lamp matrix alone, 2451 lamps on one red network. Wire it to a machine yourself. |
| `v10_display_selftest.bp.txt` | the matrix plus a constant combinator holding the finished mandelbrot frame, already wired. Draws the picture with **no processor at all** — paste this first to check wiring and colour packing. |
| `v10_processor_mandelbrot.bp.txt` | the machine alone, self-contained (see "why an export tool" below). |

Regenerate any of them:

```
.\.venv\Scripts\python.exe tools/build_v10_tests.py     # program + ROM
.\.venv\Scripts\python.exe -m tools.build_display       # all three display bps
.\.venv\Scripts\python.exe mainench\export_bp.py --source ...  # machine alone
```

## Running it

Paste `v10_mandelbrot_machine_display.bp.txt`, unpause. It computes for ~5200
ticks (~87s at normal speed, a couple of seconds at higher `game.speed`), then
halts with the picture standing on the write bus.

**The image should be GREY.** The palette is deliberately `level * 0x010101`,
so a correct packing renders greyscale and a wrong one — an alpha byte in the
low bits, a different channel order — renders tinted. That is far easier to
spot than a subtly wrong shade. If it comes out tinted, change `MB_GREY` in
`tools/build_v10_tests.py` and regenerate; nothing else needs to move.

Black = still inside the set after 20 iterations. Brightest = escaped on the
first pass. The palette is two vec-scal multiplies on the escape counter
(`esum * 12`, then `* 0x010101`) and the mux is left parked on the result.

## Wiring it by hand

If you paste `v10_display.bp.txt` separately: drag one **red** wire from any
lamp to any point on the machine's vector write bus. The write heads are the
natural tap — they are already pure consumers of the bus, so hanging lamps
(also pure consumers) off the same point cannot perturb anything. The
generator prints which head it picked and where.

Wire reach is **9 tiles**. `tools/build_display.py` inserts relay posts (empty
constant combinators, which extend a network without driving anything onto it)
wherever the run is longer than that, and `check_geometry()` fails the build if
any wire exceeds reach or any two entities share a tile. Since the compiler
started packing combinators upright the machine is narrow enough that the run
needs NO relay at all.

## Why an export tool

A compiled `source.json` cannot simply be encoded and pasted. Its address
tables are constant combinators carrying a `signal_table` **marker**, not
filters — the test runner expands them over RCON after every paste, because
~2451 rows x 7 tables is more than anyone wants checked into JSON. Paste the
runner's blueprint into a game by hand and you get a processor whose address
decode is empty, which fails *silently*. `bench/export_bp.py` inlines all
17157 rows so the output is self-contained.

## What is verified

Verified live: the combined blueprint pastes and runs, and the program's own
38 expectations (including the palette value at a probe lane) all pass. Lamps
sampled directly over RCON carried exactly the expected packed-RGB value —
including lane 2451, the far end of the address space — and every lamp queried
was on the same circuit network.

**Verified by eye 2026-07-27** (the designer pasted it in game): the whole
matrix lands, all 64 tiles of width, and the picture is the mandelbrot set.
The tell that nothing was clipped is the short stepped row along the bottom
left — that is lanes 2432..2451, the partial last row of a 2451-lane address
space laid out 64 wide. The image renders GREY with no colour cast, which
settles the open question about the packing: `color_mode: 2` really is
0xRRGGBB, so `MB_GREY = 0x010101` is right and the palette needs no change.

(An earlier RCON pass reported four lamps missing at x >= 49.5. That was a
`find_entity` lookup artifact — the entities are there.)
