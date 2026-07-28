#!/usr/bin/env python3
"""Add the CMP unit (compare/max/min deciders) to modules/processor.source.json.

Reconstructed from the user's section export (scratchpad alu_cmp.bp.txt), mapped
into main-blueprint coordinates via the shared ALU rows: section ALU_SUB at
(1,-5.5) vs main ALU_SUB (entity 24) at (0,-2.5) -> offset (-1, +3).

Entities 61-66:
  61 decider (2,-2.5): IF M < N -> M ELSE N   (min select)
  62 decider (2,-1.5): IF M > N -> M ELSE N   (max select)
  63 decider (2,-0.5): IF M >= N -> Q=1       (flag)
  64 decider (2, 0.5): IF M > N -> O=1        (flag)
  65 arith   (4,-2.5): each + 0 -> S          (min result, +1 tick)
  66 arith   (4,-1.5): each + 0 -> T          (max result, +1 tick; NOT signal-R,
                                               which is the read-address carrier)

Wired onto the shared ALU red-input / green-output buses via MUL (entity 27).
Idempotent: skips if entity 61 already exists.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "modules" / "processor.source.json"


def sig(name):
    return {"type": "virtual", "name": f"signal-{name}"}


def decider(num, x, y, conditions, outputs, else_outputs):
    return {
        "entity_number": num,
        "name": "decider-combinator",
        "position": {"x": x, "y": y},
        "direction": 4,
        "control_behavior": {
            "decider_conditions": {
                "conditions": conditions,
                "outputs": outputs,
                "else_outputs": else_outputs,
            }
        },
    }


def arith(num, x, y, out_letter):
    return {
        "entity_number": num,
        "name": "arithmetic-combinator",
        "position": {"x": x, "y": y},
        "direction": 4,
        "control_behavior": {
            "arithmetic_conditions": {
                "first_signal": {"type": "virtual", "name": "signal-each"},
                "second_constant": 0,
                "operation": "+",
                "output_signal": sig(out_letter),
            }
        },
    }


def main():
    src = json.loads(SRC.read_text(encoding="utf-8"))
    bp = src["blueprint"]
    if any(e["entity_number"] == 61 for e in bp["entities"]):
        print("CMP unit already present, nothing to do")
        return

    m_lt_n = [{"first_signal": sig("M"), "second_signal": sig("N")}]
    m_gt_n = [{"first_signal": sig("M"), "second_signal": sig("N"), "comparator": ">"}]
    m_ge_n = [{"first_signal": sig("M"), "second_signal": sig("N"), "comparator": "≥"}]

    bp["entities"] += [
        decider(61, 2, -2.5, m_lt_n, [{"signal": sig("M")}], [{"signal": sig("N")}]),
        decider(62, 2, -1.5, m_gt_n, [{"signal": sig("M")}], [{"signal": sig("N")}]),
        decider(63, 2, -0.5, m_ge_n, [{"signal": sig("Q"), "copy_count_from_input": False}], []),
        decider(64, 2, 0.5, m_gt_n, [{"signal": sig("O"), "copy_count_from_input": False}], []),
        arith(65, 4, -2.5, "S"),
        arith(66, 4, -1.5, "T"),
    ]
    bp["wires"] += [
        [27, 1, 64, 1],  # MUL red-in bus -> O decider
        [27, 4, 64, 4],  # MUL green-out bus -> O decider out
        [61, 1, 62, 1],
        [61, 4, 65, 2],  # min select -> S arith
        [62, 1, 63, 1],
        [62, 4, 66, 2],  # max select -> R arith
        [63, 1, 64, 1],
        [63, 4, 64, 4],
        [63, 4, 66, 4],  # R/S arith outputs join the green bus
        [65, 4, 66, 4],
    ]
    src["description"] = src["description"].replace(
        "Processor v4", "Processor v5 (CMP unit: M,N -> O,Q flags + R=max,S=min)"
    )
    SRC.write_text(json.dumps(src, indent=2), encoding="utf-8")
    print(f"CMP unit added: {len(bp['entities'])} entities, {len(bp['wires'])} wires")


if __name__ == "__main__":
    main()
