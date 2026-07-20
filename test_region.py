"""L6 region-solver validation: synthetic first, then a committed gain stage.

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

Then the acceptance test from REGION_SOLVER.md on a committed gain-stage
fixture (fixtures/gain_stage.kicad_pcb — this test OWNS it, replacing the old
read of Andrew's live, mid-redesign Voxy board), reported honestly — see
ACCEPTANCE below for what the stage is.

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
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GAIN_STAGE = os.path.join(FIXTURES, "gain_stage.kicad_pcb")

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
    # the enforced-by-default clearance is named on any infeasible run, so a
    # driver whose placement stopped working checks --min-gap, not the wrong
    # lever the repair walk happened to report (finding A/B review).
    check("min-gap" in (tiny.diagnostics["infeasible_reason"] or ""),
          "an infeasible run names the active min-gap clearance as a lever")


# ── the acceptance test (REGION_SOLVER.md) ───────────────────────────────────

ACCEPTANCE = """
The gain stage (fixtures/gain_stage.kicad_pcb)
----------------------------------------------
A committed, self-contained 12AX7 triode-1 gain stage this test OWNS — 13
movable parts the region solver places and routes inside a 20 x 50 mm fence at
(100, 15). It replaces the old read of Andrew's live Voxy board, which broke
whenever a part moved mid-redesign; the fixture is frozen and canonical, and
fixtures/gen_gain_stage.py documents how every asserted number was derived.

  G1T1  the tube "socket" (fixed)          grid GRIDIN / plate PLATE
  GstopT1 switched grid-stopper module     GRIDIN / GSTOPLED
  R22   grid leak to ground                GRIDIN / GND-C
  Q2 + CCS1 + R36 + R29                    the plate-load current source
  PT1   plate terminal                     PLATE / PLATEOUT
  C10   plate output coupling cap          PLATE / PLATEOUT
  R55   plate audio divider                PLATEOUT / GND-C
  R23 + R21  plate voltage sense divider   PLATE / VSENSE / GND-C
  R27   grid-stopper LED resistor          GSTOPLED / GND-C

