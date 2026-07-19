"""Regression tests for DEFECT 1: per-net-class clearance must be RESOLVED,
ENFORCED, REPORTED and — when the pitch cannot carry it — REFUSED by name.

The bug: the router resolved ONE clearance (the Default net class's) and
applied it to every net. On a tube amp that is disqualifying. Voxy-arduino has
~28 nets between 300 V and 600 V (IRFBG30 drains at ~600 V peak, confirmed by
the board's own 630 V snubbers; B+/C+/D+ and plate nodes ~300 V; VMID ~150 V)
which need 0.4-1.0 mm of creepage coated, 1.25-2.5 mm uncoated, per IPC-2221B,
alongside ~450 logic and audio nets that are fine at 0.15 mm. Declaring an HV
class in the .kicad_pro changed NOTHING: those tracks were spaced exactly like
logic, the run printed VIOLATED, and the copper was emitted anyway.

Every number below is derived by hand from the class definitions, never read
back from the implementation:

  Default   clearance 0.15  track 0.2  ->  needs pitch >= 0.35   (fits 0.6)
  HV_PA     clearance 1.00  track 0.2  ->  needs pitch >= 1.20   (does NOT)

  at pitch 0.6 the widest clearance any class can ask for is 0.6 - 0.2 = 0.4
  HV_PA track exclusion  = 1.00 + 0.2/2 = 1.10 mm -> 9 nodes on a 0.6 grid
  Default track exclusion = 0.15 + 0.2/2 = 0.25 mm -> under one grid step,
                            so the logic nets claim nothing extra at all
  HV_PA enforced separation = (floor(1.10/0.6) + 1) * 0.6 = 1.20 mm
"""
import math
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out", "test-netclass")

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ── a synthetic two-class board ──────────────────────────────────────────
PCB_HEAD = """(kicad_pcb
\t(version 20240108)
\t(generator "test_netclass_clearance")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
"""


def _pad(ref, x, y, code, name):
    return (f'\t(footprint "TP:{ref}"\n'
            f'\t\t(at {x} {y})\n'
            f'\t\t(property "Reference" "{ref}")\n'
            f'\t\t(pad "1" smd rect (at 0 0) (size 0.8 0.8) '
            f'(layers "F.Cu") (net {code} "{name}"))\n'
            f'\t)\n')


