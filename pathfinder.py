"""L3: PathFinder negotiated congestion — route every net legally, in parallel.

Classic PathFinder (McMurchie/Ebeling) reshaped for the batched GPU wavefront:
every ripped connection of an iteration routes against ONE shared cost snapshot
(hist + present * kept-usage), <=128 planes per batched_sssp launch, then usage
is re-tallied and overused nodes rip their tenants for the next round. Sharing
one snapshot trades a little convergence sharpness for using the (N,B) kernel
the way it was built to be used.

Choices that matter:
- Node capacity 1, counted per NET, not per path: a net revisiting its own
  nodes is a tree, not a conflict. Own-tree reuse is made FREE during search by
  seeding each plane's sources with the net's kept-path nodes reachable (via
  kept paths) from the a-side pad — side-aware, because seeding both sides'
  components would let a ripped connection terminate without joining them.
  With reuse free at the source set, the single shared cost vector needs no
  per-plane own-net exclusion.
- Hard legality is pad ownership (node_owner) plus, when a lattice.Clearance
  is supplied, the inflated clearance rings and board-edge band: per-net
  uint8 masks built lazily and cached, with every plane's own
  sources/targets force-cleared — a pad can snap inside another pad's
  claimed rectangle. Ring corridors between pads too close for any legal
  lane degrade to a static soft price (see route_lattice's `clearance`).
- If max_iters exhausts with overuse left, a final first-come greedy pass keeps
  one net per contested node and fails the rest: the returned result is always
  legal, never optimistic.
- When negotiation ends at overuse 0, a refinement pass (_refine) reroutes
  every connection against the FINISHED board — other nets' paths as hard
  obstacles, no hist/present pricing — and adopts strictly-shorter results.
  Negotiation prices nodes by their history, so a net routed late detours
  around congestion that no longer exists; refine recovers that slack and is
  built so it can never trade legality for it.
- paths_to_tracks dedupes same-net lattice edges/vias across paths before
  merging collinear runs: a shared trunk emits its copper once.
- Emission smooths 90-degree staircases into 45-degree segments (smooth.py)
  against an occupancy map of every routed node plus pad ownership — corners
  are cut only where the diagonal clips no foreign copper. The smoothed
  geometry lands on RouteResult.tracks/.vias; net_paths stays raw and
  paths_to_tracks stays the exact-lattice fallback.

CLI: python pathfinder.py BOARD.kicad_pcb [--pitch 1.0] [--layers F.Cu,B.Cu]
     [--svg out.svg] [--no-refine] [--no-smooth]
"""
import math
import time
from dataclasses import dataclass

import numpy as np

from backtrace import extract_path


@dataclass
class RouteResult:
    net_paths: dict     # net_code -> list of paths, each path a list of lattice node ids
    failed: list        # (net_code, reason_string) for connections never legally routed
    conflicts: list     # (node, [net_codes]) pad-snap collisions detected before routing
    iterations: int
    overuse_curve: list # total overused-node count after each iteration
    wirelength_mm: float
    via_count: int
    seconds: dict       # stage name -> wall seconds
    tracks: list = None # smoothed (x1, y1, x2, y2, layer, net) segments (45s
                        # allowed); None -> consumers use paths_to_tracks
    vias: list = None   # (x_mm, y_mm, net) matching tracks; None as above
    clearance_stats: dict = None  # ring/edge/degrade counters when a
                                  # lattice.Clearance was in force; else None
    via_stats: dict = None        # via-exclusion halo counters, or None
    geometry: object = None       # geometry.CopperGeometry in force, or None
    geometry_note: str = None   # its one-line summary (the contract, printed
                                # every run — the tool states its own limits)
    geometry_warnings: list = None   # loud numeric complaints, may be empty


@dataclass(eq=False)  # identity hash: connections are keyed by instance
class _Conn:
    net: int
    a_nodes: tuple
    b_nodes: tuple
    path: list = None
    reason: str = None


def _mst_edges(centers):
    """Prim over pad centers, Manhattan mm. Returns (i, j) index pairs."""
    P = len(centers)
    cx = np.array([c[0] for c in centers], dtype=np.float64)
    cy = np.array([c[1] for c in centers], dtype=np.float64)
    in_tree = np.zeros(P, dtype=bool)
    in_tree[0] = True
    d = np.abs(cx - cx[0]) + np.abs(cy - cy[0])
    parent = np.zeros(P, dtype=np.int64)
    edges = []
    for _ in range(P - 1):
        j = int(np.argmin(np.where(in_tree, np.inf, d)))
        edges.append((int(parent[j]), j))
        in_tree[j] = True
        nd = np.abs(cx - cx[j]) + np.abs(cy - cy[j])
        upd = (~in_tree) & (nd < d)
        d[upd] = nd[upd]
        parent[upd] = j
    return edges


def build_connections(net_pads):
    """net_pads: net_code -> [(nodes, (x_mm, y_mm)), ...], one entry per pad.

    A through-hole pad's `nodes` is its snapped node on every lattice layer;
    an SMD pad's is a single node. Returns (connections, conflicts, claim):
    one connection per MST edge over the net's distinct pads (Manhattan mm
    between pad centers). Pads with identical node sets are collapsed. A pad
    whose nodes collide with an earlier net's claim is excluded from its net
    (its connections never form) and reported in conflicts as
    (node, [net_codes]). `claim` (node -> net, lowest net wins) is THE pad
    arbitration — callers must not let a second rule disagree with it.
    """
    claim = {}
    clashes = {}
    conns = []
    for net in sorted(net_pads):
        if net <= 0:
            continue
        pads, seen = [], set()
        for nodes, center in net_pads[net]:
            key = frozenset(nodes)
            if key in seen:
                continue
            hit = [n for n in nodes if claim.get(n, net) != net]
            if hit:
                for n in hit:
                    clashes.setdefault(n, [claim[n]]).append(net)
                continue
            seen.add(key)
            for n in nodes:
                claim.setdefault(n, net)
            pads.append((tuple(int(n) for n in nodes), center))
        if len(pads) >= 2:
            for i, j in _mst_edges([c for _, c in pads]):
                conns.append(_Conn(net, pads[i][0], pads[j][0]))
    conflicts = [(n, nets) for n, nets in sorted(clashes.items())]
    return conns, conflicts, claim


