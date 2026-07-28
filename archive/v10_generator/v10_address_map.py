"""v10 address space + carrier-signal allocation — DRAFT, source of truth in progress.

Everything here is planning-stage: derived from review of the WIP BPs pasted
2026-07-21 (`modules/processor_v10.bp.txt` once saved) cross-referenced with
V8.md / SIMD.md, plus the designer's answers on chunking, op counts, and the
carrier-ranking strategy. This module is the SOLE authority on what any v10
signal means. Nothing in any pasted BP's signal/quality picks is
authoritative -- those are template placeholders -- and v10 owes v7/v8 no
compatibility: a name meaning something in an older version, or in the
current WIP BP, is not by itself a reason to exclude it here. Only genuine
constraints exclude a candidate (see _RESERVED_ELSEWHERE below).

See modules/processor_v10.design.md for the narrative writeup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import signal_space as ss

# ---------------------------------------------------------------------------
# Carrier-signal ranking + allocation
# ---------------------------------------------------------------------------
# Ranking principle (designer, 2026-07-21): rank every (signal, quality) combo
# by how likely a *real program* would ever want it as data, then allocate
# control-plane carriers from the least-likely end. Type dominates quality:
#
#   type tiers, most -> least likely to be real data:
#     0: item/fluid signals (mats, factories/objects, other in-game)
#     1: alphanumeric virtuals (signal-0..9, signal-A..Z)
#     2: everything-else virtuals (shapes/colors/arrows/utility + the 76
#        census-appended virtuals) -- "creative only" folds in here; it isn't
#        cleanly separable from signal_space.py's data alone
#
#   quality tiers, most -> least likely to be real data:
#     0: normal
#     1: legendary
#     2: uncommon / rare / epic (tied -- middle tiers, rarely referenced
#        individually in real designs)
#
# Carriers are drawn from (type=2, quality=2): everything-else virtuals at
# uncommon/rare/epic. This module is the ONLY authority on what any v10
# signal means -- there is no inherited "already used" from v7/v8 or from any
# pasted BP; those are template placeholders and v10 owes them no
# compatibility. The only valid exclusions are genuine constraints: signals
# that aren't real addressable data (wildcards), or ones that would be
# actively confusing to reuse for reasons independent of any prior version
# (see _RESERVED_ELSEWHERE). Electrically-isolated buses (e.g. an internal
# ALU-operand bus vs the general memory bus) can safely reuse the same signal
# name without collision -- isolation is about wiring topology, not naming.

_ALPHANUMERIC = {f"signal-{c}" for c in "0123456789"} | {
    f"signal-{chr(c)}" for c in range(ord("A"), ord("Z") + 1)
}

_WILDCARDS = {
    "signal-each", "signal-everything", "signal-anything", "signal-any-quality",
}

# Not "reserved by meaning" -- signal-no-entry is Factorio's default filter
# on a freshly-placed, unconfigured decider combinator (confirmed live: it's
# what shows up on every un-built stub in the WIP BPs). Keeping it out of the
# candidate pool avoids confusing a real carrier assignment with combinator-
# placement noise during construction, nothing more.
_RESERVED_ELSEWHERE = {
    "signal-no-entry",
}

_QUALITY_RANK = {"normal": 0, "legendary": 1, "uncommon": 2, "rare": 2, "epic": 2}


def _type_rank(name: str) -> int:
    if name in ss.ITEMS or name in ss.FLUIDS:
        return 0
    if name in _ALPHANUMERIC:
        return 1
    return 2  # everything-else virtual


def carrier_candidates(count: int, *, quality_pool: tuple[str, ...] = ("uncommon", "rare", "epic")) -> list[tuple[str, str]]:
    """Least-likely-to-be-real-data (signal, quality) pairs, most-obscure first.

    Deterministic (alphabetical within the safest bucket) so re-running this
    produces the same allocation every time.
    """
    pool = [
        name
        for name in sorted(set(ss.VIRTUAL_SIGNALS) | set(ss.APPENDED_VIRTUALS))
        if name not in _WILDCARDS
        and name not in _ALPHANUMERIC
        and name not in _RESERVED_ELSEWHERE
    ]
    candidates: list[tuple[str, str]] = []
    for quality in quality_pool:
        for name in pool:
            candidates.append((name, quality))
    if len(candidates) < count:
        raise ValueError("not enough safe candidates for requested count")
    return candidates[:count]


# Named carrier ROLES needed so far (traced from the two WIP BPs). Signal
# assignment happens in ONE place below (carrier_candidates(), ranked by
# obscurity) -- nothing else in the toolchain should hardcode a carrier
# signal, and no role gets a special case during assignment, including
# SCALAR_READ_ADDR_CARRIER (2026-07-22: previously pinned to a hardcoded
# "signal-R" as a one-off exception -- the designer corrected this: it must
# go through the exact same ranked draw as every other role here).
CARRIER_ROLES = [
    "VEC_WRITE_SELECT",       # W, 1..8         -- which vector register a write targets
    "VEC_READ_SELECT_PORT_A", # R, 1..28        -- vector bus port A source select
    "VEC_READ_SELECT_PORT_B", # S, 1..28        -- vector bus port B source select
    "VSVEC_BROADCAST_OPERAND",   # vec-scal->vec broadcast operand (entities 62-69)
    "VSSCAL_BROADCAST_OPERAND_1",  # vec-scal->scal MUL-reduce operand (entity 142)
    "VSSCAL_BROADCAST_OPERAND_2",  # vec-scal->scal DIV/ADD/SUB-reduce operand (148/152/156)
    "VREDUCE_CMP_THRESHOLD",   # vec->scal CMP-style reduce threshold (entities 172/175/176)
    "VQUALITY_REQUEST_LEVEL",  # VVEC_QUALITY_SET request operand, 1..5 (entity 1, proposed)
    # Scalar-core control-plane carriers (2026-07-21): the scalar core is not
    # a separable subsystem from the vector zone -- the register bank's
    # write-address decode taps directly into the SAME write-address-latch
    # value the scalar core's own chunk1 write path uses. Per the designer,
    # every signal in the scalar-core BP is also just a placeholder; these
    # replace v7/v8's historical literal-letter carriers project-wide.
    "SCALAR_WRITE_ADDR_LATCH",  # replaces legacy "signal-N": write-address
    # one-hot-match carrier (v8 entity 2, `each+0->N`) AND, confusingly but
    # harmlessly (different wire islands), the write-VALUE carrier feeding
    # the address x value product (v8 entity 9). One role covers both --
    # v8 itself reused the same literal signal for both without collision,
    # since neither wire segment overlaps the other.
    "ACCUMULATE_MASK_1", "ACCUMULATE_MASK_2", "ACCUMULATE_MASK_3", "ACCUMULATE_MASK_4",
    # replace shape-vertical/horizontal/curve/curve-2 (write_trigger_reset's
    # accumulate-cell mask set, V8.md)
    "MMAP_PORT_B_MASK",  # replaces shape-circle (mmap_addr's port-B mask, V8.md)
    "VQUALITY_TRANSFER_MARKER",  # replaces literal "signal-W" used for BOTH
    # VVEC_QUALITY_SET's quality_source_signal and its own local 5-quality
    # reference table -- was hardcoded directly in the generator (2026-07-22
    # fix), not owned by this registry at all before.
    "SCALAR_READ_ADDR_CARRIER",  # replaces legacy "signal-R": port A/B read-
    # select condition carrier (v8/WIP-BP convention). Ranked exactly like
    # every role above it -- no special case at assignment time.
    "SCALAR_WRITE_STROBE",  # replaces legacy "signal-W" (v8 entities 5/10/13):
    # a write-enable PULSE gating the value*strobe product -- separate role
    # from SCALAR_WRITE_ADDR_LATCH (address one-hot) even though the WIP-era
    # naming ("N" for address, "W" for strobe) made them look related.
    # Found missing entirely 2026-07-22 in a full signal audit of the loaded
    # scalar core -- it had never been substituted at all before this.
]

CARRIER_SIGNALS: dict[str, tuple[str, str]] = dict(zip(CARRIER_ROLES, carrier_candidates(len(CARRIER_ROLES))))

# SCALAR_READ_ADDR_CARRIER is the one role where obscurity isn't enough:
# collision with real program data must be *impossible*, not just unlikely,
# because it's a wire-level type tag on the read-select condition, not a
# value a program ever reads back. The draw above picks its signal exactly
# like every other role -- no special case at assignment time. The exclusion
# is enforced downstream instead: full_offset_address_table() (generator
# helpers module) calls signal_space.full_table() to get the complete,
# untouched 2480-entry space, and only THEN drops the single (name, quality)
# row matching CARRIER_SIGNALS["SCALAR_READ_ADDR_CARRIER"] before emitting
# table content. 2026-07-22: an earlier version of this did the exclusion by
# mutating signal_space.EXCLUDED (name-level -- removes all 5 quality tiers
# and shrinks CHUNK_WIDTH below); corrected per the designer -- exclude after
# the full table exists, and only the one (name, quality) pair actually
# drawn, not the other 4 qualities of that name.


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

CHUNK_WIDTH = ss.total_addresses()  # every signal x 5 qualities, unmodified canonical width
NUM_VECTOR_REGISTERS = 8  # confirmed live: signal-W-style select decoder is 8-wide

# Chunk 1 (addresses 1..CHUNK_WIDTH) = scalar space, unchanged from v8.
# Chunk (2 + i) for i in 0..NUM_VECTOR_REGISTERS-1 = vector register i's lanes,
# fine-grained: same per-signal layout as chunk1, just offset. Confirmed live:
# addres_table_ext1 = 2481 (chunk2 base), addres_table_ext2 = 4961 (chunk3 base),
# both exactly CHUNK_WIDTH apart.


def register_chunk_base(register_index: int) -> int:
    """First address of vector register `register_index`'s (0-based) lane block."""
    if not (0 <= register_index < NUM_VECTOR_REGISTERS):
        raise ValueError(f"register_index out of range: {register_index}")
    return CHUNK_WIDTH * (register_index + 1) + 1


