"""Tests for terminal-served nets (terminal.py + pathfinder star connectivity).

A terminal-served net is joined by WIRE: its pads cluster, each cluster gets a
solderable via terminal, and every pad routes to its NEAREST terminal only —
no on-board copper runs terminal-to-terminal (the flying lead does). These
cases pin that behaviour: 3 well-separated clusters -> 3 terminals, star (not
tree) connectivity, a single cluster -> 1 terminal, determinism, and a normal
net left bit-for-bit unchanged.

Run: .venv/bin/python test_terminal.py
"""
import os

from board import Board, Pad
from lattice import lattice_for_board
from pathfinder import (build_connections, net_pads_for_board, route_lattice)
import terminal as term_mod


def _pad(x, y, code, name, layer="F.Cu"):
    return Pad(x_mm=x, y_mm=y, layers=[layer], net_code=code, net_name=name,
               width_mm=1.0, height_mm=1.0, through_hole=False, drill_mm=0.0,
               rotation_deg=0.0)


def _board(pads, w=60.0, h=60.0):
    return Board(path="/dev/null/synthetic.kicad_pcb", origin_mm=(0.0, 0.0),
                 size_mm=(w, h), copper_layers=["F.Cu", "B.Cu"],
                 nets={0: "", 5: "HVNET", 6: "SIG"}, pads=pads, tracks=[],
                 vias=[], outline_regions=())


# Three well-separated clusters of net 5, plus a normal 2-pad net 6.
CLUSTER_A = [(6, 6), (8, 6), (6, 8)]
CLUSTER_B = [(52, 6), (54, 6)]
CLUSTER_C = [(6, 52), (6, 54)]
HV_PADS = CLUSTER_A + CLUSTER_B + CLUSTER_C


def _hv_board():
    pads = [_pad(x, y, 5, "HVNET") for x, y in HV_PADS]
    pads += [_pad(20, 30, 6, "SIG"), _pad(40, 30, 6, "SIG")]
    return _board(pads)


def test_cluster():
    """3 separated groups -> 3 clusters; one tight group -> 1; deterministic
    and independent of input order."""
    three = term_mod.cluster_pads(HV_PADS, 15.0)
    one = term_mod.cluster_pads(CLUSTER_A, 15.0)
    # Input-order independence: shuffle and re-cluster, same partition.
    shuffled = HV_PADS[::-1]
    three_s = term_mod.cluster_pads(shuffled, 15.0)

    def as_point_sets(clusters, centers):
        return sorted(tuple(sorted(centers[i] for i in c)) for c in clusters)

    ok = (len(three) == 3 and len(one) == 1
          and sorted(len(c) for c in three) == [2, 2, 3]
          and as_point_sets(three, HV_PADS) == as_point_sets(three_s, shuffled)
          # determinism
          and term_mod.cluster_pads(HV_PADS, 15.0) == three)
    print(f"CLUSTER    : {'PASS' if ok else 'FAIL'}  "
          f"3-group->{len(three)} clusters sizes "
          f"{sorted(len(c) for c in three)}  1-group->{len(one)}")
    return ok


