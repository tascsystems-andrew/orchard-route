"""L1 validation: parse a committed KiCad 10 board and check geometry end to end.

The primary fixture is fixtures/parser_board.kicad_pcb — a small, self-contained
KiCad 10 board this test OWNS (it used to read Andrew's live, mid-redesign amp
projects, which broke every time a part moved). Every spot-check expectation
below was derived BY HAND from the raw file text, so it catches transform bugs
independently of the parser:

parser_board.kicad_pcb — KiCad 10 (version 20260206): NO root net table, so net
  codes are synthesized 1..K from the sorted set of quoted names. 4-layer
  stackup (F.Cu, In1.Cu, In2.Cu, B.Cu). Edge.Cuts gr_lines span x 10 .. 70,
  y -5 .. 35 -> origin (10, -5), size (60, 40). Three footprints exercise the
  cases the live boards used to:

  C1, "Capacitor_SMD:C_0805_2012Metric", (at 30 15) UNROTATED SMD;
    pad "1" smd (at -0.95 0) net "Net-(C1-Pad1)" -> abs (29.05, 15)
    pad "2" smd (at  0.95 0) net "GND"           -> abs (30.95, 15)

  V1, "Valve:Valve_ECC-83-2", (at 45 20 -90) — the ROTATED-footprint case.
    KiCad rotation is CCW with Y down, so -90 maps local (ox, oy) to
    (fx - oy, fy + ox):
    pad "1" thru_hole (at 2.0  3.0 216) (drill oval 1.02 2.03) -> (42.0, 22.0)
    pad "4" thru_hole (at 2.0 -3.0 324) (drill oval 1.02 2.03) -> (48.0, 22.0)

  Q1, "Package_TO_SOT_SMD:SOT-89-3", (at 20 10) with a CUSTOM heat-tab pad;
    pad "2" smd custom (at 0 -0.9) (size 1.475 0.9) net "HEAT" -> abs (20, 9.1),
    read as its 1.475 x 0.9 ANCHOR rect, never the larger primitive.

Two more fixtures come from bench/boards/ (gitignored, see
bench/boards/SOURCES.md — SKIPPED with a note when absent, e.g. fresh clone):

pico_vga_sd_aud.kicad_pcb — KiCad 5 (version 20171130): footprints are
  (module ...) and simple net names are unquoted, e.g. (net 72 /SWDIO).
  Spot check: module RPi_Pico:RPi_Pico_SMD_TH (at 130.81 75) unrotated;
  pad 43 thru_hole (at 2.54 23.9) (size 1.7 1.7) (drill 1.02)
  (layers *.Cu *.Mask) (net 72 /SWDIO) -> abs (133.35, 98.9).
  Edge.Cuts gr_lines span x 93.5 .. 178.5, y 49 .. 105 -> size 85.0 x 56.0.

SparkFun_IoT_RedBoard-RP2350.kicad_pcb — KiCad 8: the whole outline is
  fp_line on Edge.Cuts inside footprint "SparkFun-Board:RedBoard"
  (at 149.87 95.26) unrotated; local x -34.29 .. 34.29, y -29.21 .. 29.21
  -> size 68.58 x 58.42 mm (= 2.7 x 2.3 in, SparkFun's published dims).
"""
import os
from collections import Counter

from board import load_board

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PARSER = os.path.join(_FIXTURES, "parser_board.kicad_pcb")

_BENCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench", "boards")
PICO_VGA = os.path.join(_BENCH, "rpi-pico-vga", "pico_vga_sd_aud.kicad_pcb")
SPARKFUN = os.path.join(_BENCH, "sparkfun-iot-redboard-rp2350",
                        "SparkFun_IoT_RedBoard-RP2350.kicad_pcb")

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def find_pad(board, x, y, eps=1e-4):
    for p in board.pads:
        if abs(p.x_mm - x) < eps and abs(p.y_mm - y) < eps:
            return p
    return None


def summarize(board):
    x0, y0 = board.origin_mm
    w, h = board.size_mm
    by_layer = Counter()
    for p in board.pads:
        for layer in p.layers:
            by_layer[layer] += 1
    print(f"\n=== {board.path.split('/')[-1]} ===")
    print(f"  size    : {w:.3f} x {h:.3f} mm   origin ({x0:.3f}, {y0:.3f})")
    print(f"  layers  : {len(board.copper_layers)} copper {board.copper_layers}")
    layer_str = "  ".join(f"{l}:{by_layer[l]}" for l in board.copper_layers)
    print(f"  pads    : {len(board.pads)} total   {layer_str}")
    print(f"  nets    : {len(board.nets)}")
    print(f"  tracks  : {len(board.tracks)}   vias: {len(board.vias)}")


