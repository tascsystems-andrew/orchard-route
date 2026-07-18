"""Writeback validation: route a real board, write the copy, prove KiCad eats it.

The fixture is Andrew's live hifi tube pre board (READ-ONLY — never write to
it; a before/after hash enforces that at the end). Three independent judges
look at the routed copy:

1. board.py re-parses it — track/via counts must be original + emitted, and
   every appended segment's net NAME must resolve to a positive code in the
   reparsed net table (KiCad 10 files carry names only, so a name that fails
   to resolve silently becomes net 0 — the exact bug this catches).
2. kicad-cli runs a real DRC on it — exit 0 proves pcbnew's own parser loads
   the file. DRC *violations* are expected (pitch-1.0 lattice copper ignores
   clearance classes) and reported as diagnostics, not failures; only a load
   error in stderr fails the test.
3. sha256 of the original file must be identical before and after.

Per-net widths get three more judges, on the same routed geometry (no
re-route): the sibling hifi .kicad_pro drives load_net_class_widths and the
result must match the JSON's own classes (or fall back cleanly if the file
ever disappears); a --width-map style override + pitch cap is applied
programmatically, a second copy written, and the reparsed per-net
track/via numbers must equal the resolved triples; the Voxy-arduino
.kicad_pro exercises the parse-only path against synthetic net names (no
Voxy route — far too slow here). A synthetic .kicad_pro written to out/
covers what the live projects don't contain: netclass_patterns globs,
explicit assignments beating patterns, priority, and per-key fallback to
the Default class. The .kicad_pro files are hash-guarded like the board.

Run: .venv/bin/python test_writeback.py
"""
import hashlib
import json
import os
import subprocess

from board import load_board
from pathfinder import route_board, paths_to_tracks
from writeback import (write_routed_copy, project_file_for,
                       load_net_class_widths, parse_width_map,
                       apply_width_map, cap_track_widths,
                       DEFAULT_TRACK_MM, DEFAULT_VIA_MM, DEFAULT_DRILL_MM)

