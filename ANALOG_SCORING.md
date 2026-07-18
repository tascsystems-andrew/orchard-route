# ANALOG_SCORING — what makes a tube-amp layout good, computably

Design proposal for optimize_region **v2**: candidate-scoring metrics and
constraint-vocabulary additions that encode valve-amp layout craft. Status:
design document, no code. Companion to ARCHITECTURE.md / REGION_SOLVER.md.

Sources, mined and reformulated (no passages reproduced):

- **TPGB** — M. Blencowe, *Designing Tube Preamps for Guitar and Bass* (2nd ed.),
  Ch. 12 "Notes on Construction", pp. 273–288 (Andrew's OCR copy,
  `~/Documents/Amps/`).
- **DVGA** — M. Blencowe, *Designing Valve Guitar Amplifiers*, Ch. 14 "Layout
  and Grounding", pp. 299–313 (valvewizard.co.uk/Grounding.pdf, fetched
  2026-07-18).
- **IPC-2221B** Table 6-1 spacing values, cross-checked against
  [smpspowersupply.com](https://www.smpspowersupply.com/ipc2221pcbclearance.html)
  and [Altium's IPC-2221 guide](https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design).

---

## 0. The thesis: craft = geometry × circuit role

Every layout rule in Blencowe reduces to the same shape: *a geometric quantity*
(length, separation, enclosed area, shared path) *conditioned on what the
conductor is in the circuit* (a grid, a plate, a heater leg, a ripple return).
The router already has the geometry — pads with absolute positions and nets
(`board.py`), routed paths per net (`RouteResult.net_paths` /
`.tracks`/`.vias`), layers, and net→class mapping from the `.kicad_pro`
(`writeback.load_net_class_names`). What it cannot have, and must never guess,
is the **role**: no geometric analysis distinguishes a grid trace from a plate
trace, or tells you which decoupling cap "belongs" to which triode.

**Position:** the single most important v2 addition is therefore not another
restriction but a *declaration layer* — three closed-enum facts
(`stage`, `net_role`, `star`) the agent asserts from the schematic, exactly as
AGENTS.md already makes the agent responsible for net classes. Every metric
below gates on its declarations being present; with none declared, v2 scores
identically to v1. That gating is the adoption story: digital boards lose
nothing, and a lazy agent gets v1, not garbage.

A second, sharper observation: **the Manhattan router's own style is
anti-craft by default.** Negotiated channel routing loves long parallel runs
in shared corridors — precisely the geometry Blencowe calls out as the
cardinal lead-dress sin (noisy conductor parallel to a sensitive one — DVGA
§14.1.2 p. 300; TPGB pp. 284–285 wants crossings at right angles). Wirelength
and via count are *blind* to parallelism; a coupling term must be in the
objective or v2 will keep producing boards that route perfectly and hum.

### The two computational primitives

Both metrics families reduce to two functions over data the codebase already
produces:

**P1 — Parallel Coupling Integral (PCI).** For two nets A, B with smoothed
segments `RouteResult.tracks`:

```
PCI(A, B) = Σ over same-layer segment pairs (a, b):
              overlap_len(a, b) / max(gap(a, b), pitch)
```

where `overlap_len` is the length of a's projection onto b that lies within
`gap < g_max` (default 3 mm — beyond that, coupling is negligible at audio
board scale), and `gap` is the perpendicular segment distance. Units:
dimensionless (mm/mm). Perpendicular crossings contribute ≈ one trace width
over one gap — near zero — so the right-angle-crossing exemption falls out of
the formula instead of needing a special case. Adjacent-layer pairs (F.Cu over
B.Cu on a 1.6 mm board) count at a configurable factor (default 0.5; broadside
coupling through FR-4 is real but weaker — *speculative default, tune on the
Voxy hand layout*). Cost: O(segment pairs) restricted to declared
aggressor×victim class pairs — hundreds of pairs per region, microseconds.

**P2 — Return Loop Area (RLA).** For a signal path P (lattice node sequence
from `net_paths`, coordinates via the lattice) and its designated return path
Q between the corresponding ground pads:

```
RLA(P, Q) = |shoelace_area( P ++ reverse(Q) )|      # mm²
```

This is the PCB translation of every "twist the pair / short direct
connections" rule: a twisted pair is a zero-area loop, and on a PCB the analog
is routing signal and return adjacent. Hum pickup and radiated coupling scale
with enclosed area (DVGA §14.2.2 p. 306: induced hum voltage grows with loop
area). Computable today from two routed paths; needs no field solver.

---

## 1. The metrics

Each metric: name, principle (with cite), exact formula, data needed, when it
applies, suggested weight (in **equivalent millimetres of wirelength** — see
§3 for why that currency).

### M1 `ground_common_impedance` — quiet returns must not share copper with noisy returns

**Principle.** A ground scheme must keep heavy/noisy supply and control
currents out of the signal ground path; every shared centimetre of ground
copper is a resistor that converts someone else's current into your grid
voltage (TPGB pp. 275–281; DVGA §14.2 p. 303 — minimise series impedance,
no loops, noisy currents away from quiet grounds; §14.2.3 p. 306 — the
transformer–rectifier–reservoir loop is a self-contained block nothing else
touches). Blencowe is explicit that in a high-gain design bad grounding alone
makes the amp unusable (TPGB p. 281).

**Formula.** The router routes GND as one tree per net. Given the declared
ground root (reservoir/entry pad) and pad noise weights `w(p)` derived from
`net_role` of the nets each pad's component also touches (quiet: grid/cathode
returns, w=0; noisy: supply, heater, control returns, w per class — Control 1.0,
Power 1.0, Heater 0.7, defaults):

```
for each quiet pad q, noisy pad n on the ground tree:
    shared(q, n) = length of path(q → root) ∩ path(n → root)
GCI = Σ_q Σ_n  w(n) · shared(q, n) · gain(stage(q))/gain_max
```

Paths in a tree are unique, so `shared` is exact and O(tree depth). The gain
factor makes a millimetre of shared copper under the first grid's return worth
more than the same millimetre at the output — Blencowe's ordering rule
(grounds joined progressively preamp→PSU, never interleaved: TPGB p. 276,
DVGA §14.2.5 p. 309) emerges as the GCI-minimal tree rather than being coded
as a special case.

**Data.** Routed GND `net_paths` + `star`/`net_role`/`stage` declarations.
**Applies** whenever a ground net is inside the region and roles are declared.
**Weight:** 200 mm at "bad" (see §3 normalization). The highest of any metric.

*Position on ground pours:* don't model pours yet. The router today produces
trees, and GCI is exact on trees. A partitioned pour with one junction point is
the PCB analog of multi-star grounding, but scoring it honestly needs current
density, not graph paths — defer, and say so in diagnostics when a pour is
detected in the source board.

### M2 `victim_coupling` — no noisy conductor runs parallel to a sensitive one

**Principle.** Field strength falls with distance; parallel runs integrate
coupling, perpendicular crossings don't (DVGA §14.1.2 p. 300; TPGB pp. 284–285
for heater dress specifically; TPGB pp. 287–288: conductors of *different
triodes* kept apart or high-gain designs oscillate via parasitic capacitance;
TPGB p. 53: high-gain preamps oscillate through exactly this path).