def register_chunk_end(register_index: int) -> int:
    return register_chunk_base(register_index) + CHUNK_WIDTH - 1


TOTAL_UNIFIED_ADDRESSES = CHUNK_WIDTH * (1 + NUM_VECTOR_REGISTERS)

# DECIDED 2026-07-21: always-on VALU / vec-scal op RESULTS get NO fine-grained
# chunk of their own -- reachable only via the R/S-select vector bus
# (whole-frame). Scalar-facing access to vector state goes through the
# reduction ops or future dedicated vector I/O, not per-lane peeking into an
# op's raw output. Unified address space stays at TOTAL_UNIFIED_ADDRESSES
# (chunk1 + 8 register chunks); it does not grow per op.


# ---------------------------------------------------------------------------
# Standard scalar port A: beyond-chunk1 read extension
# ---------------------------------------------------------------------------
# Scope for v10 (designer, 2026-07-21): port A only, ~4 combinators. Port B
# and instruction fetch stay chunk1-only.
#
# Confirmed from the read patch (modules/processor_v10_patch.bp.txt once
# saved): the two still-blank gate entities should implement
#   each[G] > 0  ->  each[R]
# i.e. condition tests the GREEN network (chunk-boundary match, fed by the
# ext1/ext2 compare stage), value comes from the RED network -- a green-enable
# / red-data split, same shape as the vec-vec MAX/MIN else-branch idiom above.
PORT_A_EXTENSION_CONDITION = "each[G] > 0 -> each[R]"


