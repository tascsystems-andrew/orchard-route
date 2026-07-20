"""L5 placement-search validation: the SA explorer on a synthetic mini-board.

The fixture is built from board.py dataclasses directly — no file, no GPU:
five movable two-pad parts chained by nets inside a 30x20 fence, plus two
frozen parts whose courtyards enter as obstacles and whose pads enter as
fixed net endpoints (the same shape boundary pseudo-pads take). Courtyard
rects for the assertions are recomputed HERE from first principles (the
parts' pad geometry is chosen so the courtyard is 3.5 x 1.5 mm by hand),
so overlap-freedom is judged independently of place.py's own geometry code.

What must hold (the task contract):
- zero courtyard overlaps in every elite state (movable-vs-movable AND
  movable-vs-frozen), courtyards inside the fence;
- hard constraints never violated in any elite;
- pool energies monotone (best first, non-decreasing down the pool);
- determinism: same seed -> identical pool, different seed may differ;
- a min_distance constraint visibly separates two refs vs the
  unconstrained run;
- pool pairwise distinct (max per-ref displacement > one grid step).

Plus the board plumbing: parts_from_board must reproduce hand-derived numbers
for a rotated valve on the committed amp_board fixture (fixtures/amp_board
.kicad_pcb — this test OWNS it, replacing the old read of Andrew's live hifi
board), and net_weights_from_project must flow through writeback's class loader.

Run: .venv/bin/python test_place.py
"""
import math
import os

from board import Pad
from constraints import evaluate_constraints, parse_constraints
from place import (COURTYARD_MARGIN_MM, Part, PlacementModel, anneal_region,
                   part_courtyard, parts_from_board, net_weights_from_project)

AMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures", "amp_board.kicad_pcb")

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def rpart(ref, x, y, nets, rot=0.0):
    """Two-pad 'resistor': pads 1x1 mm at x±1 — pad bbox 3.0 x 1.0, so the
    courtyard proxy is exactly (3.0 + 2*0.25) x (1.0 + 2*0.25) = 3.5 x 1.5."""
    pads = (Pad(x - 1.0, y, ["F.Cu"], 0, nets[0], 1.0, 1.0, False, 0.0, rot),
            Pad(x + 1.0, y, ["F.Cu"], 0, nets[1], 1.0, 1.0, False, 0.0, rot))
    return Part(ref, x, y, rot, pads)


def court_of(x, y, rot):
    """Independent courtyard math for rpart geometry at cardinal rotations."""
    hw, hh = (1.75, 0.75) if rot % 180.0 == 0.0 else (0.75, 1.75)
    return (x - hw, y - hh, x + hw, y + hh)


def overlap(a, b):
    return (a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and
            a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9)


