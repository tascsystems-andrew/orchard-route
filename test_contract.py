"""Regression tests for the geometry contract: the tool must never say one
thing and do another.

Orchard Route prints a `geometry :` line every run precisely so its limits
are inspectable by hand. Two bugs made that line lie, and both were only
visible by comparing the printed number against the copper on disk:

BUG 1 — `--width-map` was applied at EMISSION time only, after route_board
had already computed CopperGeometry from the project's net classes. A run
with `--width-map "*=0.2:1.2:0.5"` printed `via 0.60`, planned every
via-exclusion halo at r = 0.6/2 + track/2 + clearance, and then wrote 27
vias at `(size 1.2)`. The halos were sized for copper that does not exist,
so every clearance number in that run was wrong for the board it produced.

BUG 2 — the pad-ring clearance took a hardcoded DEFAULT_CLEARANCE_MM = 0.2
whenever the caller passed clearance_mm=None, even though
resolve_board_geometry had already read the project's real 0.15 and printed
it. Conservative, and still a lie: pads were walled at 0.2 mm while the
contract claimed 0.15, and it cost real nets on icebreaker-bitsy.

The tests below therefore never compare the tool against itself. They
compare the PRINTED CONTRACT against copper re-parsed from the written
file, and the ring model's inflate against the arithmetic in
lattice.clearance_map's own docstring, computed here by hand.

Boards live in bench/boards/ (gitignored — see bench/boards/SOURCES.md);
sections whose fixture is missing SKIP loudly rather than silently pass.
"""
import os
import sys
from io import StringIO

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out", "test-contract")
BENCH = os.path.join(HERE, "bench", "boards")

# A project whose Default net class asks for 0.15 mm clearance, NOT the
# hardcoded 0.2 — the board that exposed bug 2.
BITSY = os.path.join(BENCH, "icebreaker-bitsy-v1.1c", "icebreaker-bitsy.kicad_pcb")
RP2040 = os.path.join(BENCH, "rpi-rp2040-minimal", "RP2040_minimal_r2.kicad_pcb")

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def skip(name, why):
    print(f"  SKIP {name}  — {why}")


def run_writeback(argv):
    """writeback.main with stdout captured; returns (stdout, parsed stats)."""
    os.makedirs(OUT_DIR, exist_ok=True)
    import writeback
    buf, real = StringIO(), sys.stdout
    sys.stdout = buf
    try:
        writeback.main(argv)
    finally:
        sys.stdout = real
    log = buf.getvalue()
    geo = next((ln.split(":", 1)[1].strip() for ln in log.splitlines()
                if ln.startswith("geometry")), "")
    return log, geo


def contract_numbers(geo_line):
    """(track, clearance, via) as the contract line STATES them — parsed from
    the printed text, so the test reads what a human reads."""
    body = geo_line.split("|")[1].strip()      # "track W clearance C via V"
    parts = body.split()
    return (float(parts[1]), float(parts[3]), float(parts[5]))


def emitted_copper(source, path):
    """(max track width, max via size, counts) of the copper THIS RUN wrote,
    re-parsed from the file and with the source board's own copper excluded.

    Measuring the whole file would fold in the input board's pre-existing
    0.8 mm power tracks and prove nothing about what the router emitted."""
    sys.path.insert(0, os.path.join(HERE, "scripts"))
    import copper_audit as ca
    segs, vias, _ident = ca.emitted_copper(source, path)
    return (max((s[4] for s in segs), default=0.0),
            max((v[2] for v in vias), default=0.0), len(segs), len(vias))


