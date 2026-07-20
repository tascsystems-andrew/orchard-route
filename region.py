"""L6: optimize_region v1 — fence a piece of a board, place it, prove it by routing.

The agent-facing call of REGION_SOLVER.md. An AI in a design session partitions
the board by circuit function ("playgrounding"), hands one fence plus the parts
allowed to move plus constraints drawn from the schematic's story, and gets back
RANKED, INSPECTABLE candidates: each one a real board copy it can open, an SVG it
can look at, and numbers it can argue with. Nothing here decides what belongs
together; it decides where things go inside the fence, and the router — not a
score function — gets the last word on whether that arrangement is real.

No black boxes (the standing design rule): every candidate ships its own board
file and picture, every rejection has a reason, and the diagnostics block says
what was tight, what did not route and what was in its way, and which direction
the fence wants to grow. A run is conversational in length, not a coffee break.

Pipeline, one pass:

1. SCAN the source board once (place.parts_from_board): movable parts, their
   pads, every other footprint's courtyard.
2. FENCE. Out-of-region footprints whose courtyard intrudes into the region
   become frozen obstacle rects (place.part_courtyard); the placement search
   hard-rejects any overlap with them.
3. TERMINAL PROPAGATION (spec section 2, design rule 3). A net with pads both
   inside and outside the fence gets ONE pseudo-pad: the outside pad nearest
   the fence, clamped onto the fence boundary, at full weight. It is a fixed
   HPWL endpoint for the placement search AND a real routing terminal on the
   region lattice. Without it a region optimizes as if alone on the board and
   produces globally disconnected beauty. Real pads outside the fence are NOT
   routing terminals even when the lattice margin covers them — the pseudo-pad
   stands for the whole outside world, so the route proves "reaches the fence
   at the right place", which is the only claim a region CAN prove.
4. SEARCH. place.anneal_region returns an elite pool of k*3 distinct feasible
   placements, ranked by cheap class-weighted HPWL. That ranking is a hint.
5. JUDGE. EVERY finalist is really routed on its own region lattice (clearance
   rings on, the same PathFinder the whole board uses) and ranked strictly:
   failures >> constraint_violations >> wirelength + via_weight*vias. A
   placement that does not route never outranks one that does (design rule 5).
6. SHIP the top k, each with a board copy (source -> region tracks stripped ->
   footprints moved -> new copper appended) and an SVG of the fence.

The input board is never touched (AGENTS.md hard rule 1); everything lands
under out_dir.

Known limitations of v1 — disclose these when you report a result, and the
first two are why a candidate is a PROPOSAL, not a finished layout:

- TRACK-TO-TRACK clearance is still the grid pitch. lattice.Clearance models
  copper-to-PAD and copper-to-edge spacing properly, but two nets on
  diagonally adjacent nodes sit 0.35 mm apart at 0.5 mm pitch, and KiCad's DRC
  calls that a clearance violation against a 0.2 mm rule. Whole-board routing
  has the identical gap (AGENTS.md hard rule 3); a region inherits it. Route
  at a finer pitch, or expect to nudge a few segments by hand.
- CUSTOM PAD SHAPES are read as their anchor rect (see custom_pad_refs). Any
  affected footprint in the fence is named in diagnostics.geometry_warnings.
- Courtyards use the real F.CrtYd/B.CrtYd graphics where a footprint draws
  them, else a pad-bbox proxy (which under-models THT bodies overhanging their
  pads). diagnostics.courtyard_source reports the real-vs-proxy split per run,
  and the margin is in diagnostics.anneal.courtyard_margin_mm. Run
  --list-courtyards to see which footprints are proxied.
- Placement is cardinal rotations only, no side swap, no 45s; the region is
  assumed to start unrouted; and candidates are independent — committing one
  region then solving its neighbour is path-dependent (revisit fences).

CLI:
    python region.py BOARD --components V1,R4,C8 --region x,y,w,h
        --constraint "adjacency_max_distance(R4,V1,3)" ... --k 5
        --out out/region-a/ [--pitch 0.5] [--layers F.Cu,B.Cu] [--seed 0]
        [--json] [--keep-work]
"""
import json
import math
import os
import shutil
import time
from dataclasses import dataclass, field, asdict, replace

import heights as _heights
from board import Board, load_board
from boardinfo import read_board_info
from constraints import evaluate_constraints, parse_constraints
from lattice import (clearance_map, default_copper_rules, lattice_for_board,
                     pad_overlap_allowances)
from place import (COURTYARD_MARGIN_MM, PlacementModel, anneal_region,
                   net_weights_from_project, pad_clearance_report,
                   pad_world_corners, part_courtyard, parts_from_board)
# The local (unrotated) courtyard frame and the rotate-a-rect transform.
# part_courtyard is the public wrapper for ONE placement; the preflight needs
# the same geometry at rotations the part is not currently at, and a second
# implementation of KiCad's CCW/Y-down convention in this file is exactly the
# drift that produces two answers to one question.
from place import (_local_geometry, _world_rect, _rect_circle_overlap,
                   _pad_has_copper)
from pathfinder import net_pads_for_board, paths_to_tracks, route_lattice
from render import render_svg
from writeback import (board_footprints, cap_track_widths,
                       load_net_class_clearances, load_net_class_widths,
                       project_file_for, write_moved_copy, write_routed_copy)
# region.py does its own text surgery (stripping the fence's existing copper),
# and writeback owns the ONE span-preserving tokenizer for this file format.
# Re-tokenizing here with a second grammar is exactly how the two drift apart.
from writeback import _parse_spans

# A via is worth this many mm of track when ranking equally-routed candidates.
# Two layers, one drill, one blocked site on both sides — cheap enough to use
# when it genuinely shortens, dear enough that a via-stitched mess loses.
VIA_WEIGHT_MM = 2.0

# Lattice window = fence + this margin (spec section 1), so a route may bulge
# just outside the fence rather than failing on a one-node pinch at the wall.
def _window_margin(pitch_mm):
    return max(2.0, 4.0 * pitch_mm)


# ── small geometry ───────────────────────────────────────────────────────────

def _clamp_to_rect(x, y, rect):
    rx, ry, rw, rh = rect
    return (min(max(x, rx), rx + rw), min(max(y, ry), ry + rh))


def _in_rect(x, y, rect, tol=0.0):
    rx, ry, rw, rh = rect
    return (rx - tol <= x <= rx + rw + tol) and (ry - tol <= y <= ry + rh + tol)


def _rects_overlap(a, b):
    return (a[0] < b[2] - 1e-9 and b[0] < a[2] - 1e-9 and
            a[1] < b[3] - 1e-9 and b[1] < a[3] - 1e-9)


def _side_of(x, y, rect):
    """Which fence side a boundary point sits on (ties broken by name, so the
    same geometry always reports the same side)."""
    rx, ry, rw, rh = rect
    gaps = {"left": abs(x - rx), "right": abs(rx + rw - x),
            "top": abs(y - ry), "bottom": abs(ry + rh - y)}
    return min(sorted(gaps), key=lambda s: gaps[s])


def _pad_bbox(p):
    """Axis-aligned hull of a pad's true rotated rect."""
    a = math.radians(getattr(p, "rotation_deg", 0.0))
    ca, sa = abs(math.cos(a)), abs(math.sin(a))
    hw = (p.width_mm * ca + p.height_mm * sa) / 2.0
    hh = (p.width_mm * sa + p.height_mm * ca) / 2.0
    return (p.x_mm - hw, p.y_mm - hh, p.x_mm + hw, p.y_mm + hh)


def _rect_of(region):
    x, y, w, h = (float(v) for v in region)
    if w <= 0 or h <= 0:
        raise ValueError(f"region w and h must be > 0, got {tuple(region)!r}")
    return (x, y, w, h)


def _grow(rect, m):
    x, y, w, h = rect
    return (x - m, y - m, w + 2 * m, h + 2 * m)


# ── board views ──────────────────────────────────────────────────────────────

def _view(brd, path, origin_mm, size_mm, pads):
    """A Board carrying a SUBSET of pads and a chosen bbox.

    Two different views are needed and they must not be conflated:
    - the LATTICE view's bbox is the region window (it sizes the grid);
    - the CLEARANCE view's bbox is the real board outline (clearance_map turns
      the bbox boundary into a hard edge band — pointing that at the fence
      would wall the region in and make every crossing net unroutable).
    """
    return Board(path=path, origin_mm=tuple(origin_mm), size_mm=tuple(size_mm),
                 copper_layers=list(brd.copper_layers), nets=dict(brd.nets),
                 pads=list(pads), tracks=[], vias=[])


def custom_pad_refs(text):
    """Footprints (by uref) with at least one CUSTOM-shaped pad.

    board.py reads a pad as its (size w h) anchor rect, so a custom pad's
    `primitives` copper — the SOT-89/SOT-223 heat tab, a thermal paddle, any
    hand-drawn shape — is invisible to everything downstream: it owns no
    lattice nodes, grows no clearance ring, and does not enlarge the placement
    courtyard. Routing will happily run a track across it, and KiCad's DRC
    will call that a short, correctly.

    This is an L0 limitation that whole-board routing has too; region.py's job
    is not to hide it. Every affected footprint inside the fence is named in
    diagnostics.geometry_warnings so the caller knows exactly which candidate
    copper to distrust before applying anything.
    """
    root = _parse_spans(text)
    nodes = [k for tag in ("footprint", "module")
             for k in root.kids if k.tag == tag]
    records = board_footprints(text)
    if len(nodes) != len(records):
        return []
    out = []
    for rec, node in zip(records, nodes):
        for pad in node.kids:
            if pad.tag != "pad":
                continue
            if any(tok == "custom" for tok, _s, _e in pad.atoms[1:]) \
                    or any(k.tag == "primitives" for k in pad.kids):
                out.append(rec.uref)
                break
    return out


def _geometry_warnings(text, movable, obstacle_refs):
    """The places where this run's model of the copper is thinner than the
    copper. Empty when there are none — never a reassuring blank."""
    hits = set(custom_pad_refs(text)) & (set(movable) | set(obstacle_refs))
    if not hits:
        return []
    return [{"kind": "custom_pad_shape", "refs": sorted(hits),
             "detail": "board.py reads a custom pad as its anchor rect only, "
                       "so these footprints' primitive copper (heat tabs, "
                       "thermal paddles) claims no lattice nodes and grows no "
                       "clearance ring — routed copper may cross it and DRC "
                       "will report a short. Check these parts by eye in the "
                       "candidate board before applying it."}]


def _pad_owner_refs(board_path, brd):
    """[uref] parallel to brd.pads — which footprint each pad belongs to.

    board_footprints walks (footprint ...) then (module ...) in file order and
    board.load_board walks the identical order, so cumulative pad counts line
    the two up; the assertion below is what makes that safe to rely on.
    """
    with open(board_path, encoding="utf-8") as f:
        records = board_footprints(f.read())
    total = sum(r.n_pads for r in records)
    if total != len(brd.pads):
        raise RuntimeError(
            f"{board_path}: footprint scan found {total} pads but load_board "
            f"parsed {len(brd.pads)} — the order invariant broke")
    out = [None] * len(brd.pads)
    off = 0
    for r in records:
        for i in range(off, off + r.n_pads):
            out[i] = r.uref
        off += r.n_pads
    return out


# ── existing copper inside the fence ─────────────────────────────────────────

def _node_points(node):
    """(x, y) points a copper node occupies: segment start/end, via at,
    arc start/mid/end. Unknown shapes yield nothing and are left alone."""
    want = {"segment": ("start", "end"), "arc": ("start", "mid", "end"),
            "via": ("at",)}.get(node.tag)
    if not want:
        return []
    pts = []
    for kid in node.kids:
        if kid.tag not in want:
            continue
        nums = []
        for tok, _s, _e in kid.atoms[1:]:
            try:
                nums.append(float(tok))
            except ValueError:
                break
        if len(nums) >= 2:
            pts.append((nums[0], nums[1]))
    return pts


def strip_tracks_in_rect(text, rect):
    """Delete the board text's copper that lives inside rect.

    (text_without_it, n_fully_inside, n_crossing). v1 solves a region as if it
    started unrouted (spec, Not-in-v1); a track that only CROSSES the fence is
    removed too — half a track is not a thing — and counted separately so the
    diagnostics can say that copper outside the fence was removed as well.
    Deletion only: emitted copper is never hand-edited (AGENTS.md rule 2).
    """
    root = _parse_spans(text)
    cuts, inside, crossing = [], 0, 0
    for node in root.kids:
        pts = _node_points(node)
        if not pts:
            continue
        hits = [_in_rect(x, y, rect) for x, y in pts]
        if all(hits):
            inside += 1
        elif any(hits):
            crossing += 1
        else:
            continue
        cuts.append((node.start, node.end))
    for start, end in sorted(cuts, reverse=True):
        text = text[:start] + text[end:]
    return text, inside, crossing


# ── terminal propagation ─────────────────────────────────────────────────────

@dataclass
class BoundaryTerminal:
    net_code: int
    net_name: str
    x_mm: float                # the pseudo-pad, ON the fence boundary
    y_mm: float
    side: str
    from_ref: str              # the outside footprint it stands for
    from_xy: tuple             # that pad's real position
    distance_mm: float         # how far outside the fence that pad sits


