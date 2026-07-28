#!/usr/bin/env python3
import argparse
import base64
import json
import math
import os
import re
import sys
import time
import zlib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # main/ on the path

from rcon.source import Client


def one_line(lua: str) -> str:
    return " ".join(line.strip() for line in lua.strip().splitlines() if line.strip())


def lua_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# Persistent RCON connections, keyed by (host, port, password). A fresh
# Client per call pays TCP-connect + RCON-auth every time; once game.speed
# removed the per-frame latency (see CLAUDE.md "Timing"), that handshake
# became the dominant per-round-trip cost. Reusing one open socket for the
# whole run eliminates it. The socket is created in Client.__init__ and cannot
# be reconnected after close, so a dropped connection is replaced with a fresh
# Client (reconnect-and-retry-once below). One process = one runner = one
# connection per endpoint; run_all.py's own probe client is separate.
_RCON_POOL: dict[tuple[str, int, str], Any] = {}
_RCON_TIMEOUT = 30.0   # per-command ceiling; a healthy call at speed 30 is <1s


def _get_client(host: str, port: int, password: str) -> Client:
    key = (host, port, password)
    client = _RCON_POOL.get(key)
    if client is None:
        client = Client(host, port, passwd=password, timeout=_RCON_TIMEOUT)
        client.connect(login=True)
        _RCON_POOL[key] = client
    return client


def close_rcon_connections() -> None:
    while _RCON_POOL:
        _, client = _RCON_POOL.popitem()
        try:
            client.close()
        except OSError:
            pass


def run_sc(host: str, port: int, password: str, lua: str) -> str:
    """Run a /sc command over a reused connection, reconnecting once if the
    socket has gone bad. Only transport errors raise here — a Lua error comes
    back as a normal response string (ensure_ok inspects the text)."""
    command = "/sc " + one_line(lua)
    key = (host, port, password)
    for attempt in (1, 2):
        client = _get_client(host, port, password)
        try:
            return client.run(command) or ""
        except OSError:
            # socket died (reset/broken pipe/timeout) — drop it and, on the
            # first try, reconnect with a fresh Client and retry.
            try:
                client.close()
            except OSError:
                pass
            _RCON_POOL.pop(key, None)
            if attempt == 2:
                raise
    return ""


def ensure_ok(output: str) -> None:
    text = output.strip()
    if text.startswith("Cannot execute command") or "ERROR:" in text:
        raise RuntimeError(text)