def write_board(path, nets, pads, outline=(0.0, 0.0, 40.0, 20.0),
                classes=None, patterns=None):
    """A minimal but real .kicad_pcb + sibling .kicad_pro."""
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = [PCB_HEAD]
    for code, name in sorted(nets.items()):
        body.append(f'\t(net {code} "{name}")\n')
    x0, y0, x1, y1 = outline
    body.append(f'\t(gr_rect (start {x0} {y0}) (end {x1} {y1}) '
                f'(layer "Edge.Cuts") (width 0.1))\n')
    for i, (x, y, code) in enumerate(pads):
        body.append(_pad(f"TP{i}", x, y, code, nets[code]))
    body.append(")\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(body))
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    with open(pro, "w", encoding="utf-8") as f:
        json.dump({"net_settings": {
            "classes": classes or [],
            "netclass_assignments": None,
            "netclass_patterns": patterns or [],
        }}, f, indent=2)
    return path


DEFAULT_CLS = {"name": "Default", "priority": 2147483647,
               "clearance": 0.15, "track_width": 0.2,
               "via_diameter": 0.45, "via_drill": 0.25}
HV_CLS = {"name": "HV_PA", "priority": 1,
          "clearance": 1.0, "track_width": 0.2,
          "via_diameter": 0.45, "via_drill": 0.25}

NETS = {0: "", 1: "HV Plate PA1", 2: "Logic A", 3: "Logic B",
        4: "HV Screen PA1"}
PADS = [(5.0, 5.0, 1), (35.0, 5.0, 1),
        (5.0, 8.0, 2), (35.0, 8.0, 2),
        (5.0, 11.0, 3), (35.0, 11.0, 3),
        (5.0, 14.0, 4), (35.0, 14.0, 4)]


def two_class_board():
    return write_board(
        os.path.join(OUT_DIR, "two-class.kicad_pcb"), NETS, PADS,
        classes=[DEFAULT_CLS, HV_CLS],
        patterns=[{"pattern": "HV *", "netclass": "HV_PA"}])


# ── 1. resolution: the project's classes, per net ────────────────────────
def test_clearance_resolves_per_net():
    print("=== a net's clearance comes from ITS class, not the board's ===")
    board = two_class_board()
    from writeback import (load_net_class_clearances, load_net_class_names,
                           load_net_class_widths, project_file_for)
    pro = project_file_for(board)
    clr = load_net_class_clearances(pro, NETS)
    names = load_net_class_names(pro, NETS)
    widths = load_net_class_widths(pro, NETS)
    check("the two HV nets resolve to 1.0 mm",
          clr[1] == 1.0 and clr[4] == 1.0, f"{clr[1]}, {clr[4]}")
    check("the logic nets resolve to 0.15 mm",
          clr[2] == 0.15 and clr[3] == 0.15, f"{clr[2]}, {clr[3]}")
    check("clearance and width agree about which class a net is in",
          all(names[c] == "HV_PA" for c in (1, 4))
          and all(names[c] == "Default" for c in (2, 3)), str(names))
    check("and it is the SAME resolver, so it cannot drift",
          set(widths) == set(clr) == set(names))

    from geometry import resolve_net_classes
    per_net, classes = resolve_net_classes(board, NETS)
    by_name = {c.name: c for c in classes}
    check("the class table names both classes", set(by_name) == {"Default", "HV_PA"},
          str(sorted(by_name)))
    check("HV_PA counts its 2 nets", by_name["HV_PA"].net_count == 2)
    check("Default counts its 2 nets", by_name["Default"].net_count == 2)
    check("per-net map covers every net", set(per_net) >= {1, 2, 3, 4})


# ── 2. the contract line: per-class numbers, widest-class arithmetic ─────
def test_contract_reports_every_class():
    print("=== the contract line must not print ONE clearance for two classes ===")
    board = two_class_board()
    from geometry import resolve_board_geometry
    widths = {c: (0.2, 0.45, 0.25) for c in (1, 2, 3, 4)}
    g = resolve_board_geometry(board, 0.6, NETS, widths=widths)

    check("the contract carries both classes", len(g.classes) == 2,
          str([c.name for c in g.classes]))
    check("and knows they disagree", g.multi_class)
    line = g.summary()
    print(f"       {line}")
    check("the clearance field names the default AND the HV class",
          "0.15 default" in line and "1.00 HV_PA" in line and "(2 classes)" in line,
          line)
    check("it does NOT print a single clearance number",
          "clearance 0.15 via" not in line, line)

    # Hand arithmetic: 0.2 + 1.0 = 1.2, and it is the HV class that governs.
    check("required pitch is stated for the WIDEST class (0.2 + 1.0)",
          abs(g.orthogonal_pitch_mm - 1.2) < 1e-12, f"{g.orthogonal_pitch_mm}")
    check("and the line says whose number it is",
          "needs 1.20, widest class HV_PA" in line, line)
    check("orthogonal is VIOLATED at 0.6 pitch, not OK",
          not g.orthogonal_ok and "orthogonal VIOLATED" in line)
    check("the governing class is HV_PA", g.governing_class.name == "HV_PA")
    check("the widest clearance this pitch can carry is 0.6 - 0.2",
          abs(g.max_clearance_at_pitch_mm - 0.4) < 1e-12,
          f"{g.max_clearance_at_pitch_mm}")

    # The Default class alone still fits, and the diagonal gate reads THAT —
    # one HV class must not switch the whole board to 90-degree geometry.
    check("the Default class's own rule is 0.2 + 0.15 = 0.35",
          abs(g.base_orthogonal_pitch_mm - 0.35) < 1e-12)
    check("diagonals are gated on the Default class (sqrt(2)*0.35 = 0.495)",
          g.diagonals_ok and abs(g.diagonal_pitch_mm - math.sqrt(2) * 0.35) < 1e-12)


def test_impossible_class_is_named_with_a_working_pitch():
    print("=== a class the pitch cannot carry is REFUSED BY NAME ===")
    board = two_class_board()
    from geometry import resolve_board_geometry
    widths = {c: (0.2, 0.45, 0.25) for c in (1, 2, 3, 4)}
    g = resolve_board_geometry(board, 0.6, NETS, widths=widths)

    bad = g.impossible_classes()
    check("exactly one class is impossible at 0.6 pitch", len(bad) == 1,
          str([(c.name, n) for c, n in bad]))
    check("it is HV_PA, and the pitch that works is 1.2",
          bad[0][0].name == "HV_PA" and abs(bad[0][1] - 1.2) < 1e-12,
          str(bad))

    ws = g.warnings()
    hv = [w for w in ws if "HV_PA" in w and "CANNOT BE HONOURED" in w]
    check("the run says so, loudly, per class", len(hv) == 1, str(ws))
    if hv:
        w = hv[0]
        print(f"       {w}")
        check("it states the pitch that would work", "--pitch 1.200" in w, w)
        check("it states the widest clearance this pitch allows (0.400)",
              "0.400mm" in w, w)
        check("it counts the nets it is talking about", "2 net(s)" in w, w)
        check("it promises they are NOT routed at the Default clearance",
              "NOT routed at the Default clearance" in w, w)
        check("and states what IS enforced instead (1.200 mm)",
              "1.200mm of\n" not in w and "1.200" in w.split("halo")[1], w)

    # A pitch that CAN carry the class must say nothing about it.
    ok = resolve_board_geometry(board, 1.5, NETS, widths=widths)
    check("at 1.5 mm pitch no class is impossible",
          not ok.impossible_classes()
          and not any("CANNOT BE HONOURED" in w for w in ok.warnings()),
          str(ok.warnings()))
    check("and orthogonal reads OK", ok.orthogonal_ok)


# ── 3. enforcement: a wide class sterilises a wider neighbourhood ────────
def test_hv_net_claims_more_nodes_than_a_logic_net():
    print("=== the halo is sized by the NET's class, not the board's ===")
    from geometry import NetClass, halo_offsets
    from lattice import build_lattice
    from pathfinder import _footprint, _track_halo

    hv = NetClass("HV_PA", clearance_mm=1.0, track_width_mm=0.2,
                  via_size_mm=0.45, net_count=2)
    lg = NetClass("Default", clearance_mm=0.15, track_width_mm=0.2,
                  via_size_mm=0.45, net_count=2)
    check("HV track exclusion = 1.0 + 0.2/2 = 1.10",
          abs(hv.track_exclusion_mm - 1.10) < 1e-12)
    check("logic track exclusion = 0.15 + 0.2/2 = 0.25",
          abs(lg.track_exclusion_mm - 0.25) < 1e-12)

    lat = build_lattice(11, 11, 1, pitch_mm=0.6, directions="both")
    hv_halo = _track_halo(lat, hv.track_exclusion_mm)
    lg_halo = _track_halo(lat, lg.track_exclusion_mm)
    check("the HV halo is the 3x3 disk of radius 1.10 on a 0.6 grid",
          hv_halo is not None and len(hv_halo) == 9
          and set(hv_halo) == set(halo_offsets(0.6, 1.10)), str(hv_halo))
    check("the logic halo is nothing at all — 0.25 is under one grid step",
          lg_halo is None)

    path = [lat.node(x, 5, 0) for x in range(2, 8)]
    hv_fp = _footprint(lat, path, None, frozenset(), hv_halo)
    lg_fp = _footprint(lat, path, None, frozenset(), lg_halo)
    check("the HV net claims strictly more nodes for the same path",
          len(hv_fp) > len(lg_fp) and lg_fp == set(path),
          f"HV {len(hv_fp)} vs logic {len(lg_fp)} for a {len(path)}-node path")
    check("the HV claim reaches one grid step either side",
          lat.node(5, 4, 0) in hv_fp and lat.node(5, 6, 0) in hv_fp
          and lat.node(5, 7, 0) not in hv_fp)
    check("and stays on its own layer (a track is not a via)",
          all(lat.coords(n)[2] == 0 for n in hv_fp))
    check("enforced separation quantises up to 1.20, the pitch HV_PA needed",
          abs(hv.enforced_separation_mm(0.6) - 1.20) < 1e-12,
          f"{hv.enforced_separation_mm(0.6)}")


def test_negotiation_pushes_a_logic_net_out_of_an_hv_halo():
    print("=== and the negotiation ENFORCES it, with no new kernel concept ===")
    from pathfinder import route_lattice
    from lattice import build_lattice

    # Two nets that naturally want adjacent lanes down a channel. Net 1 is
    # HV (1.0 mm class), net 2 is logic (0.15 mm). Pads are exempt from halo
    # claims by design, so the property under test is about ROUTED copper.
    W, H = 22, 9
    lat = build_lattice(W, H, 1, pitch_mm=0.6, directions="both")

    def pad(x, y):
        return ((lat.node(x, y, 0),), (x * 0.6, y * 0.6))

    net_pads = {1: [pad(1, 4), pad(20, 4)],
                2: [pad(1, 5), pad(20, 5)]}
    pads = {n for pl in net_pads.values() for nodes, _ in pl for n in nodes}

    def lanes(res, net):
        return {lat.coords(v)[1] for p in res.net_paths[net] for v in p
                if v not in pads}

    def gap(res):
        a = {(lat.coords(v)[0], lat.coords(v)[1])
             for p in res.net_paths[1] for v in p if v not in pads}
        b = {(lat.coords(v)[0], lat.coords(v)[1])
             for p in res.net_paths[2] for v in p if v not in pads}
        return min((abs(ay - by) for ax, ay in a for bx, by in b
                    if ax == bx), default=99)

    off = route_lattice(lat, net_pads, refine_passes=0)
    on = route_lattice(lat, net_pads, refine_passes=0,
                       net_exclusion={1: (0.0, 1.10), 2: (0.0, 0.25)})
    check("both nets route with per-class spacing off",
          not off.failed and len(off.net_paths) == 2, str(off.failed))
    check("both nets still route with it on",
          not on.failed and len(on.net_paths) == 2, str(on.failed))
    check("without it the two nets run in ADJACENT lanes (1 grid step)",
          gap(off) == 1, f"gap {gap(off)}, lanes {lanes(off, 1)} / {lanes(off, 2)}")
    check("with it the logic net is pushed a full halo clear of the HV net",
          gap(on) >= 2,
          f"gap {gap(on)} steps = {gap(on) * 0.6:.2f} mm, "
          f"lanes {sorted(lanes(on, 1))} / {sorted(lanes(on, 2))}")
    check("which is the 1.20 mm HV_PA asked for and the pitch could not state",
          gap(on) * 0.6 >= 1.2 - 1e-9, f"{gap(on) * 0.6:.2f} mm")
    check("only the HV net claims a halo (the logic net's is nothing)",
          on.via_stats is not None and on.via_stats["track_halo_nets"] == 1,
          str(on.via_stats))


# ── 4. end to end on a real board file ───────────────────────────────────
def test_route_board_end_to_end():
    print("=== route_board resolves, enforces and reports it, on a file ===")
    board = two_class_board()
    from pathfinder import route_board
    brd, lat, res = route_board(board, pitch_mm=0.6,
                                layer_names=["F.Cu", "B.Cu"],
                                max_iters=8, refine_passes=0, smooth=False)
    print(f"       geometry: {res.geometry_note}")
    check("the contract reports both classes",
          "0.15 default" in res.geometry_note
          and "1.00 HV_PA" in res.geometry_note, res.geometry_note)
    check("the run refuses HV_PA by name with a working pitch",
          any("HV_PA" in w and "--pitch 1.200" in w
              for w in res.geometry_warnings), str(res.geometry_warnings))
    check("per-net clearance reached the router",
          res.net_clearance.get(1) == 1.0 and res.net_clearance.get(2) == 0.15,
          str({k: res.net_clearance.get(k) for k in (1, 2, 3, 4)}))
    check("only the HV nets claim a track halo",
          res.via_stats and res.via_stats["track_halo_nets"] == 2
          and res.via_stats["track_halo_max_nodes"] == 9, str(res.via_stats))
    check("the HV pads' clearance rings are wider than the logic pads'",
          res.clearance_stats["max_inflate_mm"] > res.clearance_stats["inflate_mm"],
          f"default {res.clearance_stats['inflate_mm']:.3f} vs widest "
          f"{res.clearance_stats['max_inflate_mm']:.3f}")
    check("ring inflate is per class: HV 1.0 + 0.2/2 = 1.10",
          abs(res.clearance_stats["max_inflate_mm"] - 1.10) < 1e-9,
          f"{res.clearance_stats['max_inflate_mm']}")
    check("the board still routes", not res.failed, str(res.failed[:3]))

    # A single-class board must behave exactly as it always did: one number,
    # no per-class text, no halo, no refusal.
    plain = write_board(os.path.join(OUT_DIR, "one-class.kicad_pcb"),
                        NETS, PADS, classes=[DEFAULT_CLS])
    _b, _l, r2 = route_board(plain, pitch_mm=0.6,
                             layer_names=["F.Cu", "B.Cu"],
                             max_iters=8, refine_passes=0, smooth=False)
    print(f"       geometry: {r2.geometry_note}")
    check("one class -> one clearance, printed the old way",
          "clearance 0.15 via" in r2.geometry_note
          and "classes)" not in r2.geometry_note, r2.geometry_note)
    check("one class -> no per-class refusal",
          not any("CANNOT BE HONOURED" in w for w in r2.geometry_warnings),
          str(r2.geometry_warnings))
    check("one class -> no track halo claimed by anyone",
          not (r2.via_stats or {}).get("track_halo_nets"), str(r2.via_stats))


def main():
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    test_clearance_resolves_per_net()
    test_contract_reports_every_class()
    test_impossible_class_is_named_with_a_working_pitch()
    test_hv_net_claims_more_nodes_than_a_logic_net()
    test_negotiation_pushes_a_logic_net_out_of_an_hv_halo()
    test_route_board_end_to_end()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