def _via_halo(lat, via_exclusion_mm):
    """Planar node offsets a via claims, or None when via exclusion is off.

    A via is copper of via_size diameter punched through EVERY layer. Nothing
    in the lattice says so: a layer-change edge costs via_cost and the node it
    lands on is one point. So the router claims a NEIGHBOURHOOD around it —
    every node within geometry.CopperGeometry.via_exclusion_mm, on every
    layer — as used by the via's net, and the existing capacity-1 negotiation
    does the rest. No new kernel concept: a via becomes a mini pad ring that
    appears and moves with the path that owns it.
    """
    from geometry import halo_offsets
    if not via_exclusion_mm or via_exclusion_mm <= lat.pitch_mm / 2.0:
        return None      # claims nothing beyond the via's own node
    offs = halo_offsets(lat.pitch_mm, via_exclusion_mm)
    return offs if len(offs) > 1 else None


def _footprint(lat, path, halo, exempt=frozenset()):
    """The nodes a path OCCUPIES for conflict accounting: its own nodes, plus
    (when halo is on) every halo node on every layer around each of its
    layer changes. Deliberately NOT the same thing as the path: own-tree
    seeding and _net_stays_connected reason about the path proper, because a
    connection may only terminate on real copper, never on a halo node.

    `exempt` is the PAD node set, and it is a deliberate scope line rather
    than an optimization. A pad cannot move. If a via's halo could claim a
    foreign pad's node, that pad's net becomes permanently overused with no
    alternative and fails outright — the halo would be trading a clearance
    violation for an unroutable net, which is a worse answer, not a better
    one. Via-to-PAD spacing is the pad ring model's job (lattice.Clearance);
    its inflate is clearance + track_width/2, which under-serves a via by
    (via_size - track_width)/2 — a stated residual, not a silent one. The
    halo's remit is via-vs-ROUTED-copper, which is where the measured gap is.
    """
    nodes = set(path)
    if not halo:
        return nodes
    W, H, L = lat.W, lat.H, lat.L
    plane = W * H
    for a, b in zip(path, path[1:]):
        ax, ay, al = lat.coords(a)
        if al == lat.coords(b)[2]:
            continue
        for dx, dy in halo:
            x, y = ax + dx, ay + dy
            if 0 <= x < W and 0 <= y < H:
                base = y * W + x
                for il in range(L):
                    nd = il * plane + base
                    if nd not in exempt:
                        nodes.add(nd)
    nodes.update(path)     # a path's own nodes are never exempt
    return nodes


def _own_tree_seed(a_nodes, kept_sets):
    """Side-aware source seeding: the a-side pad nodes plus every kept-path
    component of the net reachable (via kept paths) from them. Seeding only
    the a-side keeps a connection from terminating without joining its two
    sides; seeding the whole reachable component makes own-tree reuse free."""
    seed = set(a_nodes)
    pending = list(kept_sets)
    changed = True
    while changed:
        changed, rest = False, []
        for s in pending:
            if s & seed:
                seed |= s
                changed = True
            else:
                rest.append(s)
        pending = rest
    return seed


