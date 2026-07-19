"""L6 region-solver validation: synthetic first, then the real Voxy gain stage.

Synthetic scenarios are hand-built .kicad_pcb files small enough to reason
about in your head, because the four properties that matter are all easy to
fake and hard to notice faking:

1. TERMINAL PROPAGATION PULLS. The same fence, the same parts, the same seed —
   only the position of the OUTSIDE footprint changes. If the pseudo-pad is
   real, the parts follow it: outside-right pulls the placement right, and the
   crossing net's copper actually reaches the right fence edge. If terminal
   propagation were a no-op (the failure mode design rule 3 warns about), the
   two runs would be identical and the region would optimize as if alone.
2. RANKING IS STRICT. An unroutable placement never outranks a routable one,
   however short its copper — checked on the sort key directly (a weighted sum
   that lets a beautiful failure win is the bug) and on real ranked output.
3. DETERMINISM. Same seed, same everything, twice: identical placements and
   identical routed numbers.
4. OUT-OF-REGION PARTS NEVER MOVE, and the source board's bytes never change.

Then the acceptance test from REGION_SOLVER.md on the real Voxy board
(READ-ONLY), reported honestly — see ACCEPTANCE below for how the stage was
chosen.

Run: .venv/bin/python test_region.py [--no-acceptance] [--only NAME]
"""
import hashlib
import os
import shutil
import sys
import time

from writeback import board_footprints
from region import (boundary_terminals, optimize_region, rank_key,
                    strip_tracks_in_rect, _pad_owner_refs)
from board import load_board
from place import parts_from_board

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out",
                       "test-region")
SYN_DIR = os.path.join(OUT_DIR, "synthetic-src")
VOXY = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb"

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ── synthetic board fixture ──────────────────────────────────────────────────

NETS = ["", "SIG", "LINK", "GND", "LOCAL"]


def _fp(ref, x, y, pads):
    """One SMD footprint: pads are (offset_x, offset_y, net_name), 1x1 mm."""
    body = [f'\t(footprint "R_1206" (layer "F.Cu")',
            f'\t\t(at {x} {y})',
            f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))']
    for i, (ox, oy, net) in enumerate(pads, start=1):
        body.append(
            f'\t\t(pad "{i}" smd rect (at {ox} {oy}) (size 1 1) '
            f'(layers "F.Cu") (net {NETS.index(net)} "{net}"))')
    body.append("\t)")
    return "\n".join(body)


def write_synth(path, outside_x):
    """A 40 x 20 mm board. Movable A, B, C live in the left half; the fence is
    (2,2,20,16). ONE outside footprint X carries SIG and GND and sits at
    outside_x — left of the fence or right of it. F is a frozen part inside
    the fence (a real in-fence obstacle and a real fixed terminal)."""
    fps = [
        _fp("A", 5.0, 6.0, [(-1.5, 0, "SIG"), (1.5, 0, "LINK")]),
        _fp("B", 5.0, 10.0, [(-1.5, 0, "LINK"), (1.5, 0, "GND")]),
        _fp("C", 5.0, 14.0, [(-1.5, 0, "GND"), (1.5, 0, "LOCAL")]),
        _fp("F", 12.0, 18.0, [(-1.5, 0, "LOCAL"), (1.5, 0, "GND")]),
        _fp("X", outside_x, 10.0, [(-1.5, 0, "SIG"), (1.5, 0, "GND")]),
    ]
    text = (
        '(kicad_pcb (version 20240108) (generator "test_region")\n'
        '\t(general (thickness 1.6))\n'
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        + "".join(f'\t(net {i} "{n}")\n' for i, n in enumerate(NETS))
        + '\t(gr_line (start -8 0) (end 48 0) (layer "Edge.Cuts") (width 0.1))\n'
          '\t(gr_line (start 48 0) (end 48 24) (layer "Edge.Cuts") (width 0.1))\n'
          '\t(gr_line (start 48 24) (end -8 24) (layer "Edge.Cuts") (width 0.1))\n'
          '\t(gr_line (start -8 24) (end -8 0) (layer "Edge.Cuts") (width 0.1))\n'
        + "\n".join(fps) + "\n"
          '\t(segment (start 6 6) (end 10 6) (width 0.25) (layer "F.Cu") (net 2))\n'
          '\t(segment (start 30 6) (end 34 6) (width 0.25) (layer "F.Cu") (net 1))\n'
          '\t(segment (start 18 10) (end 30 10) (width 0.25) (layer "F.Cu") (net 1))\n'
          '\t(via (at 8 8) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 3))\n'
          ')\n')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


