"""L5 placement search: the router-independent half of optimize_region v1.

Simulated annealing over (x, y, rot) per movable footprint, per
REGION_SOLVER.md section 3: SA GENERATES, THE ROUTER JUDGES. Nothing here
routes — anneal_region returns an elite pool of distinct candidate
placements ranked by a cheap explorer energy, and the router (region.py,
later) gets the final word on every one of them. Because the router judges
afterwards, the pool optimizes for DIVERSITY over depth: five genuinely
different arrangements beat five decimals of one local optimum.

How diversity is kept (the contract the pool makes with region.py):

- the elite pool is NICHED on placement distance — a new accepted state
  first competes only against elites within one grid step of max per-ref
  displacement (replacing its niche-mate if better) and only then against
  the global worst, so one deep basin cannot fill the pool with near-clones;
- distinctness = max per-ref displacement > one grid step, enforced pairwise
  over the final pool (best kept first);
- reheat-on-stall re-melts the walk when the best energy plateaus, and swap
  moves exchange two parts outright — both escape mechanisms feed the pool
  new basins rather than polishing the current one.

State model and hard rules (spec section 3):

- positions snap to a placement grid (grid_mm, default 0.5) anchored at the
  region origin; rotations come from orientation_set when given, else
  {0, 90, 180, 270} plus the part's own starting angle;
- courtyard = the REAL F.CrtYd/B.CrtYd rect when the footprint carries one
  (board.footprint_courtyards, unioned with the pad bbox as a floor), else a
  pad-bbox proxy; + 0.25 mm margin either way. The proxy fallback is fine for
  SMD/ICs (pad bbox ~ body) but was catastrophic for THT bodies overhanging
  their pads until real courtyards landed; the margin is surfaced in
  AnnealResult.courtyard_margin_mm for diagnostics;
- hard rejection, never penalty: courtyard overlap movable-vs-movable and
  movable-vs-frozen (obstacles), courtyard leaving the region fence, and
  ANY violated constraint from constraints.py;
- cheap energy = net-class-weighted HPWL over the region's nets + soft
  constraint penalties + boundary-terminal pull. Boundary terminals arrive
  as fixed_points ({net_name: [(x, y), ...]}) — the caller computes the
  projected pseudo-pads (design rule 3); here they are simply immovable
  HPWL endpoints, which IS the pull. Net-class weights come from the
  project file via writeback.load_net_class_names (the emitters' own
  resolution machinery — imported, not duplicated).

Deterministic: one random.Random(seed) drives everything; identical inputs
and seed give an identical elite pool, bit for bit.

Pure CPU, stdlib + board/constraints/writeback only. No mlx, no lattice.
"""
import math
import random
from dataclasses import dataclass

from board import load_board
from constraints import evaluate_constraints, parse_constraints
from writeback import (board_footprints, load_net_class_names,
                       project_file_for, resolve_footprint)

COURTYARD_MARGIN_MM = 0.25   # spec v1: pad bbox union + this margin

# HPWL multipliers per net class for the cheap energy. Audio wants SHORT
# above all (design rule 4's local pathology), HV close behind for loop
# area; Power is wide but length-tolerant. Override per call.
DEFAULT_CLASS_WEIGHTS = {"Audio": 2.0, "HV": 1.5, "Power": 0.75}


@dataclass(frozen=True)
class Part:
    """One footprint the model knows: its CURRENT placement and its pads at
    coordinates consistent with that placement (exactly what board.load_board
    yields — pad positions absolute, pad rotations absolute)."""
    ref: str
    x_mm: float
    y_mm: float
    rot_deg: float
    pads: tuple            # of board.Pad
    locked: bool = False   # the footprint is LOCKED in KiCad (writeback.
                           # FootprintRecord.locked, propagated here so the
                           # region solver can auto-fix locked parts)
    local_courtyard: tuple = None  # board.Board.footprint_courtyards entry: the
                           # real F.CrtYd/B.CrtYd bbox in the footprint-LOCAL
                           # frame, or None. When present _local_geometry uses
                           # it (unioned with the pads) instead of the pad-bbox
                           # proxy — the true body keep-out for overhanging THT.
    sheet: str = None      # board.Board.footprint_sheets entry: the schematic
                           # sheet path (KiCad (sheetname ...)) or None — a
                           # ready-made human grouping (floorplan.py reads it).


@dataclass(frozen=True)
class Elite:
    """One candidate placement from the pool. placements covers EVERY part
    given to the model, pinned ones included, so region.py can hand it
    straight to writeback.write_moved_copy."""
    placements: dict       # ref -> (x_mm, y_mm, rot_deg)
    energy: float          # hpwl + penalty_scale * penalty (ranking key)
    hpwl_mm: float
    penalty: float         # 0.0 by construction (violations are rejected)


@dataclass
class AnnealResult:
    elites: list           # best-first, pairwise distinct
    seed: int
    grid_mm: float
    courtyard_margin_mm: float   # v1 courtyard-proxy limitation, for diagnostics
    sweeps: int
    proposals: int
    accepted: int
    rejected: int          # infeasible proposals (overlap / fence / constraint)
    reheats: int
    initial_energy: float
    repaired: bool         # initial state was infeasible and got repaired


# ── geometry ─────────────────────────────────────────────────────────────────

_COS_SIN = {0.0: (1.0, 0.0), 90.0: (0.0, 1.0),
            180.0: (-1.0, 0.0), 270.0: (0.0, -1.0)}