# ---------------------------------------------------------------------------
# Op inventory
# ---------------------------------------------------------------------------


@dataclass
class VectorOp:
    name: str
    category: str  # "vec_vec" | "vec_scal_vec" | "vec_scal_scal" | "vec_scal"
    op: str  # underlying arithmetic/decider operation
    entities: list[int] = field(default_factory=list)  # WIP-BP entity ids, for traceability
    status: str = "stub"  # "built" | "stub" | "planned"
    has_fine_chunk: bool = False  # DECIDED 2026-07-21: always False. vec-vec /
    # vec-scal-vec op outputs get no per-lane RO chunk in v10 -- reachable
    # only via the R/S-select vector bus. Scalar-facing access goes through
    # the reduction ops (VEC_SCAL_SCAL_OPS / VEC_REDUCE_OPS) or future
    # dedicated vector I/O, which are the intended useful path.
    notes: str = ""


# vec-vec: always-on, each[R] op each[G] -> each (two full vector operands).
# 8 arithmetic (confirmed live, entities still mislabeled "ALU_MUL" in the WIP
# BP except MUL) + 4 conditional (confirmed live, entities 70-73, traced via
# their read-select wiring to entities 112-115 / R=17..20). The conditional
# 4 split into two real shapes:
#   - GT_GATE/LT_GATE (70/71): each[R] where cond holds, else nothing (no
#     else_outputs) -- a value-preserving gate, not a true elementwise op.
#   - MAX/MIN (72/73): each[R] on RED when cond holds, else each[G] on GREEN
#     (else_outputs on the opposite color) -- true elementwise max/min,
#     because red+green sum together once both land on the shared vector bus.
VEC_VEC_OPS = [
    VectorOp("VVEC_XOR", "vec_vec", "XOR", entities=[74], status="built"),
    VectorOp("VVEC_OR", "vec_vec", "OR", entities=[75], status="built"),
    VectorOp("VVEC_AND", "vec_vec", "AND", entities=[76], status="built"),
    VectorOp("VVEC_POW", "vec_vec", "^", entities=[77], status="built"),
    VectorOp("VVEC_MOD", "vec_vec", "%", entities=[78], status="built"),
    VectorOp("VVEC_SUB", "vec_vec", "-", entities=[79], status="built"),
    VectorOp("VVEC_DIV", "vec_vec", "/", entities=[80], status="built"),
    VectorOp("VVEC_MUL", "vec_vec", "*", entities=[81], status="built"),
    # ADD deliberately absent: planned to come free from two-read-port bus
    # summation (V8.md TODO #4), not a dedicated combinator.
    VectorOp("VVEC_GT_GATE", "vec_vec", ">gate", entities=[70], status="built",
             notes="each[R] where R>G, else 0 (no else branch)"),
    VectorOp("VVEC_LT_GATE", "vec_vec", "<gate", entities=[71], status="built",
             notes="each[R] where R<G, else 0; comparator defaults to '<' in-game when unset"),
    VectorOp("VVEC_MAX", "vec_vec", ">select+else", entities=[72], status="built",
             notes="true elementwise max via red/green else-branch split"),
    VectorOp("VVEC_MIN", "vec_vec", "<select+else", entities=[73], status="built",
             notes="true elementwise min via red/green else-branch split"),
]