def test_star_connectivity():
    """build_connections with terminal_nets makes pad->nearest-terminal edges,
    never terminal-to-terminal, one edge per pad."""
    brd = _hv_board()
    lat, node_owner, _pn = None, None, None
    lat, pad_nodes, node_owner = lattice_for_board(brd, 1.0)
    net_pads = net_pads_for_board(brd, lat, node_owner)
    plan = term_mod.plan_terminals(brd, lat, node_owner, None, {5},
                                   cluster_mm=15.0)
    terms = plan.terminals[5]
    terminal_conn = {5: [(t.nodes, (t.x_mm, t.y_mm)) for t in terms]}
    term_node_sets = {frozenset(t.nodes) for t in terms}

    conns, _confl, _claim = build_connections(net_pads, terminal_conn)
    hv = [c for c in conns if c.net == 5]
    sig = [c for c in conns if c.net == 6]

    # one edge per HV pad, every edge pad -> a terminal, no edge terminal->term.
    each_to_terminal = all(frozenset(c.b_nodes) in term_node_sets for c in hv)
    no_term_source = all(frozenset(c.a_nodes) not in term_node_sets for c in hv)
    # nearest-terminal assignment: recompute and compare
    def nearest(center):
        return min(range(len(terms)),
                   key=lambda k: abs(terms[k].x_mm - center[0]) +
                   abs(terms[k].y_mm - center[1]))
    # map each hv conn's pad center to its terminal index
    correct_assign = True
    for c in hv:
        # a_nodes is a pad; find its center via net_pads
        center = next(e[1] for e in net_pads[5]
                      if frozenset(e[0]) == frozenset(c.a_nodes))
        if frozenset(terms[nearest(center)].nodes) != frozenset(c.b_nodes):
            correct_assign = False

    ok = (len(terms) == 3 and len(hv) == len(HV_PADS)
          and each_to_terminal and no_term_source and correct_assign
          # normal net still an MST (2 pads -> 1 edge)
          and len(sig) == 1)
    print(f"STAR       : {'PASS' if ok else 'FAIL'}  {len(terms)} terminals  "
          f"{len(hv)} pad->terminal edges (pads={len(HV_PADS)})  "
          f"terminal-as-source={not no_term_source}  assign_ok={correct_assign}")
    return ok


def test_plan_and_route():
    """Full route: 3-cluster HV net drops 3 terminals and every pad reaches
    one; the net counts as fully routed and no track joins two terminals."""
    brd = _hv_board()
    lat, pad_nodes, node_owner = lattice_for_board(brd, 1.0)
    net_pads = net_pads_for_board(brd, lat, node_owner)
    plan = term_mod.plan_terminals(brd, lat, node_owner, None, {5},
                                   cluster_mm=15.0)
    terms = plan.terminals[5]
    terminal_conn = {5: [(t.nodes, (t.x_mm, t.y_mm)) for t in terms]}
    for t in terms:
        for n in t.claim:
            node_owner.setdefault(n, 5)

    res = route_lattice(lat, net_pads, node_owner, terminal_nets=terminal_conn)
    failed_codes = {c for c, _ in res.failed}

    # every HV pad's node lands in the net's routed node set, and each pad is
    # connected (its path reaches its terminal). Fully routed == no HV failure.
    hv_routed = 5 in res.net_paths and 5 not in failed_codes
    # terminal node reached by at least one path per terminal (all pads served)
    routed_nodes = set()
    for p in res.net_paths.get(5, []):
        routed_nodes.update(p)
    terminals_reached = all(any(n in routed_nodes for n in t.nodes)
                            for t in terms)
    # normal net routed too
    sig_ok = 6 in res.net_paths and 6 not in failed_codes

    ok = (len(terms) == 3 and hv_routed and terminals_reached and sig_ok
          and not res.conflicts)
    print(f"ROUTE      : {'PASS' if ok else 'FAIL'}  terminals={len(terms)}  "
          f"HV fully routed={hv_routed}  all terminals reached="
          f"{terminals_reached}  sig routed={sig_ok}  failed={res.failed}")
    return ok


def test_single_cluster():
    """A net whose pads are all within one cluster gets exactly one terminal."""
    pads = [_pad(x, y, 5, "HVNET") for x, y in CLUSTER_A]
    brd = _board(pads, w=40, h=40)
    lat, pad_nodes, node_owner = lattice_for_board(brd, 1.0)
    plan = term_mod.plan_terminals(brd, lat, node_owner, None, {5},
                                   cluster_mm=25.0)
    terms = plan.terminals.get(5, [])
    ok = len(terms) == 1 and terms[0].cluster_pads == 3
    print(f"SINGLE     : {'PASS' if ok else 'FAIL'}  terminals={len(terms)}  "
          f"serves {terms[0].cluster_pads if terms else 0} pads")
    return ok