**Formula.**

```
VC = Σ over (aggressor net A, victim net V) pairs:
       agg(A) · sens(V) · PCI(A, V)
```

`agg` from role: heater 1.0, control/digital 1.0, supply 0.6, any *plate* net
0.5 (plates are the largest signal swings — DVGA p. 300 names power-valve
anode wires the worst offenders). `sens` from role: input 1.0, grid of stage k
scaled by downstream gain product `Π gains(k..end) / gain_max`, cathode 0.2,
everything else 0. The cross-triode oscillation rule is the sub-case A=plate
of stage j, V=grid of stage i≤j: `agg·sens` is then proportional to the
parasitic loop gain around that pair, which is the physically correct
weighting for stability risk.

**Data.** `RouteResult.tracks`, roles, stage gains. **Applies** with roles
declared; pairs with `agg·sens = 0` skipped. **Weight:** 150 mm.

### M3 `decouple_radius` + `stage_loop_area` — each stage's HF loop is local and small

**Principle.** Signal current circulates in the loop formed by a valve stage
and the smoothing cap that feeds it; the cap belongs next to its stage with
short direct connections, and components after a coupling cap ground at the
*following* stage's star (TPGB fig. 12.8/12.9 pp. 277–279, cap-too-far and
wrong-star listed as canonical mistakes; DVGA §14.2.4 figs. 14.12–14.13
pp. 307–308; TPGB p. 283: no more than two triodes per smoothing cap — a
schematic rule, but its layout shadow is the shared-cap loop).

**Formula.** Two levels. Placement-time (cheap, drives the SA):

