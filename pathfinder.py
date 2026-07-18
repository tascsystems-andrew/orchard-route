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
- paths_to_tracks dedupes same-net lattice edges/vias across paths before
  merging collinear runs: a shared trunk emits its copper once.

CLI: python pathfinder.py BOARD.kicad_pcb [--pitch 1.0] [--layers F.Cu,B.Cu]
     [--svg out.svg]
"""
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


def route_lattice(lat, net_pads, node_owner=None, hist_weight=0.5,
                  present_factor=1.0, present_growth=1.4, max_iters=40,
                  batch_size=128, max_rounds=100_000):
    """Negotiation loop over a built Lattice. net_pads as in build_connections."""
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
            masks[net] = m
        return m

    hist = np.zeros(N, dtype=np.float32)
    streak = np.zeros(N, dtype=np.int32)  # consecutive iterations overused
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
                # Side-aware seeding: a-pad plus every kept-path component of
                # this net reachable from it — own-tree reuse becomes free.
                seed = set(c.a_nodes)
                pending = list(kept_by_net.get(c.net, []))
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
        # and equal-preference nets swap forever (reviewed livelock). Nodes
        # overused >= 4 iterations running get a full rip instead — by then
        # hist separates the alternatives, and the keeper itself may be the
        # net that ought to move.
        over_nodes = set(np.flatnonzero(over).tolist())
        keeper = {}
        for net in sorted(nodes_by_net):
            for v in nodes_by_net[net] & over_nodes:
                keeper.setdefault(v, net)
        for c in conns:
            if c.path is None:
                continue
            for v in c.path:
                if v in over_nodes and (streak[v] >= 4 or keeper[v] != c.net):
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

    net_paths, failed = {}, []
    for c in conns:
        if c.path is not None:
            net_paths.setdefault(c.net, []).append(c.path)
        else:
            failed.append((c.net, c.reason or "never routed"))

    t0 = time.perf_counter()
    tracks, vias = paths_to_tracks(lat, net_paths)
    wirelength = sum(abs(x2 - x1) + abs(y2 - y1)
                     for x1, y1, x2, y2, _, _ in tracks)  # segments are axis-aligned
    sec["emit"] = time.perf_counter() - t0

    return RouteResult(net_paths=net_paths, failed=failed, conflicts=conflicts,
                       iterations=iterations, overuse_curve=overuse_curve,
                       wirelength_mm=wirelength, via_count=len(vias), seconds=sec)


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

def net_pads_for_board(board, lat):
    """Board pads -> net_pads for build_connections. A through-hole pad snaps
    on EVERY lattice layer (one set); an SMD pad on its own layer(s) present."""
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
        if nodes:
            net_pads.setdefault(pad.net_code, []).append(
                (tuple(nodes), (pad.x_mm, pad.y_mm)))
    return net_pads


def route_board(board_path, pitch_mm=1.0, layer_names=None, **kwargs):
    """Load, lattice, route. Returns (board, lat, RouteResult)."""
    from board import load_board
    from lattice import lattice_for_board

    t0 = time.perf_counter()
    brd = load_board(board_path)
    t_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    lat, _pad_nodes, node_owner = lattice_for_board(brd, pitch_mm,
                                                    layer_names=layer_names)
    t_lat = time.perf_counter() - t0
    res = route_lattice(lat, net_pads_for_board(brd, lat), node_owner, **kwargs)
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
    args = ap.parse_args(argv)
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]

    brd, lat, res = route_board(args.board, pitch_mm=args.pitch, layer_names=layers)

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
    print("seconds     : " + " | ".join(f"{k} {v:.2f}" for k, v in res.seconds.items()))
    for net, reason in res.failed[:10]:
        print(f"  failed net {net} ({brd.nets.get(net, '?')}): {reason}")
    if len(res.failed) > 10:
        print(f"  ... {len(res.failed) - 10} more")

    if args.svg:
        _write_svg(brd, lat, res, args.svg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
