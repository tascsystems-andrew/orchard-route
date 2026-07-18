#!/usr/bin/env bash
# studio-setup.sh — idempotent Orchard Route setup for the Mac Studio (or any
# Apple Silicon Mac). Safe to run twice; touches NOTHING outside the repo
# checkout and its .venv.
#
# What it does:
#   1. checks Apple Silicon + macOS version
#   2. checks Xcode CLT (git) and Homebrew are present — prints the install
#      command and STOPS if not (never installs system software silently)
#   3. clones the repo (or ff-only pulls an existing clean checkout;
#      a dirty checkout is left untouched)
#   4. creates .venv and installs mlx + numpy
#   5. runs the CPU-only smoke tests (synthetic board parse + test_board.py)
#   6. prints the GPU-validation steps for YOU to run — this script never
#      touches the GPU
#
# What it deliberately does NOT do: no system settings, no Homebrew install,
# no KiCad, no Claude Code install, no GPU work. See STUDIO.md for those.
#
# Usage:
#   ./scripts/studio-setup.sh                 # from inside a checkout
#   curl -fsSL https://raw.githubusercontent.com/tascsystems-andrew/orchard-route/main/scripts/studio-setup.sh | bash
#
# Env overrides:
#   ORCHARD_DIR       where to clone (default ~/Code/orchard-route; ignored
#                     when the script already runs inside a checkout)
#   ORCHARD_REPO_URL  git remote (default the public GitHub repo)

set -euo pipefail

REPO_URL="${ORCHARD_REPO_URL:-https://github.com/tascsystems-andrew/orchard-route.git}"
DEFAULT_DIR="${ORCHARD_DIR:-$HOME/Code/orchard-route}"

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 1. hardware
say "Checking hardware and OS"
[ "$(uname -s)" = "Darwin" ] || die "macOS only (MLX needs Apple Silicon + Metal)."
[ "$(uname -m)" = "arm64" ]  || die "Apple Silicon required — this Mac is $(uname -m). MLX does not run on Intel Macs."

osver="$(sw_vers -productVersion)"
osmajor="${osver%%.*}"
if [ "$osmajor" -lt 13 ]; then
    die "macOS $osver is too old — MLX needs macOS >= 13.5."
elif [ "$osmajor" -eq 13 ]; then
    note "WARNING: macOS $osver — MLX needs >= 13.5 and newer is better. Continuing."
fi

chip="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo unknown)"
mem_gb=$(( $(sysctl -n hw.memsize) / 1073741824 ))
# OrthoRoute's README sizing heuristic: ~200k lattice nodes per GB of memory.
nodes_est="$(awk "BEGIN{printf \"%.1f\", $mem_gb * 0.2}")"
note "macOS $osver on $chip, ${mem_gb} GB unified memory"
note "lattice capacity estimate (nodes/200k-per-GB heuristic): ~${nodes_est}M nodes"

# ------------------------------------------------------- 2. toolchain presence
say "Checking toolchain (nothing is installed without you)"
if ! xcode-select -p >/dev/null 2>&1; then
    printf '\nXcode Command Line Tools (git) are missing. Install them, then re-run:\n\n'
    printf '    xcode-select --install\n\n'
    exit 1
fi
note "git: $(git --version)"

if ! command -v brew >/dev/null 2>&1; then
    printf '\nHomebrew is not installed. This script will not install it for you.\n'
    printf 'Install it, then re-run this script:\n\n'
    printf '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"\n\n'
    exit 1
fi
note "Homebrew: $(brew --version | head -1)"

PY=""
for cand in python3.14 python3.13 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1 \
       && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        PY="$(command -v "$cand")"
        break
    fi
done
if [ -z "$PY" ]; then
    printf '\nNo Python >= 3.12 found. Install one with Homebrew, then re-run:\n\n'
    printf '    brew install python\n\n'
    exit 1
fi
note "python: $PY ($("$PY" --version 2>&1))"

# ------------------------------------------------------------ 3. repo checkout
say "Locating the repo"
# If this script runs from inside a checkout, use that checkout — never clone
# a second copy beside an existing one.
SCRIPT_SRC="${BASH_SOURCE[0]:-$0}"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_SRC")" 2>/dev/null && pwd || pwd)"
REPO=""
if top="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    remote="$(git -C "$top" remote get-url origin 2>/dev/null || echo '')"
    case "$remote" in
        *orchard-route*|*mlx-router*) REPO="$top" ;;
    esac
fi

if [ -n "$REPO" ]; then
    note "running inside existing checkout: $REPO"
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        note "working tree has local changes — SKIPPING git pull (your work is untouched)"
    else
        note "clean tree — pulling latest (ff-only)"
        git -C "$REPO" pull --ff-only || note "WARNING: pull failed (offline or diverged) — continuing with current checkout"
    fi
elif [ -d "$DEFAULT_DIR/.git" ]; then
    REPO="$DEFAULT_DIR"
    note "existing clone: $REPO"
    if [ -n "$(git -C "$REPO" status --porcelain)" ]; then
        note "working tree has local changes — SKIPPING git pull (your work is untouched)"
    else
        note "clean tree — pulling latest (ff-only)"
        git -C "$REPO" pull --ff-only || note "WARNING: pull failed (offline or diverged) — continuing with current checkout"
    fi
else
    REPO="$DEFAULT_DIR"
    note "cloning $REPO_URL -> $REPO"
    mkdir -p "$(dirname "$REPO")"
    git clone "$REPO_URL" "$REPO"
fi

# ------------------------------------------------------------------- 4. venv
say "Python environment (.venv inside the repo)"
VENVPY="$REPO/.venv/bin/python"
if [ ! -x "$VENVPY" ] || ! "$VENVPY" -c 'import sys' >/dev/null 2>&1; then
    note "creating venv with $PY"
    "$PY" -m venv "$REPO/.venv"
    "$VENVPY" -m pip install --quiet --upgrade pip
