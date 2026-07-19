"""Regression tests for DEFECT 2: a file holding SEVERAL disjoint Edge.Cuts
outlines is a PANEL of separate boards, and the space between them is air.

The bug: board.py's _edge_bbox returned the UNION bbox, lattice_for_board
covered all of it, and the router drew copper across the gap. Measured on
Voxy-arduino: 4 outline regions, ~20 mm of empty space between the main board
and the next, and 27 of 486 nets with pads on both sides of that band — all
"routed" through space that is not board.

Three things had to become true, and each is asserted here on a synthetic
panel whose numbers are chosen by hand:

  region A   x 0..40    y 0..20     pads for nets 1 (LOCAL_A) and 3 (SHARED)
  region B   x 0..40    y 45..65    pads for nets 2 (LOCAL_B) and 3 (SHARED)
  the gap    y 20..45   = 25 mm of nothing

  1. the two outlines are detected as two regions, not one 65 mm-tall board;
  2. no node in the gap is routable, so no emitted copper can cross it;
  3. net 3 is named as spanning regions, loudly, before anyone reads a score.

And the control: a single-outline board must be unchanged in every respect.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out", "test-multiboard")

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


PCB_HEAD = """(kicad_pcb
\t(version 20240108)
\t(generator "test_multiboard")
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(31 "B.Cu" signal)
\t\t(44 "Edge.Cuts" user)
\t)
"""

NETS = {0: "", 1: "LOCAL_A", 2: "LOCAL_B", 3: "SHARED"}

REGION_A = (0.0, 0.0, 40.0, 20.0)
REGION_B = (0.0, 45.0, 40.0, 65.0)
GAP_Y = (20.0, 45.0)

# Pads: each local net has two pads on one board; SHARED has two on EACH
# board, which is the panel case — the same net NAME on two products.
PADS = [
    (5.0, 5.0, 1), (35.0, 5.0, 1),          # LOCAL_A, region A
    (5.0, 15.0, 3), (35.0, 15.0, 3),        # SHARED, region A
    (5.0, 50.0, 3), (35.0, 50.0, 3),        # SHARED, region B
    (5.0, 60.0, 2), (35.0, 60.0, 2),        # LOCAL_B, region B
]


def write_board(path, outlines, pads, nets=NETS):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = [PCB_HEAD]
    for code, name in sorted(nets.items()):
        body.append(f'\t(net {code} "{name}")\n')
    for x0, y0, x1, y1 in outlines:
        body.append(f'\t(gr_rect (start {x0} {y0}) (end {x1} {y1}) '
                    f'(layer "Edge.Cuts") (width 0.1))\n')
    for i, (x, y, code) in enumerate(pads):
        body.append(f'\t(footprint "TP:TP{i}"\n'
                    f'\t\t(at {x} {y})\n'
                    f'\t\t(property "Reference" "TP{i}")\n'
                    f'\t\t(pad "1" smd rect (at 0 0) (size 0.8 0.8) '
                    f'(layers "F.Cu") (net {code} "{nets[code]}"))\n'
                    f'\t)\n')
    body.append(")\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(body))
    with open(os.path.splitext(path)[0] + ".kicad_pro", "w",
              encoding="utf-8") as f:
        json.dump({"net_settings": {"classes": [
            {"name": "Default", "priority": 2147483647, "clearance": 0.15,
             "track_width": 0.2, "via_diameter": 0.45, "via_drill": 0.25}]}},
            f, indent=2)
    return path


def panel_board():
    return write_board(os.path.join(OUT_DIR, "panel.kicad_pcb"),
                       [REGION_A, REGION_B], PADS)


def single_board():
    """The control: region A alone, with only its own pads."""
    return write_board(os.path.join(OUT_DIR, "single.kicad_pcb"),
                       [REGION_A], [p for p in PADS if p[1] < GAP_Y[0]])


# Terminal-served variant: HVSHARED (net 4) is a TIGHT 3-pad cluster on each
# board, so each board collapses to exactly ONE wire landing — unlike SHARED,
# whose 30 mm-apart pads default-cluster per pad. The boards are ~50 mm apart,
# far past any cluster threshold, so the two landings never merge across the gap.
NETS_T = {0: "", 1: "LOCAL_A", 2: "LOCAL_B", 3: "SHARED", 4: "HVSHARED"}
PADS_T = PADS + [
    (10.0, 5.0, 4), (12.0, 5.0, 4), (10.0, 7.0, 4),       # region A cluster
    (10.0, 55.0, 4), (12.0, 55.0, 4), (10.0, 57.0, 4),    # region B cluster
]


def terminal_panel():
    return write_board(os.path.join(OUT_DIR, "terminal-panel.kicad_pcb"),
                       [REGION_A, REGION_B], PADS_T, nets=NETS_T)


# ── 1. detection ─────────────────────────────────────────────────────────
def test_regions_are_detected():
    print("=== two disjoint outlines are two boards, not one tall one ===")
    from board import load_board
    brd = load_board(panel_board())
    check("two regions found", len(brd.outline_regions) == 2,
          str([(r.origin_mm, r.size_mm) for r in brd.outline_regions]))
    a, b = brd.outline_regions
    check("region 0 is the lower-y board (0,0) 40x20",
          a.origin_mm == (0.0, 0.0) and a.size_mm == (40.0, 20.0),
          f"{a.origin_mm} {a.size_mm}")
    check("region 1 is the other board (0,45) 40x20",
          b.origin_mm == (0.0, 45.0) and b.size_mm == (40.0, 20.0),
          f"{b.origin_mm} {b.size_mm}")
    check("origin_mm/size_mm keep their union-bbox contract",
          brd.origin_mm == (0.0, 0.0) and brd.size_mm == (40.0, 65.0),
          f"{brd.origin_mm} {brd.size_mm}")
    check("the union bbox is 25 mm taller than the two boards together",
          abs(brd.size_mm[1] - (a.size_mm[1] + b.size_mm[1] + 25.0)) < 1e-9)

    single = load_board(single_board())
    check("a single-outline board reports exactly one region",
          len(single.outline_regions) == 1
          and single.outline_regions[0].origin_mm == single.origin_mm
          and single.outline_regions[0].size_mm == single.size_mm,
          str(single.outline_regions))

    # An inner feature is not a board. A cutout drawn inside region A must be
    # absorbed by it, or every mounting hole on Edge.Cuts becomes a "region".
    holed = write_board(os.path.join(OUT_DIR, "holed.kicad_pcb"),
                        [REGION_A, (10.0, 8.0, 14.0, 12.0)],
                        [p for p in PADS if p[1] < GAP_Y[0]])
    hb = load_board(holed)
    check("a cutout inside the outline does NOT become its own region",
          len(hb.outline_regions) == 1, str(hb.outline_regions))


def test_spanning_nets_are_named():
    print("=== nets with pads on two boards are named before any score ===")
    from board import load_board
    from pathfinder import cross_region_nets
    brd = load_board(panel_board())
    spanning = cross_region_nets(brd, brd.outline_regions)
    check("exactly the SHARED net spans regions", list(spanning) == [3],
          str({brd.nets[c]: v for c, v in spanning.items()}))
    check("and it names both regions", spanning[3] == [0, 1], str(spanning))
    check("the local nets are not accused", 1 not in spanning and 2 not in spanning)

    single = load_board(single_board())
    check("a single-region board has no spanning nets by definition",
          cross_region_nets(single, single.outline_regions) == {})


# ── 2. the gap is not routable ───────────────────────────────────────────
def test_gap_is_blocked_in_the_lattice():
    print("=== the space between two boards is not part of the graph ===")
    from board import load_board
    from lattice import lattice_for_board
    brd = load_board(panel_board())
    pitch = 0.6
    lat, _pad_nodes, _owner = lattice_for_board(brd, pitch,
                                                layer_names=["F.Cu", "B.Cu"])
    # A node dead in the middle of the 25 mm band, on both layers.
    mid_y = (GAP_Y[0] + GAP_Y[1]) / 2.0
    blocked_ok = True
    for layer in ("F.Cu", "B.Cu"):
        nd = lat.snap(20.0, mid_y, layer)
        deg = lat.row_ptr[nd + 1] - lat.row_ptr[nd]
        blocked_ok = blocked_ok and deg == 0
    check("a node in the middle of the gap has NO edges at all", blocked_ok)

    # And a node in the middle of each board still does.
    live = True
    for y in (10.0, 55.0):
        nd = lat.snap(20.0, y, "F.Cu")
        live = live and (lat.row_ptr[nd + 1] - lat.row_ptr[nd]) > 0
    check("nodes inside each board are still fully connected", live)

    off = lattice_for_board(brd, pitch, layer_names=["F.Cu", "B.Cu"],
                            block_between_regions=False)[0]
    nd = off.snap(20.0, mid_y, "F.Cu")
    check("and with the block disabled the old behaviour is reproducible "
          "(so this test measures the fix, not the fixture)",
          (off.row_ptr[nd + 1] - off.row_ptr[nd]) > 0)

    single = load_board(single_board())
    s_lat = lattice_for_board(single, pitch, layer_names=["F.Cu", "B.Cu"])[0]
    edges = int(s_lat.row_ptr[-1])
    s_off = lattice_for_board(single, pitch, layer_names=["F.Cu", "B.Cu"],
                              block_between_regions=False)[0]
    check("a single-region board's lattice is byte-for-byte what it was",
          edges == int(s_off.row_ptr[-1])
          and (s_lat.row_ptr == s_off.row_ptr).all()
          and (s_lat.col_idx == s_off.col_idx).all(),
          f"{edges} csr entries")


def test_no_copper_crosses_the_gap():
    print("=== and no emitted copper crosses it ===")
    from pathfinder import route_board
    brd, lat, res = route_board(panel_board(), pitch_mm=0.6,
                                layer_names=["F.Cu", "B.Cu"],
                                max_iters=12, refine_passes=0)
    tracks = res.tracks if res.tracks is not None else []
    lo, hi = GAP_Y

    def crosses(t):
        x1, y1, x2, y2 = t[0], t[1], t[2], t[3]
        return (min(y1, y2) < hi - 1e-9) and (max(y1, y2) > lo + 1e-9)

    bad = [t for t in tracks if crosses(t)]
    check("copper was actually emitted (else this proves nothing)",
          len(tracks) > 0, f"{len(tracks)} segments")
    check("NO emitted segment enters the 25 mm band between the boards",
          not bad, f"{len(bad)} crossing segment(s): {bad[:3]}")
    check("no via lands in the band either",
          not [v for v in (res.vias or []) if lo < v[1] < hi],
          str([v for v in (res.vias or []) if lo < v[1] < hi][:3]))

    # Every CONNECTION's copper stays on one board. A net repeated across the
    # panel legitimately has one path per board — that is the point of
    # grouping the MST by region — so the property is per path, not per net.
    for net, paths in sorted(res.net_paths.items()):
        for i, p in enumerate(paths):
            ys = [lat.node_xy_mm(v)[1] for v in p]
            check(f"net {brd.nets[net]!r} path {i} stays on one board",
                  max(ys) < lo + 1e-6 or min(ys) > hi - 1e-6,
                  f"y {min(ys):.2f}..{max(ys):.2f}")
    shared_sides = {min(lat.node_xy_mm(v)[1] for v in p) > hi
                    for p in res.net_paths.get(3, [])}
    check("the shared net was routed on BOTH boards, separately",
          shared_sides == {False, True}, str(shared_sides))

    check("the shared net still got routed ON each board",
          3 in res.net_paths and len(res.net_paths[3]) >= 2,
          f"{len(res.net_paths.get(3, []))} path(s)")


# ── 3. the warning, and the per-region affordance ────────────────────────
def test_route_board_warns_loudly():
    print("=== the run says it is a panel and names the spanning nets ===")
    from pathfinder import route_board
    brd, _lat, res = route_board(panel_board(), pitch_mm=0.6,
                                 layer_names=["F.Cu", "B.Cu"],
                                 max_iters=12, refine_passes=0)
    ws = res.region_warnings or []
    for w in ws:
        print(f"       {w}")
    check("the panel itself is reported",
          any("MULTI-BOARD PANEL" in w and "2 disjoint" in w for w in ws), str(ws))
    check("the spanning nets are counted and named",
          any("1 net(s) have pads on more than one board region" in w
              and "SHARED" in w for w in ws), str(ws))
    check("the warning says they cannot be routed as one board",
          any("cannot be routed as one board" in w for w in ws), str(ws))
    check("and says what to do instead (including serving by wire)",
          any("route regions separately, add a connector, or serve them by "
              "wire" in w for w in ws), str(ws))
    check("it states plainly that no copper crossed",
          any("NO copper was emitted across the gap" in w for w in ws), str(ws))
    check("res.cross_region_nets is machine-readable too",
          res.cross_region_nets == {3: [0, 1]}, str(res.cross_region_nets))

    _b2, _l2, r2 = route_board(single_board(), pitch_mm=0.6,
                               layer_names=["F.Cu", "B.Cu"],
                               max_iters=12, refine_passes=0)
    check("a single-board file says nothing about regions",
          not (r2.region_warnings or []) and not r2.cross_region_nets,
          str(r2.region_warnings))


def test_region_index_routes_one_board():
    print("=== --region-index routes ONE board of the panel ===")
    from pathfinder import route_board
    board = panel_board()
    brd, lat, res = route_board(board, pitch_mm=0.6,
                                layer_names=["F.Cu", "B.Cu"],
                                region_index=0, max_iters=12, refine_passes=0)
    check("only region 0's pads are in the run",
          len(brd.pads) == 4, f"{len(brd.pads)} pads")
    check("the lattice is sized to that board, not the panel",
          lat.H * lat.pitch_mm < 30.0,
          f"{lat.W}x{lat.H} = {lat.W * lat.pitch_mm:.1f} x "
          f"{lat.H * lat.pitch_mm:.1f} mm")
    check("region 0's nets routed", set(res.net_paths) == {1, 3},
          str(sorted(res.net_paths)))
    check("region 1's own net is absent from the score entirely",
          2 not in res.net_paths and 2 not in {n for n, _ in res.failed})
    check("the run says which region it covered and what it dropped",
          any("routing region 0 of 2" in w and "4 pads" in w
              for w in res.region_warnings), str(res.region_warnings))
    check("it still names the spanning nets",
          res.cross_region_nets == {3: [0, 1]}, str(res.cross_region_nets))
    ys = [lat.node_xy_mm(v)[1] for p in res.net_paths[3] for v in p]
    check("the shared net's copper here is region 0's half only",
          max(ys) < GAP_Y[0], f"max y {max(ys):.2f}")

    _b, _l, r1 = route_board(board, pitch_mm=0.6,
                             layer_names=["F.Cu", "B.Cu"],
                             region_index=1, max_iters=12, refine_passes=0)
    check("region 1 routes its own nets", set(r1.net_paths) == {2, 3},
          str(sorted(r1.net_paths)))

    try:
        route_board(board, pitch_mm=0.6, region_index=7, max_iters=1)
        check("an out-of-range region index is refused", False)
    except ValueError as e:
        check("an out-of-range region index is refused loudly",
              "out of range" in str(e) and "2 outline region(s)" in str(e),
              str(e))


# ── 4. terminal-served spanning nets (the "virtual plane") ───────────────
def test_terminal_served_spanning_net_flies_over_the_gap():
    print("=== a declared net is joined by WIRE, not copper across the gap ===")
    from pathfinder import route_board
    lo, hi = GAP_Y

    def crosses(t):     # track (x1, y1, x2, y2) enters the between-boards band
        return (min(t[1], t[3]) < hi - 1e-9) and (max(t[1], t[3]) > lo + 1e-9)

    # Explicit: serve HVSHARED (net 4) by wire; leave everything else copper.
    brd, lat, res = route_board(terminal_panel(), pitch_mm=0.6,
                                layer_names=["F.Cu", "B.Cu"],
                                terminal_nets=["HVSHARED"],
                                max_iters=12, refine_passes=0)
    terms = res.terminals or []
    hv = [t for t in terms if t[2] == 4]
    check("HVSHARED got wire-landing terminals", len(hv) >= 2, str(terms))
    check("its tight per-board clusters collapse to ONE landing each",
          len(hv) == 2,
          f"{len(hv)} landings: {[(round(t[0], 1), round(t[1], 1)) for t in hv]}")
    check("a landing sits on each board (one below the gap, one above)",
          any(t[1] < lo for t in hv) and any(t[1] > hi for t in hv),
          str([round(t[1], 1) for t in hv]))
    check("the terminals carry the wire hole (2.0 mm pad / 1.0 mm drill)",
          all(abs(t[3] - 2.0) < 1e-9 and abs(t[4] - 1.0) < 1e-9 for t in hv),
          str([(t[3], t[4]) for t in hv]))
    check("only the DECLARED net is served (SHARED stays copper)",
          not any(t[2] == 3 for t in terms), str([t[2] for t in terms]))
    check("HVSHARED is fully routed — every pad reaches a landing",
          4 in res.net_paths and 4 not in {n for n, _ in res.failed},
          str(res.failed))
    bad = [t for t in (res.tracks or []) if crosses(t)]
    check("no HVSHARED copper crosses the 25 mm band", not bad,
          f"{len(bad)} crossing seg(s)")
    check("the MST->star contract is reported to the user",
          any("TERMINAL-SERVED net HVSHARED" in w and "star" in w
              for w in res.region_warnings), str(res.region_warnings))

    # Opt-in auto: every cross-region net is served, no list needed.
    _b, _l, res2 = route_board(terminal_panel(), pitch_mm=0.6,
                               layer_names=["F.Cu", "B.Cu"],
                               terminal_auto_spanning=True,
                               max_iters=12, refine_passes=0)
    served = {t[2] for t in (res2.terminals or [])}
    check("--terminal-auto-spanning serves EVERY spanning net (3 and 4)",
          served == {3, 4}, str(served))

    # Control: no terminal request -> no terminals, behaviour unchanged.
    _b3, _l3, res3 = route_board(terminal_panel(), pitch_mm=0.6,
                                 layer_names=["F.Cu", "B.Cu"],
                                 max_iters=12, refine_passes=0)
    check("no terminal request means no terminals and no terminal report",
          not res3.terminals and not any(
              "TERMINAL-SERVED" in w for w in (res3.region_warnings or [])),
          str(res3.terminals))


def main():
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    test_regions_are_detected()
    test_spanning_nets_are_named()
    test_gap_is_blocked_in_the_lattice()
    test_no_copper_crosses_the_gap()
    test_route_board_warns_loudly()
    test_region_index_routes_one_board()
    test_terminal_served_spanning_net_flies_over_the_gap()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
