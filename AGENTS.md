# Orchard Route — guide for AI agents driving the tool

You are the design engineer, not a button-pusher. Orchard Route handles the
geometry; every decision that encodes *circuit intent* is yours, and most of
them happen BEFORE anything is placed or routed. This guide is the contract.

## Phase 0 — design setup (yours, before any routing)

**Define net classes first.** The router emits track widths, via sizes, and
(eventually) clearances from the project's net classes (`.kicad_pro`,
`net_settings`). A board where every net is `Default` is a design decision
nobody made. From the schematic's circuit function, define classes such as:

| class    | typical nets                    | why |
|----------|--------------------------------|-----|
| Power    | B+, heater/filament, mains sec | current capacity — wide tracks, big vias |
| HV       | plate supplies, PI outputs     | clearance — creepage rules the width |
| Audio    | grid/plate signal path         | keep thin; short matters more than wide |
| Default  | logic, LEDs, misc              | fine as-is |

Assign nets to classes by pattern or explicitly, in the `.kicad_pro`. Do this
by editing the project file (it is JSON), or ask the user to do it in KiCad's
Board Setup → Net Classes. If you route a board whose every net is `Default`,
say so in your report — silence implies the widths were considered.

**Sanity-check the placement** (until placement search exists, the input
placement is authoritative): note anything that will make routing lie —
an input jack beside a power transformer is a layout bug no router fixes.
Report it; do not silently route around bad placement.

## Phase 1 — route

```sh
.venv/bin/python pathfinder.py BOARD.kicad_pcb --pitch 0.5 --layers F.Cu,B.Cu --svg out/routed.svg
.venv/bin/python writeback.py  BOARD.kicad_pcb out/routed.kicad_pcb --pitch 0.5 --layers F.Cu,B.Cu
```

- **Pitch rule:** pitch must be ≤ half the finest pad pitch on the board, and
  the pitch is also the de-facto clearance today. 0.5 mm suits 1206/0603-class
  SMD boards; 0.25 mm for finer parts (4× the nodes, slower). If you see many
  `pad-snap conflicts`, the pitch is too coarse — halve it.
- **Layers:** name the copper layers to route. Inner layers of a 4-layer board
  are usually planes — do not route signals there without being asked.
- `--no-refine` disables the post-legal shortening pass (only for debugging).
- `--width-map "GLOB=width[:via:drill]"` overrides classes per run;
  `--max-width` caps at pitch by default and WARNS naming capped nets.

## Phase 2 — read the result (this is the feedback loop)

The stats block is machine-readable intent:

- `nets X routable | Y fully routed | Z with failures` — Y/X is the score.
- `overuse [...]` — collisions per iteration. Healthy runs fall monotonically
  to 0. A long plateau then a spike then 0 is the stall escape working. Ending
  above 0 means greedy legalization dropped nets — treat as failure.
- `pad-snap conflicts` — two nets sharing one grid node: pitch too coarse.
- Failed-net reasons and your correct response:
  - `target unreachable (hard-blocked or walled off)` — pad geometry problem,
    NOT congestion. Check pitch vs the part's pad pitch; check for pad overlaps
    the board didn't intend. Rerouting harder will not help.
  - `congestion unresolved after N iterations` — genuine capacity shortage:
    finer pitch, another layer, or (when regions exist) more room.
- `refine : path cost -X%` — slack recovered after negotiation; absence means
  refine was skipped (non-legal end state).

Never present a partially-failed route as done. Report score, failures with
reasons, and your proposed remedy for each.

## Hard rules

1. The input board is READ-ONLY. All output goes to a separate path;
   `writeback.py` refuses to write into the source board's directory — do not
   work around that refusal.
2. Never hand-edit emitted `(segment)`/`(via)` nodes; regenerate instead.
3. Current honest limitations you must disclose when relevant: clearance is
   approximated by grid pitch (no per-class clearance yet); 2-layer boards
   currently pay a via at most direction changes (layer-model fix in
   progress); no placement optimization yet — the input placement is taken
   as given.

## Where this is going

The end state replaces Phase 1's whole-board CLI with a bounded, agent-callable
call (see ARCHITECTURE.md): `optimize_region(components, region, constraints,
k_candidates) -> ranked candidates + diagnostics`, with a closed constraint
vocabulary (`keepout`, `min_clearance`, `adjacency_max_distance`, ...). Your
Phase 0 duties stay the same — they just gain teeth: net classes and
constraints become inputs the solver enforces rather than notes it honors at
write-back. Partition by circuit function, propose, read diagnostics, revise.
The diagnostics are written for you — use them, don't retry blindly.
