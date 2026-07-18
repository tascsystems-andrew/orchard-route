"""45-degree corner smoothing of routed lattice paths — legality-checked.

The lattice routes in unit H/V steps, so a both-directions layer walks what
should be a diagonal as a 90-degree staircase. smooth_net_paths rewrites each
same-layer run of every routed path as a polyline in which alternating H/V
staircases become true 45-degree segments — but ONLY where the diagonal
provably clips no foreign copper.

Legality model (matches the router's clearance-by-pitch model): a unit 45
hop from grid (x, y) to (x+sx, y+sy) cuts the shared corner of four grid
cells, clipping the two off-line cells whose nodes are (x+sx, y) and
(x, y+sy). The hop is legal iff BOTH of those nodes are free or owned by the
net being smoothed (the `occupied` map: node -> net over every routed path
node plus pad ownership). A full staircase becomes one diagonal only when
every hop of it passes; otherwise each single corner is chamfered under the
same per-hop test; a corner that still fails keeps its 90. Interior lattice
points of an adopted diagonal lie exactly ON the segment (they are the
staircase's even points, own-net by construction), so copper still passes
through their centers.

Structure is preserved exactly:
- vias / layer changes split runs and are never moved — smoothing emits no
  cross-layer geometry;
- every run's endpoints are kept verbatim;
- nodes where the net's own tree branches (any path endpoint — a pad or a
  tree junction — and any node used by two or more of the net's paths) are
  never bypassed: other copper attaches at their centers;
- an edge incident to a pad node (`pad_nodes`: ownership rectangles plus
  every pad's snap/claim node) is never replaced by a diagonal, even for
  the net's own pads. KiCad's connectivity is anchor-based — a pad connects
  where its CENTER lies inside track copper — and a pad thinner than the
  pitch can carry no lattice node at all, its only contact being the body
  of the one edge that passes over its anchor. Grid geometry guarantees
  that edge is incident to the pad's snap node, so keeping those edges
  verbatim preserves every such incidental contact (observed on Voxy's
  +5v: a 0.4 mm 1206 pad strip between grid rows, disconnected by a
  chamfer whose endpoints were both preserved);
- same-net edges shared by several paths emit once (paths_to_tracks' dedup
  rule, applied BEFORE smoothing so a shared trunk is smoothed exactly once
  and duplicate edges split runs instead of double-emitting).
"""


def _protected_nodes(paths):
    """Nodes smoothing must keep on-center for the net: every path endpoint
    (pad or tree junction by construction — build_connections' MST touches
    every pad, own-tree seeding lets connections terminate mid-path) and
    every node used by >= 2 of the net's paths."""
    counts = {}
    prot = set()
    for p in paths:
        if not p:
            continue
        prot.add(p[0])
        prot.add(p[-1])
        for v in set(p):
            counts[v] = counts.get(v, 0) + 1
    prot.update(v for v, c in counts.items() if c > 1)
    return prot


def _runs(lat, path, seen_edges):
    """Split one path into same-layer runs of FRESH edges:
    [(layer_index, [(ix, iy), ...])]. A layer change (via) or an edge already
    emitted by an earlier path of the same net (dedup) ends the current run;
    the duplicate edge itself is dropped, exactly like paths_to_tracks."""
    runs = []
    cur = None
    for a, b in zip(path, path[1:]):
        ax, ay, al = lat.coords(a)
        bx, by, bl = lat.coords(b)
        if al != bl:
            cur = None
            continue
        key = (a, b) if a < b else (b, a)
        if key in seen_edges:
            cur = None
            continue
        seen_edges.add(key)
        if cur is None:
            cur = [(ax, ay)]
            runs.append((al, cur))
        cur.append((bx, by))
    return runs


def _stair_extends(steps, i, j):
    """Does steps[j] continue the monotone staircase begun at steps[i]?
    Alternating axes, consistent sign per axis."""
    dxp, dyp = steps[j - 1]
    dx, dy = steps[j]
    if (dx == 0) == (dxp == 0):
        return False              # same axis: straight, not a staircase
    if j - 2 >= i and steps[j] != steps[j - 2]:
        return False              # sign flipped within one axis: zigzag ends
    return True


