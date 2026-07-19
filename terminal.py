"""L2: terminal-served nets — connect a net by WIRE, not by on-board copper.

A normal net's pads are joined by one minimum spanning tree over the whole
board (pathfinder.build_connections -> _mst_edges). For a high-voltage supply
like a tube amp's B+ that is exactly the wrong topology: 35 pads spanning
300 mm produce a huge on-board copper tree that is both the router's worst
congestion and, at HV creepage clearance, frequently unroutable. In a real
amp B+ does not arrive as one long trace — it arrives by WIRE, dropped in at
a few star points and distributed locally.

This module makes that a first-class connectivity model. A net declared
TERMINAL-SERVED is:

  1. spatially CLUSTERED — its pads grouped into a few local groups;
  2. given one physical TERMINAL per cluster — a via with a drill large enough
     to solder a flying lead through, placed at the cluster centroid snapped to
     the nearest FREE, legal lattice node;
  3. routed pad -> nearest-terminal ONLY. The terminals of one net are joined
     OFF the board, by the wire the builder solders on, so no on-board copper
     runs terminal-to-terminal. The off-board wire IS the "virtual plane."

The net is fully routed when every pad reaches a terminal. This generalises
region.py's pseudo-pad (a fixed off-board landing several pads reach) from a
region boundary to a whole net; `_snap_terminal` is region._terminal_nodes
grown a pad-core clearance check.

Nothing here decides WHICH nets are terminal-served — that is a design
decision the caller makes (a net-class flag or an explicit list), never a
guess. Default: no net is terminal-served and the router is unchanged.
"""
import math
from dataclasses import dataclass, field

from geometry import halo_offsets


# Default cluster threshold: two pads share a terminal when both sit within
# this Manhattan distance of their group's centroid. Justification: a terminal
# is a single solder point serving its pads with SHORT on-board traces, so the
# threshold is the longest such trace we want to run before dropping another
# wire. A few times the pitch (~mm) would give a terminal per pad — many solder
# points, no collapse; the whole board in one cluster gives back the board-
# spanning tree we are trying to kill. 25 mm is the hand-wired-star middle: a
# terminal serves a ~1-inch neighbourhood, which is how a tube amp's star
# grounds and B+ droppers are actually built. Exposed, never buried.
DEFAULT_CLUSTER_MM = 25.0

# Default terminal via: a hole a 20 AWG lead (0.81 mm) solders through, with a
# healthy ring. JLCPCB standard drills 0.15-6.3 mm with NO surcharge at/above
# 0.3 mm (jlcpcb.com/capabilities/pcb-capabilities; the extra-charge list
# surcharges only holes < 0.3 mm), so a 1.0 mm drill is free, and the 2.0 mm
# pad leaves a 0.5 mm annular ring — far above the 0.05 mm via / 0.15 mm PTH
# floor and easily solderable. Both are exposed so a heavier lead can ask for
# a bigger hole.
DEFAULT_TERMINAL_SIZE_MM = 2.0
DEFAULT_TERMINAL_DRILL_MM = 1.0


@dataclass
class Terminal:
    """One physical wire-landing for a terminal-served net: a via a flying
    lead solders through. `nodes` are the lattice nodes it occupies (one per
    layer, so a route may reach it from either side, like a through-hole
    pad); `claim` is every node its copper-plus-clearance keep-out sterilises
    for foreign nets."""
    net_code: int
    net_name: str
    x_mm: float
    y_mm: float
    nodes: tuple            # lattice node ids, one per layer
    claim: tuple = ()       # keep-out node ids (nodes + exclusion halo)
    cluster_pads: int = 0   # how many of the net's pads this terminal serves
    size_mm: float = DEFAULT_TERMINAL_SIZE_MM
    drill_mm: float = DEFAULT_TERMINAL_DRILL_MM


@dataclass
class TerminalPlan:
    """What plan_terminals decided for every terminal-served net, for the
    contract/report. `terminals` is what the router and writer consume; the
    rest is the honest before/after the caller prints."""
    terminals: dict = field(default_factory=dict)   # net_code -> [Terminal]
    pad_counts: dict = field(default_factory=dict)   # net_code -> pad count
    single_mst_mm: dict = field(default_factory=dict)   # net_code -> MST mm
    terminal_mm: dict = field(default_factory=dict)  # net_code -> star mm
    walled_off: list = field(default_factory=list)   # (net_code, reason)