FENCE = (2.0, 2.0, 20.0, 16.0)
MOVABLE = ["A", "B", "C"]


def run(path, out, **kw):
    kw.setdefault("k", 3)
    kw.setdefault("pitch_mm", 0.5)
    kw.setdefault("seed", 0)
    kw.setdefault("sweeps", 60)
    return optimize_region(path, MOVABLE, FENCE, out_dir=out, **kw)


# ── 1. terminal propagation ──────────────────────────────────────────────────

def test_terminal_propagation():
    print("=== terminal propagation: the pseudo-pad must actually pull ===")
    right_src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    left_src = os.path.join(SYN_DIR, "left", "synth.kicad_pcb")
    write_synth(right_src, outside_x=34.0)
    write_synth(left_src, outside_x=-4.0)

    # the terminals themselves, before any search
    for src, side, x_expect in ((right_src, "right", 22.0),
                                (left_src, "left", 2.0)):
        brd = load_board(src)
        terms, fixed_inside = boundary_terminals(
            brd, _pad_owner_refs(src, brd), set(MOVABLE), FENCE)
        by_net = {t.net_name: t for t in terms}
        check(set(by_net) == {"SIG", "GND"},
              f"{side}: crossing nets are exactly SIG and GND "
              f"(got {sorted(by_net)})")
        check(all(t.side == side for t in terms),
              f"{side}: both pseudo-pads sit on the {side} fence edge "
              f"(got {sorted({t.side for t in terms})})")
        check(all(abs(t.x_mm - x_expect) < 1e-9 for t in terms),
              f"{side}: pseudo-pads clamped to x={x_expect} "
              f"(got {sorted({round(t.x_mm, 3) for t in terms})})")
        check("LOCAL" in fixed_inside,
              "the frozen in-fence part F contributes a fixed LOCAL terminal")
        check("LOCAL" not in by_net,
              "a net that never leaves the fence gets NO pseudo-pad")

    r_right = run(right_src, os.path.join(OUT_DIR, "pull-right"))
    r_left = run(left_src, os.path.join(OUT_DIR, "pull-left"))
    check(bool(r_right.candidates) and bool(r_left.candidates),
          "both pull runs produced candidates")
    if not (r_right.candidates and r_left.candidates):
        return

    def mean_x(res):
        p = res.candidates[0].placements
        return sum(v[0] for v in p.values()) / len(p)

    mx_r, mx_l = mean_x(r_right), mean_x(r_left)
    check(mx_r > mx_l + 1.0,
          f"outside-right pulls the placement right of outside-left "
          f"(mean x {mx_r:.2f} vs {mx_l:.2f}) — terminal propagation is live")

    # and the copper really goes there: the SIG tracks the router laid INSIDE
    # the fence must run all the way to the fence edge the terminal is on.
    # (Endpoints outside the fence are pre-existing board copper the strip
    # left alone — counting those would let the test pass on a no-op.)
    def reaches(res, side):
        brd = load_board(res.candidates[0].board_copy)
        code = next(c for c, n in brd.nets.items() if n == "SIG")
        from region import _in_rect
        xs = [p[0] for t in brd.tracks if t.net_code == code
              for p in (t.start_mm, t.end_mm)
              if _in_rect(p[0], p[1], FENCE)]
        if not xs:
            return None
        return max(xs) if side == "right" else min(xs)

    xr = reaches(r_right, "right")
    xl = reaches(r_left, "left")
    check(xr is not None and xr >= FENCE[0] + FENCE[2] - 0.75,
          f"SIG copper reaches the RIGHT fence edge at x={xr} "
          f"(fence right = {FENCE[0] + FENCE[2]})")
    check(xl is not None and xl <= FENCE[0] + 0.75,
          f"SIG copper reaches the LEFT fence edge at x={xl} "
          f"(fence left = {FENCE[0]})")
    d = r_right.diagnostics
    check(len(d["boundary_nets"]) == 2 and d["stripped_tracks"] is not None,
          f"diagnostics name the boundary nets and the stripped copper "
          f"({len(d['boundary_nets'])} nets, {d['stripped_tracks']})")


