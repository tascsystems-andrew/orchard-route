"""Component-height sourcing for the two-sided z-clearance check (§3, Land C).

Two layers, both tested here:
- board.py PARSES a height the footprint already states — an explicit height
  property, or a "height=Nmm" in the descr/tags — and refuses the "l*w*h=a*b*c"
  product form rather than misreading a length as a height;
- heights.py RESOLVES an effective height per part by priority: a designer
  override (by ref, then fpid), the parsed footprint height, a conservative
  family upper bound, else None (UNKNOWN — flagged, never assumed to fit).

Run: .venv/bin/python test_heights.py
"""
import os
import tempfile
import shutil

import board as B
import heights as H
from place import parts_from_board

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def _write(path):
    """A board exercising every height source: an electrolytic that states
    height in its descr, a power resistor with the l*w*h PRODUCT form (must NOT
    parse), a part with an explicit MAXIMUM_PACKAGE_HEIGHT property, an 0603 SMD
    (no stated height -> family upper bound), and a custom fp with nothing."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            '(kicad_pcb (version 20240108) (generator "t")\n'
            '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
            '\t(net 0 "")\n'
            '\t(gr_rect (start 0 0) (end 60 40) (layer "Edge.Cuts") (width 0.1))\n'
            '\t(footprint "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm" (layer "F.Cu") (at 8 8)\n'
            '\t\t(descr "CP, Radial, diameter=10mm, height=16mm, Electrolytic")\n'
            '\t\t(property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 "")))\n'
            '\t(footprint "Resistor_THT:R_Axial_Power" (layer "F.Cu") (at 20 8)\n'
            '\t\t(descr "Resistor, Box, 4W, length*width*height=20*6.4*6.4mm^3")\n'
            '\t\t(property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" thru_hole circle (at 0 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 0 "")))\n'
            '\t(footprint "Display:LCD_custom" (layer "F.Cu") (at 35 8)\n'
            '\t\t(property "Reference" "DS1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(property "MAXIMUM_PACKAGE_HEIGHT" "5.5mm" (at 0 0 0) (layer "F.Fab"))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n'
            '\t(footprint "Resistor_SMD:R_0603_1608Metric" (layer "F.Cu") (at 48 8)\n'
            '\t\t(property "Reference" "R2" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n'
            '\t(footprint "weird:MysteryModule" (layer "F.Cu") (at 8 30)\n'
            '\t\t(property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))\n'
            '\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 "")))\n'
            ')\n')


if __name__ == "__main__":
    print("=== board.py parses stated heights, refuses the l*w*h product ===")
    check(B._parse_height_text("diameter=10mm, height=16mm, Electrolytic") == 16.0,
          "a plain 'height=16mm' parses to 16.0")
    check(B._parse_height_text("length*width*height=20*6.4*6.4mm^3") is None,
          "the 'l*w*h=a*b*c' PRODUCT form does NOT parse (would misread length "
          "as height) — left unknown, not mis-measured")
    check(B._parse_height_text("height: 3 mm") == 3.0
          and B._parse_height_text("height 12mm") == 12.0,
          "'height: 3 mm' and 'height 12mm' both parse")
    check(B._parse_height_text("no dimensions here") is None,
          "text with no height yields None")

    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "h.kicad_pcb")
        _write(p)
        brd = B.load_board(p)
        check(len(brd.footprint_heights) == 5 and len(brd.footprint_fpids) == 5,
              f"footprint_heights/fpids parsed per footprint "
              f"({brd.footprint_heights})")
        parts = parts_from_board(p)
        check(parts["C1"].height_mm == 16.0
              and parts["C1"].fpid == "Capacitor_THT:CP_Radial_D10.0mm_P5.00mm",
              f"C1 electrolytic: height 16 from descr, fpid attached "
              f"({parts['C1'].height_mm})")
        check(parts["R1"].height_mm is None,
              "R1 power resistor: the product-form descr leaves height None (not "
              "the 20mm length)")
        check(parts["DS1"].height_mm == 5.5,
              f"DS1: MAXIMUM_PACKAGE_HEIGHT property read as 5.5 "
              f"({parts['DS1'].height_mm})")
        check(parts["R2"].height_mm is None and parts["U1"].height_mm is None,
              "R2 (0603) and U1 (custom) state no height in the PCB (None) — the "
              "family bound / flag is heights.resolve's job, not the parser's")

        print("=== heights.resolve: override > footprint > family-max > None ===")
        h, src = H.resolve("R2", parts["R2"].fpid, parts["R2"].height_mm)
        check(src == "family-max" and 0.4 <= h <= 1.0,
              f"an 0603 with no stated height gets a conservative family upper "
              f"bound ({h} mm, {src})")
        h, src = H.resolve("C1", parts["C1"].fpid, parts["C1"].height_mm)
        check(src == "footprint" and h == 16.0,
              f"a footprint-stated height wins over the family bound ({h}, {src})")
        h, src = H.resolve("U1", parts["U1"].fpid, parts["U1"].height_mm)
        check(h is None and src == "unknown",
              "a part with no stated height and no known family is UNKNOWN "
              "(flagged, never assumed)")
        ov = {"Resistor_SMD:R_0603_1608Metric": 0.45, "U1": 9.0}
        h, src = H.resolve("R2", parts["R2"].fpid, parts["R2"].height_mm, ov)
        check(h == 0.45 and src == "override:fpid",
              f"an fpid override wins over the family bound ({h}, {src})")
        h, src = H.resolve("U1", parts["U1"].fpid, parts["U1"].height_mm, ov)
        check(h == 9.0 and src == "override:ref",
              f"a ref override supplies an otherwise-UNKNOWN height ({h}, {src})")
        ov2 = {"Capacitor_THT:CP_Radial_D10.0mm_P5.00mm": 20.0, "C1": 12.0}
        h, src = H.resolve("C1", parts["C1"].fpid, parts["C1"].height_mm, ov2)
        check(h == 12.0 and src == "override:ref",
              f"a ref override beats an fpid override for the same part "
              f"({h}, {src})")

        print("=== load_overrides drops junk, keeps positive numbers ===")
        ovp = os.path.join(d, "ov.json")
        with open(ovp, "w") as f:
            f.write('{"C1": 12.5, "R2": "tall", "U1": -3, "X": 0, "Q1": 4}')
        loaded = H.load_overrides(ovp)
        check(loaded == {"C1": 12.5, "Q1": 4.0},
              f"only positive numeric overrides survive ({loaded})")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
