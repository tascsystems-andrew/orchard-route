"""L1: board input — read a real KiCad .kicad_pcb into the shared Board model.

Pure-Python s-expression reader for the KiCad text format (KiCad 5 through
10). No pcbnew, no KiCad install, stdlib only: the router has to load boards
on a headless Mac Studio, and pcbnew's Python bindings drag in the whole GUI
stack.

KiCad 5 differences handled: footprints are (module ...) not (footprint ...);
simple net names are written unquoted ((net 72 /SWDIO)); SMD pads may carry a
numberless (drill (offset ...)) node, which correctly yields drill 0.

Two file-format facts shaped this module:

- KiCad 10 (version 20260206) dropped numeric net codes: pads/segments/vias
  carry (net "name") only, and there is no root net table. Board.nets still
  promises integer codes, so they are synthesized — 0 is the unconnected net
  "", every other referenced name gets 1..K in sorted order, deterministic
  across loads. Older files that declare (net N "name") at the root keep
  their file codes instead.
- KiCad angles are degrees counterclockwise but the Y axis points DOWN, so
  rotating a footprint-local offset (x, y) by theta is
      (x cos + y sin,  -x sin + y cos)
  — the transpose of the textbook Y-up matrix. A pad's own in-file angle
  already has the footprint angle folded in (long-standing format quirk), so
  it is never applied to position a second time; it is stored verbatim as
  Pad.rotation_deg alongside the TRUE (pre-rotation) pad size, so consumers
  can do exact point-in-rotated-rect tests instead of inflating a bbox.

Unknown node types (zones, graphics, groups, properties, ...) are skipped
without complaint; only the geometry the router needs is extracted.
"""
from dataclasses import dataclass
import math
import re


@dataclass
class Pad:
    x_mm: float; y_mm: float          # ABSOLUTE board coords (footprint at/rotation composed with pad offset+rotation)
    layers: list                       # e.g. ['F.Cu'], or every copper layer for through-hole
    net_code: int; net_name: str
    width_mm: float; height_mm: float  # TRUE pad size, before rotation
    through_hole: bool
    drill_mm: float                    # 0.0 for SMD
    rotation_deg: float = 0.0          # total rotation (footprint + pad, KiCad CCW, Y-down)
    plated: bool = True                # False for np_thru_hole — a bare tooling/
                                       # mounting hole with NO copper. It is given
                                       # copper layers here so the router still
                                       # treats the drill as an obstacle, but it
                                       # carries no metal a clearance check should
                                       # see (KiCad's clearance DRC ignores it).


@dataclass
class Track:
    start_mm: tuple; end_mm: tuple; width_mm: float; layer: str; net_code: int


@dataclass
class Via:
    x_mm: float; y_mm: float; drill_mm: float; size_mm: float; net_code: int


@dataclass(frozen=True)
class OutlineRegion:
    """One connected Edge.Cuts outline — ONE physical board.

    A .kicad_pcb may carry several disjoint outlines: a PANEL of separate
    boards in one file, with empty space between them. `Board.origin_mm` /
    `size_mm` are the UNION bbox of all of them (unchanged contract), which
    on a panel describes an area that is mostly not board. Everything that
    reasons about where copper may legally go must use these regions instead:
    the space between two regions is air, and copper drawn across it is
    copper drawn through nothing.
    """
    origin_mm: tuple   # (min_x, min_y) of this region's bbox
    size_mm: tuple     # (width, height) of this region's bbox
    shapes: int = 0    # Edge.Cuts graphics that make it up (diagnostics)

    @property
    def bounds(self):
        """(x0, y0, x1, y1)."""
        return (self.origin_mm[0], self.origin_mm[1],
                self.origin_mm[0] + self.size_mm[0],
                self.origin_mm[1] + self.size_mm[1])

    def contains(self, x_mm, y_mm, tol_mm=0.0):
        x0, y0, x1, y1 = self.bounds
        return (x0 - tol_mm <= x_mm <= x1 + tol_mm
                and y0 - tol_mm <= y_mm <= y1 + tol_mm)


