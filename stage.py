"""L2.5: stage.py — a human-in-the-loop STAGING pass before per-area placement.

Between "the design thread partitioned the 487-part pile by circuit function"
and "region.py packs each area", a human wants to SEE the design as groups, not
as a pile, and to pin the few parts whose position is not the tool's to choose
(a panel-mount jack, the input valve, a heatsinked device). This module is that
pass. It obeys the same canon as the rest of the placer:

  GROUPS ARE INPUT. stage.py never infers a grouping and never reads the
  netlist to guess what is "position-specific". It RENDERS the partition it is
  given and HARVESTS the human's edits back. Two verbs, no cleverness.

Flow:

  1. python stage.py BOARD partition.json --out staged/
        Writes staged/<board>.kicad_pcb: every group's OFF-BOARD-PILE parts are
        loosely packed into a labelled box in the margin below the board, so
        KiCad opens showing 57 tidy groups instead of one 487-part heap. Parts
        already ON the board, and any LOCKED part, are left exactly where they
        are. The input board is never written (writeback._refuse_source_dir).

  2. (human) open staged/ in KiCad; drag a group's box onto the area it belongs
     to; drag any position-specific part to its true spot and LOCK it; save.

  3. python stage.py --harvest staged/ --out enriched.json
        Reads the edits back into the SAME partition, enriched:
          - a group whose parts now sit inside area N  -> area = N
          - an untouched group (still in its margin box) -> keeps its proposed area
          - a LOCKED part inside an area -> a fixed anchor {ref,x,y,rot}
        and runs a DENSITY PREFLIGHT: per area, courtyard area vs fence area,
        with a loud warning past ~60% ("this fence will likely be infeasible as
        one region — sub-fence it into bands"). That warning is the whole point:
        area 1 of Voxy failed the search at ~75% courtyard density, which is a
        one-line arithmetic diagnostic here instead of a burned minute.

Non-goals (unchanged canon): no auto-grouping, no netlist heuristic for
"position-specific". Those stay the design thread's decisions. stage.py renders
and harvests.
"""
import argparse
import json
import math
import os
import shutil

from board import load_board
from lattice import board_outline_regions
from place import part_courtyard, parts_from_board
from region import _rects_overlap, _scatter_pile
from writeback import write_moved_copy

# The margin boxes and their labels go on a NON-copper user layer so they never
# touch DRC, the netlist, or (critically) Edge.Cuts — a group box on Edge.Cuts
# would read back as a phantom board region. Cmts.User is in KiCad's default
# stackup; if a board lacks it, generate() injects the declaration.
LABEL_LAYER = "Cmts.User"
LABEL_LAYER_ORDINAL = 40                 # KiCad's canonical id for Cmts.User

MARGIN_GAP_MM = 10.0                     # clear air between board and the boxes
BOX_GAP_MM = 6.0                         # clear air between adjacent group boxes
BOX_PAD_MM = 3.0                         # padding inside a box around its parts
GRID_MM = 1.0                            # scatter grid step inside a box
PACK_EFFICIENCY = 0.55                   # courtyard-area / box-area target

# Density preflight thresholds (courtyard area / fence area). Past WARN a single
# region is LIKELY infeasible once the placement grid, courtyard margins and
# adjacencies eat into the raw area; past HARD it is provably impossible (the
# courtyards alone exceed the fence). preflight() in region.py already emits the
# HARD case at search time; this surfaces the softer WARN before the run.
DENSITY_WARN = 0.60
DENSITY_HARD = 1.00


def _courtyard_area(part):
    x0, y0, x1, y1 = part_courtyard(part)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _courtyard_dims(part):
    x0, y0, x1, y1 = part_courtyard(part)
    return (x1 - x0, y1 - y0)


def _board_bbox(parts, regions):
    """(x0, y0, x1, y1) enclosing every board region and every part — the space
    the staged boxes must sit clear of."""
    xs, ys, xe, ye = [], [], [], []
    for r in regions:
        rx0, ry0, rx1, ry1 = r.bounds
        xs.append(rx0); ys.append(ry0); xe.append(rx1); ye.append(ry1)
    for p in parts:
        cx0, cy0, cx1, cy1 = part_courtyard(p)
        xs.append(cx0); ys.append(cy0); xe.append(cx1); ye.append(cy1)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xe), max(ye))


def _area_index_of(part, regions, tol=0.0):
    """Index of the board region whose bbox contains the part's center, or None
    (still off-board / in the margin). Center, not courtyard, so a part hanging
    a hair over an area edge still counts as inside the area it was dragged to."""
    px, py = part.x_mm, part.y_mm
    for i, r in enumerate(regions):
        x0, y0, x1, y1 = r.bounds
        if x0 - tol <= px <= x1 + tol and y0 - tol <= py <= y1 + tol:
            return i
    return None


