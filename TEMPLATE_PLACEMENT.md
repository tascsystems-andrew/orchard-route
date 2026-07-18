# Stage templates vs. the annealer — a design for optimize_region v2

*Panel lens: devil's advocate against pure annealing. Position taken, argued against
the real v1 baseline (SA generates, router judges — REGION_SOLVER.md §3), not a
strawman. Deliverable of the template-idiom design agent, 2026-07-18.*

## Verdict first

**Build the constraint-compiler half of templates in v2. Defer the geometry library
to v2.5. Reject templates-as-replacement forever.**

Concretely, v2 should ship:

1. **Role inference** — anchor-first matching around tube sections that binds
   component roles (plate load, cathode R/C, grid stopper, coupling cap, …)
   automatically from the board netlist.
2. **Constraint compilation** — matched roles compile to the *existing* L3
   vocabulary plus two new forms (`order`, `symmetry`) — the constraint list the
   agent currently hand-authors per call becomes a derived artifact.
3. **Seed injection** — a handful of template-shaped placements enter the SA elite
   pool as starting states. Soft influence only. The router-judge remains the sole
   arbiter; nothing a template says can make a region infeasible.

What v2 should **not** ship: a curated geometric template library with tuned
spacings, template extraction from images, or any mode where a template bypasses
the annealer or the router. Reasons in §5.

The rest of this document is the argument and the mechanism.

---

## 1. Why pure SA deserves an adversary here

Tube-amp stages are canonical. A common-cathode triode stage, a cathode follower, a
long-tailed-pair phase inverter, an RC-ladder PSU chain — each has a layout refined
over 70 years that experienced builders reproduce nearly verbatim: component chain
along the signal path, grid components tight to the socket, decoupling local to the
stage, plate and grid sides kept apart. Three observations about the v1 baseline:

**(a) v1 already uses templates — it just makes the agent retype one every call.**
Look at the Voxy acceptance test (REGION_SOLVER.md): `fixed(V1)`,
`adjacency_max_distance(grid stopper, socket, 3)`, `min_distance(plate R, grid
input, 4)`. That constraint list *is* the common-cathode template, degenerately
encoded, authored by hand, per call. The knowledge already lives in the system;
it lives in the least reliable place — free-form agent judgment exercised fresh
each time. AGENTS.md's own Phase 0 ("constrain from the schematic's story") is an
unfunded mandate: nothing checks that the agent derived the right constraints, and
a wrong-but-parseable constraint list produces confidently wrong candidates. A
template is a *named, reusable, testable* constraint bundle. That reframing — 
templates as compiled constraints, not as a rival placer — is the core of this
design.

**(b) The router-judge has a blind spot templates can cover.** The judge scores
`failures ≫ violations ≫ wirelength + vias`. It cannot score hum coupling, Miller-
loop area, VHF stability of the grid circuit, or heater-run proximity to the grid
trace. A fully-routed, minimal-wirelength stage can still lay the grid track
parallel to the heater pair. SA+judge optimizes what it can measure; the canonical
layouts encode what seventy years of listening measured. Yes — every such concern
is *expressible* as `min_distance`/`adjacency` terms. But someone must write them,
correctly, every call (see (a)). Templates are that someone.

**(c) Search economy.** At v1 scale (k=5, ~10 parts, host-side SA) this is a mild
argument: SA at n=10 is fine. At Studio-era scale (k=64 GPU-batched candidates,
multi-stage regions) burning the exploration budget rediscovering "signal flows
left to right, cathode parts below, plate parts above" in every region is real
waste. Seeding is nearly free and compounds with batch size.

**And the honest counter-case.** The IC analog tools this repo already cites did
*not* converge on pure annealing — but their motive (nm-scale matching, parasitic
symmetry) barely applies to a guitar-amp PCB, which is electrically tolerant by
comparison. On an 8–12-part region, SA with a *well-authored* constraint list very
likely reaches fully-routed candidates within the 3-minute budget. So the honest
claim for templates is **not** "SA can't lay out a gain stage." It is: (i) the
constraint-authoring link is the weakest in the v1 chain and templates mechanize
it; (ii) two idioms (symmetry, signal-flow order) are *inexpressible* in the v1
vocabulary at any authoring effort; (iii) seeds buy convergence speed that matters
at batch scale. Those three claims are testable (§5.3), and the verdict in §6 is
contingent on them.