@dataclass
class Board:
    path: str
    origin_mm: tuple   # (min_x, min_y) of the Edge.Cuts bounding box
    size_mm: tuple     # (width, height) of that bbox
    copper_layers: list   # stackup order, F.Cu first, B.Cu last
    nets: dict         # net_code -> net_name (net 0 is the unconnected net)
    pads: list; tracks: list; vias: list
    outline_regions: tuple = ()   # [OutlineRegion], one per disjoint outline;
                                  # () when the file has no Edge.Cuts at all.
                                  # A single-outline board yields exactly one,
                                  # whose bbox equals origin_mm/size_mm.
    footprint_courtyards: tuple = ()  # one entry per footprint IN _footprints()
                                  # ORDER: the local-frame F.CrtYd/B.CrtYd bbox
                                  # rect (x0,y0,x1,y1) or None. The real body
                                  # keep-out for THT parts whose body overhangs
                                  # its pads (place._local_geometry prefers it).
    footprint_sheets: tuple = ()  # one entry per footprint IN _footprints()
                                  # ORDER: the schematic sheet path (KiCad's
                                  # (sheetname ...)) or None — a ready-made,
                                  # human-authored grouping (parts on one sheet
                                  # usually belong on one board/area).
    holes: tuple = ()             # (cx, cy, radius) per board mounting hole
                                  # (gr_circle on Edge.Cuts / User layers). The
                                  # placer keeps courtyards off these, inflated
                                  # by screw-head clearance.


# ── s-expression parsing ──────────────────────────────────────────────────────

class QStr(str):
    """A string that was quoted in the source. Needed to tell a net NAMED "12"
    apart from a legacy numeric net code 12."""


_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}
_INT = re.compile(r"-?\d+$")


def _unquote(tok):
    body = tok[1:-1]
    if "\\" not in body:
        return QStr(body)
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return QStr("".join(out))


def parse_sexpr(text):
    """Nested lists of atoms. Quoted atoms become QStr (unescaped); everything
    else stays a plain str — numbers are converted lazily at the use site."""
    stack = [[]]
    for m in _TOKENS.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            if len(stack) < 2:
                raise ValueError("unbalanced ')' in s-expression")
            node = stack.pop()
            stack[-1].append(node)
        elif t[0] == '"':
            stack[-1].append(_unquote(t))
        else:
            stack[-1].append(t)
    if len(stack) != 1 or not stack[0]:
        raise ValueError("unbalanced or empty s-expression")
    return stack[0][0]


def _kids(node, tag):
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            yield c


def _kid(node, tag):
    for c in _kids(node, tag):
        return c
    return None


def _floats(node):
    """Direct numeric atoms of a node, in order; skips symbols like 'oval'."""
    out = []
    for a in node[1:]:
        if isinstance(a, str):
            try:
                out.append(float(a))
            except ValueError:
                pass
    return out


def _rotate(x, y, deg):
    """Rotate a local offset by deg (KiCad CCW, Y-down) into board frame."""
    if not deg:
        return x, y
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c + y * s, -x * s + y * c


def _net_ref(node):
    """(net "name") | (net N) | (net N "name") | (net N name) ->
    (code or None, name or None). KiCad 5 leaves simple net names unquoted
    ((net 1 GND), (net 72 /SWDIO)), so a plain atom that is not the (first)
    integer code is a name too; quoting still wins for a net NAMED "12"."""
    code, name = None, None
    for a in node[1:]:
        if isinstance(a, QStr):
            name = str(a)
        elif isinstance(a, str):
            if code is None and _INT.match(a):
                code = int(a)
            else:
                name = str(a)
    return code, name


def _expand_layers(node, copper_layers):
    """Pad (layers ...) -> concrete copper layers; masks/paste dropped."""
    if node is None:
        return []
    out = []
    for a in node[1:]:
        if not isinstance(a, str):
            continue
        a = str(a)
        if a == "*.Cu":
            expand = copper_layers
        elif a == "F&B.Cu":
            expand = [l for l in ("F.Cu", "B.Cu") if l in copper_layers]
        elif a.endswith(".Cu") and a in copper_layers:
            expand = [a]
        else:
            continue
        out.extend(l for l in expand if l not in out)
    return out