# vec-scal->vec: each[R] op BROADCAST_OPERAND[G] -> each. Confirmed live as one
# contiguous block, entities 62-69, all keyed off the same broadcast carrier.
VEC_SCAL_VEC_OPS = [
    VectorOp("VSVEC_SUB", "vec_scal_vec", "-", entities=[62], status="built"),
    VectorOp("VSVEC_ADD", "vec_scal_vec", "+", entities=[63], status="built"),
    VectorOp("VSVEC_DIV", "vec_scal_vec", "/", entities=[64], status="built"),
    VectorOp("VSVEC_MUL", "vec_scal_vec", "*", entities=[65], status="built"),
    VectorOp("VSVEC_GT_MASK", "vec_scal_vec", ">mask", entities=[66], status="built",
             notes="copy_count_from_input=false -> outputs 1, not the lane value"),
    VectorOp("VSVEC_LT_MASK", "vec_scal_vec", "<mask", entities=[67], status="built",
             notes="comparator defaults to '<' in-game when unset"),
    VectorOp("VSVEC_GT_SELECT", "vec_scal_vec", ">select", entities=[68], status="built",
             notes="passes the lane's own value where lane > broadcast"),
    VectorOp("VSVEC_LT_SELECT", "vec_scal_vec", "<select", entities=[69], status="built"),
]

# The 8 register write-enable gates (entities 82-89, one per W=1..8) are the
# actual vector register slots -- confirmed live: they're gated by the
# write-select chain (46-53) AND feed the read-select chain (R=1..8, entities
# 124-131) at the SAME index, i.e. one physical location serves both. Their
# own decider condition is still Factorio's unconfigured-combinator default
# (`signal-no-entry != N`) -- real hold-cell logic isn't built yet. (Entities
# 90/91, previously misread as 2 of the 8 registers, are unrelated: they wire
# to write_trigger_set/write_value/pc_autoincrement/ROM -- they're the
# carried-over v8 scalar-core write injector + hold cell, just laid out
# nearby.)
VECTOR_REGISTER_ENTITIES = {  # index 0..7 -> (write-select entity, register/read-select entity)
    0: (53, 89), 1: (52, 88), 2: (51, 87), 3: (50, 86),
    4: (49, 85), 5: (48, 84), 6: (47, 83), 7: (46, 82),
}

