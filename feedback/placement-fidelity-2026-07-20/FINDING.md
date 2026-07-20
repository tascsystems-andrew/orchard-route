# Findings: placement feasibility must match DRC fidelity (+ pad clearance, two-sided THT)

**Date:** 2026-07-20. **From:** AI agent, out of the Voxy placement + adopt cycle.
**Companion to:** `../courtyard-model-2026-07-20/FINDING.md` (A courtyard, B min-gap, C
no-courtyard). These are the deeper ones — they explain *why* several "0-overlap" boards
still failed DRC in KiCad, and they back up Andrew's dev-thread notes (rotation, size-rank
placement, flexible group boundaries) with the mechanism I hit.

---

## 1. THE ROOT PATTERN: "placement-clean" was coarser than "DRC-clean"

Every board I shipped this cycle passed my feasibility check and then failed in KiCad. The
reason is one sentence: **my feasibility model was a strictly coarser approximation of DRC
than DRC itself.** Each gap was a place where "placed OK" and "manufacturable" diverged:

| I checked | KiCad also checks | what leaked through |
|---|---|---|
| courtyard bbox overlap | real courtyard (arcs/poly), all parts | THT bodies over small-courtyard parts |
| — (no courtyard = skipped) | every footprint has a body | 25 relays + MCU placed as dots |
| courtyards *touching* = OK | clearance **gap** between copper | 0.2 mm-gap "crashes" |
| — (never checked) | **pad-to-pad copper clearance, per net** | different-net pads touching = shorts |
| — | mounting-hole / keep-out zones | parts on M3 holes |

**Ask (the meta-fix):** a single `is_placeable(board)` whose definition IS the DRC rule set
the target uses — courtyard (assembly) **and** pad-copper clearance per net class **and**
hole/keep-out clearance — so that "the placer says feasible" is a promise the board passes
DRC, not a weaker proxy. Every specific item below is an instance of this. Where a check
can't be exact yet, it should **warn** rather than silently pass (Finding C).

## 2. Pad-to-pad copper clearance is not implied by courtyard non-overlap

The one that shorts boards. Two parts with clear courtyards can still have individual pads
within clearance — a FET's fat drain pad, a relay's spread pads. On Voxy this produced
different-net pads **touching** (e.g. `Q31/Output PA1` vs a `B+` pad). Courtyard/keep-out is
an *assembly* halo; it says nothing about where the copper of one pad sits relative to
another net's pad.

**Ask:** the placement feasibility + anneal energy must include **pad-copper clearance
between pads of different nets on shared layers**, using the net-class clearance (HV nets
want their big number here, not 0.2 mm). Same-net pads may touch. This is a pad-vs-pad
neighbour check, cheap with the spatial hash the router already builds.

## 3. A through-hole part on the BACK still occupies FRONT pad copper

Andrew's "put the <10 mm vactrols on the back to free the front" is right for the *body* but
not the *pads*: a THT footprint flipped to `B.Cu` moves its silk/courtyard to the back, but
its pads are `*.Cu` — **copper on both sides, one drill through**. So a back-side THT part
still blocks front copper at its holes. On Voxy the 15 vactrols went to the back cleanly on
courtyard, then collided at the **pad** level with the front relays' SMD pads (30 violations;
I got it to 10 only by clearing against *all* pads both sides — the rest are un-fixable
without moving them off their groups). Net effect: back-placing THT parts in a pad-dense
region frees courtyard but not routing copper, and can be worse than leaving them front.

**Ask:** model a back-side THT part as occupying **both** copper layers at its pads; only
its *body/courtyard* moves to the back. Then the placer won't propose back spots whose holes
land on front pads, and the "free the front" heuristic only fires where it actually helps
(SMD-on-back, or THT-on-back in copper-sparse areas).

## 4. Endorsing Andrew's three, with the mechanism I hit

- **Rotation as a first-class DOF (his "router is scared to rotate").** Yes — and the *cause*
  is the pad-rotation-absolute footprint format (footprint angle folds into each pad's
  stored angle). Any writer that sets footprint angle without re-composing every pad angle
  renders pads 90° off (I hit this early; it's why I never rotate through my sexpr writer).
  **Fix the write path to rotate pads correctly, then rotation is safe** — and it unblocks
  two things at once: the placer can rotate a long part to fit (Voxy's C9 27 mm film and
  R175 28 mm resistor are *un-placeable* un-rotated because area1's 46 %-free space is
  fragmented into sub-28 mm gaps — they fit vertical), and the router can rotate to escape.
- **Size-rank placement ("rocks first, sand after").** Strongly yes. Place by descending
  keep-out area: the bulky parts get the freedom while the board is empty; small agile parts
  fill the gaps. I did a weak version (big *groups* first); the stronger rule is per-part,
  and it composes with §1 — the "rocks" are exactly the parts whose real footprint the coarse
  models under-counted.
- **Flexible, family-scoped group boundaries.** Yes, and the key is the word "related." Give
  groups a **family** (EQ, triode, pentode, PA, control). A group that can't fit may **borrow
  space from an adjacent SAME-family group** (a low-freq EQ band with bulkier caps spilling
  into the next EQ band), but a **hard barrier** blocks cross-family spill (EQ into the PSU).
  Concretely: fences are soft within a family (overflow allowed if the family has slack) and
  hard between families. This directly fixes the "all but one placed fine" case.

## 5. One more from me: verify at write time, not just place time

`writeback.verify_emission` already refuses to emit copper that violates the geometry
contract. Extend the same spirit to placement: before a placed board is handed back, run the
§1 `is_placeable` and **fail loudly** if it isn't, listing the exact violating pairs. The
expensive lesson this cycle was a chain of confidently-wrong "0 overlaps" — a hard
post-condition that mirrors DRC would have caught each one at the source.
