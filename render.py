"""SVG rendering of a routed board — the eyeball check on the router's output.

Hand-written SVG strings, stdlib + the repo's own modules only: a render must
never be blocked by a plotting stack, and the output must be deterministic
byte-for-byte for a given (board, lattice, result) so renders diff cleanly
between runs. Coordinates are raw KiCad millimetres throughout — the viewBox
is the board bbox plus a margin, and SVG's y-down convention matches KiCad's,
so no coordinate transform exists anywhere in this file.

RouteResult and paths_to_tracks come from pathfinder.py — the render draws
exactly the copper the router counted, one implementation of the geometry.
(They were briefly duplicated here while the two modules were built in
parallel; pathfinder.py is the producer and owns the contract.)
"""
import os

from board import load_board
from lattice import lattice_for_board
from pathfinder import RouteResult, paths_to_tracks


# ── SVG ───────────────────────────────────────────────────────────────────────

_INNER_COLORS = ("#8e44ad", "#16a085")


def _layer_color(name, layer_names):
    if name == "F.Cu":
        return "#c0392b"
    if name == "B.Cu":
        return "#2980b9"
    inner = [l for l in layer_names if l not in ("F.Cu", "B.Cu")]
    i = inner.index(name) if name in inner else 0
    return _INNER_COLORS[i % 2]


def _f(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(brd, lat, result, out_path, title=""):
    """Board + lattice + RouteResult -> SVG file at out_path."""
    margin = 3.0
    vx, vy = brd.origin_mm[0] - margin, brd.origin_mm[1] - margin
    vw, vh = brd.size_mm[0] + 2 * margin, brd.size_mm[1] + 2 * margin

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{_f(vx)} {_f(vy)} {_f(vw)} {_f(vh)}" '
        f'width="{_f(vw)}mm" height="{_f(vh)}mm">',
        f'<rect x="{_f(vx)}" y="{_f(vy)}" width="{_f(vw)}" height="{_f(vh)}" fill="#ffffff"/>',
        f'<rect x="{_f(brd.origin_mm[0])}" y="{_f(brd.origin_mm[1])}" '
        f'width="{_f(brd.size_mm[0])}" height="{_f(brd.size_mm[1])}" '
        f'fill="none" stroke="#2f3437" stroke-width="0.2"/>',
    ]

    out.append('<g fill="#9aa0a6" fill-opacity="0.8">')
    for p in brd.pads:
        # KiCad angles are CCW with Y down; SVG rotate() is CW in the same
        # Y-down frame, so the sign flips.
        rot = getattr(p, "rotation_deg", 0.0) % 360.0
        xform = (f' transform="rotate({_f(-rot)} {_f(p.x_mm)} {_f(p.y_mm)})"'
                 if rot else "")
        out.append(f'<rect x="{_f(p.x_mm - p.width_mm / 2)}" y="{_f(p.y_mm - p.height_mm / 2)}" '
                   f'width="{_f(p.width_mm)}" height="{_f(p.height_mm)}"{xform}/>')
    out.append('</g>')
    out.append('<g fill="none" stroke="#5f6368" stroke-width="0.25">')
    for p in brd.pads:
        if p.through_hole:
            r = p.drill_mm / 2 if p.drill_mm > 0 else min(p.width_mm, p.height_mm) / 4
            out.append(f'<circle cx="{_f(p.x_mm)}" cy="{_f(p.y_mm)}" r="{_f(r)}"/>')
    out.append('</g>')

    # Prefer the router's smoothed geometry (RouteResult.tracks/.vias, 45s
    # allowed); fall back to raw lattice geometry when absent.
    tracks, vias = result.tracks, result.vias
    if tracks is None or vias is None:
        raw_tracks, raw_vias = paths_to_tracks(lat, result.net_paths)
        tracks = raw_tracks if tracks is None else tracks
        vias = raw_vias if vias is None else vias
    out.append('<g fill="none" stroke-width="0.35" stroke-linecap="round" stroke-opacity="0.85">')
    for x1, y1, x2, y2, layer, _net in tracks:
        out.append(f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" '
                   f'stroke="{_layer_color(layer, lat.layer_names)}"/>')
    out.append('</g>')
    out.append('<g fill="#27ae60">')
    for x, y, _net in vias:
        out.append(f'<circle cx="{_f(x)}" cy="{_f(y)}" r="0.4"/>')
    out.append('</g>')

    failed_codes = {code for code, _ in result.failed}
    out.append('<g fill="none" stroke="#e74c3c" stroke-width="0.3" stroke-dasharray="0.9 0.6">')
    for p in brd.pads:
        if p.net_code in failed_codes:
            r = max(p.width_mm, p.height_mm) / 2 + 0.6
            out.append(f'<circle cx="{_f(p.x_mm)}" cy="{_f(p.y_mm)}" r="{_f(r)}"/>')
    out.append('</g>')

    routed = len([c for c in result.net_paths if c not in failed_codes])
    total = len(set(result.net_paths) | failed_codes)
    lines = []
    if title:
        lines.append(title)
    lines += [
        f"nets  : {routed}/{total} routed",
        f"length: {result.wirelength_mm:.1f} mm",
        f"vias  : {result.via_count}",
        f"iters : {result.iterations}",
    ]
    if result.failed:
        lines.append(f"failed: {len(result.failed)}")
    if result.conflicts:
        lines.append(f"confl : {len(result.conflicts)}")

    tx, ty, step = vx + 1.5, vy + 3.5, 4.0
    out.append('<g font-family="monospace" font-size="3" fill="#202124">')
    for i, line in enumerate(lines):
        out.append(f'<text x="{_f(tx)}" y="{_f(ty + i * step)}">{_esc(line)}</text>')
    legend = [(_layer_color(n, lat.layer_names), n, "solid") for n in lat.layer_names]
    legend += [("#27ae60", "via", "solid"), ("#e74c3c", "failed net", "dash")]
    ly = ty + len(lines) * step + 1.0
    for i, (color, label, style) in enumerate(legend):
        y = ly + i * step
        dash = ' stroke-dasharray="0.9 0.6"' if style == "dash" else ""
        out.append(f'<line x1="{_f(tx)}" y1="{_f(y - 1)}" x2="{_f(tx + 4)}" y2="{_f(y - 1)}" '
                   f'stroke="{color}" stroke-width="0.8"{dash}/>')
        out.append(f'<text x="{_f(tx + 5.5)}" y="{_f(y)}">{_esc(label)}</text>')
    out.append('</g>')

    out.append('</svg>')
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def render_from_cli(board_path, svg_path, pitch, layers):
    """Load a board and render it (pads, outline, empty route) — the preview
    mode pathfinder's CLI calls before/without routing. `layers` is either a
    list of copper layer names or an int N -> F.Cu + first N-2 inners + B.Cu."""
    brd = load_board(board_path)
    if isinstance(layers, int):
        cop = brd.copper_layers
        if layers >= len(cop):
            names = list(cop)
        elif layers >= 2:
            names = [cop[0]] + cop[1:-1][:layers - 2] + [cop[-1]]
        else:
            names = [cop[0]]
    else:
        names = list(layers)
    lat, _pad_nodes, _node_owner = lattice_for_board(brd, pitch, layer_names=names)
    empty = RouteResult(net_paths={}, failed=[], conflicts=[], iterations=0,
                        overuse_curve=[], wirelength_mm=0.0, via_count=0, seconds={})
    render_svg(brd, lat, empty, svg_path, title=os.path.basename(board_path))