# vec-scal->scal (reduction): each[R] op BROADCAST_OPERAND[G], collapsed onto
# ONE output signal (Factorio sums all matching lanes' results into a single
# non-"each" output). Confirmed live, entities 142/148/152/156.
VEC_SCAL_SCAL_OPS = [
    VectorOp("VSSCAL_MUL_REDUCE", "vec_scal_scal", "*", entities=[142], status="built"),
    VectorOp("VSSCAL_DIV_REDUCE", "vec_scal_scal", "/", entities=[148], status="built"),
    VectorOp("VSSCAL_ADD_REDUCE", "vec_scal_scal", "+", entities=[152], status="built"),
    VectorOp("VSSCAL_SUB_REDUCE", "vec_scal_scal", "-", entities=[156], status="built"),
]

# vec->scal (reduction, no scalar operand): pure hardware reductions, several
# built with Factorio 2.0 selector-combinators rather than decider tricks.
#
# CORRECTED 2026-07-21 from the argmax/argmin decoder patch: the main WIP BP's
# entities 167 AND 169 both showed `select_max: true`, which was a copy-paste
# bug, not two real ops -- the intended pair is `select_max: true` (MAX) and
# `select_max: false` (MIN). The patch also adds a decoder stage on top of
# both select outputs: an address-table one-hot match (`each[G] != 0 ->
# each[R]`, same idiom as v7/v8's write-address decode) converts "which
# signal won" into "that signal's table address" -- true ARGMAX/ARGMIN, not
# just the winning value.
VEC_REDUCE_OPS = [
    VectorOp("VREDUCE_SUM", "vec_scal", "each+0", entities=[171], status="built",
             notes="plain full-lane sum, matches SIMD.md's documented idiom"),
    VectorOp("VREDUCE_COUNT", "vec_scal", "selector:count", entities=[160], status="built",
             notes="candidate popcount reduction"),
    VectorOp("VREDUCE_MAX", "vec_scal", "selector:select_max=true", status="built",
             notes="main BP entity 167; patch entity 11 confirms select_max=true is correct"),
    VectorOp("VREDUCE_MIN", "vec_scal", "selector:select_max=false", status="built",
             notes="main BP entity 169 needs select_max flipped to false (was a copy-paste dupe of 167); patch entity 16 shows the correct config"),
    VectorOp("VREDUCE_ARGMAX", "vec_scal", "address-table decode of VREDUCE_MAX's winning signal",
             status="built", notes="patch entities 7,8,9 (addres_table one-hot match on the MAX selector's output)"),
    VectorOp("VREDUCE_ARGMIN", "vec_scal", "address-table decode of VREDUCE_MIN's winning signal",
             status="built", notes="patch entities 12,13,14 (same idiom, fed from the MIN selector)"),
    VectorOp("VREDUCE_CMP", "vec_scal", "compare-vs-broadcast", entities=[172, 175, 176],
             status="built", notes="CMP-style >, <(default), = flags vs a broadcast threshold"),
    VectorOp("VREDUCE_TIME", "vec_scal", "selector:time", entities=[164], status="built",
             notes="game-tick/day-time output, doesn't consume vector inputs; kept intentionally"),
]

# Reduction OUTPUT addresses -- unlike CARRIER_ROLES (control-plane inputs,
# which should be obscure so a real program never collides with them by
# accident), these are RESULTS a program deliberately reads, so they follow
# v8's CMP-flag convention instead (O/Q/T/S at normal quality): a convenient,
# well-known address is the point, not obscurity. Kept at RARE quality (not
# normal) only to stay clear of v8's own M..T reservations at normal quality
# (see signal_space.ALU_SIGNALS_LAST) -- these are otherwise the patch's own
# picks, not re-derived through carrier_candidates().
REDUCTION_OUTPUT_SIGNALS = {
    "VSSCAL_MUL_REDUCE": ("signal-A", "uncommon"),
    "VSSCAL_DIV_REDUCE": ("signal-B", "uncommon"),
    "VSSCAL_ADD_REDUCE": ("signal-C", "uncommon"),
    "VSSCAL_SUB_REDUCE": ("signal-D", "uncommon"),
    "VREDUCE_COUNT": ("signal-K", "uncommon"),
    "VREDUCE_MAX": ("signal-M", "rare"),
    "VREDUCE_MIN": ("signal-N", "rare"),
    "VREDUCE_ARGMAX": ("signal-U", "rare"),
    "VREDUCE_ARGMIN": ("signal-V", "rare"),
    "VREDUCE_CMP_GT": ("signal-E", "uncommon"),
    "VREDUCE_CMP_LT": ("signal-F", "uncommon"),
    "VREDUCE_CMP_EQ": ("signal-G", "uncommon"),
    # VREDUCE_TIME's three outputs were hardcoded literals in the generator
    # (sig("signal-T","rare") etc) until 2026-07-22 -- same values, now
    # actually owned here instead of scattered inline.
    "VREDUCE_TIME_TICK": ("signal-T", "rare"),
    "VREDUCE_TIME_DAY_TICK": ("signal-D", "rare"),
    "VREDUCE_TIME_DAY_LENGTH": ("signal-L", "rare"),
}