---

## 2. Prior art (so we don't reinvent it unknowingly)

- **ALIGN** — open-source analog IC layout; hierarchical recognition of known
  primitives in the netlist, then constraint-driven placement. Its constraint
  vocabulary is already the ancestor of `constraints.py` (ARCHITECTURE.md rule 7).
  Relevant lesson: ALIGN front-loads *structure recognition* before any
  optimizer runs. ([paper](https://www.researchgate.net/publication/333336922_ALIGN_Open-Source_Analog_Layout_Automation_from_the_Ground_Up))
- **MAGICAL** — DARPA IDEA-era netlist-to-GDSII analog flow: automatic constraint
  extraction by pattern matching + learned models, template-and-grid device
  generation, then placement/routing. Silicon-proven. Again: recognition first,
  optimization second. ([ICCAD 2019](https://yibolin.com/publications/papers/ANALOG_ICCAD2019_Xu.pdf),
  [overview](https://par.nsf.gov/servlets/purl/10356326))
- **BAG / BAG2 (Berkeley Analog Generator)** — the fully-procedural pole: a human
  writes a parameterized *generator* (code) once; layouts are emitted, not
  searched. Maximum quality, maximum authoring burden, zero generality outside
  the authored family. ([BAG2](https://ieeexplore.ieee.org/document/8780349))
- **SubGemini** (Ohlrich/Ebeling et al., DAC 1993) — the classic subcircuit
  recognition algorithm: pick the rarest, most distinctive element as the key
  vertex, match outward from it. Near-linear in practice despite NP-complete
  worst case. Our matcher (§3.2) is anchor-first for exactly this reason.
  ([dblp](https://dblp.uni-trier.de/rec/conf/dac/OhlrichEGS93.html))
- **Constraint-extraction ML** — graph-attention symmetry extraction and kin
  ([arXiv 2312.14405](https://arxiv.org/html/2312.14405v1)): evidence that even
  the "just extract constraints" step is a live research area; our domain is far
  narrower (a dozen tube-stage idioms), which is why deterministic predicates
  suffice where ICs need GNNs.
- **PCB-side reuse** — Altium *Layout Replication* / snippets / rooms
  ([docs](https://www.altium.com/documentation/altium-designer/pcb/layout-replication))
  and KiCad's **ReplicateLayout** plugin
  ([GitHub](https://github.com/MitjaNemec/ReplicateLayout)) replicate placement +
  routing across *identical* netlist fragments (same hierarchical sheet, matched
  connections). This is template placement with the matching problem deleted:
  identity is given by sheet instancing. **The delta this design proposes over
  the entire PCB prior art is exactly one step: relax "identical sheet instance"
  to "role-isomorphic circuit fragment."** Nobody in the PCB world appears to
  have shipped that step; the IC world (ALIGN/MAGICAL) has, behind a process-node
  wall.

Spectrum, for orientation: BAG (all template, no search) ↔ ALIGN/MAGICAL
(recognize, then constrained search) ↔ v1 Orchard (all search, hand constraints).
This design moves Orchard one notch left, to the ALIGN point — not two notches to
BAG.

---

## 3. The stage-template representation

### 3.1 What a template is

A template is a declarative record (Python data, same spirit as `constraints.py` —
closed, parseable, testable), three parts:

```
StageTemplate(
  name        = "common_cathode_triode",
  anchor      = TriodeSection,            # what to search for
  roles       = { ... },                  # predicates over local netlist topology
  geometry    = { ... },                  # relative placement, parameterized
  compiles_to = [ ... ],                  # L3 constraints emitted on match
)
```

**Anchor.** A tube section: a footprint whose pins can be mapped to
grid/plate/cathode (+heaters). Pin functions come from a small static table keyed
by (footprint or symbol value): 12AX7/12AT7/12AU7 noval twin-triode → pins 1/6
plate, 2/7 grid, 3/8 cathode, 4/5/9 heater; octal power tubes, pentode preamps
similarly. This table is honest about its limits: an unrecognized tube/socket is a
**no-anchor, no-match, fall through to v1** — never a guess. (For Voxy the table
needs exactly one entry to start.)

**Roles** are predicates over the 1–2-hop net neighborhood of the anchor's pins.
Written against the *board* netlist (pads → nets, from `board.py`), not the
schematic — the board is what `optimize_region` already loads, and it avoids a
schematic↔board identity mapping. Illustrative role set for the common-cathode
stage:

| role            | predicate (structural first, net-class as tiebreak only)        | required? |
|-----------------|------------------------------------------------------------------|-----------|
| `plate_load`    | R with one pad on the plate net; other pad on a different net (prefer class Power/HV) | yes |
| `cathode_r`     | R between cathode net and GND-class net                          | yes |
| `cathode_c`     | C in parallel with `cathode_r` (same two nets)                   | no (unbypassed stages exist) |
| `grid_stopper`  | R with one pad on grid net where the grid net has exactly 2 pads (R + tube pin) | no |
| `grid_leak`     | R between grid-side net and GND                                  | no |
| `coupling_out`  | C on plate net whose other net leaves the region (boundary net) or reaches a grid pin | no |
| `decouple_c`    | C between `plate_load`'s far net and GND                         | no |

Predicates are deliberately structural (pin-on-net arithmetic), using net *class*
only to break ties — because AGENTS.md itself warns most boards arrive with
everything in `Default`, and a template system that silently requires class
hygiene would fail exactly where v1's constraint authoring fails.

**Geometry** is expressed in a stage-local frame: origin at the anchor, **u** =
signal-flow axis, **v** = perpendicular. Each role gets an offset *range* (not a
point), an orientation set, and the whole frame gets a mirror bit and u-direction
chosen from context (input boundary terminal on the −u side, output on +u). Ranges
are wide on purpose: the 70-year canon is turret-board/PTP *through-hole* practice;
what survives translation to a 1206-SMD board like Voxy is **ordering and
separation, not literal spacing**. A template that encodes inch-scale Fender
spacings would be confidently wrong on SMD; a template that encodes "grid parts
−u of socket, plate parts +u/+v, cathode parts +u/−v, decoupling adjacent" is
right on both. Grounding topology (star vs. bus) is explicitly **not** template
content — that is agent/constraint territory, it varies per design philosophy.

**Compiles_to** — the load-bearing part. On a successful binding the template
emits ordinary L3 constraints referencing the bound refs:

```
adjacency_max_distance(grid_stopper, V1, 3)      # if grid_stopper bound
min_distance(plate_load, grid_stopper, 4)
adjacency_max_distance(decouple_c, plate_load, 6)
order([input_terminal, grid_stopper, V1, coupling_out], axis=u)   # NEW form
```

Two vocabulary additions are required, both already promised by ARCHITECTURE.md's
ALIGN-derived enum and missing from v1's six:

- `order(refs..., axis)` — monotone ordering of centers along an axis. This is the
  signal-flow idiom, and it is *inexpressible* today: `adjacency` and
  `min_distance` are unordered, so no combination of them says "input, then tube,
  then output."
- `symmetry(pairs..., axis)` — mirror pairs about an axis. The long-tailed-pair PI
  is the poster child; today's vocabulary cannot ask for it at all.

Both fit `constraints.py`'s existing contract (hard verdict + soft mm-commensurate
penalty), and both are useful to human/agent callers *independently of whether any
template ever matches* — which is why they belong in v2 even under the most
template-skeptical reading of the evidence.

### 3.2 The matching algorithm, concretely

SubGemini's lesson: don't solve subgraph isomorphism, anchor on the rare thing.

```
match_templates(region_components, board, templates):
  1. Find anchors: footprints in the region present in the tube-pinout table.
     No anchors -> return [] (v1 behavior, zero cost beyond the lookup).
  2. For each anchor x each template with that anchor kind:
     a. Read the anchor's pin->net map from board pads.
     b. For each role, evaluate its predicate over components with >=1 pad on a
        net within 2 hops of the anchor's pins. Region has <=~15 parts; the
        candidate set per role is tiny; brute force is exact and instant.
        NP-hardness is a non-issue at this scale — worst case is enumerating
        a handful of ambiguous bindings.
     c. Ambiguity (two Rs both satisfy plate_load — series plate resistors,
        snubbers): enumerate ALL consistent bindings, keep each as a separate
        candidate binding. Never pick silently.
     d. Score binding = (required roles bound, optional roles bound, parts in
        region left unbound). Required role missing -> binding rejected.
  3. Return bindings sorted by score, each carrying: bound roles, compiled
     constraints, and the unbound-parts list.
```

Determinism: pure function of board + region + template set; trivially seedable
and testable against fixture boards.

**Failure modes, named (this list is the test plan):**

- *DC-coupled pairs* — a cathode follower whose grid net IS the previous plate
  net. Single-stage predicates that assume disjoint nets miss it. Fix: a distinct
  `dc_coupled_cf` template anchored on the shared net, and predicates that never
  assume net disjointness. Voxy-relevant (CF stages are common in the target amps).
- *Shared components* — one cathode resistor serving both halves of a twin triode.
  Two anchors both bind the same ref; the merge rule is "shared refs get the
  union of emitted constraints; conflicting `order` terms drop both and report."
- *Role overload* — a part legitimately serving two roles (bright cap across a
  pot that is also the coupling path); bindings overlap, both survive as
  alternatives, the SA pool tries both seeds.
- *Novel side-chains* — Voxy's vactrol/LDR switching network around a stage has
  no 1950s template. These parts land in the unbound list and stay pure-SA. This
  is the *design's* central safety property: unbound ≠ error.
- *Wrong-net-class boards* — handled by structural-first predicates (above).
- *Unknown tubes/sockets* — no anchor, clean fall-through, diagnostics say so.
- *The genuinely bad case* — a binding that satisfies predicates but is
  semantically wrong (e.g. a parasitic-suppression R mistaken for the grid
  stopper). This produces a wrong seed and wrong soft terms. The blast radius is
  bounded by the hybrid protocol (§4): soft-only influence + router-judge means
  the worst outcome is wasted pool slots, not a wrong "best" candidate — and the
  binding is printed in diagnostics for the agent to veto.

---

## 4. The hybrid protocol: seed and shape, never command

Firm position on the seed/constrain/replace trilemma: **templates seed the pool
and add soft energy terms. They never hard-constrain, never replace.** Rationale:
template applicability is *inferred*, and inferred knowledge must not be able to
render a region infeasible or override an instruction a human/agent actually gave.

Mechanics, mapped onto `place.py` as it exists:

1. **Seeding.** Each surviving binding instantiates its geometry at parameter
   midpoints, for each viable (mirror × u-direction) variant, legalized to the
   placement grid, courtyard-checked. Legal instantiations enter the SA elite
   pool as initial states. `place.py`'s pool is *niched on placement distance* —
   template seeds naturally occupy their own niches and cannot be crowded out
   early, and conversely: **reserve at least half the niches for template-free
   exploration** so seeds cannot crowd SA out either. Illegal instantiations
   (region too small for the canonical shape) are discarded with a diagnostic,
   not forced.
2. **Soft terms.** Compiled template constraints join the energy as *soft
   penalties only*, weight-capped so their sum cannot exceed the HPWL term's
   typical magnitude (concretely: normalize template weight so that at seed
   state, template energy ≤ 0.5 × HPWL energy). Explicit caller constraints keep
   their v1 status (hard rejection). Hierarchy, unambiguous:

   **caller constraints (hard) > feasibility (courtyard/fence, hard) >
   template terms (soft, capped) — and the router-judge outranks all of it.**

   A placement that routes fully always outranks one that doesn't, however
   template-faithful the loser (design rule 5 untouched).
3. **Conflict rule.** When a caller constraint contradicts a template term
   (caller pins the grid stopper across the region), the template terms touching
   that ref are dropped and the drop is *named* in diagnostics — silence would
   imply the template was honored.
4. **Partial matches.** Emit constraints only among bound roles; seed with bound
   roles placed and unbound parts at their current positions; SA explores the
   rest. Never synthesize a placement for an unbound role.
5. **Multi-stage regions.** Match per-anchor; add one macro term: `order` over
   the stage anchors along the region's signal-flow axis, derived from
   inter-stage coupling nets and boundary terminals. Full hierarchical
   floorplanning (stages as super-components with their own SA) is explicitly
   out of v2 — it's the Studio-era shape of this idea.
6. **No match.** Bit-for-bit v1 behavior. Zero regression risk by construction.
7. **Diagnostics** (the actual product, per ARCHITECTURE.md) gain:
   `templates: [{name, anchor, bindings, dropped_terms, unbound}]` or
   `templates: none` — the agent must always know which regime produced its
   candidates.

Two honest costs of even this conservative integration, named: (i) *pool-slot
displacement* — seeds consume elite slots exploration would have used (mitigated
by the reserved-niche rule); (ii) *energy distortion* — capped soft terms still
tilt the explorer away from basins the judge might have preferred. Both are
measurable in the §5.3 experiment (run hybrid with weights at 0 = seeding-only
arm).

---

## 5. Honest costs

### 5.1 Authoring burden

The tube-amp domain needs a small library — this is the domain's gift, and it does
not generalize: **common-cathode triode stage** (bypassed/unbypassed via optional
role), **cathode follower** (incl. DC-coupled pair), **long-tailed-pair PI**
(needs `symmetry`), **cathodyne PI**, **RC-ladder PSU node** (repeating dropper-R
+ filter-cap unit, anchored on the cap chain rather than a tube), **power output
stage**, maybe **tone stack**. Eight-ish templates, each ~50 declarative lines
*once the representation exists*. The representation + matcher + two new
constraint kinds is the real cost — estimate 1–2 kloc plus fixture tests, i.e.
comparable to what `constraints.py` + `place.py` already are. For a different
domain (SMPS, RF, digital) the library is empty until someone writes it: templates
are a domain bet, and this repo has already made that bet (the flagship board is a
tube amp).

**Can the AI author templates?** Split honestly:

- *From a reference board* (.kicad_pcb + netlist — e.g. Andrew's hand-laid Voxy
  gain stage, or a traced classic): **yes, and this is the good path.** Run the
  same role inference on the reference, then *record* the bound roles' relative
  geometry in the stage frame → that is template extraction, mechanically the
  KiCad-ReplicateLayout idea with sheet-identity replaced by role-isomorphism.
  The matcher is 90% of the extractor. Build in v2.5, after the matcher exists.
  A template extracted from Andrew's own accepted layout is also self-calibrating
  to SMD scale — it dodges the through-hole-canon brittleness entirely.
- *From an image* (photo of a Fender chassis, a layout diagram from the Blencowe
  book): **speculation, and I recommend against building on it.** A VLM can
  propose approximate relative positions, but role binding without a netlist is
  guesswork, and a confidently wrong template is worse than none. Revisit only if
  the reference-board path proves out and the library needs breadth.

### 5.2 Brittleness vs. SA's generality

Named plainly: templates are a bet that the future workload looks like the past
library. SA+judge handles the vactrol side-chain, the MCU corner of a mixed-signal
Voxy region, and any circuit nobody templated — templates handle none of that.
The hybrid's whole design exists to make this asymmetric: template coverage adds,
template gaps subtract nothing. The residual risks are the two §4 costs plus
matcher bugs producing wrong seeds — all bounded, all measurable, none capable of
overriding the judge.

### 5.3 The falsifiable experiment (Voxy acceptance test, extended)

Same fence, same caller constraints, same seed, same wall-clock budget, four arms:

| arm | config |
|-----|--------|
| A | v1 baseline: SA + router-judge |
| B | hybrid, seeding only (template weights = 0) |
| C | full hybrid (seeds + capped soft terms) |
| D | template-verbatim (best legal instantiation routed directly, no SA) — a *probe*, not a product mode |

Metrics: (1) wall-time to first fully-routed candidate; (2) SA iterations to a
fully-routed pool; (3) candidate-#1 region wirelength vs. Andrew's hand layout;
(4) **blind pick**: Andrew ranks candidate #1 of each arm against his hand layout
and counts the manual moves he'd make before accepting each — the only metric
that measures what the judge can't score.

Decision rule, pre-registered: **templates earn v2.5 investment** (geometry
library + extractor) if C beats A by ≥3× on (1)/(2) *or* wins (4) at equal
runtime. **Templates lose** if A matches C at equal wall-clock — then role
inference survives only as a constraint-authoring aid and the geometry machinery
stops there. Second falsifier, the expressivity probe: run all arms on a
**long-tailed-pair PI region**. If A (which cannot even express symmetry)
produces a candidate Andrew accepts in (4), the strongest template argument
collapses and the verdict below should be downgraded accordingly. If D routinely
routes and wins (4), the SA layer is over-built for canonical regions and a
fast-path is worth considering — I predict D fails on boundary-terminal pull
(canonical shapes ignore where the fence's nets enter), which is precisely why
the annealer stays in the loop.

---

## 6. Verdict, restated against the real baseline

The v1 baseline is good: SA at region scale with a hard router-judge is exactly
the honest architecture, and for *most* regions of most boards it will not be
beaten by a template library that doesn't exist yet. The devil's-advocate case
does not overturn it. It identifies three specific deficits and buys each at its
own price:

1. **Constraint authoring is v1's weakest link** — free-form, per-call,
   unchecked, and it is where the circuit knowledge actually enters. Role
   inference + constraint compilation mechanizes it for the canonical 80% of a
   tube amp. **Build in v2.** Cheap, testable, zero regression, and it makes the
   agent's Phase-0 duties enforceable instead of aspirational.
2. **`order` and `symmetry` are inexpressible today**, template or no template.
   **Build in v2** regardless of the experiment's outcome — they were promised by
   the ALIGN-derived enum and callers can use them directly.
3. **Geometric seeding and the template library** are a convergence-speed and
   judge-blind-spot bet whose payoff is unproven at k=5 scale. **Gate on the
   §5.3 experiment; build the extractor (v2.5) only if arms B/C earn it.**
   Image-based authoring: not on the roadmap.

And permanently: templates never replace the router-judge, never hard-constrain,
never run without a template-free half of the pool. The moment a template can
make a region infeasible, the system has traded a measured failure mode for an
inferred one — that trade is the actual mistake ALIGN/MAGICAL's
recognition-then-optimize architecture exists to avoid, and the PCB world's
identity-only reuse tools (Altium replication, KiCad ReplicateLayout) avoid by
refusing to infer at all. The role-isomorphic middle is worth building precisely
because both neighbors stopped one step short of it.

---

## Sources

- [ALIGN: Open-Source Analog Layout Automation from the Ground Up](https://www.researchgate.net/publication/333336922_ALIGN_Open-Source_Analog_Layout_Automation_from_the_Ground_Up)
- [MAGICAL: Toward Fully Automated Analog IC Layout (ICCAD 2019)](https://yibolin.com/publications/papers/ANALOG_ICCAD2019_Xu.pdf)
- [MAGICAL: A Silicon-Proven Open-Source Analog IC Layout System](https://par.nsf.gov/servlets/purl/10356326)
- [BAG2: A Process-Portable Framework for Generator-Based AMS Circuit Design](https://ieeexplore.ieee.org/document/8780349)
- [SubGemini: Identifying SubCircuits using a Fast Subgraph Isomorphism Algorithm (DAC 1993)](https://dblp.uni-trier.de/rec/conf/dac/OhlrichEGS93.html)
- [Graph Attention-Based Symmetry Constraint Extraction for Analog Circuits](https://arxiv.org/html/2312.14405v1)
- [Altium Designer: PCB Layout Replication](https://www.altium.com/documentation/altium-designer/pcb/layout-replication)
- [KiCad ReplicateLayout plugin](https://github.com/MitjaNemec/ReplicateLayout)