def cluster_pads(centers, threshold_mm):
    """Greedy leader clustering of pad centres, deterministic.

    Each pad joins the existing cluster whose running centroid is nearest in
    Manhattan distance, provided that distance is <= threshold_mm; otherwise it
    starts a new cluster. Pads are processed in sorted (x, y) order so the
    result never depends on pad file order. Bounds every cluster's spread to
    ~threshold about its centroid — compact local groups, which is what a
    single solder terminal can serve. Returns [[pad_index, ...], ...], each
    member list sorted, the clusters themselves ordered by first centroid.
    """
    order = sorted(range(len(centers)),
                   key=lambda i: (centers[i][0], centers[i][1], i))
    clusters = []   # each: [members, cx, cy]
    for i in order:
        x, y = centers[i]
        best, best_d = None, None
        for cl in clusters:
            d = abs(cl[1] - x) + abs(cl[2] - y)
            if d <= threshold_mm and (best_d is None or d < best_d):
                best, best_d = cl, d
        if best is None:
            clusters.append([[i], x, y])
        else:
            best[0].append(i)
            n = len(best[0])
            best[1] = (best[1] * (n - 1) + x) / n
            best[2] = (best[2] * (n - 1) + y) / n
    return [sorted(cl[0]) for cl in clusters]


def _point_pad_gap(px, py, pad):
    """mm from a point to a pad's true (rotated) copper rect; 0 if inside.
    Same board->pad-frame transform lattice.pad_ring_nodes uses."""
    t = math.radians(getattr(pad, "rotation_deg", 0.0))
    c, s = math.cos(t), math.sin(t)
    dx, dy = px - pad.x_mm, py - pad.y_mm
    lx = dx * c - dy * s
    ly = dx * s + dy * c
    ex = max(abs(lx) - pad.width_mm / 2.0, 0.0)
    ey = max(abs(ly) - pad.height_mm / 2.0, 0.0)
    return math.hypot(ex, ey)


def _snap_terminal(lat, cx, cy, net, node_owner, clearance,
                   core_mm, exclusion_mm, max_reach_mm,
                   foreign_pads=(), min_pad_gap=0.0):
    """Nearest free legal lattice site for a terminal near (cx, cy).

    A terminal is a via, so it must land where its pad copper (radius core_mm)
    overlaps no FOREIGN pad and no foreign clearance ring, on every layer, AND
    stays `min_pad_gap` (= size/2 + clearance) clear of every foreign pad's
    copper — a CONTINUOUS check against pad rects, because a wide SMD pad's edge
    falls between grid nodes and the node checks alone miss it (measured on
    Voxy: a 2.54 mm 5V_MON pad let a terminal land 0.26 mm from its copper).
    Scans grid sites outward in Chebyshev rings (deterministic order) up to
    max_reach_mm from the centroid; the first clear site wins. Returns
    (center_nodes, claim_nodes):
      center_nodes: the site's node on every layer (a via spans all layers);
      claim_nodes:  center nodes plus the exclusion halo (radius exclusion_mm,
                    every layer) minus anything already owned — the keep-out to
                    hand foreign nets so their copper stays `clearance` away.
    Returns (None, ()) when no site within reach is clear: the caller reports
    that rather than pretending it placed a terminal.
    """
    ox, oy = lat.origin_mm
    p = lat.pitch_mm
    ring = clearance.node_net if clearance is not None else {}
    ix0 = min(max(int(math.floor((cx - ox) / p + 0.5)), 0), lat.W - 1)
    iy0 = min(max(int(math.floor((cy - oy) / p + 0.5)), 0), lat.H - 1)
    core = halo_offsets(p, core_mm)          # pad-copper footprint, offsets
    reach = max(0, int(math.floor(max_reach_mm / p + 1e-9)))

    def core_clear(ix, iy):
        for dx, dy in core:
            jx, jy = ix + dx, iy + dy
            if not (0 <= jx < lat.W and 0 <= jy < lat.H):
                return False               # core would hang off the lattice
            for il in range(lat.L):
                n = lat.node(jx, jy, il)
                if node_owner.get(n, net) != net:
                    return False           # foreign pad copper here
                if ring.get(n, net) not in (net,):
                    return False           # foreign clearance ring / edge band
        if min_pad_gap > 0.0 and foreign_pads:
            px, py = ox + ix * p, oy + iy * p
            for pad in foreign_pads:
                if _point_pad_gap(px, py, pad) < min_pad_gap - 1e-9:
                    return False           # too close to foreign pad copper
        return True

    for r in range(reach + 1):
        hit = None
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue               # perimeter of ring r only
                ix, iy = ix0 + dx, iy0 + dy
                if 0 <= ix < lat.W and 0 <= iy < lat.H and core_clear(ix, iy):
                    hit = (ix, iy)
                    break
            if hit:
                break
        if not hit:
            continue
        ix, iy = hit
        centers = tuple(lat.node(ix, iy, il) for il in range(lat.L))
        claim = set(centers)
        for dx, dy in halo_offsets(p, exclusion_mm):
            jx, jy = ix + dx, iy + dy
            if 0 <= jx < lat.W and 0 <= jy < lat.H:
                for il in range(lat.L):
                    n = lat.node(jx, jy, il)
                    if node_owner.get(n, net) == net:   # never steal foreign
                        claim.add(n)
        return centers, tuple(sorted(claim))
    return None, ()


