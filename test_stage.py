"""Tests for stage.py — the human-in-the-loop staging pass.

A synthetic two-area board with an off-board pile, an on-board part and a
locked part, plus a partition. The cases pin the three jobs:

  GENERATE  pile parts move into labelled margin boxes; on-board/locked parts
            and the source file are untouched; a bad partition is refused.
  HARVEST   a group whose parts sit in an area is (re)assigned to it, an
            untouched group keeps its proposal, a locked in-area part becomes a
            fixed anchor with its exact coordinates.
  DENSITY   the preflight WARNS on the over-dense area and stays SILENT on the
            sparse one — both directions, with the ratio derived by hand.

Run: .venv/bin/python test_stage.py
"""
import hashlib
import json
import os
import shutil

import stage
from place import COURTYARD_MARGIN_MM, parts_from_board

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out", "test-stage")

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ── fixture ────────────────────────────────────────────────────────────────
# area 0: (0,0)-(80,40) = 3200 mm2 (sparse).  area 1: (0,60)-(20,80) = 400 mm2.
AREA0 = (0.0, 0.0, 80.0, 40.0)
AREA1 = (0.0, 60.0, 20.0, 80.0)
PAD = 6.0                                    # 6x6 pad -> courtyard 6.5x6.5
COURT = (PAD + 2 * COURTYARD_MARGIN_MM) ** 2  # 42.25 mm2 per part

PCB_HEAD = """(kicad_pcb
\t(version 20240108)
\t(generator "test_stage")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
"""

# parts: (ref, x, y, locked). Pile parts sit off both areas at negative coords.
PILE = [(-30.0, -30.0), (-22.0, -30.0), (-14.0, -30.0), (-30.0, -22.0),
        (-22.0, -22.0), (-14.0, -22.0), (-30.0, -14.0), (-22.0, -14.0),
        (-14.0, -14.0), (-30.0, -6.0)]


