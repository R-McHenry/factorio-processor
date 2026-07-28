#!/usr/bin/env python3
"""Schedule verifier / linter for switch-matrix processor programs (Phase 1 of
SCHEDULER_PLAN.md).

Takes a filled assembler.Program and symbolically replays the measured pipeline
timelines (slot = tick, straight-line):

- write-address latch: a const address at slot n is live for value slots >= n+2
  (port-sourced: n+1; port+const combined, the jump pattern: n+2, additive) and
  holds until the next address's live point. Every write value pairs with
  whichever latch value is live at its slot.
- port pulses: const->addr_a/b at slot p is consumed at exactly p+3, live 1 tick.
- cells: a write's value slot v lands in the memory cell at ~v+5.
- ALU: output readable by a pulse at >= v+2+latency; reading an output while a
  *different* operand cell of the same op settled nearby returns a transient
  (the fib I=H+H bug class).
- jumps: PC-write value slot v takes effect at fetch v+7; v+1..v+6 are delay
  slots that still execute.

Also: const write values with no imm ever set (explicit imm=0 is a legal
zero-write since v7.1 — the trigger fires off the matrix out2 select, not the
data), values with no latched address, imm/route validity, writes onto
ALU-output addresses, dead latch values.

verify() returns a Report; errors mean the program violates a measured rule.
"""
from dataclasses import dataclass, field

from assembler import Program, encode, SOURCES, DESTS
from signal_space import ALU_MAP, address_of, signal_at, total_addresses

WRITE_TO_CELL = 5        # value slot v -> memory cell updated at ~v+5
CONST_ADDR_PAIR = Program.ADDR_TO_VALUE   # const address at n pairs value n+2
PORT_ADDR_PAIR = 1       # port-sourced address skips the >>12 comb: pairs n+1
PORT_READ_TO_USE = Program.PORT_READ_TO_USE
JUMP_DELAY_SLOTS = Program.JUMP_DELAY_SLOTS
# Two operand-cell updates of one ALU op landing within this many ticks of a
# boundary read = mixed-freshness transient (measured: fib read I=H+H when G
# settled 2 ticks before the sample while H was exactly at the boundary).
COHERENCE_WINDOW = 6

_CANON_DEST = {"out1": "write_addr", "out2": "write_value", "out3": "addr_a", "out4": "addr_b",
               "write_addr": "write_addr", "write_value": "write_value",
               "addr_a": "addr_a", "addr_b": "addr_b"}

# Per-output ALU timing. ALU_MAP stores the worst-case latency per op; CMP's
# O/Q flags are 1 tick (single decider), only T/S pay the extra arith (2).
ALU_OUTPUT = {}
for _op, _spec in ALU_MAP.items():
    for _out in _spec["out"]:
        _lat = _spec["latency"]
        if _op == "CMP" and _out in ("O", "Q"):
            _lat = 1
        ALU_OUTPUT[f"signal-{_out}"] = {
            "op": _op,
            "latency": _lat,
            "operands": [f"signal-{s}" for s in _spec["in"]],
        }


@dataclass
class Finding:
    severity: str   # "error" | "warning" | "info"
    code: str
    slot: int
    message: str

    def __str__(self):
        return f"[{self.severity}] slot {self.slot}: {self.message} ({self.code})"


@dataclass
class LatchEvent:
    slot: int
    pair_time: int          # first value slot this address pairs with
    addr: int | None        # resolved address, None if port-sourced/dynamic
    base: int | None        # const component of a port+const (jump) address
    expr: str
    why: str


@dataclass
class WriteRecord:
    value_slot: int
    addr: int | None
    addr_expr: str
    value: int | None
    value_expr: str
    latch_slot: int

    @property
    def cell_time(self) -> int:
        return self.value_slot + WRITE_TO_CELL