G1T1 stands in for the tube socket (fixed), GstopT1 is the grid stopper that
must stay at the grid pin, and R55 is the plate resistor that must stay away
from the grid input — exactly the three constraints the spec names. Four nets
(GRIDIN, PLATEOUT, VPLUS, GND-C) reach furniture outside the fence, so they
cross it and become boundary terminals.
"""

ACC_COMPONENTS = ["G1T1", "PT1", "GstopT1", "R22", "Q2", "CCS1", "R36", "R29",
                  "C10", "R55", "R23", "R21", "R27"]
ACC_REGION = (100.0, 15.0, 20.0, 50.0)
ACC_CONSTRAINTS = [
    "fixed(G1T1)",                              # the socket does not move
    "adjacency_max_distance(GstopT1,G1T1,3)",   # grid stopper at its pin
    "min_distance(R55,G1T1,4)",                 # plate R away from grid input
]


def test_density_preflight():
    """Courtyard-density preflight (field report Finding 4): utilization is
    reported every run, above ~60% it WARNS the search will be tight, and an
    over-full fence comes back with a concrete grow number (not just 'grow the
    fence'). Parts carry a real 2x2 F.CrtYd -> part_courtyard 2.5x2.5 = 6.25 mm2
    each, so the numbers are exact and body-margin-independent."""
    print("=== density preflight (finding 4) ===")

    def board(path, n):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fps = "".join(
            f'\t(footprint "R" (layer "F.Cu") (at {2 + (i % 8) * 3} '
            f'{2 + (i // 8) * 3})\n'
            f'\t\t(property "Reference" "R{i}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            f'(layer "F.CrtYd") (width 0.05))\n'
            f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
            f'(net {i + 1} "N{i}")))\n' for i in range(n))
        nets = "".join(f'\t(net {i} "N{i}")\n' for i in range(n + 1))
        with open(path, "w") as f:
            f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                    '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                    '(44 "Edge.Cuts" user) (45 "F.CrtYd" user))\n' + nets +
                    '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") '
                    '(width 0.1))\n' + fps + ")\n")

    fence = (0.0, 0.0, 10.0, 10.0)          # 100 mm2; each part 6.25 mm2

    # 20 parts -> 125 mm2 -> 125%: impossible, zero candidates, a grow number.
    imp = os.path.join(SYN_DIR, "dens", "imp.kicad_pcb")
    board(imp, 20)
    res = optimize_region(imp, [f"R{i}" for i in range(20)], fence, k=2,
                          sweeps=20, seed=0,
                          out_dir=os.path.join(OUT_DIR, "dens-imp"))
    dn = res.diagnostics["density"]
    check(dn["verdict"] == "impossible" and abs(dn["utilization"] - 1.25) < 0.02,
          f"20 parts (125 mm2) in a 100 mm2 fence -> impossible at "
          f"{dn['utilization']:.0%}")
    check(not res.candidates, "an over-full fence returns zero candidates")
    se = res.diagnostics.get("suggested_expansion") or {}
    check(bool(se.get("direction")) and se.get("mm", 0) > 0
          and "utilization" in se.get("reason", ""),
          f"suggested_expansion is a concrete density-based grow number "
          f"({se.get('mm')} mm — {se.get('reason', '')[:48]}...)")

    # 12 parts -> 75 mm2 -> 75%: tight (warned) but still searched.
    tig = os.path.join(SYN_DIR, "dens", "tight.kicad_pcb")
    board(tig, 12)
    res2 = optimize_region(tig, [f"R{i}" for i in range(12)], fence, k=2,
                           sweeps=40, seed=0,
                           out_dir=os.path.join(OUT_DIR, "dens-tight"))
    dn2 = res2.diagnostics["density"]
    check(dn2["verdict"] == "tight" and 0.60 <= dn2["utilization"] < 1.0,
          f"12 parts -> tight at {dn2['utilization']:.0%} (warned, still searched)")

    # 4 parts -> 25 mm2 -> 25%: ok, no warning.
    okb = os.path.join(SYN_DIR, "dens", "ok.kicad_pcb")
    board(okb, 4)
    res3 = optimize_region(okb, [f"R{i}" for i in range(4)], fence, k=2,
                           sweeps=40, seed=0,
                           out_dir=os.path.join(OUT_DIR, "dens-ok"))
    dn3 = res3.diagnostics["density"]
    check(dn3["verdict"] == "ok" and dn3["utilization"] < 0.60,
          f"4 parts -> ok at {dn3['utilization']:.0%} (no density warning)")
    check(dn3["fence_mm2"] == 100.0 and abs(dn3["courtyard_mm2"] - 25.0) < 0.1,
          f"density reports exact areas (25 of 100 mm2) — utilization is not a "
          f"black box ({dn3['courtyard_mm2']} / {dn3['fence_mm2']})")


def test_preflight():
    """adjacency_max_distance is CENTER-to-center, so on real parts it has a
    hard geometric floor. The floor must be EXACT in both directions: refusing
    a buildable number is as bad as accepting an impossible one, and GstopT1 is
    the part that punishes the lazy version — its off-centre origin is 1.45 mm
    from one courtyard edge and 4.50 mm from the other (G1T1's is a centred
    0.85 mm square), so assuming a centred courtyard would put the floor at
    3.82 mm when the truth is 0.85 + 1.45 = 2.30 mm, and the spec's own 3 mm
    constraint would be wrongly rejected."""
    print("=== preflight: impossible constraints are proved impossible, and "
          "possible ones are not ===")
    from constraints import parse_constraints
    from region import preflight
    known = set(ACC_COMPONENTS)
    parts = [parts_from_board(GAIN_STAGE, ACC_COMPONENTS)[r]
             for r in ACC_COMPONENTS]

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
    than the copper. The fixture's Q2 (movable) and Q3 (a frozen part whose
    courtyard intrudes the fence) are SOT-89s whose pad 2 is a `custom` pad:
    board.py reads the 1.475 x 0.9 anchor rect and never sees the larger
    heat-tab primitive, so the router would cross it and KiCad's DRC would call
    it a short. Silence about that would be the black box."""
    print("=== the tool names its own blind spots ===")
    from region import custom_pad_refs
    with open(GAIN_STAGE, encoding="utf-8") as f:
        custom = set(custom_pad_refs(f.read()))
    check({"Q2", "Q3"} <= custom,
          f"the SOT-89s with custom heat-tab pads are detected "
          f"({len(custom)} such footprints on the board)")
    res = optimize_region(GAIN_STAGE, ACC_COMPONENTS, ACC_REGION,
                          constraints=ACC_CONSTRAINTS, k=1, min_gap_mm=0.0,
                          body_margin_mm=0.0,
                          out_dir=os.path.join(OUT_DIR, "warn"), sweeps=30)
    w = res.diagnostics.get("geometry_warnings") or []
    custom = [x for x in w if x["kind"] == "custom_pad_shape"]
    check(len(custom) == 1 and set(custom[0]["refs"]) == {"Q2", "Q3"},
          f"the run warns about exactly the parts whose copper it cannot see "
          f"({[x['refs'] for x in custom]})")
    plain = optimize_region(
        os.path.join(SYN_DIR, "right", "synth.kicad_pcb"), MOVABLE, FENCE,
        out_dir=os.path.join(OUT_DIR, "warn-none"), k=1, sweeps=20)
    plain_custom = [x for x in (plain.diagnostics.get("geometry_warnings") or [])
                    if x["kind"] == "custom_pad_shape"]
    check(plain_custom == [],
          "a board with no custom pads gets no invented custom-pad warning")


def test_acceptance():
    print("=== ACCEPTANCE: one 12AX7 gain stage ===")
    print(ACCEPTANCE)
    before = sha256(GAIN_STAGE)
    out = os.path.join(OUT_DIR, "acceptance")
    t0 = time.perf_counter()
    # min_gap_mm=0 here: the gain stage is a DELIBERATELY tight packing fixture
    # (13 parts in a 20x50 fence), infeasible under the default 0.25 mm
    # clearance gap. This test stresses the packer+router; the min-gap constraint
    # is exercised on its own in test_place / test_min_gap.
    res = optimize_region(GAIN_STAGE, ACC_COMPONENTS, ACC_REGION,
                          constraints=ACC_CONSTRAINTS, k=5, pitch_mm=0.5,
                          layers=["F.Cu", "B.Cu"], out_dir=out, seed=0,
                          min_gap_mm=0.0, body_margin_mm=0.0,
                          progress=lambda m: print("   " + m, flush=True))
    elapsed = time.perf_counter() - t0

    from region import _print_summary
    print()
    _print_summary(res, out)
    print()

    check(sha256(GAIN_STAGE) == before, "the source board's bytes are untouched")
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
    home = parts_from_board(GAIN_STAGE, ["G1T1"])["G1T1"]
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
    """The same fence and the same parts routed AT THE FIXTURE'S HAND
    PLACEMENT — the 2x yardstick of the spec's acceptance test.

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
        brd = load_board(GAIN_STAGE)
        ref_of_pad = _pad_owner_refs(GAIN_STAGE, brd)
        movable = set(ACC_COMPONENTS)
        terminals, _fixed = boundary_terminals(brd, ref_of_pad, movable,
                                               ACC_REGION)
        codes = {t.net_code for t in terminals}
        codes |= {p.net_code for i, p in enumerate(brd.pads)
                  if p.net_code > 0 and ref_of_pad[i] in movable}
        with open(GAIN_STAGE, encoding="utf-8") as f:
            text, _a, _b = strip_tracks_in_rect(f.read(), ACC_REGION)
        base = os.path.join(out, "hand", os.path.basename(GAIN_STAGE))
        os.makedirs(os.path.dirname(base), exist_ok=True)
        with open(base, "w", encoding="utf-8") as f:
            f.write(text)
        clr, width = default_copper_rules(GAIN_STAGE)
        j = _route_candidate(base, ACC_REGION, 0.5, ["F.Cu", "B.Cu"], movable,
                             codes, terminals, clr, width, {})
    except Exception as e:                       # noqa: BLE001 - reported
        print(f"  note hand-layout baseline unavailable: {e}")
        return None
    print(f"  note hand layout routes {j['nets_ok']}/{j['nets_total']} nets in "
          f"{j['wirelength_mm']:.1f} mm with {j['vias']} vias"
          + (f" ({len(j['failed'])} failed)" if j["failed"] else ""))
    return j["wirelength_mm"]


# ── multi-area board: locked auto-fix, --area fencing, off-board pile ─────────
#
# The design-driver's real board: THREE board-outline areas, some parts LOCKED
# in KiCad and pre-placed, and the free parts sitting in an off-board pile at
# ~the origin (default placement — no position information). These fixtures fake
# exactly that so the four properties that matter are checkable by hand:
#   - a LOCKED footprint inside an area is auto-fixed: obstacle + fixed anchor,
#     never moved across candidates, without being listed;
#   - --area N resolves to the Nth Edge.Cuts outline as the fence;
#   - free parts that START in a collapsed pile still scatter to legal,
#     non-overlapping positions inside the fence (the make-or-break: SA/repair
#     must ignore the pile and place fresh);
#   - a net with pads in two areas gets a boundary terminal.

MA_NETS = ["", "N_A", "N_CROSS", "GND", "N_PILE"]


def _ma_fp(ref, x, y, pads, rot=0, locked=False):
    """One 1x1 mm SMD footprint for the multi-area fixture. pads: (ox,oy,net).
    locked=True emits the KiCad 8/9/10 (locked yes) child node."""
    if locked:
        head = ['\t(footprint "R_1206"', '\t\t(locked yes)',
                '\t\t(layer "F.Cu")']
    else:
        head = ['\t(footprint "R_1206" (layer "F.Cu")']
    body = list(head)
    body.append(f'\t\t(at {x} {y})' if not rot else f'\t\t(at {x} {y} {rot})')
    body.append(f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))')
    for i, (ox, oy, net) in enumerate(pads, start=1):
        body.append(
            f'\t\t(pad "{i}" smd rect (at {ox} {oy}) (size 1 1) '
            f'(layers "F.Cu") (net {MA_NETS.index(net)} "{net}"))')
    body.append("\t)")
    return "\n".join(body)


def _rect_outline(x0, y0, x1, y1):
    """Four Edge.Cuts gr_lines forming one closed rectangle = one board area."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return "\n".join(
        f'\t(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) '
        f'(layer "Edge.Cuts") (width 0.1))'
        for a, b in zip(corners, corners[1:]))