def _merge_collinear(pts):
    """Drop interior vertices where the direction (any angle) continues."""
    out = [pts[0]]
    for x, y in pts[1:]:
        if len(out) >= 2:
            x0, y0 = out[-2]
            x1, y1 = out[-1]
            if (x1 - x0) * (y - y1) == (y1 - y0) * (x - x1) and \
                    (x1 - x0) * (x - x1) + (y1 - y0) * (y - y1) > 0:
                out[-1] = (x, y)
                continue
        out.append((x, y))
    return out


def _smooth_run(lat, pts, il, net, occupied, protected, pad_nodes):
    """One same-layer run of grid points -> smoothed grid-point polyline.
    Endpoints pts[0] / pts[-1] are preserved verbatim."""
    n = len(pts) - 1
    steps = [(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
             for k in range(n)]

    def diag_ok(i, k):
        """May pts[i]..pts[i+k] (k even staircase steps) become one 45?"""
        ax, ay = pts[i]
        bx, by = pts[i + k]
        h = k // 2
        if abs(bx - ax) != h or abs(by - ay) != h:
            return False
        # Every staircase edge would be replaced: none may be incident to a
        # pad node (module docstring's anchor-contact rule), so no point of
        # the collapsed range — endpoints included — may be a pad node.
        for t in range(k + 1):
            if lat.node(*pts[i + t], il) in pad_nodes:
                return False
        sx = 1 if bx > ax else -1
        sy = 1 if by > ay else -1
        for t in range(h):
            x, y = ax + t * sx, ay + t * sy
            for cx, cy in ((x + sx, y), (x, y + sy)):
                nd = lat.node(cx, cy, il)
                if occupied.get(nd, net) != net or nd in protected:
                    return False
        return True

    out = [pts[0]]
    i = 0
    while i < n:
        j = i + 1
        while j < n and _stair_extends(steps, i, j):
            j += 1
        m = j - i                     # maximal staircase length from i
        k_full = m - (m % 2)          # longest even (collapsible) prefix
        if k_full >= 2 and diag_ok(i, k_full):
            out.append(pts[i + k_full])   # whole staircase -> one 45
            i += k_full
        elif m >= 2 and diag_ok(i, 2):
            out.append(pts[i + 2])        # single-corner chamfer
            i += 2
        else:
            out.append(pts[i + 1])        # keep the 90
            i += 1
    return _merge_collinear(out)


def smooth_net_paths(lat, net_paths, occupied, pad_nodes=None):
    """RouteResult.net_paths -> net -> [(layer_name, [(x_mm, y_mm), ...])].

    occupied: node -> net_code covering EVERY node of every routed net's
    paths plus pad ownership (the caller builds it) — the legality oracle
    for corner cutting. pad_nodes: node ids of ANY pad's copper plus every
    pad's snap/claim node (lattice node_owner keys, claims merged); edges
    incident to them are never replaced, regardless of net — see the
    module docstring's anchor-contact rule. Vias and layer
    splits are preserved exactly (runs end at layer changes; no cross-layer
    polyline exists); run endpoints are unchanged; polyline vertices always
    lie on lattice nodes, so smoothed and raw geometry share their
    endpoints bit-for-bit."""
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    pad_nodes = pad_nodes if pad_nodes is not None else frozenset()
    out = {}
    for net in sorted(net_paths):
        paths = net_paths[net]
        protected = _protected_nodes(paths)
        seen_edges = set()
        polys = []
        for path in paths:
            for il, pts in _runs(lat, path, seen_edges):
                if len(pts) < 2:
                    continue
                sm = _smooth_run(lat, pts, il, net, occupied, protected,
                                 pad_nodes)
                polys.append((lat.layer_names[il],
                              [(ox + x * p, oy + y * p) for x, y in sm]))
        out[net] = polys
    return out


def polylines_to_tracks(net_polys):
    """smooth_net_paths output -> [(x1, y1, x2, y2, layer_name, net_code)],
    the same segment-tuple shape paths_to_tracks emits (diagonals allowed)."""
    tracks = []
    for net in sorted(net_polys):
        for layer, pts in net_polys[net]:
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                tracks.append((x1, y1, x2, y2, layer, net))
    return tracks
