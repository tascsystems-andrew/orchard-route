"""Generator for amp_board.kicad_pcb — the writeback/placement fixture.

The tests read the committed amp_board.kicad_pcb (+ amp_board.kicad_pro), NOT
this script; this file is the documented source of those bytes. Regenerate:

    python fixtures/gen_amp_board.py

A small amp board this test OWNS, replacing the old reads of Andrew's live hifi
board. It is shaped for test_place.py and test_writeback.py:

- THREE "5755" valves (duplicated designator -> 5755#1..#3 addressing), each a
  9-pad 3x3 grid rotated -90. Every asserted coordinate is hand-derivable:
  KiCad's -90 maps a footprint-local (ox, oy) to (fx - oy, fy + ox), so V1 at
  (20, 20, -90) puts its corner pad 1 (at -2 -2 216) at (22, 18), and a move of
  5755#1 to (120, 60, 180) (rotation delta 270) lands that pad at
  (120 + 2, 60 + 2) = (122, 62) with angle (216 + 270) % 360 = 126.
- four unrotated 2-pad resistors chained by four fully-routable nets
  (SIGA/SIGB/SIGC/GND) plus a 180-degrees single-pad test pin on GND — so
  route_board() routes the board with ZERO failed nets at pitch 1.0, and the
  three single-pad valve nets (B+250, sig_in, Net-(V3-Pad1)) are named but
  unrouted (the width-cap tests need one of each).
- the valves sit well clear of the resistor row, so their net-0 pads are pure
  obstacles and never block the route.

The sibling amp_board.kicad_pro carries a single Default net class (no
assignments/patterns), the "one Default class, no maps" shape test_writeback
resolves every net through; second_project.kicad_pro is a DIFFERENT project for
the parse-only path.
"""
import os

NETS = ["", "SIGA", "SIGB", "SIGC", "GND", "B+250", "sig_in", "Net-(V3-Pad1)"]
CODE = {n: i for i, n in enumerate(NETS)}


def pad(num, ox, oy, w, h, net, angle=None):
    at = f"(at {ox} {oy})" if angle is None else f"(at {ox} {oy} {angle})"
    return (f'\t\t(pad "{num}" smd rect {at} (size {w} {h}) '
            f'(layers "F.Cu") (net {CODE[net]} "{net}"))')


def footprint(ref, x, y, rot, pads, lib="R_1206"):
    at = f"(at {x} {y})" if not rot else f"(at {x} {y} {rot})"
    body = [f'\t(footprint "{lib}" (layer "F.Cu")',
            f'\t\t{at}',
            f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))']
    body.extend(pads)
    body.append("\t)")
    return "\n".join(body)


def valve(ref, x, y, pad1_net):
    """9-pad 3x3 grid (spacing 2 mm) rotated -90. Pad 1 is the (-2,-2) corner
    on pad1_net with an explicit 216-degrees angle (the move anchor); pads 2..9
    are net 0 obstacles."""
    grid = [(-2, -2), (0, -2), (2, -2), (-2, 0), (0, 0),
            (2, 0), (-2, 2), (0, 2), (2, 2)]
    pads = []
    for i, (ox, oy) in enumerate(grid, start=1):
        if i == 1:
            pads.append(pad("1", ox, oy, 1.0, 1.0, pad1_net, angle=216))
        else:
            pads.append(pad(str(i), ox, oy, 1.0, 1.0, ""))
    return footprint(ref, x, y, -90, pads, lib="Valve:Valve_ECC-83-2")


def res(ref, x, y, p1net, p2net):
    return footprint(ref, x, y, 0, [
        pad("1", -1.0, 0.0, 1.0, 1.5, p1net),
        pad("2", 1.0, 0.0, 1.0, 1.5, p2net),
    ])


def build():
    fps = [
        valve("5755", 20.0, 20.0, "B+250"),          # 5755#1
        valve("5755", 35.0, 20.0, "sig_in"),         # 5755#2
        valve("5755", 50.0, 20.0, "Net-(V3-Pad1)"),  # 5755#3
        res("R1", 20.0, 38.0, "SIGA", "SIGB"),
        res("R2", 30.0, 38.0, "SIGB", "SIGC"),
        res("R3", 40.0, 38.0, "SIGC", "GND"),
        res("R4", 50.0, 38.0, "GND", "SIGA"),
        # the pad angle folds in the footprint's 180 (as pcbnew writes it), so a
        # move back to rot 0 must drop the now-zero pad angle again.
        footprint("TP1", 60.0, 38.0, 180, [
            pad("1", 0.0, 0.0, 1.5, 1.5, "GND", angle=180)],
            lib="TestPoint:TestPoint_Pad_D2.0mm"),
    ]

    def edge(x0, y0, x1, y1):
        c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        return "\n".join(
            f'\t(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) '
            f'(layer "Edge.Cuts") (width 0.1))'
            for a, b in zip(c, c[1:]))

    return (
        '(kicad_pcb (version 20240108) (generator "gen_amp_board")\n'
        '\t(general (thickness 1.6))\n'
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        + "".join(f'\t(net {i} "{n}")\n' for i, n in enumerate(NETS))
        + edge(10, 10, 70, 48) + "\n"
        + "\n".join(fps) + "\n)\n")


PRO = ('{\n  "net_settings": {\n    "classes": [\n'
       '      { "name": "Default", "clearance": 0.2, "track_width": 0.2,\n'
       '        "via_diameter": 0.6, "via_drill": 0.3 }\n'
       '    ]\n  }\n}\n')

SECOND_PRO = ('{\n  "net_settings": {\n    "classes": [\n'
              '      { "name": "Default", "clearance": 0.2, "track_width": 0.25,\n'
              '        "via_diameter": 0.8, "via_drill": 0.4 }\n'
              '    ]\n  }\n}\n')


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "amp_board.kicad_pcb"), "w") as f:
        f.write(build())
    with open(os.path.join(here, "amp_board.kicad_pro"), "w") as f:
        f.write(PRO)
    with open(os.path.join(here, "second_project.kicad_pro"), "w") as f:
        f.write(SECOND_PRO)
    print("wrote amp_board.kicad_pcb, amp_board.kicad_pro, second_project.kicad_pro")
