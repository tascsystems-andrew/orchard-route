# Proposal: floorplan intent — group cohesion, flow direction, and human "direction flags"

**Date:** 2026-07-20. **From:** Andrew + AI agent, out of the Voxy placement work.
**Companion to:** `../courtyard-model-2026-07-20/FINDING.md` (the courtyard-vs-pad-bbox bug).
**Status:** design proposal, not a bug. Everything here is currently done *outside* the tool
with external scripts; this is about which pieces belong *inside* Orchard.

---

## Context — what the Voxy placement exposed

Placing area 1 (325 free parts) by fencing each signal-flow **zone** (8 groups per fence)
and letting `region.py` net-place within it produced a valid, overlap-free board that
nonetheless looked like **"rainbow confetti"**: parts of one functional group scattered
across the zone, because (a) a zone fence mixes ~8 groups and the anneal optimizes
net-weight, not group compactness, and (b) the density-driven halving split individual
groups across separate sub-runs.

Orchard *deliberately* doesn't cluster (`place.py`: "geometric/connectivity clustering is
the analog-layout anti-pattern the tool avoids") — groups are **input**, and that's the
right call. But right now the only channel for that input is `--components` + a fence.
There's no first-class way to express the rest of the human's **floorplan intent**:
*keep this group tight*, *signal flows this direction*, *this stage sits at that edge*.
This proposal is four increments on that channel, in rough priority order.

## 1. Group-cohesive placement (one fence per group)

The fix for confetti is one tight fence **per functional group**, tiled along the signal
path — small fence ⇒ cohesive block by construction. We prototyped it externally: a
treemap-style pack sizes each group's block to its courtyard share of its zone, then
`region.py --components <group> --region <block>` fills each. Result on Voxy: clean
function-colored blocks instead of confetti (renders in `placement-review/voxy_floorplan.*`).

**Ask:** a multi-group mode so this isn't N external invocations —
`region.py BOARD --groups groups.json` where `groups.json` is `{name:[refs], ...}` (+
optional per-group anchor/size). The tool places each group in its own sub-fence, holds
placed groups as frozen obstacles for later ones, and returns one board. This is just
formalizing the fence-per-group loop the driver already does by hand, but it lets the tool
own the tiling, the ordering, and the density check.

## 2. Cohesion as a placement/routing quality metric (Andrew's idea)

> "color-code the groups and have it visually solve to get the colors as cohesive as
> possible — maybe that's a really good way to check routing."

Formalize it: on the **group graph** (nodes = groups, edge weights = # of nets crossing
each group pair), a floorplan score = Σ inter-group net-length (or crossings). Two uses:
- **Score** a human/auto arrangement (a cheap, pre-route proxy for routability — tight
  same-color blocks + few long inter-color nets ≈ routes cleanly).
- **Drive** block placement: arrange the group-blocks to minimize that score (a small
  quadratic-assignment / force-directed layout on ~57 nodes — cheap), then fill (§1).

The color render *is* the human-facing version of the metric: a good floorplan looks like
solid color patches with short seams between them; confetti is the failure mode made
visible. Worth surfacing the numeric score next to the render.

## 3. Signal-flow direction must be configurable (not assumed L→R)

The Voxy **back main board (area 1) flows RIGHT→LEFT** — input jack on the right, PA output
at the left edge — the opposite of the L→R we first assumed. Direction is a physical
constraint (where the connectors/edges live), and it differs per board and potentially per
area.

**Ask:** a per-area flow-direction config — `LR | RL` (and likely `TB | BT` for
tall areas) — that orients the zone/stage ordering. One flag, but it flips the whole
floorplan; hardcoded L→R silently produces a mirror-image board. In our prototype this is a
single `AREA1_DIR='RL'` that reverses the zone x-band assignment; in-tool it belongs in the
per-area placement config or the staging annotations (§4).

## 4. Human "direction flags" during the staging pass (Andrew's idea)

> "maybe during the initial phase the human needs to put some flags on / around the board
> for direction etc."

This is the natural home for §3 and more, and it fits the existing `stage.py`
philosophy (human stages intent; tool never infers). Extend the staging pass so the human
drops **lightweight annotations on/around the board** that `stage.py --harvest` reads as
floorplan intent, e.g.:
- **flow arrows** per area (a graphic/text on a scratch layer, or a named marker) → §3
  direction;
- **stage/edge pins** — "this group hugs this edge" (PA output at the left edge, connectors
  at the board boundary);
- **orientation hints** for tall vs wide areas.

Mechanism could be as simple as text tokens on a User layer (`flow: RL`, `edge: PA_out=L`)
or named footprint "flag" markers the harvester parses — same spirit as dragging group
boxes today, just for *direction and anchoring* rather than *grouping*. Keeps the tool
free of inference while giving the placer the last piece of human intent it's currently
missing.

## Relationship to the courtyard finding

§1 (group-cohesive fills) leans hard on obstacle courtyards being correct — the same
`../courtyard-model-2026-07-20` bug makes per-group fills stack big THT on their own small
parts. Land that fix first; §1–§4 sit on top of it.

## Artifacts

External prototype (agent scratchpad / `placement-review/`): `floorplan.py` (treemap pack +
direction flag + function-color render), `voxy_floorplan.svg/.png` (the cohesive floorplan),
`voxy_wa_placed.*` (the confetti workaround it improves on).
