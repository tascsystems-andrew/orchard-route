"""Uncapped clearance audit of ROUTER-EMITTED copper in a written board.

Why this exists: `kicad-cli pcb drc` stops reporting a rule type after 499
violations. Every measurement of a board that exceeds that is saturated and
useless for before/after comparison — including the "499 [clearance]" figure
this work started from. This checker has no cap, and it measures exactly the
category the via/track geometry work targets: copper the ROUTER emitted
against other copper the ROUTER emitted, on the same layer, different nets.

It is deliberately NOT a general DRC. It does not check pads, zones, the
board edge, or the copper that was already in the input file (the router
cannot see that copper at all — see AGENTS.md's honest limitations).

Geometry: a segment is a capsule of `width` about its centreline, a via is a
disc of `size`. Two different-net items on a shared layer violate when their
copper-to-copper gap is below the net-class clearance. Uniform-grid broad
phase keyed on the largest clearance-inflated extent.

Usage: python scripts/copper_audit.py SOURCE.kicad_pcb ROUTED.kicad_pcb
"""
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from board import parse_sexpr, QStr                       # noqa: E402
from lattice import default_copper_rules                  # noqa: E402
from writeback import (DEFAULT_TRACK_MM, DEFAULT_VIA_MM,   # noqa: E402
                       load_net_class_widths, project_file_for)


def _kids(node, tag):
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            yield c


def _first(node, tag):
    return next(_kids(node, tag), None)


def _nums(node, n):
    out = []
    for a in node[1:]:
        if isinstance(a, str):
            try:
                out.append(float(a))
            except ValueError:
                pass
        if len(out) == n:
            break
    return out


def load_copper(path):
    """(segments, vias) of a .kicad_pcb.

    segment: (x1, y1, x2, y2, width, layer, net_key, uuid)
    via:     (x, y, size, net_key, uuid)  — vias span all copper layers here,
             which is what writeback emits.
    net_key is the net's NAME when the file references nets by name, else the
    numeric code as a string; either way it is a stable identity for
    same-net/different-net comparison within one file.
    """
    with open(path, encoding="utf-8") as f:
        root = parse_sexpr(f.read())
    segs, vias = [], []
    for s in _kids(root, "segment"):
        st, en = _first(s, "start"), _first(s, "end")
        w, ly, nt, uu = (_first(s, k) for k in ("width", "layer", "net", "uuid"))
        if not (st and en and w and ly and nt):
            continue
        (x1, y1), (x2, y2) = _nums(st, 2), _nums(en, 2)
        segs.append((x1, y1, x2, y2, float(w[1]), str(ly[1]),
                     str(nt[1]), str(uu[1]) if uu else None))
    for v in _kids(root, "via"):
        at, sz, nt, uu = (_first(v, k) for k in ("at", "size", "net", "uuid"))
        if not (at and sz and nt):
            continue
        x, y = _nums(at, 2)
        vias.append((x, y, float(sz[1]), str(nt[1]),
                     str(uu[1]) if uu else None))
    return segs, vias


def seg_seg_gap(a, b):
    """Copper edge gap between two segment capsules (negative = overlap)."""
    d = _seg_seg_dist(a[0], a[1], a[2], a[3], b[0], b[1], b[2], b[3])
    return d - a[4] / 2.0 - b[4] / 2.0


def _pt_seg(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return math.hypot(px - (ax + u * vx), py - (ay + u * vy))


def _seg_seg_dist(ax, ay, bx, by, cx, cy, dx, dy):
    # Proper intersection => distance 0; otherwise the minimum is attained at
    # one of the four endpoint-to-segment distances.
    d1 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    d2 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax)
    d3 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx)
    d4 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_pt_seg(ax, ay, cx, cy, dx, dy), _pt_seg(bx, by, cx, cy, dx, dy),
               _pt_seg(cx, cy, ax, ay, bx, by), _pt_seg(dx, dy, ax, ay, bx, by))


