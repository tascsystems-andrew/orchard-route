"""Tests for lattice.py: structure, blocking, build speed, GPU smoke, real board.

Run: .venv/bin/python test_lattice.py
The board test self-skips if board.py (being written concurrently) is absent.
"""
import os
import time

import numpy as np

from lattice import build_lattice, lattice_for_board

VOXY = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb"


def csr_entries(W, H, L):
    """Closed-form CSR entry count: 2 x undirected edges."""
    n_even = (L + 1) // 2
    n_odd = L // 2
    undirected = n_even * (W - 1) * H + n_odd * W * (H - 1) + W * H * (L - 1)
    return 2 * undirected


def csr_entries_both(W, H, L):
    """Closed-form CSR entry count for directions="both": every layer carries
    both horizontal and vertical edges."""
    undirected = L * ((W - 1) * H + W * (H - 1)) + W * H * (L - 1)
    return 2 * undirected


def test_structure():
    W, H, L = 5, 4, 3
    lat = build_lattice(W, H, L, pitch_mm=0.5, origin_mm=(10.0, 20.0))
    N = W * H * L
    E = csr_entries(W, H, L)
    assert lat.row_ptr.dtype == np.uint32
    assert lat.col_idx.dtype == np.uint32
    assert lat.weight.dtype == np.float32
    assert lat.row_ptr.shape == (N + 1,)
    assert int(lat.row_ptr[-1]) == E == lat.col_idx.size == lat.weight.size

    # Symmetry: the edge multiset equals its own reversal, weights included.
    h = np.repeat(np.arange(N, dtype=np.int64), np.diff(lat.row_ptr).astype(np.int64))
    t = lat.col_idx.astype(np.int64)
    fwd = np.lexsort((t, h))
    rev = np.lexsort((h, t))
    assert np.array_equal(h[fwd], t[rev]) and np.array_equal(t[fwd], h[rev])
    assert np.array_equal(lat.weight[fwd], lat.weight[rev])

    for nd in range(N):
        ix, iy, il = lat.coords(nd)
        assert lat.node(ix, iy, il) == nd
        x, y = lat.node_xy_mm(nd)
        layer = lat.layer_names[il]
        assert lat.snap(x, y, layer) == nd
        assert lat.snap(x + 0.2 * lat.pitch_mm, y - 0.2 * lat.pitch_mm, layer) == nd

    # snap clips to the lattice bounds
    assert lat.snap(-1e3, -1e3, lat.layer_names[0]) == lat.node(0, 0, 0)
    assert lat.snap(1e3, 1e3, lat.layer_names[2]) == lat.node(W - 1, H - 1, 2)
    print("structure: PASS")


def test_structure_both():
    W, H, L = 5, 4, 3
    base, pen, via = 1.0, 1.5, 3.0
    lat = build_lattice(W, H, L, base_cost=base, via_cost=via,
                        directions="both", dir_penalty=pen)
    N = W * H * L
    E = csr_entries_both(W, H, L)
    assert int(lat.row_ptr[-1]) == E == lat.col_idx.size == lat.weight.size

    # Symmetry: the edge multiset equals its own reversal, weights included.
    h = np.repeat(np.arange(N, dtype=np.int64), np.diff(lat.row_ptr).astype(np.int64))
    t = lat.col_idx.astype(np.int64)
    fwd = np.lexsort((t, h))
    rev = np.lexsort((h, t))
    assert np.array_equal(h[fwd], t[rev]) and np.array_equal(t[fwd], h[rev])
    assert np.array_equal(lat.weight[fwd], lat.weight[rev])

    # Every CSR entry classified and priced: preferred direction (horizontal
    # on even layers, vertical on odd) at base_cost, the other at base * pen,
    # vias at via_cost — checked exhaustively on every layer.
    hl, hrem = np.divmod(h, W * H)
    hy, hx = np.divmod(hrem, W)
    tl, trem = np.divmod(t, W * H)
    ty, tx = np.divmod(trem, W)
    horiz = (hl == tl) & (hy == ty) & (np.abs(hx - tx) == 1)
    vert = (hl == tl) & (hx == tx) & (np.abs(hy - ty) == 1)
    via_e = (np.abs(hl - tl) == 1) & (hx == tx) & (hy == ty)
    assert np.all(horiz | vert | via_e)  # nothing else exists
    even = hl % 2 == 0
    expected = np.where(via_e, via,
                        np.where(horiz == even, base, base * pen)).astype(np.float32)
    assert np.array_equal(lat.weight, expected)
    # ... and both classes are actually present on every layer.
    for l in range(L):
        assert np.any(horiz & (hl == l)) and np.any(vert & (hl == l))
    print("structure both: PASS")


