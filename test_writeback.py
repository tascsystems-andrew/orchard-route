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

write_moved_copy gets the same three-judge treatment on the SAME hifi board
(no route needed): footprints are moved AND rotated in a copy, board.py
re-parses it and every pad of every moved footprint must land at the
delta-transformed position/rotation to 1e-6 (one anchor is hand-computed,
not derived), kicad-cli must load the copy, unmoved pads and the net table
must be untouched, and the refusal guard must hold. The KiCad 5 (module ...)
form is covered twice: a synthetic minimal board written under out/ (always
runs, also covers a pad with no (at) node), and the bench pico-vga fixture
when present. `--moves-only` runs ONLY these CPU-side sections — no
pathfinder import, no routing, no GPU — for workflows that must not touch
the router while it is being rebuilt.

Run: .venv/bin/python test_writeback.py [--moves-only]
"""
import hashlib
import json
import math
import os
import subprocess
import sys

from board import load_board
from writeback import (write_routed_copy, write_moved_copy, board_footprints,
                       resolve_footprint, project_file_for,
                       load_net_class_widths, load_net_class_names,
                       parse_width_map, apply_width_map, cap_track_widths,
                       DEFAULT_TRACK_MM, DEFAULT_VIA_MM, DEFAULT_DRILL_MM)

BOARD = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"
VOXY_PRO = "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pro"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
OUT = os.path.join(OUT_DIR, "hifi-routed.kicad_pcb")
OUT_W = os.path.join(OUT_DIR, "hifi-routed-widths.kicad_pcb")
FIXTURE_PRO = os.path.join(OUT_DIR, "classes-fixture.kicad_pro")
DRC_RPT = os.path.join(OUT_DIR, "hifi-routed-drc.rpt")
KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"

PICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench",
                    "boards", "rpi-pico-vga", "pico_vga_sd_aud.kicad_pcb")
OUT_M = os.path.join(OUT_DIR, "hifi-moved.kicad_pcb")
OUT_M5 = os.path.join(OUT_DIR, "pico-moved.kicad_pcb")
DRC_M_RPT = os.path.join(OUT_DIR, "hifi-moved-drc.rpt")
# the synthetic KiCad 5 source lives in its OWN subdir so writing its moved
# copy to out/ does not trip the same-directory refusal
SYN_BOARD = os.path.join(OUT_DIR, "synthetic-src", "module-form.kicad_pcb")
OUT_MSYN = os.path.join(OUT_DIR, "synthetic-moved.kicad_pcb")

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def run_drc(target=OUT, rpt=DRC_RPT):
    """kicad-cli pcb drc on a copy. Returns completed process."""
    return subprocess.run(
        [KICAD_CLI, "pcb", "drc", "--output", rpt, target],
        capture_output=True, text=True, timeout=120)


def rot(x, y, deg):
    """KiCad rotation (CCW, Y-down) — local math, independent of the code
    under test, mirroring test_board.py's hand derivation."""
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c + y * s, -x * s + y * c


