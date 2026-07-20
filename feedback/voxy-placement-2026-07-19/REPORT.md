# Field report: placing the Voxy board with region.py — full blow-by-blow

**Date:** 2026-07-19 evening. **Board:** Voxy-arduino (3 disjoint Edge.Cuts areas on one file;
487 free parts starting off-board, 9 locked panel anchors). **Driver:** AI agent + Andrew.
**Outcome: fully placed** — but it took 6 attempts and 4 workarounds that belong in the tool.
Evidence: every referenced `run.log` is in `logs/` here, plus the partition, fences, ref
lists, and the chain scripts that finally worked. All commands ran at `--pitch 0.5
--layers F.Cu,B.Cu`, per-area/per-fence, exactly per AGENTS.md Phase 0.5.

The final composite placement (all 487 free parts verified in-area, pile empty, locked
untouched) lives in `~/Documents/Guitar/Voxy/placement-review/`.

---

## Chronology

### Run 0 — areas 0 and 2: the tool at its best
47 parts around locked S1–S5/SW2/SW3, then 43 around DS1/S6. Both: 5/5 candidates fully
routed, first try, ~2–3 min each. `auto_fixed` exactly right, `boundary_nets` exactly the
nets that should leave (I2C, GND-5, voicing nets toward the other board). No notes. This is
the baseline the rest of the report is measured against.

### Run 1 — area 1, one fence, 397 parts (`logs/area1.log`)
Infeasible in 7.1 s: *"no feasible starting placement found — C57 courtyard leaves the
region fence; … +86 more"*. Total courtyard = 13,626 mm² on a 21,600 mm² fence = **63%**.
Named offenders were the big THT parts (26.5 mm film caps, vactrols, radials).

### Run 2 — five proportional bands, chained winners (`logs/bandA..bandE.log`)
Bands cut by signal flow (input/triodes → pentode → EQ → FX → PA), widths proportional to
part count. Result: **only bandD (35% util) placed.** Per-band courtyard arithmetic after
the fact:

| band | util | outcome | note |
|---|---|---|---|
| A | **129%** | infeasible | genuinely overfull — legit rejection |
| B | 59% | infeasible | offenders: vactrols + films |
| C | 42% | infeasible | same class |
| D | 35% | **placed** | |
| E | **31%** | infeasible on TWO parts (R174/R175, 10R 3W) | not density |

A band failing at 31% utilization on two parts is not a density problem.

### Control experiment — hand-seed the two offenders
Copied the board, wrote legal in-fence positions for R174/R175 only, re-ran bandE
identically → **feasible, 3 routed candidates** (`logs/bandA_s1.log`'s sibling run; the
experiment preceded the sweeps). First hypothesis: scatter can't find spots for big parts;
seeded starts fix it.

### Run 3 — rebalanced bands + all 116 big parts pre-seeded (`logs/*_v2.log`)
Rebalanced blocks (bandA 129% → 5 bands 63% each), band widths derived by binary-searching
shelf-packing feasibility of each band's big-part set, and every part ≥50 mm² written to a
provably-legal shelf position inside its fence. Result: **only bandB.** Two observations
that took a while to untangle:

1. Failure lists named parts sitting at x = 400–620 — **the spot where KiCad's F8
   (Update PCB from Schematic) had dropped its re-added footprints.** 281 of the 397
   parts were there, not at the origin. The tool treated them as deliberately placed,
   found them outside the fence, and hard-failed. → Finding 1.
2. Failure lists ALSO named seeded parts as "courtyard leaves the region fence" when
   their file positions are verifiably centered in-fence (check `fences2.json` vs the
   seeded board). The reported positions are **post-scatter** — which reveals the seeds
   were never used. → Findings 2 and 6.

### Run 4 — 281 strays re-piled at origin (`logs/chain2.status`, second run)
Same script after sweeping every out-of-area part to (0,0). Cluster refs vanished from the
failure lists (fix confirmed) but bands A/C/D/E still failed, still naming in-fence seeded
parts as out-of-fence. Conclusion: **input positions of named components are ignored;
everything is re-scattered from scratch.**

### Seed sweep — is it luck? (`logs/bandA_s1.log` … `bandA_s8.log`)
bandA at 57.1 mm width, seeds 1–8: **eight failures in ~56 s.** Shelf-packing proves the
same part set fits at **48.5 mm**. The scatter has a capacity gap of ≥18% versus provable
feasibility for big-part-heavy groups, and one sample per run means no recovery.

### Run 5 — two waves per band: bigs alone, then smalls (`logs/chain5.status`)
Wave 1 = only the ≥50 mm² parts (14–36 per band) into the empty fence; wave 2 = smalls with
the placed bigs frozen as obstacles (which works exactly as documented — nice).
**Every wave-1 succeeded on seed 1, all five bands.** Big-parts-first is empirically the
scatter fix. Wave-2 failed in A/D/E; bandA's failure named C13 — a 22 µF radial that my
own courtyard parser had mis-classified as small (fp_circle courtyards; my bug, see
Finding 5) so four radials were never in wave 1.

### Run 6 — radial waves + halving fallback (`logs/chain6.status`) — **done**
Radials placed in their own mini-waves; smalls re-run with an automatic split-in-half
fallback when all 6 seeds fail (D and E each needed one halving level; every half then
placed on seed 1). Final: all 397 area-1 parts placed, composite assembled with areas
0/2, 487/487 verified in-area, origin pile empty, locked anchors byte-identical.

