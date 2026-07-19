"""Negotiation tests for pathfinder.py — hand-built lattices, no board files.

Each case is a 20x20x2 lattice (even layer horizontal, odd vertical) telling
one congestion story: a clean crossing, a corridor two nets must share, a
corridor only one net can win, a 4-pad tree, and a standoff whose winners all
leave (MEANDER — the refine pass's reason to exist). ASYM-YIELD is the one
exception (20x20x1, directions="both"): a keeper-blindness deadlock that only
keeper alternation can break. The corridor rows are
chosen so both nets PREFER the same gap node at iteration 0 (net at row 9 by
14 cost units, net at row 8 by only 2) — negotiation has to push the
cheap-to-move net onto the other gap node.

Run: .venv/bin/python test_pathfinder.py
"""
import time

from backtrace import path_cost
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


def _total_cost(lat, res):
    """Wirelength-equivalent cost (edge weights incl. vias) over all paths."""
    return sum(path_cost(p, lat.row_ptr, lat.col_idx, lat.weight)
               for paths in res.net_paths.values() for p in paths)


def test_cross():
    lat = build_lattice(W, H, 2)
    net_pads = {
        1: [_pad(lat, 0, 10), _pad(lat, 19, 10)],   # row 10, left to right
        2: [_pad(lat, 10, 0), _pad(lat, 10, 19)],   # column 10, top to bottom
    }
    res = route_lattice(lat, net_pads)
    # Already optimal at iteration 1: refine (on by default) must not touch it.
    res_off = route_lattice(lat, net_pads, refine_passes=0)
    ok = (not res.failed and not res.conflicts
          and len(res.net_paths.get(1, [])) == 1
          and len(res.net_paths.get(2, [])) == 1
          and res.overuse_curve[-1] == 0
          and res.via_count >= 2
          and res.net_paths == res_off.net_paths)
    print(f"CROSS      : {'PASS' if ok else 'FAIL'}  iters={res.iterations}  "
          f"overuse={res.overuse_curve}  vias={res.via_count}  "
          f"wl={res.wirelength_mm:.1f} mm  failed={res.failed}  "
          f"refine_noop={res.net_paths == res_off.net_paths}")
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


def test_meander():
    """Negotiation provably leaves a detour: gaps at y=1, 9, 17; nets on rows
    8 and 10 both prefer the middle gap by an equal delta of 12. Slow present
    growth keeps both in the standoff until streak>=4 full rips take over;
    from then hist alone prices the middle gap, crossing 12 for BOTH nets in
    the same iteration — both flee to their far gaps and the middle gap ends
    FREE but poisoned. Refine reroutes against the finished board (no hist)
    and must pull exactly one net back through the middle gap."""
    gaps = (1 * W + 10, 9 * W + 10, 17 * W + 10)
    kw = dict(present_factor=0.3, present_growth=1.1)
    lat = build_lattice(W, H, 2, blocked=_wall(gaps))
    net_pads = {
        1: [_pad(lat, 0, 8), _pad(lat, 19, 8)],
        2: [_pad(lat, 0, 10), _pad(lat, 19, 10)],
    }
    res_off = route_lattice(lat, net_pads, refine_passes=0, **kw)
    res_on = route_lattice(lat, net_pads, **kw)

    def gap_rows(res):
        return {n: sorted({lat.coords(v)[1] for p in ps for v in p
                           if lat.coords(v)[0] == 10})
                for n, ps in res.net_paths.items()}

    cost_off, cost_on = _total_cost(lat, res_off), _total_cost(lat, res_on)
    # The scenario premise: without refine, nobody crosses at y=9 — the
    # middle gap is free and both nets carry detours around pure history.
    detour_left = all(9 not in rows for rows in gap_rows(res_off).values())
    nodes1 = {v for p in res_on.net_paths.get(1, []) for v in p}
    nodes2 = {v for p in res_on.net_paths.get(2, []) for v in p}
    ok = (not res_off.failed and res_off.overuse_curve[-1] == 0
          and not res_on.failed and res_on.overuse_curve[-1] == 0
          and detour_left
          and cost_on <= cost_off           # refine never worsens...
          and cost_on < cost_off            # ...and here provably recovers
          and not (nodes1 & nodes2))        # overuse still 0 after refine
    print(f"MEANDER    : {'PASS' if ok else 'FAIL'}  iters={res_off.iterations}  "
          f"overuse={res_off.overuse_curve}  gap rows {gap_rows(res_off)} -> "
          f"{gap_rows(res_on)}  cost {cost_off:.1f} -> {cost_on:.1f}  "
          f"gain={res_on.seconds.get('refine_gain_pct', 0.0):.1f}%  "
          f"failed={res_off.failed + res_on.failed}")
    return ok