# Scalar-core ALU/CMP/PC operand+result signals (v8 entities 32-35 = MUL/DIV/
# ADD/SUB, 49-52 = CMP, 39/40 = PC). v10 explicitly KEEPS v8's exact letter
# convention here rather than reassigning through carrier_candidates() --
# same reasoning as REDUCTION_OUTPUT_SIGNALS: these are well-known, program-
# readable operand/result addresses a compiler needs to target by name, not
# control-plane inputs that should be obscure. The point of this table isn't
# to change the values -- it's for every signal in the loaded scalar core to
# trace to ONE explicit decision here, instead of silently surviving as an
# untouched legacy literal just because nothing renamed it.
#
# IMPORTANT: "signal-N" appears here (CMP's second operand, v8 entities
# 49/50) AND separately as SCALAR_WRITE_ADDR_LATCH's legacy name (write-
# address one-hot, v8 entities 2/8/9/14) -- two different, isolated v8 wire
# roles that happen to reuse the same original letter. Found 2026-07-22: an
# earlier version of generate_v10_processor.py's substitution matched by
# (name, quality) globally across the whole scalar core, so renaming
# "signal-N" for the write-address role ALSO silently renamed the CMP
# block's own N operand to the same new carrier -- breaking the M/N compare,
# since nothing on that wire actually produced the new name. Substitution
# must be scoped per ENTITY NUMBER (see generate_v10_processor.py), not by
# signal name alone, or this class of bug recurs for any other coincidental
# reuse.
ALU_CORE_SIGNALS: dict[str, tuple[str, str]] = {
    letter: (f"signal-{letter}", "normal") for letter in "ABCDEFGHIJKLMNOQST"
}
ALU_CORE_SIGNALS["PC"] = ("signal-P", "normal")

# vec->vec, unary, request-driven: single vector -> vector, no second vector
# operand. Sample op from the designer (2026-07-21), left partially built as
# an exercise -- filled in here, needs a live bench check (selector-combinator
# quality-transfer semantics can't be verified without running the game).
#
# VVEC_QUALITY_SET: rewrite every lane's quality tier to a runtime-selected
# level (1..5), values unchanged. Use case: bulk quality retagging in one
# tick ("instant DMA"), or feeding quality-aware game mechanics (quality
# filters, quality-gated recipes) directly from vector state.
#
# Patch as given: entity 3 (selector-combinator, operation="quality-transfer",
# select_quality_from_signal=true, quality_source_signal=signal-W) transfers
# every input lane to whatever quality "signal-W" carries on entity 3's green
# input. Entity 2 (constant) is a reference table: signal-A at each of the 5
# qualities, valued 1..5 -- i.e. "quality level N" encoded as data. Entity 1
# (decider, left blank) is the missing link between a runtime request (a
# plain number 1..5) and that table.
#
# Proposed fill-in for entity 1: `each[G] = VQUALITY_REQUEST_LEVEL[R] ->
# each[G]` (comparator "="; first_signal=each on GREEN so it iterates entity
# 2's 5 rows, second_signal=the new broadcast carrier below on RED so only
# ONE row matches; output=each on GREEN so the winning row's quality tag
# survives unchanged onto the wire feeding entity 3).
#
# One mismatch to flag: entity 3's quality_source_signal is fixed as
# "signal-W", but entity 2's table is built from "signal-A" -- these need to
# match (a decider output can't rename a matched "each" signal to a
# different fixed name while preserving its quality). Simplest fix: rebuild
# entity 2's table using "signal-W" instead of "signal-A" (both are
# placeholders per the no-BP-is-authoritative rule, so no real cost either
# way) rather than adding a relabeling stage.
VVEC_QUALITY_SET = VectorOp(
    "VVEC_QUALITY_SET", "vec_vec_unary", "selector:quality-transfer",
    entities=[1, 2, 3], status="built",
    notes="entity 1's condition filled in above as a proposal, not live-verified; "
          "entity 2's table signal should match entity 3's quality_source_signal (both signal-W)",
)