```
DR(stage) = dist(cap⁺ pad, plate-load supply pad)
          + dist(cap⁻ pad, stage star / cathode-return pad)
```

Route-time (exact, judges):

```
SLA(stage) = RLA( routed path cap⁺→plate-load ,
                  routed path cap⁻→cathode-return )     # mm²
```

**Data.** `star(stage, cap_ref)` declaration, pads, routed paths. **Applies**
per declared stage with a star. **Weight:** 100 mm at bad
(DR ≈ 25 mm or SLA ≈ 400 mm² — *speculative thresholds; calibrate against
the Voxy hand layout, which is the reference "good"*).

### M4 `grid_stub_antenna` — nothing upstream of the grid stopper but the stopper

**Principle.** The grid stopper works by forming an RC with the valve's input
capacitance *at the grid pin*; copper between stopper and pin is outside the
filter and is an antenna feeding the highest-impedance, highest-gain node in
the amp. Mount it at the socket (TPGB Ch. 2 p. 40: as close to the valve as
possible, ideally on the socket itself).

**Formula.**

```
GSA(stage) = routed length from stopper output pad to grid pad
           + total length of any other copper on that net segment (branches)
```

v1's `adjacency_max_distance` gets the *components* close; GSA measures the
*copper*, which is what actually radiates/receives — a 3 mm center distance
routed as a 15 mm detour is a v1 pass and a real defect.

**Data.** routed path of the grid-side net, the stopper ref (from
`net_role(net, grid)` + the two-pad resistor bridging into it — the agent
names it in the `stage` declaration). **Applies** per stage. **Weight:** 80 mm
at bad (GSA ≈ 10 mm; good ≈ 2–3 mm).

### M5 `input_path_integrity` — the jack-to-first-grid run is sacred

**Principle.** Input wiring: shortest possible, tightly paired with its
return, shielded in high-gain amps (TPGB p. 40); ground bonds to chassis at
the input end (TPGB fig. 12.3 p. 274; DVGA p. 311: the entire ground system
meets chassis at the input jack); the input valve sits at the quiet end,
farthest from power (DVGA §14.1 p. 299). Everything after the first grid
amplifies whatever this run picks up by the whole chain's gain.

**Formula.** Composite over the input net I and its return R:

```
IPI = len(routed I) / len_manhattan(I)                 # detour ratio, ≥ 1
    + α · RLA(I, R) / mm²_ref                          # pairing with return
    + β · Σ_noisy PCI(noisy, I)                        # already in M2, but
                                                       #   counted again at 2×:
                                                       #   the front door is
                                                       #   special
    + γ · layer_changes(I)                             # each via moves the
                                                       #   reference plane
```

α=1/100 mm², β=2, γ=0.5 suggested. **Data.** `net_role(net, input)`, routed
paths. **Applies** when the input net is in the region. **Weight:** 120 mm.

### M6 `heater_dress` — the heater pair is tight, remote, and encloses nothing

**Principle.** Heater current is the largest in the amp; its field scales with
current, so: run the two legs as a tightly twisted pair (PCB: minimal
enclosed area between the legs), keep them at the edge/away from signal,
cross signal only at right angles, never loop around a socket, order the
chain so the input valve is last and carries the least current (TPGB
pp. 283–285, figs. 12.13–12.14).

**Formula.** Three terms over heater nets H⁺, H⁻:

```
HD = RLA(H⁺ path, H⁻ path) / mm²_ref            # pair tightness; twisted-pair analog
   + Σ_sockets loops_around(H, socket)           # winding number of the pair's
                                                 #   midline around socket center:
                                                 #   1 if the pair encircles it
   + (PCI(H, audio) term — lives in M2 with agg=1.0, not double-counted here)
```

The winding number is computable directly from the routed polyline vs. the
socket footprint center — an integer, penalized hard (each enclosure ≈ 50 mm
equivalent). **Data.** `net_role(H⁺/H⁻, heater)`, routed paths, socket
footprint positions. **Applies** when heater nets enter the region. DC-heater
boards (a Voxy option) drop `agg` for heaters to 0.2 but keep pair tightness
— ripple on a DC heater still radiates. **Weight:** 90 mm.

### M7 `stage_order_monotonicity` — signal flows one way across the board

**Principle.** Quiet end / noisy end: input at one end, power at the other,
stages in signal order between them; grounds and supplies daisy-chain in the
same order (DVGA §14.1 p. 299; TPGB p. 276). Violations create the geometry
for output-to-input feedback — the "squeal" path.

