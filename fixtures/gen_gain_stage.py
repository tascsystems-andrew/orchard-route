"""Generator for gain_stage.kicad_pcb — the region solver's committed fixture.

The tests read the committed gain_stage.kicad_pcb, NOT this script; this file
is the documented source of those bytes. Regenerate with:

    python fixtures/gen_gain_stage.py

A small, self-consistent 12AX7 triode-1 gain stage (13 movable parts in a
13 x 52 mm strip) that optimize_region can place AND route, replacing the old
read of Andrew's live, mid-redesign Voxy board. Every number the tests assert
is engineered here on purpose:

- PREFLIGHT floor (test_preflight). adjacency_max_distance is centre-to-centre,
  so it has a geometric floor: the closest two parts' CENTRES can come without
  their courtyards overlapping. G1T1 (the "socket", fixed) has a CENTRED square
  courtyard, half-extent 0.85 mm, so it looks the same at every rotation.
  GstopT1's courtyard is OFF-CENTRE: its origin sits 1.45 mm from the near edge
  and 4.5 mm from the far one. For a square-vs-rect the floor is
  0.85 + min(one-sided extent of GstopT1) = 0.85 + 1.45 = 2.30 mm, and because
  the four extents only permute under rotation that 2.30 holds at every angle.
  A lazy "assume the courtyard is centred on the origin" would take the x
  half-width as (1.45 + 4.5)/2 = 2.975 mm and compute a 3.82 mm floor, wrongly
  rejecting the spec's own 3 mm (and even 2.3 mm) constraint — the exact bug
  the preflight test guards.

- GEOMETRY WARNINGS (test_geometry_warnings). Q2 (movable) and Q3 (a frozen
  transistor whose courtyard intrudes the fence's left edge) are the ONLY two
  footprints with a `custom` pad — an SOT-89 heat tab drawn as a primitive that
  is bigger than board.py's anchor rect. custom_pad_refs must find exactly
  {Q2, Q3}, and geometry_warnings (custom_pad_refs & (movable | obstacles))
  must be exactly {Q2, Q3}.

- ACCEPTANCE (test_acceptance). The home layout is a loose, non-overlapping,
  already-routable spread: G1T1 sits in an open corner with GstopT1 nestled at
  its grid pin (centre distance 2.5 mm, so fixed(G1T1) + adjacency(3) are
  pre-satisfied) and R55 far away (min_distance(R55,G1T1,4) pre-satisfied).
  Four nets (GRIDIN, PLATEOUT, VPLUS, GND-C) reach pads on furniture OUTSIDE
  the fence, so they cross it and become boundary terminals. optimize_region
  ships 5 fully-routed, violation-free candidates in a couple of seconds.
"""
import os

# ── net table (legacy root-table style, mirrors write_synth) ─────────────────
NETS = [
    "",             # 0 unconnected
    "GRIDIN",       # 1 grid input (crosses fence -> boundary)
    "PLATE",        # 2 plate node hub
    "PLATEOUT",     # 3 plate output (crosses fence -> boundary)
    "GSTOPLED",     # 4 grid-stopper LED node
    "CCSREF",       # 5 current-source reference
    "VPLUS",        # 6 B+ supply (crosses fence -> boundary)
    "VSENSE",       # 7 plate-voltage sense
    "GND-C",        # 8 ground (crosses fence -> boundary)
    "Q3A", "Q3B",   # 9,10 Q3 private nets (never touch a movable part)
]
CODE = {n: i for i, n in enumerate(NETS)}


def _pad(num, ox, oy, w, h, net, custom=False):
    shape = "custom" if custom else "rect"
    prim = ""
    if custom:
        # a heat-tab primitive bigger than the anchor rect: invisible to
        # board.py (reads the anchor (size ...) only) -> a geometry warning.
        prim = (" (primitives (gr_poly (pts (xy -1.55 -0.85) (xy 1.55 -0.85) "
                "(xy 1.55 0.85) (xy -1.55 0.85)) (width 0)))")
    return (f'\t\t(pad "{num}" smd {shape} (at {ox} {oy}) (size {w} {h}) '
            f'(layers "F.Cu") (net {CODE[net]} "{net}"){prim})')


def fp(ref, x, y, pads, rot=0, locked=False, lib="R_1206"):
    """One SMD footprint. pads: list of (num, ox, oy, w, h, net[, custom])."""
    if locked:
        body = [f'\t(footprint "{lib}"', '\t\t(locked yes)', '\t\t(layer "F.Cu")']
    else:
        body = [f'\t(footprint "{lib}" (layer "F.Cu")']
    body.append(f'\t\t(at {x} {y})' if not rot else f'\t\t(at {x} {y} {rot})')
    body.append(f'\t\t(property "Reference" "{ref}" (at 0 0 0) '
                f'(layer "F.SilkS"))')
    for p in pads:
        body.append(_pad(*p))
    body.append("\t)")
    return "\n".join(body)


# ── the 13 movable gain-stage parts (home = a loose, routable spread) ────────
#
# G1T1  : the "socket" (fixed). Centred-square courtyard, half-extent 0.85 mm
#         (pad-bbox union +-0.60 mm + 0.25 margin).
# GstopT1: off-centre courtyard. Near edge 1.45 mm from origin, far edge 4.5;
#         with G1T1's 0.85 square that puts the true adjacency floor at
#         0.85 + 1.45 = 2.30 mm (preflight test).
# Q2    : SOT-89 with a `custom` heat-tab pad (geometry-warning test).