def parse_prefixed(output: str, prefix: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith(prefix + "|"):
            rows.append(line.split("|"))
    return rows


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


LIST_ENTITIES_CHUNK = 50  # keep each /sc response comfortably under Factorio RCON's
# effective single-packet limit -- Factorio's RCON doesn't correctly implement the
# multi-packet convention the `rcon` PyPI client (frag_threshold=4096) relies on to
# detect fragmented responses, so one rcon.print() per entity in a single call
# silently hangs the connection once total output exceeds ~4096 bytes (measured
# 2026-07-22: v8's ~77-entity surface fit in one response and worked; v10's
# ~195-entity surface didn't and corrupted the RCON connection). Chunking the
# entity dump keeps every single response small regardless of module size.
# NOTE the asymmetry: this ~4096-byte cap is on the RESPONSE only. RCON
# REQUESTS accept ~277KB (measured 2026-07-27), which is why write_signal_table
# sends a whole ~229KB table in one command yet reads back a tiny 'TBL|' marker.


def lua_list_entities(surface: str, offset: int = 0, limit: int = LIST_ENTITIES_CHUNK) -> str:
    return f"""
    local s = game.surfaces[{lua_quote(surface)}]
    if not s then rcon.print('ERROR:no_surface') return end
    local all = s.find_entities()
    local n = 0
    for i = {offset} + 1, math.min(#all, {offset} + {limit}) do
      local e = all[i]
      if e.unit_number then
        rcon.print('ENT|' .. e.unit_number .. '|' .. e.name .. '|' .. e.position.x .. '|' .. e.position.y)
        n = n + 1
      end
    end
    rcon.print('ENTCOUNT|' .. #all)
    """


def list_all_entities(host: str, port: int, password: str, surface: str) -> str:
    """Accumulated output equivalent to a single lua_list_entities() call, but
    fetched in LIST_ENTITIES_CHUNK-sized pages so no individual RCON response
    risks the fragmentation hang described above."""
    offset = 0
    lines: list[str] = []
    total = None
    while True:
        out = run_sc(host, port, password, lua_list_entities(surface, offset, LIST_ENTITIES_CHUNK))
        ensure_ok(out)
        for raw in out.splitlines():
            line = raw.strip()
            if line.startswith("ENTCOUNT|"):
                total = int(line.split("|")[1])
            elif line:
                lines.append(line)
        offset += LIST_ENTITIES_CHUNK
        if total is not None and offset >= total:
            break
    return "\n".join(lines)


_MEM_REF_RE = re.compile(r"^\$mem\[(\d+)\]$")


def design_address_space(signals_map: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """address -> {address, name, type, quality} for THIS source's exclusion
    set, reconstructed from the embedded signals map (entries the compiler
    marked "excluded" are the rows it dropped before numbering). Same
    authority as table population: signal_space.full_table(exclude)."""
    from signal_space import full_table

    pairs: list[tuple[str, str]] = []
    for entry in signals_map.values():
        if entry.get("excluded"):
            pair = (entry["name"], entry.get("quality", "normal"))
            if pair not in pairs:
                pairs.append(pair)
    return {int(row["address"]): row for row in full_table(pairs)}


def resolve_signal_ref(
    key: str,
    signals_map: dict[str, Any],
    address_space: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve a testbench signal reference to {type, name, quality}.

    '$mem[N]' is the preferred form for memory rows: the address is the
    bench's meaning and the signal is derived through the design's address
    table — valid under any exclusion set. '$name' looks up a declared
    signal (carriers) in the embedded signals map. 'name~quality' parses an
    explicit quality; a plain name is a normal-quality virtual (the
    historical behavior).
    """
    key = str(key)
    mem = _MEM_REF_RE.match(key)
    if mem:
        if address_space is None:
            raise RuntimeError(f"'{key}' used but no address space available (source has no signals map)")
        row = address_space.get(int(mem.group(1)))
        if row is None:
            raise RuntimeError(
                f"'{key}' is outside the design's address space "
                f"(1..{max(address_space)})"
            )
        return {"type": row["type"], "name": row["name"], "quality": row["quality"]}
    if key.startswith("$"):
        ref = key[1:]
        entry = signals_map.get(ref)
        if entry is None:
            known = ", ".join(sorted(signals_map)) or "none"
            raise RuntimeError(f"Unknown symbolic signal '{key}' (source declares: {known})")
        return {
            "type": entry.get("type", "virtual"),
            "name": entry["name"],
            "quality": entry.get("quality", "normal"),
        }
    name, _, quality = key.partition("~")
    return {"type": "virtual", "name": name, "quality": quality or "normal"}


def signal_display_key(sig: dict[str, Any]) -> str:
    """The key format probes report: name, with ~quality when non-normal."""
    quality = sig.get("quality", "normal")
    return sig["name"] if quality == "normal" else f"{sig['name']}~{quality}"


def lua_set_constant_filters(unit_number: int, filters: list[dict[str, Any]]) -> str:
    filter_rows: list[str] = []
    for idx, filt in enumerate(filters, start=1):
        sig_name = filt["signal_name"]
        count = filt["count"]
        filter_rows.append(
            "{index="
            + str(idx)
            + ", value={type="
            + lua_quote(filt.get("type", "virtual"))
            + ", name="
            + lua_quote(sig_name)
            + ", quality="
            + lua_quote(filt.get("quality", "normal"))
            + "}, min="
            + str(count)
            + "}"
        )

    filter_blob = "{" + ",".join(filter_rows) + "}"

    return (
        """
        local unit = __UNIT__
        local target = nil
        for _, s in pairs(game.surfaces) do
          for _, e in pairs(s.find_entities_filtered{name='constant-combinator'}) do
            if e.unit_number == unit then target = e break end
          end
          if target then break end
        end
        if not target then rcon.print('ERROR:unit_not_found:' .. unit) return end
        local cb = target.get_or_create_control_behavior()
        local sec = cb.sections[1]
        if (not sec) and cb.add_section then
          sec = cb.add_section()
        end
        if not sec then
          rcon.print('ERROR:no_section:' .. unit)
          return
        end
            cb.enabled = true
        sec.filters = __FILTERS__
        rcon.print('SET|' .. unit)
        """
        .replace("__UNIT__", str(unit_number))
        .replace("__FILTERS__", filter_blob)
    )


def lua_read_output_signals(unit_number: int) -> str:
    return (
        """
        local unit = __UNIT__
        local target = nil
        for _, s in pairs(game.surfaces) do
          for _, e in pairs(s.find_entities()) do
            if e.unit_number == unit then target = e break end
          end
          if target then break end
        end
        if not target then rcon.print('ERROR:unit_not_found:' .. unit) return end

        for _, w in pairs({{'red', defines.wire_connector_id.circuit_red}, {'green', defines.wire_connector_id.circuit_green}}) do
          local net = target.get_circuit_network(w[2])
          if net and net.signals then
            for _, sig in pairs(net.signals) do
              if sig.signal and sig.signal.type == 'virtual' then
                rcon.print('OUT|' .. w[1] .. '|' .. sig.signal.name .. '|' .. tostring(sig.count or 0))
              end
            end
          end
        end
        """
        .replace("__UNIT__", str(unit_number))
    )


def _lua_filter_literal(r: dict[str, Any]) -> str:
    # LogisticFilter literal: min carries the count (address value). Fully
    # qualified type/name/quality — an unqualified filter imports as
    # quality=nil and emits nothing (see CLAUDE.md API gotchas).
    return (
        "{index=" + str(int(r["slot"]))
        + ",value={type=" + lua_quote(str(r["type"]))
        + ",name=" + lua_quote(str(r["name"]))
        + ",quality=" + lua_quote(str(r["quality"]))
        + ",comparator='='},min=" + str(int(r["count"])) + "}"
    )


def lua_write_table_bulk(unit_number: int, rows: list[dict[str, Any]]) -> str:
    """One command that populates a whole address-table combinator by bulk
    section assignment (`sec.filters = {...}`). Measured ~60x faster than
    per-slot set_slot: assigning the array once skips the per-slot API +
    recompute cost (2026-07-27). Factorio RCON accepts ~277KB requests, so
    the full ~2477-row table (~229KB) fits in a single round-trip; no chunking
    needed now that game.speed removed the per-frame latency that once made
    round-trip COUNT the thing to minimize."""
    by_section: dict[int, list[str]] = {}
    for r in rows:
        by_section.setdefault(int(r["section"]), []).append(_lua_filter_literal(r))
    max_section = max(by_section) if by_section else 0
    assigns = "".join(
        f"cb.sections[{s}].filters={{{','.join(by_section.get(s, []))}}} "
        for s in range(1, max_section + 1)
    )
    total_expr = "+".join(f"#cb.sections[{s}].filters" for s in range(1, max_section + 1)) or "0"
    template = """
        local unit = __UNIT__
        local target = nil
        for _, s in pairs(game.surfaces) do
          for _, e in pairs(s.find_entities_filtered{name='constant-combinator'}) do
            if e.unit_number == unit then target = e break end
          end
          if target then break end
        end
        if not target then rcon.print('ERROR:unit_not_found:' .. unit) return end
        local cb = target.get_or_create_control_behavior()
        while #cb.sections < __MAXSECTION__ do cb.add_section() end
        __ASSIGNS__
        cb.enabled = true
        rcon.print('TBL|' .. unit .. '|' .. (__TOTAL__))
    """
    return (
        template.replace("__UNIT__", str(unit_number))
        .replace("__MAXSECTION__", str(max_section))
        .replace("__ASSIGNS__", assigns)
        .replace("__TOTAL__", total_expr)
    )


def signal_table_rows(exclude: list[tuple[str, str]] | None = None,
                      offset: int = 0) -> list[dict[str, Any]]:
    """Full-address-space rows for one table combinator: slot value = address.

    exclude: (name, quality) pairs dropped BEFORE numbering — full_table()
    compacts the space, matching how the netlist compiler generated the
    design's addresses. No gaps: rows after an excluded one shift down.

    offset: added to every emitted address, so the same signal layout can
    serve a higher chunk of a unified space (v10: chunk 1 is the scalar space
    at offset 0, chunks 2..9 are the vector registers' lane blocks at offset
    CHUNK_WIDTH * k). The SLOT layout is unchanged — only the values shift —
    so a read decider comparing a parked address against an offset table
    matches exactly one chunk."""
    from signal_space import full_table

    rows = []
    for entry in full_table(exclude):
        addr = int(entry["address"])
        rows.append(
            {
                "section": (addr - 1) // 1000 + 1,
                "slot": (addr - 1) % 1000 + 1,
                "type": entry["type"],
                "name": entry["name"],
                "quality": entry["quality"],
                "count": addr + offset,
            }
        )
    return rows


def write_signal_table(
    host: str, port: int, password: str, unit_number: int,
    exclude: list[dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Populate an address-table combinator in ONE bulk command.

    exclude: [{name, quality}, ...] from the source's signal_table marker —
    excluded rows are dropped before numbering (the compiler generated the
    design's address space the same way), keeping the space compact.
    rows: precomputed signal_table_rows (all identical tables in a design
    share one exclusion set, so the caller computes them once)."""
    if rows is None:
        pairs = [(e["name"], e.get("quality", "normal")) for e in (exclude or [])]
        rows = signal_table_rows(pairs)
    out = run_sc(host, port, password, lua_write_table_bulk(unit_number, rows))
    ensure_ok(out)
    tbl_rows = parse_prefixed(out, "TBL")
    if not tbl_rows or len(tbl_rows[0]) < 3:
        raise RuntimeError(f"Missing TBL marker writing signal table: {out}")
    written = int(tbl_rows[0][2])
    sections = sorted({r["section"] for r in rows})
    return {"unit": unit_number, "slots_written": written,
            "slots_failed": len(rows) - written, "sections": len(sections)}


def lua_get_tick() -> str:
    return """
    rcon.print('TICK|' .. game.tick)
    """


def lua_get_tick_state() -> str:
    return """
    rcon.print('STATE|' .. game.tick .. '|' .. tostring(game.tick_paused) .. '|' .. tostring(game.ticks_to_run))
    """


def lua_set_tick_paused(paused: bool) -> str:
    flag = "true" if paused else "false"
    return f"""
    game.tick_paused = {flag}
    rcon.print('PAUSE|' .. tostring(game.tick_paused))
    """


def lua_set_game_speed(speed: float) -> str:
    # The headless server services RCON once per update frame, so a round-trip
    # costs ~16.67/game.speed ms (measured 2026-07-27). Raising game.speed
    # shortens the frame and speeds up every RCON call — including while paused
    # and single-stepping, which is all the runner ever does — with no effect
    # on tick-level determinism (we still step exactly one tick at a time).
    return f"""
    game.speed = {speed}
    rcon.print('SPEED|' .. game.speed)
    """


def set_game_speed(host: str, port: int, password: str, speed: float) -> None:
    out = run_sc(host, port, password, lua_set_game_speed(speed))
    ensure_ok(out)


def lua_step_ticks(step_count: int) -> str:
    return f"""
    game.ticks_to_run = {step_count}
    rcon.print('STEP|' .. tostring(game.ticks_to_run))
    """


def encode_blueprint_from_source(source: dict[str, Any]) -> str:
    blueprint = dict(source["blueprint"])
    blueprint["entities"] = [
        {k: v for k, v in entity.items() if k != "signal_table"}
        for entity in blueprint.get("entities", [])
    ]
    payload = {"blueprint": blueprint}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return "0" + base64.b64encode(zlib.compress(raw)).decode("ascii")


def lua_rebuild_module_instance(
        surface: str,
        encoded_blueprint: str,
        origin_x: float,
        origin_y: float,
) -> str:
        template = """
        local s = game.surfaces[__SURFACE__]
        if not s then rcon.print('ERROR:no_surface') return end

        local removed = 0
        for _, e in pairs(s.find_entities()) do
            if e.valid and e.type ~= 'character' then
                local ok = pcall(function() e.destroy() end)
                if ok then removed = removed + 1 end
            end
        end

        local inv = game.create_inventory(1)
        local st = inv[1]
        st.set_stack{name = 'blueprint', count = 1}

        local import_ok = st.import_stack([==[__BP__]==])
        local build_ok, build_err = pcall(function()
            st.build_blueprint{
                surface = s,
                force = game.forces.player,
                position = {x = __ORIGIN_X__, y = __ORIGIN_Y__},
                build_mode = defines.build_mode.force
            }
        end)

        local ghosts = #s.find_entities_filtered{name = 'entity-ghost'}
        local revived = 0
        for _, g in pairs(s.find_entities_filtered{name = 'entity-ghost'}) do
            if g.valid then
                local ok_r, ent = pcall(function() return g.silent_revive() end)
                if ok_r and ent then revived = revived + 1 end
            end
        end

        local placed = 0
        for _, e in pairs(s.find_entities()) do
            if e.valid and e.type ~= 'character' and e.name ~= 'entity-ghost' then
                placed = placed + 1
            end
        end

        inv.destroy()
        rcon.print('REBUILD|' .. removed .. '|' .. tostring(import_ok) .. '|' .. tostring(build_ok) .. '|' .. tostring(build_err) .. '|' .. ghosts .. '|' .. revived .. '|' .. placed)
        """
        return (
                template.replace("__SURFACE__", lua_quote(surface))
                .replace("__BP__", encoded_blueprint)
                .replace("__ORIGIN_X__", str(origin_x))
                .replace("__ORIGIN_Y__", str(origin_y))
        )


def lua_paste_fixture_blueprint(
        surface: str,
        encoded_blueprint: str,
        origin_x: float,
        origin_y: float,
        tag: str,
) -> str:
        template = """
        local s = game.surfaces[__SURFACE__]
        if not s then rcon.print('ERROR:no_surface') return end

        local inv = game.create_inventory(1)
        local st = inv[1]
        st.set_stack{name = 'blueprint', count = 1}

        local import_ok = st.import_stack([==[__BP__]==])
        local build_ok, build_err = pcall(function()
            st.build_blueprint{
                surface = s,
                force = game.forces.player,
                position = {x = __ORIGIN_X__, y = __ORIGIN_Y__},
                build_mode = defines.build_mode.normal
            }
        end)

        local ghosts = #s.find_entities_filtered{name = 'entity-ghost'}
        local revived = 0
        for _, g in pairs(s.find_entities_filtered{name = 'entity-ghost'}) do
            if g.valid then
                local ok_r, ent = pcall(function() return g.silent_revive() end)
                if ok_r and ent then revived = revived + 1 end
            end
        end

        inv.destroy()
        rcon.print('FIXTURE|' .. __TAG__ .. '|' .. tostring(import_ok) .. '|' .. tostring(build_ok) .. '|' .. tostring(build_err) .. '|' .. ghosts .. '|' .. revived)
        """
        return (
                template.replace("__SURFACE__", lua_quote(surface))
                .replace("__BP__", encoded_blueprint)
                .replace("__ORIGIN_X__", str(origin_x))
                .replace("__ORIGIN_Y__", str(origin_y))
                .replace("__TAG__", lua_quote(tag))
        )


def lua_trace_snapshot_multi(driver_units: dict[str, int], probes: list[dict[str, Any]]) -> str:
    driver_parts: list[str] = []
    for role, unit in driver_units.items():
        driver_parts.append("{role='" + str(role).replace("'", "") + "', unit=" + str(int(unit)) + "}")
    driver_table = "{" + ",".join(driver_parts) + "}"

    parts: list[str] = []
    for p in probes:
        name = str(p["name"]).replace("'", "")
        unit = int(p["unit"])
        wire = str(p["wire"]).replace("'", "")
        parts.append("{name='" + name + "', unit=" + str(unit) + ", wire='" + wire + "'}")
    probe_table = "{" + ",".join(parts) + "}"

    template = """
        local function find_entity(unit)
            for _, s in pairs(game.surfaces) do
                for _, e in pairs(s.find_entities()) do
                    if e.unit_number == unit then return e end
                end
            end
            return nil
        end

        local function emit_const(role, unit)
            local target = find_entity(unit)
            if not target then
                rcon.print('ERROR:unit_not_found:' .. unit)
                return
            end
            local cb = target.get_or_create_control_behavior()
            local sec = cb.sections and cb.sections[1]
            if not sec then return end
            local filters = sec.filters or {}
            for _, f in pairs(filters) do
                if f and f.value and f.value.name then
                    local count = f.min
                    if count == nil then count = f.count end
                    if count == nil then count = 0 end
                    rcon.print('STIM|' .. role .. '|' .. f.value.name .. '|' .. tostring(count))
                end
            end
        end

        local function emit_outputs(probes)
            for _, p in pairs(probes) do
                local target = find_entity(p.unit)
                if not target then
                    rcon.print('ERROR:unit_not_found:' .. p.unit)
                else
                    local wire_name = p.wire or 'red'
                    local connector = (wire_name == 'green') and defines.wire_connector_id.circuit_green or defines.wire_connector_id.circuit_red
                    local net = target.get_circuit_network(connector)
                    if net and net.signals then
                        for _, sig in pairs(net.signals) do
                            if sig.signal then
                                local key = sig.signal.name
                                local q = sig.signal.quality or 'normal'
                                if q ~= 'normal' then key = key .. '~' .. q end
                                rcon.print('OUT|' .. p.name .. '|' .. wire_name .. '|' .. key .. '|' .. tostring(sig.count or 0))
                            end
                        end
                    end
                end
            end
        end

        rcon.print('TICK|' .. game.tick)
        for _, d in pairs(__DRIVERS__) do
            emit_const(d.role, d.unit)
        end
        emit_outputs(__PROBES__)
    """
    return (
        template.replace("__DRIVERS__", driver_table)
        .replace("__PROBES__", probe_table)
    )


def find_translation(
    source_entities: list[dict[str, Any]],
    world_entities: list[dict[str, Any]],
    tolerance: float = 0.01,
) -> tuple[float, float]:
    src_by_num = {int(e["entity_number"]): e for e in source_entities}
    anchor = src_by_num[1]

    candidates = [
        e for e in world_entities if e["name"] == anchor["name"]
    ]

    for cand in candidates:
        dx = float(cand["x"]) - float(anchor["position"]["x"])
        dy = float(cand["y"]) - float(anchor["position"]["y"])

        ok = True
        for src in source_entities:
            sx = float(src["position"]["x"]) + dx
            sy = float(src["position"]["y"]) + dy
            matched = any(
                w["name"] == src["name"]
                and math.isclose(float(w["x"]), sx, abs_tol=tolerance)
                and math.isclose(float(w["y"]), sy, abs_tol=tolerance)
                for w in world_entities
            )
            if not matched:
                ok = False
                break

        if ok:
            return dx, dy

    raise RuntimeError("Could not find a live world instance matching source blueprint geometry")


def map_entity_numbers_to_units(
    source_entities: list[dict[str, Any]],
    world_entities: list[dict[str, Any]],
    dx: float,
    dy: float,
    tolerance: float = 0.01,
) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for src in source_entities:
        target_x = float(src["position"]["x"]) + dx
        target_y = float(src["position"]["y"]) + dy
        matches = [
            w for w in world_entities
            if w["name"] == src["name"]
            and math.isclose(float(w["x"]), target_x, abs_tol=tolerance)
            and math.isclose(float(w["y"]), target_y, abs_tol=tolerance)
        ]
        if not matches:
            raise RuntimeError(f"No world match for entity_number {src['entity_number']}")
        mapping[int(src["entity_number"])] = int(matches[0]["unit_number"])
    return mapping


def parse_world_entities(output: str) -> list[dict[str, Any]]:
    rows = parse_prefixed(output, "ENT")
    entities: list[dict[str, Any]] = []
    for row in rows:
        if len(row) != 5:
            continue
        _, unit, name, x, y = row
        entities.append(
            {
                "unit_number": int(unit),
                "name": name,
                "x": float(x),
                "y": float(y),
            }
        )
    return entities


def parse_rebuild_status(output: str) -> dict[str, Any]:
    rows = parse_prefixed(output, "REBUILD")
    if not rows or len(rows[0]) < 8:
        raise RuntimeError(f"Missing REBUILD marker in output: {output}")
    row = rows[0]
    return {
        "removed": int(row[1]),
        "import_ok": row[2] == "0",
        "build_ok": row[3].lower() == "true",
        "build_error": row[4],
        "ghosts": int(row[5]),
        "revived": int(row[6]),
        "placed": int(row[7]),
    }


def parse_fixture_status(output: str) -> dict[str, Any]:
    rows = parse_prefixed(output, "FIXTURE")
    if not rows or len(rows[0]) < 7:
        raise RuntimeError(f"Missing FIXTURE marker in output: {output}")
    row = rows[0]
    return {
        "tag": row[1],
        "import_ok": row[2] == "0",
        "build_ok": row[3].lower() == "true",
        "build_error": row[4],
        "ghosts": int(row[5]),
        "revived": int(row[6]),
    }


def parse_tick(output: str) -> int:
    rows = parse_prefixed(output, "TICK")
    if not rows or len(rows[0]) < 2:
        raise RuntimeError(f"Missing TICK marker in output: {output}")
    return int(rows[0][1])


def parse_tick_state(output: str) -> dict[str, Any]:
    rows = parse_prefixed(output, "STATE")
    if not rows or len(rows[0]) < 4:
        raise RuntimeError(f"Missing STATE marker in output: {output}")
    row = rows[0]
    return {
        "tick": int(row[1]),
        "tick_paused": row[2].lower() == "true",
        "ticks_to_run": int(row[3]),
    }


def get_game_tick(host: str, port: int, password: str) -> int:
    out = run_sc(host, port, password, lua_get_tick())
    ensure_ok(out)
    return parse_tick(out)


def get_tick_state(host: str, port: int, password: str) -> dict[str, Any]:
    out = run_sc(host, port, password, lua_get_tick_state())
    ensure_ok(out)
    return parse_tick_state(out)


def set_tick_paused(host: str, port: int, password: str, paused: bool) -> None:
    out = run_sc(host, port, password, lua_set_tick_paused(paused))
    ensure_ok(out)


def request_step_ticks(host: str, port: int, password: str, count: int) -> None:
    out = run_sc(host, port, password, lua_step_ticks(count))
    ensure_ok(out)


def wait_for_game_tick(
    host: str,
    port: int,
    password: str,
    target_tick: int,
    poll_seconds: float,
    timeout_seconds: float,
) -> int:
    start = time.time()
    while True:
        current = get_game_tick(host, port, password)
        if current >= target_tick:
            return current
        if (time.time() - start) > timeout_seconds:
            raise RuntimeError(
                f"Timed out waiting for game tick {target_tick}, current tick {current}"
            )
        time.sleep(poll_seconds)


def parse_trace_snapshot(output: str) -> dict[str, Any]:
    tick = parse_tick(output)
    stimuli: dict[str, dict[str, float]] = {}
    observed_by_probe: dict[str, dict[str, float]] = {}

    for row in parse_prefixed(output, "STIM"):
        if len(row) != 4:
            continue
        _, role, signal_name, count = row
        if role not in stimuli:
            stimuli[role] = {}
        stimuli[role][signal_name] = float(count)

    for row in parse_prefixed(output, "OUT"):
        if len(row) == 5:
            _, probe_name, _wire, signal_name, count = row
            value = float(count)
            if probe_name not in observed_by_probe:
                observed_by_probe[probe_name] = {}
            observed_by_probe[probe_name][signal_name] = (
                observed_by_probe[probe_name].get(signal_name, 0.0) + value
            )
        else:
            continue

    return {
        "tick": tick,
        "stimulus": stimuli,
        "observed_by_probe": observed_by_probe,
    }


def default_memory_timeline() -> list[dict[str, Any]]:
    timeline_steps: list[dict[str, Any]] = []

    timeline_steps.append(
        {
            "name": "seed_initial_input_state",
            "write": {"signal-1": 1, "signal-2": 111},
            "read_addr": 2,
            "expect": {"signal-1": 1},
        }
    )

    timeline_steps.append(
        {
            "name": "read_preloaded_addr_2",
            "read_addr": 2,
            "expect": {"signal-1": 1},
        }
    )
    timeline_steps.append(
        {
            "name": "read_preloaded_addr_3",
            "read_addr": 3,
            "expect": {"signal-2": 111},
        }
    )

    for idx in range(0, 9):
        signal = f"signal-{idx}"
        value = 1000 + idx
        addr = idx + 1
        timeline_steps.append(
            {
                "name": f"write_{signal}_to_addr_{addr}",
                "write": {signal: value},
                "read_addr": addr,
                "expect": {signal: value},
            }
        )

    return timeline_steps


def run_memory_tb(
    host: str,
    port: int,
    password: str,
    source: dict[str, Any],
    tb: dict[str, Any],
) -> dict[str, Any]:
    surface = source.get("surface", "nauvis")
    source_entities = source["blueprint"]["entities"]
    signals_map: dict[str, Any] = source.get("signals", {})
    address_space = design_address_space(signals_map) if signals_map else None
    rebuild_before_run = bool(tb.get("rebuild_before_run", True))
    rebuild_status: dict[str, Any] | None = None
    fixture_statuses: list[dict[str, Any]] = []

    if not rebuild_before_run:
        raise RuntimeError("rebuild_before_run must be true; reset is clear+paste only")

    timeline_cfg = tb.get("timeline", [])
    if not isinstance(timeline_cfg, list) or not timeline_cfg:
        raise RuntimeError("timeline is required and must be a non-empty list")

    pre_stimulus_dead_ticks = int(tb.get("pre_stimulus_dead_ticks", 0))
    if pre_stimulus_dead_ticks < 0:
        raise RuntimeError("pre_stimulus_dead_ticks must be >= 0")

    settle_ticks = int(tb.get("settle_ticks", 1))
    poll_seconds = float(tb.get("poll_seconds", 0.002))
    tick_wait_timeout_seconds = float(tb.get("tick_wait_timeout_seconds", 3.0))
    trace_ticks = int(tb.get("trace_ticks", max(1, settle_ticks)))
    restore_pause_state = bool(tb.get("restore_pause_state", True))
    # RCON latency == one server update frame (16.67/game.speed ms). The
    # whole run is paused single-stepping, so a high speed just drains the
    # ~hundreds of RCON round-trips per bench faster. Overridable per-tb; the
    # env var lets run_all.py boost the whole suite without touching each tb.
    sim_speed = float(tb.get("sim_speed", os.environ.get("TB_SIM_SPEED", 30)))

    initial_tick_state = get_tick_state(host, port, password)
    set_tick_paused(host, port, password, True)
    if sim_speed and sim_speed != 1:
        set_game_speed(host, port, password, sim_speed)

    flat_timeline: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    allowed_gap = [0]  # ticks the next appended sample may jump (set by fast-forward)

    def append_sample(sample: dict[str, Any]) -> None:
        if flat_timeline:
            delta = sample["tick"] - flat_timeline[-1]["tick"]
            if delta != 1 and delta != allowed_gap[0] + 1:
                raise RuntimeError(
                    f"Tick skip detected in single-step execution: "
                    f"{flat_timeline[-1]['tick']} -> {sample['tick']}"
                )
        allowed_gap[0] = 0
        flat_timeline.append(sample)

    def fast_forward(tick_count: int) -> None:
        """Advance tick_count ticks in one burst without sampling."""
        current_tick = get_game_tick(host, port, password)
        request_step_ticks(host, port, password, tick_count)
        wait_for_game_tick(
            host,
            port,
            password,
            current_tick + tick_count,
            poll_seconds,
            max(tick_wait_timeout_seconds, tick_count / 20.0 + 5.0),
        )
        allowed_gap[0] += tick_count

    def step_and_sample(driver_unit_map: dict[str, int], probes: list[dict[str, Any]]) -> dict[str, Any]:
        current_tick = get_game_tick(host, port, password)
        request_step_ticks(host, port, password, 1)
        wait_for_game_tick(
            host,
            port,
            password,
            current_tick + 1,
            poll_seconds,
            tick_wait_timeout_seconds,
        )
        snap_out = run_sc(host, port, password, lua_trace_snapshot_multi(driver_unit_map, probes))
        ensure_ok(snap_out)
        return parse_trace_snapshot(snap_out)

    try:
        encoded = encode_blueprint_from_source(source)
        origin_x = float(tb.get("paste_origin_x", 0.0))
        origin_y = float(tb.get("paste_origin_y", 0.0))
        rebuild_out = run_sc(
            host,
            port,
            password,
            lua_rebuild_module_instance(surface, encoded, origin_x, origin_y),
        )
        ensure_ok(rebuild_out)
        rebuild_status = parse_rebuild_status(rebuild_out)
        if not rebuild_status["build_ok"]:
            raise RuntimeError(f"Paste failed: {rebuild_status}")

        fixture_blueprints = tb.get("fixture_blueprints", [])
        for idx, fixture in enumerate(fixture_blueprints):
            bp_string = fixture.get("blueprint_string")
            if not bp_string:
                continue
            fx = float(fixture.get("origin_x", origin_x - 20))
            fy = float(fixture.get("origin_y", origin_y - 20))
            tag = str(fixture.get("name", f"fixture_{idx}"))
            fixture_out = run_sc(
                host,
                port,
                password,
                lua_paste_fixture_blueprint(surface, bp_string, fx, fy, tag),
            )
            ensure_ok(fixture_out)
            status = parse_fixture_status(fixture_out)
            fixture_statuses.append(status)
            if not status["build_ok"]:
                raise RuntimeError(f"Fixture paste failed: {status}")

        output = list_all_entities(host, port, password, surface)
        world_entities = parse_world_entities(output)

        expected_count = len(source_entities)
        if len(world_entities) < expected_count:
            raise RuntimeError(
                f"Clear-then-paste mismatch: expected at least {expected_count} entities, found {len(world_entities)}"
            )

        dx, dy = find_translation(source_entities, world_entities)
        mapping = map_entity_numbers_to_units(source_entities, world_entities, dx, dy)

        signal_table_statuses: list[dict[str, Any]] = []
        _rows_cache: dict[tuple, list[dict[str, Any]]] = {}
        for src_entity in source_entities:
            if src_entity.get("signal_table"):
                ent_num = int(src_entity["entity_number"])
                table_cfg = src_entity["signal_table"]
                exclude = table_cfg.get("exclude") if isinstance(table_cfg, dict) else None
                offset = int(table_cfg.get("offset", 0)) if isinstance(table_cfg, dict) else 0
                # Every table in a design shares one exclusion set, so tables at
                # the same chunk offset are identical → compute the ~2477-row
                # list once per (exclusions, offset) and reuse it.
                key = (tuple(sorted((e["name"], e.get("quality", "normal"))
                                    for e in (exclude or []))), offset)
                if key not in _rows_cache:
                    _rows_cache[key] = signal_table_rows(list(key[0]), offset=key[1])
                status = write_signal_table(host, port, password, mapping[ent_num],
                                            rows=_rows_cache[key])
                status["entity_number"] = ent_num
                signal_table_statuses.append(status)
                if status["slots_failed"]:
                    print(
                        f"WARNING: signal table entity {ent_num}: {status['slots_failed']} slots failed",
                        file=sys.stderr,
                    )

        entity_map_cfg = tb["entity_map"]
        driver_cfg: dict[str, Any] = dict(entity_map_cfg.get("drivers", {}))
        if "write_entity_number" in entity_map_cfg:
            driver_cfg.setdefault("write", entity_map_cfg["write_entity_number"])
        if "read_entity_number" in entity_map_cfg:
            driver_cfg.setdefault("read", entity_map_cfg["read_entity_number"])
        if not driver_cfg:
            raise RuntimeError(
                "entity_map must define drivers (map of role -> entity_number) "
                "or legacy write_entity_number/read_entity_number"
            )
        driver_units: dict[str, int] = {}
        for role, ent_num_raw in driver_cfg.items():
            role = str(role).strip()
            if not role:
                raise RuntimeError("Driver roles must be non-empty names")
            ent_num = int(ent_num_raw)
            if ent_num not in mapping:
                raise RuntimeError(f"Driver '{role}' entity_number {ent_num} not found in live mapping")
            driver_units[role] = mapping[ent_num]

        probe_specs: list[dict[str, Any]] = []
        configured_probes = entity_map_cfg.get("output_probes", [])
        if not isinstance(configured_probes, list) or not configured_probes:
            raise RuntimeError("entity_map.output_probes is required and must be a non-empty list")
        for p in configured_probes:
            name = str(p.get("name", "")).strip()
            ent_num = int(p["entity_number"])
            wire = str(p.get("wire", "")).strip().lower()
            if not name:
                raise RuntimeError("Each output probe must define a non-empty name")
            if wire not in {"red", "green"}:
                raise RuntimeError(f"Probe '{name}' must define wire as 'red' or 'green'")
            if ent_num not in mapping:
                raise RuntimeError(f"Output probe entity_number {ent_num} not found in live mapping")
            probe_specs.append({"name": name, "unit": mapping[ent_num], "wire": wire})

        unique_probe_names = {str(p["name"]) for p in probe_specs}
        if len(unique_probe_names) != len(probe_specs):
            raise RuntimeError("output_probes contains duplicate probe names")
        probe_names = [str(p["name"]) for p in probe_specs]

        expected_probe_name = str(entity_map_cfg.get("expected_probe_name", "")).strip()
        if not expected_probe_name:
            raise RuntimeError("entity_map.expected_probe_name is required")
        if expected_probe_name not in unique_probe_names:
            raise RuntimeError(
                f"entity_map.expected_probe_name '{expected_probe_name}' is not present in output_probes {probe_names}"
            )

        for dead_i in range(pre_stimulus_dead_ticks):
            snap = step_and_sample(driver_units, probe_specs)
            snap["phase"] = "pre_stimulus_dead"
            snap["step_index"] = -1
            snap["step_name"] = f"pre_dead_{dead_i + 1}"
            append_sample(snap)

        for step_idx, step_cfg in enumerate(timeline_cfg):
            step_name = str(step_cfg.get("name", f"step_{step_idx}"))
            drive_start_tick = get_game_tick(host, port, password)

            step_drives: dict[str, dict[str, Any]] = {}
            if "write" in step_cfg:
                step_drives["write"] = step_cfg["write"]
            if "read_addr" in step_cfg:
                step_drives["read"] = {"signal-R": int(step_cfg["read_addr"])}
            for role, signal_map in (step_cfg.get("drive") or {}).items():
                if role in step_drives:
                    raise RuntimeError(
                        f"Step '{step_name}' drives role '{role}' both via shorthand and drive map"
                    )
                if not isinstance(signal_map, dict):
                    raise RuntimeError(f"Step '{step_name}' drive['{role}'] must be a signal->value map")
                step_drives[role] = signal_map

            for role, signal_map in step_drives.items():
                if role not in driver_units:
                    raise RuntimeError(
                        f"Step '{step_name}' drives unknown role '{role}'; known drivers: {sorted(driver_units)}"
                    )
                filters = []
                for k, v in signal_map.items():
                    sig = resolve_signal_ref(str(k), signals_map, address_space)
                    filters.append({
                        "signal_name": sig["name"],
                        "type": sig["type"],
                        "quality": sig["quality"],
                        "count": int(v),
                    })
                set_out = run_sc(host, port, password, lua_set_constant_filters(driver_units[role], filters))
                ensure_ok(set_out)

            skip_ticks = int(step_cfg.get("skip_ticks", 0))
            if skip_ticks > 0:
                fast_forward(skip_ticks)

            step_settle = int(step_cfg.get("settle_ticks", settle_ticks))
            step_trace = int(step_cfg.get("trace_ticks", trace_ticks))
            total_steps = max(1, step_settle) + max(1, step_trace) - 1

            step_samples: list[dict[str, Any]] = []
            for step_i in range(total_steps):
                snap = step_and_sample(driver_units, probe_specs)
                in_trace_window = step_i >= (max(1, step_settle) - 1)
                snap["phase"] = "stimulus" if in_trace_window else "settle"
                snap["step_index"] = step_idx
                snap["step_name"] = step_name
                append_sample(snap)
                if in_trace_window:
                    step_samples.append(snap)

            step_expect_probe = str(step_cfg.get("expect_probe", expected_probe_name)).strip()
            if step_expect_probe not in unique_probe_names:
                raise RuntimeError(
                    f"Step '{step_name}' expect_probe '{step_expect_probe}' is not present in output_probes {probe_names}"
                )
            observed_expected_probe = (
                step_samples[-1].get("observed_by_probe", {}).get(step_expect_probe, {}) if step_samples else {}
            )
            expected = {
                (signal_display_key(resolve_signal_ref(sig, signals_map, address_space))
                 if str(sig).startswith("$") else str(sig)): exp
                for sig, exp in step_cfg.get("expect", {}).items()
            }
            mismatches: dict[str, Any] = {}
            passed = True
            for sig, exp in expected.items():
                got = observed_expected_probe.get(sig, 0.0)
                if float(got) != float(exp):
                    passed = False
                    mismatches[sig] = {"expected": exp, "observed": got}

            checks.append(
                {
                    "step_index": step_idx,
                    "step_name": step_name,
                    "drive_start_tick": drive_start_tick,
                    "skipped_ticks": skip_ticks,
                    "expected_probe": step_expect_probe,
                    "expected": expected,
                    "observed": observed_expected_probe,
                    "mismatches": mismatches,
                    "pass": passed,
                    "last_sample_tick": step_samples[-1]["tick"] if step_samples else None,
                }
            )
        save_name = str(tb.get("save_after_run", "")).strip()
        if save_name:
            save_out = run_sc(host, port, password, f"game.server_save({lua_quote(save_name)})")
            ensure_ok(save_out)
            print(f"Saved game as '{save_name}' after run", file=sys.stderr)
    finally:
        if sim_speed and sim_speed != 1:
            set_game_speed(host, port, password, 1.0)
        if restore_pause_state:
            set_tick_paused(host, port, password, bool(initial_tick_state["tick_paused"]))
        close_rcon_connections()

    return {
        "name": tb["name"],
        "execution_model": "single_step_paused",
        "pre_stimulus_dead_ticks": pre_stimulus_dead_ticks,
        "rebuild": rebuild_status,
        "fixtures": fixture_statuses,
        "signal_tables": signal_table_statuses if "signal_table_statuses" in locals() else [],
        "mapping": {
            "translation": {"dx": dx, "dy": dy} if "dx" in locals() else None,
            "entity_number_to_unit_number": mapping if "mapping" in locals() else {},
            "driver_units": driver_units if "driver_units" in locals() else {},
            "output_probes": probe_specs if "probe_specs" in locals() else [],
            "expected_probe_name": expected_probe_name if "expected_probe_name" in locals() else None,
        },
        "timeline": flat_timeline,
        "checks": checks,
        "pass": all(c["pass"] for c in checks),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Memory-oriented 1-tick testbench runner for small Factorio circuits"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=25575)
    parser.add_argument("--password", default="claude")

    sub = parser.add_subparsers(dest="command", required=True)

    scaffold = sub.add_parser("scaffold", help="Create a default memory testbench")
    scaffold.add_argument("--out", required=True)
    scaffold.set_defaults(which="scaffold")

    run = sub.add_parser("run", help="Run a memory testbench")
    run.add_argument("--source", required=True, help="Decoded source blueprint JSON")
    run.add_argument("--testbench", required=True)
    run.add_argument("--results", required=True)
    run.set_defaults(which="run")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.which == "scaffold":
        tb = {
            "name": "memory_basic",
            "description": "Basic memory tests with 1-tick settle semantics",
            "pre_stimulus_dead_ticks": 2,
            "settle_ticks": 1,
            "poll_seconds": 0.002,
            "tick_wait_timeout_seconds": 3.0,
            "trace_ticks": 1,
            "rebuild_before_run": True,
            "paste_origin_x": 0.0,
            "paste_origin_y": 0.0,
            "restore_pause_state": True,
            "entity_map": {
                "write_entity_number": 1,
                "address_table_entity_number": 4,
                "read_entity_number": 3,
                "expected_probe_name": "read_output",
                "output_probes": [
                    {"name": "read_output", "entity_number": 6, "wire": "green"},
                    {"name": "memory_state", "entity_number": 2, "wire": "green"},
                ],
            },
            "timeline": default_memory_timeline(),
        }
        write_json(Path(args.out), tb)
        print(json.dumps(tb, indent=2))
        return 0

    source = load_json(Path(args.source))
    tb = load_json(Path(args.testbench))
    result = run_memory_tb(args.host, args.port, args.password, source, tb)
    write_json(Path(args.results), result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise
