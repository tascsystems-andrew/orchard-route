# Finding: placement must honor NET-CLASS clearance (HV creepage), not just courtyard — it is the routing blocker

**Date:** 2026-07-20. **From:** AI agent, out of the Voxy trial-board route.
**Companion to:** `../priority-tier-routing-2026-07-20/`, `../placement-fidelity-2026-07-20/`.
This is the one that actually stops the HV board from routing.

## The symptom

Voxy region 1 (main analog, 300V board) will not route — not one-pass (70/254), and not even
the **HV nets alone** on an otherwise empty board (10/20, overuse pinned, never converges).
No router setting fixes it, because it is not a routing problem.

## The cause — placement spaced by courtyard, routing needs creepage

The placer (and my post-passes) separate parts by **courtyard clearance** (~0.3 mm). But two
parts on **different HV nets** need **net-class creepage** between their copper: HV_150 2.0 mm,
HV_300 3.7 mm, HV_SWING 7.2 mm. KiCad DRC on the placed board:

```
HV clearance violations:
  178  intra-component  (a part's OWN pins — e.g. a FET gate vs drain 0.6mm apart vs a 2mm rule;
                         package-defined, UNAVOIDABLE by placement — needs a same-footprint DRC
                         exception, not a move)
  155  inter-component  (different HV parts placed < creepage apart — FIXABLE by placement)
        66 x HV_300 (need 3.7mm)   41 x HV_SWING (need 7.2mm)   48 x HV_150 (need 2.0mm)
        e.g. R101<->R98, R110<->R106, Q27<->R114, C72<->C76
```

A router cannot separate copper whose **pads** are already 0.3 mm apart when the rule is 3.7 mm.
So the 155 inter-component violations are a hard wall in front of routing.

## Ask — placement feasibility must include per-net-class copper clearance

This is the same shape as `placement-fidelity §2` (pad-copper clearance per net), but with the
number that actually bites: **the required spacing between two parts is the max net-class
clearance over their pads' nets, not a global courtyard gap.** Concretely:

1. **Placement `is_placeable` / anneal energy** should separate two footprints by
   `max(courtyard_gap, max_over_pad_pairs(netclass_clearance(a_net, b_net)))` for pads on
   shared layers and different nets. On an HV board this dominates courtyard entirely.
2. `region.py` already resolves per-net-class clearance for the ROUTER (`resolve_net_classes`,
   `net_exclusion`). Feed the SAME numbers into placement so a group is spread enough to route
   before it is declared placed. Today place uses courtyard; route uses creepage; they disagree
   and the board is "placed" but unroutable.
3. Report it: a placed HV board should print `HV creepage: N inter-part pairs below net-class
   clearance` the way DRC would, so "placed" never silently means "unroutable."

## The design tension this exposes (real, not a tool bug)

Honoring 7.2 mm HV_SWING creepage costs board area — you cannot pack 41 swing-node pairs
7.2 mm apart in a dense strip. So creepage-aware placement forces one of: a larger HV area, a
partition that isolates the few true-swing nets, or a per-net decision that some HV nets are
hand-routed with careful creepage rather than auto-spaced. That is a design call the placer
should SURFACE (which nets drive the area), not silently violate. Size-rank placement helps
here too: place the widest-creepage nets' parts first, as rocks, with their halos.
