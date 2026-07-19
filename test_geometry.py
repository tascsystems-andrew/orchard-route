"""Tests for the copper geometry contract and the via-exclusion halo.

Every number here is derived by hand from geometry.py's docstring arithmetic,
not read back from the implementation.
"""
import math

import numpy as np

from geometry import CopperGeometry, halo_offsets
from lattice import build_lattice
from pathfinder import _footprint, _via_halo, route_lattice
from smooth import smooth_net_paths

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def test_arithmetic():
    print("=== the four numbers, hand-computed ===")
    # The measured Voxy case: track 0.25, clearance 0.20, via 0.60, pitch 0.50.
    g = CopperGeometry(pitch_mm=0.5, track_width_mm=0.25,
                       clearance_mm=0.20, via_size_mm=0.6)
    check("orthogonal needs 0.45", abs(g.orthogonal_pitch_mm - 0.45) < 1e-12)
    check("orthogonal legal at 0.5 pitch", g.orthogonal_ok)
    check("diagonal needs sqrt(2)*0.45",
          abs(g.diagonal_pitch_mm - math.sqrt(2) * 0.45) < 1e-12,
          f"{g.diagonal_pitch_mm:.4f}")
    check("diagonals ILLEGAL at 0.5 pitch", not g.diagonals_ok)
    # The reported "actual 0.100mm": 0.5/sqrt(2) - 0.25.
    actual = 0.5 / math.sqrt(2) - 0.25
    check("diagonal gap is the reported 0.104mm", abs(actual - 0.10355) < 1e-4,
          f"{actual:.5f} mm")
    check("via-track exclusion 0.625",
          abs(g.via_track_exclusion_mm - 0.625) < 1e-12)
    check("via-via exclusion 0.80", abs(g.via_via_exclusion_mm - 0.80) < 1e-12)
    check("claim radius is the via-track one",
          g.via_exclusion_mm == g.via_track_exclusion_mm)
    # Symmetric claiming is CONSERVATIVE for via-via, and must never be lax.
    check("via-via enforced >= required",
          g.via_via_enforced_mm() >= g.via_via_exclusion_mm - 1e-9,
          f"enforced {g.via_via_enforced_mm():.3f} vs needed 0.800")
    check("summary states all four", all(
        s in g.summary() for s in ("0.45", "0.64", "0.62", "pitch 0.50")),
        g.summary())
    check("no orthogonal warning when legal",
          not any("ILLEGAL" in w for w in g.warnings()))
    check("diagonal warning carries the numbers",
          any("0.104" in w for w in g.warnings()))

    fine = CopperGeometry(pitch_mm=0.25, track_width_mm=0.2,
                          clearance_mm=0.15, via_size_mm=0.6)
    check("pitch finer than track+clearance is VIOLATED", not fine.orthogonal_ok)
    check("and says so loudly with the number",
          any("ILLEGAL GEOMETRY" in w and "0.350" in w for w in fine.warnings()))


def test_halo_offsets():
    print("=== halo offsets ===")
    # r = 0.625 on a 0.5 grid: the four orthogonal neighbours at 0.500 are in,
    # the diagonals at 0.707 are out.
    offs = halo_offsets(0.5, 0.625)
    check("plus-shape at 0.5 pitch",
          set(offs) == {(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)}, str(offs))
    check("radius 0 claims only itself", halo_offsets(0.5, 0.0) == [(0, 0)])
    # r = 0.75 pulls the diagonals (0.707) in.
    check("diagonals join at r=0.75", (1, 1) in halo_offsets(0.5, 0.75))


def test_footprint_covers_all_layers():
    print("=== a via claims its neighbourhood on every layer ===")
    lat = build_lattice(9, 9, 2, pitch_mm=0.5, directions="both")
    halo = _via_halo(lat, 0.625)
    check("halo built", halo is not None and len(halo) == 5)
    # A path that crosses from layer 0 to layer 1 at (4, 4).
    path = [lat.node(3, 4, 0), lat.node(4, 4, 0), lat.node(4, 4, 1),
            lat.node(5, 4, 1)]
    fp = _footprint(lat, path, halo)
    for il in (0, 1):
        for dx, dy in halo:
            nd = lat.node(4 + dx, 4 + dy, il)
            check(f"claims ({4+dx},{4+dy},L{il})", nd in fp)
    check("does not claim beyond the radius",
          lat.node(4, 6, 0) not in fp and lat.node(6, 4, 1) not in fp)
    check("no halo without a layer change",
          _footprint(lat, [lat.node(3, 4, 0), lat.node(4, 4, 0)], halo)
          == {lat.node(3, 4, 0), lat.node(4, 4, 0)})
    check("exclusion off => footprint is the path",
          _via_halo(lat, 0.0) is None
          and _footprint(lat, path, None) == set(path))


