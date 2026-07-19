"""L1: the manufacturing contract — will the fab house actually build this?

geometry.py answers "does this copper fit its own routing grid?". That is a
question about the LATTICE. This module answers the other half: "will the
board house etch it, and will they etch it at the price on the front page?"
That is a question about a PROCESS, and the two answers do not imply each
other. A 0.45 mm via fits a 0.5 mm grid and JLCPCB builds it for free; a
0.25 mm via fits the same grid and costs a surcharge; a 0.6 mm via is free
everywhere and does not fit the grid at all.

Two ideas run through everything here:

  CAPABILITY vs STANDARD PRICE.  Every house publishes a capability table —
  the finest geometry their process can hold. Almost none of it is included
  in the prototype price. A profile's `tier` says which question its numbers
  answer: "standard" is the no-surcharge floor (what you get for the advertised
  price), "extended" is the process floor (what they CAN do, for money). Mixing
  them is how a $5 board becomes a $40 board, so they are separate profiles and
  the tier prints on every line.

  DATA, NOT LOGIC.  Every number lives in PROFILE_DATA below with the URL it
  came from and the date it was read. Fab specs change — JLCPCB re-tiered via
  pricing in living memory — so a profile that has not been re-verified says so
  out loud (see `stale_warning`). Add a house by adding a dict; no function in
  this module knows the name of any fab.

Nothing here changes the user's numbers. `check()` reports, `recommend()`
proposes; only an explicit enforce request substitutes values, and it says
exactly what it changed.
"""
import datetime
import json
import math
import os
from dataclasses import dataclass, field

MM_PER_MIL = 0.0254

VERIFIED_ON = "2026-07-19"

#: A profile older than this many days is reported as stale. Fab process
#: tiers move on a scale of months; half a year is a generous ceiling.
STALE_AFTER_DAYS = 180

# ── sources ─────────────────────────────────────────────────────────────
# Every number in PROFILE_DATA cites one of these keys. Keep the key short:
# it is what prints in a violation message.
SOURCES = {
    "jlc-cap": "https://jlcpcb.com/capabilities/pcb-capabilities",
    "jlc-extra": "https://jlcpcb.com/help/article/"
                 "in-what-cases-will-there-be-charged-extra",
    "jlc-pitfalls": "https://jlcpcb.com/blog/how-to-avoid-pitfalls-in-pcb-design",
    "pcbway-cap": "https://www.pcbway.com/capabilities.html",
    "pcbway-tol": "https://www.pcbway.com/pcb_prototype/"
                  "PCB_Manufacturing_tolerances.html",
    "pcbway-drc": "https://www.pcbway.com/pcb_prototype/PCB_Design_Rule_Check.html",
}

# ── the profiles ────────────────────────────────────────────────────────
# Field meanings (all mm; None = this profile imposes no limit on it):
#
#   min_track_mm          narrowest track the tier etches
#   min_clearance_mm      smallest copper-to-copper gap the tier etches
#   min_via_diameter_mm   smallest via PAD (outer) diameter
#   min_via_drill_mm      smallest finished via hole
#   min_annular_ring_mm   (via_diameter - via_drill) / 2
#   min_via_to_via_mm     copper edge gap between two vias
#   min_via_to_track_mm   copper edge gap between a via and a foreign track
#   min_hole_to_hole_mm   hole EDGE to hole EDGE (drilling, not etching)
#   min_edge_clearance_mm copper to the routed board outline
#
# `sources` maps a field name (or "*" for the rest) to a SOURCES key.
# Anything the house does not publish is None with a line in `notes` — an
# absent number must never be inferred, only reported as absent.

