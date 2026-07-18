# The visual judge — "if it looks good, it is good"

Design for a vision-LLM layout judge that ranks `optimize_region` candidates the
way a human expert does: by looking at them. A senior layout engineer judges a
board in seconds, and that glance compresses decades of correlated craft — flow,
alignment, breathing room, local tidiness, signal-path legibility. We treat
layout quality like face generation: the metrics are the anatomy, the glance is
whether it looks like a face.

**Honest position, stated once:** looks are a strong *proxy* for quality, not
the definition of it. Hard legality — DRC, constraints, router success — is the
structure; the visual judge is the gestalt. Both, like face generation. The
fusion rule in §4 encodes this: the judge is never allowed to promote a
candidate past a legality difference. It only orders candidates the metrics
can't tell apart.

Everything here builds on assets that already exist: `render.py` produces
deterministic SVGs of any routed board, `optimize_region` v1 (REGION_SOLVER.md)
returns K candidates each with an SVG, and `bench/boards/` holds 7
professionally human-routed boards *plus our routed versions of the same
placements* — a ready-made calibration set of real faces vs generated faces.

---

## 1. The judge protocol

### Pairwise comparison, position-swapped. Not rubric scores, not rank-K-at-once.

The LLM-judge literature is unusually unanimous here:

- **Pointwise/rubric scoring is unreliable for selection.** Absolute scores
  have "poor to moderate" psychometric reliability — one study attributes
  ~45% of score variance to within-judge noise, and judges flip verdicts on
  re-ask at a ~14% mean rate ("The Coin Flip Judge", arXiv:2606.13685).
  Worse, selection is a *within-prompt ranking* problem: a judge whose scores
  correlate decently with quality globally can still capture only ~21% of the
  achievable best-of-4 improvement, while explicit pairwise judging recovers
  ~61% ("When LLM Judge Scores Look Good but Best-of-N Decisions Fail",
  arXiv:2603.12520). Picking the best of K candidates is exactly best-of-N
  selection — pairwise is the right tool.
- **Pairwise grounds each candidate in the other** and agrees best with humans;
  this holds for vision judges too (MLLM-as-a-Judge, arXiv:2402.04788: GPT-4V
  reached ~78% human agreement on pair comparison, notably better than its
  absolute scoring).