# Three disjoint 50 x 40 mm areas with air between them; pile at (2, 80), off
# every area. AREA 0 holds two LOCKED parts (L1, L2). PILE holds nine FREE parts
# assigned to area 0. A1 (in area 1) and A2 (in area 2) are placed furniture that
# put N_CROSS and GND across the area-0 boundary.
MA_AREAS = [(10, 10, 60, 50), (80, 10, 130, 50), (150, 10, 200, 50)]
MA_PILE_XY = (2.0, 80.0)
MA_PILE = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
_MA_PILE_PADS = {
    "P1": [(-1.5, 0, "N_A"), (1.5, 0, "N_CROSS")],
    "P2": [(-1.5, 0, "N_A"), (1.5, 0, "N_PILE")],
    "P3": [(-1.5, 0, "N_PILE"), (1.5, 0, "GND")],
    "P4": [(-1.5, 0, "N_CROSS"), (1.5, 0, "N_PILE")],
    "P5": [(-1.5, 0, "N_A"), (1.5, 0, "GND")],
    "P6": [(-1.5, 0, "N_PILE"), (1.5, 0, "GND")],
    "P7": [(-1.5, 0, "N_A"), (1.5, 0, "N_PILE")],
    "P8": [(-1.5, 0, "N_CROSS"), (1.5, 0, "GND")],
    "P9": [(-1.5, 0, "N_A"), (1.5, 0, "GND")],
}


