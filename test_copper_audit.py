"""Regression tests for scripts/copper_audit.py — the checker whose numbers
get quoted as proof a routing run is clean.

The bug these exist for: the audit identified router-emitted copper by
(uuid "..."), so on a KiCad-5-era board — where writeback mirrors the source
file and emits nodes carrying NEITHER uuid nor tstamp — it matched nothing
and printed `emitted: 0 tracks, 0 vias / VIOLATIONS: 0` over 700+ real
segments. A confident zero on unmeasured copper is worse than a crash,
because a crash cannot be pasted into a report.

Three properties, each checked on real files rather than mocks:

1. IDENTITY across KiCad's three generations. bench/boards/rpi-rp2040-minimal
   (tstamp) and bench/boards/rpi-pico-vga (KiCad 5, no id on segments at all)
   are routed for real and the audit must report the same emitted counts
   writeback reports appending. A uuid board is checked too, so the fix for
   the legacy case cannot regress the modern one.
2. THE ZERO GUARD. Handed a pair it cannot measure — an output larger than
   its source with no identifiable emitted copper — audit() must raise
   AuditBlind and the CLI must exit non-zero, rather than return a clean
   bill. "I measured nothing" and "I measured zero violations" must not be
   the same output.
3. BROAD-PHASE COMPLETENESS. The uniform-grid index must find every pair the
   exhaustive O(n*m) scan finds, on real routed boards. The index is an
   optimization; if it disagrees, it is a lie with a fast path.

Boards live in bench/boards/ (gitignored — see bench/boards/SOURCES.md);
sections whose fixture is missing SKIP loudly rather than silently pass.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "scripts"))

import copper_audit as ca                                    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out", "test-copper-audit")
BENCH = os.path.join(HERE, "bench", "boards")

# (label, board, pitch) — one board per node-identity generation.
FIXTURES = [
    ("tstamp (KiCad 6/7)",
     os.path.join(BENCH, "rpi-rp2040-minimal", "RP2040_minimal_r2.kicad_pcb"),
     0.25),
    ("no id (KiCad 5)",
     os.path.join(BENCH, "rpi-pico-vga", "pico_vga_sd_aud.kicad_pcb"), 0.5),
    ("uuid (KiCad 8+)",
     os.path.join(BENCH, "icebreaker-bitsy-v1.1c", "icebreaker-bitsy.kicad_pcb"),
     0.25),
]

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def skip(name, why):
    print(f"  SKIP {name}  — {why}")


def route(board, out, pitch):
    """Route `board` to `out` with writeback's CLI; returns its stdout."""
    os.makedirs(OUT_DIR, exist_ok=True)
    import writeback
    from io import StringIO
    buf, real = StringIO(), sys.stdout
    sys.stdout = buf
    try:
        writeback.main([board, out, "--pitch", str(pitch),
                        "--layers", "F.Cu,B.Cu"])
    finally:
        sys.stdout = real
    return buf.getvalue()


def emitted_from_log(log):
    """(tracks, vias) writeback SAYS it appended — the independent number the
    audit's own count must match. Parsed from its stats block, not from any
    shared code path."""
    t = v = None
    for line in log.splitlines():
        if line.startswith("tracks      :"):
            t = int(line.split(":")[1].split()[0])
        if line.startswith("vias        :"):
            v = int(line.split(":")[1].split()[0])
    return t, v


def test_identity_generations():
    print("=== emitted copper is found on every KiCad node-identity generation ===")
    routed = {}
    for label, board, pitch in FIXTURES:
        if not os.path.isfile(board):
            skip(label, f"{board} absent (bench/boards is gitignored)")
            continue
        out = os.path.join(OUT_DIR, f"{label.split()[0]}-routed.kicad_pcb")
        log = route(board, out, pitch)
        said_t, said_v = emitted_from_log(log)
        segs, vias, ident = ca.emitted_copper(board, out)
        check(f"{label}: audit sees the tracks writeback wrote",
              len(segs) == said_t,
              f"audit {len(segs)} vs writeback {said_t}")
        check(f"{label}: audit sees the vias writeback wrote",
              len(vias) == said_v, f"audit {len(vias)} vs writeback {said_v}")
        check(f"{label}: emitted copper is non-zero", len(segs) > 0,
              f"{len(segs)} tracks")
        # The source's own copper must NOT be counted as emitted: the whole
        # point of the checker is router-copper vs router-copper.
        check(f"{label}: source copper excluded",
              len(segs) == ident["routed_tracks"] - ident["source_tracks"],
              f"routed {ident['routed_tracks']} - source "
              f"{ident['source_tracks']}")
        vio, stats = ca.audit(board, out)
        check(f"{label}: audit() agrees with emitted_copper()",
              stats["emitted_tracks"] == len(segs)
              and stats["emitted_vias"] == len(vias))
        print(f"       {label}: {len(segs)} tracks, {len(vias)} vias, "
              f"{len(vio)} violations, {stats['ids_present']} items carry an id")
        routed[label] = (board, out)
    return routed