def boundary_terminals(brd, ref_of_pad, movable, region):
    """(terminals, fixed_inside) for the nets the region owns.

    A region net is any net with at least one pad on a movable part. Its other
    pads split three ways:
    - pads of movable parts: they move, so they are the search's variables;
    - pads of frozen parts whose center is INSIDE the fence: real immovable
      terminals, returned in fixed_inside for the HPWL and left in the lattice
      as real routable pads;
    - pads outside the fence: represented by ONE pseudo-pad, the nearest such
      pad clamped onto the fence boundary (design rule 3). Nearest is measured
      to the fence, not to the region centroid: the terminal should land where
      the net actually wants to leave.
    """
    by_net = {}
    for i, pad in enumerate(brd.pads):
        if pad.net_code <= 0:
            continue
        by_net.setdefault(pad.net_code, []).append((i, pad))

    region_nets = set()
    for i, pad in enumerate(brd.pads):
        if pad.net_code > 0 and ref_of_pad[i] in movable:
            region_nets.add(pad.net_code)

    terminals, fixed_inside = [], {}
    for code in sorted(region_nets):
        best = None
        for i, pad in by_net[code]:
            if ref_of_pad[i] in movable:
                continue
            if _in_rect(pad.x_mm, pad.y_mm, region):
                fixed_inside.setdefault(pad.net_name, []).append(
                    (pad.x_mm, pad.y_mm))
                continue
            cx, cy = _clamp_to_rect(pad.x_mm, pad.y_mm, region)
            d = math.hypot(pad.x_mm - cx, pad.y_mm - cy)
            key = (d, ref_of_pad[i] or "", pad.x_mm, pad.y_mm)
            if best is None or key < best[0]:
                best = (key, i, pad, cx, cy, d)
        if best is not None:
            _key, i, pad, cx, cy, d = best
            terminals.append(BoundaryTerminal(
                net_code=code, net_name=pad.net_name, x_mm=cx, y_mm=cy,
                side=_side_of(cx, cy, region), from_ref=ref_of_pad[i] or "?",
                from_xy=(pad.x_mm, pad.y_mm), distance_mm=d))
    return terminals, fixed_inside


def _terminal_nodes(lat, x_mm, y_mm, net, node_owner, clearance, max_rings=8):
    """Lattice nodes for a pseudo-pad at (x, y): the nearest grid site that no
    FOREIGN net owns or rings, on every lattice layer (a pseudo-pad is virtual
    copper, so it may live on either side). Returns [] when the neighbourhood
    is solid — the caller reports that instead of pretending it routed."""
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    ix0 = min(max(int(math.floor((x_mm - ox) / p + 0.5)), 0), lat.W - 1)
    iy0 = min(max(int(math.floor((y_mm - oy) / p + 0.5)), 0), lat.H - 1)
    ring = clearance.node_net if clearance is not None else {}
    for r in range(max_rings + 1):
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                ix, iy = ix0 + dx, iy0 + dy
                if not (0 <= ix < lat.W and 0 <= iy < lat.H):
                    continue
                free = []
                for il in range(lat.L):
                    n = lat.node(ix, iy, il)
                    if node_owner.get(n, net) != net:
                        continue
                    if ring.get(n, net) != net:
                        continue
                    free.append(n)
                if free:
                    return free
    return []


# ── preflight ────────────────────────────────────────────────────────────────

def _rel_courtyards(part, specs):
    """The courtyard rects a part can present RELATIVE TO ITS ORIGIN, one per
    rotation it is allowed.

    Relative, not half-extents: KiCad footprint origins are routinely off
    centre (Voxy's grid-stopper module extends 1.45 mm one way and 9.07 mm the
    other), and treating the courtyard as centred makes the closest legal
    approach look 55% further away than it is — which would have this module
    refuse a perfectly buildable constraint. Rotation goes through place.py's
    own transform rather than a second implementation of KiCad's angle
    convention living here.
    """
    local = _local_geometry(part, COURTYARD_MARGIN_MM)[1]
    angles = None
    for c in specs:
        if c.kind == "orientation_set" and c.ref == part.ref:
            angles = set(c.angles) if angles is None else angles & set(c.angles)
    if angles is None:
        angles = {0.0, 90.0, 180.0, 270.0, part.rot_deg % 360.0}
    return [_world_rect(local, 0.0, 0.0, a) for a in sorted(angles)]


def _min_center_distance(a_rects, b_rects):
    """The closest two parts' centres can come without their courtyards
    overlapping, minimised over both parts' allowed rotations. On each axis
    the parts clear each other as soon as the centre offset reaches the
    smaller of the two one-sided gaps; the other axis may then be zero, so the
    distance is the smaller axis requirement."""
    best = math.inf
    for a in a_rects:
        for b in b_rects:
            dx = min(a[2] - b[0], b[2] - a[0])
            dy = min(a[3] - b[1], b[3] - a[1])
            best = min(best, max(0.0, min(dx, dy)))
    return best


def preflight(parts, region, specs, obstacles=(), body_margin_mm=0.0):
    """Reasons this problem cannot have a solution, found BEFORE the search
    burns a minute discovering it the hard way — with the number that would
    make it possible.

    The search's own failure message ("no feasible starting placement found")
    is true but useless: it names a symptom of whichever constraint the repair
    walk happened to trip over last. These are the impossibilities that can be
    proven from geometry alone, and each one comes back as an instruction.
    """
    by_ref = {p.ref: p for p in parts}
    rx, ry, rw, rh = region
    out = []

    # Area is summed PER SIDE: front and back bodies share the fence footprint
    # but not each other (§3), so the binding constraint is the busier side, not
    # the sum. Summing would over-count a two-sided layout and refuse a solvable
    # problem — the cardinal preflight sin (a verdict may be loose, never a false
    # negative). A no-courtyard part is padded by body_margin_mm exactly as the
    # model does, so the area/fit verdict matches what the search will enforce.
    by_side = {"F": 0.0, "B": 0.0}
    for p in parts:
        x0, y0, x1, y1 = part_courtyard(p, body_margin_mm=body_margin_mm)
        by_side[getattr(p, "side", "F") or "F"] += (x1 - x0) * (y1 - y0)
        if (x1 - x0) > rw + 1e-9 or (y1 - y0) > rh + 1e-9:
            if (y1 - y0) > rw + 1e-9 or (x1 - x0) > rh + 1e-9:
                out.append(
                    f"{p.ref} courtyard is {x1 - x0:.2f} x {y1 - y0:.2f} mm "
                    f"and does not fit in the {rw:.2f} x {rh:.2f} mm fence at "
                    f"any allowed rotation")
    # Obstacle area is NOT subtracted: frozen courtyards routinely overlap
    # each other on a real board, so summing them over-counts, and an
    # over-count here would refuse a solvable problem. A preflight verdict
    # must never be a false negative — it is only allowed to be loose.
    blocked = sum(max(0.0, min(o[2], rx + rw) - max(o[0], rx))
                  * max(0.0, min(o[3], ry + rh) - max(o[1], ry))
                  for o in obstacles)
    binding = max(by_side, key=lambda s: by_side[s])
    area = by_side[binding]
    two_sided = by_side["F"] > 1e-9 and by_side["B"] > 1e-9
    if area > rw * rh + 1e-9:
        out.append(
            f"the movable {binding + '-side ' if two_sided else ''}courtyards "
            f"need {area:.1f} mm2 but the fence is only {rw * rh:.1f} mm2 (and "
            f"up to {min(blocked, rw * rh):.1f} mm2 of that is under frozen "
            f"obstacles) — grow the fence or take parts out of the component list")

    for c in specs:
        if c.kind != "adjacency_max_distance":
            continue
        a, b = by_ref.get(c.ref_a), by_ref.get(c.ref_b)
        if a is None or b is None:
            continue
        if (getattr(a, "side", "F") or "F") != (getattr(b, "side", "F") or "F"):
            continue    # opposite sides can co-locate (courtyards may overlap),
                        # so their centres reach distance 0 — any max is feasible;
                        # the courtyard-overlap floor below does NOT apply (§3).
        ra, rb = _rel_courtyards(a, specs), _rel_courtyards(b, specs)
        best = _min_center_distance(ra, rb)
        if c.mm < best - 1e-9:
            wa, ha = ra[0][2] - ra[0][0], ra[0][3] - ra[0][1]
            wb, hb = rb[0][2] - rb[0][0], rb[0][3] - rb[0][1]
            out.append(
                f"{c}: {c.ref_a} and {c.ref_b} cannot have centers closer "
                f"than {best:.2f} mm without their courtyards overlapping "
                f"({c.ref_a} is {wa:.2f} x {ha:.2f} mm, {c.ref_b} is "
                f"{wb:.2f} x {hb:.2f} mm) — adjacency_max_distance is "
                f"CENTER-to-center, so raise it to at least "
                f"{math.ceil(best * 100) / 100:.2f} (a lower bound from those "
                f"two parts alone — the fence, the 0.5 mm placement grid and "
                f"their neighbours may need more)")
    diag = math.hypot(rw, rh)
    for c in specs:
        if c.kind == "min_distance" and c.mm > diag:
            out.append(f"{c}: the fence's own diagonal is only {diag:.2f} mm, "
                       f"so no placement can separate them that far")
    return out


def _density_report(parts, region, obstacles=(), body_margin_mm=0.0):
    """Courtyard-sum vs fence area — the ~10-line preflight that predicted every
    genuine infeasibility and exonerated every false one on the Voxy bands
    (field report Finding 4: bandA 129% failed, bandE 31% did not). Computed from
    courtyard AREAS, which are position- and rotation-invariant, so it is honest
    before the parts are seeded anywhere.

    utilization is courtyard-sum / fence area — the same denominator preflight's
    hard area check uses, so the two never disagree (a report that says
    'impossible' while preflight lets the search run would be exactly the tool
    saying one thing and doing another). Obstacle area under the fence is
    reported alongside (free_utilization) but does NOT drive the verdict: the
    preflight is deliberately loose so it can never be a false negative.

    Two-sided (§3): front and back bodies share the fence footprint but not each
    other, so the binding constraint is the BUSIER side, max(front, back) — NOT
    the sum, which would over-count and refuse a solvable two-sided layout (the
    cardinal preflight sin). courtyard_mm2 is that binding side; front_mm2/
    back_mm2 break it down."""
    rx, ry, rw, rh = region
    fence = rw * rh
    by_side = {"F": 0.0, "B": 0.0}
    for p in parts:
        x0, y0, x1, y1 = part_courtyard(p, body_margin_mm=body_margin_mm)
        by_side[getattr(p, "side", "F") or "F"] += (x1 - x0) * (y1 - y0)
    court = max(by_side.values())          # the busier side is the binding one
    blocked = min(fence, sum(
        max(0.0, min(o[2], rx + rw) - max(o[0], rx))
        * max(0.0, min(o[3], ry + rh) - max(o[1], ry)) for o in obstacles))
    free = max(fence - blocked, 0.0)
    util = court / fence if fence > 0 else math.inf
    # 'impossible' is EXACTLY preflight's hard area condition (busier side >
    # fence, same body-margin courtyards) so the two can never disagree — a
    # density verdict of impossible always coincides with zero candidates + an
    # infeasible reason. 'tight' is the >=60% congestion warning.
    verdict = ("impossible" if court > fence + 1e-9
               else "tight" if util >= 0.60 else "ok")
    return {"courtyard_mm2": round(court, 1), "fence_mm2": round(fence, 1),
            "front_mm2": round(by_side["F"], 1), "back_mm2": round(by_side["B"], 1),
            "blocked_mm2": round(blocked, 1), "free_mm2": round(free, 1),
            "utilization": round(util, 3),
            "free_utilization": round(court / free, 3) if free > 0 else None,
            "two_sided": by_side["F"] > 1e-9 and by_side["B"] > 1e-9,
            "verdict": verdict}


def _density_expansion(region, court_mm2, target=0.75):
    """A concrete fence growth from courtyard density (finding 4's ask: a number
    next to the utilization, not just 'grow the fence'): how much to add so the
    movable courtyards fill at most `target` of the fence. Grows the shorter side
    and reports in the same shape as _suggest_expansion, so the CLI renders it
    identically. Returns None when the fence already meets the target."""
    rx, ry, rw, rh = region
    fence = rw * rh
    if target <= 0 or court_mm2 <= target * fence + 1e-9:
        return None
    need_area = court_mm2 / target
    short, long_, side = (rw, rh, "right") if rw <= rh else (rh, rw, "bottom")
    grow = max(2.0, math.ceil((need_area / long_ - short) * 2) / 2)
    return {"direction": side, "mm": round(grow, 2), "pressure": None,
            "reason": f"courtyards need {court_mm2:.0f} mm2 = "
                      f"{court_mm2 / fence:.0%} of the {fence:.0f} mm2 fence; add "
                      f"~{grow:.1f} mm on the {side} to reach {target:.0%} "
                      f"utilization (a density lower bound — packing and "
                      f"clearances may need more)"}


def _z_clearance_report(parts, z_clear, overrides):
    """Component height vs the enclosure's per-side clearance (finding §3, the
    two-sided precondition). For each part on a side that HAS a stated clearance,
    resolve its height (heights.resolve: override > footprint > family upper
    bound > unknown) and sort it:

    - too_tall: a MEASURED height (footprint/override) above the clearance — a
      hard, state-independent infeasibility (it fits nowhere on that side), so it
      is folded into the preflight reasons and the run returns zero candidates;
    - unverified: an UNKNOWN height, or a family UPPER BOUND that exceeds the
      clearance (the real part may be shorter and fit — a conservative bound must
      never HARD-refuse, that would be a false negative). Flagged for the human;
    - fits: provably clears (measured within, or even the upper bound clears);
    - unchecked: on a side with NO stated clearance — its height fit is UNKNOWN.
      A part here is NOT verified, so a run that says 'verified' while any part is
      unchecked would be lying (adversarial review 2026-07-20): setting only ONE
      side's limit must not verbally bless the other side's parts.

    The caller folds too_tall into preflight (zero candidates), flags unverified
    AND unchecked, and only reports z_clearance_verified when every part sat on a
    limited side and passed."""
    too_tall, unverified, unchecked, fits, per_part = [], [], [], [], {}
    measured_src = ("footprint", "override:ref", "override:fpid")
    for p in parts:
        side = getattr(p, "side", "F") or "F"
        limit = z_clear.get(side)
        h, src = _heights.resolve(getattr(p, "ref", None), getattr(p, "fpid", None),
                                  getattr(p, "height_mm", None), overrides)
        per_part[p.ref] = {"side": side, "height_mm": h, "source": src,
                           "limit_mm": limit}
        if limit is None:
            unchecked.append({"ref": p.ref, "side": side})
        elif h is None:
            unverified.append({"ref": p.ref, "side": side,
                               "why": "height unknown"})
        elif h > limit + 1e-6 and src in measured_src:
            too_tall.append({"ref": p.ref, "side": side,
                             "height_mm": h, "limit_mm": limit})
        elif h > limit + 1e-6:
            unverified.append({"ref": p.ref, "side": side,
                               "why": f"family upper bound {h:g} mm exceeds the "
                                      f"{limit:g} mm limit — measure or override"})
        else:
            fits.append(p.ref)
    return too_tall, unverified, unchecked, fits, per_part


