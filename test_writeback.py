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

Run: .venv/bin/python test_writeback.py
"""
import hashlib
import os
import subprocess

from board import load_board
from pathfinder import route_board, paths_to_tracks
from writeback import write_routed_copy

BOARD = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
OUT = os.path.join(OUT_DIR, "hifi-routed.kicad_pcb")
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
    for p in (OUT, DRC_RPT):
        assert os.path.abspath(p).startswith(OUT_DIR + os.sep), \
            f"output {p} escapes {OUT_DIR}"
        if os.path.exists(p):
            os.remove(p)
    hash_before = sha256(BOARD)

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

    print("=== original untouched ===")
    check(sha256(BOARD) == hash_before,
          "original board bytes identical before and after")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