def _rot(x, y, deg):
    """board.py's rotation convention (KiCad CCW, Y-down), exact at the four
    cardinal angles so grid-snapped placements stay exactly on grid."""
    cs = _COS_SIN.get(deg % 360.0)
    if cs is None:
        t = math.radians(deg)
        cs = (math.cos(t), math.sin(t))
    c, s = cs
    return x * c + y * s, -x * s + y * c


def _world_rect(rect, x, y, deg):
    """Axis-aligned world hull of a local rect placed at (x, y, deg). Exact
    for cardinal angles; a conservative superset otherwise."""
    x0, y0, x1, y1 = rect
    pts = [_rot(px, py, deg)
           for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (x + min(xs), y + min(ys), x + max(xs), y + max(ys))


def _overlap(a, b):
    """Strict interior intersection — rects sharing only an edge don't
    overlap (the courtyard margin already provides the spacing)."""
    return (a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and
            a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9)


def _gap(a, b):
    """Minimum distance between two axis-aligned rects — 0 if they touch or
    overlap. This is the clearance a DRC check measures between two courtyards."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _too_close(a, b, min_gap):
    """Two courtyards that overlap OR sit closer than min_gap. With min_gap <= 0
    this is exactly _overlap (edge-touching allowed); with min_gap > 0 a real
    clearance gap is required — two courtyards that merely abut have ZERO
    clearance between the parts, which KiCad DRC flags (finding B)."""
    if min_gap <= 1e-9:                 # guard agrees with the tolerance below,
        return _overlap(a, b)           # so a sub-nm gap can never mask overlap
    return _gap(a, b) < min_gap - 1e-9


def _rect_circle_overlap(rect, cx, cy, r):
    """True when an axis-aligned rect and a circle (centre cx,cy, radius r)
    intersect — the closest point on the rect to the centre lies within r.
    Mounting-hole keep-outs are circular (a hole + its screw-head clearance),
    not rectangular, so a courtyard clears a corner of the hole it would fail a
    bbox test on."""
    nx = min(max(cx, rect[0]), rect[2])
    ny = min(max(cy, rect[1]), rect[3])
    return (nx - cx) ** 2 + (ny - cy) ** 2 < r * r - 1e-12


# ── pad-copper geometry (DRC-fidelity pad clearance, feedback/placement-
#    fidelity-2026-07-20 §2) ─────────────────────────────────────────────────
#
# Courtyard non-overlap is an ASSEMBLY halo; it says nothing about where the
# copper of one pad sits relative to another net's pad. Two parts with clear
# courtyards can still have individual pads within the net-class clearance (a
# FET's fat drain pad, a relay's spread pads, an HV creepage rule), and KiCad
# DRC calls that a short. So the placement model tests pad-to-pad copper
# clearance between DIFFERENT nets on SHARED copper layers, exactly, using the
# pad's true rotated rectangle — not an inflated bbox, which would over-reject
# legal corner adjacencies (a false infeasible is as wrong as a missed short).

def _pad_local_corners(part, pad):
    """A pad's four corners in the part's LOCAL, UNROTATED frame (origin at the
    part centre), so applying the part's placement rotation with _rot puts them
    in world coordinates — the pad-copper analogue of _local_geometry's rect.
    The pad's own rotation is stored ABSOLUTE (footprint angle folded in), so it
    is backed out to a footprint-relative angle here and re-composed by _rot at
    judge time; at the home placement this round-trips to the pad's file rect."""
    ox, oy = _rot(pad.x_mm - part.x_mm, pad.y_mm - part.y_mm, -part.rot_deg)
    rel = pad.rotation_deg - part.rot_deg
    hw, hh = pad.width_mm / 2.0, pad.height_mm / 2.0
    out = []
    for sx, sy in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)):
        dx, dy = _rot(sx * hw, sy * hh, rel)
        out.append((ox + dx, oy + dy))
    return out


def pad_world_corners(pad):
    """A pad's four WORLD corners at its current absolute placement (board.Pad
    coords are already absolute) — for frozen obstacle pads that never move, so
    region.py can hand a locked/out-of-fence part's copper to the model."""
    hw, hh = pad.width_mm / 2.0, pad.height_mm / 2.0
    out = []
    for sx, sy in ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)):
        dx, dy = _rot(sx * hw, sy * hh, pad.rotation_deg)
        out.append((pad.x_mm + dx, pad.y_mm + dy))
    return out


def _aabb_of(corners):
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys), max(xs), max(ys))


def _pad_has_copper(pad):
    """Whether a pad contributes copper a clearance check must see. A non-plated
    through-hole (np_thru_hole — a bare tooling/mounting hole) has none, and any
    through-hole whose pad size does not exceed its drill has no annular ring:
    KiCad's clearance DRC ignores both. board.py gives these pads all copper
    layers so the router still treats the drill as an obstacle, but modelling
    them as SOLID copper for pad clearance would flag a mounting hole as
    'shorting' a neighbour and refuse a buildable board (adversarial review,
    2026-07-20). SMD and real annular-ring THT pads always have copper."""
    if not getattr(pad, "plated", True):
        return False
    if pad.through_hole and pad.drill_mm > 0.0 \
            and max(pad.width_mm, pad.height_mm) <= pad.drill_mm + 1e-9:
        return False
    return True


