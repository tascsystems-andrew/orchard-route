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
import math

import numpy as np


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

    if blocked:
        bmask = np.zeros(N, dtype=bool)
        bmask[np.fromiter(blocked, dtype=np.int64, count=len(blocked))] = True
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


def _pads_overlap(p, q):
    """True copper rects of two pads intersect (separating-axis test over both
    rects' edge normals, rotation respected)."""
    def corners(r):
        t = math.radians(r.rotation_deg)
        c, s = math.cos(t), math.sin(t)
        hw, hh = r.width_mm / 2, r.height_mm / 2
        return [(r.x_mm + lx * c + ly * s, r.y_mm - lx * s + ly * c)
                for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]

    pc, qc = corners(p), corners(q)
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


def lattice_for_board(board, pitch_mm, layer_names=None, directions="both",
                      via_cost=8.0, dir_penalty=1.25):
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
    """
    if layer_names is None:
        layer_names = ["F.Cu", "B.Cu"]
    margin = 2.0 * pitch_mm
    ox = board.origin_mm[0] - margin
    oy = board.origin_mm[1] - margin
    W = int(np.ceil((board.size_mm[0] + 2.0 * margin) / pitch_mm)) + 1
    H = int(np.ceil((board.size_mm[1] + 2.0 * margin) / pitch_mm)) + 1
    L = len(layer_names)
    lat = build_lattice(W, H, L, pitch_mm=pitch_mm, origin_mm=(ox, oy),
                        layer_names=layer_names, directions=directions,
                        via_cost=via_cost, dir_penalty=dir_penalty)

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