def _manhattan_mst_mm(centers):
    """Total Manhattan length of a Prim MST over pad centres — the on-board
    connection length a NORMAL (single-tree) route would have to carry. A
    lower bound on the copper terminal-serving collapses."""
    if len(centers) < 2:
        return 0.0
    from pathfinder import _mst_edges
    total = 0.0
    for i, j in _mst_edges(centers):
        total += abs(centers[i][0] - centers[j][0]) + \
            abs(centers[i][1] - centers[j][1])
    return total


def plan_terminals(board, lat, node_owner, clearance, net_codes,
                   cluster_mm=DEFAULT_CLUSTER_MM,
                   size_mm=DEFAULT_TERMINAL_SIZE_MM,
                   drill_mm=DEFAULT_TERMINAL_DRILL_MM,
                   clearance_by_net=None, track_width_mm=0.2, via_size_mm=0.6,
                   overrides=None):
    """Cluster and place terminals for every net in `net_codes`.

    board/lat/node_owner/clearance are the routing context (node_owner is the
    pad-ownership map, clearance the lattice.Clearance or None). `net_codes`
    is the set/iterable of terminal-served net codes. `overrides`
    (net_code -> [(x_mm, y_mm), ...]) skips clustering for a net and places one
    terminal at each given point instead — the cheap caller override.

    Returns a TerminalPlan. A net whose every cluster fails to place a terminal
    lands in `walled_off` with a reason and contributes NO terminals, so the
    caller can fall it back to a normal route rather than drop it silently.
    """
    node_owner = node_owner or {}
    clearance_by_net = clearance_by_net or {}
    want = set(int(c) for c in net_codes)
    pads_by_net = {}
    on_lattice = []
    for pad in board.pads:
        if not any(ln in lat.layer_names for ln in pad.layers):
            continue
        on_lattice.append(pad)
        if pad.net_code in want:
            pads_by_net.setdefault(pad.net_code, []).append(pad)

    plan = TerminalPlan()
    # A running copy of ownership so terminals of different clusters/nets do
    # not land on top of each other: each placed terminal's claim is folded in
    # before the next is snapped.
    owner = dict(node_owner)

    for code in sorted(pads_by_net):
        pads = pads_by_net[code]
        centers = [(p.x_mm, p.y_mm) for p in pads]
        name = pads[0].net_name
        plan.pad_counts[code] = len(pads)
        plan.single_mst_mm[code] = _manhattan_mst_mm(centers)

        clr = float(clearance_by_net.get(code, 0.2))
        # The terminal is a fat via. Its keep-out must clear the WIDEST foreign
        # copper it can sit beside — a foreign via (via_size), which dominates a
        # foreign track — by `clr`: center-to-center >= size/2 + via/2 + clr.
        # And the terminal only lands where that WHOLE keep-out is virgin
        # copper (core = exclusion), so the disk is fully reserved and no
        # foreign pad-escape can be force-cleared into it — a wire-landing wants
        # open board anyway. Sizing it for a foreign track instead is what put
        # 66 via-via clearance violations on the first Voxy run.
        neighbor = max(track_width_mm, via_size_mm)
        exclusion = size_mm / 2.0 + neighbor / 2.0 + clr
        core = exclusion

        if overrides and code in overrides:
            seeds = [(x, y, None) for x, y in overrides[code]]
        else:
            clusters = cluster_pads(centers, cluster_mm)
            seeds = []
            for members in clusters:
                mx = sum(centers[i][0] for i in members) / len(members)
                my = sum(centers[i][1] for i in members) / len(members)
                seeds.append((mx, my, len(members)))

        placed = []
        # Reach: stay within the cluster's own neighbourhood, so a terminal
        # never wanders across the board to find air. Floor of 10 mm covers a
        # tight cluster boxed in on its centroid.
        reach = max(cluster_mm, 10.0)
        for sx, sy, npads in seeds:
            centers_nodes, claim = _snap_terminal(
                lat, sx, sy, code, owner, clearance, core, exclusion, reach)
            if centers_nodes is None:
                continue
            tx, ty = lat.node_xy_mm(centers_nodes[0])
            placed.append(Terminal(
                net_code=code, net_name=name, x_mm=tx, y_mm=ty,
                nodes=centers_nodes, claim=claim,
                cluster_pads=npads or 0, size_mm=size_mm, drill_mm=drill_mm))
            for n in claim:
                owner.setdefault(n, code)

        if not placed:
            plan.walled_off.append(
                (code, f"no free lattice site within {reach:.0f} mm of any "
                       f"cluster centroid to drop a {size_mm:.2g} mm terminal "
                       f"— the area is solid copper"))
            continue

        plan.terminals[code] = placed
        # Star connection length: each pad to its nearest placed terminal.
        star = 0.0
        for (px, py) in centers:
            star += min(abs(px - t.x_mm) + abs(py - t.y_mm) for t in placed)
        plan.terminal_mm[code] = star

    return plan