PROFILE_DATA = {
    # ── the null profile: today's behaviour, no manufacturing opinion ──
    "none": dict(
        house="(none)", tier="none", layers="any",
        verified_on=VERIFIED_ON,
        min_track_mm=None, min_clearance_mm=None,
        min_via_diameter_mm=None, min_via_drill_mm=None,
        min_annular_ring_mm=None,
        min_via_to_via_mm=None, min_via_to_track_mm=None,
        min_hole_to_hole_mm=None, min_edge_clearance_mm=None,
        blind_buried="unconstrained", via_in_pad="unconstrained",
        sources={},
        notes="No manufacturing constraints. This is the default so that "
              "adding --fab is always an explicit act: no run silently "
              "acquires or loses a limit.",
    ),

    # ── JLCPCB, 1-2 layer ─────────────────────────────────────────────
    "jlcpcb-standard": dict(
        house="JLCPCB", tier="standard", layers="1-2",
        verified_on=VERIFIED_ON,
        # 1 oz outer copper, 1-2 layer: "0.10/0.10 mm (4/4 mil)". JLCPCB's
        # published surcharge list starts at 3.0-3.5 mil (multilayer) and
        # 2-3 mil; 4 mil on a 2-layer board triggers none of them.
        min_track_mm=0.10,
        min_clearance_mm=0.10,
        # The no-surcharge via. jlc-extra: no extra charge when the via hole
        # is >= 0.3 mm AND the via diameter is >= 0.4 mm. jlc-pitfalls gives
        # JLCPCB's own single/double-sided via minimum as 0.3 mm hole /
        # 0.45 mm outer, which is the cheapest via that clears both rules.
        min_via_diameter_mm=0.45,
        min_via_drill_mm=0.30,
        # "Outer diameters should be 0.1 mm larger than the inner diameters,
        # with a recommended difference of 0.15 mm or more" -> ring >= 0.05,
        # recommended >= 0.075. The 0.18 mm figure on the capability page is
        # the PTH (component through-hole) ring, not the via ring; JLCPCB's
        # own Q&A leaves that distinction unresolved (see notes).
        min_annular_ring_mm=0.05,
        min_via_to_via_mm=0.20,
        min_via_to_track_mm=0.20,
        min_hole_to_hole_mm=0.45,
        min_edge_clearance_mm=0.20,
        blind_buried="not supported",
        via_in_pad="not standard at 1-2 layers (default for 6-layer and above)",
        sources={"min_track_mm": "jlc-cap", "min_clearance_mm": "jlc-cap",
                 "min_via_diameter_mm": "jlc-extra",
                 "min_via_drill_mm": "jlc-extra",
                 "min_annular_ring_mm": "jlc-pitfalls",
                 "*": "jlc-cap"},
        notes="No-surcharge tier. Surcharge triggers that apply to copper "
              "geometry: via hole < 0.3 mm AND via diameter <= 0.4 mm; "
              "trace width or spacing 2-3 mil (+20%); more than 150,000 "
              "drill holes per square metre. AMBIGUOUS: the capability page "
              "lists a PTH annular ring minimum of 0.18 mm while the same "
              "house publishes a 0.3/0.45 mm via (ring 0.075 mm); JLCPCB's "
              "own Q&A raises this contradiction and does not answer it. "
              "This profile applies the via rule to vias and does not model "
              "the PTH pad rule at all -- the router does not emit PTH pads.",
    ),
    "jlcpcb-extended": dict(
        house="JLCPCB", tier="extended", layers="1-2",
        verified_on=VERIFIED_ON,
        # Process floor, all of it surcharged or quote-on-review.
        min_track_mm=2 * MM_PER_MIL,       # 0.0508 mm, +20%
        min_clearance_mm=2 * MM_PER_MIL,
        min_via_diameter_mm=0.25,
        min_via_drill_mm=0.15,
        min_annular_ring_mm=0.05,
        min_via_to_via_mm=0.20,
        min_via_to_track_mm=0.20,
        min_hole_to_hole_mm=0.45,
        min_edge_clearance_mm=0.20,
        blind_buried="not supported at any tier",
        via_in_pad="available as epoxy or copper-paste fill (extra)",
        sources={"min_track_mm": "jlc-extra", "min_clearance_mm": "jlc-extra",
                 "min_via_diameter_mm": "jlc-cap",
                 "min_via_drill_mm": "jlc-cap",
                 "min_annular_ring_mm": "jlc-pitfalls",
                 "*": "jlc-cap"},
        notes="CAPABILITY, NOT PRICE. Everything below the standard tier "
              "costs money: 2-3 mil trace/space is +20%, sub-0.3 mm holes "
              "are surcharged per the extra-charge list. Blind/buried vias "
              "remain unavailable at any price on the prototype service.",
    ),

    # ── JLCPCB, 4 layer ───────────────────────────────────────────────
    "jlcpcb-standard-4layer": dict(
        house="JLCPCB", tier="standard", layers="4",
        verified_on=VERIFIED_ON,
        # Capability floor is 0.09 mm (3.5 mil), but 3.0-3.5 mil on a
        # multilayer board carries a 20-30% surcharge, so the free floor is
        # 4 mil.
        min_track_mm=0.10,
        min_clearance_mm=0.10,
        # jlc-extra: on multilayer, a 0.2 mm hole is free provided the via
        # diameter is >= 0.45 mm.
        min_via_diameter_mm=0.45,
        min_via_drill_mm=0.20,
        min_annular_ring_mm=0.05,
        min_via_to_via_mm=0.20,
        min_via_to_track_mm=0.20,
        min_hole_to_hole_mm=0.45,
        min_edge_clearance_mm=0.20,
        blind_buried="not supported",
        via_in_pad="extra at 4 layers (default for 6-layer and above)",
        sources={"min_track_mm": "jlc-extra", "min_clearance_mm": "jlc-extra",
                 "min_via_diameter_mm": "jlc-extra",
                 "min_via_drill_mm": "jlc-extra",
                 "min_annular_ring_mm": "jlc-pitfalls",
                 "*": "jlc-cap"},
        notes="4-layer buys a smaller FREE hole (0.2 mm vs 0.3 mm) at the "
              "same 0.45 mm via pad, because the multilayer line drills "
              "finer as standard. It does NOT buy free finer trace/space: "
              "3.0-3.5 mil is +20-30% on multilayer.",
    ),

    # ── PCBWay, 1-2 layer ─────────────────────────────────────────────
    "pcbway-standard": dict(
        house="PCBWay", tier="standard", layers="1-2",
        verified_on=VERIFIED_ON,
        # PCBWay publishes a 0.1 mm/4 mil capability alongside the standing
        # instruction to "strongly suggest to design trace width/spacing
        # above 6mil(0.15mm) to save cost". They publish no table tying
        # track/space to price, so 6 mil is the only defensible free floor.
        min_track_mm=6 * MM_PER_MIL,       # 0.1524 mm
        min_clearance_mm=6 * MM_PER_MIL,
        # pcbway-tol: "Any holes greater than 6.3mm or smaller than 0.3mm
        # will be subject to extra charges." Ring minimum 0.15 mm (6 mil),
        # so the cheapest via pad is 0.30 + 2*0.15 = 0.60 mm.
        min_via_diameter_mm=0.60,
        min_via_drill_mm=0.30,
        min_annular_ring_mm=0.15,
        min_via_to_via_mm=11 * MM_PER_MIL,     # 0.2794 mm
        min_via_to_track_mm=None,              # not published separately
        min_hole_to_hole_mm=16 * MM_PER_MIL,   # 0.4064 mm
        min_edge_clearance_mm=0.25,
        blind_buried="advanced service only, not the prototype tier",
        via_in_pad="resin fill is a paid additional option",
        sources={"min_track_mm": "pcbway-drc", "min_clearance_mm": "pcbway-drc",
                 "min_via_drill_mm": "pcbway-tol",
                 "min_annular_ring_mm": "pcbway-tol",
                 "min_via_diameter_mm": "pcbway-tol",
                 "*": "pcbway-cap"},
        notes="No-surcharge tier, reconstructed rather than quoted: PCBWay "
              "publishes ONE explicit price rule for copper geometry "
              "(holes < 0.3 mm cost extra) and one recommendation (6 mil "
              "trace/space 'to save cost'). min_via_diameter_mm is DERIVED "
              "(0.30 drill + 2 x 0.15 ring), not published as a via minimum. "
              "AMBIGUOUS: the 11 mil via spacing on the capability page does "
              "not say whether it is copper edge gap or hole-to-hole; this "
              "profile treats it as a copper gap, which is the conservative "
              "reading. UNVERIFIED: whether 5 mil or 4 mil trace/space "
              "actually adds cost, and by how much -- PCBWay's quote form "
              "offers 3/4/5/6/8 mil but publishes no price delta.",
    ),
    "pcbway-extended": dict(
        house="PCBWay", tier="extended", layers="1-2",
        verified_on=VERIFIED_ON,
        min_track_mm=3 * MM_PER_MIL,       # 0.0762 mm
        min_clearance_mm=3 * MM_PER_MIL,
        # 0.15 mm drill + 0.15 mm ring each side.
        min_via_diameter_mm=0.45,
        min_via_drill_mm=0.15,
        min_annular_ring_mm=0.15,
        min_via_to_via_mm=11 * MM_PER_MIL,
        min_via_to_track_mm=None,
        min_hole_to_hole_mm=16 * MM_PER_MIL,
        min_edge_clearance_mm=0.25,
        blind_buried="available on the advanced service (extra)",
        via_in_pad="resin fill available (extra)",
        sources={"min_track_mm": "pcbway-drc", "min_clearance_mm": "pcbway-drc",
                 "min_via_drill_mm": "pcbway-cap",
                 "min_annular_ring_mm": "pcbway-tol",
                 "min_via_diameter_mm": "pcbway-tol",
                 "*": "pcbway-cap"},
        notes="CAPABILITY, NOT PRICE. 3 mil trace/space is the stated "
              "process floor; sub-0.3 mm holes are explicitly surcharged. "
              "PCBWay does not publish the surcharge amounts, so the cost of "
              "this tier is quote-only -- unlike JLCPCB, which at least "
              "names its percentages.",
    ),

    # ── PCBWay, 4 layer ───────────────────────────────────────────────
    "pcbway-standard-4layer": dict(
        house="PCBWay", tier="standard", layers="4",
        verified_on=VERIFIED_ON,
        min_track_mm=6 * MM_PER_MIL,
        min_clearance_mm=6 * MM_PER_MIL,
        min_via_diameter_mm=0.60,
        min_via_drill_mm=0.30,
        min_annular_ring_mm=0.15,
        min_via_to_via_mm=11 * MM_PER_MIL,
        min_via_to_track_mm=None,
        min_hole_to_hole_mm=16 * MM_PER_MIL,
        min_edge_clearance_mm=0.25,
        blind_buried="advanced service only",
        via_in_pad="resin fill is a paid additional option",
        sources={"min_track_mm": "pcbway-drc", "min_clearance_mm": "pcbway-drc",
                 "min_via_drill_mm": "pcbway-tol",
                 "min_annular_ring_mm": "pcbway-tol",
                 "min_via_diameter_mm": "pcbway-tol",
                 "*": "pcbway-cap"},
        notes="UNVERIFIED: PCBWay's published capability table does not "
              "separate 4-layer from 1-2 layer for trace, spacing, hole or "
              "ring, and no free-tier 4-layer relaxation is documented. "
              "This profile therefore repeats the 1-2 layer numbers rather "
              "than inventing a multilayer relaxation. If you are ordering "
              "4-layer from PCBWay, get the numbers from the quote form.",
    ),
}


