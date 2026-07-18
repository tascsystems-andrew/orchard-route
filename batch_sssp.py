"""Net batching: many nets' SSSP per kernel launch — kills the latency wall.

scale_check.py showed the single-net wavefront is LATENCY-bound, not compute-bound:
growing the lattice 20x (24k -> 524k nodes) moved wall-clock only 71 -> 127 ms,
because every one of the few-hundred relaxation rounds pays a fixed launch+sync
cost regardless of how little work it contains. One net cannot fill the GPU.

Fix: give every launch B nets of work. dist becomes a (B, N) stack of distance
planes over ONE shared CSR lattice. Thread (v, b) owns element dist[b, v], so the
pull formulation carries over unchanged — still no atomics, still no races.

Convergence stays a single global check (all planes quiet). A plane that converges
early keeps relaxing to the same values (relaxation is idempotent at its fixed
point), so correctness is unaffected; the wasted compute on finished planes is
exactly what the latency wall was already throwing away.
"""
import time

import mlx.core as mx
import numpy as np

from spike_sssp import build_lattice, cpu_dijkstra

_relax_batch = mx.fast.metal_kernel(
    name="pull_relax_batch",
    input_names=["dist_in", "row_ptr", "col_idx", "weight"],
    output_names=["dist_out"],
    source=r"""
        uint v  = thread_position_in_grid.x;
        uint b  = thread_position_in_grid.y;
        uint n  = dist_in_shape[1];
        uint nb = dist_in_shape[0];
        if (v >= n || b >= nb) return;

        // size_t: at Studio scale B*N exceeds 2^32 (512 * 25M), uint would wrap.
        size_t base = (size_t)b * (size_t)n;
        float best = dist_in[base + v];
        uint start = row_ptr[v];
        uint end   = row_ptr[v + 1];

        for (uint k = start; k < end; ++k) {
            float cand = dist_in[base + col_idx[k]] + weight[k];
            if (cand < best) best = cand;
        }
        dist_out[base + v] = best;
    """,
)


def gpu_sssp_batch(rp, ci, wt, N, sources, max_rounds=100_000):
    """SSSP for len(sources) nets in lockstep over one shared CSR.

    rp/ci/wt are mx.arrays. `sources` is a list of source nodes, one per net —
    a multi-pin net just seeds several zeros in its plane (same list entry may
    become a list later; the kernel doesn't care, only the init does).
    """
    B = len(sources)
    d0 = np.full((B, N), np.inf, dtype=np.float32)
    for b, s in enumerate(sources):
        d0[b, s] = 0.0
    dist = mx.array(d0)
    mx.eval(dist)

    rounds = 0
    for _ in range(max_rounds):
        (nxt,) = _relax_batch(
            inputs=[dist, rp, ci, wt],
            output_shapes=[(B, N)],
            output_dtypes=[mx.float32],
            grid=(N, B, 1),
            threadgroup=(min(256, N), 1, 1),
        )
        done = mx.all(nxt == dist)
        mx.eval(done, nxt)
        dist = nxt
        rounds += 1
        if bool(done):
            break
    return dist, rounds


def _to_mx(row_ptr, col_idx, weight):
    return (
        mx.array(row_ptr, dtype=mx.uint32),
        mx.array(col_idx, dtype=mx.uint32),
        mx.array(weight, dtype=mx.float32),
    )


def correctness():
    """Every plane of a 16-net batch must match CPU Dijkstra exactly."""
    W, H, L, B = 24, 24, 4, 16
    row_ptr, col_idx, weight, N, E = build_lattice(W, H, L)
    rp, ci, wt = _to_mx(row_ptr, col_idx, weight)

    rng = np.random.default_rng(7)
    sources = [int(s) for s in rng.integers(0, N, size=B)]

    dist, rounds = gpu_sssp_batch(rp, ci, wt, N, sources)
    got = np.asarray(dist).astype(np.float64)

    bad_total = 0
    for b, s in enumerate(sources):
        ref = cpu_dijkstra(row_ptr, col_idx, weight, N, s)
        finite = np.isfinite(ref)
        err = np.abs(got[b][finite] - ref[finite])
        bad_total += int((err > 1e-4).sum())

    print(f"=== correctness: {B} nets on {W}x{H}x{L} ({N:,} nodes), {rounds} rounds ===")
    print(f"  mismatches across all {B} planes: {bad_total}")
    print(f"  RESULT: {'PASS' if bad_total == 0 else 'FAIL'}")
    return bad_total == 0


def bench():
    """Batched wall-clock vs (B x single-net) sequential baseline."""
    W, H, L = 64, 64, 6
    row_ptr, col_idx, weight, N, E = build_lattice(W, H, L)
    rp, ci, wt = _to_mx(row_ptr, col_idx, weight)
    rng = np.random.default_rng(1)

    # Warm-up absorbs Metal kernel compilation.
    gpu_sssp_batch(rp, ci, wt, N, [0, 1], max_rounds=5)

    t1s, r1 = [], 0
    for _ in range(3):
        t0 = time.perf_counter()
        _, r1 = gpu_sssp_batch(rp, ci, wt, N, [int(rng.integers(N))])
        t1s.append(time.perf_counter() - t0)
    t_single = min(t1s)

    print(f"\n=== batching: lattice {W}x{H}x{L}  N={N:,}  E={E:,} ===")
    print(f"  single net baseline: {t_single*1000:.1f} ms  ({r1} rounds)")
    print(f"  {'B':>5} {'rounds':>7} {'wall ms':>9} {'ms/net':>8} {'nets/s':>8} {'vs sequential':>14}")
    for B in [8, 32, 128, 512]:
        sources = [int(s) for s in rng.integers(0, N, size=B)]
        t0 = time.perf_counter()
        _, rounds = gpu_sssp_batch(rp, ci, wt, N, sources)
        t = time.perf_counter() - t0
        print(
            f"  {B:>5} {rounds:>7} {t*1000:>9.1f} {t/B*1000:>8.2f} "
            f"{B/t:>8.0f} {t_single*B/t:>13.1f}x"
        )


if __name__ == "__main__":
    ok = correctness()
    if ok:
        bench()