- **Position bias is real and must be engineered away**, not hoped away
  (Zheng et al., MT-Bench, arXiv:2306.05685; Wang et al., "LLMs are not Fair
  Evaluators", arXiv:2305.17926). The standard mitigation — run A/B and B/A,
  count only agreeing verdicts, treat splits as ties — catches more bias than
  any prompt trick.
- **Verbosity/informativeness bias**: judges over-reward the response with more
  *stuff* in it. The layout analog is a judge favoring the render with more
  annotation or denser copper "because it looks more finished". Mitigation:
  identical render style for both images, verdict-first structured output,
  rationale capped.

Rank-K-at-once is rejected: position bias compounds across K slots, attention
dilutes across K images, and cross-image indexing errors ("image 3" meaning
image 2) are a known VLM failure. K stays small (≤5), so pairwise is affordable.

### Verdict format

One judge call = two images (A, B) + the rubric + a forced structured verdict:

```json
{
  "winner": "A" | "B" | "tie",
  "margin": "clear" | "slight",
  "qualities": {"flow": "A", "alignment": "tie", "congestion": "B", ...},
  "evidence": "B has a via staircase along the left edge and a congestion
               knot at the QFN's south escape; A's bus to the connector runs
               as one aligned ribbon."
}
```

Rules baked into the prompt: verdict fields come *first* (no chain of prose
before the choice — prevents the rationale from talking the verdict into
verbosity bias); `evidence` must cite locatable features ("near the large QFN",
"top-left corner"), max two sentences; the rubric qualities are an attention
checklist, **not** numeric scores — the per-quality field is itself a mini
pairwise verdict, which is the reliable primitive.

### Rubric — nine gestalt qualities

Wording an expert would endorse; each is one line in the judge prompt.

1. **Flow coherence** — the signal path reads in one direction; connected
   stages sit near each other; traces don't double back across the board.
2. **Alignment discipline** — related traces run as ordered bundles; parallel
   runs share spacing; jogs happen at shared x/y lines, not randomly.
3. **Breathing room** — copper density is even; open space sits where the
   circuit is quiet, not trapped as unusable slivers between knots.
4. **Congestion hot-spots** — no local knots: many nets squeezed through one
   gap, traces threaded between pads that had room elsewhere.
5. **Via discipline** — vias look purposeful (layer transition at a sensible
   place, stitching in rows) vs scattered confetti or staircase chains that
   ladder a single net across the board.
6. **Detour honesty** — each trace takes a path a human would believe: roughly
   the direction of its endpoints, detouring only around visible obstacles.
7. **Escape cleanliness** — pad fan-outs leave components at uniform angles
   and lengths; fine-pitch escapes look combed, not tangled.
8. **Layer idiom** — each copper layer has a discernible habit (e.g. F.Cu
   mostly horizontal, B.Cu mostly vertical); layer changes serve that idiom.
9. **Symmetry echo** — where the circuit is symmetric (differential pairs,
   stereo channels, matched stages), the copper is visibly symmetric too.

The prompt also carries one **idiom line** derived from board metadata
(through-hole/turret-era vs fine-pitch SMD — computable from min pad size and
pad counts, which `board.py` already knows) so the judge scores the layout
against its own genre. This is the main defense against style bias (§6).

### Tournament and budget per `optimize_region` call

The router already returns candidates ranked by
`failures ≫ violations ≫ wirelength + via_weight·vias`. The judge does **not**
re-rank all K. It runs a *challenge ladder on the metric-tied band* (defined in
§4): within the band, compare adjacent pairs, winner advances; every comparison
is run in both orders. Split verdicts = tie = keep router order (ties are
respected, not agonized over — a tie between two good layouts costs nothing).

Budget arithmetic (defaults, K=5, band of 3):

- ladder = 2 pairs × 2 orderings = **4 judge calls**, hard cap 8.
- one call ≈ two ~1536 px PNGs (~1.6–3k tokens each at current image-token
  rates) + ~800-token prompt + ~300-token verdict ⇒ ~5–8k in / 0.3k out.
- at Sonnet-tier list pricing ($3/$15 per MTok) ≈ **$0.02–0.03 per call**, so
  **≈ $0.10–0.25 per optimize_region invocation**; Opus-tier ($5/$25) roughly
  doubles that. Calls are independent → parallelize; wall-clock adds ~10–30 s.
- calibration sweeps (§3) run through the Batch API at a 50% discount — the
  whole 7-board human-vs-ours battery costs on the order of **$1**.

Model choice is a calibration output, not a vibe: run §3 with an Opus-tier
judge as reference; if a Sonnet-tier judge matches its accuracy on the labeled
controls within noise, the cheap judge is the production judge and Opus stays
as the drift-check reference.

### Ties and disagreement are diagnostics, not errors

- **Tie**: reported as `looks: "tie"` in the candidate list; router order
  stands.
- **Router-vs-judge disagreement**: when the judge prefers the metrically
  *worse* candidate with `margin: "clear"` under both orderings, the result is
  still fused per §4, but diagnostics gain a `judge_disagreement` entry:
  `"judge prefers candidate 3 over 1: via placement reads as scatter near U3
  although via count is lower"`. Disagreement is signal — it usually names a
  quality the metric can't see (via *placement* vs via *count*, congestion
  *location* vs total wirelength). Persistent disagreement patterns across
  boards are the backlog for new computable metrics.

---

## 2. The rendering pipeline for judging

### Rasterizer: `rsvg-convert` (librsvg). Decided, verified.

Measured on this machine (macOS, Apple Silicon), rendering `out/voxy.svg`
(300×280 mm board):

