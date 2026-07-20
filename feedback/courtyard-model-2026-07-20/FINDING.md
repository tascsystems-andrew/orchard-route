# Finding: placement keep-outs use the pad bbox, not the real courtyard

**Date:** 2026-07-20. **Board:** Voxy-arduino (single-sided, 496 fps). **Reporter:** AI agent + Andrew.
**Severity:** high — makes auto-placement unusable for any board with THT parts whose
body overhangs its pads (radials, box film caps, power resistors, relays).
**Relationship:** this is Finding #5 from `../voxy-placement-2026-07-19/REPORT.md`
("courtyard dimensions API"), now with a root-cause trace and a minimal repro.

---

## Symptom

Placing the Voxy board with the per-zone `region.py` workflow produced **164 overlapping
courtyard pairs across 185 of 496 parts** — parts sitting bodily on top of each other
(a 27 mm film cap over two small parts; the two 10 R 3 W power resistors fully stacked;
radial cans overlapping caps). It was not an origin pile — every part was "placed," just
on top of its neighbours.

The overlaps are **not** a density artifact. Minimal repro (`repro_bigcaps.py`): 10 large
THT parts, one `region.py` run, fence `20,2,130,60` = **28 % courtyard utilization**
(very roomy), seed 1 → **6 overlapping pairs** in the winning candidate:

```
BIG parts placed: 10/10  |  overlapping pairs among them: 6
   57.8mm2  C72 x C47     (two 13 mm radials)
   25.0mm2  C31 x C9      (radial over film cap)
   17.9mm2  C16 x C8      (radial over film cap)
   17.7mm2  R174 x R175   (two 28x7 power resistors)
    8.0mm2  C72 x C13
    3.6mm2  C72 x C9
```

Full stdout in `repro_output.txt`. `region.py` reports the run as fully routed and feasible
— the overlaps are invisible to it because its collision model never sees the bodies.

## Root cause

The placement model builds every part's keep-out from the **union of its pad bounding
boxes + 0.25 mm**, because `board.py` does not parse courtyard layers at all. This is
documented, not hidden:

- `place.py:28` — *"courtyard proxy = pad-bbox union + 0.25 mm margin, computed from
  board.py pads (board.py parses no courtyard layers yet …)"*
- `place.py:141` `_local_geometry()` — the union-of-pad-bbox that every candidate,
  feasibility check, and frozen-obstacle rect flows through (`part_courtyard`,
  `PlacementModel._geom`, `preflight`, `anneal_region`).
- `region.py:55` — *"Courtyards are the pad-bbox proxy, not the real courtyard layers,
  so the candidate can place parts closer than a real courtyard check would allow."*

For SMD/IC parts the pad bbox ≈ the body, so the proxy is fine. It fails precisely for
THT parts whose **body overhangs the pad span**. What the tool actually "sees" vs. the
real `F.CrtYd`, measured on the Voxy parts (`proxy_vs_real.py`):

| ref            | tool pad-bbox proxy | real F.CrtYd  | proxy area / real |
|----------------|--------------------:|--------------:|------------------:|
| C13/C16/C31/C72 (13 mm radial) | 7.9 × 2.9 = 22.9 | 13.0 × 13.0 = 169.0 | **14 %** |
| C8/C9/C28 (27 mm box film)     | 25.4 × 2.9 = 73.7 | 27.0 × 11.0 = 297.0 | **25 %** |
| R174/R175 (28 × 7 power R)     | 28.3 × 2.9 = 82.1 | 28.3 × 6.9 = 195.3 | **42 %** |
| KBC3T1 (relay)                 | 20.9 × 2.9 = 60.6 | 20.9 × 5.5 = 114.9 | **53 %** |
| U4 (SOIC)                      | 10.5 × 9.7 = 102.3 | 10.5 × 10.7 = 112.9 | 91 % (fine) |
| OUTDRIVERP1 (relay)            | 11.3 × 11.9 = 134.0 | 12.1 × 11.9 = 143.5 | 93 % (fine) |

Two failure modes, both visible above:
1. **Radials**: two pads ~5 mm apart → a 7.9 × 2.9 strip standing in for a 13 × 13 can.
   Keep-out is 14 % of the real area; parts happily nest inside the can's footprint.
2. **Box film caps**: the proxy is only **2.9 mm tall** for an **11 mm** body, so parts
   pack right up against the cap above and below. The body also hangs asymmetrically off
   the origin, but `_rel_courtyards` (`place.py`, "treating the courtyard as centred")
   centres the proxy — so even the little keep-out there is is in the wrong place.

## Proposed fix

Parse the real courtyard and prefer it; keep the pad-bbox as the fallback for footprints
with no courtyard layer. The machinery already exists:

1. **`board.py`** — `_edge_shapes()`/`scan()` already walk `fp_line`/`fp_arc`/`fp_circle`
   for `Edge.Cuts`. Add a sibling that collects the same shapes on `F.CrtYd`/`B.CrtYd`
   per footprint and reduces them to a **local-frame** rect (rotation backed out via the
   existing `_rotate`/`_fp_frame`). Attach as `Part.local_courtyard` (None if absent).
   `fp_circle` needs the center±radius handling the Edge.Cuts path may not exercise.
2. **`place.py`** — in `_local_geometry()`, if `part.local_courtyard` is present, use it
   (still union with the pad bbox so odd footprints can't shrink below their pads) +
   `margin_mm`; else the current pad-bbox proxy. Everything downstream
   (`part_courtyard`, `_rel_courtyards`, `preflight`, obstacle rects) then gets the real
   geometry for free. `AnnealResult.courtyard_margin_mm` can stay as the fallback-margin
   diagnostic; consider adding `courtyard_source: real|pad-bbox` counts.

This also lands the `--list-courtyards` ask (Finding #5) trivially once the data exists,
and removes the need for callers to re-parse courtyards in external scripts.

## Regression test

Add a synthetic fixture with the divergence baked in and assert non-overlap:
- one footprint, two 1.6 mm THT pads 5 mm apart, a 13 × 13 mm `F.CrtYd` circle;
- place two of them in a fence that fits them side-by-side only if the real courtyard is
  honoured (e.g. a 30 × 16 fence) → assert the two courtyards do not overlap.
- Belt-and-suspenders: `repro_bigcaps.py` on a board with these parts must go **6 → 0**
  overlapping pairs. (Andrew can point it at the Voxy board; keep the fixture synthetic so
  the suite stays decoupled, per #2/#3.)

## Evidence in this folder

- `repro_bigcaps.py` / `repro_output.txt` — the 10-part, 28 %-density repro (6 overlaps).
- `proxy_vs_real.py` — the proxy-vs-real table generator (uses `place._local_geometry`).

## Workaround in use (so layout can proceed)

Hand-anchor the ~21 THT bodies (positions computed against the real courtyard), freeze
them, let `region.py` place only the well-modeled small parts, then a real-courtyard census
nudges any residual small-on-body overlaps out. It is a band-aid — frozen bodies are
under-modeled too — but it unblocks the Voxy layout while the fix lands.
