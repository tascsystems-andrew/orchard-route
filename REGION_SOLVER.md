# optimize_region v1 — design spec

The agent-facing contract from ARCHITECTURE.md, scoped to a buildable first
version. The consumer is an AI in a KiCad design session (via the Konnect
`autoroute` toolset or the CLI): it partitions the board by circuit function —
the "playgrounding" — and calls this per region. This module never decides
*what* belongs together; it decides *where* things go inside the fence it's
given, and proves the result by routing it.

## Call

```
optimize_region(
    board_path,                 # READ-ONLY, as always
    components = ["V1","R4",...],   # reference designators allowed to move
    region     = {x, y, w, h},      # mm, board coords — the fence
    constraints = [...],            # closed vocabulary, below
    k = 5,                          # ranked candidates returned
    pitch_mm = 0.5,
    layers = ["F.Cu","B.Cu"],
    out_dir = "out/region-<slug>/",
) -> {
    candidates: [   # ranked best-first
        { id, placements: {ref: (x_mm, y_mm, rot_deg)},
          routed: nets_ok/nets_total, wirelength_mm, vias,
          constraint_violations: [...], score,
          board_copy: "<out_dir>/cand-N.kicad_pcb",   # moved + routed
          svg: "<out_dir>/cand-N.svg" },
        ...
    ],
    diagnostics: {
        infeasible_reason,        # when zero candidates route fully
        binding_constraint,       # which constraint was tight across candidates
        unrouted: [(net, blocking_ref_or_edge)],
        suggested_expansion,      # direction + mm, from boundary-pressure stats
        boundary_nets: [...],     # nets crossing the fence (pseudo-pad terminals)
    }
}
```

Pure function: writes only under `out_dir`. Applying a candidate = the caller's
explicit act (open `board_copy` in KiCad, or move footprints via Konnect IPC
using `placements`). No tool ever mutates the input board.

## Constraint vocabulary (v1 subset of the ALIGN-derived enum)

- `fixed(ref)` — in the region but may not move (e.g. the tube socket).
- `keepout(x,y,w,h)` — no component copper/courtyard inside.
- `adjacency_max_distance(ref_a, ref_b, mm)` — grid stopper stays at its pin.
- `min_distance(ref_a, ref_b, mm)` — plate resistor away from grid input.
- `orientation_set(ref, [0,90,180,270])` — restrict rotations.
- `edge(ref, side)` — jacks/pots on a board edge.
Unknown constraint names are a hard error naming the valid set — free text
invites hallucination (design rule 7).

## Mechanics

1. **Region lattice.** `lattice_for_board` over region + margin (2 mm or 4·pitch,
   whichever larger). Components OUTSIDE the region are frozen obstacles: their
   pads/claim rings enter the obstacle map exactly as in whole-board routing.
2. **Terminal propagation (design rule 3).** Every net with pads both inside and
   outside the fence gets a pseudo-pad: the outside pad nearest the boundary,
   projected to the boundary, becomes a fixed terminal at full weight. Without
   this, regions optimize as if alone and produce globally disconnected beauty.
3. **Placement search: SA generates, the router judges.**
   - State: (x, y, rot) per movable ref, snapped to a placement grid (0.5 mm),
     rejected outright on courtyard overlap or constraint violation. v1
     courtyard = pad bbox union + 0.25 mm margin (board.py doesn't parse
     courtyard layers yet — noted limitation, not silently ignored: the margin
     is reported in diagnostics).
   - Cheap energy during annealing: net-class-weighted HPWL over the region's
     nets + soft constraint penalties + boundary-terminal pull. (HPWL is a liar
     on analog boards globally — design rule 4 — but inside one functional
     region with class weights and explicit min_distance terms it's an
     acceptable explorer, BECAUSE it never gets the final word:)
   - The router gets the final word. The best K·3 distinct survivors are routed
     for real (region lattice, batched planes, same PathFinder), scored
     `failures ≫ constraint_violations ≫ wirelength + via_weight·vias`, and the
     top K ship as candidates. A placement that doesn't route never outranks one
     that does (design rule 5).
4. **Determinism.** Fixed RNG seed per call (seed parameter, default 0);
   identical calls return identical candidates.

## Surfaces

- CLI: `python region.py BOARD --components V1,R4,C8 --region x,y,w,h
  --constraint "min_distance(R4,C8,5)" ... --k 5 --out out/region-a/`
- Konnect shim: `optimize_region` tool in the `autoroute` toolset, same params,
  returns the JSON. `apply_candidate` v1 = Konnect's existing footprint-move IPC
  driven from `placements` by the calling agent, or opening the board_copy.
- AGENTS.md gains the playgrounding workflow: partition by circuit function →
  fence → constrain from the schematic's story (HV clearance, grid/plate
  separation, star-ground pulls) → call → read diagnostics → adjust fence or
  constraints → iterate. Diagnostics are the feedback loop; retrying blindly is
  the failure mode.

## Not in v1 (named so nobody pretends)

- No GPU-batched K-candidate placement scoring (SA is host-side; the GPU routes
  finalists). Batching K candidates is the Studio-era upgrade.
- No global re-optimization across committed regions; path dependence is
  mitigated by the caller revisiting regions (ARCHITECTURE.md risk section).
- No true courtyards, no 45° placement rotations, no side-swapping (F↔B).
- Track existing region copper: v1 assumes the region starts unrouted (strip
  region-internal tracks in the working copy before solving).

## Acceptance test (the Voxy gain stage)

On `Voxy-arduino.kicad_pcb`: pick one triode gain stage (~8-12 parts around one
12AX7 section), fence a plausible rectangle, constraints: tube socket fixed,
grid stopper adjacency 3 mm, plate-R min_distance from grid input 4 mm. Pass =
all k candidates fully routed with zero constraint violations, candidate #1's
region wirelength within 2× of the hand layout's for the same parts, runtime
under 3 minutes on the M4 Pro, and the diagnostics section non-empty and true.
