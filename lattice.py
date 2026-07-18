"""L2: board -> routing graph. Manhattan CSR lattice over a board area.

Conventions match spike_sssp.py EXACTLY: node id = l*W*H + y*W + x, horizontal
edges on even layer indices, vertical on odd (the alternating single-direction
model), via edges between adjacent layers, symmetric adjacency, CSR as
(row_ptr uint32 [N+1], col_idx uint32 [E], weight float32 [E]) — ready for
batch_sssp.gpu_sssp_batch via to_mx().

Two deliberate choices:
- The lattice is built UNBLOCKED except the explicit `blocked` set (hard holes:
  board cutouts and the like). Pad ownership comes back as data (`node_owner`)
  so the router can apply per-net blocking masks — masks beat per-net CSR
  rebuilds when many nets batch over one shared graph.
- The CSR is assembled fully vectorized (edge chunks -> bincount -> argsort);
  a 240k-node board lattice must build in well under a second.
"""
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
                  base_cost=1.0, via_cost=3.0, blocked=frozenset()):
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

    for l in range(L):
        ids = plane + l * W * H
        if l % 2 == 0:
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


def lattice_for_board(board, pitch_mm, layer_names=None):
    """Board (board.py dataclass) -> (lattice over bbox+margin, pad_nodes, node_owner).

    pad_nodes: net_code -> node ids, each pad snapped to its nearest node on each
    of its copper layers present in the lattice.
    node_owner: node id -> net_code for every node whose (x,y) lies inside a
    pad's bbox on that node's layer. Ownership is DATA for per-net masking —
    those nodes keep all their CSR edges.
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
                        layer_names=layer_names)

    layer_index = {name: i for i, name in enumerate(layer_names)}
    pad_nodes = {}
    node_owner = {}
    eps = 1e-9
    for pad in board.pads:
        for name in pad.layers:
            il = layer_index.get(name)
            if il is None:
                continue
            nd = lat.snap(pad.x_mm, pad.y_mm, name)
            lst = pad_nodes.setdefault(pad.net_code, [])
            if nd not in lst:
                lst.append(nd)
            ix_lo = max(0, int(np.ceil((pad.x_mm - pad.width_mm / 2 - ox) / pitch_mm - eps)))
            ix_hi = min(W - 1, int(np.floor((pad.x_mm + pad.width_mm / 2 - ox) / pitch_mm + eps)))
            iy_lo = max(0, int(np.ceil((pad.y_mm - pad.height_mm / 2 - oy) / pitch_mm - eps)))
            iy_hi = min(H - 1, int(np.floor((pad.y_mm + pad.height_mm / 2 - oy) / pitch_mm + eps)))
            base = il * W * H
            for iy in range(iy_lo, iy_hi + 1):
                row = base + iy * W
                for ix in range(ix_lo, ix_hi + 1):
                    node_owner[row + ix] = pad.net_code

    return lat, pad_nodes, node_owner
