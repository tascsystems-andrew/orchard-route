"""Rotation-safety: the placer can and should rotate freely (task #35).

The design thread keeps avoiding part rotation for fear of "the footprint
issue" — a real corruption seen in feedback/voxy-placement-2026-07-19 where a
HAND sexpr edit rotated a footprint's (at ...) without adding the same delta to
every pad's (at ... angle), so pad shapes rendered 90 deg off while centres
stayed put. reference_rotation_is_safe (memory): the TOOL never does this — the
writer always rewrites each pad's absolute angle. This test retires the fear
with END-TO-END evidence on a real THT/relay-style footprint (spread thru-hole
pads + a CUSTOM heat-tab pad + an asymmetric F.CrtYd), proving:

1. THE PLACER ROTATES TO FIT. A part whose 30 mm body cannot fit a 12 mm-wide
   fence horizontally IS placed — rotated to 90/270 so its long axis runs down
   the 40 mm-tall fence (the finding's C9 27 mm-film / R175 28 mm-resistor case:
   "un-placeable un-rotated because the free space is fragmented into sub-28 mm
   gaps — they fit vertical"). Rotation is in the search space and gets used.
2. THE WRITE PATH IS CLEAN. write_moved_copy at 90/180/270 delta-transforms
   every pad's position AND absolute angle (re-derived here, independent of the
   writer), INCLUDING the custom pad — so its heat-tab primitive, which lives in
   the pad-local frame, rotates rigidly with it (proven on the written text).
3. NO SHORTS ARE INTRODUCED BY ROTATION. pad_clearance_report (finding §2) over
   the rotated+written board finds no different-net pad short — the pads turned
   with the part, they did not shear into a neighbour. "If it looks good it is
   good", made a measurement.
4. THE COURTYARD TURNS WITH THE PART. An asymmetric F.CrtYd swaps w/h at 90 deg
   (the body keep-out is not corrupted by rotation either).

Run: .venv/bin/python test_rotation.py
"""
import math
import os
import shutil
import tempfile

from board import load_board
from place import (Part, PlacementModel, anneal_region, parts_from_board,
                   part_courtyard, pad_clearance_report, pad_world_corners)
from writeback import board_footprints, write_moved_copy

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def _rot(x, y, deg):
    """KiCad rotation (CCW, Y-down) — local math, independent of place.py, so a
    shared bug in the code under test cannot hide here."""
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c + y * s, -x * s + y * c