def route_lattice(lat, net_pads, node_owner=None, extra_allow=None,
                  hist_weight=0.5, present_factor=1.0, present_growth=1.4,
                  max_iters=40, batch_size=128, max_rounds=100_000,
                  refine_passes=2, smooth=True, keeper_patience=3,
                  clique_patience=8, clearance=None, clearance_soft_cost=8.0,
                  via_exclusion_mm=0.0, allow_diagonals=True):
    """Negotiation loop over a built Lattice. net_pads as in build_connections.

    extra_allow (net -> node ids, lattice.pad_overlap_allowances) punches
    per-net holes in the ownership masks where the INPUT board already
    overlaps different-net pad copper — crossing there creates no short the
    fab won't. Without it, a pad nested inside a bigger pad's copper is
    walled in and its net can never route.

    refine_passes: post-negotiation slack-recovery reroutes (_refine), run
    only when negotiation ended at overuse 0. 0 disables.

    smooth: emit legality-checked 45-degree geometry (smooth.py) onto
    RouteResult.tracks/.vias; wirelength_mm/via_count then measure the
    smoothed copper. False leaves them None (raw paths_to_tracks only).

    keeper_patience: consecutive overused iterations a node may have the
    SAME keeper net before that keeper yields the node for one round (see
    the alternation comment in the negotiation loop). A huge value
    effectively disables alternation.

    clique_patience: iterations a MULTI-PARTY standoff (a connected component
    of >= 3 nets in the windowed net-conflict graph, every core net in
    conflict that many iterations running) may persist before its
    connections are ripped and rerouted SEQUENTIALLY against hard obstacles
    (_clique_resolve). A huge value disables it.

    clearance: optional lattice.Clearance. Its node_net claims join the
    per-net hard masks (a node claimed by a FOREIGN net or by -1 is blocked),
    with three escapes: the net's own ring is free (a), extra_allow grants
    and Clearance.free_allow punch through (b), and Clearance.soft_allow
    nodes are passable at clearance_soft_cost each (c) — a STATIC per-node
    price added to the negotiation cost snapshot and honored by _refine and
    _clique_resolve, so post-passes cannot silently undo what negotiation
    paid to respect. None (the default, and what every hand-built test uses)
    keeps behavior bit-identical to the pre-clearance router.

    via_exclusion_mm: radius a VIA claims around itself, on every layer, in
    the usage/overuse accounting (geometry.CopperGeometry.via_exclusion_mm =
    via_size/2 + track_width/2 + clearance). A via is copper with a diameter;
    without this the lattice places 0.6 mm vias on a 0.5 mm grid beside
    foreign copper and DRC catches what the router did not. Enforced DURING
    negotiation rather than post-hoc precisely because vias are dynamic —
    they appear only where a path changes layer, so a static keep-out cannot
    know where they are, and a post-hoc legalize could only delete copper the
    router had already committed to. Making the halo part of each net's
    footprint lets the EXISTING capacity-1 machinery price it, rip it and
    reroute around it. Because the claim is symmetric, via-to-via separation
    comes out stricter than strictly required (see
    CopperGeometry.via_via_enforced_mm) — that is a routability cost, and it
    is reported, not hidden. 0.0 (the default, and what every hand-built test
    uses) keeps behavior bit-identical to the pre-exclusion router.

    allow_diagonals: passed to smooth.smooth_net_paths. False refuses every
    45-degree cut (pitch too fine for a diagonal to clear a diagonally-
    adjacent node — geometry.CopperGeometry.diagonals_ok)."""
    import mlx.core as mx
    import wavefront

    sec = {"connect": 0.0, "sssp": 0.0, "backtrace": 0.0,
           "negotiate": 0.0, "emit": 0.0}
    t0 = time.perf_counter()
    conns, conflicts, claim = build_connections(net_pads)
    sec["connect"] = time.perf_counter() - t0

    N = lat.W * lat.H * lat.L
    rp, ci, wt = lat.to_mx()
    # One arbitration rule everywhere: build_connections' claims (lowest net
    # wins) override node_owner's last-pad-wins on overlapping rectangles,
    # else a net's mask can leave another net's claimed pad interior soft.
    node_owner = {**(node_owner or {}), **claim}
    if node_owner:
        own_nodes = np.fromiter(node_owner.keys(), dtype=np.int64, count=len(node_owner))
        own_nets = np.fromiter(node_owner.values(), dtype=np.int64, count=len(node_owner))
    else:
        own_nodes = own_nets = np.empty(0, dtype=np.int64)
    clr_nodes = clr_nets = np.empty(0, dtype=np.int64)
    soft_np = None
    if clearance is not None and clearance.node_net:
        clr_nodes = np.fromiter(clearance.node_net.keys(), dtype=np.int64,
                                count=len(clearance.node_net))
        clr_nets = np.fromiter(clearance.node_net.values(), dtype=np.int64,
                               count=len(clearance.node_net))
    if clearance is not None and clearance.soft_allow:
        soft_np = np.zeros(N, dtype=np.float32)
        for nodes in clearance.soft_allow.values():
            soft_np[np.fromiter(nodes, dtype=np.int64,
                                count=len(nodes))] = np.float32(clearance_soft_cost)
    masks = {}

    def net_mask(net):
        m = masks.get(net)
        if m is None:
            m = np.zeros(N, dtype=np.uint8)
            # Layering, weakest first: clearance rings block (own ring and -1
            # excepted), soft/free allowances re-open their nodes, pad
            # OWNERSHIP blocks regardless (rings and ownership are disjoint by
            # construction, but build_connections' snap claims are not), and
            # extra_allow grants override even ownership (input-board pad
            # overlaps — the long-standing rule).
            if clr_nodes.size:
                m[clr_nodes[clr_nets != net]] = 1
                for table in (clearance.soft_allow, clearance.free_allow):
                    opened = table.get(net)
                    if opened:
                        m[np.fromiter(opened, dtype=np.int64,
                                      count=len(opened))] = 0
            if own_nodes.size:
                m[own_nodes[own_nets != net]] = 1
            grant = (extra_allow or {}).get(net)
            if grant:
                m[np.fromiter(grant, dtype=np.int64, count=len(grant))] = 0
            masks[net] = m
        return m

    halo = _via_halo(lat, via_exclusion_mm)
    pad_exempt = frozenset(node_owner)   # includes build_connections' claims

    def foot(path):
        return _footprint(lat, path, halo, pad_exempt)

    hist = np.zeros(N, dtype=np.float32)
    streak = np.zeros(N, dtype=np.int32)  # consecutive iterations overused
    keeper_hold = {}  # node -> (keeper net, consecutive iterations as keeper)
    conf_streak = {}  # net -> consecutive iterations with any overused node
    edge_seen = {}    # (net_a, net_b) -> last iteration the pair shared a node
    seq_fail = {}     # id(conn) -> failures in _clique_resolve (order priority)
    present = float(present_factor)
    overuse_curve = []
    iterations = 0

    t_loop = time.perf_counter()
    for it in range(1, max_iters + 1):
        iterations = it
        # Two views of a kept path, and they are NOT interchangeable:
        # kept_by_net carries the path proper (own-tree seeding terminates on
        # real copper only), kept_foot carries the footprint (path + via
        # halos) that prices the board for everyone else.
        kept_by_net, kept_foot = {}, {}
        for c in conns:
            if c.path is not None:
                kept_by_net.setdefault(c.net, []).append(set(c.path))
                kept_foot.setdefault(c.net, set()).update(foot(c.path))
        kept_usage = np.zeros(N, dtype=np.float32)
        for net, nodes in kept_foot.items():
            kept_usage[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] += 1.0
        # A foreign via's HALO is a hard obstacle for this iteration's search,
        # not merely a priced one. Pricing alone gives the kernel no gradient
        # toward "put your copper where a via is not": the search cannot see
        # halos at all, so a ripped connection re-lands in one, gets ripped
        # again, and the overuse curve plateaus instead of falling (measured
        # on icebreaker-bitsy: stuck at ~700-1500 overused nodes through 120
        # iterations, 26 nets dropped). Blocking is safe because a halo is
        # small and dynamic — it moves with the via that owns it, so the next
        # iteration re-derives it rather than baking a permanent keep-out.
        halo_by_net = {}
        halo_all = np.zeros(N, dtype=np.uint8)
        if halo:
            for net, nodes in kept_foot.items():
                own_path = set()
                for s in kept_by_net.get(net, []):
                    own_path |= s
                ring = nodes - own_path
                if ring:
                    idx = np.fromiter(ring, dtype=np.int64, count=len(ring))
                    halo_by_net[net] = idx
                    halo_all[idx] = 1
        cost_np = hist + np.float32(present) * kept_usage
        if soft_np is not None:
            cost_np = cost_np + soft_np
        cost_mx = mx.array(cost_np)

        ripped = [c for c in conns if c.path is None]
        for lo in range(0, len(ripped), batch_size):
            chunk = ripped[lo:lo + batch_size]
            blk = np.zeros((N, len(chunk)), dtype=np.uint8)
            sources = []
            for b, c in enumerate(chunk):
                col = net_mask(c.net)
                if halo:
                    col = col | halo_all
                    own_ring = halo_by_net.get(c.net)
                    if own_ring is not None:   # own net's halo stays passable
                        col[own_ring] = net_mask(c.net)[own_ring]
                blk[:, b] = col
                # Own-tree reuse becomes free at the source set (_own_tree_seed).
                seed = _own_tree_seed(c.a_nodes, kept_by_net.get(c.net, []))
                blk[list(seed | set(c.b_nodes)), b] = 0
                sources.append(sorted(int(n) for n in seed))
            t0 = time.perf_counter()
            dist, _rounds, converged = wavefront.batched_sssp(
                rp, ci, wt, N, sources, cost=cost_mx, blocked=mx.array(blk),
                max_rounds=max_rounds)
            sec["sssp"] += time.perf_counter() - t0
            if not converged:
                for c in chunk:
                    c.reason = f"batched_sssp exhausted max_rounds={max_rounds}"
                continue
            t0 = time.perf_counter()
            for b, c in enumerate(chunk):
                dcol = np.ascontiguousarray(np.asarray(dist[:, b], dtype=np.float64))
                blist = list(c.b_nodes)
                ti = int(np.argmin(dcol[blist]))
                target = int(blist[ti])
                td = float(dcol[target])
                if not np.isfinite(td):
                    c.reason = "target unreachable (hard-blocked or walled off)"
                    continue
                # float32 ULP grows with dist magnitude — late iterations push
                # hist + present costs into the tens of thousands.
                tol = 1e-3 + 1e-6 * td
                try:
                    c.path = extract_path(dcol, lat.row_ptr, lat.col_idx,
                                          lat.weight, target, tol=tol,
                                          cost=cost_np)
                    c.reason = None
                except ValueError as e:
                    c.reason = f"backtrace failed: {e}"
            sec["backtrace"] += time.perf_counter() - t0

        nodes_by_net = {}
        for c in conns:
            if c.path is not None:
                nodes_by_net.setdefault(c.net, set()).update(foot(c.path))
        usage = np.zeros(N, dtype=np.int32)
        for net, nodes in nodes_by_net.items():
            usage[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] += 1
        over = np.maximum(usage - 1, 0)
        total = int(over.sum())
        overuse_curve.append(total)
        if total == 0:
            break

        hist += np.float32(hist_weight) * over.astype(np.float32)
        streak = np.where(over > 0, streak + 1, 0)
        if it == max_iters:
            break  # keep last paths for the final greedy legalization
        # Rip ASYMMETRICALLY: at each overused node the lowest-code tenant
        # keeps its path, so kept_usage prices the node for everyone else next
        # iteration. Co-ripping all tenants leaves contested nodes costless
        # and equal-preference nets swap forever (reviewed livelock). Two
        # escalations stack on this, and their order matters:
        # - Keeper alternation (keeper_patience): a node held by the SAME
        #   keeper net for keeper_patience consecutive overused iterations
        #   makes that keeper YIELD for one round — its connections through
        #   the node rip while every other tenant keeps. Ownership TRANSFERS
        #   instead of vacating, so the former keeper (whose kept path
        #   asymmetric rip-up never re-evaluates) finally prices the node via
        #   kept_usage and pays for its own alternative. Without this, a
        #   tenant whose only/cheapest route crosses the node bounces off an
        #   immovable keeper until max_iters even when the keeper's own
        #   alternative is cheap. The hold counter resets on yield, so a
        #   keeper that storms straight back yields again a patience later.
        # - Nodes overused >= 4 iterations running (streak) get a FULL rip —
        #   by then hist separates the alternatives, and the keeper itself
        #   may be the net that ought to move. With the default patience of
        #   3, alternation fires first (iteration 3 of a standoff); if the
        #   role swap alone doesn't clear the node, streak hits 4 the very
        #   next iteration and the full rip takes over as the deeper
        #   fallback. It outranks alternation in the rip test below on
        #   purpose: it also undoes a swap that helped nobody, restoring the
        #   symmetric hist-driven escape (see MEANDER in test_pathfinder.py).
        over_nodes = set(np.flatnonzero(over).tolist())
        tenants = {}
        for net in sorted(nodes_by_net):
            for v in nodes_by_net[net] & over_nodes:
                tenants.setdefault(v, []).append(net)
        keeper = {v: ts[0] for v, ts in tenants.items()}
        prev_hold, keeper_hold, yields = keeper_hold, {}, set()
        for v, net in keeper.items():  # O(overused nodes), like the rest
            pnet, run = prev_hold.get(v, (None, 0))
            run = run + 1 if pnet == net else 1
            if run >= keeper_patience:
                yields.add(v)
                run = 0
            keeper_hold[v] = (net, run)
        touched = {}  # conn (crossing an overused node) -> pre-rip path length
        for c in conns:
            if c.path is None:
                continue
            # Footprint, not path: a connection whose VIA HALO lands on an
            # overused node is a party to that conflict and must be rippable,
            # even though no copper of its own sits on the node.
            for v in foot(c.path):
                if v not in over_nodes:
                    continue
                if c not in touched:
                    touched[c] = len(c.path)
                # A yield inverts the roles at v: keeper rips, tenants keep.
                rips = keeper[v] == c.net if v in yields else keeper[v] != c.net
                if streak[v] >= 4 or rips:
                    c.path = None
                    c.reason = "ripped on overused nodes"
                    break
        # Multi-party standoffs outlive every per-NODE escalation above,
        # because their contested nodes WANDER: with 3+ parties (or one big
        # net contested at several spots) each rip re-lands one node over, so
        # keeper_hold and streak reset while the same NETS stay deadlocked
        # (icebreaker-v1.0e nets 95/96 vs 3: the contested node alternates
        # between (107,89..92) forever). Track conflicts at NET granularity:
        # a windowed graph over net pairs that shared an overused node, and
        # per-net conflict streaks. A connected component whose core (nets in
        # conflict clique_patience iterations running) has >= 3 nets is a
        # standoff pairwise role-swaps cannot clear — hand it to
        # _clique_resolve, which reroutes the involved connections
        # SEQUENTIALLY under hard obstacles. Two-party deadlocks stay with
        # keeper alternation. Bookkeeping is O(overused nodes) per iteration;
        # the resolve itself fires at most once per component per window.
        confl_nets = set()
        for ts in tenants.values():
            confl_nets.update(ts)
            for i in range(len(ts) - 1):
                for j in range(i + 1, len(ts)):
                    edge_seen[(ts[i], ts[j])] = it
        conf_streak = {n: conf_streak.get(n, 0) + 1 for n in confl_nets}
        edge_seen = {e: s for e, s in edge_seen.items()
                     if it - s < clique_patience}
        if clique_patience <= max_iters:
            for comp in _components(edge_seen):
                core = {n for n in comp
                        if conf_streak.get(n, 0) >= clique_patience}
                if len(core) < 3:
                    continue
                group = [c for c in touched if c.net in core]
                if not group or len(group) > 24:
                    continue  # giant tangles are congestion, not a standoff
                # Self-correcting order: nets hard-blocked in an earlier
                # resolve go first this time; then most-direct-first.
                group.sort(key=lambda c: (-seq_fail.get(id(c), 0),
                                          touched[c], c.net))
                t0 = time.perf_counter()
                _clique_resolve(lat, conns, group, net_mask, rp, ci, wt, N,
                                max_rounds, seq_fail, soft_np=soft_np,
                                foot=foot)
                sec["clique"] = sec.get("clique", 0.0) + \
                    (time.perf_counter() - t0)
                for n in core:  # cooldown: re-arm the window from scratch
                    conf_streak.pop(n, None)
                edge_seen = {e: s for e, s in edge_seen.items()
                             if e[0] not in core and e[1] not in core}
        present *= present_growth
        # Windowed, re-armable stall escape: if the best total of the last 8
        # iterations hasn't improved on the 8 before, escalate present hard.
        # (The old exact-equality one-shot never fired under oscillation.)
        if it % 8 == 0 and len(overuse_curve) >= 16 and \
                min(overuse_curve[-8:]) >= min(overuse_curve[-16:-8]):
            present *= 4.0
    sec["negotiate"] = time.perf_counter() - t_loop

    if overuse_curve and overuse_curve[-1] > 0:
        # Out of iterations with overuse left: first-come greedy keeps one net
        # per contested node, everything else fails. Result stays legal.
        claimed = {}
        for c in conns:
            if c.path is None:
                continue
            fp = foot(c.path)
            hit = next((v for v in fp if claimed.get(v, c.net) != c.net), None)
            if hit is not None:
                c.reason = (f"congestion unresolved after {iterations} iterations "
                            f"(node {hit} contested with net {claimed[hit]})")
                c.path = None
            else:
                for v in fp:
                    claimed[v] = c.net

    if refine_passes > 0 and overuse_curve and overuse_curve[-1] == 0:
        t0 = time.perf_counter()
        before, after = _refine(lat, conns, net_mask, rp, ci, wt, N,
                                refine_passes, batch_size, max_rounds,
                                soft_np=soft_np, foot=foot)
        sec["refine"] = time.perf_counter() - t0
        sec["refine_gain_pct"] = (100.0 * (before - after) / before
                                  if before > 0 else 0.0)

    net_paths, failed = {}, []
    for c in conns:
        if c.path is not None:
            net_paths.setdefault(c.net, []).append(c.path)
        else:
            failed.append((c.net, c.reason or "never routed"))

    clearance_stats = None
    if clearance is not None:
        soft_x = 0
        for c in conns:
            if c.path is None:
                continue
            opened = clearance.soft_allow.get(c.net)
            if opened and not opened.isdisjoint(c.path):
                soft_x += 1
        clearance_stats = {
            "claimed_nodes": len(clearance.node_net),
            "edge_nodes": clearance.edge_nodes,
            "degraded_pairs": clearance.degraded_pairs,
            "soft_crossings": soft_x,
            "inflate_mm": clearance.inflate_mm,
        }
    via_stats = None
    if halo:
        via_stats = {"exclusion_mm": float(via_exclusion_mm),
                     "halo_nodes_per_layer": len(halo),
                     "halo_layers": lat.L}

    t0 = time.perf_counter()
    tracks, vias = paths_to_tracks(lat, net_paths)
    sm_tracks = sm_vias = None
    if smooth:
        from smooth import polylines_to_tracks, smooth_net_paths
        # Occupancy: every routed node of every net, plus pad ownership
        # (node_owner already carries build_connections' claims) plus the
        # clearance rings — a chamfer must not cut a corner INTO a foreign
        # pad's ring the raw path respected (-1 claims read as foreign to
        # every net). Path nodes overlay pad rectangles so an extra_allow
        # crossing reads as the crossing net — conservative for the pad's
        # own net near it.
        # Via halos join the occupancy too: a chamfer must not cut a corner
        # into the exclusion zone of another net's via, which the raw path
        # (routed under the halo-aware usage model) respected by construction.
        occupied = dict(node_owner)
        if clearance is not None:
            occupied.update(clearance.node_net)
        for net, paths in net_paths.items():
            for path in paths:
                for v in _footprint(lat, path, halo, pad_exempt):
                    occupied[v] = net
        sm_tracks = polylines_to_tracks(
            smooth_net_paths(lat, net_paths, occupied,
                             pad_nodes=frozenset(node_owner),
                             allow_diagonals=allow_diagonals))
        sm_vias = vias  # smoothing preserves vias and layer splits exactly
    if sm_tracks is not None:
        wirelength = sum(math.hypot(x2 - x1, y2 - y1)
                         for x1, y1, x2, y2, _, _ in sm_tracks)
    else:
        wirelength = sum(abs(x2 - x1) + abs(y2 - y1)
                         for x1, y1, x2, y2, _, _ in tracks)  # axis-aligned
    sec["emit"] = time.perf_counter() - t0

    return RouteResult(net_paths=net_paths, failed=failed, conflicts=conflicts,
                       iterations=iterations, overuse_curve=overuse_curve,
                       wirelength_mm=wirelength, via_count=len(vias), seconds=sec,
                       tracks=sm_tracks, vias=sm_vias,
                       clearance_stats=clearance_stats,
                       via_stats=via_stats)


