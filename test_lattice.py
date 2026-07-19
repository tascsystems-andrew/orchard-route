"""Tests for lattice.py: structure, blocking, build speed, GPU smoke, real board.

Run: .venv/bin/python test_lattice.py
The board test self-skips if board.py (being written concurrently) is absent.
"""
import math
import os
import time

import numpy as np

from lattice import build_lattice, clearance_map, lattice_for_board, pad_ring_nodes

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


def _fixture_board():
    """Synthetic in-memory Board exercising every clearance_map rule."""
    from board import Board, Pad

    def pad(x, y, w, h, net, rot=0.0, layers=("F.Cu",)):
        return Pad(x, y, list(layers), net, f"N{net}", w, h, False, 0.0, rot)

    pads = [
        pad(5.0, 8.0, 1.6, 1.1, 1),         # A: isolated, axis-aligned
        pad(15.0, 8.0, 1.6, 1.1, 2),        # B: isolated, far from A
        pad(5.0, 12.0, 0.8, 0.8, 3),        # D ┐ gap 0.2 mm < 2*inflate:
        pad(6.0, 12.0, 0.8, 0.8, 4),        # E ┘ the degraded pair
        pad(12.0, 12.0, 1.6, 1.1, 5),       # F ┐ true copper overlap:
        pad(12.0, 12.2, 0.8, 0.8, 6),       # G ┘ the free-allow pair
        pad(10.0, 4.2, 1.0, 1.0, 0),        # H: unconnected copper -> -1 ring
                                            # (rect off-grid so ring nodes exist:
                                            # an aligned 1.0 pad on the 0.5 grid
                                            # has NO node within 0.325 of it)
        pad(16.0, 4.0, 0.8, 0.8, 7),        # I ┐ same net, gap 0.2: never
        pad(17.0, 4.0, 0.8, 0.8, 7),        # J ┘ degraded, never -1
        pad(16.0, 12.0, 1.6, 0.8, 8, rot=30.0),  # R: rotated ring audit
    ]
    return Board(path="/nonexistent/clearance-fixture.kicad_pcb",
                 origin_mm=(0.0, 0.0), size_mm=(20.0, 16.0),
                 copper_layers=["F.Cu", "B.Cu"],
                 nets={i: f"N{i}" for i in range(9)},
                 pads=pads, tracks=[], vias=[])


def _pad_dist(pad, x, y):
    """Independent point-to-rotated-rect distance (mm): corners from the
    hand-derived KiCad rotation (CCW, Y-down — test_board.py's convention),
    inside via cross-product signs, outside via point-segment distance."""
    t = math.radians(pad.rotation_deg)
    c, s = math.cos(t), math.sin(t)
    hw, hh = pad.width_mm / 2, pad.height_mm / 2
    cs = [(pad.x_mm + lx * c + ly * s, pad.y_mm - lx * s + ly * c)
          for lx, ly in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
    signs = []
    for i in range(4):
        ax, ay = cs[i]
        bx, by = cs[(i + 1) % 4]
        signs.append((bx - ax) * (y - ay) - (by - ay) * (x - ax))
    if all(v >= -1e-12 for v in signs) or all(v <= 1e-12 for v in signs):
        return 0.0
    best = float("inf")
    for i in range(4):
        ax, ay = cs[i]
        bx, by = cs[(i + 1) % 4]
        vx, vy = bx - ax, by - ay
        u = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) /
                         (vx * vx + vy * vy)))
        best = min(best, math.hypot(x - (ax + u * vx), y - (ay + u * vy)))
    return best


