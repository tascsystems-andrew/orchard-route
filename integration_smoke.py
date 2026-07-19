"""Integration smoke: first fixture-board route through the whole stack.

L1 (board.py) -> L2 (lattice.py) -> L4 (batch_sssp.py) -> backtrace, end to end
on the committed gain-stage fixture (READ-ONLY — the file is never written).
One 2-pad net is routed pad-to-pad and the recovered path is measured in
millimetres.

DELIBERATE LIMITATION: obstacles and other nets are ignored. The lattice is
unblocked, so the route may pass straight through other pads and tracks. This
run only proves the layers compose on real data; legality comes later with
per-net masking (see lattice.py's node_owner).
"""
import os
import time

import numpy as np

import batch_sssp
from backtrace import extract_path
from board import load_board
from lattice import lattice_for_board

BOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "fixtures", "gain_stage.kicad_pcb")


def pick_two_pad_net(board, lat):
    """First net (by code) with exactly 2 pads snapping to distinct nodes."""
    by_net = {}
    for p in board.pads:
        if p.net_code > 0:
            by_net.setdefault(p.net_code, []).append(p)
    for code in sorted(by_net):
        pads = by_net[code]
        if len(pads) != 2:
            continue
        nodes = []
        for p in pads:
            layer = next((l for l in p.layers if l in lat.layer_names), None)
            if layer is not None:
                nodes.append(lat.snap(p.x_mm, p.y_mm, layer))
        if len(nodes) == 2 and nodes[0] != nodes[1]:
            return code, board.nets[code], nodes[0], nodes[1]
    raise RuntimeError("no 2-pad net with distinct lattice nodes found")


def path_length_mm(lat, path):
    """Euclidean xy length; via hops move only in l and contribute 0 mm."""
    total = 0.0
    for a, b in zip(path, path[1:]):
        ax, ay = lat.node_xy_mm(a)
        bx, by = lat.node_xy_mm(b)
        total += float(np.hypot(bx - ax, by - ay))
    return total


def main():
    t0 = time.perf_counter()
    board = load_board(BOARD)
    t_load = time.perf_counter() - t0

    t0 = time.perf_counter()
    lat, pad_nodes, node_owner = lattice_for_board(board, pitch_mm=1.0,
                                                   layer_names=["F.Cu", "B.Cu"])
    t_lat = time.perf_counter() - t0
    N = lat.W * lat.H * lat.L

    code, name, src, dst = pick_two_pad_net(board, lat)

    t0 = time.perf_counter()
    rp, ci, wt = lat.to_mx()
    dist, rounds = batch_sssp.gpu_sssp_batch(rp, ci, wt, N, [src])
    dist0 = np.asarray(dist)[0].astype(np.float64)
    t_sssp = time.perf_counter() - t0  # includes one-time Metal kernel compile

    t0 = time.perf_counter()
    path = extract_path(dist0, lat.row_ptr, lat.col_idx, lat.weight, dst)
    t_back = time.perf_counter() - t0

    length = path_length_mm(lat, path)
    print(f"board   : {os.path.basename(BOARD)}  "
          f"({len(board.pads)} pads, {len(board.nets)} nets)")
    print(f"lattice : {lat.W}x{lat.H}x{lat.L}  N={N:,}  E={lat.col_idx.size:,}  "
          f"pitch {lat.pitch_mm} mm on {lat.layer_names}")
    print(f"net     : {name!r} (code {code})  node {src} -> {dst}")
    print(f"path    : {len(path)} nodes  {length:.1f} mm")
    print(f"timing  : load {t_load*1000:.0f} ms | lattice {t_lat*1000:.0f} ms | "
          f"sssp {t_sssp*1000:.0f} ms ({rounds} rounds) | backtrace {t_back*1000:.0f} ms")


if __name__ == "__main__":
    main()
