"""L6 floorplan view: color-by-group render + cohesion score (proposal §2).

Groups are INPUT — the same partition stage.py uses ({"groups":[{"name","refs",
...}]}). This module never infers a grouping; it RENDERS a placement colored by
the human's groups and SCORES how cohesive that placement is. A good floorplan
shows solid color patches with short seams between them; "rainbow confetti" —
one group's parts scattered across the board — is the failure mode, and this
makes it both visible (the render) and measurable (the numbers).

Two numbers, per the proposal:

- per-group FILL FRACTION = Σ courtyard area / group bounding-box area. 1.0 is a
  perfectly tight block; a low value means the group's parts are spread out with
  air between them — the confetti detector. (Uses the REAL courtyards from the
  #30 model, so THT bodies count at their true size.)
- INTER-GROUP wirelength = Σ HPWL of the nets whose pads span more than one
  group. A cheap PRE-ROUTE proxy for routability: tight blocks joined by few
  short seams route cleanly; long inter-group nets are the ones that fight. The
  companion INTRA-group figure is the copper that stays inside a block.

Coloring is by FAMILY when the partition gives one (`"family": "..."` per
group) — same-family groups share a color, so a whole functional area reads as
one patch — else one color per group. Either way the human supplies the intent;
the tool only renders it.

CLI:
    python floorplan.py BOARD partition.json --out floorplan.svg [--json]
"""
import json
import os

from board import load_board
from lattice import board_outline_regions
from place import parts_from_board, part_courtyard, _overlap
from render import _esc, _f
# _load_partition owns the partition schema + its validation; re-parsing here
# with a second reader is exactly how the two drift apart.
from stage import _load_partition

# Distinguishable fills, cycled per family. Ungrouped parts get the neutral gray.
_PALETTE = ["#3a7bd5", "#3fa34d", "#e8703a", "#9b5de5", "#d65db1", "#2ec4b6",
            "#e0b83a", "#7b6cf6", "#c0392b", "#16a085", "#e67e22", "#8e44ad",
            "#2980b9", "#d81b60", "#00897b", "#5e35b1", "#f4511e", "#43a047"]
_UNGROUPED = "#9aa0a6"


def _group_map(partition):
    """(ref -> group name, [group names in file order], group name -> family).

    A ref not in any group is simply absent from ref->group (rendered neutral,
    excluded from the group metrics). Refs use the same ref#N addressing the
    partition and parts_from_board share."""
    ref2grp, order, family = {}, [], {}
    for g in partition["groups"]:
        name = g["name"]
        if name not in family:
            order.append(name)
            family[name] = g.get("family", name)
        for r in g["refs"]:
            ref2grp[r] = name
    return ref2grp, order, family


def _colors(order, family):
    """group name -> fill color. Families (or bare group names) get palette
    slots in first-appearance order, so same-family groups share a color."""
    fam_color, out = {}, {}
    for name in order:
        fam = family[name]
        if fam not in fam_color:
            fam_color[fam] = _PALETTE[len(fam_color) % len(_PALETTE)]
        out[name] = fam_color[fam]
    return out


def _area_of(part, regions):
    """Index of the board outline region whose bbox holds the part's centre, or
    None (off every board). Centre, matching stage.py's area assignment."""
    for i, r in enumerate(regions):
        x0, y0, x1, y1 = r.bounds
        if x0 <= part.x_mm <= x1 and y0 <= part.y_mm <= y1:
            return i
    return None


def _sheets_partition(parts):
    """Derive a partition from the board's schematic sheets — the grouping the
    human already authored by drawing the schematic into sheets, read straight
    from the footprints' (sheetname ...). Each distinct sheet becomes a group
    (its own color); parts with no sheet fall into no group. This is INPUT, not
    inference: it reads a human decision, it does not cluster."""
    by_sheet = {}
    for ref in sorted(parts):
        s = parts[ref].sheet
        if s:
            by_sheet.setdefault(s, []).append(ref)
    if not by_sheet:
        raise ValueError(
            "this board's footprints carry no schematic sheet (sheetname) — "
            "pass an explicit partition.json instead of --groups-from-sheets")
    return {"groups": [{"name": s, "refs": refs, "family": s}
                       for s, refs in by_sheet.items()]}