def gpad_square(ref, x, y, p1net, p2net):
    # centred 1.2 x 1.2 pad-bbox -> 1.7 x 1.7 courtyard (square, rotation
    # invariant), origin at centre.
    return fp(ref, x, y, [
        ("1", -0.4, 0.0, 0.4, 1.2, p1net),
        ("2",  0.4, 0.0, 0.4, 1.2, p2net),
    ])


def gstop(ref, x, y, p1net, p2net):
    # tall grid pad at the near edge + a side pad reaching far out.
    return fp(ref, x, y, [
        ("1", -0.35, 0.0, 1.7, 6.5, p1net),   # bbox x[-1.2,0.5] y[-3.25,3.25]
        ("2",  3.30, 0.0, 1.9, 2.0, p2net),   # bbox x[2.35,4.25] y[-1,1]
    ])


def r2(ref, x, y, p1net, p2net):
    # 1206-ish two-pad resistor, ~2.0 x 1.4 pad bbox.
    return fp(ref, x, y, [
        ("1", -0.8, 0.0, 0.9, 1.4, p1net),
        ("2",  0.8, 0.0, 0.9, 1.4, p2net),
    ])


def sot89(ref, x, y, p1net, p2net, p3net):
    # 3-pad SOT-89, pad 2 is a `custom` heat tab (bigger than its anchor rect).
    return fp(ref, x, y, [
        ("1", -1.5, 0.9, 0.85, 1.0, p1net),
        ("2",  0.0, -0.9, 1.475, 0.9, p2net, True),
        ("3",  1.5, 0.9, 0.85, 1.0, p3net),
    ], lib="SOT-89")


def build():
    parts = []
    # Home = a loose, non-overlapping, already-routable spread. G1T1 (fixed)
    # sits in an open top-left corner with GstopT1 nestled at its grid pin
    # (centre distance 2.5 mm, adjacency pre-satisfied); R55 is kept far from
    # G1T1 (min_distance pre-satisfied). Everything else spreads below.
    parts.append(("G1T1", gpad_square("G1T1", 104.0, 22.0, "GRIDIN", "PLATE")))
    parts.append(("GstopT1", gstop("GstopT1", 106.5, 22.0, "GRIDIN",
                                   "GSTOPLED")))
    parts.append(("R22", r2("R22", 114.0, 19.0, "GRIDIN", "GND-C")))
    parts.append(("PT1", r2("PT1", 112.0, 30.0, "PLATE", "PLATEOUT")))
    parts.append(("Q2", sot89("Q2", 105.0, 33.0, "PLATE", "CCSREF", "VPLUS")))
    parts.append(("CCS1", r2("CCS1", 112.0, 36.0, "CCSREF", "VPLUS")))
    parts.append(("R36", r2("R36", 117.0, 33.0, "CCSREF", "GND-C")))
    parts.append(("R29", r2("R29", 117.0, 39.0, "VPLUS", "GND-C")))
    parts.append(("R27", r2("R27", 103.0, 40.0, "GSTOPLED", "GND-C")))
    parts.append(("C10", r2("C10", 109.0, 44.0, "PLATE", "PLATEOUT")))
    parts.append(("R55", r2("R55", 110.0, 52.0, "PLATEOUT", "GND-C")))
    parts.append(("R23", r2("R23", 116.0, 48.0, "PLATE", "VSENSE")))
    parts.append(("R21", r2("R21", 116.0, 55.0, "VSENSE", "GND-C")))

    # ── non-movable furniture ────────────────────────────────────────────────
    # Q3: frozen SOT-89 whose courtyard intrudes the fence's left edge; a
    # custom heat-tab pad -> the second geometry warning. Private nets so it is
    # never a region net.
    q3 = sot89("Q3", 99.0, 30.0, "Q3A", "Q3B", "Q3A")

    # boundary furniture: pads OUTSIDE the fence sharing region nets, so those
    # nets cross the fence and get a pseudo-pad terminal.
    j1 = fp("J1", 94.0, 22.0, [("1", 0.0, 0.0, 1.0, 1.0, "GRIDIN")])
    j2 = fp("J2", 94.0, 55.0, [("1", 0.0, 0.0, 1.0, 1.0, "PLATEOUT")])
    pwr = fp("PWR", 127.0, 35.0, [
        ("1", 0.0, -2.0, 1.2, 1.2, "VPLUS"),
        ("2", 0.0, 2.0, 1.2, 1.2, "GND-C"),
    ])

    fps = [p for _r, p in parts] + [q3, j1, j2, pwr]

    # board outline: comfortably around the fence (100,15..120,65) and all
    # furniture. x[85,135] y[5,80].
    def edge(x0, y0, x1, y1):
        c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        return "\n".join(
            f'\t(gr_line (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) '
            f'(layer "Edge.Cuts") (width 0.1))'
            for a, b in zip(c, c[1:]))

    text = (
        '(kicad_pcb (version 20240108) (generator "gen_gain_stage")\n'
        '\t(general (thickness 1.6))\n'
        '\t(paper "A4")\n'
        '\t(layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        + "".join(f'\t(net {i} "{n}")\n' for i, n in enumerate(NETS))
        + edge(85, 5, 135, 80) + "\n"
        + "\n".join(fps) + "\n)\n")
    return text


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "gain_stage.kicad_pcb")
    with open(out, "w", encoding="utf-8") as f:
        f.write(build())
    print("wrote", out)
