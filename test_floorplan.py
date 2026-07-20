"""L6 floorplan validation: the color-by-group cohesion score + render (§2).

Four properties, all easy to fake and hard to notice faking, so each is checked
on a hand-built board small enough to reason about:

1. FILL FRACTION separates a tight group from a scattered one (the confetti
   detector), and never calls a scattered group cohesive.
2. OVERLAP is flagged: a group whose courtyards overlap reports fill > 1 and is
   listed as overlapping, not as "extra cohesive".
3. WIRELENGTH splits inter- vs intra-group correctly, and the busiest-seam graph
   names the crossing group pairs.
4. COLOR is by family when given (same family -> one color) else per group; and
   the render is well-formed SVG carrying those colors.

Run: .venv/bin/python test_floorplan.py
"""
import os
import sys
import tempfile

import floorplan as F

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def _fp(ref, x, y, *pad_nets):
    """A footprint at (x, y) with one 2x2 mm pad per (net_code) given."""
    pads = "".join(
        f'\t\t(pad "{i + 1}" smd rect (at 0 0) (size 2 2) (layers "F.Cu") '
        f'(net {n} "N{n}"))\n' for i, n in enumerate(pad_nets))
    return (f'\t(footprint "R" (layer "F.Cu") (at {x} {y})\n'
            f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'{pads}\t)\n')


def _board(path):
    """100x100 board. tight T1..T4 clustered at ~(11,11), 3 mm apart so their
    2.5 mm courtyards are close but do NOT overlap (a valid tight block); loose
    L1..L4 in the four corners; stacked D1,D2 on the SAME point (overlap);
    ungrouped U1. Nets: 1 = intra-tight (T1-T2), 2 = inter (T1..loose L1),
    3 = intra-loose."""
    fps = [
        _fp("T1", 10, 10, 1, 2), _fp("T2", 13, 10, 1), _fp("T3", 10, 13),
        _fp("T4", 13, 13),
        _fp("L1", 5, 5, 2, 3), _fp("L2", 95, 5, 3), _fp("L3", 5, 95),
        _fp("L4", 95, 95),
        _fp("D1", 50, 50), _fp("D2", 50, 50),        # exactly stacked -> overlap
        _fp("U1", 70, 20),                            # ungrouped
    ]
    nets = "".join(f'\t(net {i} "N{i}")\n' for i in range(4))
    with open(path, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n' + nets +
                '\t(gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") '
                '(width 0.1))\n' + "".join(fps) + ")\n")


PARTITION = {"groups": [
    {"name": "tight", "refs": ["T1", "T2", "T3", "T4"], "family": "amp"},
    {"name": "loose", "refs": ["L1", "L2", "L3", "L4"], "family": "amp"},
    {"name": "stacked", "refs": ["D1", "D2"], "family": "hv"},
]}


def _mini(path, parts):
    """A 200x100 board with the given (ref, x, y, *net_codes) footprints."""
    fps = "".join(_fp(*p) for p in parts)
    codes = sorted({n for p in parts for n in p[3:]})
    nets = "".join(f'\t(net {c} "N{c}")\n' for c in codes)
    with open(path, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n\t(net 0 "")\n'
                + nets +
                '\t(gr_rect (start 0 0) (end 200 100) (layer "Edge.Cuts") '
                '(width 0.1))\n' + fps + ")\n")


def _sheet_board(path):
    """Two disjoint 40x40 areas with a gap. Sheet /a/ has parts in BOTH areas
    (a sheet split across boards — the smell); /b/ sits entirely in area 0."""
    def fp(ref, x, y, sheet):
        return (f'\t(footprint "R" (layer "F.Cu") (at {x} {y})\n'
                f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
                f'\t\t(sheetname "{sheet}")\n'
                f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") '
                f'(net 0 "")))\n')
    with open(path, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n\t(net 0 "")\n'
                '\t(gr_rect (start 0 0) (end 40 40) (layer "Edge.Cuts") (width 0.1))\n'
                '\t(gr_rect (start 60 0) (end 100 40) (layer "Edge.Cuts") (width 0.1))\n'
                + fp("A1", 10, 10, "/a/") + fp("A2", 80, 10, "/a/")
                + fp("B1", 20, 20, "/b/") + fp("B2", 30, 30, "/b/") + ")\n")