def _write_courtyard_board(path):
    """A radial (two THT pads 5 mm apart + a 13x13 mm F.CrtYd circle) and an SMD
    part with no courtyard layer — the exact THT-body-overhangs-pads divergence
    the field report hit (feedback/courtyard-model-2026-07-20)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user)'
            ' (45 "F.CrtYd" user) (46 "B.CrtYd" user))\n'
            '\t(net 0 "") (net 1 "A") (net 2 "B")\n'
            '\t(gr_rect (start 0 0) (end 60 40) (layer "Edge.Cuts") (width 0.1))\n'
            '\t(footprint "Radial" (layer "F.Cu")\n\t\t(at 20 20)\n'
            '\t\t(property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(fp_circle (center 0 0) (end 6.5 0) (layer "F.CrtYd"))\n'
            '\t\t(pad "1" thru_hole circle (at -2.5 0) (size 1.6 1.6) (drill 0.8) '
            '(layers "*.Cu") (net 1 "A"))\n'
            '\t\t(pad "2" thru_hole circle (at 2.5 0) (size 1.6 1.6) (drill 0.8) '
            '(layers "*.Cu") (net 2 "B"))\n\t)\n'
            '\t(footprint "SMD" (layer "F.Cu")\n\t\t(at 45 20)\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at -1 0) (size 1.2 1.4) (layers "F.Cu") '
            '(net 1 "A"))\n'
            '\t\t(pad "2" smd rect (at 1 0) (size 1.2 1.4) (layers "F.Cu") '
            '(net 2 "B"))\n\t)\n)\n')


REGION = (0.0, 0.0, 30.0, 20.0)


def mini_board():
    """5 movable + 2 frozen, one net chain A-B-C-D-E-F plus a boundary
    terminal pulling A toward the left fence edge."""
    parts = [rpart("A", 3.0, 3.0, ("N1", "N2")),
             rpart("B", 10.0, 3.0, ("N2", "N3")),
             rpart("C", 17.0, 3.0, ("N3", "N4")),
             rpart("D", 3.0, 10.0, ("N4", "N5")),
             rpart("E", 10.0, 10.0, ("N5", "N6"))]
    frozen = [rpart("F", 26.0, 17.0, ("N6", "NX")),
              rpart("G", 26.0, 12.0, ("NX", "NY"))]
    obstacles = [part_courtyard(p) for p in frozen]
    fixed_points = {"N6": [(frozen[0].pads[0].x_mm, frozen[0].pads[0].y_mm)],
                    "N1": [(0.0, 10.0)]}   # boundary pseudo-pad on the fence
    return parts, frozen, obstacles, fixed_points


def audit(res, parts, obstacles, constraints=(), home=None):
    """Every elite, judged with test-local geometry + constraints.py only."""
    rot_ok = in_region = no_overlap = hard_ok = True
    for el in res.elites:
        courts = {}
        for p in parts:
            x, y, rot = el.placements[p.ref]
            if rot % 90.0 != 0.0:
                rot_ok = False
            courts[p.ref] = court_of(x, y, rot)
        rects = list(courts.values())
        for i in range(len(rects)):
            x0, y0, x1, y1 = rects[i]
            if x0 < REGION[0] - 1e-9 or y0 < REGION[1] - 1e-9 \
                    or x1 > REGION[0] + REGION[2] + 1e-9 \
                    or y1 > REGION[1] + REGION[3] + 1e-9:
                in_region = False
            for j in range(i + 1, len(rects)):
                if overlap(rects[i], rects[j]):
                    no_overlap = False
            for ob in obstacles:
                if overlap(rects[i], ob):
                    no_overlap = False
        if constraints:
            checks = evaluate_constraints(
                parse_constraints(constraints), el.placements, courts,
                rect=REGION, home=home, edge_tol_mm=1.0)
            if any(not c.ok for c in checks):
                hard_ok = False
    return rot_ok, in_region, no_overlap, hard_ok


def dist_ab(el, a="A", b="B"):
    ax, ay, _ = el.placements[a]
    bx, by, _ = el.placements[b]
    return math.hypot(ax - bx, ay - by)


if __name__ == "__main__":
    print("=== unconstrained anneal on the mini-board ===")
    parts, frozen, obstacles, fixed_points = mini_board()
    home = {p.ref: (p.x_mm, p.y_mm, p.rot_deg) for p in parts}
    res = anneal_region(parts, REGION, obstacles=obstacles,
                        fixed_points=fixed_points, seed=0, sweeps=80)
    check(1 <= len(res.elites) <= 8, f"elite pool populated ({len(res.elites)})")
    check(res.elites[0].energy <= res.initial_energy + 1e-9,
          f"best elite {res.elites[0].energy:.3f} improves on initial "
          f"{res.initial_energy:.3f}")
    es = [el.energy for el in res.elites]
    check(all(es[i] <= es[i + 1] + 1e-12 for i in range(len(es) - 1)),
          f"pool energies monotone, best first ({[round(e, 2) for e in es]})")
    rot_ok, in_region, no_overlap, _ = audit(res, parts, obstacles)
    check(rot_ok, "every elite rotation is cardinal (0/90/180/270)")
    check(in_region, "every elite courtyard stays inside the fence")
    check(no_overlap, "zero courtyard overlaps in every elite state "
                      "(movable-vs-movable and movable-vs-frozen)")
    check(all(el.penalty == 0.0 for el in res.elites),
          "every elite carries zero soft penalty (violations are rejected)")
    for el in res.elites:
        for kept in res.elites:
            if el is not kept:
                worst = max(math.hypot(el.placements[r][0] - kept.placements[r][0],
                                       el.placements[r][1] - kept.placements[r][1])
                            for r in el.placements)
                if worst <= res.grid_mm + 1e-9:
                    check(False, "pool pairwise distinct")
                    break
        else:
            continue
        break
    else:
        check(True, "pool pairwise distinct (max per-ref displacement "
                    "> one grid step)")
    check(res.courtyard_margin_mm == COURTYARD_MARGIN_MM,
          f"courtyard margin surfaced for diagnostics "
          f"({res.courtyard_margin_mm} mm)")

    print("=== determinism ===")
    res_same = anneal_region(parts, REGION, obstacles=obstacles,
                             fixed_points=fixed_points, seed=0, sweeps=80)
    check(res.elites == res_same.elites, "same seed -> identical elite pool")
    res_other = anneal_region(parts, REGION, obstacles=obstacles,
                              fixed_points=fixed_points, seed=7, sweeps=80)
    check(res_other.elites[0].energy <= res.initial_energy,
          "different seed still anneals (and may differ freely)")

    print("=== min_distance visibly separates A and B ===")
    base_best = dist_ab(res.elites[0])
    check(base_best < 8.0,
          f"unconstrained best pulls chained A-B together ({base_best:.2f} mm)")
    cons = ["min_distance(A,B,8)"]
    res_md = anneal_region(parts, REGION, cons, obstacles=obstacles,
                           fixed_points=fixed_points, seed=0, sweeps=80)
    dists = [dist_ab(el) for el in res_md.elites]
    check(all(d >= 8.0 - 1e-9 for d in dists),
          f"constrained pool keeps A-B >= 8 mm in EVERY elite "
          f"(min {min(dists):.2f})")
    _, _, no_overlap_md, hard_ok_md = audit(res_md, parts, obstacles,
                                            cons, home)
    check(no_overlap_md and hard_ok_md,
          "constrained run: overlap-free and constraint-clean per "
          "constraints.py itself")

    print("=== fixed + orientation_set + keepout, all hard ===")
    cons = ["fixed(C)", "orientation_set(B,[90,270])", "keepout(0,14,6,6)",
            "adjacency_max_distance(D,E,9)"]
    res_c = anneal_region(parts, REGION, cons, obstacles=obstacles,
                          fixed_points=fixed_points, seed=0, sweeps=80)
    check(not res_c.repaired,
          "B's rot 0 outside its orientation_set is SNAPPED to the nearest "
          "allowed angle at init (deterministic), no random repair needed")
    check(all(el.placements["C"] == (17.0, 3.0, 0.0) for el in res_c.elites),
          "fixed(C) pins C at home in every elite")
    check(all(el.placements["B"][2] in (90.0, 270.0) for el in res_c.elites),
          "orientation_set(B,[90,270]) holds in every elite")
    check(all(dist_ab(el, 'D', 'E') <= 9.0 + 1e-9 for el in res_c.elites),
          "adjacency_max_distance(D,E,9) holds in every elite")
    _, in_r, no_ov, hard = audit(res_c, parts, obstacles, cons, home)
    check(in_r and no_ov and hard,
          "keepout + all hard rules verified independently in every elite")

    print("=== repair: infeasible start (two parts on one spot) ===")
    clash = [rpart("A", 10.0, 10.0, ("N1", "N2")),
             rpart("B", 10.0, 10.0, ("N2", "N3"))]
    res_r = anneal_region(clash, REGION, seed=0, sweeps=20)
    check(res_r.repaired, "coincident parts trigger the repair phase")
    _, in_rr, no_ovr, _ = audit(res_r, clash, [])
    check(in_rr and no_ovr, "repaired pool is overlap-free and in-fence")

    print("=== boundary-terminal pull ===")
    lone = [rpart("A", 24.0, 16.0, ("N1", ""))]
    res_p = anneal_region(lone, REGION, fixed_points={"N1": [(2.0, 2.0)]},
                          seed=0, sweeps=120)
    ax, ay, _ = res_p.elites[0].placements["A"]
    check(math.hypot(ax - 2.0, ay - 2.0) < 4.0,
          f"a part whose only net ends in a fixed terminal at (2,2) walks "
          f"to it (ended at ({ax:.1f}, {ay:.1f}))")

    print("=== error paths ===")
    try:
        anneal_region(parts, REGION, ["min_distance(A,Z,5)"])
        check(False, "unknown constraint ref raises")
    except ValueError as e:
        check("unknown ref 'Z'" in str(e), f"unknown constraint ref raises ({e})")
    try:
        anneal_region(parts + [rpart("A", 20, 16, ("NX", "NY"))], REGION)
        check(False, "duplicate part refs raise")
    except ValueError as e:
        check("duplicate part refs" in str(e), f"duplicate part refs raise ({e})")
    try:
        anneal_region([rpart("A", 3, 3, ("N1", "N2"))], (0, 0, 2, 2))
        check(False, "impossible fence raises RuntimeError")
    except RuntimeError as e:
        check("no feasible starting placement" in str(e),
              f"impossible fence raises with reasons ({e})")

    print("=== parts_from_board against the amp_board fixture ===")
    # 5755#1 is the first of three duplicated "5755" valves: a 9-pad 3x3 grid
    # at (20, 20) rotated -90. KiCad's -90 maps local (ox, oy) to
    # (fx - oy, fy + ox), so its corner pad 1 (at -2 -2 216) lands at
    # (20 + 2, 20 - 2) = (22, 18) — hand-computed, independent of the parser.
    pb = parts_from_board(AMP, ["5755#1"])
    v = pb["5755#1"]
    check(len(v.pads) == 9, f"5755#1 carries its 9 pads ({len(v.pads)})")
    check((abs(v.x_mm - 20.0) < 1e-9 and abs(v.y_mm - 20.0) < 1e-9
           and v.rot_deg == -90.0),
          "5755#1 at the file's (20, 20, -90)")
    p1 = min(v.pads, key=lambda p: math.hypot(p.x_mm - 22.0, p.y_mm - 18.0))
    check(abs(p1.x_mm - 22.0) < 1e-4 and abs(p1.y_mm - 18.0) < 1e-4,
          "pad 1 at hand-computed (22, 18) via the -90 transform")
    cy = part_courtyard(v)
    check(cy[0] < v.x_mm < cy[2] and cy[1] < v.y_mm < cy[3],
          f"valve courtyard proxy encloses its center "
          f"({', '.join(f'{c:.2f}' for c in cy)})")
    allp = parts_from_board(AMP)
    check(len(allp) == 8, f"refs=None loads all 8 footprints ({len(allp)})")
    check(sum(len(p.pads) for p in allp.values()) == 36,
          "pad slices cover the board's 36 pads exactly once")

    print("=== real F.CrtYd courtyard vs pad-bbox proxy (finding #5) ===")
    import tempfile, shutil
    from dataclasses import replace
    cyd = tempfile.mkdtemp()
    try:
        cyb = os.path.join(cyd, "cy.kicad_pcb")
        _write_courtyard_board(cyb)
        cp = parts_from_board(cyb)
        c1, r1 = cp["C1"], cp["R1"]

        # parse: the radial's real 13x13 courtyard is attached in local frame;
        # the SMD (no courtyard layer) stays None -> pad-bbox proxy fallback.
        check(c1.local_courtyard is not None
              and max(abs(a - b) for a, b in
                      zip(c1.local_courtyard, (-6.5, -6.5, 6.5, 6.5))) < 1e-6,
              f"radial's real F.CrtYd (13x13 circle) parses to a local rect "
              f"({c1.local_courtyard})")
        check(r1.local_courtyard is None,
              "an SMD part with no courtyard layer keeps local_courtyard=None "
              "(pad-bbox proxy fallback, unchanged)")

        # the collision courtyard now reflects the real BODY, not the pad strip:
        # ~13.5 mm (13 + 2*margin) vs the ~7.1 mm pad-bbox proxy.
        real = part_courtyard(c1)
        proxy = part_courtyard(replace(c1, local_courtyard=None))
        rw, pw = real[2] - real[0], proxy[2] - proxy[0]
        check(abs(rw - 13.5) < 1e-6 and rw > 1.8 * pw,
              f"radial courtyard is the {rw:.1f} mm body+margin, not the "
              f"{pw:.1f} mm pad strip")

        # THE CORRECTNESS WIN: two radials 8 mm centre-to-centre. Their pads
        # (span 5 mm) clear at 8 mm, but their 13 mm BODIES overlap. With the
        # real courtyard the model REJECTS it; with the old pad-bbox proxy it
        # wrongly passes — the exact bug (region.py reported such placements
        # "feasible" while parts sat bodily on top of each other).
        a, b = replace(c1, ref="A"), replace(c1, ref="B")
        fence = (10.0, 10.0, 40.0, 20.0)
        states = [(20.0, 20.0, 0.0), (28.0, 20.0, 0.0)]   # 8 mm apart
        real_bad = any("overlap" in s for s in
                       PlacementModel([a, b], fence).problems(states))
        proxy_bad = any("overlap" in s for s in PlacementModel(
            [replace(a, local_courtyard=None), replace(b, local_courtyard=None)],
            fence).problems(states))
        check(real_bad and not proxy_bad,
              "two radials 8 mm apart: the model SEES the body overlap with the "
              "real courtyard, and (the bug) MISSED it with the pad-bbox proxy")

        # UNION FLOOR: a courtyard SMALLER than the pad span must never shrink
        # the keep-out below the pads (else the placer could allow a pad-on-pad
        # overlap — the very thing this change prevents). Pads span 10 mm, the
        # F.CrtYd is a tiny 2x2: the keep-out stays the 10 mm pad span + margin.
        big_pads = (Pad(-5.0, 0.0, ["F.Cu"], 0, "A", 1.0, 1.0, False, 0.0, 0.0),
                    Pad(5.0, 0.0, ["F.Cu"], 0, "B", 1.0, 1.0, False, 0.0, 0.0))
        tiny = Part("T", 0.0, 0.0, 0.0, big_pads,
                    local_courtyard=(-1.0, -1.0, 1.0, 1.0))
        wct = part_courtyard(tiny)
        check(abs((wct[2] - wct[0]) - 11.5) < 1e-6,
              f"union floor: a courtyard smaller than the pads keeps the pad "
              f"span (11.5 mm = 10 pads + 2*0.25), not the 2 mm courtyard "
              f"({wct[2] - wct[0]:.2f})")

        # ROTATION on an ASYMMETRIC courtyard: authored 10 (wide) x 6 (tall),
        # off origin; at rot=90 the world courtyard must SWAP to 6.5 x 10.5
        # (proves the courtyard rotates WITH the part, not just the symmetric
        # circle the earlier checks used).
        asym = Part("R", 0.0, 0.0, 90.0,
                    (Pad(0.0, 0.0, ["F.Cu"], 0, "A", 0.5, 0.5, False, 0.0, 0.0),),
                    local_courtyard=(-1.0, -3.0, 9.0, 3.0))
        wa = part_courtyard(asym)
        check(abs((wa[2] - wa[0]) - 6.5) < 1e-6 and abs((wa[3] - wa[1]) - 10.5) < 1e-6,
              f"asymmetric courtyard at rot=90 swaps to 6.5 x 10.5 "
              f"({wa[2] - wa[0]:.2f} x {wa[3] - wa[1]:.2f})")
    finally:
        shutil.rmtree(cyd, ignore_errors=True)

    print("=== mounting-hole keep-outs (finding A) ===")
    # rpart courtyard is 3.5 x 1.5 centred on (x, y). A part ON a hole circle is
    # infeasible; one clear of it is fine.
    on = PlacementModel([rpart("K", 10.0, 10.0, ("N1", "N2"))],
                        (0.0, 0.0, 30.0, 20.0), keepouts=[(10.0, 10.0, 3.0)])
    check(any("mounting-hole" in s for s in on.problems(on.initial_states())),
          "a courtyard sitting on a mounting-hole keep-out is flagged infeasible")
    off = PlacementModel([rpart("K", 25.0, 15.0, ("N1", "N2"))],
                         (0.0, 0.0, 30.0, 20.0), keepouts=[(10.0, 10.0, 3.0)])
    check(not any("mounting-hole" in s for s in off.problems(off.initial_states())),
          "a courtyard clear of the keep-out is fine")
    # circle beats the bbox: a courtyard tucked past the hole's CORNER (inside
    # the keep-out's bbox but outside its circle) is allowed — the reason the
    # keep-out is circular, not a square.
    from place import _rect_circle_overlap
    corner = (13.0, 12.0, 14.0, 13.0)   # nearest pt (13,12), dist 3.6 from (10,10)
    check(not _rect_circle_overlap(corner, 10.0, 10.0, 3.5)
          and _rect_circle_overlap((10.0, 10.0, 11.0, 11.0), 10.0, 10.0, 3.5),
          "the keep-out is a circle: a rect past its corner clears, one over its "
          "centre does not")

    print("=== minimum clearance gap (finding B) ===")
    # A right edge = 5+1.75 = 6.75; B left edge = 8.5-1.75 = 6.75 -> gap 0 (abut).
    a = rpart("A", 5.0, 5.0, ("N1", "N2"))
    b = rpart("B", 8.5, 5.0, ("N3", "N4"))
    g0 = PlacementModel([a, b], (0.0, 0.0, 30.0, 20.0), min_gap_mm=0.0)
    check(not any(("within" in s or "overlap" in s) for s in
                  g0.problems(g0.initial_states())),
          "abutting courtyards are legal at min_gap=0 (the old behaviour)")
    g5 = PlacementModel([a, b], (0.0, 0.0, 30.0, 20.0), min_gap_mm=0.5)
    check(any("within 0.5 mm" in s for s in g5.problems(g5.initial_states())),
          "abutting courtyards (0 gap) are REJECTED when 0.5 mm clearance is "
          "required — touching = 0 clearance = a DRC crash")
    far = rpart("B", 9.5, 5.0, ("N3", "N4"))   # left edge 7.75 -> gap 1.0 mm
    g5ok = PlacementModel([a, far], (0.0, 0.0, 30.0, 20.0), min_gap_mm=0.5)
    check(not any("within" in s for s in g5ok.problems(g5ok.initial_states())),
          "a 1.0 mm gap satisfies the 0.5 mm requirement")
    from place import _gap
    check(abs(_gap(part_courtyard(a), part_courtyard(b))) < 1e-9
          and abs(_gap(part_courtyard(a), part_courtyard(far)) - 1.0) < 1e-9,
          "the closest-approach gap is measured exactly (0.0 abutting, 1.0 apart)")

    print("=== the SEARCH path (judge/anneal) honours keep-outs + min-gap ===")
    # problems() is the repair walk; judge() gates every candidate the annealer
    # accepts into the elite pool. Exercise judge() by running a real anneal with
    # a keep-out and a nonzero min-gap, then assert EVERY shipped elite honours
    # both — so a regression that drops either check from the search path fails.
    from place import _rect_circle_overlap
    aps = [rpart(f"P{i}", 3.0 + 4.0 * i, 4.0, (f"N{i}a", f"N{i}b"))
           for i in range(4)]
    res = anneal_region(aps, (0.0, 0.0, 30.0, 20.0), keepouts=[(15.0, 12.0, 3.0)],
                        min_gap_mm=0.4, seed=0, sweeps=80)
    check(bool(res.elites), f"anneal produced elites with a keep-out + min-gap "
                            f"({len(res.elites)})")
    hole_hits = gap_viols = 0
    for el in res.elites:
        cts = [court_of(*el.placements[p.ref]) for p in aps]
        hole_hits += sum(_rect_circle_overlap(c, 15.0, 12.0, 3.0) for c in cts)
        for i in range(len(cts)):
            for j in range(i + 1, len(cts)):
                if _gap(cts[i], cts[j]) < 0.4 - 1e-6:
                    gap_viols += 1
    check(hole_hits == 0,
          f"every elite clears the mounting-hole keep-out in the SEARCH path, "
          f"not just problems() ({hole_hits} hits)")
    check(gap_viols == 0,
          f"every elite honours the 0.4 mm min-gap in the search path "
          f"({gap_viols} violations)")

    # Direct judge() probes — deterministic, so they catch a dropped check that
    # the anneal's HPWL spreading would otherwise hide. A/B courtyards 0.2 mm
    # apart: judge() accepts at min_gap=0, rejects at 0.4; a courtyard on a
    # keep-out is rejected. (Exercises the exact judge() branches, not emergence.)
    two = [rpart("A", 5.0, 5.0, ("n1", "n2")), rpart("B", 8.7, 5.0, ("n3", "n4"))]
    feas0 = PlacementModel(two, (0.0, 0.0, 30.0, 20.0), min_gap_mm=0.0).judge(
        [(5.0, 5.0, 0.0), (8.7, 5.0, 0.0)])[0]
    feas4 = PlacementModel(two, (0.0, 0.0, 30.0, 20.0), min_gap_mm=0.4).judge(
        [(5.0, 5.0, 0.0), (8.7, 5.0, 0.0)])[0]
    check(feas0 and not feas4,
          "judge() accepts a 0.2 mm gap at min_gap=0 and REJECTS it at 0.4 — the "
          "min-gap branch of the search gate is live")
    fk = PlacementModel([rpart("K", 15.0, 10.0, ("n1", "n2"))],
                        (0.0, 0.0, 30.0, 20.0), keepouts=[(15.0, 10.0, 3.0)]).judge(
        [(15.0, 10.0, 0.0)])[0]
    check(not fk, "judge() rejects a courtyard sitting on a keep-out")

    print("=== net weights via writeback's class loader ===")
    w = net_weights_from_project(AMP, ["GND", "B+250"])
    check(w == {"GND": 1.0, "B+250": 1.0},
          f"amp project (Default class only) -> weight 1.0 everywhere ({w})")
    w2 = net_weights_from_project(AMP, ["GND"], class_weights={"Default": 2.5})
    check(w2 == {"GND": 2.5}, f"class_weights override flows through ({w2})")
    check(net_weights_from_project("/nonexistent/x.kicad_pcb", ["GND"]) == {},
          "no project file -> empty map (all nets weigh 1.0)")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