def check_moved_board(src_path, moved_path, placements, label):
    """Re-parse a moved copy and judge every pad of every moved footprint
    against the delta-transform of the ORIGINAL parse, to 1e-6. Also proves
    unmoved pads, the net table, and track/via counts are untouched."""
    brd = load_board(src_path)
    with open(src_path, encoding="utf-8") as f:
        recs = board_footprints(f.read())
    mb = load_board(moved_path)
    check(len(mb.pads) == len(brd.pads) and mb.nets == brd.nets
          and len(mb.tracks) == len(brd.tracks)
          and len(mb.vias) == len(brd.vias),
          f"{label}: pad/track/via counts and net table unchanged")
    with open(moved_path, encoding="utf-8") as f:
        mrecs = board_footprints(f.read())

    offsets, off = {}, 0
    for r in recs:
        offsets[r.uref] = off
        off += r.n_pads
    moved_urefs = set()
    for key, (nx, ny, nrot) in placements.items():
        r_old = resolve_footprint(recs, key)
        r_new = resolve_footprint(mrecs, key)
        moved_urefs.add(r_old.uref)
        check(abs(r_new.x_mm - nx) < 1e-6 and abs(r_new.y_mm - ny) < 1e-6
              and abs((r_new.rot_deg - nrot) % 360.0) % 360.0 < 1e-6,
              f"{label}: {key} at requested ({nx}, {ny}, {nrot}) "
              f"(got {r_new.x_mm}, {r_new.y_mm}, {r_new.rot_deg})")
        delta = nrot - r_old.rot_deg
        o = offsets[r_old.uref]
        bad = []
        for i in range(r_old.n_pads):
            p_old, p_new = brd.pads[o + i], mb.pads[o + i]
            lx, ly = rot(p_old.x_mm - r_old.x_mm, p_old.y_mm - r_old.y_mm,
                         -r_old.rot_deg)
            wx, wy = rot(lx, ly, nrot)
            ex, ey = nx + wx, ny + wy
            er = (p_old.rotation_deg + delta) % 360.0
            da = abs((p_new.rotation_deg - er) % 360.0)
            if abs(p_new.x_mm - ex) > 1e-6 or abs(p_new.y_mm - ey) > 1e-6 \
                    or min(da, 360.0 - da) > 1e-6:
                bad.append((i, (p_new.x_mm, p_new.y_mm, p_new.rotation_deg),
                            (ex, ey, er)))
        check(not bad, f"{label}: all {r_old.n_pads} pads of {key} "
                       f"delta-transformed to 1e-6"
                       + (f" (first mismatch {bad[0]})" if bad else ""))
        check(all(p_new.net_name == p_old.net_name and
                  p_new.layers == p_old.layers and
                  p_new.width_mm == p_old.width_mm and
                  p_new.height_mm == p_old.height_mm
                  for p_old, p_new in zip(brd.pads[o:o + r_old.n_pads],
                                          mb.pads[o:o + r_old.n_pads])),
              f"{label}: {key} pads keep net/layers/true size")
    untouched = [i for r in recs if r.uref not in moved_urefs
                 for i in range(offsets[r.uref], offsets[r.uref] + r.n_pads)]
    bad_u = [i for i in untouched
             if (brd.pads[i].x_mm, brd.pads[i].y_mm,
                 brd.pads[i].rotation_deg)
             != (mb.pads[i].x_mm, mb.pads[i].y_mm, mb.pads[i].rotation_deg)]
    check(not bad_u, f"{label}: all {len(untouched)} pads of unmoved "
                     f"footprints byte-identical"
          + (f" (first drift at pad {bad_u[0]})" if bad_u else ""))
    return mb