def check_common(board):
    check(board.nets and 0 in board.nets, "nets non-empty and net 0 exists")
    check(board.nets.get(0) == "", "net 0 is the unconnected net ''")
    check(board.copper_layers and board.copper_layers[0] == "F.Cu"
          and board.copper_layers[-1] == "B.Cu",
          "copper stackup runs F.Cu .. B.Cu")
    x0, y0 = board.origin_mm
    w, h = board.size_mm
    netted = [p for p in board.pads if p.net_code > 0]
    outside = [p for p in netted
               if not (x0 - 5 <= p.x_mm <= x0 + w + 5 and y0 - 5 <= p.y_mm <= y0 + h + 5)]
    check(not outside,
          f"all {len(netted)} netted pads inside Edge.Cuts bbox + 5mm"
          + (f" ({len(outside)} outside, first at "
             f"({outside[0].x_mm:.2f}, {outside[0].y_mm:.2f}))" if outside else ""))
    for p in board.pads:
        if p.through_hole:
            check(p.layers == board.copper_layers,
                  "through-hole pads span every copper layer")
            check(p.drill_mm > 0, "through-hole pads have a drill")
            break
    check(all(not p.through_hole or p.drill_mm > 0 for p in board.pads),
          "every through-hole pad has drill > 0")
    check(all(p.drill_mm == 0.0 for p in board.pads if not p.through_hole),
          "every SMD pad has drill == 0")


def check_parser(board):
    """The committed KiCad 10 fixture: synthesized net codes from quoted names,
    an unrotated SMD footprint, a footprint rotated -90, a custom pad, a 4-layer
    stackup, and an Edge.Cuts outline enclosing every pad. Spot checks are
    computed BY HAND from the raw file text (see the module docstring)."""
    check(board.copper_layers == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
          f"4-layer stackup F.Cu..In1..In2..B.Cu ({board.copper_layers})")
    x0, y0 = board.origin_mm
    w, h = board.size_mm
    check(abs(x0 - 10.0) < 1e-9 and abs(y0 - -5.0) < 1e-9,
          f"Edge.Cuts origin ({x0:.3f}, {y0:.3f}) == (10, -5)")
    check(abs(w - 60.0) < 1e-9 and abs(h - 40.0) < 1e-9,
          f"Edge.Cuts size ({w:.3f}, {h:.3f}) == (60, 40)")

    # C1, unrotated SMD. KiCad 10 has no root net table: the code is synthesized
    # from the quoted name, so a positive code AND the exact name must survive.
    p1 = find_pad(board, 29.05, 15.0)
    check(p1 is not None, "C1 pad 1 found at hand-computed (29.05, 15.0)")
    if p1:
        check(p1.net_name == "Net-(C1-Pad1)", f"C1 pad 1 quoted net {p1.net_name!r}")
        check(p1.net_code > 0, "C1 pad 1 has a positive synthesized net code")
        check(not p1.through_hole and p1.drill_mm == 0.0, "C1 pad 1 is SMD")
        check(p1.layers == ["F.Cu"], f"C1 pad 1 layers {p1.layers}")
        check(abs(p1.width_mm - 1.0) < 1e-9 and abs(p1.height_mm - 1.45) < 1e-9,
              "C1 pad 1 true size 1.0 x 1.45")
        check(p1.rotation_deg == 0.0, "C1 pad 1 rotation 0 (footprint unrotated)")
    p2 = find_pad(board, 30.95, 15.0)
    check(p2 is not None and p2.net_name == "GND",
          "C1 pad 2 found at (30.95, 15.0) with net GND")

    # V1, footprint rotated -90: (fx - oy, fy + ox); TRUE (pre-rotation) pad
    # size and the pad's own in-file angle survive.
    pv = find_pad(board, 42.0, 22.0)
    check(pv is not None,
          "V1 pad 1 (footprint rotated -90) at hand-computed (42.0, 22.0)")
    if pv:
        check(pv.through_hole and abs(pv.drill_mm - 2.03) < 1e-9,
              f"V1 pad 1 through-hole, drill {pv.drill_mm} (max of oval 1.02x2.03)")
        check(pv.layers == board.copper_layers, "V1 pad 1 on all copper layers")
        check(abs(pv.width_mm - 2.03) < 1e-9 and abs(pv.height_mm - 3.05) < 1e-9,
              "V1 pad 1 TRUE size 2.03 x 3.05 (file size, not the rotated bbox)")
        check(abs(pv.rotation_deg - 216) < 1e-9,
              f"V1 pad 1 rotation {pv.rotation_deg} == 216 (in-file angle, frot folded in)")
    p4 = find_pad(board, 48.0, 22.0)
    check(p4 is not None and p4.through_hole,
          "V1 pad 4 (footprint rotated -90) at hand-computed (48.0, 22.0)")

    # Q1 pad 2 is a `custom` pad: board.py reads its (size 1.475 0.9) ANCHOR
    # rect, never the larger heat-tab primitive (the custom_pad_refs blind spot).
    pc = find_pad(board, 20.0, 9.1)
    check(pc is not None, "Q1 custom pad 2 found at hand-computed (20.0, 9.1)")
    if pc:
        check(not pc.through_hole and pc.drill_mm == 0.0, "Q1 pad 2 is SMD")
        check(abs(pc.width_mm - 1.475) < 1e-9 and abs(pc.height_mm - 0.9) < 1e-9,
              "Q1 custom pad 2 read as its 1.475 x 0.9 anchor rect, not the primitive")

    check(len(board.tracks) > 0, f"tracks parsed ({len(board.tracks)})")
    check(len(board.vias) > 0, f"vias parsed ({len(board.vias)})")