def test_zero_guard(routed):
    print("=== a zero the checker cannot justify must NOT print as clean ===")
    if not routed:
        skip("zero guard", "no fixture routed")
        return
    board, out = next(iter(routed.values()))

    # A deliberately unmeasurable pair: same copper, bigger file. Whitespace
    # is invisible to the parser and visible to os.path.getsize, which is
    # exactly the shape of the real failure (output grew, audit saw nothing).
    padded = os.path.join(OUT_DIR, "padded-no-new-copper.kicad_pcb")
    # newline="" throughout: RP2040_minimal_r2 is a CRLF file, and universal
    # newline translation would shrink the copy by one byte per line — the
    # guard trips on BYTES, so the fixture has to round-trip as bytes.
    with open(board, encoding="utf-8", newline="") as f:
        text = f.read()
    body = text.rstrip()
    with open(padded, "w", encoding="utf-8", newline="") as f:
        f.write(body[:-1] + "\n" * 5000 + body[-1:])
    check("padded copy really is larger",
          os.path.getsize(padded) > os.path.getsize(board))

    raised = None
    try:
        ca.audit(board, padded)
    except ca.AuditBlind as e:
        raised = e
    check("audit() raises AuditBlind instead of returning 0 violations",
          raised is not None,
          "returned a clean bill on unmeasured copper" if raised is None else "")
    if raised is not None:
        msg = str(raised)
        check("the message says it MEASURED NOTHING", "MEASURED NOTHING" in msg)
        check("and warns off quoting the number",
              "do not quote" in msg.lower(), msg[:90])

    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "scripts", "copper_audit.py"),
         board, padded], capture_output=True, text=True)
    check("CLI exits non-zero on an unmeasurable pair", proc.returncode != 0,
          f"exit {proc.returncode}")
    check("CLI does not print a VIOLATIONS count it cannot justify",
          "VIOLATIONS" not in proc.stdout, proc.stdout.strip()[:120])
    check("CLI says AUDIT FAILED", "AUDIT FAILED" in proc.stdout)

    # The complement: a genuinely clean run must still report 0 violations
    # normally. The guard must not make every zero an error.
    same = os.path.join(OUT_DIR, "identical-copy.kicad_pcb")
    with open(same, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    vio, stats = ca.audit(board, same)
    check("an identical copy reports 0 emitted, 0 violations, no raise",
          not vio and stats["emitted_tracks"] == 0,
          f"{len(vio)} violations")


def test_broad_phase_vs_brute_force(routed):
    print("=== the spatial index must find every pair brute force finds ===")
    if not routed:
        skip("brute force", "no fixture routed")
        return
    for label, (board, out) in routed.items():
        fast, fstats = ca.audit(board, out)
        slow, sstats = ca.audit_bruteforce(board, out)
        fk = sorted(map(repr, fast))
        sk = sorted(map(repr, slow))
        check(f"{label}: broad phase == brute force", fk == sk,
              f"{len(fast)} vs {len(slow)} over {fstats['pairs_tested']} vs "
              f"{sstats['pairs_tested']} pairs")
        if fk != sk:
            from collections import Counter
            missed = Counter(sk) - Counter(fk)
            for r in list(missed)[:3]:
                print(f"       MISSED {r}")
        else:
            print(f"       {label}: {len(fast)} violations agreed; index "
                  f"tested {fstats['pairs_tested']} pairs vs "
                  f"{sstats['pairs_tested']} exhaustive "
                  f"({sstats['pairs_tested'] / max(fstats['pairs_tested'], 1):.1f}x "
                  f"fewer)")


def test_inflation_is_what_makes_it_complete(routed):
    print("=== and it is the CLEARANCE INFLATION that makes it complete ===")
    # Not a tautology check: bucket on the raw centreline bbox (the pre-fix
    # behaviour) and the index must MISS pairs brute force finds. If this
    # ever passes, the inflation is not doing the job the comment claims and
    # the agreement above is luck.
    if not routed:
        skip("inflation", "no fixture routed")
        return
    label, (board, out) = next(iter(routed.items()))
    segs, vias, _ = ca.emitted_copper(board, out)
    clearance, _ = ca.default_copper_rules(board)
    items = ca._items_for(segs, vias)
    import math
    cell = max(2.0, clearance + 2 * max((it[5] for it in items), default=0.5))
    grid = {}
    for i, it in enumerate(items):
        x0, x1 = sorted((it[1], it[3]))
        y0, y1 = sorted((it[2], it[4]))          # NO pad: the old, wrong way
        for gx in range(int(math.floor(x0 / cell)), int(math.floor(x1 / cell)) + 1):
            for gy in range(int(math.floor(y0 / cell)),
                            int(math.floor(y1 / cell)) + 1):
                grid.setdefault((gx, gy), []).append(i)
    seen, found = set(), []
    for bucket in grid.values():
        for ii in range(len(bucket)):
            for jj in range(ii + 1, len(bucket)):
                key = (min(bucket[ii], bucket[jj]), max(bucket[ii], bucket[jj]))
                if key in seen:
                    continue
                seen.add(key)
                v = ca._pair_violation(items[bucket[ii]], items[bucket[jj]],
                                       clearance)
                if v is not None:
                    found.append(v)
    slow, _ = ca.audit_bruteforce(board, out)
    check(f"{label}: un-inflated bucketing DOES undercount (so the fix bites)",
          len(found) < len(slow),
          f"un-inflated {len(found)} vs true {len(slow)}")


def main():
    routed = test_identity_generations()
    test_zero_guard(routed)
    test_broad_phase_vs_brute_force(routed)
    test_inflation_is_what_makes_it_complete(routed)
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
