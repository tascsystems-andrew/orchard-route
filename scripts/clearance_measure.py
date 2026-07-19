"""Measure clearance-category DRC counts for a board: untouched baseline vs routed.

For each named board it:
  1. copies the SOURCE board (+ .kicad_pro) into out/geo/ and DRCs it untouched
     -> the board's OWN baseline (violations it already had)
  2. routes + writes back into out/geo/ and DRCs that
  3. reports total / [clearance] counts and the nets-routed score

Usage: python scripts/clearance_measure.py TAG BOARDKEY [BOARDKEY ...]
       TAG is a label folded into the output filenames (e.g. "before", "after").
"""
import json
import os
import shutil
import subprocess
import sys
import time

KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out", "geo")

BOARDS = {
    # committed fixtures (amp carries a sibling .kicad_pro; gain does not)
    "amp": (os.path.join(ROOT, "fixtures", "amp_board.kicad_pcb"),
            0.5, "F.Cu,B.Cu"),
    "gain": (os.path.join(ROOT, "fixtures", "gain_stage.kicad_pcb"),
             0.5, "F.Cu,B.Cu"),
    # third-party bench boards (gitignored, see bench/boards/SOURCES.md)
    "pic": (os.path.join(ROOT, "bench/boards/kicad-demo-pic-programmer/"
                               "pic_programmer.kicad_pcb"), 0.5, "F.Cu,B.Cu"),
    "bitsy": (os.path.join(ROOT, "bench/boards/icebreaker-bitsy-v1.1c/"
                                 "icebreaker-bitsy.kicad_pcb"), 0.25, "F.Cu,B.Cu"),
}


def drc(pcb, rpt_json):
    subprocess.run([KICAD_CLI, "pcb", "drc", "--format", "json",
                    "--severity-error", "--severity-warning",
                    "-o", rpt_json, pcb],
                   check=True, capture_output=True)
    with open(rpt_json, encoding="utf-8") as f:
        d = json.load(f)
    vs = d.get("violations") or []
    counts = {}
    for v in vs:
        counts[v.get("type", "?")] = counts.get(v.get("type", "?"), 0) + 1
    return len(vs), counts.get("clearance", 0), counts


def copy_project(src_pcb, dst_pcb):
    shutil.copyfile(src_pcb, dst_pcb)
    for ext in (".kicad_pro", ".kicad_dru"):
        s = os.path.splitext(src_pcb)[0] + ext
        if os.path.isfile(s):
            shutil.copyfile(s, os.path.splitext(dst_pcb)[0] + ext)


def main(argv):
    tag, keys = argv[0], argv[1:]
    os.makedirs(OUT, exist_ok=True)
    report = {}
    for key in keys:
        src, pitch, layers = BOARDS[key]
        base_pcb = os.path.join(OUT, f"{key}-untouched.kicad_pcb")
        if not os.path.isfile(base_pcb.replace(".kicad_pcb", "-drc.json")):
            copy_project(src, base_pcb)
            b_tot, b_clr, b_all = drc(
                base_pcb, base_pcb.replace(".kicad_pcb", "-drc.json"))
        else:
            b_tot, b_clr, b_all = drc(
                base_pcb, base_pcb.replace(".kicad_pcb", "-drc.json"))

        out_pcb = os.path.join(OUT, f"{key}-{tag}.kicad_pcb")
        copy_project(src, out_pcb)          # carry the project's net classes
        os.remove(out_pcb)
        t0 = time.perf_counter()
        proc = subprocess.run(
            [sys.executable,
             os.path.join(ROOT, "writeback.py"), src, out_pcb,
             "--pitch", str(pitch), "--layers", layers],
            capture_output=True, text=True, cwd=ROOT)
        secs = time.perf_counter() - t0
        print(f"=== {key} [{tag}] route+writeback {secs:.1f}s")
        print(proc.stdout)
        if proc.returncode != 0:
            print(proc.stderr[-3000:])
            raise SystemExit(f"{key}: writeback failed")
        r_tot, r_clr, r_all = drc(
            out_pcb, out_pcb.replace(".kicad_pcb", "-drc.json"))
        report[key] = {
            "untouched_total": b_tot, "untouched_clearance": b_clr,
            "routed_total": r_tot, "routed_clearance": r_clr,
            "ours_clearance": r_clr - b_clr,
            "seconds": round(secs, 1),
            "routed_types": r_all,
            "stdout": proc.stdout,
        }
        print(f"--- {key} [{tag}]: untouched {b_tot} total / {b_clr} clearance | "
              f"routed {r_tot} total / {r_clr} clearance | "
              f"ours {r_clr - b_clr} clearance")
    with open(os.path.join(OUT, f"summary-{tag}.json"), "w") as f:
        json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
