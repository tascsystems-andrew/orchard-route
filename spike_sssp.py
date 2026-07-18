"""Spike: does a wavefront SSSP relaxation kernel actually work in MLX/Metal?

This is the single riskiest assumption in the router architecture, so we prove it
before building anything else. Validated against a plain CPU Dijkstra.

DESIGN NOTE — why there are no atomics here:
The research flagged "Metal has no native atomic_fetch_min for float, so distance
relaxation needs a uint-bitcast compare-exchange loop" as the hardest gotcha. That's
true for the PUSH formulation (each node scatters improvements to its neighbours,
so many threads race to write the same destination).

Flip it to the PULL formulation and the problem disappears: each thread owns one
node v, gathers over v's incoming edges, and computes
    dist[v] = min(dist[v], min over u in nbr(v) of dist[u] + w(u,v))
Each thread writes exactly one element it alone owns. No races, no atomics, no CAS.
On a symmetric lattice in-neighbours == out-neighbours, so the same CSR serves both.
"""
import mlx.core as mx
import numpy as np
import heapq
import time


# ── Build a Manhattan lattice, the shape the real router will use ─────────────
def build_lattice(W: int, H: int, L: int, seed: int = 0):
    """CSR (row_ptr, col_idx, weight) for a W x H x L Manhattan lattice.

    Horizontal edges on even layers, vertical on odd, vias between layers —
    the alternating single-direction model OrthoRoute uses.
    """
    rng = np.random.default_rng(seed)
    N = W * H * L

    def nid(x, y, l):
        return l * W * H + y * W + x

    rows = [[] for _ in range(N)]

    def link(a, b, w):
        rows[a].append((b, w))
        rows[b].append((a, w))

    for l in range(L):
        horizontal = (l % 2 == 0)
        for y in range(H):
            for x in range(W):
                u = nid(x, y, l)
                if horizontal and x + 1 < W:
                    link(u, nid(x + 1, y, l), 1.0 + 0.1 * rng.random())
                if (not horizontal) and y + 1 < H:
                    link(u, nid(x, y + 1, l), 1.0 + 0.1 * rng.random())
                if l + 1 < L:
                    link(u, nid(x, y, l + 1), 3.0)  # via: deliberately pricier

    row_ptr = np.zeros(N + 1, dtype=np.uint32)
    for i, r in enumerate(rows):
        row_ptr[i + 1] = row_ptr[i] + len(r)
    E = int(row_ptr[-1])
    col_idx = np.zeros(E, dtype=np.uint32)
    weight = np.zeros(E, dtype=np.float32)
    for i, r in enumerate(rows):
        base = row_ptr[i]
        for k, (j, w) in enumerate(r):
            col_idx[base + k] = j
            weight[base + k] = w
    return row_ptr, col_idx, weight, N, E


# ── Ground truth ──────────────────────────────────────────────────────────────
def cpu_dijkstra(row_ptr, col_idx, weight, N, source):
    dist = np.full(N, np.inf, dtype=np.float64)
    dist[source] = 0.0
    pq = [(0.0, source)]
    done = np.zeros(N, dtype=bool)
    while pq:
        d, u = heapq.heappop(pq)
        if done[u]:
            continue
        done[u] = True
        for k in range(int(row_ptr[u]), int(row_ptr[u + 1])):
            v = int(col_idx[k])
            nd = d + float(weight[k])
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


# ── The kernel under test: one pull-relaxation round ──────────────────────────
_relax = mx.fast.metal_kernel(
    name="pull_relax",
    input_names=["dist_in", "row_ptr", "col_idx", "weight"],
    output_names=["dist_out"],
    source=r"""
        uint v = thread_position_in_grid.x;
        uint n = dist_in_shape[0];
        if (v >= n) return;

        float best = dist_in[v];
        uint start = row_ptr[v];
        uint end   = row_ptr[v + 1];

        for (uint k = start; k < end; ++k) {
            float cand = dist_in[col_idx[k]] + weight[k];
            if (cand < best) best = cand;
        }
        dist_out[v] = best;
    """,
)


def gpu_sssp(row_ptr, col_idx, weight, N, source, max_rounds=100_000):
    rp = mx.array(row_ptr, dtype=mx.uint32)
    ci = mx.array(col_idx, dtype=mx.uint32)
    wt = mx.array(weight, dtype=mx.float32)

    dist = mx.full((N,), float("inf"), dtype=mx.float32)
    dist[source] = 0.0
    mx.eval(dist)

    tg = 256
    rounds = 0
    for _ in range(max_rounds):
        (nxt,) = _relax(
            inputs=[dist, rp, ci, wt],
            output_shapes=[(N,)],
            output_dtypes=[mx.float32],
            grid=(N, 1, 1),
            threadgroup=(min(tg, N), 1, 1),
        )
        # Converged when a full round improves nothing. One cheap MLX reduction —
        # no atomic "changed" flag needed inside the kernel.
        done = mx.all(nxt == dist)
        mx.eval(done, nxt)
        dist = nxt
        rounds += 1
        if bool(done):
            break
    return dist, rounds


if __name__ == "__main__":
    for (W, H, L) in [(24, 24, 4), (64, 64, 6)]:
        print(f"\n=== lattice {W}x{H}x{L} ===")
        row_ptr, col_idx, weight, N, E = build_lattice(W, H, L)
        print(f"  nodes={N:,}  edges={E:,}  avg degree={E/N:.1f}")

        src = 0
        t0 = time.time()
        ref = cpu_dijkstra(row_ptr, col_idx, weight, N, src)
        t_cpu = time.time() - t0

        t0 = time.time()
        dist, rounds = gpu_sssp(row_ptr, col_idx, weight, N, src)
        mx.eval(dist)
        t_gpu = time.time() - t0
        got = np.asarray(dist).astype(np.float64)

        finite = np.isfinite(ref)
        err = np.abs(got[finite] - ref[finite])
        n_bad = int((err > 1e-4).sum())

        print(f"  cpu dijkstra : {t_cpu*1000:8.1f} ms")
        print(f"  gpu wavefront: {t_gpu*1000:8.1f} ms  ({rounds} rounds)")
        print(f"  reachable    : {finite.sum():,}/{N:,}")
        print(f"  max error    : {err.max():.3e}   mismatches: {n_bad}")
        print(f"  RESULT: {'PASS' if n_bad == 0 else 'FAIL'}")
