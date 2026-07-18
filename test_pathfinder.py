"""Negotiation tests for pathfinder.py — hand-built lattices, no board files.

Each case is a 20x20x2 lattice (even layer horizontal, odd vertical) telling
one congestion story: a clean crossing, a corridor two nets must share, a
corridor only one net can win, and a 4-pad tree. The corridor rows are chosen
so both nets PREFER the same gap node at iteration 0 (net at row 9 by 14 cost
units, net at row 8 by only 2) — negotiation has to push the cheap-to-move
net onto the other gap node.

Run: .venv/bin/python test_pathfinder.py
"""
import time

from lattice import build_lattice
from pathfinder import build_connections, route_lattice

W = H = 20


def _pad(lat, ix, iy, il=0):
    nd = lat.node(ix, iy, il)
    return ((nd,), lat.node_xy_mm(nd))


def _net_nodes(res, net):
    nodes = set()
    for path in res.net_paths.get(net, []):
        nodes.update(path)
    return nodes


def _wall(gap_nodes):
    """Full x=10 wall on both layers minus the gap, as build_lattice blocked=."""
    wall = {l * W * H + y * W + 10 for l in (0, 1) for y in range(H)}
    return frozenset(wall - set(gap_nodes))


def test_cross():
    lat = build_lattice(W, H, 2)
    net_pads = {
        1: [_pad(lat, 0, 10), _pad(lat, 19, 10)],   # row 10, left to right
        2: [_pad(lat, 10, 0), _pad(lat, 10, 19)],   # column 10, top to bottom
    }
    res = route_lattice(lat, net_pads)
    ok = (not res.failed and not res.conflicts
          and len(res.net_paths.get(1, [])) == 1
          and len(res.net_paths.get(2, [])) == 1
          and res.overuse_curve[-1] == 0
          and res.via_count >= 2)
    print(f"CROSS      : {'PASS' if ok else 'FAIL'}  iters={res.iterations}  "
          f"overuse={res.overuse_curve}  vias={res.via_count}  "
          f"wl={res.wirelength_mm:.1f} mm  failed={res.failed}")
    return ok


def test_corridor():
    gap = (9 * W + 10, 10 * W + 10)               # (10,9,l0) and (10,10,l0)
    lat = build_lattice(W, H, 2, blocked=_wall(gap))
    net_pads = {
        1: [_pad(lat, 0, 9), _pad(lat, 19, 9)],
        2: [_pad(lat, 0, 8), _pad(lat, 19, 8)],
    }
    res = route_lattice(lat, net_pads)
    wall1 = {n for n in _net_nodes(res, 1) if lat.coords(n)[0] == 10}
    wall2 = {n for n in _net_nodes(res, 2) if lat.coords(n)[0] == 10}
    ok = (not res.failed and res.overuse_curve[-1] == 0
          and wall1 and wall2 and not (wall1 & wall2))
    print(f"CORRIDOR   : {'PASS' if ok else 'FAIL'}  iters={res.iterations}  "
          f"overuse={res.overuse_curve}  "
          f"wall nodes net1={sorted(lat.coords(n) for n in wall1)} "
          f"net2={sorted(lat.coords(n) for n in wall2)}  failed={res.failed}")
    return ok


def test_starvation():
    gap = (9 * W + 10,)                            # single opening (10,9,l0)
    lat = build_lattice(W, H, 2, blocked=_wall(gap))
    net_pads = {
        1: [_pad(lat, 0, 9), _pad(lat, 19, 9)],
        2: [_pad(lat, 0, 8), _pad(lat, 19, 8)],
    }
    t0 = time.perf_counter()
    res = route_lattice(lat, net_pads, max_iters=12)
    dt = time.perf_counter() - t0
    routed = {n for n, paths in res.net_paths.items() if paths}
    ok = (len(routed) == 1 and len(res.failed) == 1
          and res.failed[0][0] not in routed
          and bool(res.failed[0][1])
          and res.iterations <= 12)
    print(f"STARVATION : {'PASS' if ok else 'FAIL'}  iters={res.iterations}  "
          f"overuse={res.overuse_curve}  routed={sorted(routed)}  "
          f"failed={res.failed}  ({dt:.1f}s)")
    return ok


def test_tree():
    lat = build_lattice(W, H, 2)
    # Y shape: three tips joined at a junction pad. Unique MST: tips-to-center.
    net_pads = {5: [_pad(lat, 10, 3), _pad(lat, 3, 16),
                    _pad(lat, 17, 16), _pad(lat, 10, 10)]}
    conns, conflicts, _claim = build_connections(net_pads)
    res = route_lattice(lat, net_pads)
    ok = (len(conns) == 3 and not conflicts and not res.failed
          and len(res.net_paths.get(5, [])) == 3
          and res.overuse_curve[-1] == 0)
    print(f"TREE       : {'PASS' if ok else 'FAIL'}  connections={len(conns)}  "
          f"iters={res.iterations}  overuse={res.overuse_curve}  "
          f"failed={res.failed}")
    return ok


def test_eqdelta():
    """The reviewed livelock: two gaps, and both nets prefer gap (10,9) by an
    EQUAL detour delta. Symmetric co-ripping swapped these forever; asymmetric
    rip-up (lowest-code tenant keeps) must settle them onto separate gaps."""
    gap = (9 * W + 10, 11 * W + 10)               # (10,9,l0) and (10,11,l0)
    lat = build_lattice(W, H, 2, blocked=_wall(gap))
    net_pads = {
        1: [_pad(lat, 0, 8), _pad(lat, 19, 8)],
        2: [_pad(lat, 0, 6), _pad(lat, 19, 6)],
    }
    res = route_lattice(lat, net_pads)
    ok = (not res.failed and res.overuse_curve[-1] == 0
          and res.iterations <= 10)
    print(f"EQ-DELTA   : {'PASS' if ok else 'FAIL'}  iters={res.iterations}  "
          f"overuse={res.overuse_curve}  failed={res.failed}")
    return ok


if __name__ == "__main__":
    results = [test_cross(), test_corridor(), test_starvation(), test_tree(),
               test_eqdelta()]
    print(f"RESULT: {'PASS' if all(results) else 'FAIL'} "
          f"({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)
