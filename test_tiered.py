"""Priority-tier routing (pathfinder.route_tiered): route the most-constrained
net class first, chaining each finished tier's copper + clearance halo into
node_owner so a later, less-constrained class treats it as a hard obstacle.

This is how a wide HV-creepage class and a fine logic class coexist — one
simultaneous pass has the wide halos and the fine tracks fight over the same
nodes; routed in descending-clearance order the wide copper claims its creepage
first and the fine copper fills around it. The properties that matter:

- route_lattice exposes RouteResult.claimed = each net's path + halo footprint;
- route_tiered runs tiers most-constrained-first and chains that footprint, so a
  later tier lands a full halo clear of an earlier tier's copper WITHOUT the two
  ever being in the same negotiation;
- ORDER matters: the same nets routed least-constrained-first cannot give the HV
  net its halo — proof the descending-clearance order is load-bearing.

Run: .venv/bin/python test_tiered.py
"""
from lattice import build_lattice
from pathfinder import route_lattice, route_tiered

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


PITCH = 0.6
EXC = {1: (0.0, 1.10), 2: (0.0, 0.25), 3: (0.0, 0.25)}   # net -> (via, track) halo
TIER = {1: 1.0, 2: 0.15, 3: 0.15}                         # net -> clearance mm


def _fixture(W=24, H=9):
    lat = build_lattice(W, H, 1, pitch_mm=PITCH, directions="both")

    def pad(x, y):
        return ((lat.node(x, y, 0),), (x * PITCH, y * PITCH))

    # HV net 1 down the centre; two logic nets that want the adjacent lanes.
    net_pads = {1: [pad(1, 4), pad(W - 2, 4)],
                2: [pad(1, 5), pad(W - 2, 5)],
                3: [pad(1, 3), pad(W - 2, 3)]}
    pads = {n for pl in net_pads.values() for nodes, _ in pl for n in nodes}
    return lat, net_pads, pads


def _rows(lat, res, net, pads):
    return {(lat.coords(v)[0], lat.coords(v)[1])
            for p in res.net_paths.get(net, []) for v in p if v not in pads}


def _gap(lat, res, na, nb, pads):
    a, b = _rows(lat, res, na, pads), _rows(lat, res, nb, pads)
    return min((abs(ay - by) for ax, ay in a for bx, by in b if ax == bx),
               default=99)


if __name__ == "__main__":
    print("=== route_lattice exposes the claimed (path + halo) footprint ===")
    lat, net_pads, pads = _fixture()
    hv_only = route_lattice(lat, {1: net_pads[1]}, refine_passes=0,
                            net_exclusion={1: EXC[1]})
    path_nodes = {v for p in hv_only.net_paths[1] for v in p}
    check(hv_only.claimed is not None and 1 in hv_only.claimed,
          "RouteResult.claimed is populated per routed net")
    check(path_nodes <= hv_only.claimed[1],
          "claimed[net] contains the net's own path nodes")
    check(len(hv_only.claimed[1]) > len(path_nodes),
          f"claimed[net] is STRICTLY bigger than the path — it carries the "
          f"clearance halo ({len(hv_only.claimed[1])} vs {len(path_nodes)} nodes)")

    print("=== route_tiered: most-constrained first, halo respected via chaining ===")
    res = route_tiered(lat, net_pads, TIER, net_exclusion=EXC, refine_passes=0)
    check(not res.failed and len(res.net_paths) == 3,
          f"all three nets route through the tiers ({res.failed})")
    check([t["clearance_mm"] for t in res.tiers] == [1.0, 0.15],
          f"tiers run MOST-CONSTRAINED first (1.0 then 0.15) ({res.tiers})")
    check(res.tiers[0]["nets"] == 1 and res.tiers[1]["nets"] == 2,
          "the HV tier has 1 net, the logic tier has 2")
    g2 = _gap(lat, res, 1, 2, pads)
    g3 = _gap(lat, res, 1, 3, pads)
    check(g2 >= 2 and g3 >= 2,
          f"BOTH logic nets land a full HV halo clear of the HV net — the "
          f"inter-tier clearance the chaining enforces ({g2}, {g3} grid steps = "
          f"{g2 * PITCH:.2f} mm, HV wants >= {EXC[1][1]:.2f})")
    check(res.claimed is not None and 1 in res.claimed,
          "the merged result carries claimed for a further tiering pass")
    # the logic paths avoid the HV net's committed footprint entirely (chaining):
    hv_foot = res.claimed[1]
    logic_nodes = {v for n in (2, 3) for p in res.net_paths[n] for v in p
                   if v not in pads}
    check(logic_nodes.isdisjoint(hv_foot),
          "no logic copper sits in the HV net's claimed halo — the later tier "
          "saw it as a hard obstacle, not a priced one")

    print("=== ORDER is load-bearing: least-constrained first cannot place HV ===")
    # Route the SAME nets logic-first (wrong order): the logic nets take the
    # centre lanes with no HV halo reserved, so the HV net can no longer keep a
    # full halo from them — it either fails or runs closer than its class allows.
    wrong = route_tiered(lat, net_pads, {1: 0.15, 2: 1.0, 3: 1.0},
                         net_exclusion=EXC, refine_passes=0)
    gw = min(_gap(lat, wrong, 1, 2, pads), _gap(lat, wrong, 1, 3, pads)) \
        if not any(f[0] == 1 for f in wrong.failed) else 0
    check(gw < 2 or any(f[0] == 1 for f in wrong.failed),
          f"routing HV LAST leaves it under its own halo or unrouted "
          f"(gap {gw} steps) — most-constrained-first is why tiering works")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
