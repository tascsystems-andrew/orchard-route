"""Production batched wavefront SSSP — the L4 kernel PathFinder drives.

Successor to batch_sssp.py (kept as the validated reference), incorporating
every measured finding from the adversarial kernel reviews:

- (N,B) layout, batch as the fast axis: threads in a simdgroup share v and
  stride b, so the gather of dist[col_idx[k]] is contiguous across the batch.
  Measured 1.22-1.26x at B=512 over (B,N).
- Convergence checked every `check_every` rounds, K launches chained lazily
  with ONE eval per block: the per-round mx.all + bool() sync roughly doubled
  per-round cost (B=128 to convergence: 50ms -> 23ms, bit-exact results).
- Init built in MLX directly — batch_sssp's numpy (B,N) intermediate would be
  a second 53GB allocation at Studio scale (26M nodes x 512 planes) and OOM
  a 128GB machine. Here the only full-size host-visible arrays never exist.
- Returns an explicit `converged` flag: max_rounds exhaustion previously
  returned partially-relaxed distances indistinguishable from real ones.
- Threadgroup ~256 total (measured flat from 128-1024; 64 is -14%).

New capability for the negotiation loop (L3/L5):
- cost:    (N,) float32 added on ENTERING a node — base + history + present
           congestion. Shared across the batch (congestion is global).
- blocked: (N,B) uint8 per-plane hard mask — nodes owned by OTHER nets' pads.
           A blocked node keeps its init value (inf) forever. Callers must not
           block a plane's own source/target nodes.
- Multi-source planes: sources[b] may be a list — every entry seeds 0. That is
  how a partially-routed net's whole tree becomes the source set.
"""
import time

import mlx.core as mx
import numpy as np

_relax = mx.fast.metal_kernel(
    name="pull_relax_nb",
    input_names=["dist_in", "row_ptr", "col_idx", "weight", "cost", "blocked"],
    output_names=["dist_out"],
    source=r"""
        uint b  = thread_position_in_grid.x;
        uint v  = thread_position_in_grid.y;
        uint n  = dist_in_shape[0];
        uint nb = dist_in_shape[1];
        if (b >= nb || v >= n) return;

        // size_t: v*nb exceeds 2^32 at Studio scale (26M nodes x 512 planes).
        size_t idx = (size_t)v * nb + b;
        if (blocked[idx] != 0) { dist_out[idx] = dist_in[idx]; return; }

        float best = dist_in[idx];
        float c    = cost[v];
        uint start = row_ptr[v];
        uint end   = row_ptr[v + 1];

        for (uint k = start; k < end; ++k) {
            float cand = dist_in[(size_t)col_idx[k] * nb + b] + weight[k] + c;
            if (cand < best) best = cand;
        }
        dist_out[idx] = best;
    """,
)