| tool | verdict | facts |
|---|---|---|
| **`rsvg-convert` 2.60** (`brew install librsvg` — already installed) | **use this** | byte-identical PNGs across runs (sha-verified); `--width/--zoom` control; 1536 px render in 0.2 s |
| `qlmanage -t -s N` (built-in) | no | deterministic in test, but it's a QuickLook thumbnailer: max-dimension sizing only, fixed output naming, no contract that thumbnails stay stable across macOS versions |
| `sips` (built-in) | no | converts SVG via CoreImage but locks to the SVG's nominal size (rendered 868 px — too coarse for dense boards); no resolution control for SVG input |

No new dependencies: librsvg is a single brew formula, and `render.py` stays
stdlib-pure — rasterization is a subprocess step in the judge harness only.
Pin the librsvg version in the judge log (`rsvg-convert --version` output) so a
rasterizer upgrade is a visible calibration event, not a silent drift source.

### Judge-mode render variant — spec (do not build yet)

`render_svg(..., mode="judge")` in `render.py`, same deterministic hand-written
SVG discipline. Differences from the current debug render, each earned by
looking at an actual render at judge resolution:

1. **No stats block, no legend.** The current render prints `nets: 342/375,
   length: 29772 mm, vias: 2741, failed: 39` *inside the image*. A judge that
   reads text will anchor on the router's own scorecard and the "visual" judge
   degenerates into re-reading metrics — the exact contamination §1's protocol
   exists to avoid. Judge images carry **zero text** except the A/B label.
2. **No failed-net markers.** Dashed red failure circles (a) are the same hue
   family as F.Cu tracks and read as clutter, and (b) leak legality
   information — legality is the router's jurisdiction (§4), and candidates
   reaching the judge are legality-tied anyway. If a failed-net pair must ever
   be judged (calibration controls), failures render as ordinary absent copper.
3. **Colors tuned for discrimination, not debugging.** Keep the two-layer
   convention (warm F.Cu / cool B.Cu — red/blue is fine and idiomatic), pads
   in low-contrast gray so copper dominates, vias as filled dots in a third
   hue with a thin white halo ring so via *clusters* read as texture at a
   glance — via scatter vs purposeful stitching is rubric quality #5 and must
   be visible at judge resolution. Slightly thicker strokes than the debug
   render (0.5 mm-equivalent) so traces survive downscaling.
4. **Region crop with context ring.** For region-level judging the viewBox is
   the fence plus a 25% margin. Inside-fence content renders full-strength;
   the context ring renders at ~35% opacity — the judge sees how the region's
   copper meets its surroundings (boundary nets, terminal propagation) without
   judging copper the candidate didn't touch. The fence itself is a thin
   dashed neutral-gray rectangle.
5. **Side-by-side composite for pairwise.** One SVG containing both renders,
   separated by a neutral gutter, labeled "A" and "B" in plain text at
   top-left of each half — composed in `render.py` (string concatenation of
   two `<g>` translations), not ImageMagick, so the composite is as
   deterministic as its halves. One composite image per judge call also
   halves the image-token bill vs two attachments and removes any ambiguity
   about which attachment is "A".
6. **Resolution policy.** Whole-board: 1536 px long edge (under every current
   vision model's high-res cap, and ~2 px per 0.5 mm lattice pitch on a
   300 mm board — structure visible, single traces marginal). Region crops:
   1024–1536 px long edge, which at typical fence sizes (20–60 mm) gives
   ≥ 10 px per pitch — individual escapes and via placement clearly legible.
   Boards/fences where 1536 px can't deliver ≥ 2 px/pitch are flagged
   `resolution_limited` in diagnostics and judged by crops only (§6).

### Determinism and reproducibility of judge inputs

Already free: `render.py` output is byte-deterministic for a given (board,
lattice, result), and `rsvg-convert` is byte-deterministic for a given SVG.
The judge harness records, per call: sha256 of each input PNG, the SVG paths,
rasterizer version, judge model ID, prompt version hash, and the raw verdict
JSON — appended to `out/<run>/judge_log.jsonl`. Identical candidates therefore
produce identical judge *inputs* forever; judge *outputs* can still vary
(sampling), which is exactly what the repeat-consistency metric in §3 measures.

---