# ── errors ──────────────────────────────────────────────────────────────
class UnknownProfile(KeyError):
    """Named profile is not in PROFILE_DATA."""


class FabPitchError(ValueError):
    """No copper geometry legal for this house fits the requested pitch.

    Carries `required_pitch_mm`: the pitch at which the house's cheapest
    legal geometry WOULD fit, so the caller can say what to change.
    """

    def __init__(self, message, required_pitch_mm=None):
        super().__init__(message)
        self.required_pitch_mm = required_pitch_mm


# ── the profile object ──────────────────────────────────────────────────
@dataclass(frozen=True)
class FabProfile:
    """One house at one price tier. Every number is a floor, in mm; None
    means this profile takes no position on that dimension."""
    name: str
    house: str
    tier: str
    layers: str
    verified_on: str
    min_track_mm: float = None
    min_clearance_mm: float = None
    min_via_diameter_mm: float = None
    min_via_drill_mm: float = None
    min_annular_ring_mm: float = None
    min_via_to_via_mm: float = None
    min_via_to_track_mm: float = None
    min_hole_to_hole_mm: float = None
    min_edge_clearance_mm: float = None
    blind_buried: str = "unconstrained"
    via_in_pad: str = "unconstrained"
    sources: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def constrains(self):
        """True when this profile imposes anything at all. The `none` profile
        is the one that does not, and every wiring site branches on this
        rather than on the name."""
        return any(getattr(self, f) is not None for f in _LIMIT_FIELDS)

    def source_for(self, field_name):
        """Resolved URL backing one field: its own citation, else the
        profile's "*" fallback, else "" for the `none` profile."""
        key = self.sources.get(field_name) or self.sources.get("*")
        return SOURCES.get(key, "") if key else ""

    def verified_age_days(self, today=None):
        """Days since verified_on, or None if the date is unparseable."""
        today = today or datetime.date.today()
        try:
            then = datetime.date.fromisoformat(self.verified_on)
        except (TypeError, ValueError):
            return None
        return (today - then).days

    def is_stale(self, today=None, max_age_days=STALE_AFTER_DAYS):
        age = self.verified_age_days(today)
        return True if age is None else age > max_age_days

    def stale_warning(self, today=None, max_age_days=STALE_AFTER_DAYS):
        """A loud line when this profile's numbers are old enough to distrust,
        else None. Fab tiers move; an unverified profile is a guess wearing a
        citation."""
        if not self.constrains:
            return None
        age = self.verified_age_days(today)
        if age is None:
            return (f"STALE FAB PROFILE: {self.name} has an unparseable "
                    f"verified_on ({self.verified_on!r}). Treat every number "
                    f"in it as unverified and re-read the source pages.")
        if age > max_age_days:
            return (f"STALE FAB PROFILE: {self.name} was verified "
                    f"{self.verified_on} ({age} days ago, limit "
                    f"{max_age_days}). Fab process tiers and surcharge "
                    f"thresholds change; re-read {self.source_for('*') or 'the source pages'} "
                    f"before trusting this run's manufacturing verdict.")
        return None

    def describe(self):
        """Multi-line human dump of every number and its source."""
        out = [f"{self.name}  [{self.house} / {self.tier} tier / "
               f"{self.layers} layer]  verified {self.verified_on}"]
        for f in _LIMIT_FIELDS:
            v = getattr(self, f)
            label = _LABELS[f]
            if v is None:
                out.append(f"  {label:<22} (not constrained)")
            else:
                src = self.source_for(f)
                out.append(f"  {label:<22} >= {v:.4g} mm"
                           + (f"   {src}" if src else ""))
        out.append(f"  {'blind/buried vias':<22} {self.blind_buried}")
        out.append(f"  {'via in pad':<22} {self.via_in_pad}")
        if self.notes:
            out.append(f"  notes: {self.notes}")
        return "\n".join(out)