def _net_stays_connected(net_conns, conn, cand):
    """Would swapping `conn`'s path for `cand` keep the net's pads joined?

    Other connections of the net may terminate ON conn's old path (own-tree
    seeding), so replacing it can strand them with zero overuse — a break the
    legality audit cannot see. Model the net as blobs (every connection's
    path plus every pad node set), merge blobs that share a node, and require
    all pads to land in one component."""
    blobs, pads = [set(cand)], []
    for o in net_conns:
        if o is not conn:
            blobs.append(set(o.path))
        for p in (o.a_nodes, o.b_nodes):
            ps = set(p)
            blobs.append(ps)
            pads.append(ps)
    comps = []
    for s in blobs:
        merged, rest = set(s), []
        for cs in comps:
            if cs & merged:
                merged |= cs
            else:
                rest.append(cs)
        comps = rest + [merged]
    comp = next(cs for cs in comps if next(iter(pads[0])) in cs)
    return all(next(iter(p)) in comp for p in pads)


def _refine(lat, conns, net_mask, rp, ci, wt, N, refine_passes, batch_size,
            max_rounds, soft_np=None, foot=None):
    """Post-negotiation slack recovery. Reroute every routed connection
    against the FINISHED board — every node of every OTHER net's current path
    a hard obstacle, no hist/present pricing — and adopt a candidate only if
    it is strictly cheaper (backtrace.path_cost, wirelength + via weights).
    soft_np (clearance soft-degrade prices) IS kept in force: it prices both
    the search and the adoption comparison, so refine cannot pull a path into
    a degraded corridor that negotiation paid to stay out of.

    Legality is structural here, not negotiated: blocking makes a candidate
    conflict-free against the batch's snapshot. Two planes in one batch can
    still collide with EACH OTHER (each avoided only the other's OLD path),
    so adoption is sequential: a candidate touching a node claimed by an
    earlier adoption of a DIFFERENT net this batch is dropped, and an
    adopter's old nodes are NOT freed for later planes (the snapshot stays
    conservative; the next batch/pass picks up the slack). Intra-net safety
    is _net_stays_connected. Returns (cost_before, cost_after).

    foot(path) -> the nodes a path occupies (path + via halos; plain set(path)
    when via exclusion is off). Obstacles, adoption claims and the legality
    audit all speak footprints. The kernel cannot see a candidate's OWN new
    via halo, so every candidate is re-checked against used_all before it is
    adopted — otherwise refine could shorten a path by parking a fresh via
    beside another net's copper and the audit would fire."""
    import mlx.core as mx
    import wavefront
    from backtrace import path_cost

    if foot is None:
        def foot(path):
            return set(path)
    soft_mx = mx.array(soft_np) if soft_np is not None else None

    def soft_sum(path):
        return float(soft_np[np.asarray(path, dtype=np.int64)].sum()) \
            if soft_np is not None else 0.0

    def total_cost():
        return sum(path_cost(c.path, lat.row_ptr, lat.col_idx, lat.weight)
                   for c in conns if c.path is not None)

    routed = [c for c in conns if c.path is not None]
    cost_before = total_cost()
    for _ in range(refine_passes if routed else 0):
        for lo in range(0, len(routed), batch_size):
            chunk = routed[lo:lo + batch_size]
            # Fresh snapshot every batch, not just every pass: earlier
            # batches' adoptions must be obstacles (and seeds) here.
            by_net = {}
            for c in conns:
                if c.path is not None:
                    by_net.setdefault(c.net, []).append(c)
            used_all = np.zeros(N, dtype=np.uint8)
            own_nodes = {}
            for net, cs in by_net.items():
                nodes = set().union(*(foot(c.path) for c in cs))
                own_nodes[net] = nodes
                used_all[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] = 1

            blk = np.zeros((N, len(chunk)), dtype=np.uint8)
            sources, targets = [], []
            for b, c in enumerate(chunk):
                own = own_nodes[c.net]
                m = net_mask(c.net)
                col = m | used_all
                idx = np.fromiter(own, dtype=np.int64, count=len(own))
                col[idx] = m[idx]  # own path nodes: pad rule only
                # Seed exactly like the main loop, with c itself treated as
                # ripped — its own old path is neither seed nor obstacle.
                # But NEVER free a node a foreign path occupies: the main
                # loop's force-clear of own pad nodes is safe only because
                # negotiation re-tallies; here a pad node carried by another
                # net (extra_allow overlap) must stay a hard obstacle, and
                # can be neither source nor target.
                kept = [set(o.path) for o in by_net[c.net] if o is not c]
                seed = _own_tree_seed(c.a_nodes, kept)
                foreign = {int(v) for v in (seed | set(c.b_nodes))
                           if used_all[v] and v not in own}
                seed -= foreign
                tgts = [int(n) for n in c.b_nodes if int(n) not in foreign]
                if seed and tgts:
                    col[list(seed | set(tgts))] = 0
                blk[:, b] = col
                sources.append(sorted(int(n) for n in seed) if tgts else [])
                targets.append(tgts)

            dist, _rounds, converged = wavefront.batched_sssp(
                rp, ci, wt, N, sources, cost=soft_mx, blocked=mx.array(blk),
                max_rounds=max_rounds)
            if not converged:
                continue  # best-effort: keep every old path
            claimed = {}  # node -> net adopted this batch
            for b, c in enumerate(chunk):
                if not sources[b] or not targets[b]:
                    continue
                dcol = np.ascontiguousarray(np.asarray(dist[:, b], dtype=np.float64))
                blist = targets[b]
                target = int(blist[int(np.argmin(dcol[blist]))])
                td = float(dcol[target])
                if not np.isfinite(td):
                    continue
                try:
                    cand = extract_path(dcol, lat.row_ptr, lat.col_idx,
                                        lat.weight, target, tol=1e-3 + 1e-6 * td,
                                        cost=soft_np)
                except ValueError:
                    continue
                new_cost = path_cost(cand, lat.row_ptr, lat.col_idx,
                                     lat.weight) + soft_sum(cand)
                old_cost = path_cost(c.path, lat.row_ptr, lat.col_idx,
                                     lat.weight) + soft_sum(c.path)
                if not new_cost < old_cost - 1e-6:
                    continue
                fp = foot(cand)
                # The kernel avoided every OTHER net's footprint, but not the
                # halo of a via this candidate itself introduces. Re-check.
                if any(used_all[v] and v not in own_nodes[c.net] for v in fp):
                    continue
                if any(claimed.get(v, c.net) != c.net for v in fp):
                    continue
                if not _net_stays_connected(by_net[c.net], c, cand):
                    continue
                c.path = cand
                for v in fp:
                    claimed[v] = c.net
    cost_after = total_cost()

    # Legality audit: refine improving cost by creating overuse would be a
    # silent regression — make it loud instead.
    nodes_by_net = {}
    for c in conns:
        if c.path is not None:
            nodes_by_net.setdefault(c.net, set()).update(foot(c.path))
    usage = np.zeros(N, dtype=np.int32)
    for nodes in nodes_by_net.values():
        usage[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] += 1
    over_nodes = np.flatnonzero(usage > 1)
    if over_nodes.size:
        detail = [(int(v), lat.coords(int(v)),
                   sorted(net for net, nodes in nodes_by_net.items()
                          if int(v) in nodes))
                  for v in over_nodes[:20].tolist()]
        raise AssertionError(
            f"refine broke legality: {over_nodes.size} overused node(s); "
            f"first (node, (x, y, layer), nets): {detail}")
    return cost_before, cost_after


