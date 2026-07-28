#!/usr/bin/env python3
"""Full processor address space: every usable signal x 5 quality levels.

Address layout (1-based, deterministic):
- qualities interleave fastest: addr 1..5 = first signal normal..legendary, 6..10 = second signal, ...
- general signals first (virtuals, then items, then fluids)
- ALU register signals + signal-P at the end of the ORIGINAL 2100-slot space
- APPENDED_VIRTUALS after that (addresses 2101+): the 76 virtual signals a live
  prototype census (2026-07-13, Factorio 2.1.10) found missing from the original
  list. Appending AFTER the ALU block keeps every pre-census address stable
  (pinned constants in testbenches/ROMs stay valid).
- excluded entirely: wildcard signals (each/everything/anything/any-quality),
  signal-R (reserved as the read-address carrier on the port decode nets),
  parameter-0..9 (blueprint parameters, not real signals), hidden/special
  debug items (bottomless-chest etc.), quality-unknown
"""

QUALITY_LEVELS = ["normal", "uncommon", "rare", "epic", "legendary"]

# Single source of truth for the ALU: op -> operand/result signal letters and
# latency in ticks from operand cell update to result on the green output bus.
# The table ordering, assembler and scheduler all derive from this.
ALU_MAP = {
    "SUB": {"in": ["J", "K"], "out": ["L"], "latency": 1},
    "ADD": {"in": ["G", "H"], "out": ["I"], "latency": 1},
    "DIV": {"in": ["D", "E"], "out": ["F"], "latency": 1},
    "MUL": {"in": ["A", "B"], "out": ["C"], "latency": 1},
    # CMP: O = 1 if M>N, Q = 1 if M>=N (together: >, =, < are distinguishable),
    # T = max(M,N), S = min(M,N). Flags are 1 tick; max/min go through an extra
    # arithmetic combinator, so 2 ticks. signal-R is reserved as the read-address
    # carrier and is never an ALU output.
    "CMP": {"in": ["M", "N"], "out": ["O", "Q", "T", "S"], "latency": 2},
}

# ALU operand/result signals live at the END of the address space, away from
# program ROM at low addresses. PC (signal-P) is memory-mapped alongside them.
ALU_SIGNALS_LAST = sorted(
    {f"signal-{s}" for op in ALU_MAP.values() for s in op["in"] + op["out"]}
) + ["signal-P"]

EXCLUDED = {
    "signal-each", "signal-everything", "signal-anything", "signal-any-quality",
    "signal-R",
    # NON-TRANSMITTING SIGNALS (measured live 2026-07-27). Factorio accepts
    # these as constant-combinator filters — the filter reads back as present,
    # exactly like the missing-quality gotcha — but they NEVER appear on a
    # wire. They are blueprint-parameter placeholders and the unknown-signal
    # sentinel, not real signals.
    #
    # Found by the v10 vector_roundtrip program: reducing the whole address
    # space came out 5250 short of N(N+1)/2, and a live diff of a populated
    # address table against its own output net showed exactly 25 rows missing
    # — these 5 names at all 5 qualities. Before this they were addressable
    # rows that silently swallowed every write and read back nothing, in v8
    # as much as v10.
    "signal-fluid-parameter", "signal-fuel-parameter", "signal-item-parameter",
    "signal-signal-parameter", "signal-unknown",
}

VIRTUAL_SIGNALS = [
    *[f"signal-{i}" for i in range(10)],
    *[f"signal-{chr(65 + i)}" for i in range(26)],
    "down-arrow", "down-left-arrow", "down-right-arrow", "left-arrow", "right-arrow", "shape-circle",
    "shape-corner", "shape-cross", "shape-curve", "shape-diagonal", "shape-diagonal-cross", "shape-horizontal",
    "shape-t", "shape-vertical", "signal-any-quality", "signal-anything", "signal-black", "signal-blue",
    "signal-check", "signal-cyan", "signal-deny", "signal-dot", "signal-each", "signal-everything",
    "signal-fluid-parameter", "signal-fuel-parameter", "signal-ghost", "signal-green", "signal-grey",
    "signal-heart", "signal-info", "signal-item-parameter", "signal-pink", "signal-red", "signal-signal-parameter",
    "signal-skull", "signal-stack-size", "signal-unknown", "signal-white", "signal-yellow", "up-arrow",
    "up-left-arrow", "up-right-arrow",
]