_LIMIT_FIELDS = ("min_track_mm", "min_clearance_mm", "min_via_diameter_mm",
                 "min_via_drill_mm", "min_annular_ring_mm",
                 "min_via_to_via_mm", "min_via_to_track_mm",
                 "min_hole_to_hole_mm", "min_edge_clearance_mm")

_LABELS = {
    "min_track_mm": "track width",
    "min_clearance_mm": "clearance",
    "min_via_diameter_mm": "via diameter",
    "min_via_drill_mm": "via drill",
    "min_annular_ring_mm": "annular ring",
    "min_via_to_via_mm": "via-to-via gap",
    "min_via_to_track_mm": "via-to-track gap",
    "min_hole_to_hole_mm": "hole-to-hole gap",
    "min_edge_clearance_mm": "edge clearance",
}


# ── loading ─────────────────────────────────────────────────────────────
def list_profiles():
    """Profile names, sorted, `none` first so the default is visible."""
    rest = sorted(n for n in PROFILE_DATA if n != "none")
    return (["none"] if "none" in PROFILE_DATA else []) + rest


def load_profile(name):
    """FabProfile by name. Raises UnknownProfile naming the alternatives.

    None or "" loads `none`, so callers can pass an unset CLI flag straight
    through without branching.
    """
    if not name:
        name = "none"
    data = PROFILE_DATA.get(name)
    if data is None:
        raise UnknownProfile(
            f"unknown fab profile {name!r}; known profiles: "
            f"{', '.join(list_profiles())}")
    return FabProfile(name=name, **data)