def _load_partition(path):
    """Parse and validate a partition file. Shape:

        {"groups": [{"name": str, "refs": [str, ...], "area": int|null}, ...]}

    'area' is the proposed board-region index (or null = unassigned). Refs are
    validated against the board by the caller. Raises ValueError on a malformed
    file — a partition is the design thread's decision and a typo in it should
    stop the run, not be silently reshaped."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"{path}: 'groups' must be a non-empty list")
    seen = set()
    for g in groups:
        if not isinstance(g, dict) or "name" not in g or "refs" not in g:
            raise ValueError(f"{path}: each group needs 'name' and 'refs'")
        if not isinstance(g["refs"], list) or not g["refs"]:
            raise ValueError(f"{path}: group {g['name']!r} has no refs")
        dup = seen & set(g["refs"])
        if dup:
            raise ValueError(
                f"{path}: ref(s) {sorted(dup)} appear in more than one group — "
                f"a part belongs to exactly one group")
        seen.update(g["refs"])
        if g.get("area") is not None and not isinstance(g["area"], int):
            raise ValueError(f"{path}: group {g['name']!r} area must be int or null")
    return data


def _box_for(candidates, courts):
    """The (width, height) of a box that loosely holds `candidates`, sized so
    _scatter_pile has enough non-overlapping grid slots for all of them."""
    if not candidates:
        return (0.0, 0.0)
    cw = max(courts[r][2] - courts[r][0] for r in candidates)
    ch = max(courts[r][3] - courts[r][1] for r in candidates)
    step_x = math.ceil((cw + GRID_MM) / GRID_MM) * GRID_MM
    step_y = math.ceil((ch + GRID_MM) / GRID_MM) * GRID_MM
    n = len(candidates)
    cols = max(1, math.ceil(math.sqrt(n * step_y / step_x)))
    rows = math.ceil(n / cols)
    return (cols * step_x + 2 * BOX_PAD_MM, rows * step_y + 2 * BOX_PAD_MM)


def _tile_boxes(sizes, origin_x, origin_y, strip_width):
    """Left-to-right, top-to-bottom row packing of boxes of the given (w, h)
    into a strip `strip_width` wide starting at (origin_x, origin_y). Returns
    a rect (x0, y0, x1, y1) per box, none overlapping. Row height is the tallest
    box in the row."""
    rects = []
    x, y, row_h = origin_x, origin_y, 0.0
    for w, h in sizes:
        if x > origin_x and x + w > origin_x + strip_width:
            x, y, row_h = origin_x, y + row_h + BOX_GAP_MM, 0.0
        rects.append((x, y, x + w, y + h))
        x += w + BOX_GAP_MM
        row_h = max(row_h, h)
    return rects


def _graphics_block(labelled_boxes):
    """gr_rect box outlines + gr_text group labels on LABEL_LAYER, as KiCad
    s-expr text ready to splice before a board file's closing paren."""
    out = []
    for name, (x0, y0, x1, y1) in labelled_boxes:
        out.append(
            f'\t(gr_rect (start {x0:.3f} {y0:.3f}) (end {x1:.3f} {y1:.3f}) '
            f'(stroke (width 0.15) (type solid)) (fill none) '
            f'(layer "{LABEL_LAYER}"))')
        safe = str(name).replace('"', "'")
        out.append(
            f'\t(gr_text "{safe}" (at {x0 + 1.0:.3f} {y0 - 1.5:.3f}) '
            f'(layer "{LABEL_LAYER}") '
            f'(effects (font (size 1.5 1.5) (thickness 0.25)) '
            f'(justify left bottom)))')
    return "\n".join(out)


