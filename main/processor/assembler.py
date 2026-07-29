#!/usr/bin/env python3
"""Assembler for the switch-matrix processor.

An instruction directly encodes the routing-matrix state for one tick plus a
20-bit immediate. Bits (verified live in proc_decoder_matrix):

  bit 0..3    portA -> out1..out4
  bit 4..7    portB -> out1..out4
  bit 8..11   const (imm) -> out1..out4
  bit 12+     immediate constant (extracted by the >>12 arithmetic combinator)

Matrix outputs: out1 = write_adress, out2 = write_value, out3 = addr_rd_a,
out4 = addr_rd_b.

SECOND WORD (v10, plan/ROADMAP §3). The word above is FULL — 12 route bits
plus a 20-bit signed immediate, and the immediate cannot be narrowed because
20 signed bits is what caps the fixed-point scale (4S^2 <= 524287). So a slot
may carry a SECOND 32-bit word, fetched from a private ROM2 at the same
address by the vector-zone decoder (modules/v10_vec_decoder.fnet). It holds
the four vector control lines and nothing else:

  bits  0..5    R      mux read-select      (latched: a select persists)
  bits  6..11   S      second read-select   (latched)
  bits 12..16   W      commit register      (one tick, by construction)
  bits 17..21   erase  zero register        (one tick, same tick as W)

Word 2 is emitted only where it is non-zero, so a scalar-only program produces
no ROM2 rows at all and pays nothing.
"""

SOURCES = {"a": 0, "b": 4, "const": 8}
DESTS = {"out1": 0, "out2": 1, "out3": 2, "out4": 3, "write_addr": 0, "write_value": 1, "addr_a": 2, "addr_b": 3}

# (shift, width) per word-2 field — the decoder's masks are generated from the
# same numbers (modules/v10_vec_decoder.fnet), so they cannot drift apart
# silently: a mismatch shows up as a vector move selecting the wrong source.
WORD2_FIELDS = {"rsel": (0, 6), "ssel": (6, 6), "wsel": (12, 5), "erase": (17, 5)}


def encode_word2(rsel: int = 0, ssel: int = 0, wsel: int = 0, erase: int = 0) -> int:
    """encode_word2(rsel=27, ssel=28, wsel=1, erase=1) -> ROM2 word."""
    word = 0
    for name, value in (("rsel", rsel), ("ssel", ssel),
                        ("wsel", wsel), ("erase", erase)):
        shift, width = WORD2_FIELDS[name]
        if not (0 <= value < (1 << width)):
            raise ValueError(
                f"word-2 field {name}={value} does not fit {width} bits "
                f"(0..{(1 << width) - 1})")
        word |= value << shift
    return word


def decode_word2(word: int) -> dict:
    """Inverse of encode_word2 — used by tests and by schedule dumps."""
    return {name: (word >> shift) & ((1 << width) - 1)
            for name, (shift, width) in WORD2_FIELDS.items()}


def encode(routes: list[str], imm: int = 0) -> int:
    """encode(["const->write_addr"], imm=1500) -> instruction int.

    Each route is "<source>-><dest>", source in {a, b, const},
    dest in {out1..out4} or the alias names {write_addr, write_value, addr_a, addr_b}.
    """
    word = 0
    for route in routes:
        src, _, dst = route.replace(" ", "").partition("->")
        if src not in SOURCES:
            raise ValueError(f"Unknown source '{src}' in route '{route}'")
        if dst not in DESTS:
            raise ValueError(f"Unknown dest '{dst}' in route '{route}'")
        word |= 1 << (SOURCES[src] + DESTS[dst])
    if not (-(1 << 19) <= imm < (1 << 19)):
        raise ValueError(f"Immediate {imm} out of 20-bit range")
    word |= (imm << 12) & 0xFFFFF000
    if word >= 2 ** 31:
        word -= 2 ** 32
    return word


def assemble_program(program: list[dict]) -> list[tuple[int, int]]:
    """program: [{addr, routes, imm}] -> [(addr, instruction)] skipping NOPs."""
    out = []
    for entry in program:
        word = encode(entry.get("routes", []), entry.get("imm", 0))
        if word != 0:
            out.append((int(entry["addr"]), word))
    return out


def assemble_word2(program: list[dict]) -> list[tuple[int, int]]:
    """program: [{addr, word2}] -> [(addr, word2)] for ROM2, skipping zeros.

    A slot with no vector action contributes nothing, which is exactly what
    makes a scalar-only program cost the decoder nothing."""
    return [(int(e["addr"]), int(e["word2"])) for e in program if e.get("word2")]


