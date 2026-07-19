"""L1 validation: parse two real KiCad 10 boards and check geometry end to end.

NOTE (2026-07-19): the PRIMARY fixture (Voxy-arduino.kicad_pcb) is mid-redesign
— ~93% of its netted pads are currently staged outside the Edge.Cuts while the
board is re-laid-out, and C65 sits off-board at ~(573, -82.7). So its C65 pad
spot-check and the "all pads inside the outline" invariant are SKIPPED here
(the Edge.Cuts *size* is still asserted; origin is unchanged). Reading a live,
in-flux board for hand-verified spot-checks is the treadmill this suite is on;
the agreed real fix is committed FROZEN fixtures the tests own — see below.

The fixtures are Andrew's live amp projects (READ-ONLY — never write to them).
Spot-check expectations below were derived BY HAND from the raw file text, so
they catch transform bugs independently of the parser:

Voxy-arduino.kicad_pcb — C65 (SKIPPED, mid-redesign): this SMD cap's hand
  spot-check used to read footprint (at 243.59 22.87) -> pad1 (242.64, 22.87),
  pad2 (244.54, 22.87). The part is now staged off-board (~573, -82.7) and the
  check is skipped rather than re-pointed at a moving target.

hifi tube pre.kicad_pcb — "5755", footprint "Valve:Valve_ECC-83-2",
  footprint (at 78.241278 144.721998 -90) — the rotated-footprint case;
  pad "1" thru_hole (at 1.790008 2.351225 216) (drill oval 1.02 2.03)
  pad "4" thru_hole (at 1.71253 -11.99285 324)
  KiCad rotation is CCW with Y down, so -90 maps local (ox, oy) to
  (fx + oy, fy + ox):
  pad 1 -> (78.241278 - 2.351225, 144.721998 + 1.790008) = (75.890053, 146.512006)
  pad 4 -> (78.241278 + 11.99285, 144.721998 + 1.71253)  = (90.234128, 146.434528)

Voxy Edge.Cuts extremes read straight off the gr_rect/gr_line nodes:
  x 0 .. 365.629, y -46.99 .. 176.64  ->  origin (0, -46.99), size (365.629, 223.630)

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

PRIMARY = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb"
SECOND = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"

# Boards currently mid-redesign, where "all parts inside the outline" and the
# hand-picked pad spot-checks are legitimately in flux. Checks keyed off these
# skip-with-note instead of asserting transient geometry. See docstring NOTE.
MID_REDESIGN_PATHS = {PRIMARY}

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
    if board.path in MID_REDESIGN_PATHS:
        # This board is mid-redesign: as of 2026-07-19 ~93% of netted pads are
        # staged well outside the current Edge.Cuts while Andrew re-lays it out,
        # so "everything inside the outline" is legitimately false. Skip-with-
        # note rather than assert a transient truth; a committed frozen fixture
        # (see module docstring NOTE) is the real fix.
        print(f"  SKIP netted-pads-inside-outline: {board.path.split('/')[-1]} "
              f"is mid-redesign ({len(outside)}/{len(netted)} pads staged "
              f"off-outline) — frozen-fixture decoupling pending")
    else:
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


def check_primary(board):
    check(len(board.pads) > 100, f"pad count {len(board.pads)} > 100")
    check(len(board.copper_layers) == 4, "Voxy is a 4-layer board")
    x0, y0 = board.origin_mm
    w, h = board.size_mm
    check(abs(x0 - 0.0) < 1e-6 and abs(y0 - -46.99) < 1e-6,
          f"Edge.Cuts origin ({x0:.3f}, {y0:.3f}) == (0, -46.99)")
    # Edge.Cuts size is a comparatively stable fact (origin is unchanged at
    # (0, -46.99)); updated to the current outline. The frozen-fixture decoupling
    # (docstring NOTE) will eventually own even this, but it holds green today.
    check(abs(w - 365.629) < 1e-3 and abs(h - 223.630) < 1e-3,
          f"Edge.Cuts size ({w:.3f}, {h:.3f}) == (365.629, 223.630)")

    # C65's hand-derived pad spot-check (was (242.64/244.54, 22.87)) is SKIPPED,
    # not re-pointed: the part is currently staged off-board at ~(573, -82.7)
    # mid-redesign, and chasing that transient position is exactly the treadmill
    # the frozen fixture will end. The parser's transform math stays covered by
    # the rotated-footprint 5755 case on the SECOND board and the bench fixtures.
    print("  SKIP C65 pad hand-check: part staged off-board mid-redesign "
          "(~573, -82.7) — frozen-fixture decoupling pending")
    check(len(board.tracks) > 0, f"tracks parsed ({len(board.tracks)})")


def check_second(board):
    check(len(board.copper_layers) == 2, "hifi pre is a 2-layer board")
    p1 = find_pad(board, 75.890053, 146.512006)
    check(p1 is not None,
          "5755 pad 1 (footprint rotated -90) at hand-computed (75.890053, 146.512006)")
    if p1:
        check(p1.through_hole and abs(p1.drill_mm - 2.03) < 1e-9,
              f"5755 pad 1 through-hole, drill {p1.drill_mm} (max of oval 1.02x2.03)")
        check(p1.layers == board.copper_layers, "5755 pad 1 on all copper layers")
        check(abs(p1.width_mm - 2.03) < 1e-9 and abs(p1.height_mm - 3.05) < 1e-9,
              "5755 pad 1 TRUE size 2.03 x 3.05 (file size, not the rotated bbox)")
        check(abs(p1.rotation_deg - 216) < 1e-9,
              f"5755 pad 1 rotation {p1.rotation_deg} == 216 (in-file angle, frot folded in)")
    p4 = find_pad(board, 90.234128, 146.434528)
    check(p4 is not None,
          "5755 pad 4 (footprint rotated -90) at hand-computed (90.234128, 146.434528)")
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

    for path, checkers in ((PRIMARY, (check_common, check_primary)),
                           (SECOND, (check_common, check_second))):
        if not os.path.exists(path):
            print(f"\nSKIP {os.path.basename(path)}: fixture absent "
                  f"(laptop-local amp project — normal on other machines, "
                  f"see STUDIO.md)")
            continue
        brd = load_board(path)
        summarize(brd)
        for c in checkers:
            c(brd)
        checked += 1

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

    if checked == 0:
        print("\nRESULT: NO FIXTURES — nothing was checked on this machine. "
              "Fetch bench boards per bench/boards/SOURCES.md to make this "
              "suite meaningful here.")
        raise SystemExit(0)
    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({checked} fixture{'s' if checked != 1 else ''}, "
          f"{len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