**Formula.** Placement-time (this one is a *placer* metric — routing can't fix
a scrambled floorplan). Project stage centroids onto the declared flow axis
(default: region long axis, agent may override):

```
SOM = Σ over stage pairs i < j with x_j < x_i:  (x_i − x_j) · g(i, j)
where g(i, j) = Π stage gains i..j / gain_max         # loop-gain weighting
```

Inversions between adjacent unity-ish stages cost little; the output stage
landing beside the input costs its full loop gain times the overlap distance.
**Data.** `stage` declarations with gains, placements. **Applies** ≥ 3 stages
in region (with 2 it degenerates into M2). **Weight:** 60 mm — low, because
within one *functional region* (the intended granularity of optimize_region)
stage count is small and M2 already prices the dangerous proximities; SOM
mostly matters when someone fences half the board.

### M8 `hv_clearance` — creepage is law, not preference

**Principle & external fact.** IPC-2221B Table 6-1, external uncoated
conductors at sea level: **0.6 mm** up to 150 V, **1.25 mm** for 171–300 V,
**2.5 mm** for 301–500 V, then 2.5 mm + 5 µm/V above 500 V; polymer-coated
external drops to 0.4 mm/0.8 mm — and solder mask does *not* qualify as
polymer coating under IPC (standard industry reading). Verified via
[smpspowersupply.com](https://www.smpspowersupply.com/ipc2221pcbclearance.html)
and [Altium](https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design).

**The uncomfortable arithmetic:** AGENTS.md states clearance today ≈ grid
pitch. At the recommended 0.5 mm pitch, adjacent-channel copper sits ~0.5 mm
apart minus half-widths — for a 300 V B+ net that violates IPC by ≥ 2.5×.
**This is the one metric that is not a score at all: it's a legality
condition,** same tier as routing failures. A candidate violating HV clearance
must never outrank one that doesn't, regardless of every other number.

**Formula.**

```
HVV = Σ over (HV item, other-net item) pairs:
        max(0, s_req(V_class) − clearance(item_a, item_b))     # mm, summed depth
```

over pads *and* routed tracks, plus HV-to-board-edge at the same s_req.
`V_class` from the net class's declared working voltage (one number per class
in the `.kicad_pro`, or the constraint below). **Data.** pads, tracks, class
map. **Applies** always, once any class declares a voltage. **Weight:** none —
hard tier (§3).

### M9 `thermal_keepout` — electrolytics and vactrols away from hot glass

**Principle.** Valves need spacing of at least their own diameter for mutual
cooling; electrolytic lifetime collapses with heat, so keep them away from hot
valves and power resistors (DVGA §14.1 pp. 299–300). For Voxy add the
hand-built vactrols: LDR dark resistance drifts with temperature
(*my extension, not Blencowe — labeled speculation, but cheap to include*).

**Formula.** Placement-time:

```
TK = Σ over (hot ref h, sensitive ref s):  max(0, R_th(h) − dist(h, s))
```

`R_th` = envelope radius × 2 for tube sockets (default), declared per ref
otherwise; sensitive refs = electrolytics (footprint match) + agent-listed.
**Data.** placements, footprints. **Applies** whenever a socket is in/adjacent
to the region. **Weight:** 50 mm.

### Out of computable scope (named so nobody pretends)

Chassis bonding point, transformer core orientation and the headphone test
(DVGA §14.1.1), off-board wire twisting, insulated jack selection: real craft,
off the board. They stay in AGENTS.md as Phase-0 agent duties, not metrics.

---

## 2. Constraint vocabulary v2 — proposed additions (closed enum)

Style rules inherited from `constraints.py`: fixed signatures, unknown name =
hard ValueError quoting the valid set, penalties commensurate with millimetres
so the SA energy can sum them, `str()` round-trips through the parser.

### Declarations (facts the checker consumes; never "violated" themselves)

```
stage(name, [refs...], gain)
    Functional stage membership + small-signal voltage gain (linear, not dB).
    Checker: none. Feeds M1–M7 weighting. Malformed refs → parse error
    against known_refs, as today.

net_role(net, role)      role ∈ {input, grid, plate, cathode, ground,
                                 supply, heater, control}
    Circuit role of a net. Checker: none. One role per net; re-declaration
    with a different role is a parse error (contradictory intent must be
    loud, not last-wins).

star(stage_name, ref)
    Names the decoupling/smoothing cap whose ground pad is the stage's local
    star; the reservoir's star is declared as star(root, ref). Checker: the
    ref must have a pad on a ground-role net, else parse error.
```