def cohesion(board_path, partition):
    """Per-group geometry + fill fraction, and the inter/intra-group wirelength
    split, as plain data (also what --json emits). Reads the board's CURRENT
    placement — point it at the placed board whose cohesion you want to judge."""
    parts = parts_from_board(board_path)
    regions = board_outline_regions(load_board(board_path))
    ref2grp, order, family = _group_map(partition)

    # A partition is a human decision; a ref that names no footprint is a typo
    # that would silently measure the wrong subset. Fail loud, like stage.py.
    missing = sorted({r for g in partition["groups"] for r in g["refs"]}
                     - set(parts))
    if missing:
        raise ValueError(
            f"partition names {len(missing)} ref(s) not on this board: "
            f"{', '.join(missing[:12])}{' …' if len(missing) > 12 else ''} — "
            f"a duplicated designator needs its ref#N form")

    groups = {}
    for name in order:
        refs = [r for r in ref2grp if ref2grp[r] == name and r in parts]
        courts = [part_courtyard(parts[r]) for r in refs]
        if not courts:
            continue
        x0 = min(c[0] for c in courts); y0 = min(c[1] for c in courts)
        x1 = max(c[2] for c in courts); y1 = max(c[3] for c in courts)
        bbox_area = max(1e-9, (x1 - x0) * (y1 - y0))
        court_area = sum(max(0.0, c[2] - c[0]) * max(0.0, c[3] - c[1])
                         for c in courts)
        cx = sum((c[0] + c[2]) / 2 for c in courts) / len(courts)
        cy = sum((c[1] + c[3]) / 2 for c in courts) / len(courts)
        # which board area(s) this group's parts land in. A group whose parts
        # span more than one area is the structural inverse of confetti — a
        # sheet split across boards (Andrew's "same sheet -> same board" rule).
        areas = sorted({a for r in refs
                        if (a := _area_of(parts[r], regions)) is not None})
        groups[name] = {
            "n_parts": len(refs),
            "courtyard_area_mm2": round(court_area, 2),
            "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            "bbox_area_mm2": round(bbox_area, 2),
            # Σ courtyard / bbox: for a well-placed block this is in (0, 1] —
            # near 1 = tight, low = scattered "confetti".
            "fill_fraction": round(court_area / bbox_area, 3),
            # Two DIFFERENT problems, kept apart:
            #  - overlap_pairs: exact count of the group's courtyard pairs that
            #    intersect (place._overlap, the placer's own primitive) — a DRC
            #    collision, flagged in the render + report but the group still
            #    has a meaningful cohesion number.
            #  - piled: Σcourtyard > bbox, i.e. the parts can't even FIT their
            #    own box — grossly stacked / not really placed (the origin pile).
            #    Only piled groups are dropped from the cohesion stats, because
            #    their fill (> 1) is meaningless; a placed-but-colliding group
            #    is kept, so a real board doesn't blank out over a few overlaps.
            "overlap_pairs": sum(1 for i in range(len(courts))
                                 for j in range(i + 1, len(courts))
                                 if _overlap(courts[i], courts[j])),
            "piled": court_area > bbox_area + 1e-6,
            "areas": areas,
            "spans_areas": len(areas) > 1,
            "centroid": [round(cx, 2), round(cy, 2)],
        }

    # net connectivity: each net's pads, tagged with their owning group (None =
    # ungrouped). A net is inter-group when its pads carry >= 2 distinct groups.
    by_net = {}
    for r, p in parts.items():
        lab = ref2grp.get(r)
        for pad in p.pads:
            if pad.net_code > 0:
                by_net.setdefault(pad.net_code, []).append(
                    (lab, pad.x_mm, pad.y_mm))

    inter_wl = intra_wl = 0.0
    n_inter = n_intra = 0
    graph = {}
    for pads in by_net.values():
        # measure over the GROUPED pads only: an ungrouped part (a decoupler or
        # connector not in the partition) must not inflate a group's tightness
        # or the floorplan score. A net with no grouped pad is neither.
        grouped = [(l, x, y) for l, x, y in pads if l is not None]
        if not grouped:
            continue
        labs = sorted({l for l, _x, _y in grouped})
        xs = [x for _l, x, _y in grouped]
        ys = [y for _l, _x, y in grouped]
        hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
        if len(labs) >= 2:
            inter_wl += hpwl
            n_inter += 1
            for i in range(len(labs)):
                for j in range(i + 1, len(labs)):
                    key = f"{labs[i]} ↔ {labs[j]}"
                    graph[key] = graph.get(key, 0) + 1
        else:
            intra_wl += hpwl
            n_intra += 1

    # cohesion stats are over the groups that at least FIT their box — a piled
    # group (Σ > bbox) has a meaningless fill and is a placement error to fix
    # first. A placed-but-colliding group is kept (its fill is still real).
    valid = {n: g for n, g in groups.items() if not g["piled"]}
    fills = [g["fill_fraction"] for g in valid.values()]
    overlapping = sorted(n for n, g in groups.items() if g["overlap_pairs"])
    piled = sorted(n for n, g in groups.items() if g["piled"])
    split = sorted((n, g["areas"]) for n, g in groups.items()
                   if g["spans_areas"])
    return {
        "groups": groups,
        "n_groups": len(groups),
        "n_overlapping_groups": len(overlapping),
        "overlapping_groups": overlapping,
        "overlap_pairs_total": sum(g["overlap_pairs"] for g in groups.values()),
        "n_piled_groups": len(piled),
        "piled_groups": piled,
        "groups_split_across_areas": split,
        "mean_fill_fraction": round(sum(fills) / len(fills), 3) if fills else 0.0,
        "loosest_groups": sorted(
            ((g["fill_fraction"], name) for name, g in valid.items()))[:5],
        "inter_group_wirelength_mm": round(inter_wl, 1),
        "intra_group_wirelength_mm": round(intra_wl, 1),
        "inter_group_nets": n_inter,
        "intra_group_nets": n_intra,
        "group_graph": dict(sorted(graph.items(), key=lambda kv: -kv[1])),
    }


