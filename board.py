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


@dataclass
class Track:
    start_mm: tuple; end_mm: tuple; width_mm: float; layer: str; net_code: int


@dataclass
class Via:
    x_mm: float; y_mm: float; drill_mm: float; size_mm: float; net_code: int


@dataclass
class Board:
    path: str
    origin_mm: tuple   # (min_x, min_y) of the Edge.Cuts bounding box
    size_mm: tuple     # (width, height) of that bbox
    copper_layers: list   # stackup order, F.Cu first, B.Cu last
    nets: dict         # net_code -> net_name (net 0 is the unconnected net)
    pads: list; tracks: list; vias: list


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


def _edge_bbox(root):
    """Bounding box of Edge.Cuts graphics. Arcs and circles contribute their
    center +/- radius extremes — a conservative superset, fine for a bbox.
    Collects root gr_* nodes AND fp_* nodes inside footprints (some boards,
    e.g. SparkFun's, draw the whole outline inside a footprint), composing
    the footprint (at x y rot) transform for the latter."""
    xs, ys = [], []

    def add(x, y):
        xs.append(x)
        ys.append(y)

    def scan(parent, prefix, xform):
        def add_node_pt(node):
            if node is not None:
                f = _floats(node)
                if len(f) >= 2:
                    p = xform(f[0], f[1])
                    add(*p)
                    return p
            return None

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
            if kind in ("line", "rect"):
                add_node_pt(_kid(g, "start"))
                add_node_pt(_kid(g, "end"))
            elif kind == "circle":
                c = _floats(_kid(g, "center") or ["center"])
                e = _floats(_kid(g, "end") or ["end"])
                if len(c) >= 2 and len(e) >= 2:
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    cx, cy = xform(c[0], c[1])
                    add(cx - r, cy - r)
                    add(cx + r, cy + r)
            elif kind == "arc":
                p1 = add_node_pt(_kid(g, "start"))
                pm = add_node_pt(_kid(g, "mid"))
                p2 = add_node_pt(_kid(g, "end"))
                if p1 and pm and p2:
                    cc = _circumcenter(p1, pm, p2)
                    if cc:
                        r = math.hypot(p1[0] - cc[0], p1[1] - cc[1])
                        add(cc[0] - r, cc[1] - r)
                        add(cc[0] + r, cc[1] + r)
            elif kind == "poly":
                pts = _kid(g, "pts")
                for p in pts[1:] if pts else []:
                    if isinstance(p, list) and p and p[0] == "xy":
                        add_node_pt(p)
                    elif isinstance(p, list) and p and p[0] == "arc":
                        for sub in ("start", "mid", "end"):
                            add_node_pt(_kid(p, sub))

    scan(root, "gr_", lambda x, y: (x, y))
    for fp in _footprints(root):
        fx, fy, frot = _fp_frame(fp)

        def to_board(x, y, fx=fx, fy=fy, frot=frot):
            dx, dy = _rotate(x, y, frot)
            return fx + dx, fy + dy

        scan(fp, "fp_", to_board)
    if not xs:
        return (0.0, 0.0), (0.0, 0.0)
    return (min(xs), min(ys)), (max(xs) - min(xs), max(ys) - min(ys))


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
                            w, h, through, drill, prot))

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

    origin, size = _edge_bbox(root)
    return Board(path=path, origin_mm=origin, size_mm=size,
                 copper_layers=copper_layers, nets=nets,
                 pads=pads, tracks=tracks, vias=vias)