def load_profiles_file(path):
    """Merge a user's JSON file of extra profiles into PROFILE_DATA.

    The file is {name: {field: value}} using exactly the PROFILE_DATA schema,
    so a user extends the tool with data and never touches this module's
    logic. Returns the names added. Existing names are overwritten, which is
    how you patch a number that changed without waiting for a release.
    """
    with open(path, encoding="utf-8") as f:
        extra = json.load(f)
    if not isinstance(extra, dict):
        raise ValueError(f"{path}: expected a JSON object of "
                         f"{{profile_name: {{fields}}}}")
    added = []
    for name, data in extra.items():
        if not isinstance(data, dict):
            raise ValueError(f"{path}: profile {name!r} is not an object")
        merged = dict(data)
        merged.setdefault("house", "(user)")
        merged.setdefault("tier", "user")
        merged.setdefault("layers", "any")
        merged.setdefault("verified_on", "")
        FabProfile(name=name, **merged)     # validate the schema now, loudly
        PROFILE_DATA[name] = merged
        added.append(name)
    return added


# ── violations ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Violation:
    """One number that is below one profile's floor. Human-readable by
    construction: `message` states the value, the limit and the source, which
    is the whole reason this type exists rather than a bare bool."""
    field: str
    label: str
    value_mm: float
    limit_mm: float
    profile: str
    source: str
    message: str

    def __str__(self):
        return self.message


def _violation(profile, field_name, value_mm, limit_mm, extra=""):
    label = _LABELS.get(field_name, field_name)
    src = profile.source_for(field_name)
    msg = (f"{label} {value_mm:.4g}mm is below {profile.name} minimum "
           f"{limit_mm:.4g}mm")
    if extra:
        msg += f" ({extra})"
    msg += f" (source: {src or 'no citation recorded'})"
    return Violation(field=field_name, label=label, value_mm=float(value_mm),
                     limit_mm=float(limit_mm), profile=profile.name,
                     source=src, message=msg)


def check(geometry, profile, via_drill_mm=None):
    """Test a CopperGeometry against a profile. Returns [Violation], empty
    when the geometry is buildable at that house and tier.

    geometry: a geometry.CopperGeometry (anything with track_width_mm,
        clearance_mm, via_size_mm works — this module does not import it, to
        keep the L1 modules siblings rather than a chain).
    via_drill_mm: the drill the writer will emit. Optional because the
        geometry contract does not carry one; when absent, the drill and
        annular-ring rules are SKIPPED rather than assumed, and that omission
        is the caller's to disclose.

    The via-to-via and via-to-track rules are tested against the geometry's
    clearance, because that is the edge gap the router's via halo actually
    produces (via_size + clearance centre-to-centre leaves exactly clearance
    of copper gap). A house asking more between vias than between tracks
    therefore shows up as a clearance violation, which is the number the user
    can act on.
    """
    if not profile.constrains:
        return []
    out = []
    track = float(geometry.track_width_mm)
    clear = float(geometry.clearance_mm)
    via = float(geometry.via_size_mm)

    def test(field_name, value, extra=""):
        limit = getattr(profile, field_name)
        if limit is not None and value < limit - 1e-9:
            out.append(_violation(profile, field_name, value, limit, extra))

    test("min_track_mm", track)
    test("min_clearance_mm", clear)
    test("min_via_diameter_mm", via)
    test("min_via_to_via_mm", clear,
         "the via halo leaves exactly the clearance between two vias' copper")
    test("min_via_to_track_mm", clear,
         "the via halo leaves exactly the clearance to a foreign track")

    if via_drill_mm is not None:
        drill = float(via_drill_mm)
        test("min_via_drill_mm", drill)
        ring = (via - drill) / 2.0
        limit = profile.min_annular_ring_mm
        if limit is not None and ring < limit - 1e-9:
            out.append(_violation(
                profile, "min_annular_ring_mm", ring, limit,
                f"via {via:.4g}mm pad on a {drill:.4g}mm drill"))
    return out


# ── recommendation ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class Recommendation:
    """The cheapest-legal copper for one house at one pitch."""
    track_mm: float
    clearance_mm: float
    via_size_mm: float
    via_drill_mm: float
    profile: str

    def __iter__(self):
        """Unpacks as (track, clearance, via_size, drill)."""
        return iter((self.track_mm, self.clearance_mm,
                     self.via_size_mm, self.via_drill_mm))

    def summary(self):
        return (f"track {self.track_mm:.3g} clearance {self.clearance_mm:.3g} "
                f"via {self.via_size_mm:.3g}/{self.via_drill_mm:.3g} "
                f"({self.profile})")


def required_pitch_mm(track_mm, clearance_mm, via_size_mm):
    """Smallest routing pitch that carries this copper: the larger of the
    orthogonal track-track rule and the via halo rule, both restated from
    geometry.py so this module can answer without importing it."""
    return max(track_mm + clearance_mm,
               via_size_mm / 2.0 + track_mm / 2.0 + clearance_mm)


