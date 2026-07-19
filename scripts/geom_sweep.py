"""Sweep (via_size, track_width, clearance) on one board, driving BOTH the
router's geometry model and writeback's emission from the SAME numbers.

This script predates the CLI being trustworthy for the job. It was written
because writeback.main used to call route_board() without forwarding any
copper dimensions and then apply --width-map AFTER the route, so the router
modelled the PROJECT's net-class copper while the file received the
--width-map copper — a split that makes a geometry sweep meaningless. That
split is fixed: writeback now forwards --width-map/--max-width/--fab into
route_board, the geometry contract is computed from the resolved widths, and
writeback.verify_emission refuses to write if the two ever diverge.

The script survives because it still does something the CLI does not: it
drives clearance, track and via as one explicit triple per run, independent of
the project's net classes, which is what sweeping the geometry SPACE wants.
To sweep a real board's own copper, prefer the CLI.

Usage: python scripts/geom_sweep.py BOARD.kicad_pcb OUTDIR PITCH LAYERS \
           via:track:clear:drill [via:track:clear:drill ...]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathfinder import route_board, paths_to_tracks        # noqa: E402
from writeback import write_routed_copy                    # noqa: E402
from scripts.copper_audit import audit                     # noqa: E402


def run_one(board, outdir, pitch, layers, via, track, clear, drill,
            via_exclusion=True, tag=None):
    tag = tag or f"v{via}_t{track}_c{clear}"
    out = os.path.join(outdir, f"{os.path.splitext(os.path.basename(board))[0]}"
                               f"__{tag}.kicad_pcb")
    t0 = time.perf_counter()
    brd, lat, res = route_board(
        board, pitch_mm=pitch, layer_names=layers,
        clearance_mm=clear, track_width_mm=track, via_size_mm=via,
        via_exclusion=via_exclusion, refine_passes=2, smooth=True)
    wall = time.perf_counter() - t0

    tracks, vias = (res.tracks, res.vias) if res.tracks is not None \
        else paths_to_tracks(lat, res.net_paths)

    # Emit exactly what the router modelled: one triple for every net.
    widths = {code: (track, via, drill) for code in brd.nets}
    write_routed_copy(board, out, tracks, vias, brd.nets, widths=widths)

    vio, stats = audit(board, out, clearance_mm=clear)
    kinds = {}
    for k, *_ in vio:
        kinds[k] = kinds.get(k, 0) + 1
    worst = {}
    for k, na, nb, gap, r, at in vio:
        if k not in worst or gap < worst[k][0]:
            worst[k] = (round(gap, 4), na, nb, at)

    failed = {n for n, _ in res.failed}
    routable = set(res.net_paths) | failed
    reasons = {}
    for _n, reason in res.failed:
        key = reason.split("(")[0].strip()[:60]
        reasons[key] = reasons.get(key, 0) + 1

    rec = {
        "tag": tag, "board": os.path.basename(board), "pitch": pitch,
        "via": via, "track": track, "clearance": clear, "drill": drill,
        "via_exclusion": via_exclusion,
        "routable": len(routable), "routed": len(routable - failed),
        "failed": len(failed), "fail_reasons": reasons,
        "wirelength_mm": round(res.wirelength_mm, 1),
        "via_count": res.via_count, "seconds": round(wall, 1),
        "iterations": res.iterations,
        "overuse_tail": res.overuse_curve[-5:],
        "geometry": res.geometry_note,
        "warnings": res.geometry_warnings,
        "emitted_tracks": stats["emitted_tracks"],
        "emitted_vias": stats["emitted_vias"],
        "violations_total": len(vio), "violations_by_kind": kinds,
        "worst_gap_by_kind": worst,
        "out": out,
    }
    print(json.dumps(rec, indent=2), flush=True)
    return rec


def main(argv):
    board, outdir, pitch, layers = argv[0], argv[1], float(argv[2]), \
        [s for s in argv[3].split(",") if s]
    os.makedirs(outdir, exist_ok=True)
    recs = []
    for spec in argv[4:]:
        parts = spec.split(":")
        via, track, clear, drill = (float(p) for p in parts[:4])
        excl = not (len(parts) > 4 and parts[4] == "noexcl")
        tag = (parts[5] if len(parts) > 5 else
               f"v{via}_t{track}_c{clear}" + ("" if excl else "_noexcl"))
        print(f"\n===== {tag} =====", flush=True)
        recs.append(run_one(board, outdir, pitch, layers, via, track, clear,
                            drill, via_exclusion=excl, tag=tag))
    with open(os.path.join(outdir, "sweep.json"), "w") as f:
        json.dump(recs, f, indent=2)
    print("\n=== SUMMARY ===")
    print(f"{'tag':<28}{'routed/able':>13}{'wl_mm':>10}{'vias':>7}"
          f"{'sec':>7}{'viol':>7}  breakdown")
    for r in recs:
        print(f"{r['tag']:<28}{r['routed']}/{r['routable']:>7}"
              f"{r['wirelength_mm']:>10}{r['via_count']:>7}{r['seconds']:>7}"
              f"{r['violations_total']:>7}  {r['violations_by_kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