## 3. Calibration protocol — the judge is validated before it ranks anything real

A judge nobody has tested is a random-number generator with good prose. Three
batteries, all cheap, all mechanical to assemble.

### (a) Human-vs-ours on the bench fleet

For each of the 7 bench boards: human copper vs our routed copy of the *same
placement* (both already exist — `run_bench.py --mode route` artifacts).
Render both in judge mode, run the pairwise protocol. **Today, a trustworthy
judge prefers the human board on most of the fleet** — our routes are legal
but via-heavy and metrically behind on most boards (README scorecard). The
interesting cell is the SparkFun board, where we beat the human on both
wirelength *and* vias: a good judge should rate that one close, and *which way
it falls is information about what the judge actually sees*.

Battery: 7 boards × 2 orderings × 3 repeats = 42 calls ≈ $1 via Batch API.

Report format (`bench/judge_calibration.json`, timestamp-free, git-tracked —
same convention as `results.json`):

```json
{
  "judge": {"model": "...", "prompt_sha": "...", "rasterizer": "rsvg 2.60"},
  "human_vs_ours": {
    "per_board": {"icebreaker-v1.0e": {"verdicts": ["human","human","tie", ...],
                   "position_consistent": 5, "of": 6}},
    "accuracy": 0.86, "position_consistency": 0.90, "repeat_consistency": 0.88,
    "tie_rate": 0.12
  },
  "controls": {"refine_vs_norefine": {...}, "via_model_old_vs_new": {...},
                "scramble": {...}},
  "gate": {"passed": true, "thresholds": "see VISUAL_JUDGE.md §3"}
}
```

Four numbers matter: **accuracy** (agreement with the known-better side),
**position consistency** (same winner when A/B swapped), **repeat consistency**
(same winner when re-asked), **tie rate** (a judge that ties everything is
useless; a judge that never ties is overconfident).

### (b) Known-bad controls — free labeled pairs from our own pipeline

The pipeline mechanically generates ordered-quality pairs; no human labeling
needed:

| pair | how | what it tests |
|---|---|---|
| refine vs `--no-refine` | one flag, same seed | sensitivity to slack: refined routes are strictly shorter/straighter |
| current layer model vs old via-heavy model | git history has both (voxy at 2741 vias is the archived exhibit) | via-scatter perception — the single biggest visible defect class |
| human placement vs scrambled placement | shuffle footprint positions, re-route | gross flow/alignment perception; the "not a face" control — must be ~100% |
| fine pitch vs too-coarse pitch route | `--pitch` flag | congestion/detour perception |
| full route vs partial (subset of nets dropped) | truncate `net_paths` before render | sparse-copper sanity: less copper must not read as "cleaner" — this is the anti-sycophancy control (§6) |

Difficulty is graded: scramble is trivial, refine-vs-norefine is subtle. The
gradient tells us where the judge's perceptual floor is.

**Gate before the judge touches production ranking:** ≥ 95% on gross controls
(scramble, partial-route), ≥ 80% on subtle controls (refine), ≥ 6/7 on
human-vs-ours with position consistency ≥ 80% and repeat consistency ≥ 80%.
Below gate: fix the prompt/renders, or don't ship the judge. The gate is
re-evaluated from `judge_calibration.json`, never hand-waved.

### (c) Drift checks

The calibration set is frozen (SVG shas recorded). Rerun batteries (a)+(b)
when *anything* in the judge stack changes: model ID, prompt text, render
mode, rasterizer version. New numbers append to `judge_calibration.json` keyed
by the stack fingerprint; a model upgrade that drops accuracy is a regression
even if the new model is "better" — the judge is a measurement instrument, and
instruments get recalibrated, not vibes-upgraded.

---

## 4. Where it slots

### Ranking fusion: strict lexicographic, looks as the tiebreak band

```
failures  ≫  constraint_violations  ≫  metrics  ≫  looks
```

