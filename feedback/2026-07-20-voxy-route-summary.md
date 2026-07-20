# Session summary — Voxy trial board: placement → routing (2026-07-20)

Drove Orchard Route through the Voxy multi-amp HV board from a DRC-clean placement into a
routing attempt. Two new findings this session, plus confirmations. All numbers below are from
live `kicad-cli pcb drc` and `pathfinder.py` runs on `Voxy-arduino.kicad_pcb`, not a proxy.

## What WORKS (confirmations — no change needed)

- **Per-net-class clearance is real and correct.** Omit `--clearance` and `route_board`
  resolves `net_clearance`/`net_exclusion` per net from the project classes; the geometry line
  reports `clearance project Default net class ... widest class 7.300` and 51 nets claim their
  halo. This is the right default — a global `--clearance` silently retires it (I lost an hour
  to `--clearance 0.2` flattening HV to 0.2 mm). **Suggest: warn when a global --clearance
  overrides defined net classes.**
- **Per-region routing + LV recipe.** `--region-index N` on the 3-outline panel is the honest
  unit. Region 0 (encoders, 161 pads, LV) routes **23/23, overuse→0** at **pitch 0.6mm, 4
  signal layers, diagonals on** (0.6 ≥ sqrt(2)·(0.2+0.2)=0.566). Dense LV region 2 (594 pads)
  routes 120/155 at 0.6 — needs a finer legal pitch, i.e. narrower Default track.
- Board is 4-layer, **all four routed as signal** (no plane pours, per the user).

## What BLOCKS (the two findings)

### 1. `hv-creepage-placement-2026-07-20/` — THE blocker
HV region 1 will not route even HV-alone (10/20), because **155 different-HV-net part pairs
are placed below net-class creepage** (66×3.7mm, 41×7.2mm, 48×2.0mm) + 178 intra-component
(package pins, unavoidable). Placement separated by courtyard (~0.3mm); routing needs creepage.
**Ask: placement feasibility must use `max(courtyard, per-net-class clearance)` — feed the same
`resolve_net_classes` numbers the router uses into place/anneal, and surface HV creepage the way
DRC does.** Placed ≠ routable until this closes.

### 2. `priority-tier-routing-2026-07-20/` — how HV should route once placement allows it
One simultaneous negotiation can't solve a board mixing 7.2mm-creepage HV (489-node halos) with
0.2mm logic — overuse never falls. Route **most-constrained class first** (HV_SWING→HV_300→
HV_150→Power→Default), each tier committing its copper as `node_owner` obstacles for the next.
Feasible today: `route_lattice(net_pads_subset, node_owner=...)` + `RouteResult.net_paths`
(node ids) already expose the pieces; the clean add is `route_lattice` returning its
exclusion-halo nodes so tiers chain EXACT inter-tier clearance. Monkeypatch proof-of-shape in
the finding.

## Ordering
Finding 1 gates finding 2 gates a routed HV board. Fix creepage-aware placement first (it also
wants size-rank: widest-creepage nets placed first, as rocks), then tiered routing lands it.

## Placement side (context, in the Voxy repo not here)
Placement itself is now DRC-clean for courtyard + 0.2mm pad (kicad-cli: 0 courtyards_overlap,
0 pth_inside_courtyard). The lesson that got it there: **verify with kicad-cli DRC, not a
Python courtyard/pad proxy** — a proxy missed 92 real collisions (a caller-side rotation-sign
error: KiCad rotates CW/Y-down; the proxy rotated CCW, mirroring every 90/270 part). Not an
Orchard bug — `region.py` rotates through KiCad's own transform correctly — but it is why
"verify at write time against the real DRC" (placement-fidelity §5) matters.
