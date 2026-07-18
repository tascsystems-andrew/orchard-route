"""Quality benchmark: Orchard Route vs human ground-truth routing.

Compares the router's output against professionally-routed open-hardware
boards (bench/boards/, roster and provenance in bench/boards/SOURCES.md).
Two modes:

- baseline (default): pure parsing, no GPU, no mlx import. Loads each board
  with board.load_board and reports the HUMAN routing as found in the file —
  track count, wirelength (sum of segment euclidean lengths), via count,
  tracks per copper layer. This is the ground truth the router is judged
  against, and it is safe to run while the GPU is busy.
- route: re-routes every board from scratch with pathfinder.route_board and
  prints ratios vs the human baseline (wirelength_ours/human, vias_ours/human).
  The router ignores existing copper — existing tracks are not obstacles —
  which is fine here because we re-route from scratch and compare metrics;
  the outputs never coexist with the human tracks.

Fairness caveat, printed per affected board: 4-layer boards were human-routed
with all 4 copper layers, while we route F.Cu+B.Cu only for now. On those
boards every ratio is handicapped AGAINST the router — the human had twice
the layers (often including whole ground/power planes that absorb vias).

Per-board pitch: 0.5 mm default; 0.25 mm for fine-pitch small boards (judged
from min pad dimension — the RP2040/iCE40 QFNs have 0.2-0.3 mm pads and
majorities of pads under 1 mm). See _BOARD_CFG. Results land in
bench/results.json (no timestamps — git tracks runs).

CLI: python bench/run_bench.py [--mode baseline|route] [--boards a,b,...]
     [--pitch MM] [--layers F.Cu,B.Cu]
"""
import json
import math
import os
import sys
from collections import Counter

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from board import load_board  # noqa: E402  (path bootstrap above)

BOARDS_DIR = os.path.join(_REPO, "bench", "boards")
RESULTS_PATH = os.path.join(_REPO, "bench", "results.json")
DEFAULT_PITCH = 0.5
DEFAULT_LAYERS = ["F.Cu", "B.Cu"]

# Per-board config. pitch_mm: 0.5 default suits 1206/0603-class boards; 0.25
# where the finest pads demand it (AGENTS.md pitch rule). skip: excluded from
# both modes, listed with the reason. note: printed with the board, for
# caveats that don't disqualify it.
_BOARD_CFG = {
    "kicad-demo-pic-programmer": {},   # through-hole era, pads >= 1 mm bar one part
    "kicad-demo-video": {
        # 312x107 mm — 0.25 pitch would 4x an already-huge lattice. The few
        # 0.3 mm QFP pads may show up as pad-snap conflicts at 0.5; if route
        # mode reports many, this board needs 0.25 and a long run.
        "note": "min pad dim 0.3 mm; watch pad-snap conflicts at 0.5 pitch",
    },
    "rpi-rp2040-minimal": {"pitch_mm": 0.25},       # RP2040 QFN, 0.2 mm pads
    "icebreaker-v1.0e": {"pitch_mm": 0.25},         # iCE40 QFN, 0.25 mm pads
    "icebreaker-bitsy-v1.1c": {"pitch_mm": 0.25},   # fine-pitch, 36x18 mm
    "sparkfun-iot-redboard-rp2350": {"pitch_mm": 0.25},  # RP2350 QFN, 0.2 mm pads
    "rpi-pico-vga": {
        # USB micro-B / SD connector pads down to 0.4 mm at 0.65 mm pin
        # pitch — adjacent pads collide on a 0.5 mm lattice; board is only
        # 85x56 mm so 0.25 is affordable.
        "pitch_mm": 0.25,
    },
}


# ── board discovery ───────────────────────────────────────────────────────────

def discover_boards():
    """Subdirectories of bench/boards containing exactly one .kicad_pcb.
    Returns [(name, pcb_path)] sorted by name; boards absent from _BOARD_CFG
    get defaults (new drops into bench/boards/ just work)."""
    out = []
    for name in sorted(os.listdir(BOARDS_DIR)):
        d = os.path.join(BOARDS_DIR, name)
        if not os.path.isdir(d):
            continue
        pcbs = sorted(f for f in os.listdir(d) if f.endswith(".kicad_pcb"))
        if not pcbs:
            continue
        if len(pcbs) > 1:
            print(f"WARN {name}: {len(pcbs)} .kicad_pcb files, using {pcbs[0]}")
        out.append((name, os.path.join(d, pcbs[0])))
    return out


# ── human baseline (parsing only, no GPU) ─────────────────────────────────────

