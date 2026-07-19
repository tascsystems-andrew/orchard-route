"""L2: board -> routing graph. Manhattan CSR lattice over a board area.

Conventions match spike_sssp.py EXACTLY: node id = l*W*H + y*W + x, via edges
between adjacent layers, symmetric adjacency, CSR as (row_ptr uint32 [N+1],
col_idx uint32 [E], weight float32 [E]) — ready for batch_sssp.gpu_sssp_batch
via to_mx().

Two direction models (build_lattice `directions`):
- "alternating" (default): horizontal edges on even layer indices, vertical on
  odd — the single-direction-per-layer model spike_sssp.py established. Right
  for many-layer backplanes; on a 2-layer board it forces a via at every turn.
- "both": every layer gets BOTH horizontal and vertical edges. The layer's
  PREFERRED direction (even index -> horizontal, odd -> vertical) costs
  base_cost; the other costs base_cost * dir_penalty, so nets still sort
  themselves by direction where it's contested but a corner is a corner, not
  a layer change.

Two deliberate choices:
- The lattice is built UNBLOCKED except the explicit `blocked` set (hard holes:
  board cutouts and the like). Pad ownership comes back as data (`node_owner`)
  so the router can apply per-net blocking masks — masks beat per-net CSR
  rebuilds when many nets batch over one shared graph.
- The CSR is assembled fully vectorized (edge chunks -> bincount -> argsort);
  a 240k-node board lattice must build in well under a second.
"""
import json
import math
import os
from dataclasses import dataclass, field

import numpy as np

# Spacing the ring model guarantees between routed copper centerlines and
# foreign pad copper when no project net class is readable. Matches KiCad's
# stock Default clearance (0.2 mm) and writeback.DEFAULT_TRACK_MM (0.25 mm).
DEFAULT_CLEARANCE_MM = 0.2
DEFAULT_TRACK_WIDTH_MM = 0.25


class Lattice:
    def __init__(self, row_ptr, col_idx, weight, W, H, L, pitch_mm, origin_mm, layer_names):
        self.row_ptr = row_ptr
        self.col_idx = col_idx
        self.weight = weight
        self.W = W
        self.H = H
        self.L = L
        self.pitch_mm = pitch_mm
        self.origin_mm = origin_mm
        self.layer_names = layer_names

    def node(self, ix, iy, il):
        return il * self.W * self.H + iy * self.W + ix

    def coords(self, node):
        il, rem = divmod(int(node), self.W * self.H)
        iy, ix = divmod(rem, self.W)
        return (ix, iy, il)

    def node_xy_mm(self, node):
        ix, iy, _ = self.coords(node)
        return (self.origin_mm[0] + ix * self.pitch_mm,
                self.origin_mm[1] + iy * self.pitch_mm)

    def snap(self, x_mm, y_mm, layer_name):
        il = self.layer_names.index(layer_name)
        ix = int(np.floor((x_mm - self.origin_mm[0]) / self.pitch_mm + 0.5))
        iy = int(np.floor((y_mm - self.origin_mm[1]) / self.pitch_mm + 0.5))
        ix = min(max(ix, 0), self.W - 1)
        iy = min(max(iy, 0), self.H - 1)
        return self.node(ix, iy, il)

    def to_mx(self):
        import mlx.core as mx
        return (
            mx.array(self.row_ptr, dtype=mx.uint32),
            mx.array(self.col_idx, dtype=mx.uint32),
            mx.array(self.weight, dtype=mx.float32),
        )


