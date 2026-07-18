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
- Hard legality is pad ownership only (node_owner): per-net uint8 masks built
  lazily and cached, with every plane's own sources/targets force-cleared —
  a pad can snap inside another pad's claimed rectangle.
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


@dataclass
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
                  refine_passes=2, smooth=True, keeper_patience=3):
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
    effectively disables alternation."""
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
    masks = {}

    def net_mask(net):
        m = masks.get(net)
        if m is None:
            m = np.zeros(N, dtype=np.uint8)
            if own_nodes.size:
                m[own_nodes[own_nets != net]] = 1
            grant = (extra_allow or {}).get(net)
            if grant:
                m[np.fromiter(grant, dtype=np.int64, count=len(grant))] = 0
            masks[net] = m
        return m

    hist = np.zeros(N, dtype=np.float32)
    streak = np.zeros(N, dtype=np.int32)  # consecutive iterations overused
    keeper_hold = {}  # node -> (keeper net, consecutive iterations as keeper)
    present = float(present_factor)
    overuse_curve = []
    iterations = 0

    t_loop = time.perf_counter()
    for it in range(1, max_iters + 1):
        iterations = it
        kept_by_net = {}
        for c in conns:
            if c.path is not None:
                kept_by_net.setdefault(c.net, []).append(set(c.path))
        kept_usage = np.zeros(N, dtype=np.float32)
        for net, sets in kept_by_net.items():
            nodes = set().union(*sets)
            kept_usage[np.fromiter(nodes, dtype=np.int64, count=len(nodes))] += 1.0
        cost_np = hist + np.float32(present) * kept_usage
        cost_mx = mx.array(cost_np)

        ripped = [c for c in conns if c.path is None]
        for lo in range(0, len(ripped), batch_size):
            chunk = ripped[lo:lo + batch_size]
            blk = np.zeros((N, len(chunk)), dtype=np.uint8)
            sources = []
            for b, c in enumerate(chunk):
                blk[:, b] = net_mask(c.net)
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
                nodes_by_net.setdefault(c.net, set()).update(c.path)
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
        keeper = {}
        for net in sorted(nodes_by_net):
            for v in nodes_by_net[net] & over_nodes:
                keeper.setdefault(v, net)
        prev_hold, keeper_hold, yields = keeper_hold, {}, set()
        for v, net in keeper.items():  # O(overused nodes), like the rest
            pnet, run = prev_hold.get(v, (None, 0))
            run = run + 1 if pnet == net else 1
            if run >= keeper_patience:
                yields.add(v)
                run = 0
            keeper_hold[v] = (net, run)
        for c in conns:
            if c.path is None:
                continue
            for v in c.path:
                if v not in over_nodes:
                    continue
                # A yield inverts the roles at v: keeper rips, tenants keep.
                rips = keeper[v] == c.net if v in yields else keeper[v] != c.net
                if streak[v] >= 4 or rips:
                    c.path = None
                    c.reason = "ripped on overused nodes"
                    break
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
            hit = next((v for v in c.path if claimed.get(v, c.net) != c.net), None)
            if hit is not None:
                c.reason = (f"congestion unresolved after {iterations} iterations "
                            f"(node {hit} contested with net {claimed[hit]})")
                c.path = None
            else:
                for v in c.path:
                    claimed[v] = c.net

    if refine_passes > 0 and overuse_curve and overuse_curve[-1] == 0:
        t0 = time.perf_counter()
        before, after = _refine(lat, conns, net_mask, rp, ci, wt, N,
                                refine_passes, batch_size, max_rounds)
        sec["refine"] = time.perf_counter() - t0
        sec["refine_gain_pct"] = (100.0 * (before - after) / before
                                  if before > 0 else 0.0)

    net_paths, failed = {}, []
    for c in conns:
        if c.path is not None:
            net_paths.setdefault(c.net, []).append(c.path)
        else:
            failed.append((c.net, c.reason or "never routed"))

    t0 = time.perf_counter()
    tracks, vias = paths_to_tracks(lat, net_paths)
    sm_tracks = sm_vias = None
    if smooth:
        from smooth import polylines_to_tracks, smooth_net_paths
        # Occupancy: every routed node of every net, plus pad ownership
        # (node_owner already carries build_connections' claims). Path nodes
        # overlay pad rectangles so an extra_allow crossing reads as the
        # crossing net — conservative for the pad's own net near it.
        occupied = dict(node_owner)
        for net, paths in net_paths.items():
            for path in paths:
                for v in path:
                    occupied[v] = net
        sm_tracks = polylines_to_tracks(
            smooth_net_paths(lat, net_paths, occupied,
                             pad_nodes=frozenset(node_owner)))
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
                       tracks=sm_tracks, vias=sm_vias)


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
            max_rounds):
    """Post-negotiation slack recovery. Reroute every routed connection
    against the FINISHED board — every node of every OTHER net's current path
    a hard obstacle, no hist/present pricing — and adopt a candidate only if
    it is strictly cheaper (backtrace.path_cost, wirelength + via weights).

    Legality is structural here, not negotiated: blocking makes a candidate
    conflict-free against the batch's snapshot. Two planes in one batch can
    still collide with EACH OTHER (each avoided only the other's OLD path),
    so adoption is sequential: a candidate touching a node claimed by an
    earlier adoption of a DIFFERENT net this batch is dropped, and an
    adopter's old nodes are NOT freed for later planes (the snapshot stays
    conservative; the next batch/pass picks up the slack). Intra-net safety
    is _net_stays_connected. Returns (cost_before, cost_after)."""
    import mlx.core as mx
    import wavefront
    from backtrace import path_cost

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
                nodes = set().union(*(set(c.path) for c in cs))
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
                rp, ci, wt, N, sources, blocked=mx.array(blk),
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
                                        lat.weight, target, tol=1e-3 + 1e-6 * td)
                except ValueError:
                    continue
                new_cost = path_cost(cand, lat.row_ptr, lat.col_idx, lat.weight)
                old_cost = path_cost(c.path, lat.row_ptr, lat.col_idx, lat.weight)
                if not new_cost < old_cost - 1e-6:
                    continue
                if any(claimed.get(v, c.net) != c.net for v in cand):
                    continue
                if not _net_stays_connected(by_net[c.net], c, cand):
                    continue
                c.path = cand
                for v in cand:
                    claimed[v] = c.net
    cost_after = total_cost()

    # Legality audit: refine improving cost by creating overuse would be a
    # silent regression — make it loud instead.
    nodes_by_net = {}
    for c in conns:
        if c.path is not None:
            nodes_by_net.setdefault(c.net, set()).update(c.path)
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
                via_cost=8.0, dir_penalty=1.25, **kwargs):
    """Load, lattice, route. Returns (board, lat, RouteResult).

    directions/via_cost/dir_penalty go to lattice_for_board (board-routing
    defaults: both-direction layers, expensive vias); **kwargs go to
    route_lattice."""
    from board import load_board
    from lattice import lattice_for_board, pad_overlap_allowances

    t0 = time.perf_counter()
    brd = load_board(board_path)
    t_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    lat, _pad_nodes, node_owner = lattice_for_board(brd, pitch_mm,
                                                    layer_names=layer_names,
                                                    directions=directions,
                                                    via_cost=via_cost,
                                                    dir_penalty=dir_penalty)
    extra_allow = pad_overlap_allowances(brd, lat)
    t_lat = time.perf_counter() - t0
    res = route_lattice(lat, net_pads_for_board(brd, lat, node_owner),
                        node_owner, extra_allow=extra_allow, **kwargs)
    res.seconds = {"load": t_load, "lattice": t_lat, **res.seconds}
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
    ap.add_argument("--via-cost", type=float, default=8.0,
                    help="lattice via edge cost in grid-step units (default 12)")
    ap.add_argument("--dir-penalty", type=float, default=1.25,
                    help="cost multiplier for a layer's non-preferred direction "
                         "(directions=both only, default 1.25)")
    ap.add_argument("--directions", choices=("both", "alternating"), default="both",
                    help="both: every layer H+V with preferred-direction pricing; "
                         "alternating: one direction per layer (default both)")
    args = ap.parse_args(argv)
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]

    brd, lat, res = route_board(args.board, pitch_mm=args.pitch, layer_names=layers,
                                directions=args.directions, via_cost=args.via_cost,
                                dir_penalty=args.dir_penalty,
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
    print(f"iterations  : {res.iterations}")
    print(f"overuse     : {res.overuse_curve}")
    print(f"wirelength  : {res.wirelength_mm:.1f} mm")
    print(f"vias        : {res.via_count}")
    if "refine_gain_pct" in res.seconds:
        print(f"refine      : path cost -{res.seconds['refine_gain_pct']:.2f}%")
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