# ── BUG 1 ────────────────────────────────────────────────────────────────
def test_width_map_is_modelled_not_just_emitted():
    print("=== --width-map copper must be the copper the contract describes ===")
    if not os.path.isfile(RP2040):
        skip("width-map", f"{RP2040} absent")
        return
    out = os.path.join(OUT_DIR, "rp2040-widthmap.kicad_pcb")
    log, geo = run_writeback([RP2040, out, "--pitch", "0.25",
                              "--layers", "F.Cu,B.Cu",
                              "--width-map", "*=0.2:1.2:0.5"])
    track, clearance, via = contract_numbers(geo)
    print(f"       contract: track {track} clearance {clearance} via {via}")

    check("contract states the width-map's via, not the project class's",
          via == 1.2, f"contract says via {via}, --width-map asked 1.2")
    check("contract states the width-map's track width",
          track == 0.2, f"contract says track {track}")

    # The decisive comparison: re-parse the file and measure the copper.
    wmax, vmax, nseg, nvia = emitted_copper(RP2040, out)
    check("the board actually got new copper", nseg > 0,
          f"{nseg} emitted segments")
    check("no emitted track is wider than the contract's track",
          wmax <= track + 1e-9, f"widest written {wmax}, contract {track}")
    check("no emitted via is bigger than the contract's via",
          vmax <= via + 1e-9, f"biggest written {vmax}, contract {via}")
    if nvia > 0:
        check("and the width-map via really was emitted (1.2 mm on disk)",
              vmax == 1.2, f"biggest written via {vmax}")

    # The halo the router planned must match the copper on disk, by the
    # arithmetic in geometry.py's docstring — recomputed here, not read back.
    want_halo = via / 2.0 + track / 2.0 + clearance
    stated = float(geo.split("vias exclude r=")[1].split("mm")[0])
    check("via halo radius = via/2 + track/2 + clearance, on the EMITTED via",
          abs(stated - want_halo) < 1e-9,
          f"stated r={stated}, hand-computed {want_halo:.4f}")

    check("the contract says its copper is the WORST case, not an average",
          "widest of" in geo, geo)

    # And the guard that makes it structural rather than incidental.
    import writeback
    from geometry import CopperGeometry
    g = CopperGeometry(pitch_mm=0.25, track_width_mm=0.2, clearance_mm=0.15,
                       via_size_mm=0.6)
    breach = writeback.verify_emission(
        g, {7: (0.2, 1.2, 0.5)}, [], [(1.0, 1.0, 7)], (0.2, 0.6, 0.3))
    check("verify_emission catches a via bigger than the modelled one",
          len(breach) == 1 and "1.2" in breach[0], str(breach))
    ok = writeback.verify_emission(
        g, {7: (0.2, 0.6, 0.3)}, [], [(1.0, 1.0, 7)], (0.2, 0.6, 0.3))
    check("and passes copper that matches", ok == [], str(ok))


def test_max_width_is_modelled_too():
    print("=== --max-width raises the modelled copper, not only the emitted ===")
    if not os.path.isfile(RP2040):
        skip("max-width", f"{RP2040} absent")
        return
    # 0.6 mm tracks on a 0.25 mm pitch: without --max-width the cap is the
    # pitch, so the contract must state 0.25; with --max-width 0.6 the writer
    # emits 0.6 and the contract must say 0.6 or it is describing copper that
    # will not be on the board.
    out = os.path.join(OUT_DIR, "rp2040-maxwidth.kicad_pcb")
    _log, geo = run_writeback([RP2040, out, "--pitch", "0.25",
                               "--layers", "F.Cu,B.Cu",
                               "--width-map", "*=0.6", "--max-width", "0.6"])
    track, _c, _v = contract_numbers(geo)
    wmax, _vmax, _n, _nv = emitted_copper(RP2040, out)
    check("contract states the uncapped 0.6 mm track", track == 0.6,
          f"contract {track}")
    check("and 0.6 mm is what is on disk", wmax <= track + 1e-9 and wmax == 0.6,
          f"widest written {wmax}")

    out2 = os.path.join(OUT_DIR, "rp2040-capped.kicad_pcb")
    log2, geo2 = run_writeback([RP2040, out2, "--pitch", "0.25",
                                "--layers", "F.Cu,B.Cu", "--width-map", "*=0.6"])
    track2, _c, _v = contract_numbers(geo2)
    wmax2, _v2, _n2, _nv2 = emitted_copper(RP2040, out2)
    check("without --max-width the cap is the pitch, and the contract says so",
          track2 == 0.25, f"contract {track2}")
    check("and the capped width is what is on disk", wmax2 == 0.25,
          f"widest written {wmax2}")
    check("the cap is announced, not silent", "capped" in log2,
          [ln for ln in log2.splitlines() if "capped" in ln][:1])