def _components(edges):
    """Connected components over an iterable of (a, b) edges: [set, ...]."""
    parent = {}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b in edges:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)
    comps = {}
    for n in parent:
        comps.setdefault(find(n), set()).add(n)
    return list(comps.values())


def _clique_resolve(lat, conns, group, net_mask, rp, ci, wt, N, max_rounds,
                    seq_fail, soft_np=None, foot=None):
    """Break a multi-party standoff by SEQUENTIAL rip-up-and-reroute.

    Rip every connection in `group`, then route them one at a time against
    the rest of the board as HARD obstacles (same blocking discipline as
    _refine: every node of every other net's current path blocked, own pad
    rule only on own nodes) — each adoption immediately an obstacle for the
    next. Simultaneous negotiation lets 3+ parties yield in circles; here the
    group lands on a mutually-legal joint configuration in one shot, or a
    member fails LOUDLY (stays ripped for the normal loop, and its
    seq_fail[id] bump sends it to the FRONT of the next resolve, so the
    ordering self-corrects toward constrained-first)."""
    import mlx.core as mx
    import wavefront

    if foot is None:
        def foot(path):
            return set(path)
    soft_mx = mx.array(soft_np) if soft_np is not None else None
    for c in group:
        c.path = None
        c.reason = "ripped for multi-party sequential reroute"
    by_net = {}
    for c in conns:
        if c.path is not None:
            by_net.setdefault(c.net, []).append(c)
    used = np.zeros(N, dtype=np.uint8)
    own_nodes = {}
    for net, cs in by_net.items():
        nodes = set().union(*(foot(o.path) for o in cs))
        own_nodes[net] = nodes
        used[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] = 1
    for c in group:
        m = net_mask(c.net)
        col = m | used
        own = own_nodes.get(c.net, set())
        if own:
            idx = np.fromiter(own, dtype=np.int64, count=len(own))
            col[idx] = m[idx]  # own path nodes: pad rule only
        # Seed exactly like _refine: own-tree reuse free, but a node a
        # foreign path occupies can be neither source nor target.
        kept = [set(o.path) for o in by_net.get(c.net, [])]
        seed = _own_tree_seed(c.a_nodes, kept)
        foreign = {int(v) for v in (seed | set(c.b_nodes))
                   if used[v] and v not in own}
        seed -= foreign
        tgts = [int(n) for n in c.b_nodes if int(n) not in foreign]
        path = None
        if seed and tgts:
            col[list(seed | set(tgts))] = 0
            dist, _rounds, converged = wavefront.batched_sssp(
                rp, ci, wt, N, [sorted(seed)], cost=soft_mx,
                blocked=mx.array(col[:, None]), max_rounds=max_rounds)
            if converged:
                dcol = np.ascontiguousarray(
                    np.asarray(dist[:, 0], dtype=np.float64))
                target = int(tgts[int(np.argmin(dcol[tgts]))])
                td = float(dcol[target])
                if np.isfinite(td):
                    try:
                        path = extract_path(dcol, lat.row_ptr, lat.col_idx,
                                            lat.weight, target,
                                            tol=1e-3 + 1e-6 * td,
                                            cost=soft_np)
                    except ValueError:
                        path = None
        nodes = foot(path) if path is not None else None
        # As in _refine: the kernel could not see the halo of a via this very
        # path introduces. A candidate whose halo lands on foreign copper is
        # not a solution — fail it loudly and let the ordering self-correct.
        if nodes is not None and any(used[v] and v not in own for v in nodes):
            path = None
        if path is None:
            seq_fail[id(c)] = seq_fail.get(id(c), 0) + 1
            continue
        c.path = path
        c.reason = None
        by_net.setdefault(c.net, []).append(c)
        own_nodes.setdefault(c.net, set()).update(nodes)
        used[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] = 1