def batched_sssp(
    rp,
    ci,
    wt,
    N,
    sources,
    cost=None,
    blocked=None,
    check_every=8,
    max_rounds=100_000,
):
    """Min-plus wavefront over B planes of one shared CSR.

    sources: list of length B; each entry an int node id or an iterable of
    node ids (multi-source). Returns (dist (N,B) mx.float32, rounds, converged).
    Distances include `cost` at every entered node (sources excluded, standard).
    """
    B = len(sources)
    if cost is None:
        cost = mx.zeros((N,), dtype=mx.float32)
    if blocked is None:
        blocked = mx.zeros((N, B), dtype=mx.uint8)

    v_idx, b_idx = [], []
    for b, srcs in enumerate(sources):
        if isinstance(srcs, (int, np.integer)):
            srcs = (srcs,)
        for s in srcs:
            v_idx.append(int(s))
            b_idx.append(b)

    dist = mx.full((N, B), float("inf"), dtype=mx.float32)
    dist[mx.array(v_idx, dtype=mx.uint32), mx.array(b_idx, dtype=mx.uint32)] = 0.0
    mx.eval(dist)

    tx = min(32, B)
    tg = (tx, max(1, 256 // tx), 1)

    rounds = 0
    while rounds < max_rounds:
        prev = dist
        for _ in range(check_every):
            (dist,) = _relax(
                inputs=[dist, rp, ci, wt, cost, blocked],
                output_shapes=[(N, B)],
                output_dtypes=[mx.float32],
                grid=(B, N, 1),
                threadgroup=tg,
            )
        done = mx.all(dist == prev)
        mx.eval(done, dist)
        rounds += check_every
        if bool(done):
            return dist, rounds, True
    return dist, rounds, False


def _selftest():
    from spike_sssp import build_lattice
    from batch_sssp import gpu_sssp_batch, _to_mx

    W, H, L = 64, 64, 6
    row_ptr, col_idx, weight, N, E = build_lattice(W, H, L)
    rp, ci, wt = _to_mx(row_ptr, col_idx, weight)
    rng = np.random.default_rng(3)

    # 1. Exact agreement with the validated reference. min() and identical
    # per-candidate additions round identically, so layout must not change bits.
    B = 32
    srcs = [int(s) for s in rng.integers(0, N, size=B)]
    ref, _ = gpu_sssp_batch(rp, ci, wt, N, srcs)          # (B, N)
    got, rounds, conv = batched_sssp(rp, ci, wt, N, srcs)  # (N, B)
    exact = bool(mx.all(mx.transpose(got) == ref))
    print(f"reference agreement : {'PASS' if exact and conv else 'FAIL'} "
          f"(bit-exact={exact}, converged={conv}, {rounds} rounds)")

    # 2. Multi-source plane == elementwise min of the single-source planes.
    trio = [int(s) for s in rng.integers(0, N, size=3)]
    singles, _, _ = batched_sssp(rp, ci, wt, N, trio)
    multi, _, _ = batched_sssp(rp, ci, wt, N, [trio])
    err = float(mx.max(mx.abs(mx.min(singles, axis=1) - multi[:, 0])))
    print(f"multi-source        : {'PASS' if err < 1e-4 else 'FAIL'} (max err {err:.2e})")

    # 3. Blocking: wall off x==32 on every layer except one gap -> detour;
    # wall with no gap -> unreachable, and converged (not round-exhausted).
    def wall(gap_y):
        blk = np.zeros((N, 1), dtype=np.uint8)
        for l in range(L):
            for y in range(H):
                if y == gap_y:
                    continue
                blk[l * W * H + y * W + 32, 0] = 1
        return mx.array(blk)

    src, tgt = 0, H * W - 1  # (0,0,l0) -> (63,63,l0)
    open_d, _, c1 = batched_sssp(rp, ci, wt, N, [src])
    gap_d, _, c2 = batched_sssp(rp, ci, wt, N, [src], blocked=wall(gap_y=0))
    shut_d, _, c3 = batched_sssp(rp, ci, wt, N, [src], blocked=wall(gap_y=-1))
    o, g, s = (float(x[tgt, 0]) for x in (open_d, gap_d, shut_d))
    ok = c1 and c2 and c3 and o < g < float("inf") and np.isinf(s)
    print(f"blocking            : {'PASS' if ok else 'FAIL'} "
          f"(open {o:.1f} < gapped {g:.1f} < walled {s})")

    # 4. Node costs shift distances: +10 on every node in column x==32 must
    # make any crossing path pay, so dist rises unless a detour exists (none
    # does — the column spans the board), and rises by >= one crossing.
    cost = np.zeros(N, dtype=np.float32)
    for l in range(L):
        for y in range(H):
            cost[l * W * H + y * W + 32] = 10.0
    cost_d, _, c4 = batched_sssp(rp, ci, wt, N, [src], cost=mx.array(cost))
    cd = float(cost_d[tgt, 0])
    ok = c4 and cd >= o + 10.0 - 1e-3  # float32 slop on an exact +10 crossing
    print(f"node cost           : {'PASS' if ok else 'FAIL'} ({o:.1f} -> {cd:.1f})")

    # 5. Convergence wall-clock vs the reference at B=128.
    B = 128
    srcs = [int(s) for s in rng.integers(0, N, size=B)]
    batched_sssp(rp, ci, wt, N, srcs[:2], max_rounds=8)  # warm
    t0 = time.perf_counter()
    gpu_sssp_batch(rp, ci, wt, N, srcs)
    t_ref = time.perf_counter() - t0
    t0 = time.perf_counter()
    _, r, _ = batched_sssp(rp, ci, wt, N, srcs)
    t_new = time.perf_counter() - t0
    print(f"bench B={B}         : {t_ref*1000:.1f} ms -> {t_new*1000:.1f} ms "
          f"({t_ref/t_new:.2f}x, {r} rounds)")


if __name__ == "__main__":
    _selftest()