# ── seeding ──────────────────────────────────────────────────────────────────

def _translate(part, nx, ny):
    """The same part at a new center. Pure translation, so pad rotations and
    offsets need no rework — the one transform this module is allowed to do
    itself; anything involving rotation goes through writeback."""
    dx, dy = nx - part.x_mm, ny - part.y_mm
    pads = tuple(replace(p, x_mm=p.x_mm + dx, y_mm=p.y_mm + dy)
                 for p in part.pads)
    return replace(part, x_mm=nx, y_mm=ny, pads=pads)


def _on_board(court_rect, outline_regions):
    """True when a part's courtyard overlaps SOME Edge.Cuts outline — it sits on
    a real board, so it is not a default/F8 drop off in empty space. The
    negation is 'pile': a footprint whose courtyard is off EVERY board outline,
    belonging to no area (field report Finding 1).

    This is the predicate the UNPLACED REPORT uses (_resolve_placement_set) to
    flag UN-named parts that belong to no area. It is deliberately NOT the
    scatter trigger: scatter only ever sees parts the caller explicitly NAMED
    for a fence, and a named part off that fence was named to be moved there —
    see _scatter_pile. Outlines are bbox proxies (board.OutlineRegion), the same
    proxy the fence and clearance model already use."""
    return any(_rects_overlap(court_rect, reg.bounds)
               for reg in (outline_regions or ()))


def _scatter_pile(order, pinned, live, courts, region, grid_mm, obstacles):
    """Lay every non-pinned part whose courtyard is off THIS fence onto a
    deterministic, non-overlapping grid inside it. Mutates live/courts; returns
    [move dict].

    THE off-board-pile start (the design-driver's real case): free parts are
    default-placed in a pile at ~the board origin, carrying NO positional
    information — dozens of courtyards stacked on one point, far from the area.
    Their snapped home positions are a maximally infeasible SA start, and
    place.py's repair walk (random single-part relocation, accepted only when
    it lowers the O(N^2) overlap count) cannot reliably dig out of it.

    So the parts with no meaningful in-fence position are placed FRESH: a
    regular grid sized to the largest courtyard among them (plus one grid step
    of gap), spread across the fence, each cell snapped to the placement grid.
    Cells do not overlap and each part's courtyard fits its cell, so the scatter
    is non-overlapping at the start; obstacles and already-placed (in-fence /
    pinned) parts are stepped over. Deterministic — no RNG — so identical calls
    still return identical candidates.

    The trigger is "courtyard does not overlap THIS fence". Every part reaching
    this function was EXPLICITLY NAMED for this fence (optimize_region's
    components list), so a named part sitting off the fence — the origin pile, a
    routine F8 / Update-PCB drop at arbitrary coordinates, a neighbouring area,
    an adjacent band — is one the caller asked to move HERE, and it is scattered
    in. Only a part whose courtyard already OVERLAPS the fence has a real
    in-fence position; it is left where it is and the anneal refines it. The
    trigger is fence-relative, NOT outline-relative, on purpose: an
    outline-relative test would refuse to scatter a named on-board part (in
    another area / an adjacent band) and strand it off-fence, where the weak
    repair walk cannot reliably place it.

    To preserve a whole seeded ARRANGEMENT rather than gridding each part afresh,
    use respect_positions=True (seed_placement), which translates the off-fence
    group in as a rigid unit. 'Pile' in the field-report sense — a part off
    every board outline — is the concern of the UNPLACED REPORT
    (_resolve_placement_set / _on_board), which flags UN-named parts belonging
    to no area; scatter's narrower job is to give every NAMED part a feasible
    in-fence start.
    """
    rx, ry, rw, rh = region
    fence_rect = (rx, ry, rx + rw, ry + rh)
    scatter = [r for r in order if r not in pinned
               and not _rects_overlap(courts[r], fence_rect)]
    if not scatter:
        return []
    cw = max(courts[r][2] - courts[r][0] for r in scatter)
    ch = max(courts[r][3] - courts[r][1] for r in scatter)
    step_x = max(grid_mm, math.ceil((cw + grid_mm) / grid_mm) * grid_mm)
    step_y = max(grid_mm, math.ceil((ch + grid_mm) / grid_mm) * grid_mm)
    slots = []
    y = ry + step_y / 2.0
    while y + ch / 2.0 <= ry + rh + 1e-9 and len(slots) < 6 * len(scatter) + 1:
        x = rx + step_x / 2.0
        while x + cw / 2.0 <= rx + rw + 1e-9:
            slots.append((rx + round((x - rx) / grid_mm) * grid_mm,
                          ry + round((y - ry) / grid_mm) * grid_mm))
            x += step_x
        y += step_y
    scatter_set = set(scatter)
    used, moves = set(), []
    for r in scatter:
        for si, (sx, sy) in enumerate(slots):
            if si in used:
                continue
            cand = _translate(live[r], sx, sy)
            rect = part_courtyard(cand)
            if rect[0] < rx - 1e-9 or rect[1] < ry - 1e-9 \
                    or rect[2] > rx + rw + 1e-9 or rect[3] > ry + rh + 1e-9:
                continue
            if any(_rects_overlap(rect, ob) for ob in obstacles):
                continue
            # step over fixed furniture and parts already placed inside the
            # fence; other scatter parts get their own distinct slots
            if any(other not in scatter_set and _rects_overlap(rect, courts[other])
                   for other in courts if other != r):
                continue
            frm = [round(live[r].x_mm, 3), round(live[r].y_mm, 3)]
            live[r], courts[r], used = cand, rect, used | {si}
            moves.append({"ref": r, "constraint": "off-board pile -> fence scatter",
                          "from": frm, "to": [round(sx, 3), round(sy, 3)],
                          "distance_mm": round(math.hypot(sx - frm[0],
                                                          sy - frm[1]), 3),
                          "displaces_movable": 0})
            break
        # no free slot found: leave at home; repair walk / anneal will try, and
        # an unfittable pile is caught by preflight's area check upstream.
    return moves


def _translate_group_into_fence(order, pinned, live, courts, region):
    """RIGIDLY translate the OFF-FENCE parts by ONE common vector so their
    collective courtyard bbox lands inside the fence — preserving every
    relative offset (the caller's deliberate arrangement), never scattering
    each part independently. This is `respect_positions`: a shelf-packed or
    stage.py-staged arrangement in a margin/staging strip is a real layout, not
    a meaningless origin-pile, so it is moved in as a unit.

    Only parts whose courtyard does NOT touch the fence are moved — the SAME
    trigger _scatter_pile uses. A part already in the fence has a real position
    and is left exactly where it is (translating the whole group off ONE
    off-fence part would otherwise eject the in-fence ones). Returns the move
    list (empty if nothing is off-fence, or the off-fence group already fits).

    Caveats, honest: this does NOT avoid frozen obstacles or in-fence parts —
    if the translated group lands on a locked footprint the anneal repairs it,
    which perturbs the arrangement, so seed into a clear area. And an
    arrangement wider/taller than the fence aligns to the near edge and
    overflows; the anneal then repairs the overflowing parts to feasibility
    (the seeded arrangement is partially lost), or fails if truly infeasible."""
    rx, ry, rw, rh = region
    fence = (rx, ry, rx + rw, ry + rh)
    movers = [r for r in order if r not in pinned
              and not _rects_overlap(courts[r], fence)]
    if not movers:
        return []                       # nothing off-fence — all respected in place
    x0 = min(courts[r][0] for r in movers)
    y0 = min(courts[r][1] for r in movers)
    x1 = max(courts[r][2] for r in movers)
    y1 = max(courts[r][3] for r in movers)
    dx = (rx - x0) if x0 < rx else ((rx + rw - x1) if x1 > rx + rw else 0.0)
    dy = (ry - y0) if y0 < ry else ((ry + rh - y1) if y1 > ry + rh else 0.0)
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return []                       # already inside — respected in place
    moves = []
    for r in movers:
        frm = (live[r].x_mm, live[r].y_mm)
        live[r] = _translate(live[r], frm[0] + dx, frm[1] + dy)
        courts[r] = part_courtyard(live[r])
        moves.append({"ref": r,
                      "constraint": "respect-positions: rigid group -> fence",
                      "from": [round(frm[0], 3), round(frm[1], 3)],
                      "to": [round(live[r].x_mm, 3), round(live[r].y_mm, 3)],
                      "distance_mm": round(math.hypot(dx, dy), 3),
                      "displaces_movable": 0})
    return moves


def seed_placement(parts, region, specs, obstacles, grid_mm,
                   respect_positions=False):
    """Move parts into a tractable starting arrangement BEFORE the anneal.

    Default (respect_positions=False), two jobs in order:

    1. SCATTER the off-board pile (_scatter_pile). Parts default-placed at the
       board origin carry no position, so they are laid on a fresh grid inside
       the fence — this is the design-driver's real start, parts arriving from
       a pile rather than a rough layout.
    2. SEED tight adjacencies. place.py's repair walk fixes an infeasible start
       by random single-part relocations, which is fine for "this courtyard
       overlaps that one" and hopeless for "this part must be within 4 mm of
       that one": the target is a handful of grid sites out of thousands, and
       the walk only accepts moves that reduce the problem COUNT, so it never
       sees a gradient toward them. "The grid stopper stays at its pin" is the
       constraint an amp designer actually writes, so the part is put next to
       its partner here, then the annealer explores from there.

    respect_positions=True: the caller's positions ARE the arrangement (a
    shelf-pack or a stage.py-staged layout). Skip the scatter and the adjacency
    reseed entirely; rigidly translate the OFF-FENCE group into the fence as a
    unit, preserving relative offsets, and let the anneal refine from there.
    This is what makes a seeded start survive instead of being flattened onto a
    fresh grid. Parts already inside the fence never move (only off-fence parts
    are translated); an adjacency_max_distance the seed does not already satisfy
    is NOT reseeded, so seeds should pre-satisfy their adjacency constraints.

    Translation only, nearest grid site first, and every move is reported in
    diagnostics.seeded so nothing happens silently.
    """
    by_ref = {p.ref: p for p in parts}
    order = [p.ref for p in parts]
    pinned = {c.ref for c in specs if c.kind == "fixed"}
    rx, ry, rw, rh = region
    live = {p.ref: _translate(p, p.x_mm, p.y_mm) for p in parts}
    courts = {r: part_courtyard(p) for r, p in live.items()}
    obstacles = list(obstacles)
    if respect_positions:
        moves = _translate_group_into_fence(order, pinned, live, courts, region)
        return [live[r] for r in order], moves
    moves = _scatter_pile(order, pinned, live, courts, region, grid_mm,
                          obstacles)

    def fits(ref, cx, cy):
        """(part, n_movable_overlaps) or None.

        The fence and the FROZEN furniture are hard here — nothing downstream
        can fix a part parked half outside the region or on top of a part that
        cannot move. Overlaps with other MOVABLE parts are only counted, not
        refused: shuffling those apart is precisely what place.py's repair
        walk is good at, and refusing them would make the seed fail on every
        densely-packed hand layout (which is all of them).
        """
        p = _translate(live[ref], cx, cy)
        rect = part_courtyard(p)
        if rect[0] < rx - 1e-9 or rect[1] < ry - 1e-9 \
                or rect[2] > rx + rw + 1e-9 or rect[3] > ry + rh + 1e-9:
            return None
        for ob in obstacles:
            if _rects_overlap(rect, ob):
                return None
        clashes = sum(1 for other, r in courts.items()
                      if other != ref and _rects_overlap(rect, r))
        if any(other in pinned and _rects_overlap(rect, r)
               for other, r in courts.items() if other != ref):
            return None
        return p, clashes

    for c in specs:
        if c.kind != "adjacency_max_distance":
            continue
        if c.ref_a not in live or c.ref_b not in live:
            continue
        a, b = live[c.ref_a], live[c.ref_b]
        if math.hypot(a.x_mm - b.x_mm, a.y_mm - b.y_mm) <= c.mm + 1e-9:
            continue
        # move the one that may move; if both may, move the smaller part
        options = [r for r in (c.ref_a, c.ref_b) if r not in pinned]
        if not options:
            continue
        options.sort(key=lambda r: ((courts[r][2] - courts[r][0])
                                    * (courts[r][3] - courts[r][1]),
                                    order.index(r)))
        mover = options[0]
        anchor = live[c.ref_b if mover == c.ref_a else c.ref_a]
        span = int(math.ceil(c.mm / grid_mm)) + 1
        best = None
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                cx = rx + round((anchor.x_mm - rx) / grid_mm + dx) * grid_mm
                cy = ry + round((anchor.y_mm - ry) / grid_mm + dy) * grid_mm
                d = math.hypot(cx - anchor.x_mm, cy - anchor.y_mm)
                if d > c.mm + 1e-9:
                    continue
                got = fits(mover, cx, cy)
                if got is None:
                    continue
                p, clashes = got
                key = (clashes, round(d, 6), cy, cx)
                if best is None or key < best[0]:
                    best = (key, p)
        if best is None:
            continue          # no legal site: the anneal reports it properly
        live[mover] = best[1]
        courts[mover] = part_courtyard(best[1])
        moves.append({"ref": mover, "constraint": str(c),
                      "from": [round(by_ref[mover].x_mm, 3),
                               round(by_ref[mover].y_mm, 3)],
                      "to": [round(best[1].x_mm, 3), round(best[1].y_mm, 3)],
                      "distance_mm": round(best[0][1], 3),
                      "displaces_movable": best[0][0]})
    return [live[r] for r in order], moves


