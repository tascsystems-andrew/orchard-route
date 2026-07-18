"""Tests for smooth.py — hand-built runs and paths, no board files.

Cases: a pure staircase collapses to one 45-degree segment; the same
staircase beside a foreign net's parallel track must NOT cut through it
(one side blocked -> partial smoothing on the far side only; both sides
blocked -> every 90 stays); a single corner chamfers when its clipped node
is free and doesn't when occupied; a node where another path of the same
net attaches (junction / endpoint) is never bypassed; totals over a
multi-layer tree: smoothed wirelength <= raw, vias identical, every
polyline's endpoints unchanged. A final route_lattice integration case
proves the default smooth=True wiring end to end (needs mlx).

Run: .venv/bin/python test_smooth.py
"""
import math

from lattice import build_lattice
from pathfinder import paths_to_tracks, route_lattice
from smooth import polylines_to_tracks, smooth_net_paths

# 8-step monotone staircase (1,1) -> (5,5) on layer 0, H step first.
STAIR = [(1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3), (4, 4), (5, 4), (5, 5)]
# Its clipped off-path corner nodes, per side of the diagonal:
UP_LEFT = [(1, 2), (2, 3), (3, 4), (4, 5)]
DOWN_RIGHT = [(3, 1), (4, 2), (5, 3)]


def _lat():
    return build_lattice(8, 8, 2)


def _nodes(lat, pts, il=0):
    return [lat.node(x, y, il) for x, y in pts]


def _mm(pts):
    return [(float(x), float(y)) for x, y in pts]


def _occ(lat, net_paths, extra=()):
    occ = {}
    for net, paths in net_paths.items():
        for p in paths:
            for v in p:
                occ[v] = net
    for pts, net in extra:
        for x, y in pts:
            occ[lat.node(x, y, 0)] = net
    return occ


def test_staircase_free():
    lat = _lat()
    net_paths = {1: [_nodes(lat, STAIR)]}
    polys = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    want = [("L0", _mm([(1, 1), (5, 5)]))]
    ok = polys[1] == want
    print(f"STAIR-FREE : {'PASS' if ok else 'FAIL'}  {polys[1]}")
    return ok


def test_foreign_parallel():
    lat = _lat()
    # Foreign net 2 runs the parallel staircase one cell up-left, occupying
    # every up-left clipped corner of net 1's would-be diagonal.
    par = [(0, 1), (0, 2), (1, 2), (1, 3), (2, 3), (2, 4), (3, 4), (3, 5), (4, 5)]
    net_paths = {1: [_nodes(lat, STAIR)], 2: [_nodes(lat, par)]}
    polys = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    # Net 1 may only smooth AWAY from the foreign track: first corner stays,
    # the rest collapses on the down-right side. Net 2 mirrors it.
    want1 = [("L0", _mm([(1, 1), (2, 1), (5, 4), (5, 5)]))]
    want2 = [("L0", _mm([(0, 1), (0, 2), (3, 5), (4, 5)]))]
    ok_one = polys[1] == want1 and polys[2] == want2

    # Both sides walled in -> every 90 must survive verbatim.
    net_paths_b = {1: [_nodes(lat, STAIR)]}
    occ = _occ(lat, net_paths_b, extra=[(UP_LEFT, 2), (DOWN_RIGHT, 3)])
    polys_b = smooth_net_paths(lat, net_paths_b, occ)
    ok_both = polys_b[1] == [("L0", _mm(STAIR))]
    ok = ok_one and ok_both
    print(f"FOREIGN    : {'PASS' if ok else 'FAIL'}  one-side={polys[1]} "
          f"both-sides={'raw kept' if ok_both else polys_b[1]}")
    return ok


def test_chamfer():
    lat = _lat()
    ell = [(1, 1), (2, 1), (3, 1), (3, 2), (3, 3)]
    net_paths = {1: [_nodes(lat, ell)]}
    # Clipped node (2,2) free -> the single corner chamfers.
    polys = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    want_free = [("L0", _mm([(1, 1), (2, 1), (3, 2), (3, 3)]))]
    ok_free = polys[1] == want_free
    # Clipped node (2,2) foreign -> the corner stays a 90 (collinear-merged).
    occ = _occ(lat, net_paths, extra=[([(2, 2)], 9)])
    polys_b = smooth_net_paths(lat, net_paths, occ)
    want_blocked = [("L0", _mm([(1, 1), (3, 1), (3, 3)]))]
    ok_blocked = polys_b[1] == want_blocked
    ok = ok_free and ok_blocked
    print(f"CHAMFER    : {'PASS' if ok else 'FAIL'}  free={polys[1]}  "
          f"blocked={polys_b[1]}")
    return ok


def test_junction_protected():
    lat = _lat()
    # A second path of the SAME net attaches at (2,1) — a corner the
    # diagonal would bypass. It must stay on-center; smoothing resumes past it.
    stair = [(1, 1), (2, 1), (2, 2), (3, 2), (3, 3)]
    branch = [(2, 1), (2, 0)]
    net_paths = {1: [_nodes(lat, stair), _nodes(lat, branch)]}
    polys = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    want = [("L0", _mm([(1, 1), (2, 1), (3, 2), (3, 3)])),
            ("L0", _mm([(2, 1), (2, 0)]))]
    ok = polys[1] == want
    print(f"JUNCTION   : {'PASS' if ok else 'FAIL'}  {polys[1]}")
    return ok