def _circumcenter(p1, p2, p3):
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    return (
        (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d,
        (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d,
    )


# ── board extraction ──────────────────────────────────────────────────────────

def _footprints(root):
    """All footprint nodes: (footprint ...) in KiCad 6+, (module ...) in
    KiCad 5 and earlier. Same interior structure for everything we read."""
    yield from _kids(root, "footprint")
    yield from _kids(root, "module")


def _fp_frame(fp):
    """(x, y, rotation_deg) of a footprint's (at ...) node."""
    f_at = _floats(_kid(fp, "at") or ["at"])
    fx = f_at[0] if len(f_at) > 0 else 0.0
    fy = f_at[1] if len(f_at) > 1 else 0.0
    frot = f_at[2] if len(f_at) > 2 else 0.0
    return fx, fy, frot


def _footprint_courtyard(fp):
    """Local-frame bbox rect (x0, y0, x1, y1) of a footprint's F.CrtYd/B.CrtYd
    graphics, or None when it draws no courtyard.

    Coordinates are the footprint's own LOCAL, UNROTATED frame — exactly the
    frame place._local_geometry backs pads out to — so the footprint (at x y rot)
    transform is applied later by the placement, identically for pads and
    courtyard. This is the real body keep-out for THT parts whose body overhangs
    its pad span (radials, box-film caps, power resistors, relays), where the
    pad-bbox proxy is 14-53% of the true area (feedback/courtyard-model-*).

    fp_circle/fp_arc are bounded by their FULL-CIRCLE bbox (centre +/- radius):
    an arc bulges past its start/mid/end vertices, so a keep-out must be a
    conservative superset, never smaller than the drawn body. Flip is not
    mirrored here — matching how pads are read (no mirror), so a flipped part's
    pad and courtyard stay in one frame."""
    lo_x = lo_y = math.inf
    hi_x = hi_y = -math.inf
    for g in fp:
        if not (isinstance(g, list) and g and isinstance(g[0], str)
                and g[0].startswith("fp_")):
            continue
        kind = g[0][3:]
        if kind not in ("line", "rect", "arc", "circle", "poly"):
            continue
        layer = _kid(g, "layer")
        if not (layer and len(layer) > 1
                and str(layer[1]) in ("F.CrtYd", "B.CrtYd")):
            continue
        pts = []
        if kind == "circle":
            c = _floats(_kid(g, "center") or ["center"])
            e = _floats(_kid(g, "end") or ["end"])
            if len(c) >= 2 and len(e) >= 2:
                r = math.hypot(e[0] - c[0], e[1] - c[1])
                pts += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        elif kind == "arc":
            # An arc bulges past its start/mid/end vertices, so bound it by the
            # FULL-CIRCLE bbox (centre +/- radius) — a conservative superset,
            # never smaller than the drawn body. Same treatment the Edge.Cuts
            # scan gives arcs; both KiCad dialects handled.
            s = _floats(_kid(g, "start") or ["start"])
            e = _floats(_kid(g, "end") or ["end"])
            if _kid(g, "mid") is not None:          # KiCad 6+: start/mid/end on the curve
                m = _floats(_kid(g, "mid"))
                cc = (_circumcenter((s[0], s[1]), (m[0], m[1]), (e[0], e[1]))
                      if len(s) >= 2 and len(m) >= 2 and len(e) >= 2 else None)
                if cc:
                    r = math.hypot(s[0] - cc[0], s[1] - cc[1])
                    pts += [(cc[0] - r, cc[1] - r), (cc[0] + r, cc[1] + r)]
                elif len(s) >= 2 and len(e) >= 2:   # collinear: the chord bounds it
                    pts += [(s[0], s[1]), (e[0], e[1])]
            elif len(s) >= 2 and len(e) >= 2:       # KiCad 5: (start)=centre, (end) on circle
                r = math.hypot(e[0] - s[0], e[1] - s[1])
                pts += [(s[0] - r, s[1] - r), (s[0] + r, s[1] + r)]
        elif kind == "poly":
            pn = _kid(g, "pts")
            for xy in (pn[1:] if pn else []):
                if isinstance(xy, list) and xy and xy[0] == "xy":
                    f = _floats(xy)
                    if len(f) >= 2:
                        pts.append((f[0], f[1]))
        else:  # line / rect — the named vertices bound the shape
            for key in ("start", "end"):
                node = _kid(g, key)
                if node is not None:
                    f = _floats(node)
                    if len(f) >= 2:
                        pts.append((f[0], f[1]))
        for px, py in pts:
            lo_x = min(lo_x, px); hi_x = max(hi_x, px)
            lo_y = min(lo_y, py); hi_y = max(hi_y, py)
    if lo_x is math.inf:
        return None
    return (lo_x, lo_y, hi_x, hi_y)


def _footprint_sheet(fp):
    """The footprint's schematic sheet path — KiCad's (sheetname "...") child —
    or None. This is a grouping the human ALREADY authored (by drawing the
    schematic into sheets), so reading it is input, not inference: parts on one
    sheet usually belong on one board / one area."""
    node = _kid(fp, "sheetname")
    if node is not None and len(node) > 1 and isinstance(node[1], str):
        return str(node[1])
    return None


def _hole_layer(g):
    """The circle's layer string if it is on Edge.Cuts or a User layer (where
    mechanical holes/cutouts live), else None. Circles on silk/copper/courtyard
    layers are decoration, not holes."""
    layer = _kid(g, "layer")
    ln = str(layer[1]) if layer and len(layer) > 1 else ""
    return ln if (ln == "Edge.Cuts" or ln.startswith("User")) else None


def _board_holes(root, board_size=None):
    """(cx, cy, radius) for each board mounting hole / circular cutout: a
    gr_circle on Edge.Cuts or a User layer, PLUS fp_circle on those layers drawn
    inside a footprint (the MountingHole-footprint idiom), transformed into board
    coordinates by the footprint (at x y rot) frame. The placer keeps courtyards
    off these, inflated by screw-head clearance.

    The board's OUTER boundary circle (a round board's outline) is excluded: a
    real hole is smaller than the board, so a circle whose diameter spans the
    whole board is the outline, not a hole. (An np_thru_hole pad with no
    fp_circle outline is not modelled — the MountingHole footprints that place
    such pads draw the Edge.Cuts fp_circle this reads.)"""
    min_side = min(board_size) if board_size else math.inf
    holes = []

    def emit(cx, cy, r):
        if r > 0 and 2 * r < min_side - 1e-6:      # exclude the board-outline circle
            holes.append((cx, cy, r))

    for g in root:                                 # top-level gr_circle
        if isinstance(g, list) and g and g[0] == "gr_circle" \
                and _hole_layer(g) is not None:
            c = _floats(_kid(g, "center") or ["center"])
            e = _floats(_kid(g, "end") or ["end"])
            if len(c) >= 2 and len(e) >= 2:
                emit(c[0], c[1], math.hypot(e[0] - c[0], e[1] - c[1]))

    for fp in _footprints(root):                   # fp_circle inside footprints
        fx, fy, frot = _fp_frame(fp)
        for g in fp:
            if isinstance(g, list) and g and g[0] == "fp_circle" \
                    and _hole_layer(g) is not None:
                c = _floats(_kid(g, "center") or ["center"])
                e = _floats(_kid(g, "end") or ["end"])
                if len(c) >= 2 and len(e) >= 2:
                    dx, dy = _rotate(c[0], c[1], frot)
                    emit(fx + dx, fy + dy,
                         math.hypot(e[0] - c[0], e[1] - c[1]))
    return holes


def _edge_shapes(root):
    """Every Edge.Cuts graphic as (joint_points, (x0, y0, x1, y1)).

    `joint_points` are the graphic's real vertices — the points at which it
    can share an edge with another graphic. Circles and arcs also contribute
    their centre +/- radius extremes to the BBOX (a conservative superset,
    fine for a bbox) but never to the joints: a bbox corner is not a place
    two outlines meet, and treating it as one would fuse a panel back into
    a single region.

    Collects root gr_* nodes AND fp_* nodes inside footprints (some boards,
    e.g. SparkFun's, draw the whole outline inside a footprint), composing
    the footprint (at x y rot) transform for the latter."""
    shapes = []

    def scan(parent, prefix, xform):
        for g in parent:
            if not (isinstance(g, list) and g and isinstance(g[0], str)):
                continue
            tag = g[0]
            if not tag.startswith(prefix):
                continue
            kind = tag[len(prefix):]
            if kind not in ("line", "rect", "arc", "circle", "poly"):
                continue
            layer = _kid(g, "layer")
            if not (layer and len(layer) > 1 and str(layer[1]) == "Edge.Cuts"):
                continue
            joints, ext = [], []

            def joint(node):
                if node is not None:
                    f = _floats(node)
                    if len(f) >= 2:
                        p = xform(f[0], f[1])
                        joints.append(p)
                        ext.append(p)
                        return p
                return None

            if kind in ("line", "rect"):
                joint(_kid(g, "start"))
                joint(_kid(g, "end"))
            elif kind == "circle":
                c = _floats(_kid(g, "center") or ["center"])
                e = _floats(_kid(g, "end") or ["end"])
                if len(c) >= 2 and len(e) >= 2:
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    cx, cy = xform(c[0], c[1])
                    ext.append((cx - r, cy - r))
                    ext.append((cx + r, cy + r))
            elif kind == "arc" and _kid(g, "mid") is not None:
                # KiCad 6+: start/mid/end all lie ON the curve.
                p1 = joint(_kid(g, "start"))
                pm = joint(_kid(g, "mid"))
                p2 = joint(_kid(g, "end"))
                if p1 and pm and p2:
                    cc = _circumcenter(p1, pm, p2)
                    if cc:
                        r = math.hypot(p1[0] - cc[0], p1[1] - cc[1])
                        ext.append((cc[0] - r, cc[1] - r))
                        ext.append((cc[0] + r, cc[1] + r))
            elif kind == "arc":
                # KiCad 5 and earlier: (start) is the CENTRE, (end) is one
                # endpoint, (angle) sweeps to the other. The centre is not a
                # joint — it is inside the board — and the file format does
                # not settle the sweep's sign, so BOTH rotations of the known
                # endpoint are offered as joints. The wrong one is a point on
                # the arc's circle where no other outline graphic has a
                # vertex (a corner fillet's mirror lands mid-board), so it
                # joins nothing; the right one closes the outline. Without
                # this, every rounded corner on a KiCad 5 board splits the
                # outline into as many "regions" as it has straight edges —
                # measured on rpi-pico-vga, which reported 4.
                c = _floats(_kid(g, "start") or ["start"])
                e = _floats(_kid(g, "end") or ["end"])
                a = _floats(_kid(g, "angle") or ["angle"])
                if len(c) >= 2 and len(e) >= 2:
                    cx, cy = xform(c[0], c[1])
                    ex, ey = xform(e[0], e[1])
                    r = math.hypot(ex - cx, ey - cy)
                    ext.append((cx - r, cy - r))
                    ext.append((cx + r, cy + r))
                    joints.append((ex, ey))
                    sweep = a[0] if a else 0.0
                    for sign in (1.0, -1.0):
                        dx, dy = _rotate(ex - cx, ey - cy, sign * sweep)
                        joints.append((cx + dx, cy + dy))
            elif kind == "poly":
                pts = _kid(g, "pts")
                for p in pts[1:] if pts else []:
                    if isinstance(p, list) and p and p[0] == "xy":
                        joint(p)
                    elif isinstance(p, list) and p and p[0] == "arc":
                        for sub in ("start", "mid", "end"):
                            joint(_kid(p, sub))
            if ext:
                xs = [q[0] for q in ext]
                ys = [q[1] for q in ext]
                shapes.append((joints,
                               (min(xs), min(ys), max(xs), max(ys))))

    scan(root, "gr_", lambda x, y: (x, y))
    for fp in _footprints(root):
        fx, fy, frot = _fp_frame(fp)

        def to_board(x, y, fx=fx, fy=fy, frot=frot):
            dx, dy = _rotate(x, y, frot)
            return fx + dx, fy + dy

        scan(fp, "fp_", to_board)
    return shapes


#: Two Edge.Cuts vertices this close (mm) are the same corner. KiCad snaps
#: outline endpoints, so the tolerance only has to absorb 6-decimal rounding.
REGION_JOIN_TOL_MM = 0.01


def _edge_bbox_of(shapes):
    if not shapes:
        return (0.0, 0.0), (0.0, 0.0)
    x0 = min(b[0] for _j, b in shapes)
    y0 = min(b[1] for _j, b in shapes)
    x1 = max(b[2] for _j, b in shapes)
    y1 = max(b[3] for _j, b in shapes)
    return (x0, y0), (x1 - x0, y1 - y0)


def _edge_bbox(root):
    """Union bounding box of every Edge.Cuts graphic (the historic contract:
    Board.origin_mm / size_mm). On a panel this box is mostly not board —
    see outline_regions."""
    return _edge_bbox_of(_edge_shapes(root))


def outline_regions(shapes, tol_mm=REGION_JOIN_TOL_MM):
    """Disjoint outline regions of a board: [OutlineRegion], one per PHYSICAL
    board in the file.

    Two Edge.Cuts graphics belong to the same region when they share a vertex
    (within tol_mm) — the connected components of the outline graph. That
    alone would also split a board's INNER features into regions of their
    own: a milled slot, a routed cutout, a mounting hole drawn as an
    Edge.Cuts circle are each a closed loop touching nothing. So a component
    whose bbox lies entirely inside another component's bbox is absorbed into
    it: an inner feature is part of the board it is cut out of, not a board
    of its own.

    The known false merge is the inverse case — a small daughterboard
    panelised INSIDE a larger board's cutout would be absorbed by it. That is
    rare, and the conservative direction: it under-reports regions rather
    than splitting one real board in two.

    Returned sorted by (y0, x0) so region indices are stable across loads.
    """
    if not shapes:
        return []

    parent = list(range(len(shapes)))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    q = max(float(tol_mm), 1e-9)
    pts = [(x, y, i) for i, (joints, _b) in enumerate(shapes) for x, y in joints]
    buckets = {}
    for k, (x, y, _i) in enumerate(pts):
        buckets.setdefault((math.floor(x / q), math.floor(y / q)), []).append(k)
    for x, y, i in pts:
        cx, cy = math.floor(x / q), math.floor(y / q)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for m in buckets.get((cx + dx, cy + dy), ()):
                    x2, y2, j = pts[m]
                    if i != j and abs(x - x2) <= tol_mm and abs(y - y2) <= tol_mm:
                        union(i, j)

    comps = {}
    for i in range(len(shapes)):
        comps.setdefault(find(i), []).append(i)
    boxes = []
    for members in comps.values():
        bs = [shapes[m][1] for m in members]
        boxes.append([min(b[0] for b in bs), min(b[1] for b in bs),
                      max(b[2] for b in bs), max(b[3] for b in bs),
                      len(members)])

    def area(b):
        return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)

    # Absorb inner features. Strictly-smaller-area first so two identical
    # boxes never absorb each other into nothing.
    merged = True
    while merged and len(boxes) > 1:
        merged = False
        for a in range(len(boxes)):
            for b in range(len(boxes)):
                if a == b:
                    continue
                A, B = boxes[a], boxes[b]
                inside = (A[0] >= B[0] - tol_mm and A[1] >= B[1] - tol_mm
                          and A[2] <= B[2] + tol_mm and A[3] <= B[3] + tol_mm)
                if inside and (area(A) < area(B) - 1e-9 or a > b):
                    B[4] += A[4]
                    boxes.pop(a)
                    merged = True
                    break
            if merged:
                break

    boxes.sort(key=lambda b: (b[1], b[0]))
    return [OutlineRegion(origin_mm=(b[0], b[1]),
                          size_mm=(b[2] - b[0], b[3] - b[1]), shapes=b[4])
            for b in boxes]


def _collect_net_names(node, out):
    for c in node:
        if isinstance(c, list) and c:
            if c[0] == "net":
                _, name = _net_ref(c)
                if name is not None:
                    out.add(name)
            else:
                _collect_net_names(c, out)


def load_board(path: str) -> Board:
    with open(path, encoding="utf-8") as f:
        root = parse_sexpr(f.read())
    if root[0] != "kicad_pcb":
        raise ValueError(f"{path}: not a kicad_pcb file (root is {root[0]!r})")

    copper_layers = []
    layers_node = _kid(root, "layers")
    for entry in layers_node[1:] if layers_node else []:
        # (ordinal "Name" type [...]) — file order IS stackup order for copper.
        if isinstance(entry, list) and len(entry) >= 2 and str(entry[1]).endswith(".Cu"):
            copper_layers.append(str(entry[1]))

    # Net codes: legacy root table if present, else synthesized from all names
    # referenced anywhere in the tree (KiCad 10 has names only).
    declared = {}
    for n in _kids(root, "net"):
        code, name = _net_ref(n)
        if code is not None:
            declared[code] = name or ""
    if declared:
        nets = dict(declared)
        nets.setdefault(0, "")
    else:
        referenced = set()
        _collect_net_names(root, referenced)
        nets = {0: ""}
        for i, name in enumerate(sorted(n for n in referenced if n != ""), start=1):
            nets[i] = name
    name_to_code = {name: code for code, name in nets.items()}

    def resolve(net_node):
        if net_node is not None:
            code, name = _net_ref(net_node)
            if name is not None and name in name_to_code:
                return name_to_code[name], name
            if code is not None and code in nets:
                return code, nets[code]
        return 0, nets[0]

    pads = []
    for fp in _footprints(root):
        fx, fy, frot = _fp_frame(fp)
        for pad in _kids(fp, "pad"):
            ptype = pad[2] if len(pad) > 2 and isinstance(pad[2], str) else "smd"
            through = ptype in ("thru_hole", "np_thru_hole")

            p_at = _floats(_kid(pad, "at") or ["at"])
            ox = p_at[0] if len(p_at) > 0 else 0.0
            oy = p_at[1] if len(p_at) > 1 else 0.0
            prot = p_at[2] if len(p_at) > 2 else 0.0  # absolute (frot included)
            dx, dy = _rotate(ox, oy, frot)

            sz = _floats(_kid(pad, "size") or ["size"])
            w = sz[0] if len(sz) > 0 else 0.0
            h = sz[1] if len(sz) > 1 else w

            drill = 0.0
            dn = _kid(pad, "drill")
            if dn is not None:
                dnums = _floats(dn)  # (drill d) or (drill oval a b): take max
                drill = max(dnums) if dnums else 0.0

            if through:
                pad_layers = list(copper_layers)
            else:
                pad_layers = _expand_layers(_kid(pad, "layers"), copper_layers)

            code, name = resolve(_kid(pad, "net"))
            pads.append(Pad(fx + dx, fy + dy, pad_layers, code, name,
                            w, h, through, drill, prot,
                            plated=(ptype != "np_thru_hole")))

    tracks = []
    for seg in _kids(root, "segment"):
        s = _floats(_kid(seg, "start") or ["start"])
        e = _floats(_kid(seg, "end") or ["end"])
        if len(s) < 2 or len(e) < 2:
            continue
        wn = _floats(_kid(seg, "width") or ["width"])
        ln = _kid(seg, "layer")
        code, _ = resolve(_kid(seg, "net"))
        tracks.append(Track((s[0], s[1]), (e[0], e[1]),
                            wn[0] if wn else 0.0,
                            str(ln[1]) if ln and len(ln) > 1 else "",
                            code))

    vias = []
    for v in _kids(root, "via"):
        a = _floats(_kid(v, "at") or ["at"])
        if len(a) < 2:
            continue
        sz = _floats(_kid(v, "size") or ["size"])
        dr = _floats(_kid(v, "drill") or ["drill"])
        code, _ = resolve(_kid(v, "net"))
        vias.append(Via(a[0], a[1], dr[0] if dr else 0.0,
                        sz[0] if sz else 0.0, code))

    shapes = _edge_shapes(root)
    origin, size = _edge_bbox_of(shapes)
    courtyards = tuple(_footprint_courtyard(fp) for fp in _footprints(root))
    sheets = tuple(_footprint_sheet(fp) for fp in _footprints(root))
    return Board(path=path, origin_mm=origin, size_mm=size,
                 copper_layers=copper_layers, nets=nets,
                 pads=pads, tracks=tracks, vias=vias,
                 outline_regions=tuple(outline_regions(shapes)),
                 footprint_courtyards=courtyards,
                 footprint_sheets=sheets,
                 holes=tuple(_board_holes(root, size)))