# ── results ──────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    id: int
    placements: dict            # ref -> (x_mm, y_mm, rot_deg)
    routed: str                 # "nets_ok/nets_total"
    nets_ok: int
    nets_total: int
    wirelength_mm: float
    vias: int
    constraint_violations: list
    score: float
    board_copy: str
    svg: str
    hpwl_mm: float              # the search's cheap energy, for comparison
    failed: list                # (net_name, reason)
    elite_rank: int             # where the cheap energy had ranked it
    iterations: int


@dataclass
class RegionResult:
    candidates: list = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)

    def as_dict(self):
        return {"candidates": [asdict(c) for c in self.candidates],
                "diagnostics": self.diagnostics}


# ── area fencing and the movable/auto-fixed split ────────────────────────────

def area_fence(board_path, area_index):
    """Fence rect (x, y, w, h) for outline region `area_index` of the board.

    --area N fences on the Nth disjoint Edge.Cuts outline (board.outline_regions
    via lattice.board_outline_regions), so a multi-area / multi-board file is
    driven area by area without the caller measuring rectangles by hand. The
    fence is the region's BOUNDING BOX; a part whose courtyard would poke past a
    non-rectangular outline is caught by the anneal's fence check exactly as
    with a hand-typed region. Raises ValueError for an out-of-range index,
    naming how many regions exist."""
    from lattice import board_outline_regions
    regions = board_outline_regions(load_board(board_path))
    if not (0 <= area_index < len(regions)):
        raise ValueError(
            f"area {area_index} out of range: this board has {len(regions)} "
            f"outline region(s) (0..{len(regions) - 1}); run --list-regions")
    r = regions[area_index]
    return (r.origin_mm[0], r.origin_mm[1], r.size_mm[0], r.size_mm[1])


def net_adjacency(board_path, refs):
    """{ref: [connected refs]} — which of `refs` share a net with which, as
    DATA the design-driver may consult when partitioning by circuit function.

    This is deliberately NOT clustering: the tool never decides groupings
    (geometric/connectivity clustering is the analog-layout anti-pattern the
    architecture rejects — the design thread partitions from the SCHEMATIC).
    It only reports the net edges between the named parts, so a human/agent
    reasoning about a group can see which of its members actually connect."""
    from writeback import board_footprints
    brd = load_board(board_path)
    ref_of_pad = _pad_owner_refs(board_path, brd)
    want = set(refs)
    nets = {}
    for i, pad in enumerate(brd.pads):
        r = ref_of_pad[i]
        if r in want and pad.net_code > 0:
            nets.setdefault(pad.net_code, set()).add(r)
    adj = {r: set() for r in refs}
    for members in nets.values():
        for a in members:
            adj[a] |= (members - {a})
    return {r: sorted(adj[r]) for r in refs}


def _resolve_placement_set(all_parts, region, components, auto_fix_locked,
                           regions=None):
    """(movable, auto_fixed, extra_fixed, unplaced) for a fenced area.

    Turns the caller's intent into the two lists optimize_region runs on:

    - movable: the parts allowed to move. The PRIMARY path is an EXPLICIT ref
      list per area (components), because the design-driver's free parts start
      in an off-board pile and carry no position to select on. When components
      is None, movable falls back to the CONVENIENCE default — every UNLOCKED
      footprint whose center already sits inside the fence (an already-placed
      board being re-optimized in place).
    - auto_fixed: every LOCKED footprint whose center is inside the fence.
      Locked parts never move; they are auto-fixed (added to the model with a
      fixed() constraint) so they are obstacles and fixed HPWL anchors WITHOUT
      the caller listing them. extra_fixed is the fixed(ref) strings to add.
    - unplaced: unlocked parts the caller did NOT name that sit outside every
      outline region (a default-placed pile). They belong to no area and are
      NOT silently swept into any movable set — named here so the caller
      assigns them to an area explicitly.

    A locked part named in an explicit components list is a hard error: locked
    parts cannot move (unlock it in KiCad, or drop it — it is auto-fixed anyway
    if it is inside the fence)."""
    locked_in = sorted(r for r, p in all_parts.items()
                       if p.locked and _in_rect(p.x_mm, p.y_mm, region))
    auto_fixed = list(locked_in) if auto_fix_locked else []
    if components is None:
        movable = sorted(r for r, p in all_parts.items()
                         if not p.locked and _in_rect(p.x_mm, p.y_mm, region))
    else:
        movable = list(dict.fromkeys(components))
        bad = [r for r in movable if r in all_parts and all_parts[r].locked]
        if bad:
            raise ValueError(
                f"locked footprint(s) {', '.join(bad)} named as movable — "
                f"locked parts cannot move. Unlock them in KiCad, or drop them "
                f"from the component list (a locked part inside the fence is "
                f"auto-fixed anyway).")
    extra_fixed = [f"fixed({r})" for r in auto_fixed if r not in movable]
    unplaced = []
    if components is None and regions:
        for r, p in sorted(all_parts.items()):
            if p.locked or r in movable:
                continue
            if not _on_board(part_courtyard(p), regions):
                unplaced.append(r)
    return movable, auto_fixed, extra_fixed, unplaced


# ── the call ─────────────────────────────────────────────────────────────────