def test_asym_yield():
    """The keeper-blindness deadlock (Voxy nets 80/82 at via_cost 12,
    icebreaker-bitsy /FLASH_~{CS}, icebreaker-v1.0e /VCORE): net 2 is walled
    in so its ONLY route crosses one bridge node; net 1 (lower code, so
    always the keeper) crosses the same node cheaply but has a
    moderately-priced dodge. Asymmetric rip-up never re-evaluates the keeper,
    so net 2 bounces off it every iteration, and the streak>=4 full rip only
    makes both flee then both return (with both ripped, kept_usage prices
    nobody). hist_weight is 0 here to mirror the boards: there the loser's
    alternative was itself priced out, so hist could never brute-force the
    separation — in this toy a nonzero hist eventually would, hiding the bug.
    Keeper alternation (patience 3) must hand the bridge to net 2 for one
    round, making net 1 finally price kept_usage and take its dodge."""
    ring = [(9, 9), (11, 9), (9, 11), (11, 11), (8, 10), (12, 10)]
    kw = dict(hist_weight=0.0, present_factor=3.0)
    lat = build_lattice(W, H, 1, blocked=frozenset(y * W + x for x, y in ring),
                        directions="both")
    B = 10 * W + 10                                  # the bridge, (10, 10)
    net_pads = {
        1: [_pad(lat, 10, 0), _pad(lat, 10, 19)],    # column 10, through B;
                                                     # dodge via col 7/13: +6
        2: [_pad(lat, 9, 10), _pad(lat, 11, 10)],    # penned: B is the only way
    }
    # Premise, fix disabled: the bridge pins at overuse exactly 1 to the
    # max_iters cap and greedy legalization fails net 2 with the exact
    # contested-with-the-keeper signature seen on all three boards.
    res_off = route_lattice(lat, net_pads, keeper_patience=10**9, **kw)
    pinned = (res_off.iterations == 40
              and all(v == 1 for v in res_off.overuse_curve))
    sig = (len(res_off.failed) == 1 and res_off.failed[0][0] == 2
           and "contested with net 1" in res_off.failed[0][1])
    # Fix, default patience: ownership transfers at iteration 3, net 1 pays
    # for its own alternative, both nets route well under max_iters.
    res_on = route_lattice(lat, net_pads, **kw)
    n1, n2 = _net_nodes(res_on, 1), _net_nodes(res_on, 2)
    ok = (pinned and sig
          and not res_on.failed and res_on.overuse_curve[-1] == 0
          and B in n2 and B not in n1
          and res_on.iterations <= 8)
    print(f"ASYM-YIELD : {'PASS' if ok else 'FAIL'}  "
          f"no-fix iters={res_off.iterations}  "
          f"curve={res_off.overuse_curve}  failed={res_off.failed}")
    print(f"             fix iters={res_on.iterations}  "
          f"curve={res_on.overuse_curve}  net1 dodges B={B not in n1}  "
          f"net2 holds B={B in n2}  failed={res_on.failed}")
    return ok


def test_multiparty():
    """The MULTI-PARTY standoff (icebreaker-v1.0e nets 95/96 vs net 3): two
    nets each deadlocked against the SAME bigger net, so pairwise role swaps
    cycle without global progress. Wall on x=10, open only y=8..12. Pen net 1
    pads sit ON the wall at (10,8)/(10,12): its straight route needs ALL of
    (10,9..11). Crosser net 2 (4,9)->(19,10) can cross only at (10,9)/(10,10);
    crosser net 3 (4,10)->(19,11) only at (10,10)/(10,11). The only crossing
    of rows 9-11 clear of the crossers' spans is the west strip x<=3 — a +14
    detour the pen must take, but as the keeper at every contested node it
    never re-evaluates: yields at one node just move the conflict to the
    neighbor (the contested set WANDERS, so per-node keeper_hold and streak
    never mature), and the streak>=4 full rip co-rips everyone into an
    identical re-collision. hist_weight 0 / constant present mirror the
    boards, where every alternative is priced out (see ASYM-YIELD). Only the
    net-level clique detection sees the 3-party component and hands it to
    _clique_resolve: firing 1 (pen first, most direct) hard-blocks both
    crossers, whose seq_fail bump puts them FIRST at firing 2 — crossers take
    (10,9)/(10,10), the pen is forced onto the detour, overuse hits 0."""
    blocked = frozenset(y * W + 10 for y in range(H) if not (8 <= y <= 12))
    kw = dict(hist_weight=0.0, present_factor=3.0, present_growth=1.0)
    lat = build_lattice(W, H, 1, blocked=blocked, directions="both")
    net_pads = {
        1: [_pad(lat, 10, 8), _pad(lat, 10, 12)],
        2: [_pad(lat, 4, 9), _pad(lat, 19, 10)],
        3: [_pad(lat, 4, 10), _pad(lat, 19, 11)],
    }
    # Premise, fix disabled: overuse pins at a small constant to the
    # iteration cap and BOTH crossers fail contested with the pen.
    res_off = route_lattice(lat, net_pads, clique_patience=10**9, **kw)
    pinned = (res_off.iterations == 40
              and all(v > 0 for v in res_off.overuse_curve))
    sig = (sorted(n for n, _ in res_off.failed) == [2, 3]
           and all("congestion unresolved" in r and "contested with net 1" in r
                   for _, r in res_off.failed))
    # Fix, default clique_patience: resolved well under the cap, pen detours.
    res_on = route_lattice(lat, net_pads, **kw)
    n1 = _net_nodes(res_on, 1)
    cross2 = {lat.coords(v) for v in _net_nodes(res_on, 2)
              if lat.coords(v)[0] == 10}
    cross3 = {lat.coords(v) for v in _net_nodes(res_on, 3)
              if lat.coords(v)[0] == 10}
    ok = (pinned and sig
          and not res_on.failed and res_on.overuse_curve[-1] == 0
          and res_on.iterations < 40
          and cross2 and cross3 and not (cross2 & cross3)
          and not (n1 & (_net_nodes(res_on, 2) | _net_nodes(res_on, 3))))
    print(f"MULTIPARTY : {'PASS' if ok else 'FAIL'}  "
          f"no-fix iters={res_off.iterations}  "
          f"curve={res_off.overuse_curve}  failed={res_off.failed}")
    print(f"             fix iters={res_on.iterations}  "
          f"curve={res_on.overuse_curve}  crossings net2={sorted(cross2)} "
          f"net3={sorted(cross3)}  failed={res_on.failed}")
    return ok