ITEMS = [
    "accumulator", "active-provider-chest", "advanced-circuit", "agricultural-science-pack", "agricultural-tower",
    "arithmetic-combinator", "artificial-jellynut-soil", "artificial-yumako-soil", "artillery-shell",
    "artillery-targeting-remote", "artillery-turret", "artillery-wagon", "artillery-wagon-cannon",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3", "asteroid-collector", "atomic-bomb",
    "automation-science-pack", "barrel", "battery", "battery-equipment", "battery-mk2-equipment",
    "battery-mk3-equipment", "beacon", "belt-immunity-equipment", "big-electric-pole", "big-mining-drill",
    "biochamber", "bioflux", "biolab", "biter-egg", "blueprint", "blueprint-book", "boiler", "buffer-chest",
    "bulk-inserter", "burner-generator", "burner-inserter", "burner-mining-drill", "calcite", "cannon-shell",
    "captive-biter-spawner", "capture-robot-rocket", "car", "carbon", "carbon-fiber", "carbonic-asteroid-chunk",
    "cargo-bay", "cargo-landing-pad", "cargo-wagon", "centrifuge", "chemical-plant", "chemical-science-pack",
    "cliff-explosives", "cluster-grenade", "coal", "coin", "combat-shotgun", "concrete", "constant-combinator",
    "construction-robot", "copper-bacteria", "copper-cable", "copper-ore", "copper-plate", "copper-wire",
    "copy-paste-tool", "crude-oil-barrel", "crusher", "cryogenic-plant", "cryogenic-science-pack", "cut-paste-tool",
    "decider-combinator", "deconstruction-planner", "defender-capsule", "depleted-uranium-fuel-cell",
    "destroyer-capsule", "discharge-defense-equipment", "discharge-defense-remote", "display-panel",
    "distractor-capsule", "efficiency-module", "efficiency-module-2", "efficiency-module-3",
    "electric-energy-interface", "electric-engine-unit", "electric-furnace", "electric-mining-drill",
    "electromagnetic-plant", "electromagnetic-science-pack", "electronic-circuit", "empty-module-slot",
    "energy-shield-equipment", "energy-shield-mk2-equipment", "engine-unit", "exoskeleton-equipment",
    "explosive-cannon-shell", "explosive-rocket", "explosive-uranium-cannon-shell", "explosives", "express-loader",
    "express-splitter", "express-transport-belt", "express-underground-belt", "fast-inserter", "fast-loader",
    "fast-splitter", "fast-transport-belt", "fast-underground-belt", "firearm-magazine",
    "fission-reactor-equipment", "flamethrower", "flamethrower-ammo", "flamethrower-turret", "fluid-wagon",
    "fluoroketone-cold-barrel", "fluoroketone-hot-barrel", "flying-robot-frame", "foundation", "foundry",
    "fusion-generator", "fusion-power-cell", "fusion-reactor", "fusion-reactor-equipment", "gate", "green-wire",
    "grenade", "gun-turret", "hazard-concrete", "heat-exchanger", "heat-interface", "heat-pipe", "heating-tower",
    "heavy-armor", "heavy-oil-barrel", "holmium-ore", "holmium-plate", "ice", "ice-platform", "infinity-chest",
    "infinity-pipe", "inserter", "iron-bacteria", "iron-chest", "iron-gear-wheel", "iron-ore", "iron-plate",
    "iron-stick", "item-unknown", "jelly", "jellynut", "jellynut-seed", "lab", "land-mine", "landfill",
    "lane-splitter", "laser-turret", "light-armor", "light-oil-barrel", "lightning-collector", "lightning-rod",
    "linked-belt", "linked-chest", "lithium", "lithium-plate", "loader", "locomotive", "logistic-robot",
    "logistic-science-pack", "long-handed-inserter", "low-density-structure", "lubricant-barrel", "mech-armor",
    "medium-electric-pole", "metallic-asteroid-chunk", "metallurgic-science-pack", "military-science-pack",
    "modular-armor", "night-vision-equipment", "nuclear-fuel", "nuclear-reactor", "nutrients", "offshore-pump",
    "oil-refinery", "overgrowth-jellynut-soil", "overgrowth-yumako-soil", "oxide-asteroid-chunk",
    "passive-provider-chest", "pentapod-egg", "personal-laser-defense-equipment", "personal-roboport-equipment",
    "personal-roboport-mk2-equipment", "petroleum-gas-barrel", "piercing-rounds-magazine", "piercing-shotgun-shell",
    "pipe", "pipe-to-ground", "pistol", "plastic-bar", "poison-capsule", "power-armor", "power-armor-mk2",
    "power-switch", "processing-unit", "production-science-pack", "productivity-module", "productivity-module-2",
    "productivity-module-3", "programmable-speaker", "promethium-asteroid-chunk", "promethium-science-pack",
    "pump", "pumpjack", "quality-module", "quality-module-2", "quality-module-3", "quantum-processor", "radar",
    "rail", "rail-chain-signal", "rail-ramp", "rail-signal", "rail-support", "railgun", "railgun-ammo",
    "railgun-turret", "raw-fish", "recycler", "red-wire", "refined-concrete", "refined-hazard-concrete",
    "repair-pack", "requester-chest", "roboport", "rocket", "rocket-fuel", "rocket-launcher", "rocket-part",
    "rocket-silo", "rocket-turret", "science", "scrap", "selection-tool", "selector-combinator", "shotgun",
    "shotgun-shell", "simple-entity-with-force", "simple-entity-with-owner", "slowdown-capsule",
    "small-electric-pole", "small-lamp", "solar-panel", "solar-panel-equipment", "solid-fuel",
    "space-platform-foundation", "space-platform-hub", "space-platform-starter-pack", "space-science-pack",
    "speed-module", "speed-module-2", "speed-module-3", "spidertron", "spidertron-remote",
    "spidertron-rocket-launcher-1", "spidertron-rocket-launcher-2", "spidertron-rocket-launcher-3",
    "spidertron-rocket-launcher-4", "splitter", "spoilage", "stack-inserter", "steam-engine", "steam-turbine",
    "steel-chest", "steel-furnace", "steel-plate", "stone", "stone-brick", "stone-furnace", "stone-wall",
    "storage-chest", "storage-tank", "submachine-gun", "substation", "sulfur", "sulfuric-acid-barrel",
    "supercapacitor", "superconductor", "tank", "tank-cannon", "tank-flamethrower", "tank-machine-gun",
    "tesla-ammo", "tesla-turret", "teslagun", "thruster", "toolbelt-equipment", "train-stop", "transport-belt",
    "tree-seed", "tungsten-carbide", "tungsten-ore", "tungsten-plate", "turbo-loader", "turbo-splitter",
    "turbo-transport-belt", "turbo-underground-belt", "underground-belt", "upgrade-planner", "uranium-235",
    "uranium-238", "uranium-cannon-shell", "uranium-fuel-cell", "uranium-ore", "uranium-rounds-magazine",
    "utility-science-pack", "vehicle-machine-gun", "water-barrel", "wood", "wooden-chest", "yumako", "yumako-mash",
    "yumako-seed",
]

