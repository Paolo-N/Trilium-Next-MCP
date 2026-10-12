"""Pure helpers to build Relation Map content (no I/O)."""

from __future__ import annotations

import json
import math


def layout_positions(count: int, mode: str = "grid", spacing: int = 260) -> list[tuple[int, int]]:
    """x/y canvas positions for `count` boxes. Modes: grid | circle | row | column."""
    if mode == "circle":
        if count == 1:
            return [(0, 0)]
        radius = max(spacing, int(count * spacing / (2 * math.pi)))
        return [(round(radius * math.cos(2 * math.pi * i / count)),
                 round(radius * math.sin(2 * math.pi * i / count))) for i in range(count)]
    if mode == "row":
        return [(i * spacing, 0) for i in range(count)]
    if mode == "column":
        return [(0, i * (spacing // 2)) for i in range(count)]
    cols = max(1, math.ceil(math.sqrt(count)))
    return [((i % cols) * spacing, (i // cols) * (spacing // 2)) for i in range(count)]


def relation_map_content(note_ids: list[str], mode: str = "grid", spacing: int = 260,
                         positions: dict[str, tuple[int, int]] | None = None) -> str:
    pos = layout_positions(len(note_ids), mode, spacing)
    notes = []
    for nid, (x, y) in zip(note_ids, pos):
        if positions and nid in positions:
            x, y = positions[nid]
        notes.append({"noteId": nid, "x": x, "y": y})
    return json.dumps({"notes": notes, "transform": {"x": 0, "y": 0, "scale": 1}})
