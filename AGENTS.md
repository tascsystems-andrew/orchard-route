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

- **Pitch rule:** pitch must be ≤ half the finest pad pitch on the board.
  0.5 mm suits 1206/0603-class SMD boards; 0.25 mm for finer parts (4× the
  nodes, slower). If you see many `pad-snap conflicts`, the pitch is too
  coarse — halve it. Halving is **not** free: the pitch also has to carry the
  board's copper, and the `geometry` line (Phase 2) states what this board's
  copper actually requires. Read it before you touch pitch.
- `--no-via-exclusion` turns off the via clearance neighbourhood. It buys
  routability with illegal copper — only for A/B measurement, never for a
  board you intend to fabricate.
- **Layers:** name the copper layers to route. Inner layers of a 4-layer board
  are usually planes — do not route signals there without being asked.
- `--no-refine` disables the post-legal shortening pass (only for debugging).
- `--width-map "GLOB=width[:via:drill]"` overrides classes per run;
  `--max-width` caps at pitch by default and WARNS naming capped nets. Both
  are resolved before the route, so the geometry contract describes the copper
  they produce — widening a via with `--width-map` costs routability, visibly,
  in the same run rather than silently at DRC.

## Phase 2 — read the result (this is the feedback loop)

The stats block is machine-readable intent:

- `geometry : pitch P | track W clearance C via V | orthogonal OK/VIOLATED
  (needs X) | diagonals ON/OFF (need Y) | vias exclude r=R | source: copper
  ...; clearance ...` — **the copper geometry contract** (`geometry.py`). The
  lattice models where copper goes; this line is the tool stating how BIG that
  copper is and what the grid must therefore be. Four numbers, all derivable
  by hand:
  - orthogonal needs `track_width + clearance`. VIOLATED means every pair of
    adjacent-node tracks the router emits is a DRC violation — the board's
    own net class does not fit its own pitch. The run continues and says so
    loudly; do not present its output as clean. Fix by widening the pitch or
    narrowing the class, not by rerouting.
  - diagonals need `sqrt(2) * (track_width + clearance)`, because a 45 passes
    a diagonally-adjacent node at `pitch/sqrt(2)`. OFF means smoothing is
    disabled for this board and you get 90-degree geometry.
  - vias claim every node within `via_size/2 + track_width/2 + clearance` on
    EVERY layer, as used copper of their net. A 0.6 mm via does not fit a
    0.5 mm grid beside anything, so this costs some routability — compare
    `nets` against a `--no-via-exclusion` run if you need the number.
    Via-to-via comes out conservative (the claim is symmetric); the summary
    prints both the enforced and the required separation.
  - `source:` is PROVENANCE, and it is load-bearing. Widths are per net, so
    there is no single global geometry: `copper` names where the numbers came
    from (project net classes, `--width-map`, a fab profile, a `--max-width`
    cap) and says `widest of N net(s)` — the contract takes the WORST copper
    among the nets this run can actually route, never an average and never
    just the Default class, because a halo sized for the average is wrong for
    the widest. `clearance` names its own source, and says plainly when it is
    a built-in fallback rather than a number the project stated. These are
    resolved BEFORE routing: whatever this line says is what `writeback.py`
    emits, and `writeback.verify_emission` refuses to write if it ever isn't.
- `fab : PROFILE | track W OK (min M) | via V OK (min M) | clearance C OK
  (min M) | verified DATE` — **the manufacturing contract** (`fab.py`),
  printed only when `--fab` names a profile. The geometry line asks "does
  this copper fit its grid?"; this line asks "will the board house etch it,
  at the price on the front page?" The two are independent: KiCad's stock
  0.6 mm via is free at every house and does not fit a 0.5 mm grid.
  - `--fab jlcpcb-standard` (default `none`, which constrains nothing).
    `standard` profiles carry each house's **no-surcharge** floor; `extended`
    profiles carry the process floor, all of which costs money. Run
    `python fab.py --compare --pitch 0.5` to see the houses side by side, and
    `python fab.py NAME` for every number with its source URL.
  - A profile FILLS copper numbers the project's net classes leave unsaid —
    a class the user wrote always wins — and CHECKS the result. Violations
    are loud and **do not change the user's numbers**.
  - `--fab-enforce` snaps to the profile's cheapest legal values and names
    every substitution. It raises copper that is too small to build, and
    shrinks a via that is buildable but too big for the pitch. It never
    narrows track width or clearance: those encode current capacity and
    creepage, which no fab profile knows.
  - `verified DATE` is when the numbers were last read off the house's site.
    A profile older than `fab.STALE_AFTER_DAYS` prints a STALE warning —
    fab tiers change, so re-read the sources rather than trusting the file.
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

