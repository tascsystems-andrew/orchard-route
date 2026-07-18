# One system, not two

**Decision (2026-07-17):** the placer and the router are the same project. They share
almost all their substrate, they are mutually dependent, and the agent-facing unit of
work — "optimize this group of parts in this area and route them" — does both at once.

Treating them separately would mean building the board model, the lattice, the KiCad
I/O, the constraint vocabulary and the GPU machinery twice, then discovering they
disagree.

## Why they're inseparable

- **Placement is scored by routability.** The only honest measure of a placement is
  whether it routes — so the placer *calls* the router. Everyone else uses
  half-perimeter wirelength as a proxy precisely because their router is too slow to
  put in the loop. Ours won't be, at bounded scope.
- **Routing difficulty is set by placement.** The router inherits whatever the placer
  decided. Congestion is a placement problem wearing a routing costume.
- **They share a graph.** The Manhattan lattice the router searches is the same
  structure the placer needs to estimate routability, and the same obstacle map both
  consult.

## The stack (shared unless marked)

```
  agent  ─────────────────────────────────────────────────────────
    │  optimize_region(components, region, constraints) -> candidates
    │  apply_candidate(id)                    ← separate commit step
    ▼
  L5  Region solver         place + route jointly, bounded, batched   [BOTH]
    │     ├─ placement search   annealing / gradient over positions   [placer]
    │     └─ routability eval   calls L4 per candidate                [router]
  L4  Wavefront SSSP        mx.fast.metal_kernel, pull formulation    [router core]
  L3  Constraint IR         one vocabulary for placement AND routing  [BOTH]
  L2  Lattice / graph       flat CSR in mx.array, unified memory      [BOTH]
  L1  Board + geometry      obstacles, pads, courtyards, keepouts     [BOTH]
  L0  KiCad IPC             read/write via Konnect                    [BOTH]
```

Only **L4** is router-specific. Only the *placement search* inside L5 is placer-specific.
Everything else is common — which is the argument for one codebase in one sentence.

## The agent-facing contract

This API *is* the novel artifact. Prior art covers every mechanical piece (ECO
placement with frozen obstacles, window-based batched detailed placement, Altium
rooms / SPECCTRA fences, ALIGN's analog constraint vocabulary). What nobody has built
is a bounded place-and-route solver **exposed as an agent-callable tool with
diagnostics**, partitioned by *circuit function* rather than connectivity min-cut.

```python
optimize_region(
    components:  ["V1", "R4", "C8"],     # what may move
    region:      {x, y, w, h},           # where they may go
    constraints: [...],                  # closed enum, not free text
    k_candidates: 64,                    # batch on the GPU
) -> {
    candidates: [                        # RANKED, not a single answer
        {placements, routes, metrics, violations, score},
        ...
    ],
    diagnostics: {                       # ← the actual product
        infeasible_reason,               # why nothing worked, if nothing did
        binding_constraint,              # which limit is tight
        unrouted: [(net, blocking_obstacle)],
        congestion_peak,
        suggested_expansion,             # direction + mm
        pull_residual_per_component,     # who wants to be elsewhere
    }
}

apply_candidate(candidate_id)            # pure/read-only above; this commits
```

**Diagnostics are what make the loop converge instead of flail.** An agent that only
gets a score can't do anything but retry blindly; an agent told *"net GRID1 is unrouted,
blocked by C8's courtyard; expanding the region 4 mm east would clear it"* can act.

## Design rules earned from the research

1. **Return K ranked candidates, not one placement.** Uses the GPU batch, and lets the
   agent apply judgment it cannot express as a constraint.
2. **Separate propose from commit.** `optimize_region` is read-only; `apply_candidate`
   writes through KiCad IPC and snapshots first. The agent loop will be flaky —
   rollback is not optional.
3. **Terminal propagation from day one.** Any net with pads inside *and* outside the
   region gets a fixed pseudo-pad at the outside endpoint, at full weight. Without it
   each region optimizes as if alone on the board and you get locally beautiful,
   globally disconnected layouts.
4. **Net-class-weighted objective with repulsive terms.** Functional partitioning kills
   the *global* wirelength pathology (input jack drifting toward the mains transformer)
   but NOT the local one — within one gain stage the plate node still wants distance
   from the grid input. Needs `net_class_weight` and `min_distance(net_a, net_b)`.
5. **Route every candidate before scoring it.** Returning a placement that later proves
   unroutable is the dominant failure mode of decomposed flows, and it destroys the
   agent's ability to trust any metric it's given.
6. **Soft freeze, not hard.** `displacement_budget_mm` lets out-of-region parts shift a
   little. Pure hard freeze makes the whole system path-dependent on the order of the
   first few calls, with no recovery.
7. **Closed constraint enum**, vocabulary borrowed from ALIGN: `fixed`, `symmetry`,
   `matched_group`, `adjacency_max_distance`, `keepout`, `min_clearance`,
   `orientation_set`, `layer_side`, `loop_area_max`. Free text invites hallucination.

## Known biggest risk

**Path dependence.** Each committed region constrains the next, there's no global
objective being descended, and there's no backtracking. Mitigations: soft freeze,
propose/commit with rollback, and the agent re-visiting earlier regions when a later
one reports infeasibility. This is the thing most likely to make the system produce
mediocre boards, and it is not fully solved.

## Build order

- **Now** — GPU wavefront SSSP kernel. *Done, validated against Dijkstra (`spike_sssp.py`).*
- **Next** — net batching: route many nets per launch. Everything downstream depends on
  routing being cheap, and today's scaling test showed launch latency dominates.
- **Then** — L0–L2: real board in via Konnect, real lattice, route one real net.
- **Then** — the constraint IR (L3), because it's the API everything else speaks.
- **Then** — L5 region solver, placement search last.

Placement search comes *last* deliberately: it's the piece that's worthless without a
fast router underneath it, and the piece the research was most skeptical about.