BOARD = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"
VOXY_PRO = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pro"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
OUT = os.path.join(OUT_DIR, "hifi-routed.kicad_pcb")
OUT_W = os.path.join(OUT_DIR, "hifi-routed-widths.kicad_pcb")
FIXTURE_PRO = os.path.join(OUT_DIR, "classes-fixture.kicad_pro")
DRC_RPT = os.path.join(OUT_DIR, "hifi-routed-drc.rpt")
KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_drc():
    """kicad-cli pcb drc on the routed copy. Returns completed process."""
    return subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--output", DRC_RPT, OUT],
        capture_output=True, text=True, timeout=120)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in (OUT, OUT_W, FIXTURE_PRO, DRC_RPT):
        assert os.path.abspath(p).startswith(OUT_DIR + os.sep), \
            f"output {p} escapes {OUT_DIR}"
        if os.path.exists(p):
            os.remove(p)
    hash_before = sha256(BOARD)
    ro_pros = [p for p in (project_file_for(BOARD), VOXY_PRO)
               if p and os.path.isfile(p)]
    pro_hashes = {p: sha256(p) for p in ro_pros}

    print("=== route + write ===")
    brd, lat, res = route_board(BOARD, pitch_mm=1.0,
                                layer_names=["F.Cu", "B.Cu"])
    tracks, vias = paths_to_tracks(lat, res.net_paths)
    check(len(tracks) > 0, f"router emitted tracks ({len(tracks)})")
    check(not res.failed, f"no failed nets ({len(res.failed)} failed)")
    write_routed_copy(BOARD, OUT, tracks, vias, brd.nets)
    check(os.path.exists(OUT), f"routed copy written to {OUT}")

    print("=== re-parse with board.load_board ===")
    rb = load_board(OUT)
    check(len(rb.tracks) == len(brd.tracks) + len(tracks),
          f"track count {len(rb.tracks)} == "
          f"{len(brd.tracks)} original + {len(tracks)} emitted")
    check(len(rb.vias) == len(brd.vias) + len(vias),
          f"via count {len(rb.vias)} == "
          f"{len(brd.vias)} original + {len(vias)} emitted")
    # Appended nodes parse after the originals (file order), so the tail of
    # rb.tracks lines up 1:1 with what paths_to_tracks emitted.
    new = rb.tracks[len(brd.tracks):]
    bad = [(i, rb.nets.get(t.net_code), brd.nets[tracks[i][5]])
           for i, t in enumerate(new)
           if t.net_code <= 0 or rb.nets.get(t.net_code) != brd.nets[tracks[i][5]]]
    check(not bad,
          f"all {len(new)} appended segments resolve to their net name in the "
          f"reparsed table" + (f" (first mismatch {bad[0]})" if bad else ""))
    new_v = rb.vias[len(brd.vias):]
    bad_v = [i for i, v in enumerate(new_v)
             if v.net_code <= 0 or rb.nets.get(v.net_code) != brd.nets[vias[i][2]]]
    check(not bad_v, f"all {len(new_v)} appended vias resolve to their net name")
    check(all(abs(v.size_mm - 0.6) < 1e-9 and abs(v.drill_mm - 0.3) < 1e-9
              for v in new_v), "appended vias carry size 0.6 / drill 0.3")
    check(all(abs(t.width_mm - DEFAULT_TRACK_MM) < 1e-9 for t in new),
          f"no widths arg -> appended segments keep the "
          f"{DEFAULT_TRACK_MM} default")

    print("=== kicad-cli DRC (ground truth parser) ===")
    proc = run_drc()
    check(proc.returncode == 0,
          f"kicad-cli exit code {proc.returncode} == 0 (file loads)")
    load_errs = [l for l in proc.stderr.splitlines()
                 if any(k in l.lower() for k in
                        ("error loading", "failed to load", "parse", "expecting"))]
    check(not load_errs,
          "no load/parse errors on stderr"
          + (f" (first: {load_errs[0]!r})" if load_errs else ""))
    check(os.path.exists(DRC_RPT), f"DRC report written to {DRC_RPT}")
    if os.path.exists(DRC_RPT):
        with open(DRC_RPT, encoding="utf-8") as f:
            summary = [l.strip() for l in f if l.startswith("**")]
        print("  DRC summary (violations are diagnostic, not failures):")
        for line in summary:
            print(f"    {line}")

    print("=== net classes from the hifi .kicad_pro ===")
    pro = project_file_for(BOARD)
    if pro:
        check(True, f"sibling project file found: {os.path.basename(pro)}")
        with open(pro, encoding="utf-8") as f:
            ns = json.load(f).get("net_settings") or {}
        by_name = {c.get("name"): c for c in ns.get("classes") or []}
        proj_widths = load_net_class_widths(pro, brd.nets)
        check(set(proj_widths) == set(brd.nets),
              "loader returns a triple for every net code")
        if by_name and not (ns.get("netclass_assignments")
                            or ns.get("netclass_patterns")):
            # the live Voxy-family projects: one Default class, no maps —
            # every net must resolve to the Default class's own numbers
            d = by_name.get("Default", {})
            expect = (float(d.get("track_width") or DEFAULT_TRACK_MM),
                      float(d.get("via_diameter") or DEFAULT_VIA_MM),
                      float(d.get("via_drill") or DEFAULT_DRILL_MM))
            check(all(w == expect for w in proj_widths.values()),
                  f"all nets carry the project Default class {expect}")
    else:
        proj_widths = {}
        check(True, "no sibling .kicad_pro -> clean fallback to defaults")

    print("=== Voxy-arduino .kicad_pro, parse-only ===")
    if os.path.isfile(VOXY_PRO):
        with open(VOXY_PRO, encoding="utf-8") as f:
            vd_cls = {c.get("name"): c for c in
                      (json.load(f).get("net_settings") or {})
                      .get("classes") or []}.get("Default", {})
        # synthetic net table: the point is the parsing path, not a route
        fake_nets = {0: "", 1: "GND", 2: "B+250", 3: "Net-(U1-Pad4)"}
        vw = load_net_class_widths(VOXY_PRO, fake_nets)
        v_expect = (float(vd_cls.get("track_width") or DEFAULT_TRACK_MM),
                    float(vd_cls.get("via_diameter") or DEFAULT_VIA_MM),
                    float(vd_cls.get("via_drill") or DEFAULT_DRILL_MM))
        check(set(vw) == set(fake_nets) and all(w == v_expect
                                                for w in vw.values()),
              f"Voxy project parses; every net -> Default class {v_expect}")
    else:
        check(True, "Voxy .kicad_pro absent — parse-only check skipped")

    print("=== synthetic .kicad_pro: patterns, assignments, priority ===")
    with open(FIXTURE_PRO, "w", encoding="utf-8") as f:
        json.dump({"net_settings": {
            "classes": [
                {"name": "Default", "track_width": 0.2,
                 "via_diameter": 0.6, "via_drill": 0.3,
                 "priority": 2147483647},
                {"name": "Power", "track_width": 0.8,
                 "via_diameter": 0.9, "via_drill": 0.45, "priority": 0},
                {"name": "Fat", "track_width": 1.5, "priority": 1},
            ],
            "netclass_assignments": {"sig_in": ["Fat"]},
            "netclass_patterns": [
                {"pattern": "B+*", "netclass": "Power"},
                {"pattern": "sig_*", "netclass": "Power"},
                {"pattern": "GND", "netclass": "Power"},
                {"pattern": "G*", "netclass": "Fat"},
            ]}}, f)
    syn = load_net_class_widths(
        FIXTURE_PRO, {0: "", 1: "GND", 2: "B+250", 3: "sig_in"})
    check(syn[1] == (0.8, 0.9, 0.45),
          "GND matches two patterns; Power (priority 0) beats Fat (1)")
    check(syn[2] == (0.8, 0.9, 0.45), "glob pattern B+* -> Power class")
    check(syn[3] == (1.5, 0.6, 0.3),
          "explicit assignment beats matching pattern; missing via keys "
          "fall back to the Default class")
    check(syn[0] == (0.2, 0.6, 0.3), "unmatched net -> Default class")
    for bad in ("GND", "GND=", "=0.5", "GND=0.5:0.6", "GND=fat", "GND=-1"):
        try:
            parse_width_map(bad)
            check(False, f"parse_width_map rejects {bad!r}")
        except ValueError:
            check(True, f"parse_width_map rejects {bad!r}")

    print("=== width-map override + pitch cap -> second routed copy ===")
    # same routed geometry, new widths. This board routes a single net (the
    # other nets are single-pad), so the ROUTED net takes an over-pitch
    # override — proving the cap flows through to emitted copper — and an
    # unrouted-but-present net takes a second over-pitch entry to show the
    # cap names every offender, geometry or not.
    routed = sorted({t[5] for t in tracks} | {v[2] for v in vias})
    check(len(routed) >= 1, f"board routes >= 1 net ({len(routed)})")
    code_a = routed[0]
    name_a = brd.nets[code_a]
    others = [c for c, n in brd.nets.items() if c not in routed and n]
    check(len(others) >= 1, f"board has an unrouted named net ({len(others)})")
    code_b = others[0]
    name_b = brd.nets[code_b]
    entries = parse_width_map(f"{name_a}=1.2:0.9:0.45,{name_b}=1.5")
    widths = apply_width_map(proj_widths, brd.nets, entries)
    check(widths[code_a] == (1.2, 0.9, 0.45),
          f"{name_a} overridden to 1.2/0.9/0.45")
    check(widths[code_b][0] == 1.5, f"{name_b} overridden to 1.5")
    widths, capped = cap_track_widths(widths, brd.nets, 1.0)  # test pitch
    check(capped == sorted([name_a, name_b]),
          f"cap at pitch 1.0 names exactly the offenders ({capped})")
    check(widths[code_a] == (1.0, 0.9, 0.45),
          f"{name_a} track capped to 1.0, via override untouched")
    check(widths[code_b][0] == 1.0, f"{name_b} track width capped to 1.0")
    uncapped = [brd.nets[c] for c, w in widths.items()
                if c not in (code_a, code_b) and w[0] > 1.0]
    check(not uncapped, "no other net exceeds the cap")
    write_routed_copy(BOARD, OUT_W, tracks, vias, brd.nets, widths=widths)
    rw = load_board(OUT_W)
    fallback = (DEFAULT_TRACK_MM, DEFAULT_VIA_MM, DEFAULT_DRILL_MM)
    new_t = rw.tracks[len(brd.tracks):]
    bad_w = [(i, t.width_mm) for i, t in enumerate(new_t)
             if abs(t.width_mm - widths.get(tracks[i][5], fallback)[0]) > 1e-9]
    check(len(new_t) == len(tracks) and not bad_w,
          f"all {len(new_t)} reparsed segments carry their net's resolved "
          f"width" + (f" (first mismatch {bad_w[0]})" if bad_w else ""))
    new_vw = rw.vias[len(brd.vias):]
    bad_vw = [(i, v.size_mm, v.drill_mm) for i, v in enumerate(new_vw)
              if abs(v.size_mm - widths.get(vias[i][2], fallback)[1]) > 1e-9
              or abs(v.drill_mm - widths.get(vias[i][2], fallback)[2]) > 1e-9]
    check(len(new_vw) == len(vias) and not bad_vw,
          f"all {len(new_vw)} reparsed vias carry their net's resolved "
          f"size/drill" + (f" (first mismatch {bad_vw[0]})" if bad_vw else ""))

    print("=== original untouched ===")
    check(sha256(BOARD) == hash_before,
          "original board bytes identical before and after")
    for p, h in pro_hashes.items():
        check(sha256(p) == h,
              f"{os.path.basename(p)} bytes identical before and after")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