def human_metrics(brd):
    """Ground-truth routing stats straight out of the loaded board."""
    wl_by_layer = Counter()
    n_by_layer = Counter()
    for t in brd.tracks:
        (x1, y1), (x2, y2) = t.start_mm, t.end_mm
        wl_by_layer[t.layer] += math.hypot(x2 - x1, y2 - y1)
        n_by_layer[t.layer] += 1
    return {
        "size_mm": [round(brd.size_mm[0], 2), round(brd.size_mm[1], 2)],
        "copper_layers": list(brd.copper_layers),
        "pads": len(brd.pads),
        "nets": len(brd.nets),
        "tracks": len(brd.tracks),
        "wirelength_mm": round(sum(wl_by_layer.values()), 1),
        "vias": len(brd.vias),
        "tracks_per_layer": {l: n_by_layer[l] for l in sorted(n_by_layer)},
        "wirelength_per_layer_mm": {l: round(wl_by_layer[l], 1)
                                    for l in sorted(wl_by_layer)},
    }


def fairness_note(human, layers):
    """The handicap caveat for boards human-routed on more layers than we
    route. Quantified: how much of the human copper lives off our layers."""
    extra = [l for l in human["copper_layers"] if l not in layers]
    if not extra:
        return None
    off = sum(human["wirelength_per_layer_mm"].get(l, 0.0) for l in extra)
    total = human["wirelength_mm"]
    pct = 100.0 * off / total if total else 0.0
    return (f"human used {len(human['copper_layers'])} Cu layers, we route "
            f"{'+'.join(layers)} only — {pct:.0f}% of human wirelength is on "
            f"{'/'.join(extra)}; ratios are handicapped AGAINST the router")


# ── route mode (GPU — do not run while a routing workflow owns the GPU) ──────

def route_metrics(pcb_path, pitch_mm, layers):
    """Re-route from scratch and score against the human baseline."""
    from pathfinder import route_board
    brd, lat, res = route_board(pcb_path, pitch_mm=pitch_mm, layer_names=layers)
    failed_nets = {n for n, _ in res.failed}
    routable = set(res.net_paths) | failed_nets
    out = {
        "lattice": [lat.W, lat.H, lat.L],
        "nets_routable": len(routable),
        "nets_routed": len(routable - failed_nets),
        "nets_failed": len(failed_nets),
        "connections": sum(len(p) for p in res.net_paths.values()) + len(res.failed),
        "pad_snap_conflicts": len(res.conflicts),
        "iterations": res.iterations,
        "overuse_curve": res.overuse_curve,
        "wirelength_mm": round(res.wirelength_mm, 1),
        "via_count": res.via_count,
        "seconds": {k: round(v, 2) for k, v in res.seconds.items()
                    if not k.endswith("_pct")},
        "refine_gain_pct": round(res.seconds.get("refine_gain_pct", 0.0), 2),
        "failed": [[n, brd.nets.get(n, "?"), reason]
                   for n, reason in res.failed],
    }
    return out


def ratios(route, human):
    def ratio(a, b):
        return round(a / b, 3) if b else None
    return {
        "wirelength_ours_over_human": ratio(route["wirelength_mm"],
                                            human["wirelength_mm"]),
        "vias_ours_over_human": ratio(route["via_count"], human["vias"]),
    }


# ── reporting ─────────────────────────────────────────────────────────────────

def print_baseline_table(rows):
    hdr = (f"{'board':28s} {'size mm':>13s} {'Cu':>3s} {'pads':>5s} "
           f"{'nets':>5s} {'tracks':>6s} {'human WL mm':>12s} {'vias':>5s}")
    print(hdr)
    print("-" * len(hdr))
    for name, human, note, fair in rows:
        size = f"{human['size_mm'][0]:.1f} x {human['size_mm'][1]:.1f}"
        print(f"{name:28s} {size:>13s} {len(human['copper_layers']):>3d} "
              f"{human['pads']:>5d} {human['nets']:>5d} {human['tracks']:>6d} "
              f"{human['wirelength_mm']:>12.1f} {human['vias']:>5d}")
        per = human["tracks_per_layer"]
        print("  layers : " + " | ".join(f"{l} {per[l]}" for l in sorted(per)))
        if note:
            print(f"  note   : {note}")
        if fair:
            print(f"  fair   : {fair}")