def optimize_region(board_path, components=None, region=None, constraints=(),
                    k=5, pitch_mm=0.5, layers=None, out_dir="out/region",
                    seed=0, sweeps=200, grid_mm=0.5,
                    via_weight_mm=VIA_WEIGHT_MM, clearance_mm=None,
                    class_weights=None, keep_work=False, progress=None,
                    route_kwargs=None, area=None, auto_fix_locked=True,
                    respect_positions=False, hole_clearance_mm=3.0,
                    min_gap_mm=0.25, body_margin_mm=1.0, pad_clearance=True,
                    z_front_mm=None, z_back_mm=None, height_overrides=None):
    """Place `components` inside `region` and prove each candidate by routing.

    board_path   : READ-ONLY source board
    components   : reference designators allowed to move (ref#N for duplicates).
                   THE PRIMARY path — the design thread partitions the schematic
                   by circuit function and hands one group's refs per area,
                   because free parts start in an off-board pile and carry no
                   position to select on. None falls back to the convenience
                   default: every UNLOCKED footprint already inside the fence.
    region       : (x, y, w, h) mm in board coordinates — the fence. Omit and
                   pass `area=N` to fence on board outline region N instead.
    area         : fence on board.outline_regions[N] rather than a hand-typed
                   region (area_fence). Exactly one of region / area is given.
    auto_fix_locked : LOCKED footprints inside the fence are auto-treated as
                   fixed() — frozen at their KiCad position as obstacles and HPWL
                   anchors — without the caller listing them (reported in
                   diagnostics.auto_fixed). False disables it (A/B only).
    constraints  : the closed constraints.py vocabulary (strings or dicts)
    hole_clearance_mm : screw-head clearance kept clear around each board
                   mounting hole (default 3.0 for M3), hole-edge outward. Board
                   holes (Edge.Cuts/User gr_circle + MountingHole fp_circle)
                   become circular keep-outs courtyards are rejected from.
    min_gap_mm   : required clearance GAP between courtyards, and between a
                   courtyard and a frozen obstacle (default 0.25) — NOT just
                   non-overlap, since two abutting courtyards have zero clearance
                   and KiCad DRC flags it. 0 reverts to plain no-overlap. If a
                   run that used to place returns zero candidates, this default
                   is the first knob to check (it's named in infeasible_reason).
    pad_clearance : enforce pad-to-pad COPPER clearance between different nets
                   on shared layers, per net class (default True). Courtyard
                   non-overlap is an assembly halo and says nothing about where
                   one pad's copper sits relative to another net's — a fat FET
                   pad or an HV creepage rule shorts with clear courtyards. The
                   search hard-rejects it and every shipped candidate is
                   re-verified over its WHOLE board (diagnostics.placement_verify,
                   feedback/placement-fidelity-2026-07-20 §2/§5). False disables
                   it (A/B measurement only).
    k            : candidates to ship; k*3 finalists are routed
    out_dir      : everything this call writes lands here

    Returns RegionResult. Raises ValueError for caller mistakes (unknown ref,
    bad fence, a locked part named movable); an infeasible fence is NOT an
    exception — it comes back as zero candidates and diagnostics.infeasible_reason,
    because the caller's next move is to read the diagnostics and move the fence.
    """
    t_start = time.perf_counter()
    say = progress or (lambda *_a, **_kw: None)
    if (region is None) == (area is None):
        raise ValueError("pass exactly one of region=(x,y,w,h) or area=N "
                         "(area fences on a board outline region)")
    region = _rect_of(area_fence(board_path, int(area)) if area is not None
                      else region)
    layers = list(layers or ["F.Cu", "B.Cu"])
    k = int(k)
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    os.makedirs(out_dir, exist_ok=True)
    work = os.path.join(out_dir, "work")
    work_src = os.path.join(work, "stripped")
    work_elites = os.path.join(work, "elites")
    os.makedirs(work_src, exist_ok=True)
    os.makedirs(work_elites, exist_ok=True)

    # 1 ── scan the source board once
    with open(board_path, encoding="utf-8") as f:
        src_text = f.read()
    brd = load_board(board_path)
    ref_of_pad = _pad_owner_refs(board_path, brd)
    all_parts = parts_from_board(board_path)

    # movable / auto-fixed split. Explicit refs are the primary path; locked
    # parts inside the fence are auto-fixed; a default-placed pile off every
    # board is named, never silently swept in.
    from lattice import board_outline_regions
    outline_regions = board_outline_regions(brd)
    movable_refs, auto_fixed, extra_fixed, unplaced = _resolve_placement_set(
        all_parts, region, None if components is None else list(components),
        auto_fix_locked, regions=outline_regions)
    movable_source = "explicit list" if components is not None else \
        "auto: unlocked footprints inside the fence"
    # the model runs on movable + auto-fixed (locked) parts; auto-fixed parts
    # are pinned by their fixed() constraint but are real terminals/obstacles.
    components = list(movable_refs) + [r for r in auto_fixed
                                       if r not in movable_refs]
    if not components:
        raise ValueError(
            "nothing to place: no movable parts. Name the group's refs with "
            "components=[...] (the primary path — free parts start off-board), "
            f"or place parts inside the fence first. Fence {tuple(round(v,2) for v in region)}.")
    unknown = [r for r in components if r not in all_parts]
    if unknown:
        raise ValueError(
            f"unknown component(s) {', '.join(unknown)} — the board has "
            f"{len(all_parts)} footprints; duplicated designators must be "
            f"addressed as ref#N (see writeback.board_footprints)")
    movable = set(components)
    parts = [all_parts[r] for r in components]
    # footprints with no F.CrtYd: placed on a pad-bbox + body-margin estimate,
    # warned by name below (finding C). Computed once, used by the say line, the
    # geometry_warnings entry, and the courtyard_source diagnostic.
    no_crtyd = sorted(r for r in components
                      if all_parts[r].local_courtyard is None)
    n_real_court = len(components) - len(no_crtyd)
    n_proxy_court = len(no_crtyd)

    # constraints are parsed here too so a typo fails before any search; the
    # auto-fix fixed(ref) constraints are appended (deduped) to the caller's.
    specs = parse_constraints(list(constraints) + extra_fixed,
                              known_refs=movable)

    # 2 ── frozen furniture that intrudes into the fence
    fence_rect = (region[0], region[1],
                  region[0] + region[2], region[1] + region[3])
    obstacles, obstacle_refs, obstacle_sides, frozen_pads = [], [], [], []
    for ref, part in sorted(all_parts.items()):
        if ref in movable:
            continue
        rect = part_courtyard(part, body_margin_mm=body_margin_mm)
        if _rects_overlap(rect, fence_rect):
            obstacles.append(rect)
            obstacle_refs.append(ref)
            obstacle_sides.append(part.side)    # a back obstacle body cannot
                                                # block a front movable body (§3)
            # frozen COPPER: a locked/out-of-fence part's pads still occupy
            # copper the movable group must clear (pad clearance, finding §2).
            # Only the intruding obstacle set feeds the search (fast); the
            # per-candidate write-time verify below re-checks the WHOLE board,
            # so a short against copper outside this set is still caught (§5).
            if pad_clearance:
                for pad in part.pads:
                    if pad.layers and _pad_has_copper(pad):
                        frozen_pads.append(
                            (pad.net_name or "", frozenset(pad.layers),
                             tuple(pad_world_corners(pad))))

    # 2b ── mounting-hole keep-outs (finding A): each board hole (gr_circle),
    # inflated by screw-head clearance, that reaches into the fence. Movable
    # courtyards are hard-rejected against these — the fence-edge mechanism, but
    # circular. A hole whose inflated circle never enters the fence is skipped.
    keepouts = [(cx, cy, r + hole_clearance_mm) for cx, cy, r in brd.holes
                if _rect_circle_overlap(fence_rect, cx, cy, r + hole_clearance_mm)]

    # 2c ── density preflight (finding 4): courtyard-sum vs fence area, computed
    # from position-invariant areas, so it is honest before seeding. Predicts the
    # genuine "too much for one fence" infeasibilities and, above ~60%, warns
    # that the search will be tight — turned into a concrete grow number below.
    density = _density_report(parts, region, obstacles,
                              body_margin_mm=body_margin_mm)

    # 3 ── terminal propagation
    terminals, fixed_inside = boundary_terminals(brd, ref_of_pad, movable,
                                                 region)
    fixed_points = {name: list(pts) for name, pts in fixed_inside.items()}
    for t in terminals:
        fixed_points.setdefault(t.net_name, []).append((t.x_mm, t.y_mm))

    region_net_codes = {t.net_code for t in terminals}
    for i, pad in enumerate(brd.pads):
        if pad.net_code > 0 and ref_of_pad[i] in movable:
            region_net_codes.add(pad.net_code)
    net_names = {brd.nets.get(c, "") for c in region_net_codes}
    net_weights = net_weights_from_project(board_path, sorted(net_names),
                                           class_weights=class_weights)

    # pad-copper clearance per net class (finding §2). load_net_class_clearances
    # is the emitters' own resolution (same _resolve_net_classes as widths), so
    # a net's placement clearance and the clearance the router spaces it by can
    # never disagree. Keyed by NAME to match the model's net identity. Nets no
    # class claims fall to the project's Default clearance (default_pad_clr); on
    # a tube amp the HV class carries the creepage number the placer must honour.
    pro = project_file_for(board_path)
    pad_clr_by_name, default_pad_clr = {}, 0.0
    if pad_clearance:
        default_pad_clr = default_copper_rules(board_path)[0]
        if pro:
            pad_clr_by_name = {brd.nets.get(code, ""): c for code, c
                               in load_net_class_clearances(pro, brd.nets).items()}
    pad_clearances = pad_clr_by_name if pad_clearance else None

    say(f"fence {region[2]:.1f} x {region[3]:.1f} mm"
        + (f" (area {area})" if area is not None else "")
        + f" | {len(movable_refs)} movable | {len(auto_fixed)} auto-fixed locked"
        + (f" ({', '.join(auto_fixed)})" if auto_fixed else "")
        + f" | {len(obstacles)} frozen obstacles | {len(keepouts)} hole keep-outs"
        + f" | {len(terminals)} boundary nets"
        + (" | pad-clearance on" if pad_clearance else ""))
    if unplaced:
        say(f"note    {len(unplaced)} unlocked part(s) sit off every board "
            f"outline (a default-placed pile) and were NOT auto-added: "
            f"{', '.join(unplaced[:12])}"
            + (" ..." if len(unplaced) > 12 else "")
            + " — name them in components to place them in an area")
    if no_crtyd:
        say(f"warn    {len(no_crtyd)} footprint(s) draw NO courtyard — keep-out "
            f"is a pad-bbox + {body_margin_mm:g} mm body estimate, not a real "
            f"courtyard (a body overhanging its pads is under-modelled): "
            f"{', '.join(no_crtyd[:12])}"
            + (" ..." if len(no_crtyd) > 12 else ""))
    # density: always printed (finding 4 — predictable failures should be
    # predicted); a WARN prefix at >=60%, since above that the search is tight
    # even when it succeeds and above 100% it cannot succeed as one fence.
    say((f"warn    " if density["verdict"] != "ok" else "density ")
        + f"{density['utilization']:.0%} courtyard density "
        + (f"(busier side: {density['courtyard_mm2']:.0f} of "
           f"{density['fence_mm2']:.0f} mm2 fence; F {density['front_mm2']:.0f} / "
           f"B {density['back_mm2']:.0f})" if density.get("two_sided")
           else f"({density['courtyard_mm2']:.0f} of {density['fence_mm2']:.0f} "
                f"mm2 fence" + (f", {density['blocked_mm2']:.0f} mm2 under "
                                f"obstacles" if density["blocked_mm2"] > 0.5
                                else "") + ")")
        + ("  — IMPOSSIBLE as one fence; grow it or split the group"
           if density["verdict"] == "impossible"
           else "  — tight, expect congestion and a slow / failing search"
           if density["verdict"] == "tight" else ""))

    # component height vs the enclosure's per-side z-clearance (finding §3, the
    # two-sided precondition). The available height per side comes from the
    # board's own info box (boardinfo — the "box beside each board" the human
    # fills), with the call params overriding it; heights resolve through the C1
    # sourcing layer. A back part TALLER than the back-side gap is physically
    # un-buildable — the model places bodies correctly in XY, but a chassis foul
    # is invisible to copper, so it is named here, never silently blessed.
    binfo = read_board_info(board_path)
    z_clear = {"F": z_front_mm if z_front_mm is not None else binfo["z_front_mm"],
               "B": z_back_mm if z_back_mm is not None else binfo["z_back_mm"]}
    z_active = any(v is not None for v in z_clear.values())
    z_source = ("call params" if (z_front_mm is not None or z_back_mm is not None)
                else "board-info box" if binfo["found"] else None)
    back_refs = sorted(p.ref for p in parts
                       if (getattr(p, "side", "F") or "F") == "B")
    z_too_tall, z_unverified, z_unchecked, z_fits, z_per_part = (
        _z_clearance_report(parts, z_clear, dict(height_overrides or {}))
        if z_active else ([], [], [], [], {}))
    # a part on a side with NO limit is UNCHECKED; verified is true only when
    # EVERY part sat on a limited side and cleared (no too_tall, no unverified,
    # no unchecked) — setting one side's limit must never bless the other's.
    z_verified = bool(z_active and not z_too_tall and not z_unverified
                      and not z_unchecked)
    back_unchecked = [v["ref"] for v in z_unchecked if v["side"] == "B"]
    if z_active:
        say(f"z-clear  front {z_clear['F'] if z_clear['F'] is not None else '—'} "
            f"/ back {z_clear['B'] if z_clear['B'] is not None else '—'} mm "
            f"({z_source}) | {len(z_fits)} fit, {len(z_too_tall)} too tall, "
            f"{len(z_unverified)} unverified, {len(z_unchecked)} unchecked")
    if z_too_tall:
        say(f"warn    {len(z_too_tall)} part(s) exceed their side's enclosure "
            f"clearance and cannot be placed there: "
            + "; ".join(f"{v['ref']} {v['height_mm']:g}>{v['limit_mm']:g}mm "
                        f"({'back' if v['side'] == 'B' else 'front'})"
                        for v in z_too_tall[:8]))
    if z_unverified:
        say(f"warn    {len(z_unverified)} part(s) on a height-limited side have "
            f"UNVERIFIED height — measure or add a --height override: "
            + ", ".join(v["ref"] for v in z_unverified[:12]))
    # the BACK is the tight side: if it carries parts but has NO limit, warn
    # whether or not the FRONT was limited (the review's exact bug — a front
    # limit must not suppress the back-unverified signal).
    if back_unchecked:
        say(f"warn    {len(back_unchecked)} part(s) on the BACK but NO back "
            f"z-clearance set (board-info box or --z-back) — their height vs the "
            f"chassis is UNVERIFIED: {', '.join(back_unchecked[:12])}"
            + (" ..." if len(back_unchecked) > 12 else ""))

    # 4 ── placement search. Preflight first (proving it impossible costs
    # milliseconds, discovering it by search costs the user's attention),
    # then a constraint-aware seed, then the anneal.
    impossible = preflight(parts, region, specs, obstacles,
                           body_margin_mm=body_margin_mm)
    # a part taller than its side's enclosure clearance fits NOWHERE on that side
    # (state-independent, like a part too big for the fence) — so it is a
    # preflight infeasibility, not a search failure. Only MEASURED over-height
    # hard-blocks; an unknown or an upper-bound-over-limit is flagged, not refused.
    impossible = impossible + [
        f"{v['ref']} is {v['height_mm']:g} mm tall but the "
        f"{'back' if v['side'] == 'B' else 'front'} side has only "
        f"{v['limit_mm']:g} mm of enclosure clearance — move it to the other "
        f"side or use a shorter part" for v in z_too_tall]
    parts, seeded = ([], []) if impossible else \
        seed_placement(parts, region, specs, obstacles, grid_mm,
                       respect_positions=respect_positions)
    if seeded:
        say("seed    " + "; ".join(
            f"{m['ref']} -> {m['constraint']} ({m['distance_mm']} mm)"
            for m in seeded))
    model = None if impossible else PlacementModel(
        parts, region, specs, obstacles=obstacles, obstacle_sides=obstacle_sides,
        keepouts=keepouts, fixed_points=fixed_points, net_weights=net_weights,
        min_gap_mm=min_gap_mm, body_margin_mm=body_margin_mm,
        pad_clearances=pad_clearances, default_pad_clearance_mm=default_pad_clr,
        frozen_pads=frozen_pads)
    # how many placed footprints the collision model models from their REAL
    # F.CrtYd vs the pad-bbox proxy — so the note below tells the driver the
    # truth about this run's model instead of a stale blanket "proxy" claim.
    # WARN by name (finding C): a footprint with no F.CrtYd is placed on a
    # pad-bbox estimate of its body (+ body margin), so a relay/connector whose
    # case overhangs its pads is under-modelled. Silence here is how a board
    # reports "0 overlaps" while a fifth of its relays sit on their neighbours.
    no_crtyd_warning = ([{"kind": "no_courtyard", "refs": no_crtyd,
                          "detail": f"these footprints draw no F.CrtYd — their "
                          f"keep-out is a pad-bbox estimate + {body_margin_mm:g} "
                          f"mm body margin, NOT a real courtyard. A body that "
                          f"overhangs its pads (relays, connectors) is under-"
                          f"modelled; check them by eye and add courtyards where "
                          f"it matters."}] if no_crtyd else [])
    diagnostics = {
        "region": list(region),
        "area": int(area) if area is not None else None,
        "pitch_mm": pitch_mm,
        "layers": list(layers),
        "seed": seed,
        "movable": list(movable_refs),
        "movable_source": movable_source,
        "auto_fixed": list(auto_fixed),
        "auto_fixed_note": (
            "locked footprints inside the fence, frozen at their KiCad "
            "position as obstacles + fixed HPWL anchors (never moved)"),
        "unplaced_free_parts": list(unplaced),
        "unplaced_note": (
            "unlocked parts sitting off every board outline (a default-placed "
            "pile) — assigned to NO area, not auto-added; name them in "
            "components to place them, or roughly place them in KiCad first"),
        "frozen_obstacles": obstacle_refs,
        "hole_keepouts": [{"x": round(cx, 2), "y": round(cy, 2),
                           "radius_mm": round(r, 2)} for cx, cy, r in keepouts],
        "hole_clearance_mm": hole_clearance_mm,
        "min_gap_mm": min_gap_mm,
        "body_margin_mm": body_margin_mm,
        "density": density,
        "two_sided": {
            "back_parts": back_refs,
            "z_clearance_mm": {"F": z_clear["F"], "B": z_clear["B"]},
            "z_clearance_source": z_source,
            "z_clearance_verified": z_verified,
            "layout_direction": binfo["layout_direction"],
            "too_tall": z_too_tall,
            "unverified": z_unverified,
            "unchecked": z_unchecked,
            "fits": z_fits,
            "heights": z_per_part,
            "note": (
                ("component height checked against the enclosure z-clearance"
                 + (f"; {len(z_too_tall)} too tall (see infeasible_reason)"
                    if z_too_tall else "")
                 + (f"; {len(z_unverified)} unverified — measure or override"
                    if z_unverified else "")
                 + (f"; {len(z_unchecked)} on side(s) with NO stated clearance — "
                    f"NOT checked" if z_unchecked else "")
                 + (" — all clear" if z_verified else "")) if z_active
                else "back-side parts present but NO per-side z-clearance given "
                     "(board-info box or --z-back) — height fit is UNVERIFIED"
                if back_refs else None)},
        "pad_clearance": {
            "enabled": bool(pad_clearance),
            "default_mm": round(default_pad_clr, 4) if pad_clearance else None,
            "max_mm": (round(max(pad_clr_by_name.values()), 4)
                       if pad_clr_by_name else
                       (round(default_pad_clr, 4) if pad_clearance else None)),
            "frozen_pads": len(frozen_pads),
            "note": ("pad-to-pad copper clearance between DIFFERENT nets on "
                     "shared layers, per net class — courtyard non-overlap does "
                     "not imply it (finding §2)") if pad_clearance
                    else "disabled (--no-pad-clearance)"},
        "placement_verify": None,
        "no_courtyard_footprints": no_crtyd,
        "seeded": seeded,
        "boundary_nets": [asdict(t) for t in terminals],
        "fixed_inside_nets": sorted(fixed_inside),
        "courtyard_source": {"real_crtyd": n_real_court,
                             "pad_bbox_proxy": n_proxy_court},
        "courtyard_note": (
            f"courtyards: the real F.CrtYd/B.CrtYd where the footprint draws one "
            f"({n_real_court} of {len(components)} placed), else a pad-bbox "
            f"proxy ({n_proxy_court}); + {COURTYARD_MARGIN_MM} mm margin either "
            f"way. The proxy under-models THT bodies that overhang their pads — "
            f"run --list-courtyards to see which parts are proxied"),
        "geometry_warnings": _geometry_warnings(src_text, movable,
                                                obstacle_refs) + no_crtyd_warning,
        "infeasible_reason": None,
        "binding_constraint": None,
        "unrouted": [],
        "suggested_expansion": None,
    }
    diagnostics["preflight"] = impossible
    try:
        if impossible:
            raise RuntimeError("; ".join(impossible))
        pool = anneal_region(parts, region, specs, obstacles=obstacles,
                             obstacle_sides=obstacle_sides,
                             keepouts=keepouts, fixed_points=fixed_points,
                             net_weights=net_weights, grid_mm=grid_mm,
                             min_gap_mm=min_gap_mm, body_margin_mm=body_margin_mm,
                             pad_clearances=pad_clearances,
                             default_pad_clearance_mm=default_pad_clr,
                             frozen_pads=frozen_pads,
                             seed=seed, pool_size=k * 3, sweeps=sweeps)
    except RuntimeError as e:
        reason = str(e)
        # The greedy repair walk reports whichever problem it got stuck on last —
        # often an adjacency/fence failure even when the real blocker is the
        # tightened default clearance, which the walk never names. So ALWAYS list
        # the active clearance knobs on an infeasible run, so a driver whose
        # placement used to work reaches for the right lever (--min-gap /
        # --hole-clearance) instead of loosening the wrong constraint.
        extras = []
        if min_gap_mm > 0:
            extras.append(f"a {min_gap_mm:g} mm inter-courtyard clearance is "
                          f"enforced by default (--min-gap 0 to relax)")
        if keepouts:
            extras.append(f"{len(keepouts)} mounting-hole keep-out(s) active at "
                          f"{hole_clearance_mm:g} mm clearance (--hole-clearance)")
        if pad_clearance:
            extras.append("pad-to-pad copper clearance per net class is enforced "
                          "(--no-pad-clearance to relax) — an HV/creepage class "
                          "asks for far more than the courtyard margin")
        if extras:
            reason += " | also active: " + "; ".join(extras)
        diagnostics["infeasible_reason"] = reason
        # when courtyard density is the blocker, a concrete grow number beats the
        # boundary-pressure reading (which is meaningless with no placement yet).
        diagnostics["suggested_expansion"] = (
            _density_expansion(region, density["courtyard_mm2"])
            if density["verdict"] == "impossible" else None) or _suggest_expansion(
            region, [], terminals, pitch_mm,
            fallback="no feasible placement exists yet — the positions in the "
                     "reason above are POST-SCATTER (the tool moved parts into "
                     "the fence before failing), not your input; fix the reason "
                     "before reading anything into this direction")
        diagnostics["runtime_s"] = round(time.perf_counter() - t_start, 2)
        return RegionResult(candidates=[], diagnostics=diagnostics)

    diagnostics["anneal"] = {
        "elites": len(pool.elites), "sweeps": pool.sweeps,
        "proposals": pool.proposals, "accepted": pool.accepted,
        "rejected_infeasible": pool.rejected, "reheats": pool.reheats,
        "initial_energy": round(pool.initial_energy, 3),
        "repaired_start": pool.repaired,
        "grid_mm": pool.grid_mm,
        "courtyard_margin_mm": pool.courtyard_margin_mm,
        "best_energy": round(pool.elites[0].energy, 3) if pool.elites else None,
    }
    say(f"search  {len(pool.elites)} distinct placements "
        f"(energy {pool.elites[0].energy:.1f} .. {pool.elites[-1].energy:.1f}, "
        f"{pool.rejected} infeasible proposals rejected"
        f"{', repaired start' if pool.repaired else ''})")

    # 5 ── strip the fence's copper once; every finalist is moved off this copy
    stripped_text, n_inside, n_crossing = strip_tracks_in_rect(src_text, region)
    stripped = os.path.join(work_src, os.path.basename(board_path))
    with open(stripped, "w", encoding="utf-8") as f:
        f.write(stripped_text)
    diagnostics["stripped_tracks"] = {"inside_fence": n_inside,
                                      "crossing_fence": n_crossing}

    pro = project_file_for(board_path)
    widths = load_net_class_widths(pro, brd.nets) if pro else {}
    widths, capped = cap_track_widths(widths, brd.nets, pitch_mm)
    pro_clearance, pro_width = default_copper_rules(board_path)
    if clearance_mm is None:
        clearance_mm = pro_clearance
    diagnostics["net_classes"] = {
        "project": pro, "width_capped_nets": capped,
        "clearance_mm": clearance_mm, "track_width_mm": pro_width,
        "weights": {n: net_weights.get(n, 1.0) for n in sorted(net_names)}}

    # 6 ── route EVERY finalist; the router gets the final word
    scored = []
    for idx, elite in enumerate(pool.elites):
        base = os.path.join(work_elites, f"elite-{idx}.kicad_pcb")
        write_moved_copy(stripped, base, elite.placements)
        judged = _route_candidate(base, region, pitch_mm, layers, movable,
                                  region_net_codes, terminals, clearance_mm,
                                  pro_width, route_kwargs or {})
        checks, courts = _constraint_checks(model, elite.placements, region)
        violations = [c.reason for c in checks if not c.ok]
        score = (len(judged["failed"]) * 1e6 + len(violations) * 1e3
                 + judged["wirelength_mm"] + via_weight_mm * judged["vias"])
        scored.append({"elite": idx, "elite_obj": elite, "base": base,
                       "checks": checks, "courtyards": courts,
                       "violations": violations, "score": score, **judged})
        say(f"  elite {idx:<2} {judged['nets_ok']}/{judged['nets_total']} nets "
            f"| {judged['wirelength_mm']:7.2f} mm | {judged['vias']:3d} vias"
            f"{' | ' + str(len(violations)) + ' violations' if violations else ''}"
            f"{' | ' + str(len(judged['failed'])) + ' failed' if judged['failed'] else ''}")

    scored.sort(key=lambda s: rank_key(s, via_weight_mm))

    candidates = []
    for n, s in enumerate(scored[:k], start=1):
        board_copy = os.path.join(out_dir, f"cand-{n}.kicad_pcb")
        svg = os.path.join(out_dir, f"cand-{n}.svg")
        write_routed_copy(s["base"], board_copy, s["tracks"], s["vias_xy"],
                          brd.nets, widths=widths)
        render_svg(s["svg_view"], s["lat"], s["result"], svg,
                   title=f"cand-{n} ({s['nets_ok']}/{s['nets_total']} nets, "
                         f"{s['wirelength_mm']:.1f} mm, {s['vias']} vias)")
        candidates.append(Candidate(
            id=n, placements={r: tuple(v) for r, v in
                              s["elite_obj"].placements.items()},
            routed=f"{s['nets_ok']}/{s['nets_total']}",
            nets_ok=s["nets_ok"], nets_total=s["nets_total"],
            wirelength_mm=round(s["wirelength_mm"], 3), vias=s["vias"],
            constraint_violations=s["violations"], score=round(s["score"], 3),
            board_copy=board_copy, svg=svg,
            hpwl_mm=round(s["elite_obj"].hpwl_mm, 3),
            failed=s["failed"], elite_rank=s["elite"],
            iterations=s["result"].iterations))

    # 6b ── write-time verify (finding §5): re-check every SHIPPED candidate's
    # pad-copper clearance over its WHOLE moved board, not just the region model.
    # Elites are pad-clean by construction, so a hit here is copper OUTSIDE the
    # search's obstacle set (a frozen part the fence did not intrude) — surface
    # it loudly, never ship a confidently-wrong "0 overlaps". Only pairs
    # involving a MOVED part are this run's responsibility; a pre-existing tight
    # spot elsewhere on the board is not, and is filtered out.
    placement_verify = {"pad_clearance_checked": bool(pad_clearance),
                        "clean": True, "candidates_with_shorts": 0,
                        "violations": []}
    if pad_clearance:
        for s in scored[:k]:
            cand, rop = s["board"], s["ref_of_pad"]
            pads = [(rop[i], p.net_name, frozenset(p.layers), pad_world_corners(p))
                    for i, p in enumerate(cand.pads)
                    if p.layers and _pad_has_copper(p)]
            hits = [{"a": a, "net_a": na, "b": b, "net_b": nb,
                     "gap_mm": round(g, 4), "clearance_mm": need}
                    for a, na, b, nb, g, need in
                    pad_clearance_report(pads, pad_clr_by_name, default_pad_clr)
                    if a in movable or b in movable]
            if hits:
                placement_verify["clean"] = False
                placement_verify["candidates_with_shorts"] += 1
                placement_verify["violations"].append(
                    {"elite": s["elite"], "pairs": hits})
        if not placement_verify["clean"]:
            say(f"WARN    pad-clearance verify found copper shorts in "
                f"{placement_verify['candidates_with_shorts']} shipped "
                f"candidate(s) — see diagnostics.placement_verify; these are "
                f"pairs the search's obstacle set did not cover, do NOT apply "
                f"without inspecting")
    diagnostics["placement_verify"] = placement_verify

    # 7 ── diagnostics that are true (spec's whole feedback loop)
    fully = [s for s in scored[:k] if not s["failed"]]
    if not scored:
        diagnostics["infeasible_reason"] = (
            "the placement search produced no distinct feasible placements")
    elif not fully:
        worst = scored[0]
        diagnostics["infeasible_reason"] = (
            f"no candidate routed fully — best was elite {worst['elite']} with "
            f"{len(worst['failed'])} failed connection(s): "
            + "; ".join(f"{n}: {r}" for n, r in worst["failed"][:4]))
    diagnostics["unrouted"] = _blame_unrouted(scored[:k], region)
    diagnostics["binding_constraint"] = _binding_constraint(
        scored[:k], region, model.edge_tol_mm)
    diagnostics["suggested_expansion"] = _suggest_expansion(
        region, scored[:k], terminals, pitch_mm)
    diagnostics["candidates_routed_fully"] = f"{len(fully)}/{len(candidates)}"
    diagnostics["finalists_routed"] = len(scored)
    diagnostics["lattice"] = scored[0]["lattice_stats"] if scored else None
    diagnostics["runtime_s"] = round(time.perf_counter() - t_start, 2)
    diagnostics["work_dir"] = None if not keep_work else work

    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)

    return RegionResult(candidates=candidates, diagnostics=diagnostics)