def recommend(profile, pitch_mm):
    """Cheapest copper geometry that is legal at this house AND fits pitch_mm.

    Returns a Recommendation (unpackable as track, clearance, via, drill), or
    None for the `none` profile, which has no opinion to offer.

    "Cheapest" means the profile's floors: at a standard-tier profile those
    ARE the no-surcharge minimums, and they are simultaneously the most
    routable copper the house will build for the base price. There is no
    tradeoff to tune here, which is why this returns one answer rather than a
    ranked list.

    Raises FabPitchError, carrying required_pitch_mm, when even the house's
    finest free geometry needs a coarser grid than the caller asked for. That
    error is the useful output: it names the pitch that would work.
    """
    if not profile.constrains:
        return None

    track = profile.min_track_mm or 0.0
    # Clearance must satisfy the plain copper rule AND any via-specific gap,
    # because the router emits one clearance number for all of it.
    clearance = max(profile.min_clearance_mm or 0.0,
                    profile.min_via_to_via_mm or 0.0,
                    profile.min_via_to_track_mm or 0.0)
    drill = profile.min_via_drill_mm or 0.0
    via = max(profile.min_via_diameter_mm or 0.0,
              drill + 2.0 * (profile.min_annular_ring_mm or 0.0))

    need = required_pitch_mm(track, clearance, via)
    if pitch_mm < need - 1e-9:
        ortho = track + clearance
        halo = via / 2.0 + track / 2.0 + clearance
        binding = "the via halo" if halo >= ortho else "orthogonal track-track"
        raise FabPitchError(
            f"no {profile.name} geometry fits a {pitch_mm:.3g}mm pitch: the "
            f"cheapest legal copper at {profile.house} is track "
            f"{track:.3g} / clearance {clearance:.3g} / via {via:.3g} on a "
            f"{drill:.3g} drill, and {binding} needs {need:.3g}mm "
            f"(track+clearance {ortho:.3g}, via halo {halo:.3g}). "
            f"Route at {need:.3g}mm pitch or coarser, or accept the "
            f"{profile.house} extended tier and its surcharges.",
            required_pitch_mm=need)

    return Recommendation(track_mm=track, clearance_mm=clearance,
                          via_size_mm=via, via_drill_mm=drill,
                          profile=profile.name)


# ── the printed contract ────────────────────────────────────────────────
def summary_line(geometry, profile, via_drill_mm=None, violations=None):
    """The one line every run prints beside the geometry contract.

    fab: NAME | track W OK (min M) | via V OK (min M) | clearance C OK (min M)
         | verified DATE
    """
    if not profile.constrains:
        return f"{profile.name} (no manufacturing constraints applied)"
    if violations is None:
        violations = check(geometry, profile, via_drill_mm)
    bad = {v.field for v in violations}

    def part(field_name, value):
        limit = getattr(profile, field_name)
        if limit is None:
            return f"{_LABELS[field_name].split()[0]} {value:.3g} (unconstrained)"
        verdict = "FAIL" if field_name in bad else "OK"
        return (f"{_LABELS[field_name].split()[0]} {value:.3g} {verdict} "
                f"(min {limit:.3g})")

    bits = [profile.name,
            part("min_track_mm", float(geometry.track_width_mm)),
            part("min_via_diameter_mm", float(geometry.via_size_mm)),
            part("min_clearance_mm", float(geometry.clearance_mm))]
    if via_drill_mm is not None:
        bits.append(part("min_via_drill_mm", float(via_drill_mm)))
    bits.append(f"verified {profile.verified_on}")
    return " | ".join(bits)


def violation_warnings(violations, profile):
    """Loud multi-line complaints for a run's WARNING block. The tool stating
    its own limits is the point; a violation here means the board house will
    reject, re-quote, or silently 'engineer' the design."""
    if not violations:
        return []
    out = [f"FAB VIOLATION ({profile.house}, {profile.tier} tier): "
           f"{len(violations)} of this board's copper dimensions are below "
           f"what {profile.name} builds. The numbers were NOT changed — pass "
           f"--fab-enforce to snap them to the cheapest legal values."]
    out += [f"  {v.message}" for v in violations]
    return out


# ── reconciliation: the thing the CLIs call ─────────────────────────────
def _project_specified(board_path):
    """Which copper numbers the board's sibling .kicad_pro actually states.

    Returns a subset of {"clearance", "track", "via"}. A fab profile fills
    only what the PROJECT leaves unsaid: a net class the user wrote is a
    design decision and outranks a manufacturing floor, even a violated one
    (we warn about it instead). Malformed or absent project files specify
    nothing, which is the correct conservative answer.
    """
    got = set()
    pro = os.path.splitext(board_path)[0] + ".kicad_pro"
    try:
        with open(pro, encoding="utf-8") as f:
            classes = (json.load(f).get("net_settings") or {}).get("classes") or []
    except (OSError, ValueError):
        return got
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        for key, tag in (("clearance", "clearance"), ("track_width", "track"),
                         ("via_diameter", "via")):
            v = cls.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                got.add(tag)
    return got


@dataclass
class FabOutcome:
    """Everything a CLI needs to print about manufacturing, in one object."""
    profile: FabProfile
    violations: list = field(default_factory=list)
    changes: list = field(default_factory=list)   # human strings
    notes: list = field(default_factory=list)     # non-fatal explanations
    track_mm: float = None       # resolved overrides, None = leave alone
    clearance_mm: float = None
    via_size_mm: float = None
    via_drill_mm: float = None

    @property
    def ok(self):
        return not self.violations


