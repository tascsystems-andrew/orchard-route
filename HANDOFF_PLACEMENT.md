# Prompt for the placement design thread (3-area board, some parts locked)

Copy everything below the line into that session. Set `BOARD` to the real board
first.

---

I want to place and route a board with Orchard Route, a GPU place-and-route tool
at `~/Code/mlx-router` (public: github.com/tascsystems-andrew/orchard-route). It
runs from the CLI — use Bash, there's no MCP tool for it yet.

```sh
cd ~/Code/mlx-router
BOARD="<ABSOLUTE PATH TO MY .kicad_pcb>"     # set this first
```

The board has **THREE board-outline areas** in one file. **Some components are
already placed and LOCKED in KiCad; the rest are free and sit in an off-board
pile at the origin** (default placement — they have no meaningful position yet).
Your job is to decide, from the SCHEMATIC, which free parts go in which area, and
let the tool place-and-route each group with the locked parts respected.

**Read `~/Code/mlx-router/AGENTS.md` first — Phase 0 and Phase 0.5 especially.**
It is written for you: the constraint vocabulary, how to read the diagnostics,
and the tool's honest limitations. Follow it.

## The division of labour — do not skip Step 1

- **Step 1 is yours (the schematic, not the layout).** The free parts are in a
  pile, so there is nothing to read a grouping off of — you must group from the
  **schematic**. Read it with Konnect, partition the parts by **circuit
  function** (a gain stage, the switching section, a supply leg, the panel I/O),
  and assign each group to ONE of the three areas. Name the refs for each group.
  `region.py "$BOARD" --list-connections R1,R2,...` prints the net edges among a
  set of refs as advisory data if you want to sanity-check a group — but the
  partition is your schematic decision. The tool has no clusterer and will never
  group for you.
- **Step 2 is the tool's.** It places the group you name into the area you pick,
  freezes the locked parts, and routes every candidate.

## Step 1 — net classes FIRST (creepage before placement, and ask me)

This board has **HV nets** — I will name them. Set their net class **before you
place anything**, because on HV the rule is creepage, not current: the required
copper-to-copper spacing decides how far apart parts and traces must sit, and
that has to be an input to placement, not a correction after it.

1. Ask me which nets are HV and roughly what voltage.
2. Make an `HV` net class in the `.kicad_pro` (it's JSON — edit
   `net_settings.classes` and the net→class map) or via Board Setup → Net
   Classes, with a wide `clearance` sized for the voltage. Give `Power` (B+,
   heater, rails) a wide `track_width` too. Leave logic/low-level audio on
   `Default`.
3. Tell me the classes you propose and the clearances, and wait for my OK before
   placing. A board where every net is `Default` is a decision nobody made.

(If any HV net is a supply you'd rather join by a flying lead than an on-board
trace, note it — the tool can serve a net by wire terminals, but that path is
still in progress; expect to hand-route HV either way. See "limitations".)

## Step 2 — lock what must not move (in KiCad)

Anything that is mechanically fixed — panel switches, pots, jacks, the socket,
mounting-tied parts — **lock in KiCad** (select → Edit → Lock, or the padlock).
The tool reads KiCad's own `(locked yes)` flag: a locked footprint whose position
falls inside an area is **auto-frozen** at its exact coordinates as an obstacle
and a fixed anchor, WITHOUT you listing it. Confirm the lock set is what you
expect with:

```sh
.venv/bin/python region.py "$BOARD" --list-regions      # the 3 areas + spanning nets
```

## Step 3 — place-and-route each area's group

For each of the three areas, run the group you assigned in Step 1:

```sh
.venv/bin/python region.py "$BOARD" --area 0 \
  --components R1,C1,V1,R4,C8 \
  --constraint "adjacency_max_distance(GstopT1,V1,3)" \
  --constraint "min_distance(R_plate,V1,4)" \
  --k 5 --out out/area0/
```

- `--area N` fences on board outline area N (from `--list-regions`).
- `--components` is the explicit ref list for that area's group — the primary
  input. The parts start in the off-board pile; the tool scatters them into the
  fence for you, you do NOT pre-place them.
- Constraints are the closed six from AGENTS.md: `fixed`, `adjacency_max_distance`,
  `min_distance`, `keepout`, `orientation_set`, `edge`. Write them the way you'd
  describe the stage out loud (grid stopper at its pin, plate R off the grid
  input, a pot on the edge). Anything else errors and names the valid set —
  don't invent forms. `fixed()` is added for you on locked parts.
- Read the run's `auto-fixed` line — it should list this area's locked parts.
  Read `diagnostics`: `binding_constraint` (what's tight), `boundary_nets` (nets
  leaving the area), `unrouted`/`infeasible_reason`, `suggested_expansion`. If it
  comes back with zero candidates, that is not an error — read the reason and
  change the group, the area, or a constraint. Don't re-run the identical call.

Repeat for `--area 1` and `--area 2` with their groups.

## Step 4 — inspect every candidate (they are PROPOSALS)

Each candidate is a routed **copy** plus a picture — nothing touches the source
board (it's read-only; the writer refuses its directory). For each area:

```sh
open out/area0/cand-1.svg          # look at it
# open out/area0/cand-1.kicad_pcb in KiCad to inspect the real copper
```

Look at the SVG of each `--k` candidate. Ranking is strict (failures ≫
constraint violations ≫ wirelength + vias), so cand-1 is the tool's best, but
YOU decide whether it's any good. Report back which candidate per area looks
like something a person would have drawn, and why. **Do not apply anything** —
we decide together before any footprint moves. Applying = opening the chosen
board copy, or moving footprints via Konnect from the candidate's `placements`.

## Step 5 — route (whole board, once placement is settled)

When the three areas are placed the way we like, route the whole board and write
a routed copy (Phase 1 in AGENTS.md), then verify honestly (Phase 3 — the
uncapped `scripts/copper_audit.py`, not raw kicad-cli DRC totals).

## Known limitations — disclose these when you report

- **HV nets will likely be HAND-ROUTED.** The tool spaces copper by net-class
  clearance, but wire-terminal serving of HV supplies, and inter-area
  (cross-boundary) connections, are still in progress. Treat HV routing as
  something you finish by hand, and keep the class clearances wide so the
  auto-routed low-voltage copper stays clear of the HV parts the placement froze.
- **Custom pad shapes are unseen.** The parser reads a custom pad as its anchor
  rectangle only, so a SOT-89/SOT-223 heat tab or a thermal paddle owns no
  copper the router sees — it will route across it and DRC will call a short. The
  run flags the affected parts in `diagnostics.geometry_warnings`; check those by
  eye in the candidate before trusting their copper.
- **Two copper layers only.** Inner layers aren't modelled.
- **Off-board parts you don't name are dropped from a group, not silently
  swept in.** A free part sitting off every area shows up in
  `diagnostics.unplaced_free_parts` — assign it to a group or it won't be placed.
- **Terminal propagation across area boundaries is a fence-edge pseudo-pad**, not
  a real inter-area route. A net that leaves an area routes *to the edge it
  leaves by*; joining the two areas is still your call (a hand route, or the
  wire-terminal path once it lands).
- **Every candidate is a proposal.** Open it, look at it, and tell me whether
  it's good before anyone talks about moving footprints for real.

Report back with: the HV net-class proposal from Step 1, the `--list-regions`
map, per area the run's stats + `auto-fixed` line + your read of the best
candidate, and any `geometry_warnings` or `unplaced_free_parts` the runs raised.