def print_route_table(rows):
    hdr = (f"{'board':28s} {'pitch':>5s} {'routed':>9s} {'iters':>5s} "
           f"{'WL ours':>9s} {'WL human':>9s} {'WL x':>6s} "
           f"{'vias':>5s} {'v hum':>5s} {'via x':>6s}")
    print(hdr)
    print("-" * len(hdr))
    for name, cfg, human, route, rat, note, fair in rows:
        routed = f"{route['nets_routed']}/{route['nets_routable']}"
        wlx = rat["wirelength_ours_over_human"]
        vix = rat["vias_ours_over_human"]
        print(f"{name:28s} {cfg['pitch_mm']:>5.2f} {routed:>9s} "
              f"{route['iterations']:>5d} {route['wirelength_mm']:>9.1f} "
              f"{human['wirelength_mm']:>9.1f} "
              f"{wlx if wlx is not None else '-':>6} "
              f"{route['via_count']:>5d} {human['vias']:>5d} "
              f"{vix if vix is not None else '-':>6}")
        secs = " | ".join(f"{k} {v}" for k, v in route["seconds"].items())
        print(f"  seconds: {secs}   refine -{route['refine_gain_pct']:.2f}%")
        if route["nets_failed"]:
            print(f"  failed : {route['nets_failed']} net(s) — "
                  + "; ".join(f"{nm or code}: {r}"
                              for code, nm, r in route["failed"][:5]))
        if route["pad_snap_conflicts"]:
            print(f"  snap   : {route['pad_snap_conflicts']} pad-snap "
                  f"conflicts — pitch too coarse for this board")
        if note:
            print(f"  note   : {note}")
        if fair:
            print(f"  fair   : {fair}")


# ── main ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Benchmark Orchard Route against human-routed boards")
    ap.add_argument("--mode", choices=("baseline", "route"), default="baseline",
                    help="baseline: parse human ground truth only (no GPU); "
                         "route: re-route every board and compare (GPU)")
    ap.add_argument("--boards", default=None,
                    help="comma-list of board dir names to include (default all)")
    ap.add_argument("--pitch", type=float, default=None,
                    help="override per-board routing pitch (mm)")
    ap.add_argument("--layers", default=None,
                    help="override layers to route, e.g. F.Cu,B.Cu")
    args = ap.parse_args(argv)

    wanted = ([s.strip() for s in args.boards.split(",") if s.strip()]
              if args.boards else None)
    layers_override = ([s.strip() for s in args.layers.split(",") if s.strip()]
                       if args.layers else None)

    discovered = discover_boards()
    known = {name for name, _ in discovered}
    for name in wanted or []:
        if name not in known:
            print(f"WARN --boards {name}: no such board dir under bench/boards/")

    results = {"mode": args.mode, "boards": {}}
    table_rows = []
    skipped = []

    for name, pcb_path in discovered:
        if wanted and name not in wanted:
            continue
        cfg = _BOARD_CFG.get(name, {})
        if "skip" in cfg:
            skipped.append((name, cfg["skip"]))
            results["boards"][name] = {
                "pcb": os.path.relpath(pcb_path, _REPO),
                "skipped": cfg["skip"],
            }
            continue
        pitch = args.pitch if args.pitch is not None else \
            cfg.get("pitch_mm", DEFAULT_PITCH)
        layers = layers_override or cfg.get("layers", DEFAULT_LAYERS)
        note = cfg.get("note")

        human = human_metrics(load_board(pcb_path))
        fair = fairness_note(human, layers)
        entry = {
            "pcb": os.path.relpath(pcb_path, _REPO),
            "config": {"pitch_mm": pitch, "layers": layers},
            "human": human,
        }
        if note:
            entry["note"] = note
        if fair:
            entry["fairness_caveat"] = fair

        if args.mode == "route":
            route = route_metrics(pcb_path, pitch, layers)
            rat = ratios(route, human)
            entry["route"] = route
            entry["ratios"] = rat
            table_rows.append((name, {"pitch_mm": pitch}, human, route, rat,
                               note, fair))
        else:
            table_rows.append((name, human, note, fair))
        results["boards"][name] = entry

    print(f"mode        : {args.mode}"
          + ("" if args.mode == "baseline" else
             "  (re-routed from scratch; existing copper is NOT an obstacle "
             "— metrics comparison only, outputs never coexist)"))
    if args.mode == "route":
        print_route_table(table_rows)
    else:
        print_baseline_table(table_rows)
    if skipped:
        print("skipped     :")
        for name, why in skipped:
            print(f"  {name}: {why}")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
        f.write("\n")
    print(f"results     : {os.path.relpath(RESULTS_PATH, os.getcwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