def run_move_tests():
    print("=== write_moved_copy: move + rotate on the hifi board ===")
    hash_hifi = sha256(BOARD)
    with open(BOARD, encoding="utf-8") as f:
        recs = board_footprints(f.read())
    check(len(recs) == 116 and sum(r.n_pads for r in recs) == 230,
          f"scanner sees 116 footprints / 230 pads "
          f"({len(recs)} / {sum(r.n_pads for r in recs)})")
    try:
        resolve_footprint(recs, "5755")
        check(False, "duplicate plain ref refused")
    except ValueError as e:
        check("disambiguate as 5755#1 .. 5755#3" in str(e),
              f"duplicate plain ref refused with disambiguators ({e})")
    try:
        resolve_footprint(recs, "R999")
        check(False, "unknown ref refused")
    except ValueError as e:
        check("not found" in str(e), f"unknown ref refused ({e})")

    # three moves: the -90 valve rotated to 180 (rotation delta 270), an
    # unrotated footprint rotated to 90 (angle appears), and a 180-rotated
    # test pin rotated to 0 (fp AND pad angle must be OMITTED again)
    unrot = next(r for r in recs if r.rot_deg == 0.0 and r.n_pads >= 1)
    pin180 = next(r for r in recs if r.rot_deg == 180.0 and r.n_pads == 1)
    placements = {
        "5755#1": (120.0, 60.0, 180.0),
        unrot.uref: (unrot.x_mm + 2.5, unrot.y_mm - 1.75, 90.0),
        pin180.uref: (pin180.x_mm + 4.0, pin180.y_mm, 0.0),
    }
    write_moved_copy(BOARD, OUT_M, placements)
    check(os.path.exists(OUT_M), f"moved copy written to {OUT_M}")
    mb = check_moved_board(BOARD, OUT_M, placements, "hifi")

    # hand-computed anchor (NOT derived through any parser): valve pad 1 is
    # (at 1.790008 2.351225 216) in-file; at (120, 60) rot 180 that is
    # (120 - 1.790008, 60 - 2.351225) and angle 216 + 270 - 360 = 126
    anchor = [p for p in mb.pads
              if abs(p.x_mm - 118.209992) < 1e-6
              and abs(p.y_mm - 57.648775) < 1e-6
              and abs(p.rotation_deg - 126.0) < 1e-6]
    check(len(anchor) == 1,
          "hand-computed anchor pad at (118.209992, 57.648775) rot 126")
    with open(OUT_M, encoding="utf-8") as f:
        mtext = f.read()
    check("(at 120 60 180)" in mtext, "valve (at 120 60 180) written")

    def fmt(v):   # pcbnew's number style, local copy for the text probe
        return f"{round(v, 6):.6f}".rstrip("0").rstrip(".") or "0"

    check(f"(at {fmt(pin180.x_mm + 4.0)} {fmt(pin180.y_mm)})" in mtext,
          "rot->0 footprint's (at ...) omits the zero angle, as pcbnew does")
    pin_new = resolve_footprint(board_footprints(mtext), pin180.uref)
    check(pin_new.pad_ats[0][2][2:] == (),
          "rot->0 test pin's pad (at ...) omits the zero angle too")

    print("=== kicad-cli loads the moved copy ===")
    proc = run_drc(OUT_M, DRC_M_RPT)
    check(proc.returncode == 0,
          f"kicad-cli exit code {proc.returncode} == 0 (file loads)")
    load_errs = [l for l in proc.stderr.splitlines()
                 if any(k in l.lower() for k in
                        ("error loading", "failed to load", "parse", "expecting"))]
    check(not load_errs,
          "no load/parse errors on stderr"
          + (f" (first: {load_errs[0]!r})" if load_errs else ""))

    print("=== KiCad 5 (module ...) form: synthetic minimal board ===")
    os.makedirs(os.path.dirname(SYN_BOARD), exist_ok=True)
    with open(SYN_BOARD, "w", encoding="utf-8") as f:
        f.write("""(kicad_pcb (version 20171130) (host pcbnew 5.0.0)
  (layers (0 F.Cu signal) (31 B.Cu signal))
  (net 0 "") (net 1 GND)
  (module M1 (layer F.Cu) (tedit 0) (tstamp 0)
    (at 10 10)
    (fp_text reference U9 (at 0 0) (layer F.SilkS)
      (effects (font (size 1 1) (thickness 0.15))))
    (pad 1 smd rect (size 1 2) (layers F.Cu) (net 1 GND))
    (pad 2 smd rect (at 3 0 45) (size 1 2) (layers F.Cu) (net 1 GND))
  )
)
""")
    write_moved_copy(SYN_BOARD, OUT_MSYN, {"U9": (20.0, 15.0, 90.0)})
    sb = load_board(OUT_MSYN)
    # pad 1 has NO (at) node: local (0,0,0) -> lands AT the footprint, and a
    # fresh (at 0 0 90) must be spliced in for the baked absolute angle
    check(abs(sb.pads[0].x_mm - 20.0) < 1e-9
          and abs(sb.pads[0].y_mm - 15.0) < 1e-9
          and sb.pads[0].rotation_deg == 90.0,
          "module-form pad WITHOUT (at) gains the baked angle at the origin")
    # pad 2: local (3, 0) rotated 90 (CCW, Y-down) -> (20, 15 - 3); 45 + 90
    check(abs(sb.pads[1].x_mm - 20.0) < 1e-9
          and abs(sb.pads[1].y_mm - 12.0) < 1e-9
          and sb.pads[1].rotation_deg == 135.0,
          f"module-form pad rotates about the module origin "
          f"(got {sb.pads[1].x_mm}, {sb.pads[1].y_mm}, "
          f"{sb.pads[1].rotation_deg})")

    print("=== KiCad 5 (module ...) form: bench pico-vga fixture ===")
    if os.path.exists(PICO):
        hash_pico = sha256(PICO)
        with open(PICO, encoding="utf-8") as f:
            precs = board_footprints(f.read())
        r11 = resolve_footprint(precs, "R11")
        check(r11.rot_deg == 90.0 and r11.n_pads == 2,
              f"R11 found: module at rot 90 with 2 pads")
        pico_place = {"R11": (150.0, 98.0, 180.0)}
        write_moved_copy(PICO, OUT_M5, pico_place)
        check_moved_board(PICO, OUT_M5, pico_place, "pico")
        check(sha256(PICO) == hash_pico, "pico board bytes untouched")
    else:
        print(f"  SKIP {os.path.basename(PICO)}: fixture absent "
              f"(bench/boards/ is gitignored — see bench/boards/SOURCES.md)")

    print("=== refusal guard + source untouched ===")
    try:
        write_moved_copy(BOARD, os.path.join(os.path.dirname(BOARD),
                                             "evil-moved.kicad_pcb"),
                         {"5755#1": (0.0, 0.0, 0.0)})
        check(False, "write into the source board's directory refused")
    except ValueError as e:
        check("refusing to write" in str(e),
              f"write into the source board's directory refused ({e})")
    check(sha256(BOARD) == hash_hifi,
          "hifi board bytes identical before and after the move tests")

    print("=== load_net_class_names shares the widths resolution ===")
    with open(FIXTURE_PRO, "w", encoding="utf-8") as f:
        json.dump({"net_settings": {
            "classes": [
                {"name": "Default", "track_width": 0.2, "priority": 2147483647},
                {"name": "Power", "track_width": 0.8, "priority": 0},
                {"name": "Fat", "track_width": 1.5, "priority": 1},
            ],
            "netclass_assignments": {"sig_in": ["Fat"]},
            "netclass_patterns": [
                {"pattern": "B+*", "netclass": "Power"},
                {"pattern": "sig_*", "netclass": "Power"},
                {"pattern": "GND", "netclass": "Power"},
                {"pattern": "G*", "netclass": "Fat"},
            ]}}, f)
    names = load_net_class_names(
        FIXTURE_PRO, {0: "", 1: "GND", 2: "B+250", 3: "sig_in"})
    check(names == {0: "Default", 1: "Power", 2: "Power", 3: "Fat"},
          f"class NAMES resolve exactly like the widths did ({names})")


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    for p in (OUT, OUT_W, FIXTURE_PRO, DRC_RPT,
              OUT_M, OUT_M5, OUT_MSYN, DRC_M_RPT, SYN_BOARD):
        assert os.path.abspath(p).startswith(OUT_DIR + os.sep), \
            f"output {p} escapes {OUT_DIR}"
        if os.path.exists(p):
            os.remove(p)
    hash_before = sha256(BOARD)
    ro_pros = [p for p in (project_file_for(BOARD), VOXY_PRO)
               if p and os.path.isfile(p)]
    pro_hashes = {p: sha256(p) for p in ro_pros}

    if "--moves-only" in sys.argv:
        # CPU-side sections only: no pathfinder import, no route, no GPU.
        run_move_tests()
        print("=== original untouched ===")
        check(sha256(BOARD) == hash_before,
              "original board bytes identical before and after")
        for p, h in pro_hashes.items():
            check(sha256(p) == h,
                  f"{os.path.basename(p)} bytes identical before and after")
        print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
              f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
        raise SystemExit(1 if failures else 0)

    from pathfinder import route_board, paths_to_tracks

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

    run_move_tests()

    print("=== original untouched ===")
    check(sha256(BOARD) == hash_before,
          "original board bytes identical before and after")
    for p, h in pro_hashes.items():
        check(sha256(p) == h,
              f"{os.path.basename(p)} bytes identical before and after")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
