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

IDENTIFYING EMITTED COPPER is the whole measurement, and it must never fail
quietly. KiCad's node identity has changed twice: KiCad 8+ writes
(uuid "..."), KiCad 6/7 writes (tstamp ...), and KiCad 5 files
(bench/boards/rpi-pico-vga) write NEITHER on segments. writeback mirrors
whatever the source file does, so on a legacy board the emitted copper
carries no id at all. Keying on uuid alone reported `emitted: 0 tracks,
0 vias / VIOLATIONS: 0` for a file containing 700+ router segments — a
confident clean bill on copper that was never looked at, which is the worst
failure mode this repo has.

So: items are matched by id (uuid OR tstamp) when they have one, and by an
exact geometric signature when they do not — writeback only ever APPENDS, so
every source item reappears byte-identical in the output and the leftover is
the emission. On top of that a hard guard refuses to report at all when the
audit found nothing in an output file that is larger than its source.
"I measured nothing" and "I measured zero violations" print differently and
exit differently.

Usage: python scripts/copper_audit.py SOURCE.kicad_pcb ROUTED.kicad_pcb
                                      [--brute-force]
"""
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from board import parse_sexpr                             # noqa: E402
from lattice import default_copper_rules                  # noqa: E402


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


def _node_id(node):
    """A node's stable identity across KiCad generations: (uuid "...") in
    KiCad 8+, (tstamp ...) in 6/7, None in KiCad 5 (which wrote neither on
    track nodes). None is a legitimate answer and callers must handle it —
    treating it as "not emitted" is the bug this function exists to end."""
    for tag in ("uuid", "tstamp"):
        got = _first(node, tag)
        if got is not None and len(got) > 1:
            return str(got[1])
    return None


def load_copper(path):
    """(segments, vias) of a .kicad_pcb.

    segment: (x1, y1, x2, y2, width, layer, net_key, node_id)
    via:     (x, y, size, net_key, node_id)  — vias span all copper layers
             here, which is what writeback emits.
    net_key is the net's NAME when the file references nets by name, else the
    numeric code as a string; either way it is a stable identity for
    same-net/different-net comparison within one file. node_id is uuid or
    tstamp, or None on files that carry neither.
    """
    with open(path, encoding="utf-8") as f:
        root = parse_sexpr(f.read())
    segs, vias = [], []
    for s in _kids(root, "segment"):
        st, en = _first(s, "start"), _first(s, "end")
        w, ly, nt = (_first(s, k) for k in ("width", "layer", "net"))
        if not (st and en and w and ly and nt):
            continue
        (x1, y1), (x2, y2) = _nums(st, 2), _nums(en, 2)
        segs.append((x1, y1, x2, y2, float(w[1]), str(ly[1]),
                     str(nt[1]), _node_id(s)))
    for v in _kids(root, "via"):
        at, sz, nt = (_first(v, k) for k in ("at", "size", "net"))
        if not (at and sz and nt):
            continue
        x, y = _nums(at, 2)
        vias.append((x, y, float(sz[1]), str(nt[1]), _node_id(v)))
    return segs, vias


class AuditBlind(RuntimeError):
    """The audit could not see the copper it was asked to measure.

    Raised instead of returning a clean bill, because a zero this checker
    cannot justify is indistinguishable from a zero it earned, and callers
    quote these numbers as proof a routing run is clean."""


def _identity(item, kind):
    """The key that decides whether an item in the routed file also exists in
    the source: its node id when it has one, else its exact geometry. Both
    are exact — writeback appends text and rewrites nothing, so a source item
    reappears in the output with identical bytes."""
    nid = item[-1]
    if nid is not None:
        return ("id", nid)
    return ("geom", kind) + tuple(item[:-1])


def emitted_copper(source_pcb, routed_pcb):
    """(new_segments, new_vias): the copper `routed_pcb` has that
    `source_pcb` does not, by multiset difference over _identity keys.

    Multiset, not set: a KiCad 5 board can legitimately carry two identical
    id-less segments, and a set difference would drop the router's copy of
    one."""
    src_segs, src_vias = load_copper(source_pcb)
    segs, vias = load_copper(routed_pcb)

    def diff(source_items, routed_items, kind):
        pool = Counter(_identity(it, kind) for it in source_items)
        out = []
        for it in routed_items:
            key = _identity(it, kind)
            if pool.get(key):
                pool[key] -= 1
            else:
                out.append(it)
        return out

    return (diff(src_segs, segs, "seg"), diff(src_vias, vias, "via"),
            {"source_tracks": len(src_segs), "source_vias": len(src_vias),
             "routed_tracks": len(segs), "routed_vias": len(vias),
             "ids_present": sum(1 for it in segs + vias if it[-1] is not None)})


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


def _items_for(new_segs, new_vias):
    """(kind, x0, y0, x1, y1, half_width, layer_or_None, net) per copper item.
    A via's layer is None: its barrel is on every copper layer."""
    items = []
    for x1, y1, x2, y2, w, ly, net, _i in new_segs:
        items.append(("track", x1, y1, x2, y2, w / 2.0, ly, net))
    for x, y, size, net, _i in new_vias:
        items.append(("via", x, y, x, y, size / 2.0, None, net))
    return items


def _pair_violation(a, b, req):
    """The violation tuple for a pair, or None. The one place the geometry
    predicate lives, so the broad-phase and brute-force paths cannot disagree
    about what a violation IS — only about which pairs they look at."""
    if a[7] == b[7]:
        return None                                   # same net
    if a[6] is not None and b[6] is not None and a[6] != b[6]:
        return None                                   # different single layers
    d = _seg_seg_dist(a[1], a[2], a[3], a[4], b[1], b[2], b[3], b[4])
    gap = d - a[5] - b[5]
    if gap >= req - 1e-6:
        return None
    kind = f"{a[0]}-{b[0]}" if a[0] <= b[0] else f"{b[0]}-{a[0]}"
    return (kind, a[7], b[7], gap, req,
            (round((a[1] + b[1]) / 2, 3), round((a[2] + b[2]) / 2, 3)))


