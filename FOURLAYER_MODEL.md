# The 4-layer model — vias are columns, planes are supernodes

Design document, 2026-07-18. No code here changes anything yet; this is the
spec the multilayer work lands against.

**Position, in four sentences.** A via is a *column*, not a ladder: one drill,
one shared resource, one price, connecting every layer at once — the lattice
gets a via-column node per (x,y) and drops the adjacent-layer edge model.
A plane is a *supernode*, not a layer: declared explicitly as `LAYER=NET`,
cross-checked against the board's own zone, and reached through any via
column — it never appears in the lattice as routable copper. Voxy and five of
the seven bench boards are signal/plane/plane/signal, so the cheapest correct
4-layer model is today's 2-signal-layer lattice plus plane supernodes — the
full 4-signal-layer lattice is real but third in line, needed today by exactly
one bench board. And plane binding is never guessed from net names: Voxy
carries **six** distinct GND nets (star grounding — `GND-24`, `GND-5`,
`GND-B`, `GND-C`, `GND-D`, `GND-MASTER (PSU)`); any "name contains GND"
heuristic would quietly weld Andrew's star ground into one blob, which is the
one mistake a tube-amp layout tool must never make.

---

## 1. Audit: what the code does today, and what is actually wrong

Read against `lattice.py` (`build_lattice`, `lattice_for_board`),
`pathfinder.py` (`paths_to_tracks`, usage accounting), `writeback.py`
(`_file_facts`, via emission).

### 1.1 Via edges are adjacent-layer ladders with per-hop cost

`build_lattice` adds `(x,y,l) ↔ (x,y,l+1)` edges, each at full `via_cost`
(lattice.py, the `if l + 1 < L:` arm). Consequences on `L=4`:

- **3× miscost.** F.Cu → B.Cu at one (x,y) traverses three edges and pays
  `3 · via_cost = 24` grid steps (board default `via_cost=8`). Physically it
  is one through-drill. Result if we simply passed `--layers
  F.Cu,In1.Cu,In2.Cu,B.Cu` today: the router would treat the far side of the
  board as three vias away and starve the inner layers — the model
  *discourages exactly the thing 4-layer routing exists for*.
- **The blocking hole.** Two nets may make *disjoint partial spans at the same
  (x,y)*: net A hops F.Cu→In1.Cu (occupies layer-0 and layer-1 nodes), net B
  hops In2.Cu→B.Cu (occupies layer-2 and layer-3 nodes). Node capacity 1 is
  satisfied — no conflict is ever seen. Physically both are the **same
  through-drill** (see §1.3: cheap fab has no blind/buried vias), i.e. a dead
  short between two nets that the model charges full price for and forbids
  nothing about. This is the "charges 3× and forbids nothing it should"
  failure in one concrete example.
- **Accidentally-correct corner:** on `L=2` (all current usage) the model is
  coincidentally sound — the single hop costs one `via_cost`, and the path
  threads both endpoints' nodes so the column *is* fully occupied. Every
  current result stands; the wound only opens when inner layers become
  routable.

### 1.2 Emission is (mostly) already right — the search is what lies

- `paths_to_tracks` (pathfinder.py) emits **one via per distinct layer-change
  (x,y) per net** (`via_pts` is a set of (ix,iy)). So even a 3-hop ladder
  collapses to one emitted `(via)`. The *emitted board* and the reported
  `via_count` are correct through-via semantics; only the *search cost* and
  the *blocking model* are wrong. The bug is in pricing and legality, not in
  output.