def rank_key(s, via_weight_mm=VIA_WEIGHT_MM):
    """The strict ranking of REGION_SOLVER.md section 3, as a sort key:
    failures >> constraint_violations >> wirelength + via_weight*vias, with
    the elite index last so ties never depend on dict or thread order.

    Lexicographic, not weighted-sum, on purpose: a placement that does not
    route must NEVER outrank one that does, no matter how short its copper
    (design rule 5). Weighted sums let a beautiful failure win.
    """
    return (len(s["failed"]), len(s["violations"]),
            s["wirelength_mm"] + via_weight_mm * s["vias"], s["elite"])


# ── routing one finalist ─────────────────────────────────────────────────────

def _route_candidate(base_path, region, pitch_mm, layers, movable,
                     region_net_codes, terminals, clearance_mm,
                     track_width_mm, route_kwargs):
    """Route one moved board copy on a fence-sized lattice.

    The board is re-loaded from the moved copy rather than transformed in
    memory: the geometry that gets routed is then provably the geometry that
    gets written, with no second implementation of KiCad's rotation rules.
    """
    cand = load_board(base_path)
    ref_of_pad = _pad_owner_refs(base_path, cand)
    margin = _window_margin(pitch_mm)
    window = _grow(region, margin)
    wrect = (window[0], window[1], window[0] + window[2], window[1] + window[3])

    window_pads = [p for p in cand.pads if _rects_overlap(_pad_bbox(p), wrect)]
    lat_view = _view(cand, base_path, (window[0], window[1]),
                     (window[2], window[3]), window_pads)
    lat, pad_nodes, node_owner = lattice_for_board(lat_view, pitch_mm,
                                                   layer_names=layers)
    clr_view = _view(cand, base_path, cand.origin_mm, cand.size_mm, window_pads)
    extra_allow = pad_overlap_allowances(clr_view, lat)
    # clearance/width come from the SOURCE project (resolved once by the
    # caller): the working copy has no sibling .kicad_pro, and silently
    # falling back to stock numbers would route to rules nobody chose.
    clearance = None
    if clearance_mm > 0:
        clearance = clearance_map(clr_view, lat, node_owner, pad_nodes,
                                  clearance_mm=clearance_mm,
                                  track_width_mm=track_width_mm)

    # real terminals: region-net pads that are INSIDE the fence (movable parts
    # and frozen in-fence pads). Pads outside the fence stay obstacles only —
    # the pseudo-pad speaks for the outside world (see the module docstring).
    inside_pads = []
    for i, p in enumerate(cand.pads):
        if p.net_code not in region_net_codes:
            continue
        if ref_of_pad[i] in movable or _in_rect(p.x_mm, p.y_mm, region):
            inside_pads.append(p)
    term_view = _view(cand, base_path, cand.origin_mm, cand.size_mm,
                      inside_pads)
    net_pads = net_pads_for_board(term_view, lat, node_owner)

    walled_off = set()
    for t in terminals:
        nodes = _terminal_nodes(lat, t.x_mm, t.y_mm, t.net_code, node_owner,
                                clearance)
        if nodes:
            net_pads.setdefault(t.net_code, []).append(
                (tuple(nodes), (t.x_mm, t.y_mm)))
        else:
            walled_off.add(t.net_code)

    net_pads = {c: v for c, v in net_pads.items() if c in region_net_codes}
    routable = {c for c, v in net_pads.items() if len(v) >= 2} | walled_off
    res = route_lattice(lat, net_pads, node_owner, extra_allow=extra_allow,
                        clearance=clearance, **route_kwargs)

    tracks, vias_xy = (res.tracks, res.vias) if res.tracks is not None \
        else paths_to_tracks(lat, res.net_paths)
    failed_codes = {c for c, _ in res.failed} | walled_off
    failed = [(cand.nets.get(c, str(c)), r) for c, r in res.failed]
    failed += [(cand.nets.get(c, str(c)),
                "boundary terminal has no free lattice node — the fence edge "
                "is solid copper where this net must leave")
               for c in sorted(walled_off)]
    nets_total = len(routable)
    nets_ok = len(routable - failed_codes)
    return {
        "result": res, "lat": lat, "tracks": tracks, "vias_xy": vias_xy,
        "svg_view": _view(cand, base_path, (region[0], region[1]),
                          (region[2], region[3]), window_pads),
        "wirelength_mm": float(res.wirelength_mm), "vias": int(res.via_count),
        "nets_ok": max(nets_ok, 0), "nets_total": nets_total, "failed": failed,
        "board": cand, "window_pads": window_pads, "ref_of_pad": ref_of_pad,
        "lattice_stats": {
            "nodes": lat.W * lat.H * lat.L, "W": lat.W, "H": lat.H, "L": lat.L,
            "window_mm": [round(v, 3) for v in window],
            "window_pads": len(window_pads),
            "clearance_inflate_mm": round(clearance.inflate_mm, 4)
            if clearance else None,
            "degraded_pad_pairs": clearance.degraded_pairs if clearance else 0,
            "pad_snap_conflicts": len(res.conflicts)},
    }


