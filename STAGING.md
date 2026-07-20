# Staging pass (`stage.py`)

A human-in-the-loop step between **partitioning** (the design thread groups the
parts by circuit function, from the schematic) and **placement** (`region.py`
packs each area). It exists because on a real board — Voxy: 487 free parts, 3
areas — three things went wrong that a picture would have caught:

- the locked-anchor set was only what the human happened to remember to lock;
- the functional partition never got a human review before the run;
- area 1 failed **infeasible at ~75% courtyard density** — provable by
  arithmetic *before* the run, but nothing computed it.

`stage.py` keeps the placer's canon: **groups are INPUT.** It never infers a
grouping and never reads the netlist to guess which parts are "position
specific." It only **renders** the partition you give it and **harvests** your
edits back. Two verbs, no cleverness.

## The three-step workflow

```bash
# 1. render the partition as tidy, labelled groups in a board COPY
python stage.py BOARD.kicad_pcb partition.json --out staged/

# 2. (human, in KiCad) open staged/ — you see labelled group boxes in the
#    margin instead of a 487-part heap. Drag a group's box onto the area it
#    belongs to; drag any position-specific part to its true spot and LOCK it
#    (press L). Save.

# 3. harvest the edits into an enriched partition + run the density preflight
python stage.py --harvest staged/ --out partition-enriched.json
```

The input board is **never written** — every output goes under `--out`
(enforced by `writeback._refuse_source_dir`; a run that would touch the source
directory aborts before writing anything).

### What `generate` does

- Each group's **off-board-pile** parts (off every board region **and** not
  locked) are loosely packed into a labelled box in the margin below the board.
  The group name is written as board text on `Cmts.User`.
- Parts already **on the board**, and any **locked** part, are left exactly
  where they are — their position is information you already chose.
- The partition and the sibling `.kicad_pro` are copied into `staged/` so
  `--harvest staged/` is self-contained and KiCad opens the copy as a project.

### What `harvest` does

- A group whose parts now sit **inside area N** (you dragged its box there) →
  `area: N`. Assignment is by **majority vote** over the group's parts — the
  area holding the most of them wins (a centroid can land in a *third* area when
  a group straddles two boards, so it isn't used).
- An **untouched** group (still in its margin box) → keeps its proposed area.
- A **locked** part inside an area → a fixed **anchor** `{ref, x, y, rot}` the
  per-area `region.py` run must honour exactly.
- **Density preflight**, per area: courtyard area ÷ fence area.
  - `> 60%` → soft warning: *"LIKELY infeasible as one region — sub-fence it
    into bands"* (run `region.py` per band instead of once per area).
  - `> 100%` → hard: *"IMPOSSIBLE — move parts out or sub-fence."*
  This is the check that turns Voxy's area-1 blowup into one line, pre-run.

## `partition.json` schema

The partition is **your** decision (a schematic one), so its shape is a plain
list of groups. Minimum input:

```json
{
  "groups": [
    { "name": "input_gain_stage", "refs": ["V1", "R4", "R5", "C8"], "area": 1 },
    { "name": "ht_supply",        "refs": ["Q23", "Q24", "C75"],    "area": 0 },
    { "name": "front_panel",      "refs": ["J1", "SW2"],            "area": 2 }
  ]
}
```

- `name` — a human label; becomes the box's board text.
- `refs` — the footprint references in this group. Every ref must exist on the
  board and belong to **exactly one** group (both are validated; a typo stops
  the run rather than being silently reshaped). Use the `ref#N` form for a
  duplicated designator, matching `writeback`'s addressing.
- `area` — the **proposed** board-region index (0-based, into
  `board_outline_regions` / `region.py --list-regions`), or `null` for
  unassigned. `harvest` may change it based on where you dragged the group.

`harvest` writes the **same** JSON back, enriched: each group's `area` updated
to what you staged, plus an `anchors` array on any group with locked in-area
parts:

```json
{ "name": "front_panel", "refs": ["J1", "SW2"], "area": 2,
  "anchors": [ { "ref": "J1", "x": 128.0, "y": 12.5, "rot": 90.0 } ] }
```

## Feeding `region.py`

The enriched partition is the plan for the per-area runs. For each area N, take
the groups with `area == N`, and run `region.py` with their combined `refs` as
the component list, fencing on `--area N`; pass each anchor as a
`fixed:REF` constraint so the locked parts are honoured. If the density
preflight warned on area N, sub-fence it: split the area into bands and run one
`region.py` per band.

## Non-goals

No auto-grouping. No netlist heuristic for "position specific." Both stay the
design thread's decisions — `stage.py` renders and harvests, nothing more.