### Restrictions

```
clearance_min(class_a, class_b, mm)
    Minimum copper-to-copper clearance between two net classes ("*" wildcard
    allowed for class_b). Checker: pad/track pair distances (same primitive
    as M8). Penalty: Σ penetration depth, mm. HV pairs are additionally
    HARD (legality tier) — see §3. This also finally gives the router real
    per-class clearance instead of pitch-as-clearance.

dress_min_separation(class_a, class_b, mm)
    No same-layer parallel adjacency closer than mm between the classes;
    perpendicular crossings exempt by construction (PCI primitive: only
    overlapping parallel projections count). Checker: PCI with gap threshold
    mm, overlap tolerance 2× trace width. Penalty: Σ (mm − gap) over
    offending overlap length, normalized per mm of run → mm-commensurate.

loop_area_max(net_a, net_b, mm2)
    Enclosed area between the routed paths of two nets (signal/return, or
    heater pair) ≤ mm2. Checker: RLA. Penalty: (RLA − mm2) / 10 mm (area
    excess priced at 10 mm² per equivalent-mm — tunable constant, stated in
    the signature docs, not hidden).

pair_route(net_a, net_b, mm)
    The two nets route as a pair: maximum perpendicular separation between
    the polylines, sampled at pitch, ≤ mm along their common extent.
    Checker: hausdorff-lite over sampled points. Penalty: mean excess
    separation, mm. (Heater legs; also usable for a send/return pair.)

stub_max(net, ref, mm)
    Copper on net beyond ref's pad (the grid side of a stopper) ≤ mm total
    routed length including branches. Checker: routed-tree length past the
    pad node. Penalty: excess mm.

ground_topology(scheme, root_ref)      scheme ∈ {multi_star, single_star, bus}
    Validates the routed ground tree against the declared scheme:
    multi_star — every pad declared to a stage must reach root_ref via its
    stage's star pad before sharing any tree edge with a pad of another
    stage or of a noisy role; single_star — all ground pads join at root
    with no shared intermediate edges; bus — tree is a path and pads attach
    in declared stage order. Checker: tree-path intersection (M1 primitive).
    Penalty: total illegally-shared length, mm. Verdict ok/violated like
    every other Check, so it can be hard or soft per call.

thermal_keepout(ref, mm)
    No electrolytic-class or agent-listed sensitive ref's courtyard within
    mm of ref's center. Checker: courtyard-to-center distance. Penalty:
    penetration depth, mm.
```

Deliberately **not** proposed: `guard_trace`, `pour_partition`, anything
needing zones — the router has no zone model; promising constraints it can't
check violates the project's honesty rule (limitations are disclosed, not
hidden). Also not proposed: free-form `weight(metric, w)` — weights are call
parameters, not constraints, or the enum stops being closed in spirit.

---

## 3. Scoring recipe — composing with wirelength/vias without drowning either

v1 ranks by `failures ≫ constraint_violations ≫ wirelength + via_w·vias`.
v2 keeps the lexicographic skeleton and inserts one tier:

```
score = C_fail · failures                     # tier 1: it must route
      + C_hard · hard_violations              # tier 2: legality —
                                              #   HV clearance lives HERE
      + Σ_i  w_i · φ_i(m_i)                   # tier 3: analog craft
      + wirelength_mm + via_w · vias          # tier 4: geometry
```

with `C_fail ≫ C_hard ≫ max possible tier-3 sum`, as today.

**One currency.** Every `w_i` above is denominated in *equivalent millimetres
of wirelength*: the detour you would gladly accept to fix the defect. That is
the design decision that keeps tier 3 and tier 4 composable instead of a
weight soup — on an amp board, a centimetre of extra track is cheap and a bad
ground is fatal, and the weights (200 mm for GCI down to 50 mm for thermal)
say exactly that, in the router's native unit.

**Hinge normalization (the anti-drowning device).** Raw metrics have wild
scales (mm², dimensionless ratios, integers). Each is passed through

```
φ_i(m) = clip( (m − good_i) / (bad_i − good_i), 0, 3 )
```