def fill_defaults(board_path, profile, pitch_mm, clearance_mm=None,
                  track_width_mm=None, via_size_mm=None):
    """Fab-derived values for whatever the project and the caller leave unset.

    Precedence, highest first: an explicit argument, the project's net
    classes, this profile. Returns (clearance, track, via, notes) with the
    same None-means-unset convention the callers use, so a `none` profile or
    a fully-specified project is a no-op.

    A FabPitchError here is NOT raised: the caller asked to route, not to
    order boards. It comes back as a note and the defaults stay unset.
    """
    notes = []
    if not profile.constrains:
        return clearance_mm, track_width_mm, via_size_mm, notes
    try:
        rec = recommend(profile, pitch_mm)
    except FabPitchError as e:
        notes.append(f"fab defaults not applied — {e}")
        return clearance_mm, track_width_mm, via_size_mm, notes

    spec = _project_specified(board_path)
    filled = []
    if clearance_mm is None and "clearance" not in spec:
        clearance_mm = rec.clearance_mm
        filled.append(f"clearance {rec.clearance_mm:.3g}")
    if track_width_mm is None and "track" not in spec:
        track_width_mm = rec.track_mm
        filled.append(f"track {rec.track_mm:.3g}")
    if via_size_mm is None and "via" not in spec:
        via_size_mm = rec.via_size_mm
        filled.append(f"via {rec.via_size_mm:.3g}")
    if filled:
        notes.append(f"fab defaults from {profile.name}: {', '.join(filled)} "
                     f"(the project's net classes did not specify these)")
    return clearance_mm, track_width_mm, via_size_mm, notes


def reconcile(geometry, profile, pitch_mm, via_drill_mm=None, enforce=False):
    """Check a resolved geometry against a profile; optionally snap it.

    Returns a FabOutcome. With enforce=False (the default and the rule) the
    geometry is untouched and every shortfall is a violation to shout about.
    With enforce=True the cheapest legal values replace the offending ones and
    each substitution is recorded in `changes` — the tool may override the
    user's numbers only when asked, and only out loud.
    """
    out = FabOutcome(profile=profile)
    if not profile.constrains:
        return out

    out.violations = check(geometry, profile, via_drill_mm)
    if not enforce:
        return out

    try:
        rec = recommend(profile, pitch_mm)
    except FabPitchError as e:
        out.notes.append(
            f"--fab-enforce could not snap this board: {e} "
            f"Geometry left exactly as resolved; the violations below stand.")
        return out

    for field_name, attr, current, target in (
            ("min_track_mm", "track_mm", float(geometry.track_width_mm),
             rec.track_mm),
            ("min_clearance_mm", "clearance_mm", float(geometry.clearance_mm),
             rec.clearance_mm),
            ("min_via_diameter_mm", "via_size_mm", float(geometry.via_size_mm),
             rec.via_size_mm)):
        if current < target - 1e-9:
            setattr(out, attr, target)
            out.changes.append(
                f"{_LABELS[field_name]} {current:.4g} -> {target:.4g} mm "
                f"({profile.name} minimum)")
    if via_drill_mm is not None and rec.via_drill_mm and \
            float(via_drill_mm) < rec.via_drill_mm - 1e-9:
        out.via_drill_mm = rec.via_drill_mm
        out.changes.append(
            f"via drill {float(via_drill_mm):.4g} -> {rec.via_drill_mm:.4g} mm "
            f"({profile.name} minimum)")

    # Pitch rescue. Everything above raises copper that is too SMALL to build.
    # The commoner failure on a hobby board is the opposite: copper that is
    # perfectly buildable and too BIG for its own routing grid — KiCad's stock
    # 0.6 mm via on a 0.5 mm pitch is exactly this, and no fab profile flags
    # it because every house will happily etch it.
    #
    # Only the VIA is shrunk, and only when shrinking it actually rescues the
    # pitch. Track width is never narrowed: a track's width is current
    # capacity, which is circuit intent the fab house knows nothing about, and
    # silently thinning a tube amp's B+ rail to the cheapest legal copper
    # would be the router overruling the designer. Clearance is never
    # narrowed for the same reason — it may be creepage, not manufacturing.
    track_now = out.track_mm if out.track_mm is not None \
        else float(geometry.track_width_mm)
    clear_now = out.clearance_mm if out.clearance_mm is not None \
        else float(geometry.clearance_mm)
    via_now = out.via_size_mm if out.via_size_mm is not None \
        else float(geometry.via_size_mm)
    need_now = required_pitch_mm(track_now, clear_now, via_now)
    if need_now > pitch_mm + 1e-9 and not (
            rec.via_size_mm < via_now - 1e-9 and
            required_pitch_mm(track_now, clear_now, rec.via_size_mm)
            <= pitch_mm + 1e-9):
        # Unfittable and the cheapest legal via does not rescue it. Say what
        # WOULD, rather than leaving the user to solve the inequality.
        rescued = required_pitch_mm(track_now, clear_now, rec.via_size_mm)
        out.notes.append(
            f"this board's copper (track {track_now:.3g}, clearance "
            f"{clear_now:.3g}, via {via_now:.3g}) needs a {need_now:.3g}mm "
            f"pitch and this run uses {pitch_mm:.3g}mm. --fab-enforce did not "
            f"rescue it: dropping to {profile.name}'s cheapest legal via "
            f"({rec.via_size_mm:.3g}mm) would still need {rescued:.3g}mm. "
            f"Options: route at {need_now:.3g}mm pitch, or adopt the full "
            f"{profile.name} floor ({rec.summary()}), which needs "
            f"{required_pitch_mm(rec.track_mm, rec.clearance_mm, rec.via_size_mm):.3g}mm. "
            f"Track width and clearance are NOT narrowed automatically — they "
            f"encode current capacity and creepage, which no fab profile knows.")
    if need_now > pitch_mm + 1e-9 \
            and rec.via_size_mm < via_now - 1e-9 \
            and required_pitch_mm(track_now, clear_now, rec.via_size_mm) \
            <= pitch_mm + 1e-9:
        out.via_size_mm = rec.via_size_mm
        out.changes.append(
            f"via diameter {via_now:.4g} -> {rec.via_size_mm:.4g} mm "
            f"({profile.name} cheapest legal via; the {via_now:.4g} mm via is "
            f"buildable but its halo needs a "
            f"{required_pitch_mm(track_now, clear_now, via_now):.3g}mm pitch "
            f"and this run uses {pitch_mm:.3g}mm)")
        if via_drill_mm is not None and out.via_drill_mm is None and \
                float(via_drill_mm) > rec.via_size_mm - \
                2.0 * (profile.min_annular_ring_mm or 0.0) + 1e-9:
            # The old drill no longer leaves a legal ring in the smaller pad.
            out.via_drill_mm = rec.via_drill_mm
            out.changes.append(
                f"via drill {float(via_drill_mm):.4g} -> "
                f"{rec.via_drill_mm:.4g} mm (to keep a legal annular ring in "
                f"the smaller pad)")

    if out.changes:
        # Snapping copper up can break the pitch that the un-snapped copper
        # fitted. Say so rather than emitting geometry that trades a fab
        # violation for a DRC one.
        need = required_pitch_mm(out.track_mm or float(geometry.track_width_mm),
                                 out.clearance_mm or float(geometry.clearance_mm),
                                 out.via_size_mm or float(geometry.via_size_mm))
        if pitch_mm < need - 1e-9:
            out.notes.append(
                f"enforced geometry needs a {need:.3g}mm pitch but this run "
                f"uses {pitch_mm:.3g}mm — the copper is now buildable and no "
                f"longer fits its own grid. Re-run at --pitch {need:.3g}.")
    return out


