# Orchard Route

**A GPU place-and-route engine for KiCad, native to Apple Silicon.**

Orchard Route routes printed circuit boards on the Mac GPU using
[MLX](https://github.com/ml-explore/mlx) and hand-written Metal kernels — no CUDA,
no cloud, no external EDA tooling in the loop. It reads a `.kicad_pcb`, negotiates
every net's copper simultaneously on the GPU, and writes back a routed board that
KiCad opens directly.

The name is a tip of the hat to
[OrthoRoute](https://github.com/bbenchoff/OrthoRoute), whose functional intent —
Manhattan-lattice GPU routing for KiCad — this project rebuilds natively for
Apple Silicon. No OrthoRoute code was ported; the architecture was redesigned
around what Metal and unified memory are actually good at.

## Status — working prototype

First full-board route (2026-07-18, MacBook Pro M4 Pro): a real 300×279 mm,
487-net amplifier board — **399 of 410 routable nets negotiated to a fully legal
routing** (zero copper overlaps) in 19 PathFinder iterations, ~29.5 m of track,
under 5 minutes end-to-end including parsing and write-back.

What works today:

- **`board.py`** — pure-Python `.kicad_pcb` parser (KiCad 9/10, no `pcbnew` needed)
- **`lattice.py`** — vectorized board → Manhattan CSR lattice (247k nodes in ~10 ms)
- **`wavefront.py`** — batched min-plus wavefront SSSP as an `mx.fast.metal_kernel`;
  the pull formulation needs **no atomics**, and 128 nets route per kernel launch
  (~0.5 ms per net; routing 32 nets costs the same wall-clock as routing 1)
- **`pathfinder.py`** — parallel PathFinder negotiation: batched routing against a
  shared congestion snapshot, asymmetric rip-up, windowed stall escape, guaranteed-legal
  output
- **`backtrace.py` / `render.py` / `writeback.py`** — distance fields → paths → SVG
  render → a routed *copy* of the board that `kicad-cli` loads clean

Honest limitations, today: routes are legal but not yet pretty (via-heavy — the
alternating-layer model is being replaced for 2-layer boards), clearance classes are
not yet enforced (grid pitch is the de-facto clearance), and placement optimization
is designed but not built.

## Where it's going

The destination is not "another autorouter." It's an **agent-callable
place-and-route tool**:

```
optimize_region(components, region, constraints, k_candidates)
    -> ranked candidates + diagnostics
```

— hand an AI assistant a group of components, a patch of board, and constraints in a
closed vocabulary, and it places *and* routes just that region, returning ranked
options and machine-readable diagnostics ("net X unrouted, blocked by C8's courtyard;
4 mm more room east would clear it"). Full design in [ARCHITECTURE.md](ARCHITECTURE.md).

## Requirements

- Apple Silicon Mac (developed on M4 Pro; scales with GPU cores and unified memory)
- Python 3.12+, `mlx`, `numpy`
- KiCad 9/10 board files (KiCad itself only needed if you want `kicad-cli` DRC)

## Quickstart

```sh
python -m venv .venv && .venv/bin/pip install mlx numpy
.venv/bin/python pathfinder.py your_board.kicad_pcb --pitch 0.5 --layers F.Cu,B.Cu --svg out/routed.svg
.venv/bin/python writeback.py your_board.kicad_pcb out/routed.kicad_pcb --pitch 0.5 --layers F.Cu,B.Cu
```

Your input board is never modified; write-back refuses to write into the source
board's directory.

## License

Orchard Route is **dual-licensed**:

- **AGPL-3.0-only** (see [LICENSE](LICENSE)) for open use: if you distribute it,
  or run a modified version for others over a network, you must share your source.
- **Commercial licenses** for any use the AGPL doesn't fit — see
  [COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

Contributions require a lightweight relicensing grant — see
[CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

Copyright © 2026 Andrew Sanche.
