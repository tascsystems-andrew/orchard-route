# V2_ROADMAP — adversarial merge of the v2 design panel

Synthesis document, 2026-07-18. Inputs: `ANALOG_SCORING.md`, `TEMPLATE_PLACEMENT.md`,
`FOURLAYER_MODEL.md` (unedited; this document overrides none of their text, only
decides what gets built and when). Ground truth consulted: `ARCHITECTURE.md`,
`REGION_SOLVER.md`, `AGENTS.md`, `README.md`, `STUDIO.md`, and the docstrings of
`pathfinder.py` / `place.py` / `constraints.py`.

The rule of this merge: a roadmap that accepts everything is a wish list. Each
panel doc gets its weakest load-bearing claim attacked first; conflicts get
resolved with a decision, not a compromise; the backlog is sequenced against the
work that is *already queued* (region.py v1 integration, clearance rings,
multi-party standoffs, ROI corridors, Studio deployment) — panel ideas do not
jump that queue by virtue of being newer; and §4 kills things on the record.

---

## 1. Attacks — one per document, on the claim its verdict stands on

### 1.1 ANALOG_SCORING: the coupling metric observes a defect the search cannot act on

The document's build-order note closes with: *"Each lands as a pure scoring
function over existing structures; none requires touching the kernel, the
lattice, or the SA move set."* Its thesis section says: *"a coupling term must
be in the objective or v2 will keep producing boards that route perfectly and
hum."* These two sentences cannot both be load-bearing. The router is a
deterministic function of a placement: every one of the K candidates is routed
by the same coupling-blind negotiator, whose channel style — the doc's own
observation — *manufactures* parallel runs. So the route-level component of
`victim_coupling` is largely **common-mode across candidates**: tier-3 VC
differences will mostly reflect placement adjacency (real, and worth scoring),
while the parallelism the router itself creates inside shared corridors is
invisible to a ranking over candidates that all share it. Making the router
actually *avoid* parallel runs means putting coupling into the negotiation cost
(a same-direction-neighbor usage surcharge on the cost vector — host-side but
deep in `pathfinder.py`'s pricing loop) — precisely the work the doc disclaims.
Verdict: the metric survives as a placement-ranking and diagnostic term; the
"or v2 hums" framing oversells what pure post-route scoring can deliver, and the
gap becomes a gated experiment (backlog D6), not a silent promise. Secondary
weakness, noted for the calibration item: the entire weight scheme hangs on
hinge thresholds calibrated against exactly one board (the hand-routed Voxy) —
that is a single-sample fit and must be labeled as such in diagnostics until a
second reference layout exists.

### 1.2 TEMPLATE_PLACEMENT: the "weakest link" is a prediction about a system that has never run

The verdict rests on: *"Constraint authoring is v1's weakest link — free-form,
per-call, unchecked, and it is where the circuit knowledge actually enters,"*
priced at *"1–2 kloc plus fixture tests, i.e. comparable to what
`constraints.py` + `place.py` already are."* But region.py v1 is **not yet
wired** (README: "designed but not yet wired together"); zero real
`optimize_region` calls have ever been made, so the weakest-link diagnosis is a
forecast, not a field observation — and the doc's own concession ("SA at n=10
with a well-authored constraint list very likely reaches fully-routed candidates
within the 3-minute budget") undercuts unconditional v2 inclusion. The offer is
to roughly double the placer-side codebase to mechanize what is today ~4 lines
of agent-authored constraints per call, in a domain with one user and a pin
table with one entry, where the doc itself concedes the canon's *numbers* don't
survive the through-hole→SMD translation (the numeric parameters in
`compiles_to` are exactly as hand-chosen as the agent's). And the trust boundary
only moves: bindings are "printed in diagnostics for the agent to veto," so the
agent's judgment remains the backstop — "enforceable instead of aspirational"
overstates. The salvage is real, though: the *representation* half (declarations
+ `order`/`symmetry`) is cheap and independently justified, and the matcher can
ship first as a zero-influence **advisor** (backlog D1) whose adoption in real
sessions is the evidence the full compiler currently lacks.

### 1.3 FOURLAYER_MODEL: "cost-identical" is not "behavior-identical"

The landing argument is: *"it degenerates cost-identically on L=2 so the bench
needs no recalibration"* plus *"Zero kernel changes — graph shape + host
bookkeeping only."* Path *costs* are identical (one hop of 8 → two hops of 4);
negotiation *dynamics* are not. Under columns, a contested via site carries a
third node (`V`), so overuse counts, history accrual, and present-cost stacking
all change scale — and `AGENTS.md`'s health heuristics ("overuse falls
monotonically to 0", stall-escape timing, `clique_patience` standoff detection
in `pathfinder.py`) are calibrated against today's scale. Every board also pays
+50% nodes unconditionally (the virtual layer is a full W·H slab: Voxy 2-layer
goes 682k → 1.02M nodes, and the (N,B) distance matrix grows with it), on a
bench whose via ratios are already the sore spot (1.7–2.5× human on dense
boards). None of this is disqualifying — the model is right, the L=2 cost
identity is a genuinely good regression property — but "needs no recalibration"
must be read as "no re-derivation of `via_cost`," not "no re-run": v2a lands
with a full bench re-run and an expectation of drift in via counts and standoff
behavior. Second, smaller point the roadmap must respect: the doc's honesty
section concedes Voxy has zero zones today, so v2b's measurable impact is on
the **bench fleet**, not the flagship — its Voxy value is contingent on a
circuit decision (which ground gets a plane) that belongs to Andrew.

---

## 2. Conflict resolutions

### R1 — One vocabulary: declarations are the interface, inference is one producer

ANALOG_SCORING says the agent asserts `stage`/`net_role`/`star` from the
schematic. TEMPLATE_PLACEMENT says a matcher infers roles from the board
netlist. These are the **same facts** arriving from two authors. Decision: the
declaration triplet is the canonical interface; the template matcher's real v2
job is to *emit suggested declarations and constraints into diagnostics* for
the agent to confirm — provenance-tagged (`declared_by: agent | inferred`), so
a metric can always say which regime fed it. This resolves both docs'
authorship anxiety at once: SCORING gets its declarations without demanding the
agent hand-derive every gain and role; TEMPLATE gets its role inference shipped
without any influence on search until it earns it.

### R2 — One currency: template energy terms merge into the constraint-penalty channel

TEMPLATE caps template soft terms at 0.5× HPWL energy; SCORING denominates
everything in equivalent-mm with hinge normalization. Two parallel weighting
schemes in one annealer is a calibration bug waiting to happen. Decision:
compiled template output is **ordinary L3 constraints only** (which already
carry mm-commensurate soft penalties per `constraints.py`'s contract); the
separate capped template-energy channel is killed (§4). Seeds, if the
experiment ever earns them, enter through the elite pool with reserved
template-free niches exactly as TEMPLATE §4 specifies — pool mechanics, not
energy mechanics.

### R3 — `order` subsumes SOM; the enum grows once

TEMPLATE's `order(refs, axis)` (hard/soft constraint) and SCORING's M7
`stage_order_monotonicity` (weight-60 metric that by its own admission "won't
decide a Voxy region") encode the same idiom. Decision: build `order` once as a
constraint; M7 dies as an independent metric (§4). Same consolidation for the
rest of the enum: `symmetry` (TEMPLATE), `clearance_min`, `stub_max`,
`loop_area_max`, `thermal_keepout`, `ground_topology(multi_star)` (SCORING) are
in; `pair_route` and the `single_star`/`bus` schemes are out (§4).

### R4 — Planes break the tree metrics; gate metrics on binding, and the docs compose into Blencowe

SCORING's M1 (GCI) and RLA-based metrics assume ground is a **routed tree**;
FOURLAYER's plane supernode makes the bound net's return *the plane* — no
polyline exists, tree-path sharing is meaningless, and M5's per-via penalty
("each via moves the reference plane") inverts on a solid-plane stackup. PCI's
adjacent-layer factor (0.5 for F-over-B) also goes to ~0 when two plane layers
sit between the signal layers. Decision: every routed-path metric gates on the
net **not** being plane-bound, and PCI becomes stackup-aware (adjacent-layer
factor is a function of the declared stackup, 0 across planes). Crucially the
two designs then compose into exactly Blencowe's scheme, which resolves the
apparent Voxy conflict: the plane (if Andrew creates one) binds the **noisy**
ground — `GND-MASTER (PSU)` / MCU / vactrol-LED returns — while the quiet audio
stars (`GND-B/C/D…`) stay as routed copper that GCI scores. FOURLAYER's
six-GND-nets warning and SCORING's "non-audio grounds return to the reservoir"
rule are the same fact seen from two sides. The AGENTS.md Phase-0 paragraph
that v2b adds should say this explicitly.

### R5 — Judge before explorer: no metric enters the SA energy until it has judged real candidates

SCORING wants placement-time proxies inside the annealer's cheap energy from
day one. But region.py v1 — the judge loop itself — is unbuilt, and tuning the
explorer toward unvalidated terms risks optimizing candidates toward metrics
whose thresholds are single-sample fits (attack 1.1). Decision, preserving the
project's own generate/judge contract: metrics land first as **post-route
scorers + diagnostics** (`binding_metric`); only after the Voxy calibration
run confirms the hand layout scores φ≈0 do the cheap proxies (DR, TK,
Euclidean PCI stand-ins) enter the SA energy. Same order for template terms
(R2). The cheap term never gets the final word — and it doesn't get a vote at
all until the final word is implemented.

### R6 — One legality substrate: clearance rings, HV tier, and column masks are the same machinery

Three efforts touch lattice legality: the already-queued clearance rings,
SCORING's `clearance_min`/M8 hard tier, and FOURLAYER's §2.3 column blocking
rules. Decision: SCORING's `clearance_min(class_a, class_b, mm)` with per-class
working voltages **is the spec for the queued clearance-ring work** — one item,
not two — and it carries the HV floors (IPC-2221B external uncoated: 1.25 mm at
171–300 V, 2.5 mm at 301–500 V) as a legality tier, because pitch-as-clearance
at 0.5 mm is ~2.5× under the floor for a 300 V B+ net. That is a fabrication-
safety bug on the flagship board class, which is why it keeps its queue position
(second, right behind region.py v1) instead of joining the analog-scoring batch.
Column masks (v2a) then land on top of the finished ring machinery rather than
interleaved with it.

---

## 3. The ordered v2 backlog

Sizes: **S** ≈ a day / a few hundred lines, **M** ≈ days / ~1 kloc, **L** ≈ a
week+. "Queued" marks the pre-panel queue; panel items argue their slots.

### Phase A — finish what v1 promised (queue order preserved)

| # | item | source | prereq | impact | size |
|---|------|--------|--------|--------|------|
| A1 | **region.py v1 integration** — wire SA (`place.py`) to the router-judge per REGION_SOLVER.md; Voxy acceptance test runs | queued | none | everything in Phases B/D is meaningless without it; the acceptance test exists at all | **L** |
| A2 | **Clearance rings, absorbing `clearance_min` + the HV legality tier** (R6) — per-class clearance in the obstacle model, per-class working voltage in `.kicad_pro`/constraint, HV violations scored in the hard tier, retire the pitch-as-clearance disclosure in AGENTS.md | queued + SCORING M8 | none (parallel with A1) | Voxy: output becomes legal to fabricate at 300 V; bench: real DRC-comparable clearance | **M** + S for the HV tier |
| A3 | **Multi-party standoffs** — the remaining 5 bench nets | queued | none | bench 811/816 → 816/816 | **M** |

*Argument:* nothing from the panel jumps Phase A. The panel's only claim urgent
enough to touch it — HV clearance — doesn't jump the queue, it **is** the queue
(R6).

### Phase B — the analog vocabulary and the craft-aware judge (all prereq A1)

| # | item | source | prereq | impact | size |
|---|------|--------|--------|--------|------|
| B1 | **Declarations**: `stage`, `net_role`, `star` in `constraints.py`, with provenance field (R1); metrics gate on presence — none declared ⇒ scores identical to v1 | SCORING §2 | A1 | adoption story: digital boards regress nothing | **S** |
| B2 | **Constraint enum growth, consolidated** (R3): `order`, `symmetry` (TEMPLATE — inexpressible today, promised by the ALIGN enum, useful to callers with no template ever matching), `stub_max`, `loop_area_max`, `thermal_keepout`, `ground_topology(multi_star)` | TEMPLATE §3.1 + SCORING §2 | A1; B1 for `ground_topology` | LTP-PI symmetry and signal-flow ordering become expressible; grid-stopper rule becomes a copper rule | **M** |
| B3 | **Scoring primitives + judges**: PCI and RLA; M1 GCI, M2 VC, M3 DR/SLA, M4 GSA, M5 IPI (with the M2/M5 double-count folded — §4), stackup-aware PCI factors (R4); tier-3 insertion into region.py's score; `binding_metric` + per-φ diagnostics | SCORING §1/§3 | A1, B1 | candidate ranking finally sees ground sharing, coupling adjacency, loop area — the things wirelength is blind to | **M** |
| B4 | **Calibration protocol**: score the hand-routed Voxy board; every metric it fails is mis-thresholded; single-sample-fit caveat recorded in diagnostics until a second reference exists (attack 1.1) | SCORING §3 | B3 | thresholds stop being speculation; acceptance test upgraded to craft-aware ranking | **S** |
| B5 | **Proxies into the SA energy** (DR, TK, Euclidean-PCI) — only now (R5) | SCORING §3 | B4 passes | annealer explores toward craft instead of discovering it by rejection | **S** |

### Phase C — multilayer track (independent of Phase B; can run in parallel after A2)

| # | item | source | prereq | impact | size |
|---|------|--------|--------|--------|------|
| C1 | **v2a via columns** — virtual column layer, adjacent-layer edges deleted, usage injection, column legality (foreign-pad and under-SMD blocking); lands with a **full bench re-run**, expecting drift (attack 1.3), not with a no-recalibration waiver | FOURLAYER §2/§5 | A2 (shared mask machinery, R6) | closes the same-(x,y) short hole and the via-under-foreign-pad bug; prerequisite for planes; +50% nodes on every lattice — measured, not hidden | **M** |
| C2 | **v2b planes** — zone-header + `zone_connect` parse, `--plane LAYER=NET` with the declared-vs-zone cross-check matrix, supernode star connectivity, tap emission + refill notice, AGENTS.md Phase-0 paragraph incl. the noisy-ground-gets-the-plane guidance (R4) | FOURLAYER §3 | C1 | bench: 4 of 5 four-layer comparisons become fair (392/157/79 human plane-tap vias stop counting against us); Voxy impact contingent on Andrew's zone decision — scored as bench work | **M** |
| C3 | **ROI corridors** — slotted *after* C1 so corridor extraction is built once against the final (column) graph shape rather than rebuilt for it | queued | C1 | whole-board Phase-1 runtime; offsets C1's +50% node cost | **M** |
| C4 | **Studio deployment** | queued | none (ops) | unblocks the (N,B) memory rows of v2b/v3 whole-board runs, k=64 batching, and the D-phase experiments; calibration sweeps go unattended | **S–M** |

### Phase D — gated items (each names its gate; none is promised)

| # | item | source | prereq | gate | size |
|---|------|--------|--------|------|------|
| D1 | **Role-inference advisor** — tube pin-function table + SubGemini-style anchor matcher, emitting *suggested* declarations (B1) and constraints (B2) into diagnostics only; zero influence on search | TEMPLATE §3, downgraded per attack 1.2 | B1, B2, plus real region.py sessions | build when field sessions show constraint authoring actually failing or grating; the advisor's suggestions being adopted is itself the evidence for D2 | **M** |
| D2 | **Constraint compilation + seeding experiment** — TEMPLATE §5.3's four pre-registered arms on the Voxy gain stage + the LTP-PI expressivity probe | TEMPLATE §5.3 | D1, C4 (batch scale is where seeding pays) | pre-registered: ≥3× time-to-first-routed-candidate or Andrew's blind pick at equal runtime | **S** to run |
| D3 | **Template geometry library + extractor-from-reference-board** (role-isomorphic ReplicateLayout) | TEMPLATE §5.1 | D2 wins | the one genuinely novel PCB-side step; unbuilt until paid for | **L** |
| D4 | **v3 four-signal routing** — >2 signal layers over columns; H/V alternation already falls out of `build_lattice` | FOURLAYER §5 | C1, C4 | exactly one bench board needs it (kicad-demo-video); also the best stress test | **M** |
| D5 | **Voxy plane zones (design act, not code)** — Andrew/agent choose which ground and supply get planes, create zones via Konnect | FOURLAYER §3.4 | C2 | converts v2b from bench feature to flagship feature | **S** (human) |
| D6 | **Coupling-aware negotiation pricing experiment** — same-direction neighbor-usage surcharge in the cost vector; run only if B3's VC scores show a large candidate-invariant residual (i.e., routing-manufactured coupling that placement ranking provably can't reduce — attack 1.1's testable form) | synthesis of SCORING's thesis | B3/B4 evidence | the VC-residual measurement is the gate; without it this is speculative kernel-adjacent work | **M–L** |

---

## 4. Kill list

Rejected outright, with reasons. Re-opening any of these requires new evidence,
not re-argument.

1. **Image-based template authoring** (TEMPLATE §5.1 raised it to reject it —
   ratified). Role binding without a netlist is guesswork; a confidently wrong
   template is worse than none.
2. **Template-verbatim placement as a product mode** (arm D of the experiment).
   It exists as a probe only. Canonical shapes ignore boundary-terminal pull,
   which is the whole reason the annealer exists.
3. **The separate capped template-energy channel** (TEMPLATE §4.2's
   "≤ 0.5 × HPWL" scheme) — killed by R2. One currency: constraint penalties in
   equivalent-mm. Two weighting schemes in one annealer is how calibration dies.
4. **M7 `stage_order_monotonicity` as an independent metric** — subsumed by the
   `order` constraint (R3); SCORING's own text concedes it "won't decide a Voxy
   region."
5. **`pair_route`** — redundant with `loop_area_max`: RLA already prices pair
   tightness (the twisted-pair analog), and two checkers for one intent means
   two thresholds to mis-set. If a send/return pair ever needs Hausdorff-style
   tracking that RLA can't express, that's the evidence to reopen.
6. **`ground_topology` schemes `single_star` and `bus`** — enum surface for
   philosophies nobody driving this tool has asked for. `multi_star` is the
   flagship scheme; the others are a parse-error away when someone wants them.
7. **M5's 2× re-count of noisy-PCI on the input net** ("counted again at 2×:
   the front door is special") — double-counting the same physical overlap in
   two metrics muddies `binding_metric` diagnostics; the front door's priority
   is expressed once, through `sens(input) = 1.0` in M2 and M5's remaining
   terms.
8. **Plane binding by net name or net class** (FOURLAYER raised to reject —
   ratified, loudly). Voxy's six GND nets are the proof: any name heuristic
   welds the star ground into a blob, the one mistake a tube-amp tool must
   never make.
9. **Routable plane layers at high cost** — a cost knob that invites return-path
   damage exactly when congested. Planes are supernodes or nothing.
10. **Blind/buried/micro vias** — the target fab's standard service doesn't
    offer them (JLCPCB, per FOURLAYER §1.3). Behind a fab profile, later, maybe.
11. **Free-form `weight(metric, w)` constraints and guard-trace / pour-partition
    constraints** (SCORING raised to reject — ratified). Weights are call
    parameters; promising zone-dependent constraints without a zone model
    violates the honesty rule.
12. **A fixed adjacent-layer PCI factor (0.5)** — killed as a constant,
    survives as a stackup function (R4): ~0.5 on 2-layer FR-4, ~0 across plane
    pairs. A constant would score phantom coupling on exactly the boards v2b
    makes routable.

---

## 5. The one-paragraph shape of v2

v2 is three composable moves, each independently valuable and separately
falsifiable: **(A)** finish v1's promises — region solver wired, clearance made
real with HV legality inside it, the last bench nets closed; **(B)** teach the
judge what analog craft is — declarations in, nine-metrics-minus-consolidation
scored on routed candidates, calibrated against the one board we trust;
**(C)** make four layers true — vias become columns, planes become supernodes,
the bench becomes fair. Templates enter as an advisor and must win a
pre-registered experiment to become more; coupling-aware routing must be shown
necessary by measurement before anyone touches the negotiator's pricing for it.
Everything else is on the kill list, where a roadmap keeps its ideas honest.