# ── BUG 2 ────────────────────────────────────────────────────────────────
def test_ring_clearance_is_the_stated_clearance():
    print("=== the pad ring must be built from the clearance the tool STATES ===")
    if not os.path.isfile(BITSY):
        skip("ring clearance", f"{BITSY} absent")
        return
    from lattice import DEFAULT_CLEARANCE_MM, default_copper_rules
    from pathfinder import route_board

    project_clearance, _w = default_copper_rules(BITSY)
    check("fixture is a board whose project disagrees with the hardcoded "
          "default (otherwise this test proves nothing)",
          abs(project_clearance - DEFAULT_CLEARANCE_MM) > 1e-9,
          f"project {project_clearance}, hardcoded {DEFAULT_CLEARANCE_MM}")

    _brd, _lat, res = route_board(BITSY, pitch_mm=0.25,
                                  layer_names=["F.Cu", "B.Cu"],
                                  max_iters=1, refine_passes=0,
                                  smooth=False)
    geo = res.geometry
    check("contract states the PROJECT's clearance",
          abs(geo.clearance_mm - project_clearance) < 1e-9,
          f"contract {geo.clearance_mm}, project {project_clearance}")

    # lattice.clearance_map's own docstring: inflate = clearance + width/2,
    # width capped at the pitch. Computed here by hand from the two numbers
    # the contract printed — this is the equation that was silently fed 0.2.
    want = geo.clearance_mm + min(geo.track_width_mm, 0.25) / 2.0
    got = res.clearance_stats["inflate_mm"]
    check("ring inflate = stated clearance + stated track/2",
          abs(got - want) < 1e-9,
          f"inflate {got:.4f}, hand-computed {want:.4f}")
    wrong = DEFAULT_CLEARANCE_MM + min(geo.track_width_mm, 0.25) / 2.0
    check("and is NOT the hardcoded-default value the bug produced",
          abs(got - wrong) > 1e-9,
          f"inflate {got:.4f} would be {wrong:.4f} under the old fallback")

    check("the contract says where its clearance came from",
          "project" in (geo.clearance_source or ""), geo.clearance_source)

    # An explicit caller argument must still win, and be named as such.
    _b2, _l2, res2 = route_board(BITSY, pitch_mm=0.25,
                                 layer_names=["F.Cu", "B.Cu"],
                                 clearance_mm=0.3, max_iters=1,
                                 refine_passes=0, smooth=False)
    check("an explicit clearance overrides the project",
          abs(res2.geometry.clearance_mm - 0.3) < 1e-9,
          str(res2.geometry.clearance_mm))
    check("and the ring model follows it",
          abs(res2.clearance_stats["inflate_mm"]
              - (0.3 + min(res2.geometry.track_width_mm, 0.25) / 2.0)) < 1e-9,
          f"inflate {res2.clearance_stats['inflate_mm']:.4f}")
    check("provenance names the caller, not the project",
          "caller" in (res2.geometry.clearance_source or ""),
          res2.geometry.clearance_source)