# ── scoring helpers ──────────────────────────────────────────────────────────

def _constraint_checks(model, placements, region):
    """(checks, courtyards) — re-judging a router-scored candidate through
    the same PlacementModel the search used, so a candidate can never ship
    with a violation the search claimed it had rejected."""
    states = [tuple(placements[r]) for r in model.refs]
    courts = model.courtyards(states)
    return evaluate_constraints(model.constraints, model.placements(states),
                                courts, rect=region, home=model.home,
                                edge_tol_mm=model.edge_tol_mm), courts


def _slack(constraint, placements, courtyards, region, edge_tol_mm):
    """Continuous headroom in mm for the constraints that have one; None for
    the yes/no forms (fixed, orientation_set, keepout)."""
    c = constraint
    if c.kind in ("adjacency_max_distance", "min_distance"):
        ax, ay, _ = placements[c.ref_a]
        bx, by, _ = placements[c.ref_b]
        d = math.hypot(ax - bx, ay - by)
        return (c.mm - d) if c.kind == "adjacency_max_distance" else (d - c.mm)
    if c.kind == "edge":
        x0, y0, x1, y1 = courtyards[c.ref]
        rx, ry, rw, rh = region
        gap = {"left": x0 - rx, "right": (rx + rw) - x1,
               "top": y0 - ry, "bottom": (ry + rh) - y1}[c.side]
        return edge_tol_mm - gap
    return None


def _binding_constraint(shipped, region, edge_tol_mm=1.0):
    """Which constraint was TIGHT across the shipped candidates — the one the
    caller should relax first if it wants a better layout. Violations outrank
    tightness; among satisfied constraints the smallest headroom wins."""
    agg = {}
    for s in shipped:
        for chk in s["checks"]:
            key = str(chk.constraint)
            slot = agg.setdefault(key, {"constraint": key, "violated": 0,
                                        "min_slack_mm": None, "n": 0})
            slot["n"] += 1
            if not chk.ok:
                slot["violated"] += 1
            sl = _slack(chk.constraint, s["elite_obj"].placements,
                        s["courtyards"], region, edge_tol_mm)
            if sl is not None:
                slot["min_slack_mm"] = sl if slot["min_slack_mm"] is None \
                    else min(slot["min_slack_mm"], sl)
    if not agg:
        return None
    ranked = sorted(agg.values(), key=lambda s: (
        -s["violated"],
        s["min_slack_mm"] if s["min_slack_mm"] is not None else math.inf,
        s["constraint"]))
    best = ranked[0]
    if best["violated"] == 0 and best["min_slack_mm"] is None:
        return None      # only yes/no constraints, all satisfied: nothing tight
    out = dict(best)
    if out["min_slack_mm"] is not None:
        out["min_slack_mm"] = round(out["min_slack_mm"], 3)
    return out


def _blame_unrouted(shipped, region):
    """[(net, blocking_ref_or_edge)] — for every failed connection in the
    shipped candidates, the nearest FOREIGN pad's footprint, or the fence side
    the net was trying to leave by. A guess, and labelled as one: 'nearest
    foreign copper' is what a human reads off the picture too."""
    seen, out = set(), []
    for s in shipped:
        cand, ref_of_pad = s["board"], s["ref_of_pad"]
        for name, reason in s["failed"]:
            if name in seen:
                continue
            seen.add(name)
            mine = [p for p in cand.pads
                    if p.net_name == name and _in_rect(p.x_mm, p.y_mm, region,
                                                       tol=2.0)]
            blocker, best = None, math.inf
            for i, p in enumerate(cand.pads):
                if p.net_name == name or p.net_code <= 0:
                    continue
                for q in mine:
                    d = math.hypot(p.x_mm - q.x_mm, p.y_mm - q.y_mm)
                    if d < best:
                        best, blocker = d, ref_of_pad[i]
            if blocker is not None and best <= 5.0:
                blocker = f"{blocker} (nearest foreign copper, {best:.2f} mm)"
            elif mine:
                q = min(mine, key=lambda p: math.dist(
                    (p.x_mm, p.y_mm), _clamp_to_rect(p.x_mm, p.y_mm, region)))
                blocker = f"fence:{_side_of(q.x_mm, q.y_mm, region)}"
            else:
                blocker = "congestion"
            out.append([name, blocker, reason])
    return out