@dataclass
class Report:
    name: str = ""
    findings: list = field(default_factory=list)
    manifest: list = field(default_factory=list)   # WriteRecords in value-slot order
    jumps: list = field(default_factory=list)      # {value_slot, dest, conditional, resume_time}

    def add(self, severity, code, slot, message):
        self.findings.append(Finding(severity, code, slot, message))

    @property
    def errors(self):
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self):
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self, verbose: bool = False) -> str:
        lines = [f"verify {self.name or '<program>'}: "
                 f"{'OK' if self.ok else 'FAIL'} "
                 f"({len(self.errors)} errors, {len(self.warnings)} warnings)"]
        for f in self.findings:
            if verbose or f.severity != "info":
                lines.append(f"  {f}")
        if verbose:
            lines.append("  write manifest:")
            for w in self.manifest:
                lines.append(f"    slot {w.value_slot}: mem[{w.addr_expr}] <- {w.value_expr}"
                             f" (lands ~{w.cell_time})")
        return "\n".join(lines)


def _sig_name(addr: int) -> str:
    try:
        s = signal_at(addr)
    except IndexError:
        return f"addr {addr} (out of range)"
    q = "" if s["quality"] == "normal" else f"~{s['quality']}"
    return f"{s['name']}{q}"


def verify(program: Program, parked_ports: dict | None = None, name: str = "") -> Report:
    """parked_ports: {"a": addr} — port address held externally (array mode),
    so its consumers don't need a const->addr pulse."""
    parked_ports = parked_ports or {}
    rep = Report(name=name)
    slots = program.slots

    # -- parse routes, validate encodability -------------------------------
    parsed = {}   # slot -> list[(src, dest)]
    for slot, entry in sorted(slots.items()):
        try:
            encode(entry["routes"], entry["imm"] or 0)
        except ValueError as exc:
            rep.add("error", "encode", slot, str(exc))
            continue
        routed = []
        for route in entry["routes"]:
            src, _, dst = route.replace(" ", "").partition("->")
            routed.append((src, _CANON_DEST[dst]))
        parsed[slot] = routed

    def imm(slot):
        return slots[slot]["imm"]

    def why(slot):
        return "; ".join(slots[slot]["why"])

    # -- port pulses and consumers -----------------------------------------
    pulses = {}   # (slot, port) -> read address
    for slot, routed in parsed.items():
        for port in ("a", "b"):
            if ("const", f"addr_{port}") in routed:
                addr = imm(slot) or 0
                if not (1 <= addr <= total_addresses()):
                    rep.add("error", "port-addr-range", slot,
                            f"port {port} pulse reads address {addr}, outside 1..{total_addresses()}")
                pulses[(slot, port)] = addr

    def port_value_expr(slot, port):
        """What a src=a/b route at `slot` actually carries, or None + error."""
        pulse = pulses.get((slot - PORT_READ_TO_USE, port))
        if pulse is not None:
            return pulse, f"port{port}[{_sig_name(pulse)}]"
        if port in parked_ports:
            return parked_ports[port], f"port{port}[parked:{_sig_name(parked_ports[port])}]"
        rep.add("error", "port-no-pulse", slot,
                f"route from port {port} but no const->addr_{port} pulse at slot "
                f"{slot - PORT_READ_TO_USE} and port not parked ({why(slot)})")
        return None, f"port{port}[?]"

    for slot, routed in parsed.items():
        for src, _dest in routed:
            if src in ("a", "b"):
                port_value_expr(slot, src)
    for (slot, port), _addr in pulses.items():
        use = slot + PORT_READ_TO_USE
        if not any(src == port for src, _ in parsed.get(use, [])):
            rep.add("warning", "port-unused", slot,
                    f"port {port} pulse at slot {slot} has no consumer at slot {use} "
                    f"— the value is live that tick only ({why(slot)})")

    # -- write-address latch timeline --------------------------------------
    events = []
    for slot, routed in parsed.items():
        srcs = sorted({src for src, dest in routed if dest == "write_addr"})
        if not srcs:
            continue
        ports = [s for s in srcs if s in ("a", "b")]
        if "const" in srcs and ports:
            base = imm(slot) or 0
            _, pexpr = port_value_expr(slot, ports[0])
            events.append(LatchEvent(slot, slot + CONST_ADDR_PAIR, None, base,
                                     f"{_sig_name(base)}+{pexpr}", why(slot)))
        elif "const" in srcs:
            addr = imm(slot)
            if not addr:
                rep.add("error", "addr-zero", slot,
                        f"const->write_addr with no/zero imm — address 0 is invalid ({why(slot)})")
                addr = None
            events.append(LatchEvent(slot, slot + CONST_ADDR_PAIR, addr, None,
                                     _sig_name(addr) if addr else "0", why(slot)))
        else:
            paddr, pexpr = port_value_expr(slot, ports[0])
            events.append(LatchEvent(slot, slot + PORT_ADDR_PAIR, None, None, pexpr, why(slot)))
    events.sort(key=lambda e: e.pair_time)
    for prev, nxt in zip(events, events[1:]):
        if prev.pair_time == nxt.pair_time:
            rep.add("error", "latch-collision", nxt.slot,
                    f"addresses from slots {prev.slot} and {nxt.slot} both reach the latch "
                    f"at value-slot {nxt.pair_time} ({prev.expr} vs {nxt.expr})")

    def live_event(value_slot):
        live = None
        for e in events:
            if e.pair_time <= value_slot:
                live = e
        return live

    # -- write values -> manifest ------------------------------------------
    for slot, routed in sorted(parsed.items()):
        srcs = sorted({src for src, dest in routed if dest == "write_value"})
        if not srcs:
            continue
        value, vparts = None, []
        if "const" in srcs:
            value = imm(slot)
            if value is None:
                # explicit 0 is a legal zero-write since v7.1 (trigger fires off the
                # matrix select, not the data); a const value with no imm ever set is
                # almost certainly a forgotten immediate
                rep.add("error", "value-missing-imm", slot,
                        f"const->write_value but no imm was ever set on this slot — pass "
                        f"imm=0 explicitly for a zero-write ({why(slot)})")
            vparts.append(str(value if value is not None else 0))
        for p in ("a", "b"):
            if p in srcs:
                _, pexpr = port_value_expr(slot, p)
                vparts.append(pexpr)
                value = None   # port-sourced: unknown statically
        event = live_event(slot)
        if event is None:
            rep.add("error", "value-no-addr", slot,
                    f"write value at slot {slot} but no address has reached the latch yet "
                    f"({why(slot)})")
            continue
        rec = WriteRecord(slot, event.addr, event.expr, value, "+".join(vparts), event.slot)
        rep.manifest.append(rec)
        if event.addr is not None and signal_at(event.addr)["name"] in ALU_OUTPUT \
                and signal_at(event.addr)["quality"] == "normal":
            rep.add("warning", "write-alu-output", slot,
                    f"writing to {event.expr}, an ALU output address — port reads there "
                    f"return cell + ALU sum ({why(slot)})")

    # dead latch values (usually intentional warm-latch pulses -> info)
    for i, e in enumerate(events):
        window_end = events[i + 1].pair_time if i + 1 < len(events) else None
        used = any(w.latch_slot == e.slot for w in rep.manifest)
        if not used:
            rep.add("info", "latch-unused", e.slot,
                    f"address {e.expr} latched at slot {e.slot} never pairs a value "
                    f"(window [{e.pair_time}, {window_end}) — warm pulse?) ({e.why})")
    if rep.manifest:
        first = rep.manifest[0]
        warm = sum(1 for e in events if e.slot < first.latch_slot)
        if warm == 0:
            rep.add("warning", "cold-latch", first.value_slot,
                    f"first write value pairs the program's first address pulse — a cold "
                    f"latch pipeline can drop leading values; warm_latch() with 2-3 pulses")

    # -- cell update timeline (resolved const addresses only) ---------------
    cell_writes = {}   # addr -> sorted value slots
    for w in rep.manifest:
        if w.addr is not None:
            cell_writes.setdefault(w.addr, []).append(w.value_slot)

    # -- read timing: plain cells and ALU outputs ---------------------------
    for (pslot, port), addr in sorted(pulses.items()):
        if not (1 <= addr <= total_addresses()):
            continue
        sig = signal_at(addr)
        sample = pslot + PORT_READ_TO_USE   # consumer slot = bus sample time
        if sig["quality"] == "normal" and sig["name"] in ALU_OUTPUT:
            spec = ALU_OUTPUT[sig["name"]]
            lat = spec["latency"]
            op_writes = {o: [v for v in cell_writes.get(address_of(o), []) if v <= pslot]
                         for o in spec["operands"]}
            for operand, writes in op_writes.items():
                if not writes:
                    continue
                v_star = max(writes)
                ready = Program.alu_ready_pulse(v_star, lat)
                if pslot < ready:
                    rep.add("error", "alu-read-early", pslot,
                            f"port {port} reads {sig['name']} ({spec['op']}) but operand "
                            f"{operand} was written at value slot {v_star} — earliest safe "
                            f"pulse is {ready} (alu_ready_pulse), this one is at {pslot}")
                elif pslot == ready:
                    # boundary read is measured-safe UNLESS another operand of the
                    # same op also settled nearby -> mixed-freshness transient
                    for other, owrites in op_writes.items():
                        if other == operand:
                            continue
                        for v2 in owrites:
                            vis2 = v2 + WRITE_TO_CELL + lat
                            if abs(sample - vis2) < COHERENCE_WINDOW:
                                rep.add("error", "alu-read-transient", pslot,
                                        f"port {port} reads {sig['name']} ({spec['op']}) at the "
                                        f"ready boundary for {operand} (write slot {v_star}) while "
                                        f"{other} settles at ~{vis2}, sample at {sample} — "
                                        f"mixed-freshness transient (the fib I=H+H bug class); "
                                        f"read the cell post-update or delay the pulse")
        else:
            writes = [v for v in cell_writes.get(addr, []) if v <= pslot]
            if writes and pslot < max(writes) + 2:
                rep.add("warning", "read-stale", pslot,
                        f"port {port} reads {_sig_name(addr)} at pulse {pslot} but the write at "
                        f"value slot {max(writes)} lands at ~{max(writes) + WRITE_TO_CELL} — "
                        f"sample at {sample} returns the pre-write value")

    # -- jumps / PC writes ---------------------------------------------------
    pc_addr = address_of("signal-P")
    jump_slots = []
    for w in rep.manifest:
        conditional = w.addr is None and any(
            e.slot == w.latch_slot and e.base == pc_addr for e in events)
        if w.addr == pc_addr or conditional:
            resume = w.value_slot + JUMP_DELAY_SLOTS + 1
            rep.jumps.append({"value_slot": w.value_slot, "dest": w.value,
                              "conditional": conditional, "resume_time": resume})
            jump_slots.append(w.value_slot)
            shadow = [s for s in parsed
                      if w.value_slot < s <= w.value_slot + JUMP_DELAY_SLOTS]
            if shadow:
                rep.add("info", "jump-shadow", w.value_slot,
                        f"{'conditional ' if conditional else ''}jump to {w.value} at value slot "
                        f"{w.value_slot}: slots {shadow} sit in the {JUMP_DELAY_SLOTS} delay "
                        f"slots and still execute; fetch resumes at tick {resume}")
    for j in rep.jumps:
        for other in jump_slots:
            if j["value_slot"] < other <= j["value_slot"] + JUMP_DELAY_SLOTS:
                rep.add("warning", "jump-in-shadow", other,
                        f"PC write at slot {other} sits inside the delay slots of the jump "
                        f"at {j['value_slot']}")

    rep.manifest.sort(key=lambda w: w.value_slot)
    return rep


def verify_or_die(program: Program, parked_ports: dict | None = None, name: str = "") -> Program:
    """Generator-side gate: print the report, raise on any rule violation."""
    rep = verify(program, parked_ports, name)
    print(rep.render())
    if not rep.ok:
        raise RuntimeError(f"{name or 'program'} fails schedule verification: "
                           + "; ".join(str(e) for e in rep.errors))
    return program


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
    from build_closed_loop import fib_program, fib_cmp_program

    for builder in (fib_program, fib_cmp_program):
        print(verify(builder(), name=builder.__name__).render(verbose=True))
        print()