def audit(source_pcb, routed_pcb, clearance_mm=None):
    """Count uncapped clearance violations among ROUTER-EMITTED copper.

    Router-emitted = present in routed_pcb and absent from source_pcb, matched
    by uuid (writeback mints a fresh uuid4 per emitted node).
    Returns (violations, stats) where each violation is
    (kind, net_a, net_b, gap_mm, required_mm, (x, y)).
    """
    with open(source_pcb, encoding="utf-8") as f:
        old_uuids = set(re.findall(r'\(uuid "([0-9a-fA-F-]{36})"\)', f.read()))
    segs, vias = load_copper(routed_pcb)
    new_segs = [s for s in segs if s[7] and s[7] not in old_uuids]
    new_vias = [v for v in vias if v[4] and v[4] not in old_uuids]

    if clearance_mm is None:
        clearance_mm, _ = default_copper_rules(source_pcb)

    # Per-net clearance would need the class map; the Default clearance is what
    # lattice.clearance_map and geometry.py both use, so use the same number.
    req = clearance_mm

    items = []   # (kind, x0, y0, x1, y1, half_width, layerset, net)
    for x1, y1, x2, y2, w, ly, net, _u in new_segs:
        items.append(("track", x1, y1, x2, y2, w / 2.0, ly, net))
    for x, y, size, net, _u in new_vias:
        items.append(("via", x, y, x, y, size / 2.0, None, net))  # all layers

    cell = max(2.0, req + 2 * max((it[5] for it in items), default=0.5))
    grid = {}
    for i, it in enumerate(items):
        # INFLATE before bucketing: two items violate when their COPPER EDGES
        # come within `req`, so an item's reach is its centreline bbox grown by
        # half its width plus the clearance. Bucketing the raw bbox let a
        # violating pair sit in adjacent cells and never be compared — it
        # undercounted every board it measured (voxy 17 reported vs 29 real).
        pad = it[5] + req
        x0, x1 = sorted((it[1], it[3]))
        y0, y1 = sorted((it[2], it[4]))
        x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
        for gx in range(int(math.floor(x0 / cell)), int(math.floor(x1 / cell)) + 1):
            for gy in range(int(math.floor(y0 / cell)),
                            int(math.floor(y1 / cell)) + 1):
                grid.setdefault((gx, gy), []).append(i)

    seen, out = set(), []
    for bucket in grid.values():
        for ii in range(len(bucket)):
            for jj in range(ii + 1, len(bucket)):
                a, b = items[bucket[ii]], items[bucket[jj]]
                if a[7] == b[7]:
                    continue                      # same net
                key = (min(bucket[ii], bucket[jj]), max(bucket[ii], bucket[jj]))
                if key in seen:
                    continue
                seen.add(key)
                if a[6] is not None and b[6] is not None and a[6] != b[6]:
                    continue                      # different single layers
                d = _seg_seg_dist(a[1], a[2], a[3], a[4], b[1], b[2], b[3], b[4])
                gap = d - a[5] - b[5]
                if gap < req - 1e-6:
                    kind = f"{a[0]}-{b[0]}" if a[0] <= b[0] else f"{b[0]}-{a[0]}"
                    out.append((kind, a[7], b[7], gap, req,
                                (round((a[1] + b[1]) / 2, 3),
                                 round((a[2] + b[2]) / 2, 3))))
    stats = {"emitted_tracks": len(new_segs), "emitted_vias": len(new_vias),
             "required_clearance_mm": req, "pairs_tested": len(seen)}
    return out, stats


def main(argv):
    src, routed = argv[0], argv[1]
    vio, stats = audit(src, routed)
    kinds = {}
    for k, *_ in vio:
        kinds[k] = kinds.get(k, 0) + 1
    worst = sorted(vio, key=lambda v: v[3])[:5]
    print(f"{os.path.basename(routed)}")
    print(f"  emitted    : {stats['emitted_tracks']} tracks, "
          f"{stats['emitted_vias']} vias | clearance {stats['required_clearance_mm']} mm")
    print(f"  VIOLATIONS : {len(vio)} (uncapped, router-copper vs router-copper)")
    for k in sorted(kinds):
        print(f"    {k:<12} {kinds[k]}")
    for k, na, nb, gap, r, at in worst:
        print(f"    worst {k} {na!r} vs {nb!r}: gap {gap:.4f} mm "
              f"(need {r}) at {at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