- `write_routed_copy` emits `(layers "F.Cu" "B.Cu")` — but not as a
  hardcoded pair: `_file_facts` takes the file's own copper table and spans
  `copper[0] .. copper[-1]`. On a 4-layer file whose layer table reads
  F.Cu/In1.Cu/In2.Cu/B.Cu (Voxy's does, in that stackup order), the span is
  still `F.Cu B.Cu` — which is **exactly what KiCad wants for a through
  via**. Per the KiCad dev docs, the via's `layers` token "define[s] the
  canonical layer set the via connects", the type token is *omitted* for a
  through via, and only `blind`/`micro` vias carry a type and a partial span
  ([KiCad board file format][kicad-fmt]). Verdict: **via emission needs no
  change for through-vias on 4-layer boards.** Blind/buried emission would
  need the type token and a true partial span — deliberately out of scope
  (§1.3).
- One latent legality gap the audit surfaced: today a via can land **under a
  foreign SMD pad** — the SMD pad owns only its own layer's nodes, the other
  layers' nodes at that (x,y) are unowned, so a layer change there is legal
  in-model while the physical barrel would hit the pad's copper. Rare at
  0.5 mm pitch on 1206-class boards, but it is the same class of bug as the
  blocking hole and the column model fixes both with one rule (§2.3).

### 1.3 Fab reality check: through-vias only

JLCPCB — the fab this project's boards actually go to — does not offer
blind/buried vias on its standard service; a 4-layer board gets through-vias,
period ([JLCPCB: Blind Via Support][jlc-blind]). So the correct v2 physical
model is: **every via connects all copper layers and blocks its (x,y) column
everywhere.** Blind/micro vias are a later, fab-profile-gated feature, not a
generality to design in now.

---

## 2. The via-column encoding

### 2.1 Structure

Add one **via-column node** `V(x,y)` per lattice (x,y) — a virtual layer at
index `L`, so node ids stay `l·W·H + y·W + x` with `l ∈ [0, L]` and every
existing helper (`node`, `coords`, `snap`) works unchanged; `coords()`
returning `il == L` *means* "in the barrel".

- **Delete** the adjacent-layer edges entirely.
- **Add** `(x,y,l) ↔ V(x,y)` for every signal layer `l`, each at cost
  `via_cost / 2`.

Any layer-to-any-layer transition is then `layer → V → layer` =
`via_cost/2 + via_cost/2` = **one `via_cost`, regardless of span** — the
single-drill price the physics demands. On `L=2` the totals are identical to
today's model (one hop of 8 becomes two hops of 4), which is the regression
property that lets this land without recalibrating the bench.

**No kernel changes.** This is graph shape plus host bookkeeping; the wavefront
kernel, `batched_sssp`, and `extract_path` consume CSR and know nothing about
layers. That is the headline argument for this encoding over anything cleverer.

### 2.2 The column as one shared resource

PathFinder's capacity is per node. `V(x,y)` has capacity 1, so **two nets can
never via at the same (x,y)** — the §1.1 blocking hole closes by construction.
Two host-side rules complete it:

1. **Usage injection.** When a kept path contains `V(x,y)`, its net's usage is
   tallied on `V(x,y)` *and on every* `(x,y,l)` — the barrel passes through
   all layers, so a foreign trace crossing (x,y) on In1.Cu must see the
   contested node and negotiate. (The reverse needs no rule: the trace net
   occupies `(x,y,In1)`, so a later via attempt through that column collides
   there.)
2. **Symmetrically, `V` never appears in wirelength**; `paths_to_tracks` /
   smoothing treat `… (x,y,l₁), V(x,y), (x,y,l₂) …` as one layer change —
   the existing `al != bl` test needs a two-line generalization to skip the
   virtual layer and still emit exactly one via.

### 2.3 Legality at the column

Hard-block `V(x,y)` (via the existing per-net mask machinery — `node_owner`
plus lazily-built uint8 masks, no new mechanism) when:

- any `(x,y,l)` is owned by a *foreign* pad (minus the
  `pad_overlap_allowances` pairs, exactly as for traces) — fixes the
  under-foreign-SMD-pad via of §1.2;
- (x,y) lies under **any** SMD pad rect, own net included — via-in-pad is a
  deliberate manufacturing decision (paste wicking), not something an
  autorouter should emit silently. Flag to re-enable later.
- A through-hole pad's (x,y): its column is its net's property — its own net
  changes layers there for free (already true: TH pads snap on every layer),
  foreign nets are excluded by ownership inheritance.

### 2.4 CSR-size math at Voxy scale

Voxy-arduino via `board.load_board`: bbox 300.3 × 279.0 mm, 1622 pads, 487
nets, copper F/In1/In2/B. At pitch 0.5 mm with the standard 2·pitch margin:
`W = 606, H = 563, W·H = 341,178`.

| model | nodes N | undirected edges | CSR entries (2×) | col_idx+weight | row_ptr |
|---|---|---|---|---|---|
| today, L=2 (current usage) | 682,356 | 1,362,374 intra + 341,178 via | 3,407,104 | 27.3 MB | 2.7 MB |
| today's model naively at L=4 | 1,364,712 | 2,724,748 intra + 1,023,534 via | 7,496,564 | 60.0 MB | 5.5 MB |
| **column, 2 signal + 2 plane supernodes (v2, Voxy)** | 1,023,538 | 1,362,374 intra + 682,356 col + 682,356 plane | 5,454,172 | 43.6 MB | 4.1 MB |
| **column, 4 signal layers (v3)** | 1,705,890 | 2,724,748 intra + 1,364,712 col | 8,178,920 | 65.4 MB | 6.8 MB |

(intra-layer per "both"-direction layer: `H(W−1) + W(H−1)` = 681,187;
column edges `L·W·H`; plane edges §3.)

Graph memory is a non-issue everywhere. The number that actually moves is the
**distance matrix**: `(N, B)` float32 at B = 128 planes is 524 MB for the v2
graph and 873 MB for v3 — trivially fine on the 128 GB Mac Studio target,
tolerable-but-noticeable on a 24 GB M4 Pro. Region-solver lattices (the actual
hot path per ARCHITECTURE.md) are 30–60× smaller and don't care.

---

## 3. Planes: declared, supernoded, never routed

### 3.1 The model

A declared plane contributes **one supernode `P` per plane layer** (2 extra
nodes, not 341k): `V(x,y) ↔ P` for every column, cost `via_cost/2` — so
surface pad → F.Cu route → `V` → `P` prices at exactly one via, consistent
with §2.1. `node_owner[P] = bound_net` and the existing mask machinery makes
the plane unreachable for every other net; foreign nets' vias still pass
*through* the plane freely (fab antipads handle clearance — modeling antipad
perforation of the plane is real EMC/hum territory but **speculation** at this
stage; later, §5).

**Plane layers do not appear in the lattice.** No nodes, no edges, no
"routable at high cost". Reasons this beats routable-with-penalty:

- Signal traces on a plane layer slice the return path — the one thing a
  plane is *for*. A cost knob invites the router to do EMC damage exactly
  when congested, i.e. exactly when it's most tempted.
- The bench fleet says humans don't do it either: on the 4-layer boards, inner
  copper is planes plus a handful of jumpers (§4 table). Modeling the common
  case exactly beats modeling the rare case badly.
- It makes Voxy's 4-layer lattice *the same size as today's 2-layer one*
  (§2.4 row 3). Correctness that makes the problem smaller should win.

### 3.2 What "routing GND" means when a plane exists

For the bound net, connectivity changes shape: the plane joins everything
touching it, so the MST-over-pads decomposition is replaced by a **star: one
connection per pad, target = `P`**.

- An SMD pad of the bound net routes: pad → (short surface escape) → nearest
  legal column → `P`. Because own-net reuse is free at the source set
  (pathfinder's kept-path seeding), a cluster of nearby GND pads converges on
  a shared tap via *without any new mechanism* — tap sharing falls out of the
  existing machinery.
- A through-hole pad of the bound net is **already connected** (its barrel
  meets the plane; KiCad zone fill connects it, thermally or solid). Zero
  connections generated. Caveat, stated in diagnostics not hidden: board.py
  doesn't parse per-pad `zone_connect` overrides yet, so a pad explicitly set
  to "no connection to zones" would be wrongly assumed connected — parse it
  in v2b (it's a one-token read) or disclose.
- Wirelength for the bound net collapses; via count rises by one per tap.
  Both are *correct* — and they are what makes human via counts finally
  comparable (§6).

### 3.3 How the router knows a net belongs to a plane

**Explicit declaration, cross-checked against the board's own zones. Never
names, never net classes.**

- Surface: `--plane In1.Cu=+5V --plane In2.Cu=GND` on the CLIs;
  `plane_layers={"In1.Cu": "+5V", ...}` in `optimize_region`. One net per
  plane layer, plane layers excluded from `--layers`.
- Cross-check: KiCad zones carry `(net …) (net_name …)` and their layer(s)
  in-file ([KiCad format docs][kicad-fmt]) — board.py grows a *zone-header*
  parse (net, layers, rough bbox coverage; not the fill polygons).
  - Declared but **no matching zone** → hard error. The emitted taps would
    connect to nothing; a plane that doesn't exist in copper is not a plane.
  - Zone present on an inner layer but **undeclared** → route normally, warn:
    "inner plane zone (net +5V, In1.Cu) detected — pass --plane to use it."
    Mirrors AGENTS.md's existing "inner layers are usually planes — do not
    route signals there without being asked."
  - Declared net ≠ zone net → hard error naming both.
- Why not net classes: classes are width/clearance families. On Voxy,
  `GND-B/C/D/…` and `GND-MASTER (PSU)` would rightly share a Power-ish class
  while **only one of them is the plane** — the class is structurally unable
  to express the binding. Why not names: six GND nets, above. Star grounding
  is the flagship use case, and explicit binding is what keeps a star a star.
- Emission for taps: plain through `(via … (layers "F.Cu" "B.Cu") (net
  "GND"))` — correct per §1.2; KiCad connects barrel to zone at refill.
  Report in the stats block that the user must refill zones (B) on open.

### 3.4 Voxy reality check (honesty section)

Voxy-arduino **today** declares 4 copper layers but contains *zero copper
zones*, 32 tracks, 2 vias — the inner planes are aspirational, not present.
So the v2 plane feature's first Voxy act is a Phase-0 duty for the driving
agent (AGENTS.md): create the In1/In2 zones via Konnect (pick which single
GND net — presumably `GND-MASTER (PSU)` — and which supply gets a plane, a
*circuit* decision that belongs to Andrew/the agent, not this tool), then
declare and route. The tool must refuse to invent the zones itself.

---

## 4. Layer assignment and direction preference

Per-layer copper occurrences and plane zones across the bench fleet (grep of
`(layer "…")` occurrences, segment-dominated; zone nets from zone headers —
coarse but decisive):

| board | F.Cu | In1.Cu | In2.Cu | B.Cu | inner zones |
|---|---|---|---|---|---|
| icebreaker-bitsy-v1.1c | 576 | 4 | 17 | 215 | 27 zones spanning In1+In2 (GND/supplies) |
| icebreaker-v1.0e | 1148 | 8 | 58 | 431 | In1/In2 supply+GND zones |
| kicad-demo-video | 3771 | 74 | 544 | 3743 | In1 = +5V plane, In2 = GND plane |
| sparkfun-iot-redboard-rp2350 | 999 | 44 | 1 | 535 | all-layer GND zone incl. In1/In2 |
| rpi-pico-vga | 433 | 1 | 1 | 33 | none parsed (KiCad 5) |

Every 4-layer board in the fleet is **signal / plane / plane / signal** with
occasional inner jumpers (kicad-demo-video's 544 In2 segments are the only
substantial inner routing, and even there inner copper is ~7% of outer).
Nobody ships signal/signal/plane/plane, and for good reason — it gives the two
signal layers asymmetric return-path quality and puts all crosstalk on one
pair. Position:

- **v2**: stackup is `signal, plane, plane, signal`; the direction model is
  untouched — outers keep `directions="both"` with the existing H/V
  preference split (F=H-pref, B=V-pref) and `dir_penalty=1.25`.
- **v3** (4 signal layers, when a board earns it): preferences alternate
  H,V,H,V by layer index — `build_lattice` already does exactly this for any
  L, so the direction scheme needs *zero* new design, only the via model
  under it fixed. One considered refinement, not committed: a higher
  `dir_penalty` on inner layers to keep them disciplined trunk channels
  (inner layers are cheap to keep clean because nothing has to escape on
  them — no pads). Calibrate on the bench, not by taste.
- Blind/buried never enter the direction discussion because they don't exist
  at the target fab (§1.3).

---

## 5. Migration path

**v2a — via columns (small, pure correctness).** `build_lattice` grows the
virtual column layer + edge changes (§2.1); usage injection + emission skip
(§2.2); column legality rules (§2.3). No kernel work, no new file formats.
L=2 behavior is cost-identical, so the bench doesn't recalibrate. This is the
cheapest correct subset and unblocks everything else.

**v2b — planes.** Zone-header parsing in board.py (+ `zone_connect`),
`--plane LAYER=NET` on pathfinder/writeback CLIs and in the region-solver
call, supernode + star connectivity for bound nets, tap emission + refill
notice, the declaration/zone cross-check matrix (§3.3), and the AGENTS.md
Phase-0 paragraph (planes are a circuit decision; create zones first; never
bind by name). v2a+v2b together make Voxy's real stackup routable *at today's
lattice size*.

**v3 — true 4-signal routing.** `--layers` with >2 signal layers over the
column model; memory row 4 of §2.4; batch-size tuning for the (N,B) distance
matrix on sub-Studio machines. Needed today by exactly one bench board
(kicad-demo-video) — which is also the largest and the best stress test.

**Later, explicitly not now:** blind/micro vias behind a fab profile (the
emission side is known: type token + true span, [KiCad docs][kicad-fmt]);
split planes / multiple zones per layer (Voxy may eventually want *two* ground
plane islands — the star, again); plane-perforation congestion (a cost on
columns proportional to local tap density — **speculative**, bench it before
believing it); per-pad thermal-relief inductance (out of scope, probably
forever).

## 6. What the bench gains

Today every 4-layer board is scored as if 2-layer, which biases *against* the
router twice: it must spend F/B copper on nets the human sank into planes, and
the human's via count includes hundreds of plane taps the router isn't allowed
to make. After v2b the comparison becomes fair on:
sparkfun-iot-redboard-rp2350 (GND plane; 392 human vias), icebreaker-v1.0e
(157), icebreaker-bitsy-v1.1c (79), kicad-demo-video (+5V/GND planes; 808 —
fair-*er*, fully fair only after v3 opens its inner jumpers), and rpi-pico-vga
(4-layer but zone-free as parsed — verify its KiCad-5 zones before claiming
it). Prediction, labeled as such: on the S-P-P-S boards the router's fully
routed fraction rises materially once GND/+5V leave the F/B congestion
budget — those are the highest-fanout nets on every one of these boards
(212 GND-family pads on Voxy alone).

[kicad-fmt]: https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/
[jlc-blind]: https://jlcpcb.com/help/answers/detail/659-Blind%20Via%20Support