def main():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "b.kicad_pcb")
    _board(src)
    data = F.cohesion(src, PARTITION)
    g = data["groups"]

    print("=== 1. fill fraction: tight >> loose (confetti detector) ===")
    check(g["tight"]["fill_fraction"] > 5 * g["loose"]["fill_fraction"],
          f"tight ({g['tight']['fill_fraction']}) is far tighter than loose "
          f"({g['loose']['fill_fraction']})")
    check(g["tight"]["bbox_area_mm2"] < g["loose"]["bbox_area_mm2"] / 10,
          "the tight group's bbox is a small fraction of the loose one's")
    check((frac := data["loosest_groups"][0])[1] == "loose",
          f"the loosest VALID group is named as loose ({frac})")
    expect_mean = round((g["tight"]["fill_fraction"]
                         + g["loose"]["fill_fraction"]) / 2, 3)
    check(data["mean_fill_fraction"] == expect_mean,
          f"mean_fill_fraction is the mean of the VALID groups only "
          f"({data['mean_fill_fraction']} == {expect_mean}; overlapping "
          f"'stacked' excluded)")

    print("\n=== 2. overlap is flagged, not read as cohesion ===")
    check(g["stacked"]["piled"] and g["stacked"]["overlap_pairs"] > 0
          and g["stacked"]["fill_fraction"] > 1.0,
          f"stacked parts: fill > 1, piled=True, overlap detected "
          f"({g['stacked']['fill_fraction']}, {g['stacked']['overlap_pairs']} pair)")
    check(data["n_overlapping_groups"] == 1
          and data["overlapping_groups"] == ["stacked"]
          and data["n_piled_groups"] == 1,
          f"exactly the stacked group is flagged overlapping AND piled "
          f"({data['overlapping_groups']} / {data['piled_groups']})")
    check(not g["tight"]["overlap_pairs"] and not g["loose"]["overlap_pairs"]
          and not g["tight"]["piled"],
          "the tight/loose groups are NOT flagged overlapping or piled")
    check(all(name != "stacked" for _f, name in data["loosest_groups"]),
          "a PILED group is excluded from the cohesion (loosest) stats")

    print("\n=== 3. inter- vs intra-group wirelength ===")
    # net 1 = T1-T2 (both tight -> intra); net 2 = T1-L1 (tight<->loose ->
    # inter); net 3 = L1-L2 (both loose -> intra).
    check(data["inter_group_nets"] == 1 and data["intra_group_nets"] == 2,
          f"1 inter-group net (T1-L1), 2 intra ({data['inter_group_nets']} / "
          f"{data['intra_group_nets']})")
    check("loose ↔ tight" in data["group_graph"]
          and data["group_graph"]["loose ↔ tight"] == 1,
          f"the group graph names the tight<->loose crossing "
          f"({data['group_graph']})")
    check(data["inter_group_wirelength_mm"] > 0
          and data["intra_group_wirelength_mm"] > 0,
          "both wirelength figures are populated")

    print("\n=== 4. color by family, then render ===")
    c = F._colors(["tight", "loose", "stacked"],
                  {"tight": "amp", "loose": "amp", "stacked": "hv"})
    check(c["tight"] == c["loose"] and c["tight"] != c["stacked"],
          f"same family shares a color, different family differs "
          f"({c['tight']}/{c['loose']}/{c['stacked']})")
    c2 = F._colors(["a", "b"], {"a": "a", "b": "b"})
    check(c2["a"] != c2["b"], "with no family, each group gets its own color")

    svg = os.path.join(d, "b-floorplan.svg")
    F.render(src, PARTITION, svg, data=data)
    with open(svg, encoding="utf-8") as f:
        txt = f.read()
    check(txt.startswith("<svg") and txt.rstrip().endswith("</svg>"),
          "render writes well-formed SVG")
    check(c["tight"] in txt and "OVERLAP" in txt,
          "the SVG carries the group color and flags the overlapping group")
    # ungrouped U1 must not appear in any group's stats
    check("U1" not in str(data["groups"]),
          "an ungrouped part is excluded from the group metrics")

    print("\n=== 5. sheet-derived grouping + split-across-areas lint ===")
    from place import parts_from_board
    sb = os.path.join(d, "s.kicad_pcb")
    _sheet_board(sb)
    sp = F._sheets_partition(parts_from_board(sb))
    names = sorted(g["name"] for g in sp["groups"])
    check(names == ["/a/", "/b/"],
          f"each schematic sheet becomes a group, zero partition.json ({names})")
    check(sorted(next(g["refs"] for g in sp["groups"] if g["name"] == "/a/"))
          == ["A1", "A2"],
          "the sheet's parts are gathered onto its group")
    sd = F.cohesion(sb, sp)
    check(sd["groups"]["/a/"]["spans_areas"]
          and sd["groups"]["/a/"]["areas"] == [0, 1],
          f"a sheet with parts in two areas is flagged spans_areas "
          f"({sd['groups']['/a/']['areas']})")
    check(not sd["groups"]["/b/"]["spans_areas"]
          and sd["groups"]["/b/"]["areas"] == [0],
          "a sheet contained in one area is NOT flagged")
    check([n for n, _a in sd["groups_split_across_areas"]] == ["/a/"],
          f"exactly the split sheet is reported "
          f"({sd['groups_split_across_areas']})")

    print("\n=== 6. review fixes: exact overlap, ungrouped WL, validation, escaping ===")
    # (a) EXACT overlap: P1/P2 overlap but P3 spreads the bbox so Σcourtyard <
    #     bbox — the old area-ratio test missed this; place._overlap catches it.
    ob = os.path.join(d, "ov.kicad_pcb")
    _mini(ob, [("P1", 50, 50, 1), ("P2", 50.3, 50, 1), ("P3", 100, 50, 1)])
    od = F.cohesion(ob, {"groups": [{"name": "g", "refs": ["P1", "P2", "P3"]}]})
    gg = od["groups"]["g"]
    check(gg["fill_fraction"] <= 1.0 and gg["overlap_pairs"] == 1
          and not gg["piled"] and od["n_overlapping_groups"] == 1
          and od["n_piled_groups"] == 0,
          f"a group that overlaps AND spreads (fill {gg['fill_fraction']} <= 1) "
          f"is flagged overlapping (1 pair) but NOT dropped as piled")

    # (b) ungrouped parts don't inflate a group's wirelength: net G1-U1 where U1
    #     is ungrouped and 180 mm away must NOT add to the group's tightness.
    wb = os.path.join(d, "wl.kicad_pcb")
    _mini(wb, [("G1", 10, 10, 1, 2), ("G2", 12, 10, 1), ("U1", 190, 10, 2)])
    wd = F.cohesion(wb, {"groups": [{"name": "g", "refs": ["G1", "G2"]}]})
    check(wd["intra_group_wirelength_mm"] < 5.0,
          f"a net from the group to a far UNGROUPED part does not inflate intra "
          f"wirelength (got {wd['intra_group_wirelength_mm']} mm)")

    # (c) a partition ref not on the board fails loud (not silently dropped).
    try:
        F.cohesion(wb, {"groups": [{"name": "g", "refs": ["G1", "NOPE"]}]})
        check(False, "a bogus partition ref should raise")
    except ValueError as e:
        check("NOPE" in str(e),
              f"a partition ref not on the board fails loud ({str(e)[:48]})")

    # (d) SVG escaping: a group name with XML metacharacters must yield valid XML
    #     (dropping _esc makes minidom.parseString raise — the real guard).
    import xml.dom.minidom as MD
    eb = os.path.join(d, "esc.kicad_pcb")
    _mini(eb, [("P1", 10, 10, 1)])
    esvg = os.path.join(d, "esc.svg")
    F.render(eb, {"groups": [{"name": 'a<b>&"z"', "refs": ["P1"]}]}, esvg)
    raw = open(esvg, encoding="utf-8").read()
    try:
        MD.parseString(raw)
        parsed_ok = True
    except Exception:                              # noqa: BLE001
        parsed_ok = False
    check(parsed_ok and "&lt;b&gt;" in raw,
          "a group name with <, >, & renders as valid escaped XML")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