# Live-census additions (see module docstring). MUST stay after the ALU block
# in address order — append new discoveries to the END of this list only.
APPENDED_VIRTUALS = [
    "shape-corner-2", "shape-corner-3", "shape-corner-4", "shape-curve-2", "shape-curve-3",
    "shape-curve-4", "shape-diagonal-2", "shape-t-2", "shape-t-3", "shape-t-4",
    "signal-alarm", "signal-alert", "signal-ampersand", "signal-anticlockwise-circle-arrow",
    "signal-apostrophe", "signal-battery-full", "signal-battery-low", "signal-battery-mid-level",
    "signal-circumflex-accent", "signal-clock", "signal-clockwise-circle-arrow", "signal-colon",
    "signal-comma", "signal-damage", "signal-division", "signal-equal", "signal-exclamation-mark",
    "signal-explosion", "signal-fire", "signal-fuel", "signal-greater-than",
    "signal-greater-than-or-equal-to", "signal-hourglass", "signal-input",
    "signal-left-parenthesis", "signal-left-right-arrow", "signal-left-square-bracket",
    "signal-less-than", "signal-less-than-or-equal-to", "signal-letter-dot", "signal-lightning",
    "signal-liquid", "signal-lock", "signal-map-marker", "signal-mining", "signal-minus",
    "signal-moon", "signal-multiplication", "signal-no-entry", "signal-not-equal",
    "signal-number-sign", "signal-output", "signal-percent", "signal-plus",
    "signal-question-mark", "signal-quotation-mark", "signal-radioactivity", "signal-recycle",
    "signal-right-parenthesis", "signal-right-square-bracket", "signal-rightwards-leftwards-arrow",
    "signal-science-pack", "signal-shuffle", "signal-slash", "signal-snowflake", "signal-speed",
    "signal-star", "signal-sun", "signal-thermometer-blue", "signal-thermometer-red",
    "signal-trash-bin", "signal-unlock", "signal-up-down-arrow", "signal-upwards-downwards-arrow",
    "signal-weapon", "signal-white-flag",
]