def _view_bbox(regions, parts, margin=4.0):
    xs, ys, xe, ye = [], [], [], []
    for r in regions:
        b = r.bounds
        xs.append(b[0]); ys.append(b[1]); xe.append(b[2]); ye.append(b[3])
    for p in parts.values():
        c = part_courtyard(p)
        xs.append(c[0]); ys.append(c[1]); xe.append(c[2]); ye.append(c[3])
    if not xs:
        return (0.0, 0.0, 100.0, 100.0)
    return (min(xs) - margin, min(ys) - margin,
            max(xe) + margin, max(ye) + margin)


def render(board_path, partition, out_path, data=None):
    """Write the color-by-group SVG. Each part's real courtyard is filled with
    its group color; each group's bbox is outlined (its 'patch') and labelled;
    a header line carries the headline cohesion numbers."""
    brd = load_board(board_path)
    regions = board_outline_regions(brd)
    parts = parts_from_board(board_path)
    ref2grp, order, family = _group_map(partition)
    color = _colors(order, family)
    data = data or cohesion(board_path, partition)

    vx, vy, ex, ey = _view_bbox(regions, parts)
    vw, vh = ex - vx, ey - vy
    head = 7.0                                    # header strip above the board
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{_f(vx)} {_f(vy - head)} {_f(vw)} {_f(vh + head)}" '
        f'width="{_f(vw)}mm" height="{_f(vh + head)}mm" font-family="monospace">',
        f'<rect x="{_f(vx)}" y="{_f(vy - head)}" width="{_f(vw)}" '
        f'height="{_f(vh + head)}" fill="#0d0d12"/>',
    ]
    ov = (f'  |  ⚠ {data["n_overlapping_groups"]} groups OVERLAP'
          if data["n_overlapping_groups"] else "")
    out.append(
        f'<text x="{_f(vx + 1)}" y="{_f(vy - head + 4)}" fill="#e8e8ee" '
        f'font-size="3">{_esc(os.path.basename(board_path))}  |  '
        f'{data["n_groups"]} groups  |  inter-group '
        f'{data["inter_group_wirelength_mm"]:.0f} mm (the floorplan score) / '
        f'intra {data["intra_group_wirelength_mm"]:.0f} mm{ov}</text>')

    for i, r in enumerate(regions):
        b = r.bounds
        out.append(
            f'<rect x="{_f(b[0])}" y="{_f(b[1])}" width="{_f(b[2] - b[0])}" '
            f'height="{_f(b[3] - b[1])}" fill="none" stroke="#2b6" '
            f'stroke-width="0.4"/>')
        out.append(f'<text x="{_f(b[0] + 1)}" y="{_f(b[1] - 1)}" fill="#2b6" '
                   f'font-size="3">area{i}</text>')

    # every part's real courtyard, filled by group (ungrouped = neutral gray)
    out.append('<g stroke-width="0.15">')
    for r, p in sorted(parts.items()):
        c = part_courtyard(p)
        fill = color.get(ref2grp.get(r), _UNGROUPED)
        out.append(
            f'<rect x="{_f(c[0])}" y="{_f(c[1])}" width="{_f(c[2] - c[0])}" '
            f'height="{_f(c[3] - c[1])}" fill="{fill}" fill-opacity="0.62" '
            f'stroke="{fill}"/>')
    out.append('</g>')

    # each group's patch: its bbox outline + name, in the group color; a group
    # whose parts OVERLAP is outlined in warning red instead (not valid yet).
    for name, g in data["groups"].items():
        x0, y0, x1, y1 = g["bbox"]
        col = "#ff5252" if g["overlap_pairs"] else color.get(name, _UNGROUPED)
        out.append(
            f'<rect x="{_f(x0)}" y="{_f(y0)}" width="{_f(x1 - x0)}" '
            f'height="{_f(y1 - y0)}" fill="none" stroke="{col}" '
            f'stroke-width="0.3" stroke-dasharray="1.2 0.8" '
            f'stroke-opacity="0.9"/>')
        tag = ("PILED" if g["piled"] else f'{g["overlap_pairs"]} OVERLAP'
               if g["overlap_pairs"] else f'{g["fill_fraction"]:.0%}')
        out.append(
            f'<text x="{_f(x0 + 0.5)}" y="{_f(y0 + 3)}" fill="{col}" '
            f'font-size="2.4">{_esc(name[:22])} {tag}</text>')

    out.append('</svg>')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return out_path