# ── 2. ranking ───────────────────────────────────────────────────────────────

def test_ranking():
    print("=== ranking: a placement that does not route never outranks one "
          "that does ===")
    routable = {"failed": [], "violations": [], "wirelength_mm": 999.0,
                "vias": 40, "elite": 9}
    broken = {"failed": [("SIG", "target unreachable")], "violations": [],
              "wirelength_mm": 1.0, "vias": 0, "elite": 0}
    dirty = {"failed": [], "violations": ["R1-R2 too close"],
             "wirelength_mm": 1.0, "vias": 0, "elite": 1}
    order = sorted([broken, dirty, routable], key=rank_key)
    check(order[0] is routable and order[1] is dirty and order[2] is broken,
          "1079 mm of clean copper beats a 1 mm route that violates a "
          "constraint, and BOTH beat a 1 mm route that does not connect")
    tie_a = {"failed": [], "violations": [], "wirelength_mm": 10.0,
             "vias": 1, "elite": 5}
    tie_b = {"failed": [], "violations": [], "wirelength_mm": 13.0,
             "vias": 0, "elite": 2}
    check(sorted([tie_b, tie_a], key=rank_key)[0] is tie_a,
          "10 mm + 1 via (= 12 mm) beats 13 mm + 0 vias")
    free_a = {"failed": [], "violations": [], "wirelength_mm": 10.0,
              "vias": 0, "elite": 5}
    free_b = {"failed": [], "violations": [], "wirelength_mm": 10.0,
              "vias": 1, "elite": 2}
    check(sorted([free_b, free_a], key=rank_key)[0] is free_a,
          "a via is never free: same copper, fewer vias wins")
    same = [{"failed": [], "violations": [], "wirelength_mm": 5.0,
             "vias": 0, "elite": e} for e in (3, 1, 2)]
    check([s["elite"] for s in sorted(same, key=rank_key)] == [1, 2, 3],
          "exact ties break on the elite index, never on dict order")

    src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    res = run(src, os.path.join(OUT_DIR, "rank"), k=4)
    keys = [(c.nets_total - c.nets_ok, len(c.constraint_violations),
             c.score) for c in res.candidates]
    check(keys == sorted(keys),
          f"shipped candidates are in non-decreasing rank order ({keys})")
    check(all(c.nets_ok <= c.nets_total for c in res.candidates),
          "routed counts are self-consistent")


# ── 3. determinism ───────────────────────────────────────────────────────────

def test_determinism():
    print("=== determinism: same seed, same answer ===")
    src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    a = run(src, os.path.join(OUT_DIR, "det-a"))
    b = run(src, os.path.join(OUT_DIR, "det-b"))
    same_place = [c.placements for c in a.candidates] == \
                 [c.placements for c in b.candidates]
    same_route = [(c.routed, c.wirelength_mm, c.vias, c.score)
                  for c in a.candidates] == \
                 [(c.routed, c.wirelength_mm, c.vias, c.score)
                  for c in b.candidates]
    check(same_place, "identical placements across two identical calls")
    check(same_route, "identical routed metrics across two identical calls "
                      "(the GPU router is deterministic too)")
    c = run(src, os.path.join(OUT_DIR, "det-c"), seed=7)
    differs = [x.placements for x in c.candidates] != \
              [x.placements for x in a.candidates]
    print(f"  note seed 7 {'explores different placements' if differs else
                           'happened to land on the same pool'}")