def check_pico_vga(board):
    """KiCad 5 fixture: (module ...) footprints + unquoted legacy net names."""
    check(len(board.pads) > 300, f"pad count {len(board.pads)} > 300")
    check(len(board.copper_layers) == 4, "pico-vga is a 4-layer board")
    w, h = board.size_mm
    check(abs(w - 85.0) < 0.1 and abs(h - 56.0) < 0.1,
          f"Edge.Cuts size ({w:.3f}, {h:.3f}) == (85.0, 56.0)")
    check(board.nets.get(1) == "GND",
          f"legacy net code 1 survives with unquoted name {board.nets.get(1)!r}")
    check(len(board.nets) == 73, f"all 73 root-table nets present ({len(board.nets)})")
    check(all(name != "" for code, name in board.nets.items() if code > 0),
          "no positive net code lost its name")
    p = find_pad(board, 133.35, 98.9)
    check(p is not None, "Pico module pad 43 found at hand-computed (133.35, 98.9)")
    if p:
        check(p.net_code == 72 and p.net_name == "/SWDIO",
              f"pad 43 net ({p.net_code}, {p.net_name!r}) == (72, '/SWDIO')")
        check(p.through_hole and abs(p.drill_mm - 1.02) < 1e-9,
              f"pad 43 through-hole, drill {p.drill_mm}")
        check(p.layers == board.copper_layers, "pad 43 (*.Cu) on all copper layers")
    check(len(board.tracks) > 300, f"tracks parsed ({len(board.tracks)})")
    check(len(board.vias) > 0, f"vias parsed ({len(board.vias)})")


def check_sparkfun(board):
    """KiCad 8 fixture: board outline lives in fp_line nodes inside a footprint."""
    check(len(board.pads) > 500, f"pad count {len(board.pads)} > 500")
    check(len(board.copper_layers) == 4, "RedBoard RP2350 is a 4-layer board")
    w, h = board.size_mm
    check(abs(w - 68.58) < 0.05 and abs(h - 58.42) < 0.05,
          f"Edge.Cuts size ({w:.3f}, {h:.3f}) == (68.58, 58.42) from footprint fp_lines")
    x0, y0 = board.origin_mm
    check(abs(x0 - 115.58) < 0.05 and abs(y0 - 66.05) < 0.05,
          f"Edge.Cuts origin ({x0:.3f}, {y0:.3f}) == (115.58, 66.05)")
    check(len(board.nets) > 100, f"nets non-empty ({len(board.nets)})")
    check(len(board.tracks) > 1000, f"tracks parsed ({len(board.tracks)})")
    check(len(board.vias) > 300, f"vias parsed ({len(board.vias)})")


if __name__ == "__main__":
    checked = 0

    # The committed, self-contained fixture (always present).
    brd = load_board(PARSER)
    summarize(brd)
    check_common(brd)
    check_parser(brd)
    checked += 1

    # Third-party bench boards (gitignored; skipped on a fresh clone) exercise
    # the KiCad 5 and KiCad 8 dialects the committed fixture does not.
    for path, checker in ((PICO_VGA, check_pico_vga), (SPARKFUN, check_sparkfun)):
        if not os.path.exists(path):
            print(f"\nSKIP {os.path.basename(path)}: fixture absent "
                  f"(bench/boards/ is gitignored — see bench/boards/SOURCES.md)")
            continue
        brd = load_board(path)
        summarize(brd)
        check_common(brd)
        checker(brd)
        checked += 1

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({checked} fixture{'s' if checked != 1 else ''}, "
          f"{len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