Argued, not assumed: a layout that doesn't route is not a worse-looking
layout, it's not a layout — same for constraint violations, which encode
circuit intent (HV clearance, grid/plate separation) that no amount of visual
charm offsets. Letting looks override legality is exactly the
tidy-but-electrically-wrong failure mode (§6). Metrics vs looks is the only
genuinely debatable boundary, and the band construction resolves it: within
candidates tied on failures and violations, those whose combined metric score
(`wirelength + via_weight·vias`) is within **5%** of the band leader are
*metrically tied* — inside that band the metric difference is below the
router's own noise floor (seed-to-seed variance), so the judge's ordering is
strictly better information than a 2% wirelength delta. Outside the band,
metrics stand and the judge is only an annotator.

### When the judge runs

- **Default: final-candidates-only.** After `optimize_region` produces its
  ranked K, the judge ladders the metric-tied band at the top (typically 2–3
  candidates, 4–8 calls). Not every SA survivor — the router already routes
  and scores K·3 survivors cheaply; burning ~$0.03 and ~10 s per pair on 15
  survivors is cost without benefit when metrics can separate most of them.
- **Opt-out / opt-in:** `--judge=off` (metrics only, today's behavior),
  `--judge=band` (default), `--judge=full` (round-robin all K — calibration
  and experiments only).
- **On-demand:** a standalone `judge.py A.svg B.svg` CLI so Andrew (or an
  agent session) can ask "which of these two reads better and why" outside any
  optimize_region call. This is also the manual-labeling surface for §5.

### Diagnostics contribution

Each candidate gains a `looks` field; the diagnostics block gains judge
entries. The text is written for the calling agent, same register as the
existing diagnostics:

```
candidates[1].looks = {
  "rank_in_band": 1,
  "verdicts": {"vs_cand2": "win/clear", "vs_cand3": "win/slight"},
  "summary": "reads cleanest: signal flow left-to-right, escapes combed,
              vias clustered at the two layer-change points"
}
diagnostics.judge_disagreement = [
  "judge prefers candidate 2 over metric-leader 1 (clear, both orderings):
   candidate 1's lower via count is spent as a staircase chain along the
   fence's east edge; candidate 2 clusters vias but flows cleaner"
]
```

An agent told *why* a candidate reads better can fold that into constraints on
the next call ("add adjacency to pull the layer-change cluster near U3") —
which is the whole ARCHITECTURE.md thesis: diagnostics make the loop converge.

---

## 5. The longer arc — from VLM judge to trained ranker (labeled speculation)

The original instinct was a dedicated ranking network trained on millions of
boards. The VLM judge is the today-step, and it is also the *data engine* for
that network. What the pipeline accrues, at zero marginal effort:

- every `optimize_region` call: K candidate SVGs + full metric vectors,
- every judge ladder: pairwise verdicts with per-quality breakdowns,
- every `apply_candidate`: a human-in-the-loop label — the candidate Andrew
  actually committed, which is the highest-value signal in the system,
- unlimited mechanical pairs from §3(b)-style degradations.

**Speculative assessment of the trained-ranker step.** A small pairwise ranker
(Siamese CNN/ViT over judge-mode renders, hinge loss on verdicts) is the
classic distillation shape. Where it wins over calling the VLM: **latency and
placement in the loop** — milliseconds per pair on the same GPU MLX already
owns, cheap enough to rank all K·3 SA survivors, or eventually to sit *inside*
the annealer as a perceptual energy term, which the VLM can never do at 10 s
a call. Where it likely never wins: judgment quality on novel board styles,
and it can't write the diagnostics prose — so the VLM stays as the explainer
and the drift reference even if a distilled ranker takes over bulk ranking.

Scale, honestly: LLM-judge distillation for a narrow visual domain plausibly
wants 10⁴–10⁵ labeled pairs. Organic accrual at ~6 verdicts per
optimize_region call means ~2–15k calls — months-to-a-year of real use, not
weeks. Mechanical degradation pairs close the volume gap for free but teach
"detect damage", not "prefer better-among-plausible"; the VLM verdicts on real
candidate sets are the scarce, valuable labels, and Andrew's applied-candidate
picks are worth more still. "Millions of boards" of *human* ground truth does
not exist in public (open human-routed KiCad boards number in the thousands);
the pipeline's own generative capacity is the realistic corpus. Decision
point, not commitment: revisit when `judge_log.jsonl` crosses ~10k verdicts —
train the small ranker, evaluate it against the frozen §3 batteries like any
other judge, and let the calibration numbers decide whether it's promoted.

---

## 6. Failure modes and mitigations

| failure mode | what it looks like | mitigation |
|---|---|---|
| **Sycophancy toward tidy-but-wrong** | a sparse, half-routed or constraint-violating layout reads "cleaner" than a correct dense one; judge rewards absence of copper | structural: judge only ever orders candidates already tied on failures/violations (§4) — it *cannot* promote wrong past right. Verified: partial-route control in §3(b) must score ~100% |
| **Metric leakage** | judge reads the stats text burned into today's renders and parrots the router's ranking | judge-mode render strips all text and failure markers (§2, item 1–2); calibration would catch a judge that agrees with metrics *too* perfectly on pairs constructed to dissociate them |
| **Style bias** | judge trained on the internet's SMD boards penalizes a turret-board/through-hole idiom (the Voxy aesthetic) for not looking like a phone motherboard | idiom line in the prompt from board metadata (§1); calibration fleet spans 1972-idiom (pic-programmer) to 0.2 mm QFN (RP2350); report accuracy *per board class* and gate on the worst class, not the average |
| **Position bias** | verdict flips with image order | every comparison run both orders; splits are ties (§1). Position consistency is a first-class calibration metric with a gate |
| **Verbosity/informativeness bias** | the render with more visual "stuff" wins | identical render style both sides, single composite image, verdict-before-rationale output, rationale capped at two sentences |
| **Resolution limits on dense boards** | at 1536 px a 300 mm board's traces are ~1 px; judge sees texture, not routing | region crops are the primary judging unit (§2 item 4/6); whole-board views judge only layout-scale qualities (flow, density balance); boards under 2 px/pitch flagged `resolution_limited` and never silently judged at full extent |
| **Cost blowup** | judge calls scale with agent enthusiasm | hard cap 8 calls/invocation; band-only default; ties break early; Batch API for calibration; distilled ranker (§5) is the structural fix |
| **Judge drift after model updates** | silent behavior change re-orders candidates across sessions | frozen calibration set + stack fingerprint + mandatory §3(c) rerun; judge model pinned by exact ID in the harness, never "latest" |
| **Anchoring between calls** | judge sees candidate names/paths hinting at rank | images labeled only "A"/"B"; assignment randomized per call (the swap protocol randomizes it anyway); file paths never enter the prompt |

---

## Intersections with sibling designs

Written against the sibling briefs (ANALOG_SCORING.md, TEMPLATE_PLACEMENT.md,
FOURLAYER_MODEL.md — not yet in the tree at time of writing; the synthesis
pass should check these seams):

- **Analog scoring × visual judge: complementary rankers, same slot.**
  Computable analog metrics (loop area, coupling distance, star-ground
  topology) are *legality-adjacent* — they belong in the metrics tier of §4's
  lexicographic order, upstream of looks. The judge then handles exactly the
  residue those metrics can't compute: whether the layout *reads* right. The
  disagreement diagnostic (§1) is the pipeline between them — a persistent
  judge preference the analog metrics don't explain is a candidate for a new
  computable metric, migrating gestalt into structure over time.
- **Template placement:** templates are priors over *placement*; the judge
  evaluates *outcomes*. Two seams: judge verdicts on candidates seeded from
  different templates are a free template-quality signal, and symmetry echo
  (rubric #9) should be told, via the idiom line, when a template intends
  symmetry so the judge checks intent rather than guessing it.
- **Four-layer model:** judge-mode rendering must decide what to show for
  inner layers (planes as dim fills? signal-only view per layer pair?) before
  4-layer candidates are judged; rubric #8 (layer idiom) generalizes but the
  render spec in §2 is explicitly 2-layer today. Flagging so the 4-layer
  design reserves a `mode="judge"` answer rather than inheriting the debug
  render.