def test_pad_node_kept():
    lat = _lat()
    # Edges incident to a pad node are never replaced: KiCad connects a pad
    # where its ANCHOR lies inside track copper, and a pad thinner than the
    # pitch may be contacted only by the body of one edge (its snap node is
    # the edge's endpoint). Both the corner itself and an edge endpoint of
    # the replaced pair must block the chamfer — even for the net's OWN pad.
    ell = [(1, 1), (2, 1), (3, 1), (3, 2), (3, 3)]
    net_paths = {1: [_nodes(lat, ell)]}
    want_raw = [("L0", _mm([(1, 1), (3, 1), (3, 3)]))]
    got = {}
    for label, pad_pt in (("corner", (3, 1)), ("edge-end", (3, 2))):
        occ = _occ(lat, net_paths)
        occ[lat.node(*pad_pt, 0)] = 1   # own-net pad copper / snap node
        got[label] = smooth_net_paths(lat, net_paths, occ,
                                      pad_nodes={lat.node(*pad_pt, 0)})[1]
    ok_kept = all(v == want_raw for v in got.values())
    # Same geometry without any pad claim chamfers as usual.
    polys_free = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    ok_free = polys_free[1] == [("L0", _mm([(1, 1), (2, 1), (3, 2), (3, 3)]))]
    ok = ok_kept and ok_free
    print(f"PAD-KEPT   : {'PASS' if ok else 'FAIL'}  corner={got['corner']}  "
          f"edge-end={got['edge-end']}  free={polys_free[1]}")
    return ok


def test_totals():
    lat = _lat()
    # Net 1: staircase on L0, via at (3,3), straight run on L1. Net 2: straight.
    p1 = (_nodes(lat, [(1, 1), (2, 1), (2, 2), (3, 2), (3, 3)], il=0)
          + _nodes(lat, [(3, 3), (3, 4), (3, 5)], il=1))
    p2 = _nodes(lat, [(1, 6), (2, 6), (3, 6), (4, 6)])
    net_paths = {1: [p1], 2: [p2]}
    raw_tracks, raw_vias = paths_to_tracks(lat, net_paths)
    raw_wl = sum(abs(x2 - x1) + abs(y2 - y1)
                 for x1, y1, x2, y2, _, _ in raw_tracks)
    polys = smooth_net_paths(lat, net_paths, _occ(lat, net_paths))
    sm_tracks = polylines_to_tracks(polys)
    sm_wl = sum(math.hypot(x2 - x1, y2 - y1)
                for x1, y1, x2, y2, _, _ in sm_tracks)
    # Layer split preserved: two polylines for net 1, endpoints verbatim,
    # no cross-layer polyline anywhere; via extraction untouched.
    want1 = [("L0", _mm([(1, 1), (3, 3)])), ("L1", _mm([(3, 3), (3, 5)]))]
    endpoints_ok = (polys[1] == want1
                    and polys[2] == [("L0", _mm([(1, 6), (4, 6)]))])
    vias_ok = raw_vias == [(3.0, 3.0, 1)]
    wl_ok = sm_wl <= raw_wl + 1e-9
    exact_ok = abs(sm_wl - (2 * math.sqrt(2) + 2 + 3)) < 1e-9
    ok = endpoints_ok and vias_ok and wl_ok and exact_ok
    print(f"TOTALS     : {'PASS' if ok else 'FAIL'}  wl {raw_wl:.3f} -> "
          f"{sm_wl:.3f} mm  vias={raw_vias}  endpoints_ok={endpoints_ok}")
    return ok


def test_route_lattice_integration():
    lat = build_lattice(20, 20, 2, directions="both")

    def pad(ix, iy):
        nd = lat.node(ix, iy, 0)
        return ((nd,), lat.node_xy_mm(nd))

    net_pads = {1: [pad(2, 2), pad(12, 12)], 2: [pad(2, 12), pad(12, 2)]}
    res = route_lattice(lat, net_pads)                  # smooth default ON
    res_off = route_lattice(lat, net_pads, smooth=False)
    raw_tracks, raw_vias = paths_to_tracks(lat, res.net_paths)
    raw_wl = sum(abs(x2 - x1) + abs(y2 - y1)
                 for x1, y1, x2, y2, _, _ in raw_tracks)
    sm_wl = sum(math.hypot(x2 - x1, y2 - y1)
                for x1, y1, x2, y2, _, _ in res.tracks)
    diag = [t for t in res.tracks
            if t[0] != t[2] and t[1] != t[3]]
    angles_ok = all(x1 == x2 or y1 == y2 or abs(x2 - x1) == abs(y2 - y1)
                    for x1, y1, x2, y2, _, _ in res.tracks)
    ends = {(x1, y1) for x1, y1, *_ in res.tracks} | \
           {(x2, y2) for _, _, x2, y2, *_ in res.tracks}
    pads_ok = {(2.0, 2.0), (12.0, 12.0), (2.0, 12.0), (12.0, 2.0)} <= ends
    ok = (not res.failed and res.overuse_curve[-1] == 0
          and res.tracks is not None and res.vias is not None
          and res_off.tracks is None and res_off.vias is None
          and res.vias == raw_vias
          and res.via_count == len(raw_vias)
          and abs(res.wirelength_mm - sm_wl) < 1e-6
          and sm_wl <= raw_wl + 1e-9
          and (not diag or sm_wl < raw_wl)
          and angles_ok and pads_ok)
    print(f"INTEGRATE  : {'PASS' if ok else 'FAIL'}  wl {raw_wl:.2f} -> "
          f"{res.wirelength_mm:.2f} mm  segs={len(res.tracks)} "
          f"(45s={len(diag)})  vias={res.via_count}  angles_ok={angles_ok}  "
          f"pads_ok={pads_ok}")
    return ok


if __name__ == "__main__":
    results = [test_staircase_free(), test_foreign_parallel(), test_chamfer(),
               test_junction_protected(), test_pad_node_kept(), test_totals(),
               test_route_lattice_integration()]
    print(f"RESULT: {'PASS' if all(results) else 'FAIL'} "
          f"({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)
