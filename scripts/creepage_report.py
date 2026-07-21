"""Honest creepage / clearance scorecard for a kicad-cli DRC report.

Why this exists: a KiCad net-class `clearance` is enforced as a blanket
pad-to-pad rule. Set HV creepage values (2-7 mm) on the classes and the DRC
report fills with violations that NO placement or router can fix — most of
them between the two pads of a SINGLE component, whose pitch is fixed by the
part (measured on Voxy region 1: 214 of 225 pad-pad clearance violations were
intra-component). Counting those as failures makes a good board look broken and
sends place/route chasing an impossible target.

This tool reads `kicad-cli pcb drc --format json` output and splits the
clearance-family violations into three honest buckets:

  intra-component : both items are pads of the SAME footprint. A part's own pad
                    pitch — unfixable by place or route. Creepage between the
                    terminals of one component is the component's voltage
                    rating, not a PCB rule (see the project's creepage memo).
  same-net        : both items are the same net — connected copper, never a
                    real clearance concern; KiCad already ignores most of these
                    but track/pad mixes can slip through.
  real            : different nets on different footprints — the physically
                    meaningful creepage between distinct nodes. THIS is the
                    number place/route is accountable for.

The metric to judge an HV route by is `real` (plus shorts, which are always
real). It never invents a violation KiCad did not report — it only reclassifies.

Usage: python scripts/creepage_report.py DRC.json [--list]
"""
import json
import os
import re
import sys
from collections import Counter

# "Pad 2 [Net-(Q9-G)] of R110 on F.Cu" / "PTH pad 3 [Net-(Q31-S)] of Q31"
_NET_RE = re.compile(r"\[([^\]]+)\]")
_REF_RE = re.compile(r"\bof (\S+)")

# clearance-FAMILY types: a copper-to-copper spacing rule creepage governs.
# shorting_items is copper actually touching — always real, never reclassified.
CLEARANCE_TYPES = {"clearance", "hole_clearance", "copper_edge_clearance"}


def _item_ref_net(item):
    """(footprint_ref, net) for a violation item, or (None, None) when the item
    is not a pad (a track/via/zone carries a net but no owning footprint ref)."""
    d = item.get("description", "")
    net = _NET_RE.search(d)
    ref = _REF_RE.search(d)
    is_pad = "pad" in d.lower()
    return (ref.group(1) if (ref and is_pad) else None,
            net.group(1) if net else None)


def classify(violations):
    """Split a kicad-cli violations list into honest buckets. Returns a dict:
    {real, intra_component, same_net, non_clearance, shorts} each a list of the
    original violation objects, plus `counts`."""
    out = {"real": [], "intra_component": [], "same_net": [],
           "non_clearance": [], "shorts": []}
    for v in violations:
        t = v.get("type", "")
        if t == "shorting_items":
            out["shorts"].append(v)
            continue
        if t not in CLEARANCE_TYPES:
            out["non_clearance"].append(v)
            continue
        items = v.get("items", [])
        if len(items) != 2:
            out["real"].append(v)           # can't reason about it; keep honest
            continue
        (ra, na), (rb, nb) = (_item_ref_net(items[0]), _item_ref_net(items[1]))
        if na is not None and na == nb:
            out["same_net"].append(v)
        elif ra is not None and ra == rb:
            out["intra_component"].append(v)
        else:
            out["real"].append(v)
    out["counts"] = {k: len(x) for k, x in out.items() if k != "counts"}
    return out


def load(drc_json_path):
    with open(drc_json_path, encoding="utf-8") as f:
        return json.load(f).get("violations", [])


def _short(v):
    desc = (v.get("description") or "")[:70]
    refs = " / ".join(_REF_RE.search(i.get("description", "")).group(1)
                      if _REF_RE.search(i.get("description", "")) else "?"
                      for i in v.get("items", []))
    return f"{desc}  [{refs}]"


def main(argv):
    want_list = "--list" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    viol = load(args[0])
    r = classify(viol)
    c = r["counts"]
    total_clr = c["real"] + c["intra_component"] + c["same_net"]
    print(f"{os.path.basename(args[0])}: {len(viol)} total violations")
    print(f"  clearance-family: {total_clr}")
    print(f"    REAL (distinct nodes, place/route accountable) : {c['real']}")
    print(f"    intra-component (part's own pads, unfixable)   : {c['intra_component']}")
    print(f"    same-net (connected copper, not a concern)     : {c['same_net']}")
    print(f"  shorts (always real)                             : {c['shorts']}")
    print(f"  other DRC types                                  : {c['non_clearance']}")
    print(f"\n  HONEST FAILURE COUNT (real clearance + shorts)   : "
          f"{c['real'] + c['shorts']}")
    if want_list:
        print("\n  real clearance violations:")
        for v in r["real"][:40]:
            print("   ", _short(v))
        print("\n  shorts:")
        for v in r["shorts"][:40]:
            print("   ", _short(v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