def test_hardcoded_default_only_when_nothing_resolvable():
    print("=== the hardcoded default is a LAST resort, and says so ===")
    from geometry import resolve_board_geometry
    from lattice import DEFAULT_CLEARANCE_MM

    os.makedirs(OUT_DIR, exist_ok=True)
    lone = os.path.join(OUT_DIR, "no-project-board.kicad_pcb")
    with open(lone, "w", encoding="utf-8") as f:
        f.write('(kicad_pcb (version 20240108) (generator "test"))')
    check("a board with no .kicad_pro does not silently claim a project value",
          not os.path.isfile(os.path.splitext(lone)[0] + ".kicad_pro"))

    g = resolve_board_geometry(lone, 0.5, {})
    check("it falls back to the built-in default",
          abs(g.clearance_mm - DEFAULT_CLEARANCE_MM) < 1e-9, str(g.clearance_mm))
    check("and the contract line NAMES it as a built-in default",
          "built-in default" in g.summary(), g.summary())
    check("the summary still carries all four numbers",
          all(s in g.summary() for s in ("pitch 0.50", "clearance 0.20")),
          g.summary())

    # Nothing resolvable at all: the caller asked for 0 (clearance model off).
    g0 = resolve_board_geometry(lone, 0.5, {}, clearance_mm=0)
    check("clearance 0 still yields a stated number with stated provenance",
          "built-in default" in (g0.clearance_source or "")
          and "nothing else resolvable" in g0.clearance_source,
          g0.clearance_source)


def test_fab_enforce_moves_the_emitted_copper():
    print("=== --fab-enforce must move the COPPER, not just the contract ===")
    if not os.path.isfile(RP2040):
        skip("fab-enforce", f"{RP2040} absent")
        return
    # pcbway-standard's 0.1524 mm minimum track is ABOVE this project's
    # 0.15 mm Default class, so enforce has something real to raise; 0.7 mm
    # pitch is coarse enough that the profile fits and enforce runs rather
    # than bailing out with "could not snap".
    out = os.path.join(OUT_DIR, "rp2040-fabenforce.kicad_pcb")
    log, geo = run_writeback([RP2040, out, "--pitch", "0.7",
                              "--layers", "F.Cu,B.Cu",
                              "--fab", "pcbway-standard", "--fab-enforce"])
    track, clearance, _via = contract_numbers(geo)
    check("enforce actually fired (otherwise this proves nothing)",
          "--fab-enforce changed" in log,
          [ln for ln in log.splitlines() if "--fab-enforce changed" in ln][:1])
    check("contract states the profile's minimum track", track == 0.1524,
          f"contract {track}")
    check("contract states the profile's minimum clearance",
          clearance == 0.2794, f"contract {clearance}")
    wmax, _v, nseg, _nv = emitted_copper(RP2040, out)
    check("and the raised track is what is on disk, not the old 0.15",
          nseg > 0 and wmax == 0.1524, f"widest emitted {wmax}")
    check("the contract does not round 0.1524 away to 0.15",
          "0.1524" in geo, geo)

    # The unit beneath it, both directions.
    from fab import FabOutcome, load_profile
    from geometry import CopperGeometry
    from pathfinder import _apply_fab_outcome
    g = CopperGeometry(pitch_mm=0.5, track_width_mm=0.15, clearance_mm=0.2,
                       via_size_mm=0.6)
    up = FabOutcome(profile=load_profile("pcbway-standard"),
                    changes=["x"], track_mm=0.1524)
    check("a raise reaches every net's track",
          _apply_fab_outcome({1: (0.15, 0.6, 0.3), 2: (0.3, 0.6, 0.3)}, up, g)
          == {1: (0.1524, 0.6, 0.3), 2: (0.3, 0.6, 0.3)},
          "and never narrows a net that is already wider")
    down = FabOutcome(profile=load_profile("jlcpcb-standard"),
                      changes=["x"], via_size_mm=0.45)
    check("a pitch-rescue via SHRINK reaches every net's via too",
          _apply_fab_outcome({1: (0.15, 0.6, 0.3)}, down, g)
          == {1: (0.15, 0.45, 0.3)},
          "the contract shrinks the via; so must the copper")
    check("no changes means no silent edits",
          _apply_fab_outcome({1: (0.15, 0.6, 0.3)},
                             FabOutcome(profile=load_profile("none")), g)
          == {1: (0.15, 0.6, 0.3)})


def main():
    test_width_map_is_modelled_not_just_emitted()
    test_max_width_is_modelled_too()
    test_fab_enforce_moves_the_emitted_copper()
    test_ring_clearance_is_the_stated_clearance()
    test_hardcoded_default_only_when_nothing_resolvable()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