def test_determinism():
    """Identical calls return identical terminals (coords + nodes)."""
    brd = _hv_board()
    lat, pad_nodes, node_owner = lattice_for_board(brd, 1.0)
    p1 = term_mod.plan_terminals(brd, lat, dict(node_owner), None, {5},
                                 cluster_mm=15.0)
    p2 = term_mod.plan_terminals(brd, lat, dict(node_owner), None, {5},
                                 cluster_mm=15.0)

    def sig(plan):
        return [(t.net_code, round(t.x_mm, 6), round(t.y_mm, 6), t.nodes)
                for t in plan.terminals[5]]

    ok = sig(p1) == sig(p2)
    print(f"DETERMINISM: {'PASS' if ok else 'FAIL'}  terminals={sig(p1)}")
    return ok


def test_normal_unchanged():
    """A non-terminal net is bit-for-bit identical with or without the
    terminal_nets argument — connections and routed paths."""
    pads = [_pad(20, 30, 6, "SIG"), _pad(40, 30, 6, "SIG")]
    brd = _board(pads)
    lat, pad_nodes, node_owner = lattice_for_board(brd, 1.0)
    net_pads = net_pads_for_board(brd, lat, node_owner)

    def as_tuples(conns):
        return sorted((c.net, tuple(c.a_nodes), tuple(c.b_nodes))
                      for c in conns)

    c_none, _cf0, cl0 = build_connections(net_pads)
    c_empty, _cf1, cl1 = build_connections(net_pads, {})
    c_other, _cf2, cl2 = build_connections(net_pads, {5: [((0, 1), (0, 0))]})
    conns_same = (as_tuples(c_none) == as_tuples(c_empty) == as_tuples(c_other))
    claim_same = (cl0 == cl1 == cl2)

    r0 = route_lattice(lat, net_pads, node_owner)
    r1 = route_lattice(lat, net_pads, node_owner, terminal_nets={})
    paths_same = r0.net_paths == r1.net_paths

    ok = conns_same and claim_same and paths_same
    print(f"NORMAL     : {'PASS' if ok else 'FAIL'}  conns_identical="
          f"{conns_same}  claim_identical={claim_same}  "
          f"paths_identical={paths_same}")
    return ok


def test_emit():
    """write_routed_copy appends each terminal as a (via) carrying its net with
    its own large drill. Uses the KiCad-5 pico-vga fixture if present."""
    from writeback import write_routed_copy
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "bench", "boards", "rpi-pico-vga",
                           "pico_vga_sd_aud.kicad_pcb")
    if not os.path.isfile(fixture):
        print("EMIT       : SKIP  (bench/boards fixture absent)")
        return True
    from board import load_board
    brd = load_board(fixture)
    # pick a real net so net_attr resolves
    code = next(c for c in brd.nets if c > 0)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out",
                       "terminal-emit-test.kicad_pcb")
    terminals = [(50.0, 40.0, code, 2.0, 1.0)]
    write_routed_copy(fixture, out, [], [], brd.nets, terminals=terminals)
    with open(out, encoding="utf-8") as f:
        text = f.read()
    # the via must be present, with the big drill and the net
    ok = ("(drill 1)" in text and "(size 2)" in text
          and text.count("(via") >= 1)
    # confirm it round-trips through the parser as one more via on that net
    brd2 = load_board(out)
    added = [v for v in brd2.vias if abs(v.x_mm - 50.0) < 1e-6
             and abs(v.y_mm - 40.0) < 1e-6]
    reparses = len(added) == 1 and added[0].drill_mm == 1.0 \
        and added[0].size_mm == 2.0 and added[0].net_code == code
    ok = ok and reparses
    print(f"EMIT       : {'PASS' if ok else 'FAIL'}  via in text={ok}  "
          f"reparsed drill/size/net ok={reparses}")
    return ok


if __name__ == "__main__":
    # Cheap, pure-Python cases first (clustering, star connectivity, planning),
    # then the ones that spin up the MLX router, then emission (fixture-gated).
    results = [
        test_cluster(),
        test_star_connectivity(),
        test_single_cluster(),
        test_determinism(),
        test_normal_unchanged(),
        test_plan_and_route(),
        test_emit(),
    ]
    ok = all(results)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}  "
          f"({sum(1 for r in results if not r)} failed)")
    raise SystemExit(0 if ok else 1)