def write_relay_board(path):
    """A board with a relay-style THT footprint K1 and an SMD neighbour R1.

    K1: two thru_hole pads 26 mm apart on the long axis (net A, net B), one
    CUSTOM smd pad carrying a heat-tab primitive (net TAB), and a 30 x 6 mm
    F.CrtYd — the body overhangs the pad span, the exact THT case the field
    report cared about. Placed at (30, 30, 0). R1 is a 2-pad SMD part on nets
    B and C near the top-left, so a careless rotation of K1 could short into it.
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "test_rotation")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) '
            '(44 "Edge.Cuts" user) (45 "F.CrtYd" user))\n'
            '\t(net 0 "") (net 1 "A") (net 2 "B") (net 3 "C") (net 4 "TAB")\n'
            '\t(gr_rect (start 0 0) (end 60 60) (layer "Edge.Cuts") (width 0.1))\n'
            # --- K1: THT relay with a custom heat-tab pad + asymmetric courtyard
            '\t(footprint "Relay" (layer "F.Cu")\n\t\t(at 30 30 0)\n'
            '\t\t(property "Reference" "K1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(fp_poly (pts (xy -15 -3) (xy 15 -3) (xy 15 3) (xy -15 3)) '
            '(layer "F.CrtYd") (width 0.05))\n'
            '\t\t(pad "1" thru_hole circle (at -13 0 0) (size 2 2) (drill 1) '
            '(layers "*.Cu") (net 1 "A"))\n'
            '\t\t(pad "2" thru_hole circle (at 13 0 0) (size 2 2) (drill 1) '
            '(layers "*.Cu") (net 2 "B"))\n'
            '\t\t(pad "3" smd custom (at 0 2 0) (size 1 1) (layers "F.Cu")\n'
            '\t\t\t(options (clearance outline) (anchor rect))\n'
            '\t\t\t(primitives (gr_poly (pts (xy -3 0) (xy 3 0) (xy 3 2) '
            '(xy -3 2)) (width 0)))\n'
            '\t\t\t(net 4 "TAB"))\n\t)\n'
            # --- R1: SMD neighbour, nets B and C
            '\t(footprint "R_1206" (layer "F.Cu")\n\t\t(at 8 8 0)\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") '
            '(net 2 "B"))\n'
            '\t\t(pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") '
            '(net 3 "C"))\n\t)\n)\n')


if __name__ == "__main__":
    work = tempfile.mkdtemp()
    try:
        srcdir = os.path.join(work, "src")
        outdir = os.path.join(work, "out")
        os.makedirs(srcdir)
        os.makedirs(outdir)
        src = os.path.join(srcdir, "relay.kicad_pcb")
        write_relay_board(src)
        parts = parts_from_board(src)
        k1 = parts["K1"]

        print("=== the footprint the fear is about parses as expected ===")
        check(len(k1.pads) == 3 and any(p.through_hole for p in k1.pads),
              f"K1 has 3 pads incl. thru-hole ({len(k1.pads)})")
        cy0 = part_courtyard(k1)          # 30 x 6 courtyard + 2*0.25 margin
        check(abs((cy0[2] - cy0[0]) - 30.5) < 1e-6
              and abs((cy0[3] - cy0[1]) - 6.5) < 1e-6,
              f"K1 courtyard is the 30.5 x 6.5 mm body+margin at home "
              f"({cy0[2] - cy0[0]:.2f} x {cy0[3] - cy0[1]:.2f})")

        print("=== (1) the placer ROTATES a too-long part to fit the fence ===")
        # Fence 12 wide x 40 tall: K1's 30 mm body cannot fit horizontally
        # (30.5 > 12) but fits vertically (30.5 < 40). Only a 90/270 placement
        # is feasible, so the anneal MUST rotate it — proof rotation is used.
        fence = (24.0, 10.0, 12.0, 40.0)
        res = anneal_region([k1], fence, seed=0, sweeps=120)
        check(bool(res.elites), f"a feasible placement exists — found rotated "
                                f"({len(res.elites)} elite(s))")
        rots = {el.placements["K1"][2] % 180.0 for el in res.elites}
        check(res.elites and rots == {90.0},
              f"EVERY elite rotates K1 to 90/270 (the only way its 30 mm body "
              f"fits a 12 mm-wide fence) — the placer rotates to fit ({sorted({el.placements['K1'][2] for el in res.elites})})")
        # and the horizontal placement really is infeasible (control):
        m = PlacementModel([k1], fence)
        flat = m.judge([(30.0, 30.0, 0.0)])[0]
        vert = m.judge([(30.0, 30.0, 90.0)])[0]
        check(not flat and vert,
              "un-rotated K1 does NOT fit the fence; rotated 90 it does "
              "(the finding's un-placeable-un-rotated case, made concrete)")

        print("=== (2) write_moved_copy delta-transforms pads AND the custom "
              "pad, at every rotation ===")
        recs = board_footprints(open(src).read())
        for delta in (90.0, 180.0, 270.0):
            out = os.path.join(outdir, f"rot{int(delta)}.kicad_pcb")
            # rotate K1 in place (keep centre), leave R1 alone
            placements = {"K1": (30.0, 30.0, delta), "R1": (8.0, 8.0, 0.0)}
            write_moved_copy(src, out, placements)
            mb = load_board(out)
            mk = parts_from_board(out)["K1"]
            # each pad's world position and absolute angle = the delta-transform
            # of the original, recomputed here from first principles.
            bad = []
            for po, pn in zip(k1.pads, mk.pads):
                lx, ly = _rot(po.x_mm - 30.0, po.y_mm - 30.0, 0.0)   # home rot 0
                wx, wy = _rot(lx, ly, delta)
                exp_x, exp_y = 30.0 + wx, 30.0 + wy
                exp_a = (po.rotation_deg + delta) % 360.0
                if (abs(pn.x_mm - exp_x) > 1e-4 or abs(pn.y_mm - exp_y) > 1e-4
                        or abs((pn.rotation_deg - exp_a) % 360.0) > 1e-4):
                    bad.append((pn.x_mm, pn.y_mm, pn.rotation_deg,
                                exp_x, exp_y, exp_a))
            check(not bad,
                  f"rot {int(delta)}: all 3 K1 pads (incl. the custom heat-tab "
                  f"pad) delta-transform to 1e-4 — pad copper follows the part, "
                  f"no shear ({bad[:1]})")
            # the custom pad's angle in the WRITTEN TEXT rotated by delta, so its
            # heat-tab primitive (pad-local) turns rigidly with it.
            txt = open(out).read()
            import re
            m3 = re.search(r'\(pad "3"[^\n]*\(at [\d.\-]+ [\d.\-]+ ([\d.\-]+)\)',
                           txt)
            wrote_angle = float(m3.group(1)) if m3 else None
            check(wrote_angle is not None
                  and abs((wrote_angle - delta) % 360.0) < 1e-4,
                  f"rot {int(delta)}: the custom pad's written angle is {delta} "
                  f"(its heat-tab primitive rotates with the pad) — got "
                  f"{wrote_angle}")

            print(f"    -- (3) no NEW pad short after rotating K1 to "
                  f"{int(delta)} --")
            clr = {"A": 0.2, "B": 0.2, "C": 0.2, "TAB": 0.2, "": 0.2}
            from region import _pad_owner_refs
            rop = _pad_owner_refs(out, mb)
            pads = [(rop[i], p.net_name, frozenset(p.layers), pad_world_corners(p))
                    for i, p in enumerate(mb.pads) if p.layers]
            rep = pad_clearance_report(pads, clr, 0.2)
            check(rep == [],
                  f"rot {int(delta)}: pad_clearance_report finds NO different-net "
                  f"short — rotation did not push K1's copper into R1 ({rep[:1]})")

            print(f"    -- (4) courtyard turns with the part at {int(delta)} --")
            cyr = part_courtyard(mk)
            if delta in (90.0, 270.0):
                ok_ct = (abs((cyr[2] - cyr[0]) - 6.5) < 1e-6
                         and abs((cyr[3] - cyr[1]) - 30.5) < 1e-6)
            else:
                ok_ct = (abs((cyr[2] - cyr[0]) - 30.5) < 1e-6
                         and abs((cyr[3] - cyr[1]) - 6.5) < 1e-6)
            check(ok_ct,
                  f"rot {int(delta)}: courtyard is "
                  f"{cyr[2] - cyr[0]:.1f} x {cyr[3] - cyr[1]:.1f} — the body "
                  f"keep-out rotated with the part, not corrupted")

        print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
              f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    raise SystemExit(1 if failures else 0)