def _inject_graphics(path, graphics_text):
    """Splice a graphics block in before the final top-level ')' of a KiCad
    board file, first ensuring LABEL_LAYER is declared in the (layers ...) block
    (KiCad rejects an object on an undeclared layer)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if f'"{LABEL_LAYER}"' not in text:
        # Declare the user layer just before the layer list's closing paren.
        marker = "(layers"
        i = text.find(marker)
        if i != -1:
            depth, j = 0, i + len(marker)
            while j < len(text):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    if depth == 0:
                        break
                    depth -= 1
                j += 1
            decl = f'\n\t\t({LABEL_LAYER_ORDINAL} "{LABEL_LAYER}" user)'
            text = text[:j] + decl + "\n\t" + text[j:]
    end = text.rstrip()
    if not end.endswith(")"):
        raise ValueError(f"{path}: not a well-formed board file (no closing paren)")
    cut = end.rfind(")")
    text = end[:cut] + graphics_text + "\n" + end[cut:] + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def generate(board_path, partition_path, out_dir):
    """Write a staged copy of the board with each group's off-board-pile parts
    packed into a labelled margin box. Returns the staged board path."""
    data = _load_partition(partition_path)
    parts = list(parts_from_board(board_path).values())
    by_ref = {p.ref: p for p in parts}
    regions = board_outline_regions(load_board(board_path))

    # Validate every ref exists before touching the filesystem.
    named = {r for g in data["groups"] for r in g["refs"]}
    missing = sorted(named - set(by_ref))
    if missing:
        raise ValueError(
            f"{partition_path}: {len(missing)} ref(s) are not footprints on "
            f"{os.path.basename(board_path)}: {', '.join(missing[:20])}"
            + (" ..." if len(missing) > 20 else ""))

    courts = {p.ref: part_courtyard(p) for p in parts}

    # A group's SCATTER candidates are its parts that carry no real position:
    # off every board region AND not locked. On-board or locked parts are left
    # untouched (their position is information the human/tool already chose).
    def is_pile(ref):
        p = by_ref[ref]
        return not p.locked and _area_index_of(p, regions) is None

    group_candidates = []
    for g in data["groups"]:
        cands = [r for r in g["refs"] if is_pile(r)]
        group_candidates.append((g["name"], cands))

    # Lay the boxes in a strip below the board, as wide as the board.
    bx0, by0, bx1, by1 = _board_bbox(parts, regions)
    strip_w = max(bx1 - bx0, 50.0)
    sizes = [_box_for(cands, courts) if cands else (0.0, 0.0)
             for _name, cands in group_candidates]
    rects = _tile_boxes([s for s in sizes], bx0, by1 + MARGIN_GAP_MM, strip_w)

    placements, labelled = {}, []
    for (name, cands), box in zip(group_candidates, rects):
        if not cands:
            continue
        labelled.append((name, box))
        rx0, ry0, rx1, ry1 = box
        region_rect = (rx0 + BOX_PAD_MM, ry0 + BOX_PAD_MM,
                       rx1 - rx0 - 2 * BOX_PAD_MM, ry1 - ry0 - 2 * BOX_PAD_MM)
        live = {r: by_ref[r] for r in cands}
        lc = {r: courts[r] for r in cands}
        _scatter_pile(cands, set(), live, lc, region_rect, GRID_MM, obstacles=())
        for r in cands:
            placements[r] = (live[r].x_mm, live[r].y_mm, live[r].rot_deg)

    os.makedirs(out_dir, exist_ok=True)
    staged = os.path.join(out_dir, os.path.basename(board_path))
    write_moved_copy(board_path, staged, placements)     # refuses source dir
    if labelled:
        _inject_graphics(staged, _graphics_block(labelled))

    # Carry the partition and the sibling .kicad_pro alongside so `--harvest
    # out_dir` is self-contained and KiCad opens the copy as a project.
    shutil.copyfile(partition_path, os.path.join(out_dir, "partition.json"))
    pro = os.path.splitext(board_path)[0] + ".kicad_pro"
    if os.path.isfile(pro):
        shutil.copyfile(pro, os.path.splitext(staged)[0] + ".kicad_pro")

    unstaged = sum(1 for _n, c in group_candidates if not c)
    print(f"staged      : {staged}")
    print(f"groups      : {len(data['groups'])} "
          f"({len(labelled)} with pile parts boxed, "
          f"{unstaged} fully on-board/locked)")
    print(f"parts moved : {len(placements)} pile part(s) into margin boxes")
    print(f"next        : open {out_dir} in KiCad, drag boxes onto their area, "
          f"lock position-specific parts, then `stage.py --harvest {out_dir}`")
    return staged


def _density_preflight(parts_by_area, regions):
    """Per area: courtyard area vs fence area, and the warnings. Returns
    (rows, warnings) — rows for the report, warnings the loud lines."""
    rows, warnings = [], []
    for i, r in enumerate(regions):
        x0, y0, x1, y1 = r.bounds
        fence = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        court = sum(_courtyard_area(p) for p in parts_by_area.get(i, ()))
        ratio = court / fence if fence > 1e-9 else math.inf
        rows.append((i, court, fence, ratio, len(parts_by_area.get(i, ()))))
        if ratio > DENSITY_HARD:
            warnings.append(
                f"area {i}: courtyards need {court:.0f} mm2 but the fence is "
                f"only {fence:.0f} mm2 ({ratio:.0%}) — IMPOSSIBLE as one region; "
                f"move parts out or sub-fence it into bands")
        elif ratio > DENSITY_WARN:
            warnings.append(
                f"area {i}: courtyard density {ratio:.0%} ({court:.0f} of "
                f"{fence:.0f} mm2) — this fence will LIKELY be infeasible as one "
                f"region once the placement grid and courtyard margins are "
                f"counted; sub-fence it into bands (per-band region.py runs)")
    return rows, warnings


def harvest(staged_dir_or_board, partition_path, out_path):
    """Read the human's edits on a staged board back into an enriched partition,
    and run the density preflight. Returns the enriched dict."""
    if os.path.isdir(staged_dir_or_board):
        boards = [f for f in os.listdir(staged_dir_or_board)
                  if f.endswith(".kicad_pcb")]
        if len(boards) != 1:
            raise ValueError(
                f"{staged_dir_or_board}: expected exactly one .kicad_pcb, "
                f"found {len(boards)}")
        board_path = os.path.join(staged_dir_or_board, boards[0])
        if partition_path is None:
            partition_path = os.path.join(staged_dir_or_board, "partition.json")
    else:
        board_path = staged_dir_or_board
    if partition_path is None or not os.path.isfile(partition_path):
        raise ValueError(
            "harvest needs the partition file (staged/partition.json or "
            "--partition PATH) to map refs to groups")

    data = _load_partition(partition_path)
    parts = list(parts_from_board(board_path).values())
    by_ref = {p.ref: p for p in parts}
    regions = board_outline_regions(load_board(board_path))

    parts_by_area, moved = {}, 0
    for g in data["groups"]:
        present = [by_ref[r] for r in g["refs"] if r in by_ref]
        # Where does the group sit now? A group whose CENTROID landed inside an
        # area was dragged there; one still in the margin keeps its proposal.
        placed = [p for p in present if _area_index_of(p, regions) is not None]
        harvested = g.get("area")
        if placed:
            cx = sum(p.x_mm for p in placed) / len(placed)
            cy = sum(p.y_mm for p in placed) / len(placed)
            for i, r in enumerate(regions):
                x0, y0, x1, y1 = r.bounds
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    if harvested != i:
                        moved += 1
                    harvested = i
                    break
        g["area"] = harvested

        # LOCKED parts inside an area are fixed anchors — an exact position the
        # per-area region.py run must honour, not re-place.
        anchors = []
        for p in present:
            ai = _area_index_of(p, regions)
            if p.locked and ai is not None:
                anchors.append({"ref": p.ref, "x": round(p.x_mm, 4),
                                "y": round(p.y_mm, 4), "rot": round(p.rot_deg, 4)})
        if anchors:
            g["anchors"] = anchors
        elif "anchors" in g:
            del g["anchors"]

        # Accumulate this group's parts against its (harvested) area for density.
        if harvested is not None and 0 <= harvested < len(regions):
            parts_by_area.setdefault(harvested, []).extend(present)

    # Parts placed inside an area independently of any group assignment still
    # occupy it — count them too, without double-counting group members.
    counted = {p.ref for ps in parts_by_area.values() for p in ps}
    for p in parts:
        ai = _area_index_of(p, regions)
        if ai is not None and p.ref not in counted:
            parts_by_area.setdefault(ai, []).append(p)
            counted.add(p.ref)

    rows, warnings = _density_preflight(parts_by_area, regions)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"enriched    : {out_path}")
    print(f"groups      : {len(data['groups'])} "
          f"({moved} reassigned to an area by drag, "
          f"{sum(len(g.get('anchors', [])) for g in data['groups'])} locked "
          f"anchor(s))")
    print("density     : " + " | ".join(
        f"area {i} {ratio:.0%} ({n}p)" for i, _c, _f, ratio, n in rows))
    for w in warnings:
        print(f"WARNING     : {w}")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Stage a partitioned board for human review, then harvest "
                    "the edits back into an enriched partition.")
    ap.add_argument("board", help="board .kicad_pcb (generate), or the staged "
                                  "board / dir (with --harvest)")
    ap.add_argument("partition", nargs="?", default=None,
                    help="partition.json (required for generate; for harvest "
                         "defaults to <staged-dir>/partition.json)")
    ap.add_argument("--harvest", action="store_true",
                    help="read a staged board's edits back into an enriched "
                         "partition + run the density preflight")
    ap.add_argument("--out", default=None,
                    help="generate: output dir (default out/staged); "
                         "harvest: enriched json path (default out/partition-enriched.json)")
    ap.add_argument("--partition", dest="partition_opt", default=None,
                    help="explicit partition path (harvest, if not in the dir)")
    args = ap.parse_args(argv)

    try:
        if args.harvest:
            out = args.out or os.path.join("out", "partition-enriched.json")
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            harvest(args.board, args.partition or args.partition_opt, out)
        else:
            if not args.partition:
                ap.error("generate needs a partition.json: "
                         "stage.py BOARD partition.json --out staged/")
            out = args.out or os.path.join("out", "staged")
            generate(args.board, args.partition, out)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        ap.error(str(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