def test_clearance_map():
    brd = _fixture_board()
    lat, pad_nodes, node_owner = lattice_for_board(
        brd, pitch_mm=0.5, layer_names=["F.Cu", "B.Cu"])
    clr = clearance_map(brd, lat, node_owner, pad_nodes)
    # no .kicad_pro exists -> DEFAULT_TRACK_WIDTH_MM 0.25 -> 0.2 + 0.125
    inflate = clr.inflate_mm
    assert abs(inflate - 0.325) < 1e-9, inflate

    # Rings and ownership are disjoint; no ring node claims its own interior.
    assert not set(clr.node_net) & set(node_owner)

    # Ring audit, brute force, on the axis-aligned isolated pad A and the
    # rotated pad R: every F.Cu node strictly outside the rect but within
    # inflate — and not pad-owned — must be in pad_ring_nodes, none other.
    for pad in (brd.pads[0], brd.pads[9]):
        got = set(pad_ring_nodes(lat, pad, "F.Cu", inflate))
        expect = set()
        for iy in range(lat.H):
            for ix in range(lat.W):
                x = lat.origin_mm[0] + ix * lat.pitch_mm
                y = lat.origin_mm[1] + iy * lat.pitch_mm
                d = _pad_dist(pad, x, y)
                if 1e-6 < d <= inflate - 1e-6:
                    expect.add(lat.node(ix, iy, 0))
        # brute force skips the +/-1e-6 boundary shell on purpose: geometry
        # in the fixture keeps node distances off the exact boundary.
        assert expect <= got, sorted(got.symmetric_difference(expect))[:8]
        for n in got - expect:
            ix, iy, _ = lat.coords(n)
            d = _pad_dist(pad, lat.origin_mm[0] + ix * lat.pitch_mm,
                          lat.origin_mm[1] + iy * lat.pitch_mm)
            assert 0.0 < d <= inflate + 1e-6, (lat.coords(n), d)
        # ... and A's unowned ring nodes actually claim net 1 (or -1 where a
        # neighbor also reaches — none exists for A).
        if pad is brd.pads[0]:
            for n in got:
                if n not in node_owner:
                    assert clr.node_net.get(n) == 1, (lat.coords(n),
                                                      clr.node_net.get(n))

    # Degraded pair D/E (nets 3/4, gap 0.2 < 2*inflate, no overlap): both
    # nets get the SAME soft corridor, the corridor is claimed -1 (both
    # rings), and the pair is counted.
    assert clr.degraded_pairs >= 1
    assert clr.soft_allow.get(3) and clr.soft_allow.get(3) == clr.soft_allow.get(4)
    for n in clr.soft_allow[3]:
        assert clr.node_net.get(n) == -1, (lat.coords(n), clr.node_net.get(n))
    corridor_node = lat.snap(5.5, 12.0, "F.Cu")   # midway between D and E
    assert corridor_node in clr.soft_allow[3]

    # Overlap pair F/G (nets 5/6): free passage through both rings, no
    # degrade count, no soft price.
    assert clr.free_allow.get(5) and clr.free_allow.get(6)
    assert 5 not in clr.soft_allow and 6 not in clr.soft_allow

    # Unconnected pad H (net 0): ring claimed -1, hard for everyone.
    n_h = lat.snap(10.0, 5.0, "F.Cu")             # 0.3 below H's rect edge
    assert clr.node_net.get(n_h) == -1

    # Same-net pair I/J (net 7 twice, gap 0.2): their shared corridor keeps
    # the plain net-7 claim — never -1, never degraded.
    n_ij = lat.snap(16.5, 4.0, "F.Cu")
    assert clr.node_net.get(n_ij) == 7
    assert 7 not in clr.soft_allow

    # Board edge: the bbox boundary and the margin beyond are -1 on every
    # layer; the first interior node past the band is unclaimed.
    for il, ln in enumerate(lat.layer_names):
        assert clr.node_net.get(lat.snap(0.0, 8.0, ln)) == -1     # on the edge
        assert clr.node_net.get(lat.snap(-1.0, 8.0, ln)) == -1    # margin
        assert lat.snap(1.0, 8.0, ln) not in clr.node_net         # d=1.0 > inflate
    assert clr.edge_nodes > 0

    print(f"clearance map: PASS  inflate={inflate} mm  "
          f"{len(clr.node_net)} claimed nodes ({clr.edge_nodes} edge)  "
          f"degraded_pairs={clr.degraded_pairs}  "
          f"corridor={sorted(lat.coords(n) for n in clr.soft_allow[3])}")


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
    test_clearance_map()
    test_board()