def test_clearance():
    """The clearance structure end-to-end on a hand lattice (20x20x1, both):
    (1) a foreign ring blob detours the net that doesn't own it and is
    transparent to the net that does; (2) a full -1 ring wall with BOTH a free
    gap and a nearer soft corridor: the soft price must steer the route to the
    free gap (and refine, which prices soft too, must not pull it back);
    (3) with only the soft corridor, the net crosses it and the crossing is
    counted; (4) with no opening at all the wall is hard: target unreachable."""
    from lattice import Clearance

    lat = build_lattice(W, H, 1, directions="both")
    row10 = {1: [_pad(lat, 0, 10), _pad(lat, 19, 10)]}

    # (1) foreign blob at x=10, y=8..12 claimed by net 2.
    blob = {lat.node(10, y, 0): 2 for y in range(8, 13)}
    res_f = route_lattice(lat, row10, clearance=Clearance(node_net=dict(blob)))
    n1 = _net_nodes(res_f, 1)
    detoured = not res_f.failed and not (n1 & set(blob))
    res_o = route_lattice(lat, {2: [_pad(lat, 0, 10), _pad(lat, 19, 10)]},
                          clearance=Clearance(node_net=dict(blob)))
    straight = not res_o.failed and lat.node(10, 10, 0) in _net_nodes(res_o, 2)
    ok1 = (detoured and straight
           and res_f.clearance_stats["soft_crossings"] == 0)

    # (2)/(3)/(4) the wall. Free gap y=15 (detour 10 penalized steps), soft
    # corridor y=8 (detour 4): without the soft price y=8 wins, with a fat
    # price (20) the free gap must win.
    wall = {lat.node(10, y, 0): -1 for y in range(H)}
    wall_gap = {n: c for n, c in wall.items() if n != lat.node(10, 15, 0)}
    soft = {1: {lat.node(10, 8, 0)}}
    res_g = route_lattice(lat, row10, clearance=Clearance(
        node_net=wall_gap, soft_allow={1: set(soft[1])}),
        clearance_soft_cost=20.0)
    cross_g = {lat.coords(v) for v in _net_nodes(res_g, 1)
               if lat.coords(v)[0] == 10}
    ok2 = (not res_g.failed and cross_g == {(10, 15, 0)}
           and res_g.clearance_stats["soft_crossings"] == 0)

    res_s = route_lattice(lat, row10, clearance=Clearance(
        node_net=dict(wall), soft_allow={1: set(soft[1])}),
        clearance_soft_cost=20.0)
    cross_s = {lat.coords(v) for v in _net_nodes(res_s, 1)
               if lat.coords(v)[0] == 10}
    ok3 = (not res_s.failed and cross_s == {(10, 8, 0)}
           and res_s.clearance_stats["soft_crossings"] == 1)

    res_w = route_lattice(lat, row10, clearance=Clearance(node_net=dict(wall)))
    ok4 = (len(res_w.failed) == 1
           and "target unreachable" in res_w.failed[0][1])

    ok = ok1 and ok2 and ok3 and ok4
    print(f"CLEARANCE  : {'PASS' if ok else 'FAIL'}  "
          f"blob detour={detoured} own-ring straight={straight}  "
          f"gap-vs-soft crossing={sorted(cross_g)}  "
          f"soft-only crossing={sorted(cross_s)} "
          f"(counted={res_s.clearance_stats['soft_crossings']})  "
          f"walled failed={res_w.failed}")
    return ok


if __name__ == "__main__":
    results = [test_cross(), test_corridor(), test_starvation(), test_tree(),
               test_eqdelta(), test_meander(), test_asym_yield(),
               test_multiparty(), test_clearance()]
    print(f"RESULT: {'PASS' if all(results) else 'FAIL'} "
          f"({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)