ALL_OPS = VEC_VEC_OPS + VEC_SCAL_VEC_OPS + VEC_SCAL_SCAL_OPS + VEC_REDUCE_OPS + [VVEC_QUALITY_SET]


# ---------------------------------------------------------------------------
# Vector-bus read-select enumeration (R = 1..28)
# ---------------------------------------------------------------------------
# Wire-confirmed live (traced entity-by-entity through the R/S select-gate
# chain, entities 104-131, each gated on `signal-R~uncommon == N` OR
# `signal-S~uncommon == N` -- confirmed by designer as OR, i.e. two
# genuinely independent selectors): read-select does NOT enumerate in clean
# category blocks in ascending op order -- each category is wired in REVERSE
# op order, and category order is registers, vec-vec arith, vec-vec
# conditional, vec-scal-vec conditional, vec-scal-vec arith. Two independent
# select registers (Port A = R, Port B = S) each index this same 28-entry
# list; the two selected full-vector frames sum onto the shared vector bus.

READ_SELECT_TABLE = [
    # (R index, source name, gate entity, source entity/entities)
    (1, "VREG0", 89, [89]), (2, "VREG1", 88, [88]), (3, "VREG2", 87, [87]),
    (4, "VREG3", 86, [86]), (5, "VREG4", 85, [85]), (6, "VREG5", 84, [84]),
    (7, "VREG6", 83, [83]), (8, "VREG7", 82, [82]),
    (9, "VVEC_MUL", 123, [81]), (10, "VVEC_DIV", 122, [80]), (11, "VVEC_SUB", 121, [79]),
    (12, "VVEC_MOD", 120, [78]), (13, "VVEC_POW", 119, [77]), (14, "VVEC_AND", 118, [76]),
    (15, "VVEC_OR", 117, [75]), (16, "VVEC_XOR", 116, [74]),
    (17, "VVEC_MIN", 115, [73]), (18, "VVEC_MAX", 114, [72]),
    (19, "VVEC_LT_GATE", 113, [71]), (20, "VVEC_GT_GATE", 112, [70]),
    (21, "VSVEC_LT_SELECT", 111, [69]), (22, "VSVEC_GT_SELECT", 110, [68]),
    (23, "VSVEC_LT_MASK", 109, [67]), (24, "VSVEC_GT_MASK", 108, [66]),
    (25, "VSVEC_MUL", 107, [65]), (26, "VSVEC_DIV", 106, [64]),
    (27, "VSVEC_ADD", 105, [63]), (28, "VSVEC_SUB", 104, [62]),
]
assert len(READ_SELECT_TABLE) == 28

READ_SELECT_SOURCES = [row[1] for row in READ_SELECT_TABLE]
READ_SELECT_INDEX = {row[1]: row[0] for row in READ_SELECT_TABLE}  # name -> R/S value, 1-based


if __name__ == "__main__":
    print(f"CHUNK_WIDTH = {CHUNK_WIDTH}")
    print(f"chunk1 (scalar): 1..{CHUNK_WIDTH}")
    for i in range(NUM_VECTOR_REGISTERS):
        print(f"chunk{i + 2} (VREG{i}): {register_chunk_base(i)}..{register_chunk_end(i)}")
    print(f"total unified addresses: {TOTAL_UNIFIED_ADDRESSES}")
    print()
    print("read-select (R = 1..28):")
    for name, idx in READ_SELECT_INDEX.items():
        print(f"  R={idx:2d}  {name}")
    print()
    print("carrier allocation:")
    for role, (name, quality) in CARRIER_SIGNALS.items():
        addr = ss.address_of(name, quality)
        note = " (dropped from v10's own generated offset tables)" if role == "SCALAR_READ_ADDR_CARRIER" else ""
        print(f"  {role:32s} -> {name}@{quality}  (chunk1 addr {addr}){note}")