# ── 4. the fence is a fence ──────────────────────────────────────────────────

def test_frozen_parts():
    print("=== out-of-region parts never move; the source is never touched ===")
    src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    before = sha256(src)
    res = run(src, os.path.join(OUT_DIR, "frozen"))
    check(sha256(src) == before, "source board bytes identical after the run")
    with open(src, encoding="utf-8") as f:
        src_recs = {r.uref: (r.x_mm, r.y_mm, r.rot_deg)
                    for r in board_footprints(f.read())}
    moved_any = False
    for cand in res.candidates:
        with open(cand.board_copy, encoding="utf-8") as f:
            recs = {r.uref: (r.x_mm, r.y_mm, r.rot_deg)
                    for r in board_footprints(f.read())}
        for ref, place in src_recs.items():
            if ref in MOVABLE:
                moved_any = moved_any or recs[ref] != place
                continue
            if recs[ref] != place:
                check(False, f"cand-{cand.id}: frozen {ref} moved "
                             f"{place} -> {recs[ref]}")
                return
        for ref in MOVABLE:
            want = tuple(round(v, 6) for v in cand.placements[ref])
            got = tuple(round(v, 6) for v in recs[ref])
            got = (got[0], got[1], got[2] % 360.0)
            want = (want[0], want[1], want[2] % 360.0)
            if got != want:
                check(False, f"cand-{cand.id}: {ref} board copy says {got}, "
                             f"candidate says {want}")
                return
    check(True, f"X and F sit exactly where they started in all "
                f"{len(res.candidates)} candidate boards")
    check(moved_any, "at least one movable part actually moved "
                     "(the run was not a no-op)")
    for cand in res.candidates:
        check(os.path.isfile(cand.board_copy) and os.path.isfile(cand.svg),
              f"cand-{cand.id} shipped a board copy and an SVG")
        break


def test_strip():
    print("=== stripping the fence's existing copper ===")
    src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    with open(src, encoding="utf-8") as f:
        text = f.read()
    out, inside, crossing = strip_tracks_in_rect(text, FENCE)
    check((inside, crossing) == (2, 1),
          f"2 fully-inside copper items (1 segment + 1 via) and 1 crossing "
          f"segment removed, 1 outside segment kept (got {inside}, {crossing})")
    check(out.count("(segment") == 1 and out.count("(via") == 0,
          f"the survivor is the segment wholly outside the fence "
          f"({out.count('(segment')} segments, {out.count('(via')} vias left)")
    check(len(load_board(_tmp(out)).pads) == len(load_board(src).pads),
          "stripping copper leaves every footprint and pad intact")


def _tmp(text):
    p = os.path.join(OUT_DIR, "stripped-tmp.kicad_pcb")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def test_errors():
    print("=== caller mistakes fail loudly, an infeasible fence does not ===")
    src = os.path.join(SYN_DIR, "right", "synth.kicad_pcb")
    try:
        optimize_region(src, ["A", "NOPE"], FENCE,
                        out_dir=os.path.join(OUT_DIR, "err"))
        check(False, "unknown component rejected")
    except ValueError as e:
        check("NOPE" in str(e), f"unknown component rejected ({e})")
    try:
        optimize_region(src, ["A"], FENCE, constraints=["glue(A,B)"],
                        out_dir=os.path.join(OUT_DIR, "err"))
        check(False, "unknown constraint rejected")
    except ValueError as e:
        check("valid constraints are" in str(e),
              f"unknown constraint names the valid set ({str(e)[:60]}...)")
    tiny = optimize_region(src, MOVABLE, (2.0, 2.0, 3.0, 3.0),
                           out_dir=os.path.join(OUT_DIR, "tiny"), k=2,
                           sweeps=20)
    check(not tiny.candidates and tiny.diagnostics["infeasible_reason"],
          f"a fence too small returns zero candidates WITH a reason, not an "
          f"exception ({str(tiny.diagnostics['infeasible_reason'])[:70]}...)")
    check(tiny.diagnostics.get("suggested_expansion") is not None,
          "the infeasible path still suggests where to grow the fence")