def _convex_overlap(a, b):
    """Separating-axis test for two convex polygons (corner lists). True when
    no axis separates them — i.e. their interiors intersect. Edge normals need
    not be normalised for a pure disjoint test."""
    for poly in (a, b):
        n = len(poly)
        for k in range(n):
            x1, y1 = poly[k]
            x2, y2 = poly[(k + 1) % n]
            nx, ny = -(y2 - y1), (x2 - x1)
            amin = amax = None
            for px, py in a:
                dpv = px * nx + py * ny
                amin = dpv if amin is None else min(amin, dpv)
                amax = dpv if amax is None else max(amax, dpv)
            bmin = bmax = None
            for px, py in b:
                dpv = px * nx + py * ny
                bmin = dpv if bmin is None else min(bmin, dpv)
                bmax = dpv if bmax is None else max(bmax, dpv)
            if amax < bmin - 1e-12 or bmax < amin - 1e-12:
                return False
    return True


def _pt_seg_dist2(px, py, ax, ay, bx, by):
    """Squared distance from a point to a segment."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 1e-18:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def _poly_gap(a, b):
    """Exact Euclidean gap between two convex polygons: 0 if they intersect,
    else the minimum vertex-to-opposite-edge distance (which, for convex
    polygons, IS the closest approach). Used for pad-copper clearance, so it
    must be exact in both directions — a conservative bbox proxy would refuse
    buildable placements, an optimistic one would ship shorts."""
    if _convex_overlap(a, b):
        return 0.0
    best = math.inf
    for p, q in ((a, b), (b, a)):
        m = len(q)
        for px, py in p:
            for k in range(m):
                qx, qy = q[k]
                rx, ry = q[(k + 1) % m]
                best = min(best, _pt_seg_dist2(px, py, qx, qy, rx, ry))
    return math.sqrt(best)


def _local_geometry(part, margin_mm, body_margin_mm=0.0):
    """(pad terminals in footprint-local frame, local courtyard rect).

    Terminals: (ox, oy, net_name) with the part's own rotation backed out,
    so any candidate (x, y, rot) maps them with one _rot. Courtyard: the real
    F.CrtYd when present (unioned with the pad bbox) + margin_mm, else the
    pad-bbox proxy. body_margin_mm is an EXTRA margin added ONLY to the proxy
    (no-courtyard footprints), where the pads under-model the body — a relay or
    connector whose case overhangs its pads (finding C). It never touches a part
    that draws a real courtyard."""
    terms = []
    lo_x = lo_y = math.inf
    hi_x = hi_y = -math.inf
    for p in part.pads:
        ox, oy = _rot(p.x_mm - part.x_mm, p.y_mm - part.y_mm, -part.rot_deg)
        a = math.radians(p.rotation_deg - part.rot_deg)
        ca, sa = abs(math.cos(a)), abs(math.sin(a))
        hw = (p.width_mm * ca + p.height_mm * sa) / 2.0
        hh = (p.width_mm * sa + p.height_mm * ca) / 2.0
        lo_x = min(lo_x, ox - hw)
        hi_x = max(hi_x, ox + hw)
        lo_y = min(lo_y, oy - hh)
        hi_y = max(hi_y, oy + hh)
        terms.append((ox, oy, p.net_name))
    if not terms:
        lo_x = lo_y = hi_x = hi_y = 0.0
    # Prefer the REAL courtyard when the footprint carries one, unioned with the
    # pad bbox so a part can never model smaller than its own pads. Both rects
    # are in the same footprint-local frame, so the union is direct and _rot in
    # _world_rect places it. The pad-bbox proxy remains the fallback for parts
    # with no courtyard layer (SMD/ICs, where pad bbox ~ body). This is the fix
    # for THT bodies overhanging their pads (feedback/courtyard-model-*): the
    # proxy alone let radials/box-caps nest bodily inside one another.
    lc = part.local_courtyard
    if lc is not None:
        lo_x = min(lo_x, lc[0]); lo_y = min(lo_y, lc[1])
        hi_x = max(hi_x, lc[2]); hi_y = max(hi_y, lc[3])
        m = margin_mm                  # a real courtyard already bounds the body
    else:
        m = margin_mm + body_margin_mm  # proxy: pad the guessed body extent
    return terms, (lo_x - m, lo_y - m, hi_x + m, hi_y + m)


def pad_clearance_report(pads, clr_by_name, default_mm):
    """Different-net pad-copper clearance breaches over a WHOLE board — the
    DRC-mirroring post-condition of feedback/placement-fidelity-2026-07-20 §5.

    pads: iterable of (owner_ref, net_name, layers, world_corners). Returns
    [(refA, netA, refB, netB, gap_mm, need_mm)] for every pair of pads that
    belong to DIFFERENT footprints and DIFFERENT nets, share a copper layer,
    and sit closer than max(their two net-class clearances). Same-net and
    same-footprint pairs are legal and skipped. Broad-phased with a uniform
    grid sized to the widest clearance, so it scales to a full board; the
    survivors are measured with the exact _poly_gap. This is the check KiCad
    runs that courtyard non-overlap never implied (a fat FET pad, an HV creepage
    rule, a back-side THT pad under a front SMD pad)."""
    from collections import defaultdict
    items, max_clr = [], float(default_mm)
    for ref, net, layers, corners in pads:
        ly = frozenset(layers)
        if not ly:
            continue
        cor = list(corners)
        c = clr_by_name.get(net or "", default_mm)
        max_clr = max(max_clr, c)
        items.append((ref, net or "", ly, cor, _aabb_of(cor), c))
    if max_clr <= 0.0 or not items:
        return []
    cell = max(max_clr, 1.0)
    # bucket each pad by its AABB INFLATED by half the widest clearance: two pads
    # whose true gap is < clearance then have overlapping inflated boxes and are
    # guaranteed to share a grid cell (without the inflation, a within-clearance
    # pair one cell apart would never be compared — a silently missed short).
    h = max_clr / 2.0
    grid = defaultdict(list)
    for idx, (_r, _n, _l, _c, ab, _cl) in enumerate(items):
        for gx in range(int(math.floor((ab[0] - h) / cell)),
                        int(math.floor((ab[2] + h) / cell)) + 1):
            for gy in range(int(math.floor((ab[1] - h) / cell)),
                            int(math.floor((ab[3] + h) / cell)) + 1):
                grid[(gx, gy)].append(idx)
    seen, out = set(), []
    for bucket in grid.values():
        for u in range(len(bucket)):
            for v in range(u + 1, len(bucket)):
                i, j = bucket[u], bucket[v]
                key = (i, j) if i < j else (j, i)
                if key in seen:
                    continue
                seen.add(key)
                ri, ni, lyi, cori, abi, ci = items[i]
                rj, nj, lyj, corj, abj, cj = items[j]
                if ri == rj:                       # same footprint
                    continue
                if ni == nj and ni != "":          # same net may touch
                    continue
                if not (lyi & lyj):                # no shared copper layer
                    continue
                need = max(ci, cj)
                if need <= 0.0 or _gap(abi, abj) >= need:
                    continue
                g = _poly_gap(cori, corj)
                if g < need - 1e-9:
                    out.append((ri, ni, rj, nj, g, need))
    out.sort(key=lambda t: (t[4], t[0], t[2]))
    return out


def part_courtyard(part, margin_mm=COURTYARD_MARGIN_MM, body_margin_mm=0.0):
    """World courtyard rect of a part AT ITS CURRENT PLACEMENT — the helper
    region.py uses to turn out-of-region footprints into frozen obstacle
    rects for anneal_region(obstacles=...). body_margin_mm pads no-courtyard
    footprints (see _local_geometry); 0 leaves the raw pad-bbox proxy."""
    _, local = _local_geometry(part, margin_mm, body_margin_mm)
    return _world_rect(local, part.x_mm, part.y_mm, part.rot_deg)


# ── the model ────────────────────────────────────────────────────────────────

class PlacementModel:
    """Everything the annealer (and later region.py) asks about a candidate
    placement: courtyards, hard feasibility, cheap energy. Placements are
    keyed by part ref; internally states are a list of (x, y, rot) aligned
    with the parts list."""

    def __init__(self, parts, region, constraints=(), *, obstacles=(),
                 keepouts=(), fixed_points=None, net_weights=None,
                 margin_mm=COURTYARD_MARGIN_MM, edge_tol_mm=1.0,
                 penalty_scale_mm=10.0, min_gap_mm=0.0, body_margin_mm=0.0,
                 pad_clearances=None, default_pad_clearance_mm=0.0,
                 frozen_pads=()):
        self.parts = list(parts)
        self.refs = [p.ref for p in self.parts]
        if len(set(self.refs)) != len(self.refs):
            dup = sorted({r for r in self.refs if self.refs.count(r) > 1})
            raise ValueError(f"duplicate part refs {dup} — address duplicates "
                             f"with their ref#N form (see writeback."
                             f"board_footprints)")
        rx, ry, rw, rh = (float(v) for v in region)
        if rw <= 0 or rh <= 0:
            raise ValueError(f"region w and h must be > 0, got {region!r}")
        self.region = (rx, ry, rw, rh)
        self.margin_mm = float(margin_mm)
        self.edge_tol_mm = float(edge_tol_mm)
        self.penalty_scale_mm = float(penalty_scale_mm)
        # required clearance GAP between courtyards (and between a courtyard and
        # a frozen obstacle) — not just non-overlap; touching = 0 clearance = a
        # DRC crash (finding B). 0 keeps the old "no overlap" behaviour.
        self.min_gap_mm = float(min_gap_mm)
        self.obstacles = [tuple(float(v) for v in r) for r in obstacles]
        # circular mounting-hole keep-outs (cx, cy, radius) — hole + screw-head
        # clearance, already inflated by the caller. Movable courtyards are
        # hard-rejected against these, exactly like the frozen obstacles.
        self.keepouts = [(float(cx), float(cy), float(r))
                         for cx, cy, r in keepouts]
        self.constraints = parse_constraints(constraints,
                                             known_refs=set(self.refs))
        self.home = {p.ref: (p.x_mm, p.y_mm, p.rot_deg) for p in self.parts}

        self.body_margin_mm = float(body_margin_mm)
        self._geom = [_local_geometry(p, self.margin_mm, self.body_margin_mm)
                      for p in self.parts]

        # pad-copper clearance (feedback/placement-fidelity-2026-07-20 §2).
        # pad_clearances is {net_name: min copper clearance mm}; None disables
        # the whole check (the default — old callers and the synthetic anneal
        # tests are unaffected). Each part's pads are stored as LOCAL corner
        # quads so a candidate (x, y, rot) maps them with one _rot, exactly like
        # the courtyard rect. frozen_pads are (net, layers, world_corners) for
        # obstacle parts that never move (locked / out-of-fence copper).
        self._pad_check = pad_clearances is not None
        self._pad_clr = dict(pad_clearances or {})
        self._pad_clr_default = float(default_pad_clearance_mm)
        self._padgeom = []
        for p in self.parts:
            pg = []
            for pad in p.pads:
                layers = frozenset(pad.layers or ())
                if not layers or not _pad_has_copper(pad):
                    continue                   # no copper (paste/mask only, or a
                                               # bare np_thru_hole / no-annulus
                                               # hole) -> nothing to clear
                pg.append((pad.net_name or "", layers,
                           tuple(_pad_local_corners(p, pad))))
            self._padgeom.append(pg)
        self._frozen_pads = []
        for net, layers, corners in (frozen_pads or ()):
            ly = frozenset(layers)
            if not ly:
                continue
            cs = tuple(corners)
            self._frozen_pads.append((net or "", ly, cs, _aabb_of(cs)))
        # broad-phase gate: two parts can only violate if their courtyards (which
        # ALWAYS contain their pads — _local_geometry floors on the pad bbox) are
        # closer than the largest clearance any involved net asks for.
        if self._pad_check:
            names = {nn for pg in self._padgeom for nn, _l, _c in pg}
            names |= {nn for nn, _l, _c, _a in self._frozen_pads}
            vals = [self._pad_clr.get(nn, self._pad_clr_default) for nn in names]
            self._max_pad_clr = max(vals) if vals else 0.0
        else:
            self._max_pad_clr = 0.0

        # nets: name -> (weight, [fixed (x, y)], [(part_idx, ox, oy)]);
        # only nets with >= 2 endpoints pull. Sorted for stable float sums.
        weights = dict(net_weights or {})
        gather = {}
        for i, (terms, _) in enumerate(self._geom):
            for ox, oy, net in terms:
                if net:
                    gather.setdefault(net, ([], []))[1].append((i, ox, oy))
        for net, pts in (fixed_points or {}).items():
            for x, y in pts:
                gather.setdefault(net, ([], []))[0].append((float(x), float(y)))
        self._nets = {
            net: (float(weights.get(net, 1.0)), fixed, terms)
            for net, (fixed, terms) in sorted(gather.items())
            if len(fixed) + len(terms) >= 2}

    # -- state helpers --

    def initial_states(self):
        return [(p.x_mm, p.y_mm, p.rot_deg % 360.0) for p in self.parts]

    def placements(self, states):
        return {r: s for r, s in zip(self.refs, states)}

    def courtyards(self, states):
        return {r: _world_rect(self._geom[i][1], s[0], s[1], s[2])
                for i, (r, s) in enumerate(zip(self.refs, states))}

    def _pad_conflicts(self, courts, states):
        """Pad-copper clearance breaches at a state: different-net pads on a
        shared copper layer whose true rotated rectangles sit closer than the
        net-class clearance. One record per offending part-pair (the first pad
        pair that breaches) — enough for judge()'s hard reject and readable in
        problems(). Empty when the check is off or nothing breaches.

        The gate is the courtyard proximity already computed by the caller: a
        courtyard contains its part's pads, so two courtyards farther apart than
        the widest clearance cannot have pads within it — no missed short, and
        the exact _poly_gap on the survivors never over-rejects."""
        if not self._pad_check or self._max_pad_clr <= 0.0:
            return []
        d = self._pad_clr_default
        clr = self._pad_clr
        world = {}

        def wp(i):
            w = world.get(i)
            if w is None:
                x, y, rot = states[i]
                w = []
                for nn, ly, corners in self._padgeom[i]:
                    w.append((nn, ly, [(x + rx, y + ry) for rx, ry in
                                       (_rot(cx, cy, rot) for cx, cy in corners)]))
                world[i] = w
            return w

        def first_breach(pads_a, pads_b):
            for na, la, ca in pads_a:
                for nb, lb, cb in pads_b:
                    if na == nb and na != "":        # same net may touch
                        continue
                    if not (la & lb):                # no shared copper layer
                        continue
                    need = max(clr.get(na, d), clr.get(nb, d))
                    if need <= 0.0:
                        continue
                    g = _poly_gap(ca, cb)
                    if g < need - 1e-9:
                        return na, nb, need, g
            return None

        out = []
        n = len(courts)
        for i in range(n):
            if not self._padgeom[i]:
                continue
            for j in range(i + 1, n):
                if not self._padgeom[j]:
                    continue
                if _gap(courts[i], courts[j]) >= self._max_pad_clr - 1e-12:
                    continue
                hit = first_breach(wp(i), wp(j))
                if hit:
                    na, nb, need, g = hit
                    out.append({"a": self.refs[i], "b": self.refs[j],
                                "net_a": na, "net_b": nb,
                                "clearance_mm": need, "gap_mm": g,
                                "frozen": False})
        for i in range(n):
            if not self._padgeom[i]:
                continue
            ci = courts[i]
            for nb, lb, cb, ab in self._frozen_pads:
                if _gap(ci, ab) >= self._max_pad_clr - 1e-12:
                    continue
                hit = first_breach(wp(i), [(nb, lb, cb)])
                if hit:
                    na, fnb, need, g = hit
                    out.append({"a": self.refs[i], "b": "frozen obstacle",
                                "net_a": na, "net_b": fnb,
                                "clearance_mm": need, "gap_mm": g,
                                "frozen": True})
                    break
        return out

    # -- judgment --

    def problems(self, states):
        """Every hard-rule breach of a state, as human-readable strings.
        Empty list == feasible. This is the slow, explaining sibling of
        feasible(); the repair phase and error messages use it."""
        out = []
        rx, ry, rw, rh = self.region
        courts = [_world_rect(self._geom[i][1], s[0], s[1], s[2])
                  for i, s in enumerate(states)]
        for i, (x0, y0, x1, y1) in enumerate(courts):
            if x0 < rx - 1e-9 or y0 < ry - 1e-9 \
                    or x1 > rx + rw + 1e-9 or y1 > ry + rh + 1e-9:
                out.append(f"{self.refs[i]} courtyard leaves the region fence")
        g = self.min_gap_mm
        near = " overlap" if g <= 0 else f" are within {g:g} mm"
        for i in range(len(courts)):
            for j in range(i + 1, len(courts)):
                if _too_close(courts[i], courts[j], g):
                    out.append(f"{self.refs[i]} and {self.refs[j]} "
                               f"courtyards{near}")
            for ob in self.obstacles:
                if _too_close(courts[i], ob, g):
                    out.append(f"{self.refs[i]} courtyard {near.strip()} a "
                               f"frozen obstacle at ({ob[0]:.2f}, {ob[1]:.2f})")
            for cx, cy, hr in self.keepouts:
                if _rect_circle_overlap(courts[i], cx, cy, hr):
                    out.append(f"{self.refs[i]} courtyard sits on a mounting-"
                               f"hole keep-out at ({cx:.2f}, {cy:.2f})")
        for v in self._pad_conflicts(courts, states):
            out.append(
                f"{v['a']} pad (net {v['net_a'] or '<none>'}) and {v['b']} pad "
                f"(net {v['net_b'] or '<none>'}) are {v['gap_mm']:.3f} mm apart "
                f"but need {v['clearance_mm']:g} mm copper clearance")
        for c in evaluate_constraints(self.constraints,
                                      self.placements(states),
                                      dict(zip(self.refs, courts)),
                                      rect=self.region, home=self.home,
                                      edge_tol_mm=self.edge_tol_mm):
            if not c.ok:
                out.append(f"constraint violated: {c.reason}")
        return out

    def judge(self, states):
        """(feasible, energy, hpwl, penalty) — one pass, cheap, no strings
        on the hot path beyond what evaluate_constraints builds."""
        rx, ry, rw, rh = self.region
        courts = []
        for i, s in enumerate(states):
            r = _world_rect(self._geom[i][1], s[0], s[1], s[2])
            if r[0] < rx - 1e-9 or r[1] < ry - 1e-9 \
                    or r[2] > rx + rw + 1e-9 or r[3] > ry + rh + 1e-9:
                return False, math.inf, math.inf, math.inf
            for ob in self.obstacles:
                if _too_close(r, ob, self.min_gap_mm):
                    return False, math.inf, math.inf, math.inf
            for cx, cy, hr in self.keepouts:
                if _rect_circle_overlap(r, cx, cy, hr):
                    return False, math.inf, math.inf, math.inf
            for prev in courts:
                if _too_close(r, prev, self.min_gap_mm):
                    return False, math.inf, math.inf, math.inf
            courts.append(r)
        if self._pad_check and self._pad_conflicts(courts, states):
            return False, math.inf, math.inf, math.inf
        checks = evaluate_constraints(self.constraints,
                                      self.placements(states),
                                      dict(zip(self.refs, courts)),
                                      rect=self.region, home=self.home,
                                      edge_tol_mm=self.edge_tol_mm)
        if any(not c.ok for c in checks):
            return False, math.inf, math.inf, math.inf
        penalty = sum(c.penalty for c in checks)   # 0.0 when all ok
        hpwl = 0.0
        for _, (w, fixed, terms) in self._nets.items():
            lo_x = lo_y = math.inf
            hi_x = hi_y = -math.inf
            for fx, fy in fixed:
                lo_x = min(lo_x, fx); hi_x = max(hi_x, fx)
                lo_y = min(lo_y, fy); hi_y = max(hi_y, fy)
            for i, ox, oy in terms:
                x, y, rot = states[i]
                dx, dy = _rot(ox, oy, rot)
                px, py = x + dx, y + dy
                lo_x = min(lo_x, px); hi_x = max(hi_x, px)
                lo_y = min(lo_y, py); hi_y = max(hi_y, py)
            hpwl += w * ((hi_x - lo_x) + (hi_y - lo_y))
        return True, hpwl + self.penalty_scale_mm * penalty, hpwl, penalty

    def evaluate(self, placements):
        """Public re-judgment of a full {ref: (x, y, rot)} placement —
        region.py scores router-judged candidates through this. Returns
        (feasible, energy, hpwl, penalty, problems)."""
        states = [tuple(float(v) for v in placements[r]) for r in self.refs]
        feasible, energy, hpwl, penalty = self.judge(states)
        return feasible, energy, hpwl, penalty, \
            ([] if feasible else self.problems(states))


# ── the annealer ─────────────────────────────────────────────────────────────

def _distinct(p1, p2, grid_mm):
    """Two placements are distinct when some ref moved by MORE than one
    grid step between them (the spec's pool-diversity metric)."""
    worst = 0.0
    for r, (x1, y1, _) in p1.items():
        x2, y2, _ = p2[r]
        worst = max(worst, math.hypot(x1 - x2, y1 - y2))
    return worst > grid_mm + 1e-9


def anneal_region(parts, region, constraints=(), *, obstacles=(),
                  keepouts=(), fixed_points=None, net_weights=None,
                  grid_mm=0.5, margin_mm=COURTYARD_MARGIN_MM,
                  edge_tol_mm=1.0, penalty_scale_mm=10.0, min_gap_mm=0.0,
                  body_margin_mm=0.0, pad_clearances=None,
                  default_pad_clearance_mm=0.0, frozen_pads=(),
                  seed=0, pool_size=8, sweeps=200, proposals_per_sweep=None,
                  t0=None, cool=0.95, reheat_factor=8.0, stall_sweeps=15):
    """Explore placements of parts inside a region fence; return the elite
    pool for the router to judge (see the module docstring for the state
    model, hard-rejection rules, energy, and how pool diversity is kept).

    parts        : [Part] — refs must be unique (use ref#N for duplicates)
    region       : (x, y, w, h) mm — courtyards must stay inside
    constraints  : constraint specs (strings/dicts/Constraint), the closed
                   constraints.py vocabulary; ALL are enforced hard
    obstacles    : frozen courtyard rects (x0, y0, x1, y1) — furniture from
                   outside the region that intrudes into it
    fixed_points : {net_name: [(x, y), ...]} extra immovable HPWL endpoints —
                   boundary pseudo-pads and frozen in-region pads
    net_weights  : {net_name: weight} — build via net_weights_from_project

    Returns AnnealResult; elites[0] is the cheapest-energy candidate, the
    pool is pairwise distinct, and elites is never empty (the feasible
    initial/repaired state seeds it). Raises RuntimeError when no feasible
    starting state can be built at all — with the reasons, per the
    diagnostics-first contract.
    """
    model = PlacementModel(parts, region, constraints, obstacles=obstacles,
                           keepouts=keepouts, fixed_points=fixed_points,
                           net_weights=net_weights,
                           margin_mm=margin_mm, edge_tol_mm=edge_tol_mm,
                           penalty_scale_mm=penalty_scale_mm,
                           min_gap_mm=min_gap_mm, body_margin_mm=body_margin_mm,
                           pad_clearances=pad_clearances,
                           default_pad_clearance_mm=default_pad_clearance_mm,
                           frozen_pads=frozen_pads)
    rng = random.Random(seed)
    rx, ry, rw, rh = model.region
    g = float(grid_mm)
    if g <= 0:
        raise ValueError(f"grid_mm must be > 0, got {grid_mm!r}")

    pinned = set()
    allowed = {}
    for c in model.constraints:
        if c.kind == "fixed":
            pinned.add(c.ref)
        elif c.kind == "orientation_set":
            prev = allowed.get(c.ref)
            angs = set(c.angles) if prev is None else prev & set(c.angles)
            if not angs:
                raise ValueError(f"orientation_set constraints on {c.ref!r} "
                                 f"intersect to nothing")
            allowed[c.ref] = angs
    movable = [i for i, r in enumerate(model.refs) if r not in pinned]
    rots = []
    for i, p in enumerate(model.parts):
        home_rot = p.rot_deg % 360.0
        if p.ref in allowed:
            rots.append(tuple(sorted(allowed[p.ref])))
        else:
            rots.append(tuple(sorted({0.0, 90.0, 180.0, 270.0, home_rot})))

    def snap(v, origin):
        return origin + round((v - origin) / g) * g

    movable_set = set(movable)
    states = []
    for i, p in enumerate(model.parts):
        if i in movable_set:
            r0 = p.rot_deg % 360.0
            rot = r0 if r0 in rots[i] else min(
                rots[i], key=lambda a: min(abs(a - r0), 360 - abs(a - r0)))
            states.append((snap(p.x_mm, rx), snap(p.y_mm, ry), rot))
        else:
            states.append((p.x_mm, p.y_mm, p.rot_deg % 360.0))

    # -- repair: walk the initial state to feasibility if it isn't there --
    repaired = False
    problems = model.problems(states)
    if problems and movable:
        count = len(problems)
        cells_x = max(1, int(rw / g))
        cells_y = max(1, int(rh / g))
        for _ in range(1500):
            if count == 0:
                break
            i = rng.choice(movable)
            trial = list(states)
            trial[i] = (rx + rng.randrange(cells_x + 1) * g,
                        ry + rng.randrange(cells_y + 1) * g,
                        rng.choice(rots[i]))
            n = len(model.problems(trial))
            if n < count or (n == count and rng.random() < 0.25):
                states, count = trial, n
        problems = model.problems(states)
        repaired = not problems
    if problems:
        raise RuntimeError(
            "no feasible starting placement found in the region — "
            + "; ".join(problems[:6])
            + (f"; +{len(problems) - 6} more" if len(problems) > 6 else ""))

    feasible, energy, hpwl, penalty = model.judge(states)
    assert feasible
    initial_energy = energy

    # -- elite pool, niched (see module docstring) --
    pool = []   # [Elite], kept sorted by energy

    def offer(sts, e, hp, pen):
        cand = Elite(placements={r: s for r, s in zip(model.refs, sts)},
                     energy=e, hpwl_mm=hp, penalty=pen)
        mates = [k for k, el in enumerate(pool)
                 if not _distinct(el.placements, cand.placements, g)]
        if mates:
            best = min(mates, key=lambda k: pool[k].energy)
            if e < pool[best].energy - 1e-12:
                pool[best] = cand
                pool.sort(key=lambda el: el.energy)
            return
        if len(pool) < pool_size * 2:      # over-provision; dedupe at the end
            pool.append(cand)
        elif e < pool[-1].energy - 1e-12:
            pool[-1] = cand
        else:
            return
        pool.sort(key=lambda el: el.energy)

    offer(states, energy, hpwl, penalty)

    n_prop = proposals_per_sweep or max(30, 15 * max(1, len(movable)))
    t = t0 if t0 is not None else 0.25 * max(energy, 1.0)
    t_start = t
    best_energy = energy
    stall = 0
    proposals = accepted = rejected = reheats = 0
    max_cells = max(2, int(min(rw, rh) / g))

    if movable:
        for _ in range(sweeps):
            improved = False
            radius = max(1, int(round(max_cells * 0.5 * min(1.0, t / t_start))))
            for _ in range(n_prop):
                proposals += 1
                u = rng.random()
                trial = list(states)
                if u < 0.15 and len(movable) >= 2:            # swap
                    i, j = rng.sample(movable, 2)
                    xi, yi, ri = trial[i]
                    xj, yj, rj = trial[j]
                    trial[i] = (xj, yj, ri)
                    trial[j] = (xi, yi, rj)
                elif u < 0.30 and any(len(rots[i]) > 1 for i in movable):
                    i = rng.choice([k for k in movable if len(rots[k]) > 1])
                    x, y, r = trial[i]
                    trial[i] = (x, y, rng.choice(
                        [a for a in rots[i] if a != r]))
                else:                                          # translate
                    i = rng.choice(movable)
                    dx = rng.randint(-radius, radius)
                    dy = rng.randint(-radius, radius)
                    if dx == 0 and dy == 0:
                        continue
                    x, y, r = trial[i]
                    trial[i] = (x + dx * g, y + dy * g, r)
                ok, e2, hp2, pen2 = model.judge(trial)
                if not ok:
                    rejected += 1
                    continue
                de = e2 - energy
                if de <= 0 or rng.random() < math.exp(-de / max(t, 1e-12)):
                    states, energy, hpwl, penalty = trial, e2, hp2, pen2
                    accepted += 1
                    offer(states, energy, hpwl, penalty)
                    if energy < best_energy - 1e-12:
                        best_energy = energy
                        improved = True
            t *= cool
            stall = 0 if improved else stall + 1
            if stall >= stall_sweeps:
                t = min(t_start, t * reheat_factor)
                reheats += 1
                stall = 0

    # -- final pairwise-distinct pool, best first --
    elites = []
    for el in pool:
        if all(_distinct(el.placements, kept.placements, g)
               for kept in elites):
            elites.append(el)
        if len(elites) == pool_size:
            break

    return AnnealResult(elites=elites, seed=seed, grid_mm=g,
                        courtyard_margin_mm=model.margin_mm,
                        sweeps=sweeps if movable else 0,
                        proposals=proposals, accepted=accepted,
                        rejected=rejected, reheats=reheats,
                        initial_energy=initial_energy, repaired=repaired)


# ── board plumbing ───────────────────────────────────────────────────────────

def parts_from_board(board_path, refs=None):
    """{key: Part} for the named footprints of a real board.

    Keys follow writeback's addressing: a plain ref when unique on the
    board, ref#N when duplicated (this board family duplicates freely).
    refs=None loads every footprint keyed by its unique form. Pads come from
    board.load_board and are matched to footprints by file order — the two
    parsers walk the identical node order (asserted here), so no transform
    is ever duplicated."""
    brd = load_board(board_path)
    with open(board_path, encoding="utf-8") as f:
        records = board_footprints(f.read())
    if sum(r.n_pads for r in records) != len(brd.pads):
        raise RuntimeError(
            f"{board_path}: footprint scan found "
            f"{sum(r.n_pads for r in records)} pads but board.load_board "
            f"parsed {len(brd.pads)} — the order invariant broke")
    offsets = {}
    off = 0
    for r in records:
        offsets[r.uref] = off
        off += r.n_pads
    # board.py's footprint_courtyards is aligned with board_footprints ORDER
    # (both walk the identical node order — the same invariant the pad-count
    # assert above guards). Key it by uref so lookups survive the ref/ref#N
    # addressing. If the two ever disagree in count, fall back to no courtyard
    # rather than misattribute one part's body to another.
    court, sheet = {}, {}
    if len(brd.footprint_courtyards) == len(records):
        court = {r.uref: brd.footprint_courtyards[i]
                 for i, r in enumerate(records)}
    if len(brd.footprint_sheets) == len(records):
        sheet = {r.uref: brd.footprint_sheets[i]
                 for i, r in enumerate(records)}
    out = {}
    for key in ([r.uref for r in records] if refs is None else refs):
        rec = resolve_footprint(records, key)
        o = offsets[rec.uref]
        out[key] = Part(ref=key, x_mm=rec.x_mm, y_mm=rec.y_mm,
                        rot_deg=rec.rot_deg,
                        pads=tuple(brd.pads[o:o + rec.n_pads]),
                        locked=rec.locked,
                        local_courtyard=court.get(rec.uref),
                        sheet=sheet.get(rec.uref))
    return out


def net_weights_from_project(board_path, net_names, class_weights=None):
    """{net_name: HPWL weight} for the given nets, resolved through the
    board's sibling .kicad_pro net classes (writeback.load_net_class_names —
    the emitters' own machinery, imported not duplicated). class_weights
    maps class NAME -> multiplier and overlays DEFAULT_CLASS_WEIGHTS;
    classes in neither map weigh 1.0. Returns {} (all nets weigh 1.0) when
    the board has no project file."""
    pro = project_file_for(board_path)
    if not pro:
        return {}
    fake = {i: n for i, n in enumerate(sorted(set(net_names)))}
    names = load_net_class_names(pro, fake)
    weights = dict(DEFAULT_CLASS_WEIGHTS)
    weights.update(class_weights or {})
    return {fake[i]: float(weights.get(cls, 1.0))
            for i, cls in names.items()}