def test_gpu_smoke_both():
    """L-shaped two-point route on a directions="both" 2-layer lattice must
    stay on one layer: 23 preferred steps + 17 penalized (23 + 17*1.25 =
    44.25) beats the via round trip (23 + 17 + 2*3 = 46) even at the compat
    via_cost of 3. Backtrace the path and demand ZERO layer changes."""
    import batch_sssp
    from backtrace import extract_path
    W, H, L = 30, 30, 2
    lat = build_lattice(W, H, L, layer_names=["F.Cu", "B.Cu"],
                        directions="both", dir_penalty=1.25)
    src = lat.snap(2.0, 3.0, "F.Cu")
    dst = lat.snap(25.0, 20.0, "F.Cu")
    rp, ci, wt = lat.to_mx()
    dist, rounds = batch_sssp.gpu_sssp_batch(rp, ci, wt, W * H * L, [src])
    dcol = np.ascontiguousarray(np.asarray(dist, dtype=np.float64)[0])
    d = float(dcol[dst])
    expected = 23 * 1.0 + 17 * 1.25
    assert np.isfinite(d)
    assert abs(d - expected) < 1e-4, (d, expected)
    path = extract_path(dcol, lat.row_ptr, lat.col_idx, lat.weight, dst)
    assert path[0] == src and path[-1] == dst
    layers = [lat.coords(n)[2] for n in path]
    vias = sum(1 for a, b in zip(layers, layers[1:]) if a != b)
    assert vias == 0, f"same-layer L-route took {vias} via(s)"
    print(f"gpu smoke both: PASS  (dist={d}, expected={expected}, "
          f"path={len(path)} nodes, vias={vias}, rounds={rounds})")


def test_blocked():
    W, H, L = 5, 4, 3
    full = build_lattice(W, H, L)
    bnode = full.node(2, 2, 0)  # degree 3: two horizontal + one via up
    lat = build_lattice(W, H, L, blocked=frozenset({bnode}))
    assert int(lat.row_ptr[bnode + 1]) == int(lat.row_ptr[bnode])
    assert not np.any(lat.col_idx == bnode)
    assert lat.col_idx.size == full.col_idx.size - 6
    print("blocked: PASS")


def test_build_speed():
    W, H, L = 405, 305, 2  # ~200x150mm board at 0.5mm pitch
    t0 = time.perf_counter()
    lat = build_lattice(W, H, L, pitch_mm=0.5)
    dt = time.perf_counter() - t0
    assert int(lat.row_ptr[-1]) == csr_entries(W, H, L)
    assert dt < 1.0, f"CSR build took {dt:.2f}s"
    print(f"build speed: PASS  ({W * H * L:,} nodes, {lat.col_idx.size:,} entries in {dt * 1000:.0f} ms)")


def test_gpu_smoke():
    import batch_sssp
    W, H, L = 30, 30, 2
    lat = build_lattice(W, H, L, layer_names=["F.Cu", "B.Cu"])
    src = lat.snap(2.0, 3.0, "F.Cu")
    dst = lat.snap(25.0, 20.0, "F.Cu")
    rp, ci, wt = lat.to_mx()
    dist, rounds = batch_sssp.gpu_sssp_batch(rp, ci, wt, W * H * L, [src])
    d = float(np.asarray(dist)[0, dst])
    # F.Cu (layer 0) is horizontal-only, so the 17 vertical steps need a
    # round trip to B.Cu: 23 + 17 planar steps + 2 vias at cost 3.
    expected = 23 * 1.0 + 17 * 1.0 + 2 * 3.0
    assert np.isfinite(d)
    assert abs(d - expected) < 1e-4, (d, expected)
    print(f"gpu smoke: PASS  (dist={d}, expected={expected}, rounds={rounds})")


def test_board():
    try:
        from board import load_board
    except ImportError:
        print("board: SKIP (board.py not present yet)")
        return
    if not os.path.exists(VOXY):
        print("board: SKIP (Voxy .kicad_pcb not found)")
        return
    board = load_board(VOXY)
    t0 = time.perf_counter()
    lat, pad_nodes, node_owner = lattice_for_board(board, pitch_mm=1.0)
    dt = time.perf_counter() - t0
    nets_with_pads = {p.net_code for p in board.pads if p.net_code != 0}
    missing = sorted(nc for nc in nets_with_pads if not pad_nodes.get(nc))
    assert not missing, f"nets with pads but no lattice nodes: {missing}"
    print(f"board: PASS  {lat.W}x{lat.H}x{lat.L} lattice, "
          f"{lat.W * lat.H * lat.L:,} nodes, {lat.col_idx.size:,} csr entries, "
          f"{len(nets_with_pads)} nets with pads, {len(node_owner):,} pad-owned nodes, "
          f"built in {dt * 1000:.0f} ms")


if __name__ == "__main__":
    test_structure()
    test_structure_both()
    test_blocked()
    test_build_speed()
    test_gpu_smoke()
    test_gpu_smoke_both()
    test_board()