def write_multiarea(path):
    fps = [
        _ma_fp("L1", 25.0, 25.0, [(-1.5, 0, "N_A"), (1.5, 0, "GND")], locked=True),
        _ma_fp("L2", 45.0, 38.0, [(-1.5, 0, "N_A"), (1.5, 0, "GND")], locked=True),
        _ma_fp("A1", 100.0, 30.0, [(-1.5, 0, "N_CROSS"), (1.5, 0, "GND")]),
        _ma_fp("A2", 170.0, 30.0, [(-1.5, 0, "GND"), (1.5, 0, "N_PILE")]),
    ]
    # nine free parts stacked on ONE point — the off-board pile, no positions
    for j, ref in enumerate(MA_PILE):
        fps.append(_ma_fp(ref, MA_PILE_XY[0] + 0.001 * j, MA_PILE_XY[1],
                          _MA_PILE_PADS[ref]))
    text = (
        '(kicad_pcb (version 20240108) (generator "test_region")\n'
        '\t(general (thickness 1.6))\n'
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        + "".join(f'\t(net {i} "{n}")\n' for i, n in enumerate(MA_NETS))
        + "\n".join(_rect_outline(*a) for a in MA_AREAS) + "\n"
        + "\n".join(fps) + "\n)\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def _courts_of(board_path, refs):
    """{ref: courtyard rect} at a board's CURRENT placement, via place.py's own
    geometry — so overlap/inside checks use the same courtyard the solver did."""
    from place import parts_from_board, part_courtyard
    parts = parts_from_board(board_path)
    return {r: part_courtyard(parts[r]) for r in refs if r in parts}


def test_locked_flag():
    print("=== the locked flag is parsed from the real KiCad token ===")
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    with open(src, encoding="utf-8") as f:
        recs = {r.uref: r for r in board_footprints(f.read())}
    check(recs["L1"].locked and recs["L2"].locked,
          "the two (locked yes) footprints read as locked")
    check(not any(recs[r].locked for r in MA_PILE + ["A1", "A2"]),
          "every unlocked footprint reads as unlocked (no false positives)")
    from place import parts_from_board
    parts = parts_from_board(src)
    check(parts["L1"].locked and not parts["P1"].locked,
          "the flag propagates through place.Part")


def test_area_fence():
    print("=== --area N fences on a board outline region ===")
    from region import area_fence
    from lattice import board_outline_regions
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    regions = board_outline_regions(load_board(src))
    check(len(regions) == 3, f"the 3 disjoint outlines read as 3 areas "
                             f"(got {len(regions)})")
    f0 = area_fence(src, 0)
    check(abs(f0[0] - 10) < 1e-6 and abs(f0[2] - 50) < 1e-6,
          f"area 0 resolves to its bbox (10,10,50,40) — got "
          f"{tuple(round(v, 1) for v in f0)}")
    try:
        area_fence(src, 5)
        check(False, "out-of-range area rejected")
    except ValueError as e:
        check("out of range" in str(e), f"out-of-range area names the count "
                                        f"({str(e)[:50]}...)")


def test_auto_fix_locked():
    print("=== locked parts inside the fence are auto-fixed and never move; "
          "free parts land in the area; a crossing net gets a terminal ===")
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    before = sha256(src)
    home = _courts_of(src, ["L1", "L2"])
    from place import parts_from_board
    home_xy = {r: (parts_from_board(src)[r].x_mm, parts_from_board(src)[r].y_mm)
               for r in ("L1", "L2")}

    res = optimize_region(src, MA_PILE, None, area=0, k=3, pitch_mm=0.5,
                          seed=0, sweeps=80,
                          out_dir=os.path.join(OUT_DIR, "ma-autofix"))
    check(sha256(src) == before, "source board bytes untouched")
    d = res.diagnostics
    check(set(d["auto_fixed"]) == {"L1", "L2"},
          f"the two locked in-fence parts were auto-fixed WITHOUT being listed "
          f"(got {d['auto_fixed']})")
    check(sorted(d["movable"]) == sorted(MA_PILE),
          f"only the named free parts are movable (got {d['movable']})")
    check(bool(res.candidates), f"the off-board pile produced candidates "
                                f"({len(res.candidates)})")
    if not res.candidates:
        return
    fence = (10.0, 10.0, 50.0, 40.0)
    from region import _in_rect
    for cand in res.candidates:
        recs = parts_from_board(cand.board_copy)
        for r in ("L1", "L2"):
            gx, gy = recs[r].x_mm, recs[r].y_mm
            if abs(gx - home_xy[r][0]) > 1e-6 or abs(gy - home_xy[r][1]) > 1e-6:
                check(False, f"cand-{cand.id}: locked {r} moved "
                             f"{home_xy[r]} -> {(gx, gy)}")
                return
        for r in MA_PILE:
            c = _courts_of(cand.board_copy, [r])[r]
            if not (c[0] >= fence[0] - 1e-6 and c[1] >= fence[1] - 1e-6
                    and c[2] <= fence[0] + fence[2] + 1e-6
                    and c[3] <= fence[1] + fence[3] + 1e-6):
                check(False, f"cand-{cand.id}: free {r} courtyard {c} left "
                             f"area 0")
                return
    check(True, "L1/L2 sit exactly at their KiCad position in every candidate, "
                "and every free part's courtyard is inside area 0")
    crossing = {t["net_name"] for t in d["boundary_nets"]}
    check("N_CROSS" in crossing,
          f"the net with pads in area 0 AND area 1 got a boundary terminal "
          f"(crossing nets: {sorted(crossing)})")


def test_offboard_pile():
    print("=== the off-board pile: N parts on ONE point scatter to legal, "
          "non-overlapping positions inside the fence ===")
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    # every pile part starts within a thousandth of a mm of the same point
    from place import parts_from_board
    start = parts_from_board(src)
    piled = [start[r] for r in MA_PILE]
    span = max(abs(a.x_mm - b.x_mm) + abs(a.y_mm - b.y_mm)
               for a in piled for b in piled)
    check(span < 0.05, f"the fixture really is a collapsed pile "
                       f"(max pairwise start offset {span:.4f} mm)")

    res = optimize_region(src, MA_PILE, None, area=0, k=3, pitch_mm=0.5,
                          seed=0, sweeps=80,
                          out_dir=os.path.join(OUT_DIR, "ma-pile"))
    check(bool(res.candidates),
          f"a maximally-infeasible pile start still yields candidates "
          f"({len(res.candidates)}) — SA/repair ignored the pile and placed fresh")
    seeded = res.diagnostics.get("seeded") or []
    scattered = [m for m in seeded if "scatter" in m["constraint"]]
    check(len(scattered) == len(MA_PILE),
          f"all {len(MA_PILE)} piled parts were scattered before annealing "
          f"(got {len(scattered)} scatter moves)")
    if not res.candidates:
        return
    fence = (10.0, 10.0, 50.0, 40.0)
    ok_all = True
    for cand in res.candidates:
        courts = _courts_of(cand.board_copy, MA_PILE)
        # inside the fence
        for r, c in courts.items():
            if not (c[0] >= fence[0] - 1e-6 and c[1] >= fence[1] - 1e-6
                    and c[2] <= fence[0] + fence[2] + 1e-6
                    and c[3] <= fence[1] + fence[3] + 1e-6):
                ok_all = False
        # pairwise non-overlapping (the courtyard proxy the solver enforces)
        refs = list(courts)
        for i in range(len(refs)):
            for j in range(i + 1, len(refs)):
                a, b = courts[refs[i]], courts[refs[j]]
                if (a[0] < b[2] - 1e-6 and b[0] < a[2] - 1e-6
                        and a[1] < b[3] - 1e-6 and b[1] < a[3] - 1e-6):
                    ok_all = False
    check(ok_all, "in every candidate all nine formerly-piled parts are inside "
                  "area 0 and pairwise non-overlapping")
    # determinism holds through the scatter
    res2 = optimize_region(src, MA_PILE, None, area=0, k=3, pitch_mm=0.5,
                           seed=0, sweeps=80,
                           out_dir=os.path.join(OUT_DIR, "ma-pile-b"))
    check([c.placements for c in res.candidates] ==
          [c.placements for c in res2.candidates],
          "the scatter is deterministic (identical placements on a re-run)")


def test_pile_explicit_required():
    print("=== a locked part named movable is a hard error; the primary path "
          "is an explicit ref list ===")
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    try:
        optimize_region(src, ["P1", "L1"], None, area=0,
                        out_dir=os.path.join(OUT_DIR, "ma-err"))
        check(False, "locked part named movable rejected")
    except ValueError as e:
        check("locked" in str(e).lower() and "L1" in str(e),
              f"naming a locked part movable fails loudly ({str(e)[:60]}...)")
    from region import net_adjacency
    adj = net_adjacency(src, ["P1", "P4", "A1"])
    check(adj["P1"] == ["A1", "P4"] or "P4" in adj["P1"],
          f"net_adjacency exposes connectivity as data (P1 -> {adj['P1']})")


def test_pile_report_vs_named_scatter():
    """Finding 1, correctly scoped — two questions the tool must NOT conflate:

    - The UNPLACED REPORT names which UN-named parts belong to no area, and it
      is board-outline-relative: a part whose courtyard is off EVERY Edge.Cuts
      outline is pile; on-board furniture never is. This is the field report's
      actual ask, and it is the behaviour that changed here (courtyard-vs-every-
      outline, replacing an older centre-in-outline test).

    - The SCATTER only ever sees parts the caller EXPLICITLY NAMED for this
      fence, so 'off this fence' is the right trigger there: a named part off
      the fence — origin pile, an F8 / Update-PCB drop at arbitrary coordinates,
      a neighbouring area, an adjacent band — was named to be moved HERE and is
      scattered in. Making scatter outline-relative would refuse to move a named
      on-board part and STRAND it off-fence (an adversarial review caught
      exactly that regression); the guards below lock the fence-relative
      behaviour in."""
    print("=== pile: outline-relative REPORT, fence-relative NAMED scatter ===")
    from region import seed_placement, _translate, _resolve_placement_set
    from lattice import board_outline_regions
    from place import part_courtyard
    src = os.path.join(SYN_DIR, "multiarea", "ma.kicad_pcb")
    write_multiarea(src)
    regions = board_outline_regions(load_board(src))
    check(len(regions) == 3, f"the fixture has 3 outline regions (got {len(regions)})")
    all_parts = parts_from_board(src)
    area0 = (10.0, 10.0, 50.0, 40.0)   # fences area 0; areas 1/2 are elsewhere

    def inside0(part):
        c = part_courtyard(part)
        return (c[0] >= area0[0] - 1e-6 and c[1] >= area0[1] - 1e-6
                and c[2] <= area0[0] + area0[2] + 1e-6
                and c[3] <= area0[1] + area0[3] + 1e-6)

    # REPORT is outline-relative. components=None -> unplaced names exactly the
    # off-board pile (P1..P9 at (2,80)); on-board furniture A1 (area 1) and A2
    # (area 2) are on real boards and are NEVER called unplaced.
    _mv, _af, _ef, unplaced = _resolve_placement_set(
        all_parts, area0, None, True, regions=regions)
    check(sorted(unplaced) == sorted(MA_PILE),
          f"unplaced report is outline-relative: exactly the off-board pile is "
          f"flagged, on-board A1/A2 are not (got {sorted(unplaced)})")

    # SCATTER guard 1 (the regression the review caught): a NAMED part sitting
    # ON the board in another area — off THIS fence — is still scattered INTO
    # this fence. It was named to be moved here; an outline-relative trigger
    # would leave it stranded at (175, 30). This is the mutation guard.
    p_area2 = _translate(all_parts["P1"], 175.0, 30.0)     # squarely in area 2
    out, moves = seed_placement([p_area2], area0, [], [], 0.5)
    p1 = next(p for p in out if p.ref == "P1")
    check({m["ref"] for m in moves} == {"P1"} and inside0(p1),
          "a named on-board part in another area IS scattered into this fence "
          "(named = move it here) — not left stranded off-fence")

    # SCATTER guard 2 (headline fix, already on HEAD): F8 drops at arbitrary
    # off-board coordinates — not just the collapsed origin — all scatter in.
    f8 = [_translate(all_parts[r], 400.0 + 25.0 * j, 30.0)
          for j, r in enumerate(MA_PILE)]
    _o, mv2 = seed_placement(f8, area0, [], [], 0.5)
    check({m["ref"] for m in mv2} == set(MA_PILE)
          and all(inside0(p) for p in _o),
          f"F8 drops at arbitrary off-board coordinates all scatter in "
          f"({len(mv2)}/{len(MA_PILE)}) — pile is not just the origin")

    # A part whose courtyard already OVERLAPS the fence keeps its position (the
    # anneal refines it); only off-fence named parts are gridded fresh.
    p_in = _translate(all_parts["P2"], 30.0, 30.0)         # inside area 0
    out3, mv3 = seed_placement([p_in], area0, [], [], 0.5)
    p2 = next(p for p in out3 if p.ref == "P2")
    check(not mv3 and (round(p2.x_mm, 3), round(p2.y_mm, 3)) == (30.0, 30.0),
          "a named part already inside the fence is left where it is, not "
          "re-scattered")


def test_list_courtyards():
    """--list-courtyards (finding #5): reports each footprint's courtyard w x h,
    flags the pad-bbox PROXY vs a real F.CrtYd, and never silently lists the
    whole board on an empty filter."""
    print("=== --list-courtyards: real-vs-proxy census ===")
    import io
    from contextlib import redirect_stdout
    from region import _list_courtyards
    src = os.path.join(SYN_DIR, "courtyard", "cy.kicad_pcb")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    with open(src, "w", encoding="utf-8") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user) (45 "F.CrtYd" user))\n'
            '\t(net 0 "")\n'
            '\t(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (width 0.1))\n'
            '\t(footprint "Rad" (layer "F.Cu") (at 10 10)\n'
            '\t\t(property "Reference" "REAL" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(fp_circle (center 0 0) (end 6.5 0) (layer "F.CrtYd"))\n'
            '\t\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) '
            '(layers "*.Cu") (net 0 "")))\n'
            '\t(footprint "Smd" (layer "F.Cu") (at 30 10)\n'
            '\t\t(property "Reference" "PROXY" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 2 1) (layers "F.Cu") (net 0 "")))\n'
            ')\n')

    def run(refs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _list_courtyards(src, refs)
        return buf.getvalue()

    out = run(["REAL", "PROXY", "BOGUS"])
    lines = out.splitlines()
    real_line = next(l for l in lines if l.strip().startswith("REAL"))
    proxy_line = next(l for l in lines if l.strip().startswith("PROXY"))
    check("13.00" in real_line and "169.00" in real_line and "[proxy]" not in real_line,
          f"REAL: real 13x13 courtyard (169 mm2), NOT flagged proxy ({real_line.strip()})")
    check("[proxy]" in proxy_line,
          f"PROXY (no F.CrtYd): flagged [proxy] ({proxy_line.strip()})")
    check(any("BOGUS" in l and "not on this board" in l for l in lines),
          "a bogus ref is reported '(not on this board)'")
    check("1/2 from real courtyard graphics, 1 pad-bbox proxy" in out,
          f"summary counts real vs proxy ({[l for l in lines if 'summary' in l]})")

    # a non-None but EMPTY selection lists nothing — never the whole board
    empty = run([])
    check("0/0" in empty and "REAL" not in empty,
          "an empty --components selection lists nothing, not the whole board")

    whole = run(None)
    check("REAL" in whole and "PROXY" in whole and "2 footprint(s)" in whole,
          "refs=None lists the whole board (header count matches)")


def test_hole_keepouts():
    """optimize_region wires board holes into circular keep-outs (finding A): the
    inflated radius reaches diagnostics, placed courtyards clear the hole, and a
    hole outside the fence is filtered out (gain_stage has no holes, so this is
    the only coverage of the region-level wiring)."""
    print("=== mounting-hole keep-outs through optimize_region ===")
    from place import parts_from_board, part_courtyard, _rect_circle_overlap
    src = os.path.join(SYN_DIR, "holes", "h.kicad_pcb")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    fps = "".join(
        f'\t(footprint "R" (layer "F.Cu") (at {5 + 3 * i} 5)\n'
        f'\t\t(property "Reference" "R{i}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
        f'(net {i + 1} "N{i}")))\n' for i in range(6))
    nets = "".join(f'\t(net {i} "N{i}")\n' for i in range(7))
    with open(src, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
                + nets +
                '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.1))\n'
                '\t(gr_circle (center 20 20) (end 21.6 20) (layer "Edge.Cuts") (width 0.1))\n'
                '\t(gr_circle (center 100 100) (end 101.6 100) (layer "Edge.Cuts") (width 0.1))\n'
                + fps + ")\n")
    res = optimize_region(src, ["R0", "R1", "R2", "R3", "R4", "R5"],
                          (1.0, 1.0, 38.0, 38.0), k=2, sweeps=60, seed=0,
                          hole_clearance_mm=3.0,
                          out_dir=os.path.join(OUT_DIR, "holes"))
    d = res.diagnostics
    check(len(d["hole_keepouts"]) == 1
          and abs(d["hole_keepouts"][0]["radius_mm"] - 4.6) < 1e-6,
          f"only the IN-FENCE hole becomes a keep-out, inflated to r=1.6+3.0=4.6 "
          f"(the far hole is filtered) ({d['hole_keepouts']})")
    check(d.get("hole_clearance_mm") == 3.0 and d.get("min_gap_mm") == 0.25,
          f"clearance settings reach diagnostics "
          f"({d.get('hole_clearance_mm')}, {d.get('min_gap_mm')})")
    check(bool(res.candidates),
          f"parts place around the hole ({len(res.candidates)} candidates)")
    clear = all(
        not _rect_circle_overlap(part_courtyard(parts_from_board(c.board_copy)[r]),
                                 20.0, 20.0, 4.6)
        for c in res.candidates for r in ["R0", "R1", "R2", "R3", "R4", "R5"])
    check(clear, "no placed courtyard intersects the hole keep-out")

    # finding C: R0..R5 are SMD with no F.CrtYd, so the run WARNS them by name
    # (silence here is how a board reports "0 overlaps" while relays overlap).
    allrefs = {"R0", "R1", "R2", "R3", "R4", "R5"}
    nc = [x for x in (d.get("geometry_warnings") or [])
          if x["kind"] == "no_courtyard"]
    check(len(nc) == 1 and set(nc[0]["refs"]) == allrefs
          and set(d.get("no_courtyard_footprints") or []) == allrefs,
          f"no-courtyard footprints are warned BY NAME (finding C) "
          f"({d.get('no_courtyard_footprints')})")


def test_pad_clearance():
    """optimize_region enforces pad-to-pad COPPER clearance per net class
    (finding §2/§5): the project's HV creepage number reaches the placer, the
    whole-board write-time verify runs and is clean, and --no-pad-clearance
    disables it. gain_stage carries only a Default class, so this owns the
    region-level wiring for a real class-specific clearance."""
    print("=== pad-copper clearance through optimize_region (finding §2/§5) ===")
    import json as _json
    from place import pad_clearance_report, pad_world_corners
    src = os.path.join(SYN_DIR, "padclr", "p.kicad_pcb")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    # 4 movable SMD parts chained N0-N1-N2-N3-N4 so HPWL pulls them together.
    fps = "".join(
        f'\t(footprint "R" (layer "F.Cu") (at {5 + 5 * i} 9)\n'
        f'\t\t(property "Reference" "R{i}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'\t\t(pad "1" smd rect (at -1 0) (size 1.5 1.5) (layers "F.Cu") '
        f'(net {i} "N{i}"))\n'
        f'\t\t(pad "2" smd rect (at 1 0) (size 1.5 1.5) (layers "F.Cu") '
        f'(net {i + 1} "N{i + 1}")))\n' for i in range(4))
    nets = "".join(f'\t(net {i} "N{i}")\n' for i in range(5))
    with open(src, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                '(44 "Edge.Cuts" user))\n' + nets +
                '\t(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") '
                '(width 0.1))\n' + fps + ")\n")
    # sibling project: an HV class on N2 at 0.6 mm (a creepage number, 3x the
    # 0.2 default) — the exact "HV nets want their big number here" of the finding.
    pro = os.path.splitext(src)[0] + ".kicad_pro"
    with open(pro, "w", encoding="utf-8") as f:
        _json.dump({"net_settings": {"classes": [
            {"name": "Default", "clearance": 0.2},
            {"name": "HV", "clearance": 0.6}],
            "netclass_assignments": {"N2": "HV"}}}, f)

    refs = ["R0", "R1", "R2", "R3"]
    fence = (1.0, 1.0, 38.0, 18.0)
    res = optimize_region(src, refs, fence, k=3, sweeps=60, seed=0,
                          out_dir=os.path.join(OUT_DIR, "padclr"))
    d = res.diagnostics
    check(d["pad_clearance"]["enabled"] is True
          and abs(d["pad_clearance"]["max_mm"] - 0.6) < 1e-9,
          f"the project's HV 0.6 mm clearance reaches placement, not the 0.2 "
          f"default (max_mm={d['pad_clearance']['max_mm']})")
    pv = d.get("placement_verify") or {}
    check(pv.get("pad_clearance_checked") is True and pv.get("clean") is True,
          "the whole-board write-time verify runs and every shipped candidate is "
          "pad-clean (finding §5)")
    check(bool(res.candidates),
          f"parts still place with pad clearance on ({len(res.candidates)})")
    # independent whole-board scan: no different-net pad on any candidate sits
    # within the resolved clearance (0.6 for N2 pairs, 0.2 elsewhere).
    clr_by_name = {"N0": 0.2, "N1": 0.2, "N2": 0.6, "N3": 0.2, "N4": 0.2, "": 0.2}
    want = set(refs)
    shorts = 0
    for c in res.candidates:
        cb = load_board(c.board_copy)
        rop = _pad_owner_refs(c.board_copy, cb)
        pads = [(rop[i], p.net_name, frozenset(p.layers), pad_world_corners(p))
                for i, p in enumerate(cb.pads) if p.layers]
        shorts += sum(1 for v in pad_clearance_report(pads, clr_by_name, 0.2)
                      if v[0] in want or v[2] in want)
    check(shorts == 0,
          f"independent whole-board scan finds no different-net pad short in any "
          f"shipped candidate at the resolved clearances ({shorts})")

    # non-vacuous: the same output boards DO carry different-net pads within an
    # absurd 5 mm clearance — so 'clean at 0.6' is a real threshold, not parts
    # that happen to be miles apart.
    tight = 0
    for c in res.candidates:
        cb = load_board(c.board_copy)
        rop = _pad_owner_refs(c.board_copy, cb)
        pads = [(rop[i], p.net_name, frozenset(p.layers), pad_world_corners(p))
                for i, p in enumerate(cb.pads) if p.layers]
        tight += sum(1 for v in pad_clearance_report(
            pads, {n: 5.0 for n in clr_by_name}, 5.0)
            if v[0] in want or v[2] in want)
    check(tight > 0,
          "the same candidates have different-net pads within 5 mm — the 0.6 mm "
          "clean bill is a real constraint, not trivially-separated parts")

    res_off = optimize_region(src, refs, fence, k=2, sweeps=40, seed=0,
                              pad_clearance=False,
                              out_dir=os.path.join(OUT_DIR, "padclr-off"))
    d2 = res_off.diagnostics
    check(d2["pad_clearance"]["enabled"] is False
          and (d2.get("placement_verify") or {}).get(
              "pad_clearance_checked") is False,
          "--no-pad-clearance disables the check and its write-time verify "
          "(A/B measurement only)")


def test_two_sided():
    """§3 two-sided: a back part's body may share XY with a front part's (they are
    on opposite sides), so a fence too small for two SAME-side bodies fits one
    front + one back; and density/preflight count PER SIDE (the busier side),
    never front+back summed, so a solvable two-sided layout is not false-refused
    (the cardinal preflight sin). Parts carry a real 2x2 courtyard on their own
    side's CrtYd layer -> part_courtyard 2.5x2.5 = 6.25 mm2."""
    print("=== two-sided placement (§3) ===")

    def board(path, specs):                 # specs: [(ref, "F"|"B")]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fps = []
        for i, (ref, side) in enumerate(specs):
            cu = "F.Cu" if side == "F" else "B.Cu"
            cy = "F.CrtYd" if side == "F" else "B.CrtYd"
            fps.append(
                f'\t(footprint "R" (layer "{cu}") (at {2 + (i % 6) * 3} '
                f'{2 + (i // 6) * 3})\n'
                f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
                f'\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
                f'(layer "{cy}") (width 0.05))\n'
                f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "{cu}") '
                f'(net 0 "")))')
        with open(path, "w") as f:
            f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                    '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                    '(44 "Edge.Cuts" user) (45 "F.CrtYd" user) '
                    '(46 "B.CrtYd" user))\n\t(net 0 "")\n'
                    '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") '
                    '(width 0.1))\n' + "\n".join(fps) + "\n)\n")

    # CONTROL: two FRONT bodies cannot both fit a 4x4 fence (each 2.5x2.5, and
    # 2*2.5 > 4 on both axes) -> the search returns nothing.
    ctl = os.path.join(SYN_DIR, "twosided", "ctl.kicad_pcb")
    board(ctl, [("A", "F"), ("B", "F")])
    r_ctl = optimize_region(ctl, ["A", "B"], (0.0, 0.0, 4.0, 4.0), k=2,
                            sweeps=40, seed=0,
                            out_dir=os.path.join(OUT_DIR, "2s-ctl"))
    check(not r_ctl.candidates,
          "two FRONT bodies do NOT fit a 4x4 fence (each 2.5x2.5) — 0 candidates")

    # THE WIN: one FRONT + one BACK co-locate in the SAME 4x4 fence.
    win = os.path.join(SYN_DIR, "twosided", "win.kicad_pcb")
    board(win, [("A", "F"), ("B", "B")])
    r_win = optimize_region(win, ["A", "B"], (0.0, 0.0, 4.0, 4.0), k=2,
                            sweeps=40, seed=0,
                            out_dir=os.path.join(OUT_DIR, "2s-win"))
    check(bool(r_win.candidates),
          "one FRONT + one BACK body co-locate in that same 4x4 fence (§3 — the "
          "'free the front' move) where two same-side bodies could not")
    dw = r_win.diagnostics["density"]
    check(dw.get("two_sided") is True and abs(dw["front_mm2"] - 6.25) < 0.1
          and abs(dw["back_mm2"] - 6.25) < 0.1,
          f"density breaks out per side (F {dw.get('front_mm2')} / "
          f"B {dw.get('back_mm2')} mm2)")
    ts = r_win.diagnostics.get("two_sided") or {}
    check(ts.get("back_parts") == ["B"]
          and ts.get("z_clearance_verified") is False,
          "back-side parts are NAMED and z-clearance is flagged UNVERIFIED — the "
          "run does not silently bless a component that may not fit the chassis")

    # PER-SIDE, NOT SUMMED: 10 front + 10 back — sum is 125% of a 100 mm2 fence
    # (would be 'impossible' if summed) but the busier side is 62.5%, so it reads
    # TIGHT and preflight does NOT refuse it.
    big = os.path.join(SYN_DIR, "twosided", "big.kicad_pcb")
    board(big, [(f"F{i}", "F") for i in range(10)]
          + [(f"B{i}", "B") for i in range(10)])
    r_big = optimize_region(
        big, [f"F{i}" for i in range(10)] + [f"B{i}" for i in range(10)],
        (0.0, 0.0, 10.0, 10.0), k=1, sweeps=10, seed=0,
        out_dir=os.path.join(OUT_DIR, "2s-big"))
    db = r_big.diagnostics["density"]
    check(db["verdict"] == "tight" and abs(db["utilization"] - 0.625) < 0.02,
          f"10 front + 10 back (sum 125%, busier side 62.5%) reads TIGHT, not "
          f"impossible — per-side, not summed ({db['utilization']:.0%})")
    check(r_big.diagnostics["preflight"] == [],
          "preflight does NOT declare the two-sided layout impossible — summing "
          "front+back would have (the cardinal false-negative, avoided)")