def audit_bruteforce(source_pcb, routed_pcb, clearance_mm=None):
    """The same measurement with NO broad phase: every emitted pair, O(n^2).

    Exists to keep the spatial index honest. A broad phase can only ever be
    wrong in one direction that matters — missing pairs — and the only way to
    know it does not is to compare against the exhaustive answer on real
    boards. See test_copper_audit.py.
    """
    new_segs, new_vias, _ = emitted_copper(source_pcb, routed_pcb)
    if clearance_mm is None:
        clearance_mm, _ = default_copper_rules(source_pcb)
    items = _items_for(new_segs, new_vias)
    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            v = _pair_violation(items[i], items[j], clearance_mm)
            if v is not None:
                out.append(v)
    return out, {"emitted_tracks": len(new_segs), "emitted_vias": len(new_vias),
                 "required_clearance_mm": clearance_mm,
                 "pairs_tested": len(items) * (len(items) - 1) // 2}


def audit(source_pcb, routed_pcb, clearance_mm=None):
    """Count uncapped clearance violations among ROUTER-EMITTED copper.

    Router-emitted = present in routed_pcb and absent from source_pcb — see
    emitted_copper for how that is decided across KiCad's three node-identity
    generations.

    Returns (violations, stats) where each violation is
    (kind, net_a, net_b, gap_mm, required_mm, (x, y)).

    Raises AuditBlind when it finds no emitted copper in an output file that
    is bigger than its source. That combination means the identification rule
    failed, not that the router emitted nothing, and reporting
    "VIOLATIONS: 0" for it would be a lie of exactly the kind this checker
    was written to catch elsewhere.
    """
    new_segs, new_vias, ident = emitted_copper(source_pcb, routed_pcb)

    if not new_segs and not new_vias:
        src_bytes = os.path.getsize(source_pcb)
        out_bytes = os.path.getsize(routed_pcb)
        if out_bytes > src_bytes:
            raise AuditBlind(
                f"MEASURED NOTHING. {os.path.basename(routed_pcb)} is "
                f"{out_bytes - src_bytes} bytes larger than "
                f"{os.path.basename(source_pcb)}, yet no emitted copper could "
                f"be identified ({ident['routed_tracks']} tracks / "
                f"{ident['routed_vias']} vias in the output, "
                f"{ident['source_tracks']}/{ident['source_vias']} in the "
                f"source, {ident['ids_present']} of them carrying a "
                f"uuid/tstamp). This is a failure to MEASURE, not a clean "
                f"board: do not quote a violation count from this run.")

    if clearance_mm is None:
        clearance_mm, _ = default_copper_rules(source_pcb)

    # Per-net clearance would need the class map; the Default clearance is what
    # lattice.clearance_map and geometry.py both use, so use the same number.
    req = clearance_mm

    items = _items_for(new_segs, new_vias)

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
                key = (min(bucket[ii], bucket[jj]), max(bucket[ii], bucket[jj]))
                if key in seen:
                    continue
                seen.add(key)
                v = _pair_violation(items[bucket[ii]], items[bucket[jj]], req)
                if v is not None:
                    out.append(v)
    stats = {"emitted_tracks": len(new_segs), "emitted_vias": len(new_vias),
             "required_clearance_mm": req, "pairs_tested": len(seen),
             "items": len(items), **ident}
    return out, stats


def main(argv):
    brute = "--brute-force" in argv
    argv = [a for a in argv if not a.startswith("--")]
    src, routed = argv[0], argv[1]
    try:
        vio, stats = audit(src, routed)
    except AuditBlind as e:
        # Loud and non-zero. The caller is about to quote this number as
        # evidence a routing run is clean; it must not get a number at all.
        print(f"{os.path.basename(routed)}")
        print(f"  AUDIT FAILED: {e}")
        return 2
    kinds = {}
    for k, *_ in vio:
        kinds[k] = kinds.get(k, 0) + 1
    worst = sorted(vio, key=lambda v: v[3])[:5]
    print(f"{os.path.basename(routed)}")
    print(f"  emitted    : {stats['emitted_tracks']} tracks, "
          f"{stats['emitted_vias']} vias | clearance {stats['required_clearance_mm']} mm")
    print(f"  measured   : {stats['pairs_tested']} pairs over "
          f"{stats['items']} items "
          f"({stats['ids_present']} carry a uuid/tstamp)")
    print(f"  VIOLATIONS : {len(vio)} (uncapped, router-copper vs router-copper)")
    for k in sorted(kinds):
        print(f"    {k:<12} {kinds[k]}")
    for k, na, nb, gap, r, at in worst:
        print(f"    worst {k} {na!r} vs {nb!r}: gap {gap:.4f} mm "
              f"(need {r}) at {at}")
    if brute:
        bvio, bstats = audit_bruteforce(src, routed)
        agree = sorted(map(repr, vio)) == sorted(map(repr, bvio))
        print(f"  brute force: {len(bvio)} violations over "
              f"{bstats['pairs_tested']} pairs (O(n^2), no spatial index) — "
              f"{'AGREES' if agree else 'DISAGREES'}")
        if not agree:
            missed = Counter(map(repr, bvio)) - Counter(map(repr, vio))
            extra = Counter(map(repr, vio)) - Counter(map(repr, bvio))
            print(f"    broad phase MISSED {sum(missed.values())}, "
                  f"invented {sum(extra.values())}")
            for r in list(missed)[:5]:
                print(f"    missed {r}")
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
