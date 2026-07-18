"""Test backtrace: paths recovered from a GPU distance plane must be real paths.

Real means: starts at a source (dist==0), ends at the requested target, every
hop is an actual CSR edge, and the summed edge weights reproduce dist[target].
That last check is the strong one — a walk that cut a corner or reused a
non-relaxed edge can't match the distance the kernel computed.
"""
import numpy as np

from spike_sssp import build_lattice, gpu_sssp
from backtrace import extract_path, path_cost


def main():
    W, H, L = 24, 24, 4
    row_ptr, col_idx, weight, N, E = build_lattice(W, H, L)

    src = 0
    dist_mx, rounds = gpu_sssp(row_ptr, col_idx, weight, N, src)
    dist = np.asarray(dist_mx).astype(np.float64)
    print(f"=== backtrace on {W}x{H}x{L} ({N:,} nodes), SSSP from {src}, {rounds} rounds ===")

    rng = np.random.default_rng(42)
    reachable = np.flatnonzero(np.isfinite(dist))
    targets = [int(t) for t in rng.choice(reachable, size=8, replace=False)]

    failures = 0
    for t in targets:
        path = extract_path(dist, row_ptr, col_idx, weight, t)
        ok = True
        if dist[path[0]] != 0.0:
            ok = False
            print(f"  target {t}: FAIL — path starts at dist={dist[path[0]]}, not a source")
        if path[-1] != t:
            ok = False
            print(f"  target {t}: FAIL — path ends at {path[-1]}")
        # path_cost itself raises if any consecutive pair is not adjacent.
        cost = path_cost(path, row_ptr, col_idx, weight)
        if abs(cost - dist[t]) > 1e-3:
            ok = False
            print(f"  target {t}: FAIL — path_cost {cost:.6f} != dist {dist[t]:.6f}")
        if ok:
            print(f"  target {t:>5}: len={len(path):>3}  cost={cost:.4f}  dist={dist[t]:.4f}  OK")
        else:
            failures += 1

    # Unreachable target: an inf entry must raise, not walk.
    bad = dist.copy()
    bad[123] = np.inf
    try:
        extract_path(bad, row_ptr, col_idx, weight, 123)
        failures += 1
        print("  unreachable: FAIL — no ValueError raised")
    except ValueError as e:
        print(f"  unreachable: OK — ValueError: {e}")

    # Inconsistent plane (finite target, no downhill neighbour): must raise, not loop.
    flat = np.ones(N, dtype=np.float64)
    try:
        extract_path(flat, row_ptr, col_idx, weight, 0)
        failures += 1
        print("  stalled walk: FAIL — no ValueError raised")
    except ValueError as e:
        print(f"  stalled walk: OK — ValueError: {e}")

    print(f"  RESULT: {'PASS' if failures == 0 else 'FAIL'}")
    return failures == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