def write_board(path, footprints):
    """footprints: list of (ref, x, y, locked_bool)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = [PCB_HEAD, '\t(net 0 "")\n', '\t(net 1 "N")\n']
    for x0, y0, x1, y1 in (AREA0, AREA1):
        body.append(f'\t(gr_rect (start {x0} {y0}) (end {x1} {y1}) '
                    f'(layer "Edge.Cuts") (width 0.1))\n')
    for ref, x, y, locked in footprints:
        lk = "\t\t(locked yes)\n" if locked else ""
        body.append(
            f'\t(footprint "R:R" (layer "F.Cu")\n'
            f'\t\t(at {x} {y})\n{lk}'
            f'\t\t(property "Reference" "{ref}")\n'
            f'\t\t(pad "1" smd rect (at 0 0) (size {PAD} {PAD}) '
            f'(layers "F.Cu") (net 1 "N"))\n'
            f'\t)\n')
    body.append(")\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(body))
    return path


def base_board():
    fps = [(f"G{i+1}", px, py, False) for i, (px, py) in enumerate(PILE[:3])]
    fps += [(f"PW{i+1}", px, py, False) for i, (px, py) in enumerate(PILE[3:10])]
    fps += [("ONB1", 40.0, 20.0, False)]           # on-board, area 0
    fps += [("LK1", 10.0, 70.0, True)]             # locked, area 1
    return write_board(os.path.join(OUT, "board.kicad_pcb"), fps)


PARTITION = {"groups": [
    {"name": "gain", "refs": ["G1", "G2", "G3"], "area": 0},
    {"name": "power", "refs": ["PW1", "PW2", "PW3", "PW4", "PW5", "PW6", "PW7"],
     "area": 1},
    {"name": "onboard", "refs": ["ONB1"], "area": 0},
    {"name": "anchor", "refs": ["LK1"], "area": 1},
]}


def _write_partition(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


# ── generate ───────────────────────────────────────────────────────────────
def test_generate():
    print("=== generate: pile boxed, on-board/locked and source untouched ===")
    board = base_board()
    part = _write_partition(os.path.join(OUT, "partition.json"), PARTITION)
    before = sha256(board)
    staged_dir = os.path.join(OUT, "staged")
    shutil.rmtree(staged_dir, ignore_errors=True)

    staged = stage.generate(board, part, staged_dir)
    sp = parts_from_board(staged)
    op = parts_from_board(board)

    # every pile part moved DOWN into the margin (below both areas), none of
    # them left where it started
    pile_refs = [f"G{i+1}" for i in range(3)] + [f"PW{i+1}" for i in range(7)]
    moved = all(sp[r].y_mm > 80.0 and
                (abs(sp[r].x_mm - op[r].x_mm) > 1e-6 or
                 abs(sp[r].y_mm - op[r].y_mm) > 1e-6) for r in pile_refs)
    check("all 10 pile parts moved into the margin below the board", moved,
          f"e.g. G1 -> ({sp['G1'].x_mm:.1f}, {sp['G1'].y_mm:.1f})")
    check("the on-board part ONB1 did NOT move",
          abs(sp["ONB1"].x_mm - 40.0) < 1e-6 and abs(sp["ONB1"].y_mm - 20.0) < 1e-6,
          f"({sp['ONB1'].x_mm}, {sp['ONB1'].y_mm})")
    check("the locked part LK1 did NOT move",
          abs(sp["LK1"].x_mm - 10.0) < 1e-6 and abs(sp["LK1"].y_mm - 70.0) < 1e-6,
          f"({sp['LK1'].x_mm}, {sp['LK1'].y_mm})")
    with open(staged, encoding="utf-8") as f:
        txt = f.read()
    check("group labels are written as board text on the user layer",
          all(f'"{n}"' in txt for n in ("gain", "power"))
          and stage.LABEL_LAYER in txt,
          f"layer {stage.LABEL_LAYER}")
    check("no group box sits on Edge.Cuts (would be a phantom region)",
          "Edge.Cuts" in txt and txt.count("gr_rect") >= 2
          and stage.LABEL_LAYER in txt)
    check("the partition was carried into the staged dir",
          os.path.isfile(os.path.join(staged_dir, "partition.json")))
    check("the SOURCE board is byte-identical (read-only honoured)",
          sha256(board) == before)


def test_generate_refuses_source_dir_and_bad_partition():
    print("=== generate: refuses writing into the source dir + bad partitions ===")
    board = base_board()
    part = _write_partition(os.path.join(OUT, "partition.json"), PARTITION)
    # out dir == source dir -> writeback._refuse_source_dir must fire
    try:
        stage.generate(board, part, os.path.dirname(board))
        check("writing a staged copy into the source dir is refused", False)
    except ValueError as e:
        check("writing a staged copy into the source dir is refused",
              "source" in str(e).lower(), str(e))

    dup = _write_partition(os.path.join(OUT, "dup.json"), {"groups": [
        {"name": "a", "refs": ["G1", "G2"], "area": 0},
        {"name": "b", "refs": ["G2", "G3"], "area": 1}]})   # G2 in two groups
    try:
        stage.generate(board, dup, os.path.join(OUT, "x"))
        check("a ref in two groups is refused", False)
    except ValueError as e:
        check("a ref in two groups is refused", "more than one group" in str(e),
              str(e))

    bad = _write_partition(os.path.join(OUT, "bad.json"), {"groups": [
        {"name": "a", "refs": ["NOPE1", "NOPE2"], "area": 0}]})
    try:
        stage.generate(board, bad, os.path.join(OUT, "x"))
        check("refs that aren't on the board are refused", False)
    except ValueError as e:
        check("refs that aren't on the board are refused",
              "not footprints" in str(e) and "NOPE1" in str(e), str(e))


# ── harvest + density ────────────────────────────────────────────────────────
def test_harvest_proposed_keeps_area_and_anchors_locked():
    print("=== harvest (no drags): keeps proposals, anchors the locked part ===")
    board = base_board()
    part = _write_partition(os.path.join(OUT, "partition.json"), PARTITION)
    staged_dir = os.path.join(OUT, "staged2")
    shutil.rmtree(staged_dir, ignore_errors=True)
    stage.generate(board, part, staged_dir)

    out = os.path.join(OUT, "enriched.json")
    data = stage.harvest(staged_dir, None, out)
    byname = {g["name"]: g for g in data["groups"]}

    # pile groups still in the margin -> proposals unchanged
    check("group 'power' keeps its proposed area 1 (still in the margin)",
          byname["power"]["area"] == 1, str(byname["power"]["area"]))
    check("group 'gain' keeps its proposed area 0", byname["gain"]["area"] == 0)
    # LK1 is locked inside area 1 -> a fixed anchor at its exact coordinates
    anch = byname["anchor"].get("anchors", [])
    check("the locked in-area part LK1 became a fixed anchor",
          len(anch) == 1 and anch[0]["ref"] == "LK1"
          and abs(anch[0]["x"] - 10.0) < 1e-6 and abs(anch[0]["y"] - 70.0) < 1e-6,
          str(anch))
    check("the un-locked on-board group has NO anchor",
          "anchors" not in byname["onboard"] or not byname["onboard"]["anchors"])
    check("enriched json round-trips as valid json", os.path.isfile(out)
          and json.load(open(out)).get("groups"))


def test_harvest_drag_reassigns_area():
    print("=== harvest: a group dragged onto an area is reassigned to it ===")
    board = base_board()
    part = _write_partition(os.path.join(OUT, "partition.json"), PARTITION)
    staged_dir = os.path.join(OUT, "staged3")
    shutil.rmtree(staged_dir, ignore_errors=True)
    staged = stage.generate(board, part, staged_dir)

    # simulate the human dragging group 'gain' (proposed area 0) into area 1
    from writeback import write_moved_copy
    edited = os.path.join(OUT, "edited.kicad_pcb")
    write_moved_copy(staged, edited, {
        "G1": (4.0, 62.0, 0.0), "G2": (10.0, 65.0, 0.0), "G3": (16.0, 68.0, 0.0)})

    out = os.path.join(OUT, "enriched3.json")
    data = stage.harvest(edited, part, out)
    byname = {g["name"]: g for g in data["groups"]}
    check("group 'gain' was reassigned from area 0 to area 1 by the drag",
          byname["gain"]["area"] == 1, str(byname["gain"]["area"]))
    check("group 'onboard' (ONB1 in area 0) resolves to area 0",
          byname["onboard"]["area"] == 0, str(byname["onboard"]["area"]))
    check("group 'power' (still in margin) keeps proposed area 1",
          byname["power"]["area"] == 1)


def test_density_preflight_both_directions():
    print("=== density preflight: warns on the dense area, silent on the sparse ===")
    board = base_board()
    from board import load_board
    from lattice import board_outline_regions
    regions = board_outline_regions(load_board(board))
    parts = parts_from_board(board)
    # area 1 proposal: power(7) + anchor LK1(1) = 8 parts; area 0: gain(3)+ONB1(1)=4
    a1 = [parts[r] for r in ("PW1", "PW2", "PW3", "PW4", "PW5", "PW6", "PW7", "LK1")]
    a0 = [parts[r] for r in ("G1", "G2", "G3", "ONB1")]
    rows, warns = stage._density_preflight({0: a0, 1: a1}, regions)

    ratio1 = 8 * COURT / 400.0            # ~0.845 by hand
    ratio0 = 4 * COURT / 3200.0           # ~0.053
    got1 = next(r[3] for r in rows if r[0] == 1)
    got0 = next(r[3] for r in rows if r[0] == 0)
    check("area 1 density computed correctly (~0.85)", abs(got1 - ratio1) < 1e-6,
          f"{got1:.3f} vs {ratio1:.3f}")
    check("area 0 density computed correctly (~0.05)", abs(got0 - ratio0) < 1e-6,
          f"{got0:.3f} vs {ratio0:.3f}")
    check("the over-dense area 1 PRODUCES a warning",
          any("area 1" in w for w in warns), str(warns))
    check("the sparse area 0 produces NO warning",
          not any("area 0" in w for w in warns), str(warns))
    check("the warning names the infeasibility, not just a number",
          any("infeasible" in w.lower() or "impossible" in w.lower()
              for w in warns if "area 1" in w))


def test_density_hard_vs_soft_threshold():
    print("=== density preflight: soft (>60%) vs hard (>100%) wording ===")
    board = base_board()
    from board import load_board
    from lattice import board_outline_regions
    regions = board_outline_regions(load_board(board))
    parts = parts_from_board(board)
    # 8 parts * 42.25 = 338 mm2 in a 400 mm2 fence -> 84.5% -> soft
    soft = [parts[r] for r in ("PW1", "PW2", "PW3", "PW4", "PW5", "PW6", "PW7", "LK1")]
    _r, w_soft = stage._density_preflight({1: soft}, regions)
    check("84% density is the soft 'LIKELY infeasible' warning",
          any("area 1" in w and "LIKELY" in w for w in w_soft), str(w_soft))
    # 12 parts * 42.25 = 507 mm2 > 400 -> hard impossible
    hard = soft + [parts[r] for r in ("G1", "G2", "G3", "ONB1")]
    _r2, w_hard = stage._density_preflight({1: hard}, regions)
    check("127% density is the hard 'IMPOSSIBLE' warning",
          any("area 1" in w and "IMPOSSIBLE" in w for w in w_hard), str(w_hard))


if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    test_generate()
    test_generate_refuses_source_dir_and_bad_partition()
    test_harvest_proposed_keeps_area_and_anchors_locked()
    test_harvest_drag_reassigns_area()
    test_density_preflight_both_directions()
    test_density_hard_vs_soft_threshold()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    raise SystemExit(1 if FAILED else 0)
