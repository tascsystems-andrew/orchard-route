"""L1 validation: parse two real KiCad 10 boards and check geometry end to end.

The fixtures are Andrew's live amp projects (READ-ONLY — never write to them).
Spot-check expectations below were derived BY HAND from the raw file text, so
they catch transform bugs independently of the parser:

Voxy-arduino.kicad_pcb — C65, footprint "Capacitor_SMD:C_0805_2012Metric",
  footprint (at 243.59 22.87) unrotated;
  pad "1" smd (at -0.95 0) net "Net-(D4-K)"   -> abs (242.64, 22.87)
  pad "2" smd (at  0.95 0) net "Net-(U4A--)"  -> abs (244.54, 22.87)

hifi tube pre.kicad_pcb — "5755", footprint "Valve:Valve_ECC-83-2",
  footprint (at 78.241278 144.721998 -90) — the rotated-footprint case;
  pad "1" thru_hole (at 1.790008 2.351225 216) (drill oval 1.02 2.03)
  pad "4" thru_hole (at 1.71253 -11.99285 324)
  KiCad rotation is CCW with Y down, so -90 maps local (ox, oy) to
  (fx + oy, fy + ox):
  pad 1 -> (78.241278 - 2.351225, 144.721998 + 1.790008) = (75.890053, 146.512006)
  pad 4 -> (78.241278 + 11.99285, 144.721998 + 1.71253)  = (90.234128, 146.434528)

Voxy Edge.Cuts extremes read straight off the gr_rect/gr_line nodes:
  x 0 .. 300.254, y -46.99 .. 232  ->  origin (0, -46.99), size (300.254, 278.99)
"""
from collections import Counter

from board import load_board

PRIMARY = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb"
SECOND = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"

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


def check_primary(board):
    check(len(board.pads) > 100, f"pad count {len(board.pads)} > 100")
    check(len(board.copper_layers) == 4, "Voxy is a 4-layer board")
    x0, y0 = board.origin_mm
    w, h = board.size_mm
    check(abs(x0 - 0.0) < 1e-6 and abs(y0 - -46.99) < 1e-6,
          f"Edge.Cuts origin ({x0:.3f}, {y0:.3f}) == (0, -46.99)")
    check(abs(w - 300.254) < 1e-6 and abs(h - 278.99) < 1e-6,
          f"Edge.Cuts size ({w:.3f}, {h:.3f}) == (300.254, 278.99)")

    p1 = find_pad(board, 242.64, 22.87)
    check(p1 is not None, "C65 pad 1 found at hand-computed (242.64, 22.87)")
    if p1:
        check(p1.net_name == "Net-(D4-K)", f"C65 pad 1 net {p1.net_name!r}")
        check(p1.net_code > 0, "C65 pad 1 has a positive synthesized net code")
        check(not p1.through_hole and p1.drill_mm == 0.0, "C65 pad 1 is SMD")
        check(p1.layers == ["F.Cu"], f"C65 pad 1 layers {p1.layers}")
        check(abs(p1.width_mm - 1.0) < 1e-9 and abs(p1.height_mm - 1.45) < 1e-9,
              "C65 pad 1 size 1.0 x 1.45 (rotation 0, no swap)")
    p2 = find_pad(board, 244.54, 22.87)
    check(p2 is not None and p2.net_name == "Net-(U4A--)",
          "C65 pad 2 found at (244.54, 22.87) with net Net-(U4A--)")
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
    p4 = find_pad(board, 90.234128, 146.434528)
    check(p4 is not None,
          "5755 pad 4 (footprint rotated -90) at hand-computed (90.234128, 146.434528)")
    check(len(board.vias) > 0, f"vias parsed ({len(board.vias)})")


if __name__ == "__main__":
    primary = load_board(PRIMARY)
    second = load_board(SECOND)

    summarize(primary)
    check_common(primary)
    check_primary(primary)

    summarize(second)
    check_common(second)
    check_second(second)

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
