"""Backtrace: turn an SSSP distance plane into an actual routed path.

The wavefront kernel produces only distances — no predecessor array, because
storing/updating one inside the pull kernel would reintroduce the write races
the formulation exists to avoid. But predecessors are recoverable for free:
on a symmetric CSR every out-edge is an in-edge, so from any node v some
neighbour u must satisfy dist[u] + w(u,v) == dist[v] (up to float slop).
Walking that inequality greedily downhill from the target reaches a zero of
the plane — a source — in at most path-length steps. This runs on CPU: one
walk per net is trivial next to the relaxation rounds, and it needs random
access, not bandwidth.

Multi-source planes (multi-pin nets seeded with several zeros) need no special
handling — the walk stops at whichever zero it descends into.
"""
import numpy as np


def extract_path(dist, row_ptr, col_idx, weight, target, tol=1e-4, cost=None):
    """Walk downhill from `target` to a zero of `dist`; return node ids source-first.

    dist is one (N,) plane from gpu_sssp / gpu_sssp_batch (numpy). Raises
    ValueError if target is unreachable or dist is not a consistent SSSP
    fixed point (walk stalls or exceeds N steps).

    cost: optional (N,) per-node entry costs the plane was relaxed WITH
    (wavefront.batched_sssp's `cost` argument). The predecessor condition
    becomes dist[u] + w(u,v) + cost[v] <= dist[v] + tol; default None keeps
    the plain-SSSP behavior bit-for-bit.
    """
    N = dist.shape[0]
    dt = float(dist[target])
    if not np.isfinite(dt):
        raise ValueError(f"target {target} unreachable: dist[{target}] = {dt}")

    path = [int(target)]
    v = int(target)
    for _ in range(N):
        dv = float(dist[v])
        if dv == 0.0:
            path.reverse()
            return path
        cv = 0.0 if cost is None else float(cost[v])
        best_u = -1
        best_du = np.inf
        for k in range(int(row_ptr[v]), int(row_ptr[v + 1])):
            u = int(col_idx[k])
            du = float(dist[u])
            if du < dv and du + float(weight[k]) + cv <= dv + tol and du < best_du:
                best_du = du
                best_u = u
        if best_u < 0:
            raise ValueError(
                f"backtrace stalled at node {v} (dist={dv}): no neighbour u with "
                f"dist[u] + w <= dist[v] + {tol} — dist plane is not a converged SSSP"
            )
        path.append(best_u)
        v = best_u
    raise ValueError(
        f"backtrace from target {target} exceeded {N} steps without reaching a "
        f"source (dist==0) — dist plane is inconsistent (negative cycle or bad tol)"
    )


def path_cost(path, row_ptr, col_idx, weight):
    """Sum of edge weights along `path`. Raises if consecutive nodes aren't adjacent."""
    total = 0.0
    for a, b in zip(path, path[1:]):
        a, b = int(a), int(b)
        s, e = int(row_ptr[a]), int(row_ptr[a + 1])
        hits = np.nonzero(col_idx[s:e] == b)[0]
        if hits.size == 0:
            raise ValueError(f"path nodes {a} -> {b} are not adjacent in the CSR")
        # Parallel edges can't occur on the lattice, but take the min if they do —
        # that's the edge SSSP would have relaxed through.
        total += float(weight[s:e][hits].min())
    return total