def test_via_exclusion_separates_nets():
    print("=== exclusion pushes a foreign net off a via's neighbourhood ===")
    # Net 1 must change layer in a narrow channel; net 2 wants the node right
    # beside the via. With exclusion off both fit; with it on they cannot
    # share the neighbourhood.
    # A wall down layer 0 at x = 6 forces BOTH nets to dive to layer 1 and
    # back, so each plants two vias in the same narrow window. Their natural
    # (unconstrained) lanes are adjacent rows, which is exactly the 0.5 mm
    # via-to-track spacing the arithmetic forbids.
    W, H = 14, 11
    probe = build_lattice(W, H, 2, pitch_mm=0.5)
    wall = frozenset(probe.node(6, y, 0) for y in range(H))
    lat = build_lattice(W, H, 2, pitch_mm=0.5, directions="both",
                        via_cost=1.0, blocked=wall)

    def pad(x, y, layer):
        return ((lat.node(x, y, layer),), (x * 0.5, y * 0.5))

    net_pads = {1: [pad(1, 3, 0), pad(12, 3, 0)],
                2: [pad(1, 4, 0), pad(12, 4, 0)]}

    def via_xy(res):
        out = []
        for paths in res.net_paths.values():
            for p in paths:
                for a, b in zip(p, p[1:]):
                    ax, ay, al = lat.coords(a)
                    if al != lat.coords(b)[2]:
                        out.append((ax, ay))
        return out

    off = route_lattice(lat, net_pads, via_exclusion_mm=0.0, refine_passes=0)
    on = route_lattice(lat, net_pads, via_exclusion_mm=0.625, refine_passes=0)
    check("both route with exclusion off", not off.failed and len(off.net_paths) == 2)
    check("both still route with exclusion on",
          not on.failed and len(on.net_paths) == 2,
          f"failed={on.failed}")
    # The decisive property: with exclusion on, no node of net 2 lies within
    # the claim radius of any of net 1's vias (and vice versa).
    halo = _via_halo(lat, 0.625)
    # Pad nodes are exempt from halo claims by design (_footprint's docstring),
    # so the property under test is about ROUTED copper only.
    pads = {n for pl in net_pads.values() for nodes, _ in pl for n in nodes}

    def foreign_overlap(res):
        claimed = set()
        for p in res.net_paths[1]:
            claimed |= _footprint(lat, p, halo, frozenset(pads)) - set(p)
        n2 = {v for p in res.net_paths[2] for v in p} - pads
        return claimed & n2

    check("net 2's copper keeps clear of net 1's via halo",
          not foreign_overlap(on), f"overlap={sorted(foreign_overlap(on))}")
    print(f"  note exclusion-off overlap: {len(foreign_overlap(off))} node(s); "
          f"on: {len(foreign_overlap(on))}")
    check("exclusion off DID let copper into the halo (the model bites)",
          bool(foreign_overlap(off)))


def test_pitch_gate_kills_diagonals():
    print("=== smooth.py refuses 45s when the pitch cannot carry them ===")
    lat = build_lattice(9, 9, 1, pitch_mm=0.5, directions="both")
    path = [lat.node(1, 1, 0), lat.node(2, 1, 0), lat.node(2, 2, 0),
            lat.node(3, 2, 0), lat.node(3, 3, 0)]
    occ = {v: 1 for v in path}
    on = smooth_net_paths(lat, {1: [path]}, occ, allow_diagonals=True)[1]
    off = smooth_net_paths(lat, {1: [path]}, occ, allow_diagonals=False)[1]

    def angles(polys):
        out = []
        for _, pts in polys:
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                out.append(abs(x2 - x1) > 1e-9 and abs(y2 - y1) > 1e-9)
        return out

    check("diagonals appear when allowed", any(angles(on)), str(on))
    check("no diagonal survives the gate", not any(angles(off)), str(off))
    check("gate keeps the endpoints", off[0][1][0] == on[0][1][0]
          and off[0][1][-1] == on[0][1][-1])


def main():
    test_arithmetic()
    test_halo_offsets()
    test_footprint_covers_all_layers()
    test_via_exclusion_separates_nets()
    test_pitch_gate_kills_diagonals()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