# ── the acceptance test (REGION_SOLVER.md) ───────────────────────────────────

ACCEPTANCE = """
How the stage was chosen (Voxy-arduino.kicad_pcb, READ-ONLY)
-----------------------------------------------------------
This board carries NO 9-pin socket: it is the Voxy control/switching board and
the 12AX7 itself lives on the tube board, reached through 2-pin terminals. So
"the triode's ~8-12 associated parts" were found from the netlist, not from a
socket footprint. Nets are suffixed by section (T1/T2 = the two triodes, P1 =
the pentode); the triode-1 GAIN STAGE is the cluster that touches its grid and
plate terminals:

  G1T1  grid terminal to the tube          net Net-(G1T1-Pad1)   <- the socket
  PT1   plate terminal to the tube         net Net-(Q2-G)
  GstopT1 switched grid-stopper module     Audio Input T1 / Net-(G1T1-Pad1)
  R22   grid leak to ground                Audio Input T1 / GND-C
  Q2 + CCS1 + R36 + R29                    the plate-load current source
  C10   plate output coupling cap          Plate Audio Out T1
  R55   plate audio divider                Plate Audio Out T1 / GND-C
  R23 + R21  plate voltage sense divider   Plate Voltage T1
  R27   grid-stopper LED resistor          Gstop LED T1

13 parts in a 13 x 52 mm strip at x~104, y~18..62. G1T1 stands in for the tube
socket (fixed), GstopT1 is the grid stopper that must stay at the grid pin, and
R55 is the plate resistor that must stay away from the grid input — exactly the
three constraints the spec names.
"""

ACC_COMPONENTS = ["G1T1", "PT1", "GstopT1", "R22", "Q2", "CCS1", "R36", "R29",
                  "C10", "R55", "R23", "R21", "R27"]
ACC_REGION = (100.0, 15.0, 20.0, 50.0)
ACC_CONSTRAINTS = [
    "fixed(G1T1)",                              # the socket does not move
    "adjacency_max_distance(GstopT1,G1T1,3)",   # grid stopper at its pin
    "min_distance(R55,G1T1,4)",                 # plate R away from grid input
]


def test_preflight():
    """adjacency_max_distance is CENTER-to-center, so on real parts it has a
    hard geometric floor. The floor must be EXACT in both directions: refusing
    a buildable number is as bad as accepting an impossible one, and Voxy is
    the board that punishes the lazy version — its grid-stopper module's
    origin is 1.45 mm from one courtyard edge and 9.07 mm from the other, so
    assuming a centred courtyard puts the floor at 3.57 mm when the truth is
    2.30 mm, and the spec's own 3 mm constraint would be wrongly rejected."""
    print("=== preflight: impossible constraints are proved impossible, and "
          "possible ones are not ===")
    if not os.path.exists(VOXY):
        print(f"  SKIP {VOXY}: board absent")
        return
    from constraints import parse_constraints
    from region import preflight
    known = set(ACC_COMPONENTS)
    parts = [parts_from_board(VOXY, ACC_COMPONENTS)[r] for r in ACC_COMPONENTS]

    def pf(mm):
        return preflight(parts, ACC_REGION, parse_constraints(
            [f"adjacency_max_distance(GstopT1,G1T1,{mm})"], known_refs=known))

    check(len(pf(2.0)) == 1 and "2.30" in pf(2.0)[0],
          f"2 mm is refused with the exact floor: {pf(2.0)[0][:96]}...")
    check(pf(2.3) == [] and pf(3) == [],
          "2.3 mm and the spec's 3 mm are NOT refused (an off-centre "
          "courtyard must not invent an impossibility)")
    huge = preflight(parts, ACC_REGION, parse_constraints(
        ["min_distance(R55,G1T1,900)"], known_refs=known))
    check(len(huge) == 1 and "diagonal" in huge[0],
          f"a separation larger than the fence's diagonal is refused "
          f"({huge[0][:70]}...)")
    tiny = preflight(parts, (100.0, 15.0, 6.0, 6.0), [])
    check(any("mm2" in r for r in tiny),
          "a fence with less free area than the parts need is refused on area "
          "alone, before any search")