else
    note "venv already exists — reusing"
fi
note "installing mlx + numpy (no-op when already present)"
"$VENVPY" -m pip install --quiet mlx numpy
"$VENVPY" -c 'import numpy' || die "numpy failed to import from the venv"
mlx_ver="$("$VENVPY" -m pip show mlx 2>/dev/null | awk '/^Version:/{print $2}')"
np_ver="$("$VENVPY" -m pip show numpy 2>/dev/null | awk '/^Version:/{print $2}')"
[ -n "$mlx_ver" ] || die "mlx did not install (pip show mlx found nothing)"
note "mlx $mlx_ver, numpy $np_ver installed"
note "(mlx is deliberately NOT imported here — first Metal touch is yours, in the GPU steps below)"

# ------------------------------------------------------------ 5. CPU smoke
say "Smoke 1/2: synthetic board -> parser -> CPU lattice (no fixtures needed)"
mkdir -p "$REPO/out"
ORCHARD_REPO="$REPO" "$VENVPY" - <<'PYEOF'
import os, sys
repo = os.environ["ORCHARD_REPO"]
sys.path.insert(0, repo)
from board import load_board
from lattice import lattice_for_board

SYNTH = """(kicad_pcb (version 20241229) (generator "orchard-smoke")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (25 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "N1")
  (net 2 "GND")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts") (width 0.1))
  (footprint "Smoke:R_1206"
    (layer "F.Cu")
    (at 5 10)
    (pad "1" smd rect (at -0.9 0) (size 1.0 1.5) (layers "F.Cu") (net 1 "N1"))
    (pad "2" smd rect (at 0.9 0) (size 1.0 1.5) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Smoke:R_1206b"
    (layer "F.Cu")
    (at 15 10 90)
    (pad "1" smd rect (at -0.9 0) (size 1.0 1.5) (layers "F.Cu") (net 1 "N1"))
    (pad "2" thru_hole circle (at 0.9 0) (size 1.6 1.6) (drill 0.8) (layers "*.Cu") (net 2 "GND"))
  )
)
"""
path = os.path.join(repo, "out", "_studio_smoke_board.kicad_pcb")
with open(path, "w") as f:
    f.write(SYNTH)
try:
    b = load_board(path)
    w, h = b.size_mm
    assert abs(w - 20.0) < 1e-6 and abs(h - 20.0) < 1e-6, f"size {w}x{h} != 20x20"
    assert len(b.pads) == 4, f"pads {len(b.pads)} != 4"
    assert b.nets.get(1) == "N1" and b.nets.get(2) == "GND", f"nets {b.nets}"
    assert b.copper_layers == ["F.Cu", "B.Cu"], f"layers {b.copper_layers}"
    th = [p for p in b.pads if p.through_hole]
    assert len(th) == 1 and abs(th[0].drill_mm - 0.8) < 1e-9, "thru-hole pad wrong"
    # rotated-footprint transform check: CCW with Y down, 90 deg maps
    # local (-0.9, 0) at (15, 10) to (15.0, 10.9)
    rp = [p for p in b.pads if abs(p.x_mm - 15.0) < 1e-6 and abs(p.y_mm - 10.9) < 1e-6]
    assert rp and rp[0].net_name == "N1", "rotated footprint transform broken"
    lat, pad_nodes, node_owner = lattice_for_board(b, pitch_mm=0.5)
    n = lat.W * lat.H * lat.L
    assert n > 1000, f"lattice too small: {n}"
    assert pad_nodes.get(1) and pad_nodes.get(2), "a net got no lattice nodes"
    print(f"    PASS: 20x20 mm synthetic board, 4 pads, "
          f"lattice {lat.W}x{lat.H}x{lat.L} = {n:,} nodes (CPU only)")
finally:
    os.unlink(path)
PYEOF

say "Smoke 2/2: test_board.py (real-board fixtures where available)"
VOXY_FIXTURE="/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb"
if (cd "$REPO" && "$VENVPY" test_board.py); then
    note "test_board.py PASS"
else
    if [ ! -f "$VOXY_FIXTURE" ]; then
        note "test_board.py did not pass on this machine, and its primary fixtures"
        note "(Andrew's laptop-local amp projects under ~/Documents/Guitar/Voxy/) are absent."
        note "That is expected on the Studio with an older checkout of test_board.py."
        note "The synthetic smoke above already validated the parser + lattice here."
        note "For the full suite: fetch bench boards per bench/boards/SOURCES.md and re-run."
    else
        die "test_board.py FAILED with its fixtures present — investigate before using this machine."
    fi
fi

# ------------------------------------------------------------- 6. next steps
say "Setup complete. GPU validation is YOURS to run (this script never touches the GPU):"
cat <<EOF

    cd $REPO

    # 1. SSSP kernel vs CPU Dijkstra — expect 0 mismatches
    .venv/bin/python spike_sssp.py

    # 2. Lattice suite incl. GPU smoke (laptop-only fixtures auto-skip)
    .venv/bin/python test_lattice.py

    # 3. Bench fleet. bench/boards/ is gitignored — re-fetch every board
    #    first, following the URLs + commits + sha256s in
    #    bench/boards/SOURCES.md (keep the same directory names).
    .venv/bin/python bench/run_bench.py --mode baseline   # CPU: human ground truth
    caffeinate -dims .venv/bin/python bench/run_bench.py --mode route   # GPU: the real bench

    Compare the route-mode ratios against the scorecard in README.md.
    Remote Claude Code access, KiCad, and the laptop<->Studio handoff
    workflow are documented in STUDIO.md.

EOF