def _runs(values):
    """Sorted ints -> (start, end) inclusive runs of consecutive values."""
    vs = sorted(values)
    start = prev = vs[0]
    for v in vs[1:]:
        if v == prev + 1:
            prev = v
        else:
            yield start, prev
            start = prev = v
    yield start, prev


def paths_to_tracks(lat, net_paths):
    """RouteResult.net_paths -> (tracks, vias).

    tracks: (x1_mm, y1_mm, x2_mm, y2_mm, layer_name, net_code), collinear
    same-layer runs merged; same-net edges shared by several paths emit once.
    vias: (x_mm, y_mm, net_code), one per distinct layer-change (x, y) per net.
    """
    tracks, vias = [], []
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    for net in sorted(net_paths):
        hor, ver, via_pts = {}, {}, set()
        for path in net_paths[net]:
            for a, b in zip(path, path[1:]):
                ax, ay, al = lat.coords(a)
                bx, by, bl = lat.coords(b)
                if al != bl:
                    via_pts.add((ax, ay))
                elif ay == by:
                    hor.setdefault((al, ay), set()).add(min(ax, bx))
                else:
                    ver.setdefault((al, ax), set()).add(min(ay, by))
        for (il, iy), xs in sorted(hor.items()):
            for x0, x1 in _runs(xs):
                tracks.append((ox + x0 * p, oy + iy * p,
                               ox + (x1 + 1) * p, oy + iy * p,
                               lat.layer_names[il], net))
        for (il, ix), ys in sorted(ver.items()):
            for y0, y1 in _runs(ys):
                tracks.append((ox + ix * p, oy + y0 * p,
                               ox + ix * p, oy + (y1 + 1) * p,
                               lat.layer_names[il], net))
        for ix, iy in sorted(via_pts):
            vias.append((ox + ix * p, oy + iy * p, net))
    return tracks, vias


