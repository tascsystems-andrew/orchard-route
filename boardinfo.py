"""L1 board-info box: the per-board facts the router needs but cannot read off
the copper — the enclosure's per-side component-height clearance and the
intended layout flow direction.

Andrew's model (feedback/placement-fidelity §3, z-clearance): a text box on the
board, filled by the human (ideally pre-templated by the app during schematic
capture), that travels WITH the design instead of living in a CLI flag nobody
remembers. It is a KiCad-native `gr_text` — so it round-trips through the editor
untouched — whose lines the router parses tolerantly:

    max component height front: 30 mm
    max component height back: 3 mm
    layout direction: left to right

Any line the router does not recognise is ignored, and any field the box omits
comes back None (then a CLI value, or an unverified flag, fills in). This module
only READS; writing the template box into a board is a KiCad-editor / Konnect
job, not the router's.
"""
import re

from board import parse_sexpr

# "max [component] height front|back|top|bottom [:=] N [mm]" — tolerant of
# spacing, punctuation, and the mm unit. front/top -> F, back/bottom -> B.
_H_RE = re.compile(
    r"max\s*(?:component\s*)?height\s*(front|back|top|bottom)\s*[:=]?\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*(?:mm)?(?![A-Za-z])", re.I)
_LAYOUT_RE = re.compile(r"layout(?:\s*direction)?\s*[:=]\s*([A-Za-z][A-Za-z \-]*)",
                        re.I)
_SIDE = {"front": "F", "top": "F", "back": "B", "bottom": "B"}


def _iter_text(node):
    """Every gr_text / fp_text string on the board, wherever it nests."""
    if isinstance(node, list) and node:
        if node[0] in ("gr_text", "fp_text") and len(node) > 1 \
                and isinstance(node[1], str):
            yield str(node[1])
        for c in node:
            yield from _iter_text(c)


def parse_board_info_text(text):
    """Parse recognised key:value lines out of one or more free-text strings.
    Returns {z_front_mm, z_back_mm, layout_direction} with None for anything not
    stated. Later mentions win (a box edited in place keeps its last value)."""
    info = {"z_front_mm": None, "z_back_mm": None, "layout_direction": None}
    for m in _H_RE.finditer(text):
        side = _SIDE[m.group(1).lower()]
        val = float(m.group(2))
        info["z_front_mm" if side == "F" else "z_back_mm"] = val
    lm = None
    for lm in _LAYOUT_RE.finditer(text):
        pass                                   # keep the last layout: line
    if lm:
        info["layout_direction"] = lm.group(1).strip().lower()
    return info


def read_board_info(board_path):
    """{z_front_mm, z_back_mm, layout_direction, found} from a board's text
    boxes. `found` is True when any recognised field was present, so the caller
    can tell "box says nothing" from "no box at all" and warn accordingly.
    Never raises on a normal board — a board with no info box just returns all
    None / found False."""
    try:
        with open(board_path, encoding="utf-8") as f:
            root = parse_sexpr(f.read())
    except (OSError, ValueError):
        return {"z_front_mm": None, "z_back_mm": None,
                "layout_direction": None, "found": False}
    blob = "\n".join(_iter_text(root))
    info = parse_board_info_text(blob)
    info["found"] = any(info[k] is not None
                        for k in ("z_front_mm", "z_back_mm", "layout_direction"))
    return info