def build_lattice(W, H, L, pitch_mm=1.0, origin_mm=(0.0, 0.0), layer_names=None,
                  base_cost=1.0, via_cost=3.0, blocked=frozenset(),
                  directions="alternating", dir_penalty=1.25):
    if directions not in ("alternating", "both"):
        raise ValueError(f"directions must be 'alternating' or 'both', "
                         f"got {directions!r}")
    if layer_names is None:
        layer_names = [f"L{i}" for i in range(L)]
    if len(layer_names) != L:
        raise ValueError(f"{len(layer_names)} layer names for L={L}")
    N = W * H * L
    plane = np.arange(W * H, dtype=np.int64).reshape(H, W)  # plane[y, x] = y*W + x

    us, vs, ws = [], [], []

    def add(a, b, cost):
        a, b = a.ravel(), b.ravel()
        us.append(a)
        vs.append(b)
        ws.append(np.full(a.size, cost, dtype=np.float32))

    penalized = base_cost * dir_penalty
    for l in range(L):
        ids = plane + l * W * H
        if directions == "both":
            # Preferred direction at base_cost, the other at base * dir_penalty.
            h_cost = base_cost if l % 2 == 0 else penalized
            v_cost = penalized if l % 2 == 0 else base_cost
            add(ids[:, :-1], ids[:, 1:], h_cost)
            add(ids[:-1, :], ids[1:, :], v_cost)
        elif l % 2 == 0:
            add(ids[:, :-1], ids[:, 1:], base_cost)   # horizontal on even layers
        else:
            add(ids[:-1, :], ids[1:, :], base_cost)   # vertical on odd layers
        if l + 1 < L:
            add(ids, ids + W * H, via_cost)

    u = np.concatenate(us)
    v = np.concatenate(vs)
    w = np.concatenate(ws)

    if blocked is not None and len(blocked):
        idx = blocked if isinstance(blocked, np.ndarray) else \
            np.fromiter(blocked, dtype=np.int64, count=len(blocked))
        bmask = np.zeros(N, dtype=bool)
        bmask[idx] = True
        keep = ~(bmask[u] | bmask[v])
        u, v, w = u[keep], v[keep], w[keep]

    # Symmetric adjacency: every undirected edge appears in both rows.
    heads = np.concatenate([u, v])
    tails = np.concatenate([v, u])
    wboth = np.concatenate([w, w])
    order = np.argsort(heads, kind="stable")
    row_ptr = np.concatenate(
        ([0], np.cumsum(np.bincount(heads, minlength=N)))
    ).astype(np.uint32)
    col_idx = tails[order].astype(np.uint32)
    weight = wboth[order]

    return Lattice(row_ptr, col_idx, weight, W, H, L,
                   pitch_mm, tuple(origin_mm), list(layer_names))


def pad_rect_nodes(lat, pad, layer_name):
    """Node ids under a pad's TRUE rectangle (width x height at rotation_deg)
    on one layer. Iterates the rotated rect's axis-aligned bbox, then keeps
    only nodes inside the rect proper — claiming the whole bbox over-claims
    for rotated pads and can swallow a small neighbor pad entirely."""
    il = lat.layer_names.index(layer_name)
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    eps = 1e-9
    t = math.radians(getattr(pad, "rotation_deg", 0.0))
    c, s = math.cos(t), math.sin(t)
    hw, hh = pad.width_mm / 2, pad.height_mm / 2
    bx = abs(hw * c) + abs(hh * s)   # rotated rect's half-extents in board frame
    by = abs(hw * s) + abs(hh * c)
    ix_lo = max(0, int(np.ceil((pad.x_mm - bx - ox) / p - eps)))
    ix_hi = min(lat.W - 1, int(np.floor((pad.x_mm + bx - ox) / p + eps)))
    iy_lo = max(0, int(np.ceil((pad.y_mm - by - oy) / p - eps)))
    iy_hi = min(lat.H - 1, int(np.floor((pad.y_mm + by - oy) / p + eps)))
    base = il * lat.W * lat.H
    tol = 1e-6  # mm: nodes exactly on the pad edge stay inside
    out = []
    for iy in range(iy_lo, iy_hi + 1):
        dy = oy + iy * p - pad.y_mm
        for ix in range(ix_lo, ix_hi + 1):
            dx = ox + ix * p - pad.x_mm
            # board -> pad frame: inverse of board.py's _rotate (CCW, Y-down)
            lx = dx * c - dy * s
            ly = dx * s + dy * c
            if abs(lx) <= hw + tol and abs(ly) <= hh + tol:
                out.append(base + iy * lat.W + ix)
    return out