def _suggest_expansion(region, shipped, terminals, pitch_mm, fallback=None):
    """Which way the fence wants to grow, from boundary pressure: movable
    courtyards pressed against a side, plus the sides the boundary nets leave
    by. All four numbers are reported — the suggestion is a reading of them,
    not a black box."""
    rx, ry, rw, rh = region
    band = max(2.0 * pitch_mm, 0.5)
    pressure = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}
    for t in terminals:
        pressure[t.side] += 1.0
    for s in shipped:
        for x0, y0, x1, y1 in s.get("courtyards", {}).values():
            if x0 - rx <= band:
                pressure["left"] += 1.0
            if (rx + rw) - x1 <= band:
                pressure["right"] += 1.0
            if y0 - ry <= band:
                pressure["top"] += 1.0
            if (ry + rh) - y1 <= band:
                pressure["bottom"] += 1.0
    total = sum(pressure.values())
    if total <= 0:
        return {"direction": None, "mm": 0.0, "pressure": pressure,
                "reason": fallback or "no boundary pressure measured"}
    side = max(sorted(pressure), key=lambda s: pressure[s])
    span = rw if side in ("left", "right") else rh
    grow = max(2.0, round(0.25 * span / pitch_mm) * pitch_mm)
    return {"direction": side, "mm": grow, "pressure": pressure,
            "reason": fallback or
            f"{pressure[side]:.0f} of {total:.0f} pressure counts are on the "
            f"{side} side (boundary terminals + courtyards within "
            f"{band:.2f} mm of the fence)"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_summary(result, out_dir):
    d = result.diagnostics
    print(f"region      : {d['region'][0]:.2f},{d['region'][1]:.2f} "
          f"{d['region'][2]:.2f}x{d['region'][3]:.2f} mm  "
          + (f"area {d['area']}  " if d.get('area') is not None else "")
          + f"pitch {d['pitch_mm']} mm  layers {','.join(d['layers'])}  "
          f"seed {d['seed']}")
    print(f"movable     : {len(d['movable'])} ({d.get('movable_source', '')})"
          + (f" — {', '.join(d['movable'])}" if d['movable'] else ""))
    if d.get('auto_fixed'):
        print(f"auto-fixed  : {len(d['auto_fixed'])} locked part(s) held at "
              f"their KiCad position — {', '.join(d['auto_fixed'])}")
    if d.get('unplaced_free_parts'):
        up = d['unplaced_free_parts']
        print(f"unplaced    : {len(up)} unlocked part(s) off every board "
              f"outline, NOT auto-added — {', '.join(up[:12])}"
              + (" ..." if len(up) > 12 else ""))
    print(f"frozen      : {len(d['frozen_obstacles'])} intruding courtyard(s)"
          + (f" — {', '.join(d['frozen_obstacles'])}"
             if d['frozen_obstacles'] else ""))
    print(f"boundary    : {len(d['boundary_nets'])} net(s) cross the fence"
          + (" — " + ", ".join(f"{t['net_name']}->{t['side']}"
                               for t in d['boundary_nets'][:6])
             if d['boundary_nets'] else ""))
    if len(d['boundary_nets']) > 6:
        print(f"              (+{len(d['boundary_nets']) - 6} more)")
    dn = d.get("density")
    if dn:
        flag = {"impossible": "  IMPOSSIBLE as one fence",
                "tight": "  tight — expect congestion"}.get(dn["verdict"], "")
        print(f"density     : {dn['utilization']:.0%} "
              f"({dn['courtyard_mm2']:.0f} of {dn['fence_mm2']:.0f} mm2"
              + (f", {dn['blocked_mm2']:.0f} under obstacles"
                 if dn['blocked_mm2'] > 0.5 else "") + ")" + flag)
    ts = d.get("two_sided") or {}
    zc = ts.get("z_clearance_mm") or {}
    if ts.get("back_parts") or zc.get("F") is not None or zc.get("B") is not None:
        zf = zc.get("F"); zb = zc.get("B")
        print(f"z-clearance : front {zf if zf is not None else '—'} / back "
              f"{zb if zb is not None else '—'} mm"
              + (f" (from {ts['z_clearance_source']})"
                 if ts.get("z_clearance_source") else "")
              + f" | {len(ts.get('back_parts', []))} back-side part(s)")
        if ts.get("too_tall"):
            for v in ts["too_tall"][:6]:
                print(f"      TOO TALL  {v['ref']} {v['height_mm']:g} mm > "
                      f"{v['limit_mm']:g} mm ({'back' if v['side'] == 'B' else 'front'})")
        if ts.get("unverified"):
            print(f"      unverified: "
                  + ", ".join(v["ref"] for v in ts["unverified"][:12]))
        back_unchecked = [v["ref"] for v in ts.get("unchecked", [])
                          if v["side"] == "B"]
        if back_unchecked:
            print(f"      NOTE      {len(back_unchecked)} back-side part(s) with "
                  f"NO back z-clearance — height fit UNVERIFIED (set the board-"
                  f"info box or --z-back)")
    print()
    if not result.candidates:
        print("candidates  : NONE")
    else:
        print(f"candidates  : {len(result.candidates)} ranked "
              f"(failures >> violations >> wirelength + "
              f"{VIA_WEIGHT_MM}*vias)")
        print("  #  routed    length    vias  viol  score      board")
        for c in result.candidates:
            print(f"  {c.id}  {c.routed:>7}  {c.wirelength_mm:8.2f}  "
                  f"{c.vias:4d}  {len(c.constraint_violations):4d}  "
                  f"{c.score:9.2f}  {os.path.basename(c.board_copy)}")
        for c in result.candidates:
            for v in c.constraint_violations:
                print(f"       cand-{c.id} violation: {v}")
    print()
    print("diagnostics")
    print(f"  routed fully    : {d.get('candidates_routed_fully', '0/0')} "
          f"shipped ({d.get('finalists_routed', 0)} finalists routed)")
    if d.get("infeasible_reason"):
        print(f"  infeasible      : {d['infeasible_reason']}")
    bc = d.get("binding_constraint")
    print(f"  binding         : "
          + ("none tight" if not bc else
             f"{bc['constraint']} — {bc['violated']}/{bc['n']} violated"
             + (f", min slack {bc['min_slack_mm']} mm"
                if bc.get('min_slack_mm') is not None else "")))
    if d.get("unrouted"):
        print(f"  unrouted        : {len(d['unrouted'])}")
        for net, blocker, reason in d["unrouted"][:8]:
            print(f"      {net} — blocked by {blocker} — {reason}")
    else:
        print("  unrouted        : none")
    se = d.get("suggested_expansion") or {}
    print(f"  expand          : "
          + (f"{se.get('direction')} by {se.get('mm')} mm — {se.get('reason')}"
             if se.get("direction") else se.get("reason", "n/a")))
    an = d.get("anneal")
    if an:
        print(f"  search          : {an['elites']} elites from "
              f"{an['proposals']} proposals ({an['accepted']} accepted, "
              f"{an['rejected_infeasible']} infeasible), {an['reheats']} "
              f"reheat(s), initial energy {an['initial_energy']}"
              + (", START REPAIRED" if an["repaired_start"] else ""))
        cs = d.get("courtyard_source") or {}
        print(f"  courtyards      : {cs.get('real_crtyd', 0)} real F.CrtYd, "
              f"{cs.get('pad_bbox_proxy', 0)} pad-bbox proxy, "
              f"+{an['courtyard_margin_mm']} mm margin — {d['courtyard_note']}")
    lt = d.get("lattice")
    if lt:
        print(f"  lattice         : {lt['W']}x{lt['H']}x{lt['L']} = "
              f"{lt['nodes']} nodes over {lt['window_mm'][2]:.1f}x"
              f"{lt['window_mm'][3]:.1f} mm, {lt['window_pads']} pads in "
              f"window, clearance inflate {lt['clearance_inflate_mm']} mm, "
              f"{lt['pad_snap_conflicts']} pad-snap conflict(s)")
    pv = d.get("placement_verify") or {}
    if pv.get("pad_clearance_checked"):
        if pv.get("clean"):
            print("  pad clearance   : verified clean on every shipped candidate "
                  "(whole-board pad-copper check)")
        else:
            print(f"  pad clearance   : SHORTS in "
                  f"{pv.get('candidates_with_shorts')} candidate(s) — inspect "
                  f"before applying")
            for v in pv.get("violations", [])[:3]:
                for pr in v["pairs"][:4]:
                    print(f"      elite {v['elite']}: {pr['a']}({pr['net_a']}) <-> "
                          f"{pr['b']}({pr['net_b']}) — {pr['gap_mm']} mm < "
                          f"{pr['clearance_mm']} mm")
    for w in d.get("geometry_warnings") or []:
        print(f"  WARNING         : {w['kind']} on {', '.join(w['refs'])}")
        print(f"                    {w['detail']}")
    st = d.get("stripped_tracks")
    if st:
        print(f"  stripped copper : {st['inside_fence']} inside the fence, "
              f"{st['crossing_fence']} crossing it (v1 solves a region as if "
              f"it started unrouted)")
    print(f"  runtime         : {d.get('runtime_s')} s")
    print(f"  out             : {out_dir}")


def _list_regions(board_path):
    """Print the board's outline regions (the --area indices) and each net that
    spans more than one, then return 0 — the same map pathfinder --list-regions
    prints, so the design thread picks an --area N from one place."""
    from lattice import board_outline_regions, board_outline_regions_all
    from pathfinder import cross_region_nets, pad_region_index
    brd = load_board(board_path)
    regions = board_outline_regions(brd)
    dropped = len(board_outline_regions_all(brd)) - len(regions)
    print(f"board       : {os.path.basename(board_path)}")
    print(f"areas       : {len(regions)} disjoint Edge.Cuts outline(s) "
          f"(use --area N)")
    if dropped:
        print(f"  note      : {dropped} degenerate outline(s) (zero width/height) "
              f"dropped — likely a stray Edge.Cuts line; check your board if you "
              f"expected {len(regions) + dropped} areas")
    for i, r in enumerate(regions):
        n = sum(1 for p in brd.pads if pad_region_index([r], p) is not None)
        print(f"  area {i}: origin ({r.origin_mm[0]:.2f}, {r.origin_mm[1]:.2f}) "
              f"size {r.size_mm[0]:.2f} x {r.size_mm[1]:.2f} mm | "
              f"{r.shapes} outline graphic(s) | {n} pads")
    spanning = cross_region_nets(brd, regions)
    print(f"spanning    : {len(spanning)} net(s) with pads on more than one area")
    for code, idx in sorted(spanning.items(),
                            key=lambda kv: str(brd.nets.get(kv[0]))):
        print(f"  {brd.nets.get(code, code)}: areas {idx}")
    return 0


def _list_courtyards(board_path, refs=None):
    """Print each footprint's courtyard — ref, w x h, area, and SOURCE (real
    F.CrtYd/B.CrtYd graphics vs the pad-bbox proxy fallback). Finding #5's ask:
    it makes seeding, density preflight, band sizing and partition sanity a
    one-liner for any driver, and removes the whole class of external
    courtyard-re-parsing bugs the field report hit.

    Dimensions are the courtyard's own extent (no placement margin — the placer
    adds COURTYARD_MARGIN_MM on top); the [proxy] flag names exactly the parts
    whose body the tool is guessing from pads, so a driver knows which numbers
    to distrust for overhanging THT before leaning on them."""
    from place import parts_from_board, _local_geometry
    parts = parts_from_board(board_path)
    if refs is None:
        keys = sorted(parts)                     # whole board
    else:
        # Resolve each requested ref to its uref(s): a plain designator that is
        # DUPLICATED on the board (5755 -> 5755#1..#3) lists every instance —
        # the same addressing the placement --components path uses, so a driver
        # can reuse one ref list across both. A non-None but empty selection
        # (e.g. --components ' , ,') lists nothing, never the whole board.
        keys = []
        for k in refs:
            matches = [u for u in sorted(parts)
                       if u == k or u.split("#")[0] == k]
            keys.extend(matches or [k])          # keep k to report it missing
        keys = list(dict.fromkeys(keys))         # dedup, preserve order
    print(f"board       : {os.path.basename(board_path)}")
    rows, n_real, n_named = [], 0, 0
    for k in keys:
        p = parts.get(k)
        if p is None:
            rows.append(f"  {k:<12} (not on this board)")
            continue
        _, local = _local_geometry(p, 0.0)
        w, h = local[2] - local[0], local[3] - local[1]
        real = p.local_courtyard is not None
        n_real += real
        n_named += 1
        rows.append(f"  {k:<12} {w:6.2f} x {h:6.2f} mm  {w * h:8.2f} mm2"
                    f"{'' if real else '  [proxy]'}")
    print(f"courtyards  : {n_named} footprint(s) — w x h (mm), no margin; "
          f"[proxy] = pad-bbox guess, not real F.CrtYd")
    for row in rows:
        print(row)
    print(f"summary     : {n_real}/{n_named} from real courtyard graphics, "
          f"{n_named - n_real} pad-bbox proxy")
    return 0


def _list_heights(board_path, refs=None, overrides_path=None):
    """Print each footprint's resolved above-board height and its SOURCE — the
    z-clearance inspector (finding §3). Heights come from a designer override
    (by fpid or ref), else the footprint's own descr/property, else a
    conservative family upper bound, else UNKNOWN. This makes the two-sided
    precondition visible: run it before relying on a back-side placement to see
    exactly which parts have a real height and which the tool cannot vouch for.

    [measured] = the footprint stated it or you overrode it; [family-max] = a
    conservative upper bound for the package family (safe to pass a part on, but
    not a measured value); [UNKNOWN] = flagged, never assumed to fit."""
    import heights as H
    from place import parts_from_board
    overrides = H.load_overrides(overrides_path) if overrides_path else {}
    parts = parts_from_board(board_path)
    if refs is None:
        keys = sorted(parts)
    else:
        keys = []
        for k in refs:
            m = [u for u in sorted(parts) if u == k or u.split("#")[0] == k]
            keys.extend(m or [k])
        keys = list(dict.fromkeys(keys))
    print(f"board       : {os.path.basename(board_path)}"
          + (f"  (+overrides {os.path.basename(overrides_path)})"
             if overrides_path else ""))
    n_meas = n_fam = n_unk = 0
    rows = []
    for k in keys:
        p = parts.get(k)
        if p is None:
            rows.append(f"  {k:<10} (not on this board)")
            continue
        h, src = H.resolve(k, p.fpid, p.height_mm, overrides)
        if src in ("override:ref", "override:fpid", "footprint"):
            tag = "measured"
            n_meas += 1
        elif src == "family-max":
            tag = "family-max"
            n_fam += 1
        else:
            tag = "UNKNOWN"
            n_unk += 1
        hs = f"{h:5.1f} mm" if h is not None else "   ?   "
        rows.append(f"  {k:<10} {p.side}  {hs}  [{tag:<10}] {(p.fpid or '')[:40]}")
    print(f"heights     : {len(keys)} footprint(s) — above-board mm and source; "
          f"[family-max] = conservative upper bound, [UNKNOWN] = flagged")
    for row in rows:
        print(row)
    print(f"summary     : {n_meas} measured/override, {n_fam} family-max "
          f"(upper bound), {n_unk} UNKNOWN (flagged — give a height override or "
          f"add height=Nmm to the footprint)")
    return 0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Place and route one fenced region/area of a KiCad board")
    ap.add_argument("board")
    ap.add_argument("--components", default=None,
                    help="comma-separated refs allowed to move (ref#N for "
                         "duplicated designators). THE primary path — name one "
                         "circuit-function group per area. Omit with --area to "
                         "default to every unlocked footprint already inside it")
    ap.add_argument("--region", default=None, metavar="x,y,w,h",
                    help="the fence, mm, board coordinates")
    ap.add_argument("--area", type=int, default=None, metavar="N",
                    help="fence on board outline region N instead of --region "
                         "(see --list-regions)")
    ap.add_argument("--list-regions", action="store_true",
                    help="print the board's outline areas (the --area indices) "
                         "and the nets that span them, then exit")
    ap.add_argument("--list-connections", default=None, metavar="R1,R2,...",
                    help="print the net edges among these refs (advisory DATA "
                         "for partitioning — the tool never groups), then exit")
    ap.add_argument("--list-courtyards", action="store_true",
                    help="print every footprint's courtyard (ref, w x h, area, "
                         "and whether it is the REAL F.CrtYd or a pad-bbox "
                         "proxy), then exit. Optionally filter with --components")
    ap.add_argument("--list-heights", action="store_true",
                    help="print every footprint's resolved above-board HEIGHT and "
                         "its source (measured / conservative family-max / "
                         "UNKNOWN), then exit — the two-sided z-clearance "
                         "inspector. Filter with --components; feed --heights")
    ap.add_argument("--heights", default=None, metavar="FILE",
                    help="JSON height overrides (mm) by fpid \"LIB:NAME\" or ref; "
                         "the precise, designer-owned height source that a "
                         "BOM-enrichment step would emit")
    ap.add_argument("--constraint", action="append", default=[],
                    help="repeatable, e.g. \"min_distance(R4,C8,5)\"")
    ap.add_argument("--no-auto-fix-locked", action="store_true",
                    help="do NOT auto-freeze locked footprints inside the fence "
                         "(default is to hold them fixed; A/B measurement only)")
    ap.add_argument("--respect-positions", action="store_true",
                    help="treat the components' input positions as a deliberate "
                         "arrangement (a shelf-pack or stage.py-staged layout): "
                         "rigidly translate the off-fence group into the fence "
                         "preserving relative offsets instead of scattering it "
                         "onto a fresh grid, then anneal from there. In-fence "
                         "parts stay put; seed clear of locked parts and "
                         "pre-satisfy any adjacency_max_distance constraints")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=None, help="output directory")
    ap.add_argument("--pitch", type=float, default=0.5)
    ap.add_argument("--layers", default="F.Cu,B.Cu")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--sweeps", type=int, default=200)
    ap.add_argument("--hole-clearance", type=float, default=3.0,
                    metavar="MM", help="screw-head clearance kept clear around "
                    "each board mounting hole, hole-edge outward (default 3.0 "
                    "for M3); courtyards are hard-rejected from these keep-outs. "
                    "0 keeps only the drilled hole itself clear, no head margin")
    ap.add_argument("--min-gap", type=float, default=0.25, metavar="MM",
                    help="minimum clearance GAP required between courtyards "
                    "(default 0.25); touching courtyards = 0 clearance = a DRC "
                    "crash. 0 reverts to plain no-overlap")
    ap.add_argument("--body-margin", type=float, default=1.0, metavar="MM",
                    help="extra keep-out margin for footprints with NO F.CrtYd "
                    "(default 1.0), where the pad bbox under-models the body "
                    "(relays, connectors); parts WITH a real courtyard are "
                    "untouched. 0 uses the raw pad bbox")
    ap.add_argument("--no-pad-clearance", action="store_true",
                    help="disable pad-to-pad copper clearance (on by default, "
                    "per net class): courtyard non-overlap does not imply it, so "
                    "a fat pad or an HV creepage rule shorts with clear bodies. "
                    "Off is A/B measurement only — it ships copper DRC will fail")
    ap.add_argument("--z-front", type=float, default=None, metavar="MM",
                    help="available component height on the FRONT before the "
                    "enclosure (finding §3). A front part taller than this is "
                    "refused. Overrides the board-info box; omit to use the box")
    ap.add_argument("--z-back", type=float, default=None, metavar="MM",
                    help="available component height on the BACK before the "
                    "chassis — the tight one on a two-sided board. A back part "
                    "taller than this is refused. Overrides the board-info box")
    ap.add_argument("--via-weight", type=float, default=VIA_WEIGHT_MM,
                    help="mm of track a via is worth when ranking")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep the per-finalist intermediate board copies")
    ap.add_argument("--json", action="store_true",
                    help="emit the machine-readable result on stdout")
    args = ap.parse_args(argv)

    if args.list_regions:
        return _list_regions(args.board)
    if args.list_courtyards:
        refs = None if args.components is None else \
            [c.strip() for c in args.components.split(",") if c.strip()]
        return _list_courtyards(args.board, refs)
    if args.list_heights:
        refs = None if args.components is None else \
            [c.strip() for c in args.components.split(",") if c.strip()]
        return _list_heights(args.board, refs, args.heights)
    if args.list_connections:
        refs = [c.strip() for c in args.list_connections.split(",") if c.strip()]
        adj = net_adjacency(args.board, refs)
        print(f"connections : net edges among {len(refs)} ref(s) "
              f"(advisory data — the tool does not group)")
        for r in refs:
            print(f"  {r:<10} -> {', '.join(adj.get(r, [])) or '(none)'}")
        return 0

    if (args.region is None) == (args.area is None):
        ap.error("pass exactly one of --region x,y,w,h or --area N")
    region = None
    if args.region is not None:
        try:
            region = tuple(float(v) for v in args.region.split(","))
            if len(region) != 4:
                raise ValueError
        except ValueError:
            ap.error(f"--region must be x,y,w,h in mm (got {args.region!r})")
    components = None if args.components is None else \
        [c.strip() for c in args.components.split(",") if c.strip()]
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]
    slug = os.path.splitext(os.path.basename(args.board))[0].lower()
    suffix = f"area{args.area}" if args.area is not None else slug
    out_dir = args.out or os.path.join("out", f"region-{suffix}")

    try:
        result = optimize_region(
            args.board, components, region, constraints=args.constraint,
            k=args.k, pitch_mm=args.pitch, layers=layers, out_dir=out_dir,
            seed=args.seed, sweeps=args.sweeps, via_weight_mm=args.via_weight,
            keep_work=args.keep_work, area=args.area,
            auto_fix_locked=not args.no_auto_fix_locked,
            respect_positions=args.respect_positions,
            hole_clearance_mm=args.hole_clearance, min_gap_mm=args.min_gap,
            body_margin_mm=args.body_margin,
            pad_clearance=not args.no_pad_clearance,
            z_front_mm=args.z_front, z_back_mm=args.z_back,
            height_overrides=(_heights.load_overrides(args.heights)
                              if args.heights else None),
            progress=None if args.json else (lambda m: print(m, flush=True)))
    except ValueError as e:
        ap.error(str(e))

    if args.json:
        print(json.dumps(result.as_dict(), indent=2, default=str))
    else:
        print()
        _print_summary(result, out_dir)
    return 0 if result.candidates else 1


if __name__ == "__main__":
    raise SystemExit(main())