where `good_i` is craft-clean (φ=0 — a clean board pays nothing and ranking
degrades gracefully to v1) and `bad_i` is one clear defect (φ=1, costing
exactly `w_i` mm). The clip at 3 means no single metric can contribute more
than 3·w_i, so a pathological outlier cannot drown the other metrics or the
wirelength term — and can never climb into tier 2. `good_i`/`bad_i` defaults
ship in code; **calibration protocol:** score Andrew's hand-routed Voxy board,
which should land near φ≈0 on every metric — any metric it fails is
mis-thresholded, not evidence the hand layout is wrong. (The hand layout is
the ground truth this whole document is trying to reproduce.)

**Generate/judge split, preserved.** Placement-time proxies (DR, SOM, TK,
Euclidean stand-ins for PCI) join the SA's cheap energy so the annealer
*explores toward* craft; route-time exact values (GCI, VC, SLA, GSA, IPI, HD)
are computed on the routed candidate and *decide*. Same contract as HPWL vs.
the router today: the cheap term never gets the final word.

**Diagnostics grow one field.** Alongside `binding_constraint`:
`binding_metric` — the tier-3 metric with the largest φ across the candidate
set, plus its worst pair/net ("VC: heater H+ ∥ net GRID1, 22 mm at 0.6 mm on
F.Cu"). The per-candidate `metrics` dict reports every φ_i and raw m_i. An
agent told *which* craft rule is binding can fix the fence, add a
`dress_min_separation`, or accept the tradeoff — an agent given one scalar
can only retry. This is the same argument ARCHITECTURE.md makes for
diagnostics generally, applied to analog craft.

**Determinism.** Every metric is a pure function of candidate geometry +
declarations; same seed, same candidates, same scores, bit for bit.

---

## 4. Top 5 for a Voxy-class board, argued

Voxy: two cascaded 12AX7 stages (loop gain order 10³), SMD 1206, film caps in
the audio path, hand-built vactrols, and — the thing vintage craft lore never
had to price — an Arduino doing topology switching. The aggressor set is
bigger than any amp Blencowe's chapter contemplates, and the board is small,
so everything is near everything.

1. **M8 `hv_clearance`** — first not because it's subtle but because it is
   currently *wrong*: pitch-as-clearance at 0.5 mm is under the IPC-2221B
   1.25 mm floor for a 250–300 V B+ by 2.5×. Every other metric ranks
   candidates; this one determines whether the tool's output is safe to
   fabricate. It must land before any craft scoring does.
2. **M1 `ground_common_impedance`** — the classic first failure of a PCB tube
   preamp, and Voxy's MCU + vactrol-LED drive currents are precisely the
   "non-audio grounds are noisy, return them to the reservoir, never to an
   audio star" case (DVGA p. 311). Two cascaded stages of gain make
   millivolts on shared ground copper audible; Blencowe's own escalation —
   grounding decides usability in high-gain designs (TPGB p. 281) — puts it
   at the top of the soft tier.
3. **M2 `victim_coupling`** — the router's channel style manufactures
   parallel runs, the board mixes 6.3 V heater and digital control lines with
   grid traces in a few thousand mm², and no existing term even observes the
   problem. Also the only metric that catches the cross-stage plate→grid
   proximity that turns a compact layout into an oscillator (TPGB
   pp. 287–288).
4. **M3 `decouple_radius`/`stage_loop_area`** — two stages off one supply
   chain is exactly the motorboating geometry (TPGB p. 283, figs. 12.8–12.9);
   on a PCB the "cap close, connections direct" rule costs nothing when
   scored during placement and is nearly unfixable after commit. Highest
   value-per-flop: the placement proxy is two distance lookups.
5. **M4 + M5 (`grid_stub_antenna`, `input_path_integrity`)** — bundled as
   front-door integrity: the input run and first grid stub set the noise
   floor the entire chain amplifies. Cheap to compute, and they convert v1's
   already-planned `adjacency_max_distance` acceptance test (grid stopper at
   3 mm) from a component rule into a copper rule, which is the one that
   matters.

M6 heater dress just misses the cut only if Voxy commits to DC heaters; on AC
heaters it displaces #5. M7 and M9 are worth carrying because they're nearly
free, but they won't decide a Voxy region.

---

## 5. Build-order note (non-binding)

The two primitives (PCI, RLA) plus the declaration triplet unlock everything
else; `clearance_min` shares its checker with M8 and also retires the
pitch-as-clearance disclosure in AGENTS.md. Suggested order: declarations →
clearance_min/M8 (legality first) → PCI/M2 → tree-path sharing/M1 → RLA/M3/M5
→ the rest. Each lands as a pure scoring function over existing structures;
none requires touching the kernel, the lattice, or the SA move set.