def test_geometry_warnings():
    """The module must name the places its model of the copper is thinner
    than the copper. Voxy's Q2/Q3 are SOT-89s whose pad 2 is a `custom` pad:
    board.py reads the 1.475 x 0.9 anchor rect and never sees the 3.1 x 1.7 mm
    heat-tab primitive, so the router will cross it and KiCad's DRC will call
    it a short — verified against kicad-cli on cand-1. Silence about that
    would be the black box."""
    print("=== the tool names its own blind spots ===")
    if not os.path.exists(VOXY):
        print(f"  SKIP {VOXY}: board absent")
        return
    from region import custom_pad_refs
    with open(VOXY, encoding="utf-8") as f:
        custom = set(custom_pad_refs(f.read()))
    check({"Q2", "Q3"} <= custom,
          f"the SOT-89s with custom heat-tab pads are detected "
          f"({len(custom)} such footprints on the board)")
    res = optimize_region(VOXY, ACC_COMPONENTS, ACC_REGION,
                          constraints=ACC_CONSTRAINTS, k=1,
                          out_dir=os.path.join(OUT_DIR, "warn"), sweeps=30)
    w = res.diagnostics.get("geometry_warnings") or []
    check(len(w) == 1 and set(w[0]["refs"]) == {"Q2", "Q3"},
          f"the run warns about exactly the parts whose copper it cannot see "
          f"({[x['refs'] for x in w]})")
    plain = optimize_region(
        os.path.join(SYN_DIR, "right", "synth.kicad_pcb"), MOVABLE, FENCE,
        out_dir=os.path.join(OUT_DIR, "warn-none"), k=1, sweeps=20)
    check((plain.diagnostics.get("geometry_warnings") or []) == [],
          "a board with no custom pads gets no invented warning")


def test_acceptance():
    print("=== ACCEPTANCE: one Voxy 12AX7 gain stage ===")
    print(ACCEPTANCE)
    if not os.path.exists(VOXY):
        print(f"  SKIP {VOXY}: board absent")
        return
    before = sha256(VOXY)
    out = os.path.join(OUT_DIR, "acceptance")
    t0 = time.perf_counter()
    res = optimize_region(VOXY, ACC_COMPONENTS, ACC_REGION,
                          constraints=ACC_CONSTRAINTS, k=5, pitch_mm=0.5,
                          layers=["F.Cu", "B.Cu"], out_dir=out, seed=0,
                          progress=lambda m: print("   " + m, flush=True))
    elapsed = time.perf_counter() - t0

    from region import _print_summary
    print()
    _print_summary(res, out)
    print()

    check(sha256(VOXY) == before, "the Voxy board's bytes are untouched")
    check(len(res.candidates) == 5, f"5 candidates shipped "
                                    f"(got {len(res.candidates)})")
    allrouted = all(c.nets_ok == c.nets_total for c in res.candidates)
    check(allrouted, "all k candidates fully routed "
          + ("" if allrouted else "— "
             + ", ".join(f"cand-{c.id} {c.routed}" for c in res.candidates)))
    noviol = all(not c.constraint_violations for c in res.candidates)
    check(noviol, "zero constraint violations across all candidates")
    check(elapsed < 180.0, f"runtime {elapsed:.1f} s is under the 3-minute "
                           f"target")
    d = res.diagnostics
    check(bool(d["boundary_nets"]),
          f"diagnostics: {len(d['boundary_nets'])} boundary nets named")
    check(bool(d.get("anneal")) and bool(d.get("lattice")),
          "diagnostics: search and lattice blocks present")
    bc = d.get("binding_constraint")
    check(bc is not None and bc.get("min_slack_mm") is not None,
          f"diagnostics: the binding constraint is named with its slack "
          f"({bc['constraint']} at {bc['min_slack_mm']} mm)" if bc else
          "diagnostics: no binding constraint reported")
    home = parts_from_board(VOXY, ["G1T1"])["G1T1"]
    check(all(abs(c.placements["G1T1"][0] - home.x_mm) < 1e-9
              and abs(c.placements["G1T1"][1] - home.y_mm) < 1e-9
              for c in res.candidates),
          "the fixed 'socket' G1T1 is at its original position in every "
          "candidate")
    check(d.get("seeded") is not None,
          f"diagnostics: seeded moves are reported "
          f"({len(d.get('seeded') or [])} pre-anneal move(s))")
    check(d.get("suggested_expansion") is not None,
          "diagnostics: an expansion suggestion is present")

    # candidate #1 vs the hand layout, same parts, same fence
    hand = _hand_wirelength(out)
    if hand is not None and res.candidates:
        got = res.candidates[0].wirelength_mm
        check(got <= 2.0 * hand,
              f"cand-1 region wirelength {got:.1f} mm is within 2x the hand "
              f"layout's {hand:.1f} mm (ratio {got / hand:.2f})")