def test_z_clearance():
    """§3 z-clearance enforcement (Land C): component height vs the enclosure's
    per-side gap. A back part TALLER than the back clearance is refused (state-
    independent, folded into preflight -> zero candidates + a fix); an UNKNOWN
    height on a limited side is FLAGGED, never silently blocked or passed. The
    per-side gap is read from the board-info box, overridable by --z-back."""
    print("=== z-clearance enforcement (finding §3, Land C) ===")
    src = os.path.join(SYN_DIR, "zclear", "z.kicad_pcb")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    # C1: back electrolytic stating height=16mm; U1: back part of unknown height;
    # R1: front 0603. Board-info box: front 30 mm, back 3 mm.
    with open(src, "w") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user) '
            '(45 "F.CrtYd" user) (46 "B.CrtYd" user))\n\t(net 0 "")\n'
            '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.1))\n'
            '\t(gr_text "max component height front: 30mm\\n'
            'max component height back: 3mm\\nlayout: left to right" '
            '(at 20 38) (layer "Cmts.User"))\n'
            '\t(footprint "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm" (layer "B.Cu") '
            '(at 8 8)\n\t\t(descr "CP, Radial, diameter=10mm, height=16mm, Elec")\n'
            '\t\t(property "Reference" "C1" (at 0 0 0) (layer "B.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            '(layer "B.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) '
            '(layers "*.Cu") (net 0 "")))\n'
            '\t(footprint "weird:MysteryModule" (layer "B.Cu") (at 20 8)\n'
            '\t\t(property "Reference" "U1" (at 0 0 0) (layer "B.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            '(layer "B.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net 0 "")))\n'
            '\t(footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 32 8)\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            '(layer "F.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n)\n')

    refs = ["C1", "U1", "R1"]
    fence = (0.0, 0.0, 40.0, 40.0)
    # box: back 3 mm -> the 16 mm cap is too tall.
    r = optimize_region(src, refs, fence, k=1, sweeps=10, seed=0,
                        out_dir=os.path.join(OUT_DIR, "z-tall"))
    ts = r.diagnostics["two_sided"]
    check([t["ref"] for t in ts["too_tall"]] == ["C1"] and not r.candidates,
          "a 16 mm back cap under a 3 mm back clearance -> too tall, zero "
          "candidates (state-independent infeasibility, folded into preflight)")
    check("C1 is 16 mm" in (r.diagnostics.get("infeasible_reason") or "")
          and "back side has only 3 mm" in (r.diagnostics.get("infeasible_reason") or ""),
          f"infeasible_reason names the part, its height, the gap, and the fix "
          f"({(r.diagnostics.get('infeasible_reason') or '')[:70]}...)")
    check(any(v["ref"] == "U1" for v in ts["unverified"]),
          "the UNKNOWN-height back part U1 is FLAGGED unverified — neither "
          "silently blocked nor silently passed")
    check(ts["z_clearance_source"] == "board-info box"
          and ts["z_clearance_mm"]["B"] == 3.0
          and ts["layout_direction"] == "left to right",
          f"z-clearance and layout came from the board-info box ({ts['z_clearance_mm']})")

    # --z-back 20 overrides the box: now the 16 mm cap fits, run proceeds.
    r2 = optimize_region(src, refs, fence, k=1, sweeps=15, seed=0, z_back_mm=20.0,
                         out_dir=os.path.join(OUT_DIR, "z-ok"))
    ts2 = r2.diagnostics["two_sided"]
    check(not ts2["too_tall"] and "C1" in ts2["fits"]
          and ts2["z_clearance_source"] == "call params",
          "--z-back 20 (overriding the box) lets the 16 mm cap fit and is "
          "reported as the source")
    check(bool(r2.candidates) and any(v["ref"] == "U1" for v in ts2["unverified"]),
          "the run still PLACES — an unknown-height part is flagged, never blocks "
          "the search (a flag is not a false negative)")

    # PARTIAL LIMIT (adversarial review): a FRONT-only limit must NOT bless the
    # unchecked BACK parts. Uses a NO-BOX board so --z-front is the only limit;
    # the back parts are on an unlimited side -> unchecked -> NOT verified.
    nobox = os.path.join(SYN_DIR, "zclear", "nobox.kicad_pcb")
    with open(nobox, "w") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user) '
            '(45 "F.CrtYd" user) (46 "B.CrtYd" user))\n\t(net 0 "")\n'
            '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.1))\n'
            '\t(footprint "weird:MysteryModule" (layer "B.Cu") (at 10 10)\n'
            '\t\t(property "Reference" "U1" (at 0 0 0) (layer "B.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            '(layer "B.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "B.Cu") (net 0 "")))\n'
            '\t(footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 30 10)\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1) (xy -1 1)) '
            '(layer "F.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n)\n')
    r3 = optimize_region(nobox, ["U1", "R1"], fence, k=1, sweeps=10, seed=0,
                         z_front_mm=30.0, out_dir=os.path.join(OUT_DIR, "z-partial"))
    ts3 = r3.diagnostics["two_sided"]
    back_unchecked = {v["ref"] for v in ts3.get("unchecked", []) if v["side"] == "B"}
    check(ts3["z_clearance_verified"] is False
          and "U1" in back_unchecked and not ts3["too_tall"],
          "a FRONT-only z limit does NOT verify the BACK — the back part is named "
          "'unchecked' and z_clearance_verified stays False (no silent blessing)")


# ── driver ───────────────────────────────────────────────────────────────────

def test_respect_positions():
    print("=== respect_positions: rigid group translate, not scatter ===")
    from region import seed_placement
    from place import parts_from_board, part_courtyard
    src = os.path.join(SYN_DIR, "respect", "seed.kicad_pcb")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    # A 2x2 shelf-pack in a staging strip at x 52..58, y 10..20 — ENTIRELY
    # outside the fence (0,0,40,40). Each part on its own net (no HPWL pull).
    pos = [("A", 52.0, 10.0), ("B", 58.0, 10.0),
           ("C", 52.0, 20.0), ("D", 58.0, 20.0)]
    fps = "\n".join(
        f'\t(footprint "R" (layer "F.Cu")\n\t\t(at {x} {y})\n'
        f'\t\t(property "Reference" "{r}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'\t\t(pad "1" smd rect (at 0 0) (size 3 3) (layers "F.Cu") '
        f'(net {i + 1} "{r}"))\n\t)' for i, (r, x, y) in enumerate(pos))
    with open(src, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
                '(44 "Edge.Cuts" user))\n'
                + "".join(f'\t(net {i} "{n}")\n'
                          for i, n in enumerate(["", "A", "B", "C", "D"]))
                + '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") '
                '(width 0.1))\n' + fps + "\n)\n")
    bp = parts_from_board(src)
    parts = [bp[r] for r, _x, _y in pos]
    region = (0.0, 0.0, 40.0, 40.0)
    inp = {p.ref: (p.x_mm, p.y_mm) for p in parts}

    def off(a, b, d):
        return (round(d[b][0] - d[a][0], 3), round(d[b][1] - d[a][1], 3))

    sc, _ = seed_placement(parts, region, [], [], 0.5, respect_positions=False)
    sco = {p.ref: (p.x_mm, p.y_mm) for p in sc}
    check(off("A", "B", inp) != off("A", "B", sco)
          or off("A", "C", inp) != off("A", "C", sco),
          "default scatter breaks the relative arrangement (the bug being fixed)")

    rp, _ = seed_placement(parts, region, [], [], 0.5, respect_positions=True)
    rpo = {p.ref: (p.x_mm, p.y_mm) for p in rp}
    preserved = all(off(a, b, inp) == off(a, b, rpo)
                    for a, b in (("A", "B"), ("A", "C"), ("A", "D")))
    inside = all(part_courtyard(p)[0] >= -1e-6 and part_courtyard(p)[2] <= 40 + 1e-6
                 and part_courtyard(p)[1] >= -1e-6 and part_courtyard(p)[3] <= 40 + 1e-6
                 for p in rp)
    check(preserved, "respect_positions PRESERVES every relative offset")
    check(inside, "respect_positions brings the whole arrangement inside the fence")

    # a group ALREADY inside the fence is left exactly where it is
    from region import _translate
    inf_parts = [_translate(p, p.x_mm - 40.0, p.y_mm) for p in parts]  # strip -> fence
    inf = {p.ref: (p.x_mm, p.y_mm) for p in inf_parts}
    kept, moves = seed_placement(inf_parts, region, [], [], 0.5,
                                 respect_positions=True)
    kepto = {p.ref: (round(p.x_mm, 3), round(p.y_mm, 3)) for p in kept}
    check(kepto == {r: (round(v[0], 3), round(v[1], 3)) for r, v in inf.items()}
          and not moves,
          "an in-fence arrangement is not moved at all under respect_positions")

    # MIXED group (the review's blocking bug): a part already inside the fence
    # must NOT be ejected when an off-fence part dominates the group bbox.
    a_in = _translate(bp["A"], 20.0, 20.0)   # inside the (0,0,40,40) fence
    b_off = bp["B"]                          # still at x=58 -> off-fence
    mp, mmoves = seed_placement([a_in, b_off], region, [], [], 0.5,
                                respect_positions=True)
    mo = {p.ref: (round(p.x_mm, 3), round(p.y_mm, 3)) for p in mp}
    b_ct = part_courtyard(next(p for p in mp if p.ref == "B"))
    check(mo["A"] == (20.0, 20.0)
          and all(m["ref"] == "B" for m in mmoves)
          and 0 <= b_ct[0] and b_ct[2] <= 40,
          "mixed group: the in-fence part stays put; only the off-fence part "
          "is translated in")

    # INTEGRATION: the flag must actually REACH seed_placement through
    # optimize_region (a hardcoded respect_positions=False there would be a
    # dead flag with a green unit suite). Spy on seed_placement, abort before
    # the router runs — CPU-only, no GPU.
    import region as _rg

    class _StopSeed(Exception):
        pass
    seen = {}
    orig = _rg.seed_placement

    def _spy(*a, **kw):
        seen["respect"] = kw.get("respect_positions")
        raise _StopSeed
    _rg.seed_placement = _spy
    try:
        _rg.optimize_region(src, ["A"], region,
                            out_dir=os.path.join(OUT_DIR, "rp_wire"),
                            respect_positions=True)
    except _StopSeed:
        pass
    finally:
        _rg.seed_placement = orig
    check(seen.get("respect") is True,
          "optimize_region forwards respect_positions through to seed_placement")


TESTS = [("terminal", test_terminal_propagation), ("rank", test_ranking),
         ("determinism", test_determinism), ("frozen", test_frozen_parts),
         ("strip", test_strip), ("errors", test_errors),
         ("locked_flag", test_locked_flag), ("area_fence", test_area_fence),
         ("auto_fix", test_auto_fix_locked), ("pile", test_offboard_pile),
         ("pile_explicit", test_pile_explicit_required),
         ("pile_report", test_pile_report_vs_named_scatter),
         ("list_courtyards", test_list_courtyards),
         ("hole_keepouts", test_hole_keepouts),
         ("pad_clearance", test_pad_clearance),
         ("two_sided", test_two_sided),
         ("z_clearance", test_z_clearance),
         ("density", test_density_preflight),
         ("preflight", test_preflight), ("warnings", test_geometry_warnings),
         ("respect_positions", test_respect_positions),
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