FLUIDS = [
    "ammonia", "ammoniacal-solution", "crude-oil", "electrolyte", "fluid-unknown", "fluorine",
    "fluoroketone-cold", "fluoroketone-hot", "fusion-plasma", "heavy-oil", "holmium-solution", "lava",
    "light-oil", "lithium-brine", "lubricant", "molten-copper", "molten-iron", "petroleum-gas", "steam",
    "sulfuric-acid", "thruster-fuel", "thruster-oxidizer", "water",
]


def _signal_type(name: str) -> str:
    if name in FLUIDS:
        return "fluid"
    if name in ITEMS:
        return "item"
    return "virtual"


def ordered_signals() -> list[str]:
    """Signal names in address order (before quality interleave)."""
    general = [
        s for s in (VIRTUAL_SIGNALS + ITEMS + FLUIDS)
        if s not in EXCLUDED and s not in ALU_SIGNALS_LAST
    ]
    return general + ALU_SIGNALS_LAST + APPENDED_VIRTUALS


def full_table(exclude=None) -> list[dict]:
    """[{address, name, type, quality}] for the address space, address = 1..n.

    exclude: iterable of (name, quality) pairs dropped BEFORE numbering —
    exclusion compacts the space (addresses after an excluded row shift down
    by one), it never leaves gaps. Per-quality: excluding (x, "uncommon")
    keeps x's other four rows addressable. This is the single numbering
    authority: the netlist compiler and the testbench runner both build the
    space through here with the same exclusions.
    """
    excluded = {(name, quality) for name, quality in (exclude or [])}
    table = []
    addr = 1
    for name in ordered_signals():
        sig_type = _signal_type(name)
        for quality in QUALITY_LEVELS:
            if (name, quality) in excluded:
                continue
            table.append({"address": addr, "name": name, "type": sig_type, "quality": quality})
            addr += 1
    return table


def address_of(name: str, quality: str = "normal") -> int:
    signals = ordered_signals()
    if name not in signals:
        raise KeyError(f"{name} is not in the address space")
    return signals.index(name) * len(QUALITY_LEVELS) + QUALITY_LEVELS.index(quality) + 1


def signal_at(address: int) -> dict:
    signals = ordered_signals()
    idx = address - 1
    name = signals[idx // len(QUALITY_LEVELS)]
    return {
        "address": address,
        "name": name,
        "type": _signal_type(name),
        "quality": QUALITY_LEVELS[idx % len(QUALITY_LEVELS)],
    }


def total_addresses() -> int:
    return len(ordered_signals()) * len(QUALITY_LEVELS)


if __name__ == "__main__":
    print("total addresses:", total_addresses())
    print("addr 1..7:", [signal_at(a) for a in range(1, 8)])
    print("signal-heart normal:", address_of("signal-heart"))
    print("signal-P normal (PC):", address_of("signal-P"))