def _print_report(data):
    print(f"groups          : {data['n_groups']}")
    print(f"floorplan score : {data['inter_group_wirelength_mm']:.0f} mm across "
          f"groups ({data['inter_group_nets']} nets) — the routability proxy, "
          f"lower is better")
    print(f"group tightness : {data['intra_group_wirelength_mm']:.0f} mm within "
          f"groups ({data['intra_group_nets']} nets); mean fill "
          f"{data['mean_fill_fraction']:.0%} of each block's bbox "
          f"(higher = tighter)")
    if data["n_piled_groups"]:
        show = ", ".join(data["piled_groups"][:6])
        print(f"⚠ piled         : {data['n_piled_groups']} group(s) can't fit "
              f"their own box — parts stacked / not placed yet (dropped from "
              f"cohesion): {show}")
    if data["n_overlapping_groups"]:
        show = ", ".join(data["overlapping_groups"][:6])
        print(f"⚠ overlapping   : {data['n_overlapping_groups']} group(s) have "
              f"overlapping courtyards ({data['overlap_pairs_total']} pair(s) "
              f"total) — real collisions to resolve: {show}")
    if data["groups_split_across_areas"]:
        show = "; ".join(f"{n} (areas {','.join(map(str, a))})"
                         for n, a in data["groups_split_across_areas"][:6])
        print(f"⚠ split sheets  : {len(data['groups_split_across_areas'])} "
              f"group(s) span >1 board area — same sheet, different boards: "
              f"{show}")
    if data["loosest_groups"]:
        worst = ", ".join(f"{name} {frac:.0%}"
                          for frac, name in data["loosest_groups"])
        print(f"confetti watch  : loosest valid groups — {worst}")
    top = list(data["group_graph"].items())[:6]
    if top:
        print("busiest seams   : " + "; ".join(f"{k} ({n})" for k, n in top))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Color-by-group floorplan render + cohesion score (§2)")
    ap.add_argument("board")
    ap.add_argument("partition", nargs="?", default=None,
                    help="partition.json ({groups:[{name,refs}]}); an optional "
                         "per-group \"family\" shares a color. Omit and pass "
                         "--groups-from-sheets to use the schematic sheets")
    ap.add_argument("--groups-from-sheets", action="store_true",
                    help="derive the grouping from the board's schematic sheets "
                         "(KiCad sheetname) — the grouping the human already "
                         "authored, zero setup — instead of a partition.json")
    ap.add_argument("--out", default=None, help="SVG path (default: <board>-floorplan.svg)")
    ap.add_argument("--json", action="store_true",
                    help="emit the cohesion data as JSON on stdout")
    args = ap.parse_args(argv)

    if args.groups_from_sheets and args.partition:
        ap.error("pass either a partition.json or --groups-from-sheets, not both")
    if not args.groups_from_sheets and not args.partition:
        ap.error("pass a partition.json, or --groups-from-sheets to use the "
                 "board's schematic sheets")
    # a bad board path, malformed/typo'd partition, or a no-sheets board is an
    # ordinary bad invocation — report it cleanly, not as a raw traceback
    # (matching stage.py / region.py's CLI house style).
    try:
        partition = (_sheets_partition(parts_from_board(args.board))
                     if args.groups_from_sheets
                     else _load_partition(args.partition))
        data = cohesion(args.board, partition)
        out = args.out or (os.path.splitext(args.board)[0] + "-floorplan.svg")
        render(args.board, partition, out, data=data)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        ap.error(str(e))
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"board           : {os.path.basename(args.board)}")
        _print_report(data)
        print(f"render          : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
