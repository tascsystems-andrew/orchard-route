"""L0 component heights: resolve an above-board height (mm) per footprint for the
two-sided z-clearance check (place/region, feedback/placement-fidelity §3).

A .kicad_pcb rarely states a height (Voxy: 8 of 496 footprints), so a height is
resolved from, in priority:

1. an explicit OVERRIDE the designer gives, by fpid ("LIB:NAME") or by ref
   ("C31") — the precise source, and where a BOM-enrichment pass (real xyz per
   part) feeds in;
2. a HEIGHT the footprint itself states (board._footprint_height reads
   MAXIMUM_PACKAGE_HEIGHT / a height=Nmm in the descr/tags) — free, and catches
   the tall electrolytics/power parts that state it;
3. a CONSERVATIVE family UPPER BOUND from the fpid — an 0603 is at most ~0.6 mm,
   so if even the upper bound clears the enclosure the part is provably safe;
4. None — UNKNOWN, which the z-check FLAGS (cannot verify), never assumes fits.

Erring HIGH is deliberate. Over-estimating a height at worst flags a placement
the designer then confirms; under-estimating would silently bless a part that
fouls the chassis. So the built-in bounds are the tall variant of each family
(tantalum/bulk MLCC, tallest package option), and they are used only as an UPPER
BOUND, never presented as a measured height. This module states heights; it never
decides placement — that is the model's job, from the numbers here.
"""
import json

# Conservative UPPER-BOUND heights (mm) by fpid-name substring — the worst case
# for the family, matched against the part after the "LIB:" prefix. First match
# wins, so order longer/more-specific keys before their prefixes.
_BUILTIN = (
    ("R_0402", 0.6), ("R_0603", 0.6), ("R_0805", 0.7), ("R_1206", 0.8),
    ("R_1210", 0.9), ("R_2010", 0.9), ("R_2512", 0.9),
    ("C_0402", 0.9), ("C_0603", 1.0), ("C_0805", 1.5), ("C_1206", 1.8),
    ("C_1210", 2.8), ("C_1812", 2.8),
    ("CP_Elec", 12.0), ("L_0603", 1.0), ("L_0805", 1.2), ("L_1210", 2.0),
    ("LED_0603", 0.8), ("LED_0805", 0.9), ("LED_1206", 1.1),
    ("SOT-23", 1.3), ("SOT-323", 1.1), ("SOT-353", 1.1), ("SOT-89", 1.6),
    ("SOT-223", 1.8), ("SOD-123", 1.2), ("SOD-323", 1.0), ("SOD", 1.4),
    ("TSSOP", 1.3), ("SSOP", 2.0), ("MSOP", 1.1), ("SOIC", 1.9), ("SOP", 1.9),
    ("QFN", 1.0), ("DFN", 1.0), ("LQFP", 1.7), ("TQFP", 1.3), ("QFP", 1.7),
    ("BGA", 1.5), ("TestPoint", 0.2), ("Fiducial", 0.05),
)


def builtin_upper_bound(fpid):
    """A conservative upper-bound height (mm) for a common footprint family, or
    None if the fpid matches no known family. NEVER a measured value — only a
    'no taller than this' bound the z-check may safely pass a part on."""
    if not fpid:
        return None
    name = fpid.split(":", 1)[-1]
    for sub, h in _BUILTIN:
        if sub in name:
            return h
    return None


def load_overrides(path):
    """{key: mm} from a JSON file mapping an fpid ("LIB:NAME") OR a ref ("C31")
    to a height in mm. This is the precise, designer-owned source (and the shape
    a BOM-enrichment step would emit). Non-positive / non-numeric values are
    dropped. Malformed JSON raises — the caller decides."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[str(k)] = float(v)
    return out


def resolve(ref, fpid, parsed_mm, overrides=None):
    """(height_mm or None, source) for one part. Priority: ref override, fpid
    override, the footprint's own parsed height, the conservative family upper
    bound, else None. `source` names which — "override:ref", "override:fpid",
    "footprint", "family-max", or "unknown" — so a report can flag the parts
    whose height is a bound or a guess rather than measured."""
    ov = overrides or {}
    if ref is not None and ref in ov:
        return ov[ref], "override:ref"
    if fpid and fpid in ov:
        return ov[fpid], "override:fpid"
    if parsed_mm is not None:
        return float(parsed_mm), "footprint"
    ub = builtin_upper_bound(fpid)
    if ub is not None:
        return ub, "family-max"
    return None, "unknown"