def _pad_corners(r):
    """Board-frame corners of a pad's true rotated rect (KiCad CCW, Y-down)."""
    t = math.radians(r.rotation_deg)
    c, s = math.cos(t), math.sin(t)
    hw, hh = r.width_mm / 2, r.height_mm / 2
    return [(r.x_mm + lx * c + ly * s, r.y_mm - lx * s + ly * c)
            for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def _pads_overlap(p, q):
    """True copper rects of two pads intersect (separating-axis test over both
    rects' edge normals, rotation respected)."""
    pc, qc = _pad_corners(p), _pad_corners(q)
    for rect in (p, q):
        t = math.radians(rect.rotation_deg)
        for ax, ay in ((math.cos(t), -math.sin(t)), (math.sin(t), math.cos(t))):
            pp = [x * ax + y * ay for x, y in pc]
            qq = [x * ax + y * ay for x, y in qc]
            if max(pp) <= min(qq) + 1e-9 or max(qq) <= min(pp) + 1e-9:
                return False
    return True


def pad_overlap_allowances(board, lat):
    """net_code -> node ids that net may cross despite another net's ownership.

    Real boards (Voxy above all) OVERLAP different-net pads on purpose: 0603s
    nested inside DIP LongPads, SMD pads centered on through-hole pads, header
    pads longer than their pin spacing. Where two pads' true copper rects
    intersect on a shared layer, the input board already joins those nets
    electrically at that spot — routing either net across either pad there
    creates no connection the fab won't. Refusing passage is what breaks:
    the smaller pad sits entirely inside the bigger one's copper, every
    surrounding node is owned by the bigger net, and the small pad's net
    becomes unroutable at any pitch. So each net of an overlapping pair is
    allowed through BOTH pads' nodes; all other nets still see hard walls.
    """
    # 4mm spatial hash: max pad half-diagonal seen in the wild is ~1.8mm, so
    # overlapping centers are never more than one cell apart.
    cell = 4.0
    grid = {}
    for pad in board.pads:
        if not any(ln in lat.layer_names for ln in pad.layers):
            continue
        key = (int(pad.x_mm // cell), int(pad.y_mm // cell))
        grid.setdefault(key, []).append(pad)

    def nodes_of(pad):
        out = set()
        for ln in pad.layers:
            if ln in lat.layer_names:
                out.update(pad_rect_nodes(lat, pad, ln))
        return out

    allow = {}
    for (cx, cy), pads in grid.items():
        neighborhood = []
        for kx in (cx, cx + 1):
            for ky in (cy - 1, cy, cy + 1):
                if (kx, ky) >= (cx, cy):  # half-neighborhood: each pair once
                    neighborhood.append(grid.get((kx, ky), []))
        for i, p in enumerate(pads):
            for cellpads in neighborhood:
                for q in (cellpads[i + 1:] if cellpads is pads else cellpads):
                    if p.net_code == q.net_code:
                        continue
                    if abs(p.x_mm - q.x_mm) > cell or abs(p.y_mm - q.y_mm) > cell:
                        continue
                    if not set(p.layers) & set(q.layers):
                        continue
                    if not _pads_overlap(p, q):
                        continue
                    shared = nodes_of(p) | nodes_of(q)
                    for net in (p.net_code, q.net_code):
                        if net > 0:
                            allow.setdefault(net, set()).update(shared)
    return allow


def pad_ring_nodes(lat, pad, layer_name, inflate_mm):
    """Node ids OUTSIDE a pad's true rotated rect but within inflate_mm of its
    boundary, on one layer — the clearance ring. A route whose centerline
    enters this ring puts copper closer than (inflate - track_width/2) to the
    pad; with inflate = clearance + width/2 that is a DRC clearance violation.
    Same bbox-then-exact-test structure as pad_rect_nodes, with the exact test
    being distance-to-rect in the pad frame instead of containment."""
    il = lat.layer_names.index(layer_name)
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    eps = 1e-9
    t = math.radians(getattr(pad, "rotation_deg", 0.0))
    c, s = math.cos(t), math.sin(t)
    hw, hh = pad.width_mm / 2, pad.height_mm / 2
    bx = abs(hw * c) + abs(hh * s) + inflate_mm
    by = abs(hw * s) + abs(hh * c) + inflate_mm
    ix_lo = max(0, int(np.ceil((pad.x_mm - bx - ox) / p - eps)))
    ix_hi = min(lat.W - 1, int(np.floor((pad.x_mm + bx - ox) / p + eps)))
    iy_lo = max(0, int(np.ceil((pad.y_mm - by - oy) / p - eps)))
    iy_hi = min(lat.H - 1, int(np.floor((pad.y_mm + by - oy) / p + eps)))
    base = il * lat.W * lat.H
    tol = 1e-6  # mm, matching pad_rect_nodes: a boundary node counts as inside
    r2 = (inflate_mm + tol) ** 2
    out = []
    for iy in range(iy_lo, iy_hi + 1):
        dy = oy + iy * p - pad.y_mm
        for ix in range(ix_lo, ix_hi + 1):
            dx = ox + ix * p - pad.x_mm
            lx = dx * c - dy * s   # board -> pad frame (see pad_rect_nodes)
            ly = dx * s + dy * c
            ex = max(abs(lx) - hw, 0.0)
            ey = max(abs(ly) - hh, 0.0)
            if ex <= tol and ey <= tol:
                continue           # inside the rect: ownership's territory
            if ex * ex + ey * ey <= r2:
                out.append(base + iy * lat.W + ix)
    return out


def _pt_seg_dist(pt, a, b):
    """Point-to-segment distance in mm."""
    px, py = pt
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    if L2 <= 1e-18:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / L2))
    return math.hypot(px - (ax + u * vx), py - (ay + u * vy))


def _rect_gap(p, q):
    """Minimum copper-to-copper distance between two pads' true rotated
    rects; 0.0 when they overlap. Convex-polygon distance is attained
    vertex-to-edge (vertex-vertex included as segment endpoints), checked in
    both directions."""
    if _pads_overlap(p, q):
        return 0.0
    pc, qc = _pad_corners(p), _pad_corners(q)
    best = float("inf")
    for A, B in ((pc, qc), (qc, pc)):
        for pt in A:
            for i in range(4):
                d = _pt_seg_dist(pt, B[i], B[(i + 1) % 4])
                if d < best:
                    best = d
    return best


def default_copper_rules(board_path):
    """(clearance_mm, track_width_mm) of the sibling .kicad_pro's Default net
    class, with DEFAULT_CLEARANCE_MM / DEFAULT_TRACK_WIDTH_MM filling any
    hole (no project, malformed JSON, missing/zero values). Deliberately
    minimal duplicate of the resolution in writeback.load_net_class_widths
    (the full per-net resolver): the ring model needs ONE representative
    clearance and width for the whole board, and importing the L0 output
    module from L2 would invert the layer stack. Clearance comes from the
    project because DRC will enforce the PROJECT's number, not ours —
    icebreaker-bitsy's Default class asks 0.15 mm, and walling 0.2 mm on its
    0.25 mm lattice would hard-block both neighbor lanes of every pad."""
    clearance, width = DEFAULT_CLEARANCE_MM, DEFAULT_TRACK_WIDTH_MM
    pro = os.path.splitext(board_path)[0] + ".kicad_pro"
    try:
        with open(pro, encoding="utf-8") as f:
            classes = (json.load(f).get("net_settings") or {}).get("classes") or []
        for cls in classes:
            if isinstance(cls, dict) and cls.get("name") == "Default":
                for key, fallback in (("clearance", clearance),
                                      ("track_width", width)):
                    v = cls.get(key)
                    if isinstance(v, (int, float)) and not isinstance(v, bool) \
                            and v > 0:
                        if key == "clearance":
                            clearance = float(v)
                        else:
                            width = float(v)
                break
    except (OSError, ValueError):
        pass
    return clearance, width


@dataclass
class Clearance:
    """Per-net soft-obstacle map for real spacing, parallel to node_owner —
    NEVER merged into it (same-net pads must still connect; a pad's own ring
    must never block its own net).

    node_net: node -> claiming net for every clearance-ring node (nodes within
        inflate_mm of a pad's rect boundary, outside the rect, not pad-owned),
        or -1 when the node is claimed by two different nets' rings or by the
        board-edge band — those are hard for EVERY net. Masks block a node for
        net n iff node_net[node] not in (n,).
    soft_allow: net -> nodes that net may cross at a HIGH SOFT COST instead of
        a hard wall: corridors between pad pairs closer than 2*inflate_mm
        (fine-pitch escapes that a hard ring would make impossible).
    free_allow: net -> nodes that net may cross freely: the rings of pad pairs
        whose copper already OVERLAPS in the input board (pad_overlap_allowances
        rationale — the board joins those nets on purpose).
    degraded_pairs / edge_nodes: reporting counters.
    """
    node_net: dict = field(default_factory=dict)
    soft_allow: dict = field(default_factory=dict)
    free_allow: dict = field(default_factory=dict)
    degraded_pairs: int = 0
    edge_nodes: int = 0
    inflate_mm: float = 0.0       # the DEFAULT class's ring inflation
    max_inflate_mm: float = 0.0   # the widest class's, when clearance is
                                  # resolved per net class; else == inflate_mm


MIN_REGION_MM = 1.0  # a real board area is at least this on each side


def board_outline_regions(board, min_side_mm=MIN_REGION_MM):
    """[OutlineRegion-like] for a board, never empty.

    Falls back to ONE region spanning origin_mm/size_mm for boards whose
    outline could not be read and for the synthetic Boards that tests and
    region.py build by hand — so every caller can loop over regions without
    branching on whether the board knew about them.

    Degenerate outlines (a stray Edge.Cuts line or point — zero width or
    height) cannot hold a board and are dropped, because a zero-area region
    breaks `--area` and reads as a fourth "board" that isn't one. Callers
    that want to warn the user about the stray graphic use
    `board_outline_regions_all()` to see what was filtered."""
    kept = [r for r in board_outline_regions_all(board)
            if r.size_mm[0] >= min_side_mm and r.size_mm[1] >= min_side_mm]
    if kept:
        return kept
    return board_outline_regions_all(board)  # all degenerate: don't lie, return them


def board_outline_regions_all(board):
    """Every outline region including degenerate ones (see board_outline_regions)."""
    regions = getattr(board, "outline_regions", None)
    if regions:
        return list(regions)
    from board import OutlineRegion
    return [OutlineRegion(origin_mm=tuple(board.origin_mm),
                          size_mm=tuple(board.size_mm), shapes=0)]


def _region_depth(regions, xs, ys):
    """(H, W) array: distance from each grid point INTO the nearest region
    that contains it, negative outside every region.

    One region reproduces the old single-bbox `din` exactly. Several regions
    take the elementwise MAX, which is what makes the empty band between two
    panelised boards read as outside-the-board rather than as interior: no
    region claims it, so its depth is the (negative) distance to the closest
    outline."""
    depth = None
    for r in regions:
        x0, y0 = r.origin_mm
        x1 = x0 + r.size_mm[0]
        y1 = y0 + r.size_mm[1]
        d = np.minimum(np.minimum(xs - x0, x1 - xs)[None, :],
                       np.minimum(ys - y0, y1 - ys)[:, None])
        depth = d if depth is None else np.maximum(depth, d)
    return depth


def clearance_map(board, lat, node_owner, pad_nodes,
                  clearance_mm=None, track_width_mm=None,
                  clearance_by_net=None):
    """Build the Clearance structure for a board on its lattice.

    inflate = clearance + track_width/2: the centerline keep-out that makes a
    track's EDGE respect `clearance` against pad copper. Both numbers default
    to the project Default class (default_copper_rules); the width is capped
    at the pitch, mirroring writeback's emit-time cap — copper wider than
    the pitch is refused there too.

    clearance_by_net (net_code -> clearance_mm) makes the inflation PER PAD
    NET, which is what a board carrying an HV class actually needs: a plate
    node's pad must hold foreign copper 1.0 mm away while a logic pad two
    millimetres along is fine at 0.15 mm. One global number cannot say that,
    and saying it with the global number is how a declared HV class became
    decoration. Absent (the default) every pad inflates by the same
    `clearance_mm`, bit-identical to the pre-per-class model.

    RESIDUAL, stated rather than hidden: the ring belongs to the PAD's class,
    so HV pad vs any copper is enforced at the HV number, but HV TRACK vs a
    logic pad is enforced at the logic pad's number. Track-vs-track spacing
    for the HV net is enforced by its exclusion halo in pathfinder; a pad
    cannot move, so widening every pad's ring to the widest class on the
    board would wall in nets that have no HV anywhere near them.

    Rules:
    - ring nodes claim their pad's net; nodes already in node_owner are
      skipped (ownership wins; rings and ownership stay disjoint);
    - a net<=0 pad (unconnected copper, mounting holes) claims -1: hard for
      every routed net;
    - two DIFFERENT nets claiming one node -> -1;
    - overlapping different-net pads (the pad_overlap_allowances case) grant
      each involved net free passage through BOTH rings;
    - non-overlapping pads with rect gap < 2*inflate can have NO legal lane
      between them at all: the ring intersection (the corridor) degrades to
      soft for the two pads' nets, counted in degraded_pairs;
    - every node within inflate of ANY outline region's boundary, and every
      node outside every region (the lattice margin, and the empty band
      between two panelised boards) is claimed -1, except pad-owned/snap
      nodes.
    """
    pro_clearance, pro_width = default_copper_rules(board.path)
    if clearance_mm is None:
        clearance_mm = pro_clearance
    if track_width_mm is None:
        track_width_mm = pro_width
    track_width_mm = min(track_width_mm, lat.pitch_mm)
    half_w = track_width_mm / 2.0
    inflate = clearance_mm + half_w
    clr = Clearance(inflate_mm=inflate)
    node_net = clr.node_net

    def inflate_of(pad):
        if not clearance_by_net:
            return inflate
        return float(clearance_by_net.get(pad.net_code, clearance_mm)) + half_w

    max_inflate = inflate
    if clearance_by_net:
        max_inflate = max([inflate]
                          + [float(v) + half_w for v in clearance_by_net.values()])
    clr.max_inflate_mm = max_inflate

    ring_cache = {}

    def ring_of(pad):
        key = id(pad)
        got = ring_cache.get(key)
        if got is None:
            got = set()
            infl = inflate_of(pad)
            for ln in pad.layers:
                if ln in lat.layer_names:
                    got.update(n for n in pad_ring_nodes(lat, pad, ln, infl)
                               if n not in node_owner)
            ring_cache[key] = got
        return got

    live = [pad for pad in board.pads
            if any(ln in lat.layer_names for ln in pad.layers)]
    for pad in live:
        claim = pad.net_code if pad.net_code > 0 else -1
        for n in ring_of(pad):
            prev = node_net.get(n)
            if prev is None:
                node_net[n] = claim
            elif prev != claim:
                node_net[n] = -1

    # Pad-pair scan: same 4 mm spatial hash as pad_overlap_allowances (max pad
    # half-diagonal in the wild ~1.8 mm; 2*inflate adds well under the cell).
    # A wide net class pushes 2*inflate past that, so the cell grows with it —
    # pairs further apart than one cell are skipped, and skipping a pair only
    # forgoes a soft-degrade allowance, which is the conservative direction.
    cell = max(4.0, 2.0 * max_inflate + 2.0)
    grid = {}
    for pad in live:
        key = (int(pad.x_mm // cell), int(pad.y_mm // cell))
        grid.setdefault(key, []).append(pad)

    def allow(table, net, nodes):
        if net > 0 and nodes:
            table.setdefault(net, set()).update(nodes)

    for (cx, cy), pads in grid.items():
        neighborhood = []
        for kx in (cx, cx + 1):
            for ky in (cy - 1, cy, cy + 1):
                if (kx, ky) >= (cx, cy):  # half-neighborhood: each pair once
                    neighborhood.append(grid.get((kx, ky), []))
        for i, p in enumerate(pads):
            for cellpads in neighborhood:
                for q in (cellpads[i + 1:] if cellpads is pads else cellpads):
                    if p.net_code == q.net_code and p.net_code > 0:
                        continue      # same net: no clearance between them
                    if p.net_code <= 0 and q.net_code <= 0:
                        continue      # no routable net involved: nothing to allow
                    if abs(p.x_mm - q.x_mm) > cell or abs(p.y_mm - q.y_mm) > cell:
                        continue
                    if not set(p.layers) & set(q.layers):
                        continue
                    gap = _rect_gap(p, q)
                    if gap >= inflate_of(p) + inflate_of(q):
                        continue
                    if gap == 0.0:
                        # Board-intended overlap: both nets pass both rings
                        # freely (crossing there creates no short the fab
                        # won't — see pad_overlap_allowances).
                        both = ring_of(p) | ring_of(q)
                        allow(clr.free_allow, p.net_code, both)
                        allow(clr.free_allow, q.net_code, both)
                        continue
                    # No lane between these pads can clear both rings: the
                    # corridor (ring intersection) degrades to SOFT for the
                    # two involved nets so fine-pitch escapes stay possible.
                    corridor = ring_of(p) & ring_of(q)
                    if corridor:
                        allow(clr.soft_allow, p.net_code, corridor)
                        allow(clr.soft_allow, q.net_code, corridor)
                        clr.degraded_pairs += 1

    # Board-edge band: nodes within inflate of ANY outline region's boundary,
    # and every node outside every region — copper there is a copper_edge
    # violation, or is off the board entirely. On a PANEL that second clause
    # is the whole fix: the empty band between two boards belongs to no
    # region, so it reads as off-board and stops being routable. Pad-owned
    # and pad-snap nodes are exempt: the pad copper already lives there in
    # the input board, and walling a pad in would fail its whole net for a
    # violation the router did not create.
    ox, oy = lat.origin_mm
    xs = ox + np.arange(lat.W, dtype=np.float64) * lat.pitch_mm
    ys = oy + np.arange(lat.H, dtype=np.float64) * lat.pitch_mm
    din = _region_depth(board_outline_regions(board), xs, ys)   # (H, W)
    plane = np.flatnonzero((din < inflate - 1e-9).ravel())
    exempt = set(node_owner)
    for nodes in pad_nodes.values():
        exempt.update(nodes)
    band = set()
    for il in range(lat.L):
        base = il * lat.W * lat.H
        for n in plane:
            nd = base + int(n)
            if nd in exempt:
                continue
            band.add(nd)
            if node_net.get(nd) != -1:
                node_net[nd] = -1
                clr.edge_nodes += 1
    # Edge clearance outranks every pad-pair allowance: a corridor that only
    # exists inside the edge band is not a legal escape.
    for table in (clr.soft_allow, clr.free_allow):
        for net in list(table):
            table[net] -= band
            if not table[net]:
                del table[net]
    return clr


def _inter_region_nodes(board, W, H, L, pitch_mm, origin_mm):
    """Lattice nodes that are not on ANY board, for a multi-region file.

    A .kicad_pcb holding several disjoint outlines is a PANEL of separate
    boards, and the space between them is air. The lattice spans the union
    bbox, so without this every one of those empty nodes is a routable node
    and the router draws copper across nothing — measured on Voxy-arduino,
    where 27 nets have pads on two different boards and were "routed"
    through a 20 mm gap.

    Returns an int64 array of blocked node ids (empty for a single-region or
    outline-less board, so nothing about those changes). Nodes under a PAD
    are never blocked: a footprint may legitimately hang over its outline,
    and blocking its copper would fail that pad's net for a fault the router
    did not create — the same exemption the edge band already makes.
    """
    regions = board_outline_regions(board)
    if len(regions) < 2:
        return np.empty(0, dtype=np.int64)
    ox, oy = origin_mm
    xs = ox + np.arange(W, dtype=np.float64) * pitch_mm
    ys = oy + np.arange(H, dtype=np.float64) * pitch_mm
    keep = _region_depth(regions, xs, ys) >= -1e-9      # (H, W), on a board
    for pad in board.pads:
        t = math.radians(getattr(pad, "rotation_deg", 0.0))
        c, s = math.cos(t), math.sin(t)
        hw, hh = pad.width_mm / 2.0, pad.height_mm / 2.0
        bx = abs(hw * c) + abs(hh * s)
        by = abs(hw * s) + abs(hh * c)
        ix0 = max(0, int(np.ceil((pad.x_mm - bx - ox) / pitch_mm - 1e-9)))
        ix1 = min(W - 1, int(np.floor((pad.x_mm + bx - ox) / pitch_mm + 1e-9)))
        iy0 = max(0, int(np.ceil((pad.y_mm - by - oy) / pitch_mm - 1e-9)))
        iy1 = min(H - 1, int(np.floor((pad.y_mm + by - oy) / pitch_mm + 1e-9)))
        if ix0 <= ix1 and iy0 <= iy1:
            keep[iy0:iy1 + 1, ix0:ix1 + 1] = True
    plane = np.flatnonzero((~keep).ravel())
    if not plane.size:
        return np.empty(0, dtype=np.int64)
    return (plane[None, :] + (np.arange(L, dtype=np.int64) * (W * H))[:, None]
            ).ravel()


def lattice_for_board(board, pitch_mm, layer_names=None, directions="both",
                      via_cost=12.0, dir_penalty=1.25,
                      block_between_regions=True):
    """Board (board.py dataclass) -> (lattice over bbox+margin, pad_nodes, node_owner).

    Board-routing defaults deliberately differ from build_lattice's compat
    defaults: directions="both" (a same-layer corner must not cost a via —
    the 2-layer case) and via_cost=8.0 (a via is ~8 grid steps of pain — 12 provably livelocks
    negotiation on Voxy (single-node standoff, net pair 80/82); final calibration
    belongs to the bench fleet, not one board:
    drill cost, reliability, and it blocks BOTH layers).

    pad_nodes: net_code -> node ids, each pad snapped to its nearest node on each
    of its copper layers present in the lattice.
    node_owner: node id -> net_code for every node whose (x,y) lies inside a
    pad's true rotated rectangle on that node's layer. Where two pads' rects
    overlap a node, the pad whose CENTER is nearest wins — last-write-wins
    entombs the loser (a pad with zero owned nodes cannot escape its own
    footprint).
    Ownership is DATA for per-net masking — those nodes keep all their CSR edges.

    block_between_regions: on a board whose file holds SEVERAL disjoint
    Edge.Cuts outlines (a panel), hard-block every node that lies on none of
    them, so the router cannot route through the air between two boards
    (_inter_region_nodes). No effect on a single-outline board. False
    restores the pre-panel lattice for A/B measurement only.
    """
    if layer_names is None:
        layer_names = ["F.Cu", "B.Cu"]
    margin = 2.0 * pitch_mm
    ox = board.origin_mm[0] - margin
    oy = board.origin_mm[1] - margin
    W = int(np.ceil((board.size_mm[0] + 2.0 * margin) / pitch_mm)) + 1
    H = int(np.ceil((board.size_mm[1] + 2.0 * margin) / pitch_mm)) + 1
    L = len(layer_names)
    blocked = _inter_region_nodes(board, W, H, L, pitch_mm, (ox, oy)) \
        if block_between_regions else np.empty(0, dtype=np.int64)
    lat = build_lattice(W, H, L, pitch_mm=pitch_mm, origin_mm=(ox, oy),
                        layer_names=layer_names, directions=directions,
                        via_cost=via_cost, dir_penalty=dir_penalty,
                        blocked=blocked)

    layer_index = {name: i for i, name in enumerate(layer_names)}
    pad_nodes = {}
    node_owner = {}
    owner_d2 = {}
    for pad in board.pads:
        for name in pad.layers:
            if name not in layer_index:
                continue
            nd = lat.snap(pad.x_mm, pad.y_mm, name)
            lst = pad_nodes.setdefault(pad.net_code, [])
            if nd not in lst:
                lst.append(nd)
            for n in pad_rect_nodes(lat, pad, name):
                nx, ny = lat.node_xy_mm(n)
                d2 = (nx - pad.x_mm) ** 2 + (ny - pad.y_mm) ** 2
                if d2 < owner_d2.get(n, float("inf")):
                    owner_d2[n] = d2
                    node_owner[n] = pad.net_code

    return lat, pad_nodes, node_owner
