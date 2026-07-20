"""boardinfo: the per-board 'info box' the router reads (z-clearance, layout).

The box is a KiCad gr_text the human fills; parsing must be tolerant of the ways
a person actually types it (mm or bare, ':' or '=', any case, front/back or
top/bottom) and must leave anything unstated as None — a missing field falls back
to a CLI value or an unverified flag downstream, never a silent default.

Run: .venv/bin/python test_boardinfo.py
"""
import os
import tempfile
import shutil

import boardinfo as bi

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


if __name__ == "__main__":
    print("=== parse tolerant key:value forms ===")
    a = bi.parse_board_info_text(
        "orchard board info\nmax component height front: 30mm\n"
        "max component height back: 3 mm\nlayout direction: left to right")
    check(a == {"z_front_mm": 30.0, "z_back_mm": 3.0,
                "layout_direction": "left to right"},
          f"the canonical box parses front/back/layout ({a})")
    b = bi.parse_board_info_text("Max Height Back = 5")
    check(b["z_back_mm"] == 5.0 and b["z_front_mm"] is None
          and b["layout_direction"] is None,
          f"bare number, '=', no 'mm', mixed case, only the back stated ({b})")
    c = bi.parse_board_info_text("max height top: 12\nmax height bottom: 4")
    check(c["z_front_mm"] == 12.0 and c["z_back_mm"] == 4.0,
          f"top->front, bottom->back ({c})")
    check(bi.parse_board_info_text("nothing to see here")
          == {"z_front_mm": None, "z_back_mm": None, "layout_direction": None},
          "free text with no recognised field yields all None")
    check(bi.parse_board_info_text("back: 3\nmax component height back: 4"
                                   )["z_back_mm"] == 4.0,
          "a later mention wins (a box edited in place keeps its last value)")
    check(bi.parse_board_info_text("height back 2")["z_back_mm"] is None,
          "'height back' without the 'max' keyword is NOT matched (avoids reading "
          "a stray descr line as a board limit)")
    check(bi.parse_board_info_text("max height back: 3cm")["z_back_mm"] is None,
          "a foreign unit (3cm) is REJECTED, not read as 3 mm — a 10x under-read "
          "could refuse a buildable board (adversarial review)")
    check(bi.parse_board_info_text("max height back: 3")["z_back_mm"] == 3.0
          and bi.parse_board_info_text("max height back: 3 mm")["z_back_mm"] == 3.0,
          "a bare number and an explicit mm both still parse")

    print("=== read_board_info from a real board gr_text ===")
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "b.kicad_pcb")
        with open(p, "w") as f:
            f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                    '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n\t(net 0 "")\n'
                    '\t(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (width 0.1))\n'
                    '\t(gr_text "max component height front: 25mm\\n'
                    'max component height back: 3mm\\nlayout: right to left" '
                    '(at 20 10) (layer "Cmts.User"))\n)\n')
        info = bi.read_board_info(p)
        check(info["z_front_mm"] == 25.0 and info["z_back_mm"] == 3.0
              and info["layout_direction"] == "right to left"
              and info["found"] is True,
              f"read_board_info parses the gr_text box, found=True ({info})")

        q = os.path.join(d, "none.kicad_pcb")
        with open(q, "w") as f:
            f.write('(kicad_pcb (version 20240108) (generator "t")\n'
                    '\t(layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))\n\t(net 0 "")\n'
                    '\t(gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts") (width 0.1))\n)\n')
        info2 = bi.read_board_info(q)
        check(info2["found"] is False and info2["z_back_mm"] is None,
              f"a board with NO info box: found=False, all None (distinguishes "
              f"'box says nothing' from 'no box') ({info2})")
        check(bi.read_board_info("/nonexistent/x.kicad_pcb")["found"] is False,
              "a missing file returns found=False rather than raising")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