# ── board plumbing ────────────────────────────────────────────────────────────

def net_pads_for_board(board, lat, node_owner=None):
    """Board pads -> net_pads for build_connections. A through-hole pad snaps
    on EVERY lattice layer (one set); an SMD pad on its own layer(s) present.

    A pad's node set is its snap node PLUS every footprint node the ownership
    arbitration assigned to its net: the whole footprint is the escape set, so
    a route can leave by any side of the pad, not only past the snap node —
    fine-pitch pads whose snap node is hemmed in by a neighbor's claim would
    otherwise be unreachable."""
    from lattice import pad_rect_nodes
    node_owner = node_owner or {}
    net_pads = {}
    for pad in board.pads:
        if pad.net_code <= 0:
            continue
        layers = lat.layer_names if pad.through_hole else \
            [ln for ln in pad.layers if ln in lat.layer_names]
        nodes = []
        for ln in layers:
            nd = lat.snap(pad.x_mm, pad.y_mm, ln)
            if nd not in nodes:
                nodes.append(nd)
            for n in pad_rect_nodes(lat, pad, ln):
                if node_owner.get(n) == pad.net_code and n not in nodes:
                    nodes.append(n)
        if nodes:
            net_pads.setdefault(pad.net_code, []).append(
                (tuple(nodes), (pad.x_mm, pad.y_mm)))
    return net_pads