---

## Findings and asks, ranked

### 1. Pile detection is position-exact — a routine F8 poisons the whole workflow
Free parts are only treated as "unplaced pile" when stacked at the origin. KiCad's
Update-PCB-from-Schematic drops added/re-added footprints at the cursor position, so
after any sync involving footprint changes, hundreds of parts sit at arbitrary
coordinates, read as deliberately placed, and hard-fail every fence that names them.
**Ask:** treat any unlocked footprint whose courtyard is outside every Edge.Cuts outline
as pile (or add an explicit `--pile-from-outside` / per-part unplaced flag). This was the
single biggest time sink of the night.

### 2. Named components' input positions are ignored
A component named in `--components` is re-scattered even when its current position is
legal and inside the fence. This forecloses every seeded/staged workflow: we shelf-packed
116 big parts into provably legal positions and the tool threw that information away.
**Ask:** use in-fence starting positions of named components as the scatter's initial
sample (fall back to scatter only for parts without a legal in-fence position). This also
makes iterative re-runs convergent instead of starting from zero.

### 3. Scatter is single-sample, size-blind, and gives up in 7 s
bandA: infeasible at 57.1 mm for all 8 seeds while shelf-packing fits the same set at
48.5 mm. Meanwhile **bigs-first waves succeeded on seed 1 in all five bands** — ordering
by size is empirically sufficient. **Ask:** scatter big parts first (descending courtyard
area), and retry internally (N samples) before declaring infeasibility. Fixing #2 gets
most of this for free.

### 4. Density preflight — predictable failures should be predicted
Courtyard-sum vs fence-area predicted every genuine infeasibility tonight (bandA at 129%)
and exonerated every false one (bandE at 31%). It's ~10 lines against data the tool
already has. **Ask:** print utilization per run next to `suggested_expansion`, and warn
above ~60% before the search runs. (Also: when infeasible, consider labeling the reported
part positions as *post-scatter* in the message — "courtyard leaves the region fence" for
a part whose file position is centered in-fence sent us down a long wrong path.)

### 5. Courtyard dimensions API
Half our workaround code was re-implementing courtyard bboxes outside the tool — and we
hit a real parsing trap doing it (radials' `fp_circle` courtyards, preceded by silk
shapes, lazily mis-matched → 5 big radials classified as 0603-sized). **Ask:**
`region.py BOARD --list-courtyards` → `ref, w, h, area` per footprint. It makes seeding,
staging, density checks, and partition sanity one-liners for any agent, and removes a
whole class of external-parser bugs.

### 6. `stage.py` — Andrew's staging-pass proposal (now with evidence)
Proposed flow, keeping the no-clusterer philosophy (groups are INPUT, never inferred):

1. `stage.py BOARD partition.json --out staged/` — write a board copy with each group's
   parts loosely packed in a labeled box in the off-board margin. The user opens it in
   KiCad, sees the design as groups instead of a 487-part pile, drags anything
   position-specific to its true spot, and locks it.
2. `stage.py --harvest staged.kicad_pcb` — read the human's edits back: locked-in-area
   parts → fixed anchors (existing auto-fix semantics); a group box dragged onto an area
   → that group's area assignment; untouched boxes keep the proposed assignment. Output:
   the enriched partition, ready for per-area runs.
3. Harvest prints the Finding-4 density report per area.

Note how stage.py dissolves Findings 1–2 by construction: a part in a staging box is
*known* unplaced regardless of coordinates, and harvest hands the placer explicit
starting knowledge instead of a pile convention.

---

## What already works well (keep it)

Fast infeasibility (7 s, not 7 min). Diagnostics as machine-readable JSON — every failure
tonight was diagnosed from `infeasible_reason` + `boundary_nets` + file inspection, never
from guessing. Boundary pseudo-pads: groups route toward the rest of the board, and the
area-0/2 boundary nets were exactly the schematic's spanning nets. Locked auto-fix:
correct all night, never moved a panel control. Frozen-obstacle semantics for unnamed
in-fence parts made the two-wave workflow possible without any tool change. Candidates as
proposals with routed proof: the source board was never touched across ~40 runs.

## The workaround playbook (until the asks land)

For a dense area: (1) sweep all free parts to the origin first; (2) cut fences so
courtyard utilization ≤ ~60%, sized by shelf-packing the big parts; (3) run each fence in
two waves — parts ≥50 mm² first, then the rest; (4) sweep `--seed 1..6`; (5) if a smalls
wave fails all seeds, halve the group and run the halves sequentially. Scripts:
`chain5.sh` / `chain6.sh` in this folder.

**Caution for anyone hand-seeding by sexpr edit:** pad rotations in `.kicad_pcb` are
ABSOLUTE (footprint angle + pad local angle). If you write a rotation into a footprint's
`(at …)` without adding the same delta to every pad's `(at … rot)`, pad shapes render 90°
off the body (centers stay correct, so it is easy to miss). We hit this on 30 footprints /
320 pads and repaired by delta-diff against the pristine source. It is also an argument
for Finding 2: if the tool accepted seeded starts, nobody would be editing sexpr by hand.