def _hand_wirelength(out):
    """The same fence and the same parts routed AT ANDREW'S HAND PLACEMENT —
    the 2x yardstick of the spec's acceptance test.

    It goes through region.py's own _route_candidate on the stripped source
    board, so the baseline comes from the identical lattice, clearance model,
    terminal propagation and router; only the placement differs. (It cannot go
    through optimize_region with fixed(...) on everything: the hand layout has
    a courtyard-proxy overlap of its own, which the search would repair — and
    then the 'baseline' would no longer be the hand layout.)
    """
    from region import (_route_candidate, boundary_terminals,
                        strip_tracks_in_rect, _pad_owner_refs)
    from lattice import default_copper_rules
    try:
        brd = load_board(VOXY)
        ref_of_pad = _pad_owner_refs(VOXY, brd)
        movable = set(ACC_COMPONENTS)
        terminals, _fixed = boundary_terminals(brd, ref_of_pad, movable,
                                               ACC_REGION)
        codes = {t.net_code for t in terminals}
        codes |= {p.net_code for i, p in enumerate(brd.pads)
                  if p.net_code > 0 and ref_of_pad[i] in movable}
        with open(VOXY, encoding="utf-8") as f:
            text, _a, _b = strip_tracks_in_rect(f.read(), ACC_REGION)
        base = os.path.join(out, "hand", os.path.basename(VOXY))
        os.makedirs(os.path.dirname(base), exist_ok=True)
        with open(base, "w", encoding="utf-8") as f:
            f.write(text)
        clr, width = default_copper_rules(VOXY)
        j = _route_candidate(base, ACC_REGION, 0.5, ["F.Cu", "B.Cu"], movable,
                             codes, terminals, clr, width, {})
    except Exception as e:                       # noqa: BLE001 - reported
        print(f"  note hand-layout baseline unavailable: {e}")
        return None
    print(f"  note hand layout routes {j['nets_ok']}/{j['nets_total']} nets in "
          f"{j['wirelength_mm']:.1f} mm with {j['vias']} vias"
          + (f" ({len(j['failed'])} failed)" if j["failed"] else ""))
    return j["wirelength_mm"]


# ── driver ───────────────────────────────────────────────────────────────────

TESTS = [("terminal", test_terminal_propagation), ("rank", test_ranking),
         ("determinism", test_determinism), ("frozen", test_frozen_parts),
         ("strip", test_strip), ("errors", test_errors),
         ("preflight", test_preflight), ("warnings", test_geometry_warnings),
         ("acceptance", test_acceptance)]


def main(argv):
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    skip_acc = "--no-acceptance" in argv
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    write_synth(os.path.join(SYN_DIR, "right", "synth.kicad_pcb"), 34.0)
    t0 = time.perf_counter()
    for name, fn in TESTS:
        if only and name != only:
            continue
        if name == "acceptance" and skip_acc and not only:
            continue
        fn()
        print()
    print(f"{'FAILURES: ' + str(len(failures)) if failures else 'all checks passed'}"
          f"  ({time.perf_counter() - t0:.1f} s)")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