# ── comparison ──────────────────────────────────────────────────────────
def compare(names=("jlcpcb-standard", "pcbway-standard"), pitch_mm=None):
    """Side-by-side table of several profiles, as a list of text lines.

    With pitch_mm given, appends what each house's cheapest legal geometry
    does at that pitch — which is the question a user actually has.
    """
    profs = [load_profile(n) for n in names]
    w = max(len(_LABELS[f]) for f in _LIMIT_FIELDS) + 2
    col = max(max(len(p.name) for p in profs), 14) + 2
    lines = ["".ljust(w) + "".join(p.name.ljust(col) for p in profs),
             "".ljust(w) + "".join(f"{p.house}/{p.tier}".ljust(col)
                                   for p in profs),
             "-" * (w + col * len(profs))]
    for f in _LIMIT_FIELDS:
        cells = []
        for p in profs:
            v = getattr(p, f)
            cells.append(("-" if v is None else f"{v:.4g} mm").ljust(col))
        lines.append(_LABELS[f].ljust(w) + "".join(cells))
    for label, attr in (("blind/buried", "blind_buried"),
                        ("via in pad", "via_in_pad"),
                        ("verified", "verified_on")):
        lines.append(label.ljust(w)
                     + "".join(str(getattr(p, attr))[:col - 2].ljust(col)
                               for p in profs))
    if pitch_mm is not None:
        lines.append("")
        lines.append(f"at {pitch_mm:.3g} mm routing pitch:")
        for p in profs:
            try:
                rec = recommend(p, pitch_mm)
            except FabPitchError as e:
                lines.append(f"  {p.name}: NO FIT — needs "
                             f"{e.required_pitch_mm:.3g} mm pitch")
                continue
            if rec is None:
                lines.append(f"  {p.name}: unconstrained")
                continue
            lines.append(f"  {p.name}: {rec.summary()} "
                         f"(needs {required_pitch_mm(rec.track_mm, rec.clearance_mm, rec.via_size_mm):.3g} mm)")
    return lines


def main(argv=None):
    """`python fab.py` — inspect and compare profiles without routing."""
    import argparse
    ap = argparse.ArgumentParser(
        description="Inspect Orchard Route fab profiles")
    ap.add_argument("profiles", nargs="*",
                    help="profile names to show (default: list them all)")
    ap.add_argument("--compare", action="store_true",
                    help="side-by-side table instead of per-profile detail")
    ap.add_argument("--pitch", type=float, default=None,
                    help="also report each profile's cheapest legal geometry "
                         "at this routing pitch")
    ap.add_argument("--profiles-file", default=None,
                    help="JSON file of additional profiles to merge in")
    args = ap.parse_args(argv)

    if args.profiles_file:
        added = load_profiles_file(args.profiles_file)
        print(f"loaded {len(added)} profile(s) from {args.profiles_file}: "
              f"{', '.join(added)}\n")

    names = args.profiles or [n for n in list_profiles() if n != "none"]
    if args.compare:
        print("\n".join(compare(names, pitch_mm=args.pitch)))
        return 0
    for n in names:
        p = load_profile(n)
        print(p.describe())
        warn = p.stale_warning()
        if warn:
            print(f"  WARNING: {warn}")
        if args.pitch is not None:
            try:
                rec = recommend(p, args.pitch)
                print(f"  at pitch {args.pitch:.3g}: "
                      f"{rec.summary() if rec else 'unconstrained'}")
            except FabPitchError as e:
                print(f"  at pitch {args.pitch:.3g}: NO FIT — {e}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