## Phase 3 — verify the copper (do NOT trust kicad-cli alone)

**`kicad-cli pcb drc` stops reporting a rule type after 499 violations.** A
board at 499 `[clearance]` is *saturated*, not measured: 1,968 real violations
and 499 real violations both print as 499, so a before/after comparison across
that ceiling is meaningless. Check every count against 199/200/499 before you
quote it.

For the category this router controls — copper it emitted against other copper
it emitted — use the uncapped checker:

```sh
.venv/bin/python scripts/copper_audit.py SOURCE.kicad_pcb out/routed.kicad_pcb
```

It reports track-track / track-via / via-via violations with the worst gaps and
their coordinates, and it is the number to quote when you claim a routing run
is clean. It deliberately ignores pads, zones, the board edge, and copper that
was already in the input file.

Read the `measured :` line before you quote the `VIOLATIONS :` line. A
violation count is only meaningful next to the emitted-item count that
produced it — this checker once printed `emitted: 0 tracks, 0 vias /
VIOLATIONS: 0` for a board carrying 700+ router segments, because it
identified emitted copper by `uuid` and KiCad-5-era files (rpi-pico-vga) carry
no id on track nodes at all. It now matches on uuid, tstamp, or exact
geometry, and it **exits non-zero with `AUDIT FAILED`** rather than report a
clean bill when it finds nothing in an output larger than its source. If you
see that, you have a measurement failure, not a clean board: do not quote a
number from it. `--brute-force` re-runs the same measurement with no spatial
index (O(n²)) and reports whether the two agree — use it when a violation
count is going into a report.

## Hard rules

1. The input board is READ-ONLY. All output goes to a separate path;
   `writeback.py` refuses to write into the source board's directory — do not
   work around that refusal.
2. Never hand-edit emitted `(segment)`/`(via)` nodes; regenerate instead.
3. Current honest limitations you must disclose when relevant:
   - **Track-to-track spacing is verified, not enforced.** The `geometry`
     line proves numerically whether the pitch clears the resolved net-class
     width; when it does not, you get a loud VIOLATED warning and illegal
     copper anyway. The usage model still gives a track exactly one node.
   - **Via-to-PAD spacing is the pad ring's job, and the ring is sized for a
     track.** Ring inflate is `clearance + track_width/2`; a via wants
     `clearance + via_size/2`. Via halos deliberately do not claim pad nodes
     (a pad cannot move — claiming it would fail that pad's net outright
     rather than fix anything), so a via sitting beside a foreign pad can
     still violate by `(via_size - track_width)/2`.
   - **Pre-existing copper in the input board is not an obstacle.** The
     router sees pads, not the tracks and vias already in the file. On a
     partly-routed board, expect violations against that copper.
   - 2-layer boards currently pay a via at most direction changes (layer-model
     fix in progress); no placement optimization yet — the input placement is
     taken as given.

## Where this is going

The end state replaces Phase 1's whole-board CLI with a bounded, agent-callable
call (see ARCHITECTURE.md): `optimize_region(components, region, constraints,
k_candidates) -> ranked candidates + diagnostics`, with a closed constraint
vocabulary (`keepout`, `min_clearance`, `adjacency_max_distance`, ...). Your
Phase 0 duties stay the same — they just gain teeth: net classes and
constraints become inputs the solver enforces rather than notes it honors at
write-back. Partition by circuit function, propose, read diagnostics, revise.
The diagnostics are written for you — use them, don't retry blindly.