def route_board(board_path, pitch_mm=1.0, layer_names=None, directions="both",
                via_cost=12.0, dir_penalty=1.25, clearance_mm=None,
                track_width_mm=None, via_size_mm=None, via_exclusion=True,
                **kwargs):
    """Load, lattice, route. Returns (board, lat, RouteResult).

    directions/via_cost/dir_penalty go to lattice_for_board (board-routing
    defaults: both-direction layers, expensive vias); **kwargs go to
    route_lattice.

    clearance_mm: real spacing between routed copper and foreign pads /
    the board edge (lattice.clearance_map). None -> the project Default
    class clearance, else 0.2 mm; 0 disables the clearance model entirely
    (the old pitch-is-clearance behavior). track_width_mm overrides the
    project Default class width the ring inflation reads.

    via_size_mm / via_exclusion: the copper geometry contract
    (geometry.resolve_board_geometry) is computed for every board and drives
    two things the lattice alone cannot know — the radius a via claims around
    itself (via_exclusion=False disables it, restoring the pre-exclusion
    router) and whether 45-degree smoothing is geometrically legal at this
    pitch. It also VERIFIES the orthogonal track-track claim numerically and
    puts the numbers on RouteResult.geometry_warnings when a board's pitch is
    too fine for its own track width. Nothing here is silent."""
    from board import load_board
    from geometry import resolve_board_geometry
    from lattice import (DEFAULT_CLEARANCE_MM, clearance_map, lattice_for_board,
                         pad_overlap_allowances)

    t0 = time.perf_counter()
    brd = load_board(board_path)
    t_load = time.perf_counter() - t0
    geo = resolve_board_geometry(board_path, pitch_mm, brd.nets,
                                 clearance_mm=clearance_mm,
                                 track_width_mm=track_width_mm,
                                 via_size_mm=via_size_mm)
    t0 = time.perf_counter()
    lat, pad_nodes, node_owner = lattice_for_board(brd, pitch_mm,
                                                   layer_names=layer_names,
                                                   directions=directions,
                                                   via_cost=via_cost,
                                                   dir_penalty=dir_penalty)
    extra_allow = pad_overlap_allowances(brd, lat)
    if clearance_mm is None:
        clearance_mm = DEFAULT_CLEARANCE_MM
    clearance = clearance_map(brd, lat, node_owner, pad_nodes,
                              clearance_mm=clearance_mm,
                              track_width_mm=track_width_mm) \
        if clearance_mm > 0 else None
    t_lat = time.perf_counter() - t0
    kwargs.setdefault("via_exclusion_mm",
                      geo.via_exclusion_mm if via_exclusion else 0.0)
    kwargs.setdefault("allow_diagonals", geo.diagonals_ok)
    res = route_lattice(lat, net_pads_for_board(brd, lat, node_owner),
                        node_owner, extra_allow=extra_allow,
                        clearance=clearance, **kwargs)
    res.seconds = {"load": t_load, "lattice": t_lat, **res.seconds}
    res.geometry = geo
    res.geometry_note = geo.summary()
    res.geometry_warnings = geo.warnings()
    return brd, lat, res


def _write_svg(brd, lat, res, path):
    # render.py is built concurrently against the same RouteResult interface;
    # guard both its absence and its breakage.
    try:
        import render
    except Exception as e:
        print(f"svg         : skipped — render.py not importable ({e})")
        return
    fn = next((getattr(render, name) for name in
               ("render_svg", "write_svg", "render") if hasattr(render, name)), None)
    if fn is None:
        print("svg         : skipped — render.py has no render_svg/write_svg/render")
        return
    try:
        try:
            fn(brd, lat, res, path)
        except TypeError:
            fn(lat, res, path)
        print(f"svg         : wrote {path}")
    except Exception as e:
        print(f"svg         : skipped — render failed ({e})")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="PathFinder route a KiCad board")
    ap.add_argument("board")
    ap.add_argument("--pitch", type=float, default=1.0)
    ap.add_argument("--layers", default="F.Cu,B.Cu")
    ap.add_argument("--svg", default=None)
    ap.add_argument("--no-refine", action="store_true",
                    help="skip the post-negotiation slack-recovery pass")
    ap.add_argument("--no-smooth", action="store_true",
                    help="emit raw 90-degree lattice geometry instead of "
                         "legality-checked 45-degree smoothing")
    ap.add_argument("--via-cost", type=float, default=12.0,
                    help="lattice via edge cost in grid-step units (default 12)")
    ap.add_argument("--dir-penalty", type=float, default=1.25,
                    help="cost multiplier for a layer's non-preferred direction "
                         "(directions=both only, default 1.25)")
    ap.add_argument("--directions", choices=("both", "alternating"), default="both",
                    help="both: every layer H+V with preferred-direction pricing; "
                         "alternating: one direction per layer (default both)")
    ap.add_argument("--no-via-exclusion", action="store_true",
                    help="stop vias claiming their clearance neighbourhood "
                         "(restores the pre-exclusion router; emits illegal "
                         "copper wherever a via sits beside another net)")
    ap.add_argument("--clearance", type=float, default=None,
                    help="copper-to-foreign-pad / board-edge spacing in mm "
                         "(default 0.2; 0 disables the clearance model)")
    args = ap.parse_args(argv)
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]

    brd, lat, res = route_board(args.board, pitch_mm=args.pitch, layer_names=layers,
                                directions=args.directions, via_cost=args.via_cost,
                                dir_penalty=args.dir_penalty,
                                clearance_mm=args.clearance,
                                via_exclusion=not args.no_via_exclusion,
                                refine_passes=0 if args.no_refine else 2,
                                smooth=not args.no_smooth)

    failed_nets = {n for n, _ in res.failed}
    routable = set(res.net_paths) | failed_nets
    conn_total = sum(len(p) for p in res.net_paths.values()) + len(res.failed)
    print(f"board       : {args.board.split('/')[-1]}  "
          f"({len(brd.pads)} pads, {len(brd.nets)} nets)")
    print(f"lattice     : {lat.W}x{lat.H}x{lat.L}  pitch {lat.pitch_mm} mm  "
          f"{lat.layer_names}")
    print(f"nets        : {len(routable)} routable | "
          f"{len(routable - failed_nets)} fully routed | {len(failed_nets)} with failures")
    print(f"connections : {conn_total}  ({len(res.conflicts)} pad-snap conflicts)")
    print(f"geometry    : {res.geometry_note}")
    for w in (res.geometry_warnings or []):
        print(f"WARNING     : {w}")
    print(f"iterations  : {res.iterations}")
    print(f"overuse     : {res.overuse_curve}")
    print(f"wirelength  : {res.wirelength_mm:.1f} mm")
    print(f"vias        : {res.via_count}")
    if "refine_gain_pct" in res.seconds:
        print(f"refine      : path cost -{res.seconds['refine_gain_pct']:.2f}%")
    if res.clearance_stats:
        cs = res.clearance_stats
        print(f"clearance   : inflate {cs['inflate_mm']:.3f} mm | "
              f"{cs['claimed_nodes']} ring/edge nodes "
              f"({cs['edge_nodes']} edge) | "
              f"{cs['degraded_pairs']} pad pairs degraded to soft | "
              f"{cs['soft_crossings']} connections cross soft nodes")
    if res.via_stats:
        vs = res.via_stats
        print(f"via exclude : r {vs['exclusion_mm']:.3f} mm | "
              f"{vs['halo_nodes_per_layer']} nodes/layer x "
              f"{vs['halo_layers']} layers claimed per via")
    print("seconds     : " + " | ".join(f"{k} {v:.2f}" for k, v in res.seconds.items()
                                        if not k.endswith("_pct")))
    for net, reason in res.failed[:10]:
        print(f"  failed net {net} ({brd.nets.get(net, '?')}): {reason}")
    if len(res.failed) > 10:
        print(f"  ... {len(res.failed) - 10} more")

    if args.svg:
        _write_svg(brd, lat, res, args.svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