class Program:
    """Slot-based program builder encoding the measured pipeline rules.

    Timing constants (all verified live — see processor.design.md):
    - ADDR_TO_VALUE: a const write ADDRESS at slot n pairs the write VALUE at n+2
      (the address path is 2 combinators deeper than the value path).
    - PORT_READ_TO_USE: a port address pulse (const->addr_a/b at slot p) is live
      for one tick; its consumer (a/b-> route) must sit at exactly p+3.
    - WRITE_TO_CELL: a write's value slot n lands in the memory cell ~n+5; an ALU
      result derived from it is port-readable by a pulse at n+2+op_latency.
    - JUMP_DELAY_SLOTS: a PC-write's value slot n takes effect at fetch n+7;
      slots n+1..n+6 still fetch and execute.
    """

    ADDR_TO_VALUE = 2
    PORT_READ_TO_USE = 3
    JUMP_DELAY_SLOTS = 6

    def __init__(self):
        self.slots: dict[int, dict] = {}
        self.word2: dict[int, int] = {}

    def at_word2(self, slot: int, word: int, why: str = "") -> "Program":
        """Attach the vector control word to a slot. ONE per slot — the ROM2
        row IS the slot address — so a slot carries at most one vector action.
        The slot is also reserved in `slots` (with no routes, so it assembles
        to no ROM word), which is what keeps jump shadows and `end()` honest."""
        if slot in self.word2:
            raise ValueError(f"Slot {slot}: word 2 already set ({why})")
        self.word2[slot] = word
        self.at(slot, [], None, why)
        return self

    def at(self, slot: int, routes: list[str], imm: int | None = None, why: str = "") -> "Program":
        """Place routes at a slot, merging with existing routes. One imm per slot."""
        if slot < 1:
            raise ValueError(f"Slot {slot} out of range ({why})")
        entry = self.slots.setdefault(slot, {"routes": [], "imm": None, "why": []})
        for route in routes:
            if route in entry["routes"]:
                raise ValueError(f"Slot {slot}: duplicate route {route} ({why})")
            entry["routes"].append(route)
        if imm is not None:
            if entry["imm"] is not None and entry["imm"] != imm:
                raise ValueError(
                    f"Slot {slot}: imm conflict {entry['imm']} vs {imm} "
                    f"({'; '.join(entry['why'])} vs {why})"
                )
            entry["imm"] = imm
        if why:
            entry["why"].append(why)
        return self

    def write_imm(self, slot: int, addr: int, value: int, why: str = "") -> int:
        """Write an immediate value to a fixed address. Returns the value slot."""
        self.at(slot, ["const->write_addr"], addr, why or f"write_imm addr {addr}")
        self.at(slot + self.ADDR_TO_VALUE, ["const->write_value"], value,
                why or f"write_imm value {value}")
        return slot + self.ADDR_TO_VALUE

    def copy(self, slot: int, src: int, dst: int, port: str = "a", why: str = "") -> int:
        """Copy mem[src] -> mem[dst] through a read port. Returns the value slot."""
        self.at(slot, [f"const->addr_{port}"], src, why or f"copy read {src}")
        self.at(slot + 1, ["const->write_addr"], dst, why or f"copy dest {dst}")
        value_slot = slot + self.PORT_READ_TO_USE
        self.at(value_slot, [f"{port}->write_value"], why=why or f"copy consume {src}->{dst}")
        return value_slot

    def jump_offset(self, slot: int, offset_src: int, base: int, dest: int,
                    port: str = "b", why: str = "") -> int:
        """Write `dest` to address base + mem[offset_src] (jump when offset is 0).
        Returns the value slot; fetch resumes at dest+1 seven slots after it."""
        self.at(slot, [f"const->addr_{port}"], offset_src, why or "jump offset read")
        addr_slot = slot + self.PORT_READ_TO_USE
        self.at(addr_slot, [f"{port}->write_addr", "const->write_addr"], base,
                why or f"jump addr {base}+offset")
        value_slot = addr_slot + self.ADDR_TO_VALUE
        self.at(value_slot, ["const->write_value"], dest, why or f"jump dest {dest}")
        return value_slot

    def warm_latch(self, slot: int, addr: int) -> None:
        """Prelude: fill the cold write-address latch pipeline (2-3 pulses)."""
        for i in range(3):
            self.at(slot + i, ["const->write_addr"], addr, "warm latch")

    @staticmethod
    def alu_ready_pulse(value_slot: int, op_latency: int = 1) -> int:
        """Earliest port-pulse slot that reads an ALU result derived from a write
        whose value slot was `value_slot` (measured: value+2+latency)."""
        return value_slot + 2 + op_latency

    def rom_entries(self) -> list[dict]:
        return [
            {"addr": slot, "routes": e["routes"], "imm": e["imm"] or 0,
             "word2": self.word2.get(slot, 0)}
            for slot, e in sorted(self.slots.items())
        ]

    def end(self) -> int:
        return max(self.slots) if self.slots else 0


if __name__ == "__main__":
    # verified against proc_decoder_matrix live results
    assert encode(["a->out1"]) == 1
    assert encode(["a->out2"]) == 2
    assert encode(["b->out3"]) == 64
    assert encode(["const->out4"], imm=12345) == 2048 + (12345 << 12)
    assert encode(["a->out1", "b->out2", "const->out4"], imm=99) == 1 + 32 + 2048 + (99 << 12)
    # word 2: the decoder's field layout, round-tripped
    assert encode_word2(rsel=27, ssel=28, wsel=1, erase=1) == 27 + (28 << 6) + (1 << 12) + (1 << 17)
    assert decode_word2(encode_word2(rsel=48, ssel=47, wsel=12, erase=31)) == {
        "rsel": 48, "ssel": 47, "wsel": 12, "erase": 31}
    assert encode_word2() == 0
    try:
        encode_word2(wsel=32)
    except ValueError:
        pass
    else:
        raise AssertionError("a 6-bit value in the 5-bit W field must raise")
    print("assembler self-test ok")
