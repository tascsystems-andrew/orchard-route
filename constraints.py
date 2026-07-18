"""L3: the closed constraint vocabulary for optimize_region v1.

Six forms, borrowed from ALIGN via REGION_SOLVER.md — and ONLY six. Free
text invites hallucination (design rule 7), so an unknown constraint name or
a malformed argument list is a hard ValueError whose message names the full
valid set; nothing is guessed, coerced, or silently dropped.

    fixed(ref)                              may not move (e.g. the tube socket)
    keepout(x,y,w,h)                        no component courtyard inside, mm
    adjacency_max_distance(ref_a,ref_b,mm)  centers at most mm apart
    min_distance(ref_a,ref_b,mm)            centers at least mm apart
    orientation_set(ref,[0,90,180,270])     allowed rotations (that closed set)
    edge(ref,side)                          courtyard on a fence edge;
                                            side in left/right/top/bottom

Constraints parse from two surfaces and normalize to the same frozen
Constraint value:

- the CLI string form: "min_distance(R4,C8,5)", "orientation_set(V1,[0,90])"
  (brackets around the angle list optional, whitespace ignored);
- structured dicts: {"type": "min_distance", "ref_a": "R4", "ref_b": "C8",
  "mm": 5} — exactly the keys of the form, extras are errors.

str(constraint) round-trips through parse_constraint.

Checking is separate from parsing: evaluate_constraints takes concrete
placements {ref: (x_mm, y_mm, rot_deg)} plus per-ref courtyard rects
(x0, y0, x1, y1) and returns one Check per constraint carrying BOTH the
hard verdict (ok / violated, with a human-readable reason either way) and a
soft penalty for the annealer: 0.0 when satisfied, else a magnitude in mm
(distance shortfall/excess, keepout penetration depth, edge gap) or, for
orientation_set and fixed rotation, degrees-off / 90 — commensurate with
millimetres so the SA energy can sum them. Distances are center-to-center
between placement points; keepout and edge look at courtyard rects.

Reference-name errors are split by stage: parse_constraints(specs,
known_refs=...) rejects refs the caller doesn't know at parse time;
evaluate_constraints raises on refs missing from placements/courtyards
(that is a programming error, not a constraint violation).
"""
from dataclasses import dataclass
import math
import re

# Signature strings, in spec order — quoted verbatim in every unknown-name
# error so the caller (an AI mid-session) sees the whole valid set.
SIGNATURES = (
    "fixed(ref)",
    "keepout(x,y,w,h)",
    "adjacency_max_distance(ref_a,ref_b,mm)",
    "min_distance(ref_a,ref_b,mm)",
    "orientation_set(ref,[0,90,180,270])",
    "edge(ref,side)",
)
VALID_KINDS = tuple(s.split("(")[0] for s in SIGNATURES)
VALID_SET_MSG = "valid constraints are " + ", ".join(SIGNATURES)
EDGE_SIDES = ("left", "right", "top", "bottom")
ORIENTATIONS = (0.0, 90.0, 180.0, 270.0)

# dict-form keys per kind, also the canonical argument order of the CLI form
_FIELDS = {
    "fixed": ("ref",),
    "keepout": ("x", "y", "w", "h"),
    "adjacency_max_distance": ("ref_a", "ref_b", "mm"),
    "min_distance": ("ref_a", "ref_b", "mm"),
    "orientation_set": ("ref", "angles"),
    "edge": ("ref", "side"),
}


@dataclass(frozen=True)
class Constraint:
    kind: str
    ref: str = None            # fixed / orientation_set / edge
    ref_a: str = None          # pair-distance forms
    ref_b: str = None
    mm: float = None           # pair-distance forms
    rect: tuple = None         # keepout (x, y, w, h)
    angles: tuple = None       # orientation_set, sorted unique
    side: str = None           # edge

    def refs(self):
        """The reference designators this constraint names, in order."""
        return tuple(r for r in (self.ref, self.ref_a, self.ref_b)
                     if r is not None)

    def __str__(self):
        if self.kind == "fixed":
            return f"fixed({self.ref})"
        if self.kind == "keepout":
            return "keepout({},{},{},{})".format(*(_num(v) for v in self.rect))
        if self.kind in ("adjacency_max_distance", "min_distance"):
            return f"{self.kind}({self.ref_a},{self.ref_b},{_num(self.mm)})"
        if self.kind == "orientation_set":
            return "orientation_set({},[{}])".format(
                self.ref, ",".join(_num(a) for a in self.angles))
        return f"edge({self.ref},{self.side})"


@dataclass(frozen=True)
class Check:
    """One constraint's verdict against one concrete placement set."""
    constraint: Constraint
    ok: bool
    reason: str        # human-readable in both directions
    penalty: float     # 0.0 when ok; violation magnitude otherwise (see module doc)


def _num(v):
    """Number -> minimal text ('5', '2.5') for canonical string forms."""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


_CALL = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", re.S)


def _err(msg):
    raise ValueError(msg)


def _need_number(kind, field, value, source):
    try:
        f = float(value)
    except (TypeError, ValueError):
        _err(f"{kind}: {value!r} is not a number for {field} in {source!r}")
    if not math.isfinite(f):
        _err(f"{kind}: {value!r} is not a finite number for {field} "
             f"in {source!r}")
    return f


def _need_ref(kind, field, value, source):
    if not isinstance(value, str) or not value.strip():
        _err(f"{kind}: {field} must be a non-empty reference string "
             f"in {source!r}")
    return value.strip()


def _angles_tuple(kind, values, source):
    out = []
    for v in values:
        a = _need_number(kind, "angle", v, source)
        if a not in ORIENTATIONS:
            _err(f"{kind}: angles must be from 0, 90, 180, 270 "
                 f"(got {_num(a)} in {source!r})")
        if a not in out:
            out.append(a)
    if not out:
        _err(f"{kind}: at least one angle is required in {source!r}")
    return tuple(sorted(out))


def _build(kind, vals, source):
    """vals: {field: raw value} for the kind's fields; validates + freezes."""
    if kind == "fixed":
        return Constraint("fixed", ref=_need_ref(kind, "ref", vals["ref"], source))
    if kind == "keepout":
        rect = tuple(_need_number(kind, f, vals[f], source)
                     for f in ("x", "y", "w", "h"))
        if rect[2] <= 0 or rect[3] <= 0:
            _err(f"keepout: w and h must be > 0 in {source!r}")
        return Constraint("keepout", rect=rect)
    if kind in ("adjacency_max_distance", "min_distance"):
        a = _need_ref(kind, "ref_a", vals["ref_a"], source)
        b = _need_ref(kind, "ref_b", vals["ref_b"], source)
        if a == b:
            _err(f"{kind}: ref_a and ref_b must be different refs "
                 f"(got {a!r} twice)")
        mm = _need_number(kind, "mm", vals["mm"], source)
        if mm <= 0:
            _err(f"{kind}: mm must be > 0 (got {_num(mm)} in {source!r})")
        return Constraint(kind, ref_a=a, ref_b=b, mm=mm)
    if kind == "orientation_set":
        angles = vals["angles"]
        if isinstance(angles, (int, float, str)):
            angles = [angles]
        return Constraint("orientation_set",
                          ref=_need_ref(kind, "ref", vals["ref"], source),
                          angles=_angles_tuple(kind, angles, source))
    # edge
    side = vals["side"]
    if not isinstance(side, str) or side.strip().lower() not in EDGE_SIDES:
        _err(f"edge: side must be one of {', '.join(EDGE_SIDES)} "
             f"(got {side!r})")
    return Constraint("edge", ref=_need_ref(kind, "ref", vals["ref"], source),
                      side=side.strip().lower())


def _parse_string(spec):
    m = _CALL.match(spec)
    if not m:
        _err(f"malformed constraint {spec!r}: expected name(arg,...) — "
             + VALID_SET_MSG)
    kind, argstr = m.group(1), m.group(2)
    if kind not in _FIELDS:
        _err(f"unknown constraint {kind!r}: " + VALID_SET_MSG)
    fields = _FIELDS[kind]
    if kind == "orientation_set":
        # orientation_set(V1,[0,90]) or orientation_set(V1,0,90)
        head, sep, tail = argstr.partition(",")
        if not sep:
            _err(f"orientation_set expects a ref and at least one angle, "
                 f"got {spec!r}")
        tail = tail.strip()
        if tail.startswith("[") != tail.endswith("]"):
            _err(f"orientation_set: unbalanced brackets in {spec!r}")
        if tail.startswith("["):
            tail = tail[1:-1]
        angles = [a.strip() for a in tail.split(",") if a.strip()]
        return _build(kind, {"ref": head.strip(), "angles": angles}, spec)
    args = [a.strip() for a in argstr.split(",")] if argstr.strip() else []
    if len(args) != len(fields):
        _err(f"{kind} expects {len(fields)} argument"
             f"{'s' if len(fields) != 1 else ''} ({','.join(fields)}), "
             f"got {len(args)} in {spec!r}")
    return _build(kind, dict(zip(fields, args)), spec)


def _parse_dict(spec):
    if "type" not in spec:
        _err(f"constraint dict {spec!r} missing 'type': " + VALID_SET_MSG)
    kind = spec["type"]
    if kind not in _FIELDS:
        _err(f"unknown constraint {kind!r}: " + VALID_SET_MSG)
    fields = _FIELDS[kind]
    extra = sorted(set(spec) - {"type"} - set(fields))
    if extra:
        _err(f"{kind}: unexpected key(s) {', '.join(map(repr, extra))} — "
             f"expected type, {', '.join(fields)}")
    missing = sorted(set(fields) - set(spec))
    if missing:
        _err(f"{kind}: missing key(s) {', '.join(map(repr, missing))} — "
             f"expected type, {', '.join(fields)}")
    return _build(kind, {f: spec[f] for f in fields}, str(spec))


def parse_constraint(spec, known_refs=None):
    """One spec (CLI string, structured dict, or Constraint) -> Constraint.

    known_refs, when given, is the closed set of reference designators the
    caller can move or see; a constraint naming any other ref is a hard
    ValueError listing the known refs.
    """
    if isinstance(spec, Constraint):
        c = spec
    elif isinstance(spec, str):
        c = _parse_string(spec)
    elif isinstance(spec, dict):
        c = _parse_dict(spec)
    else:
        _err(f"constraint must be a string or dict, got "
             f"{type(spec).__name__} ({spec!r})")
    if known_refs is not None:
        known = set(known_refs)
        for r in c.refs():
            if r not in known:
                _err(f"{c.kind}: unknown ref {r!r} — known refs: "
                     + ", ".join(sorted(known)))
    return c


def parse_constraints(specs, known_refs=None):
    """Parse a whole list; errors carry the offending spec's own text."""
    return [parse_constraint(s, known_refs) for s in specs]


# ── checking ──────────────────────────────────────────────────────────────────


def _ang_diff(a, b):
    """Smallest absolute angular difference in degrees, on the circle."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def _place(placements, kind, ref):
    try:
        return placements[ref]
    except KeyError:
        _err(f"{kind}: ref {ref!r} not in placements")


def _court(courtyards, kind, ref):
    try:
        return courtyards[ref]
    except KeyError:
        _err(f"{kind}: ref {ref!r} not in courtyards")


def evaluate_constraints(constraints, placements, courtyards, *,
                         rect=None, home=None, edge_tol_mm=1.0):
    """Judge every constraint against one concrete placement set.

    placements : {ref: (x_mm, y_mm, rot_deg)}
    courtyards : {ref: (x0, y0, x1, y1)} world-frame courtyard rects
    rect       : (x, y, w, h) fence rect — required by edge(...) constraints
                 (v1 measures edge against the fence the caller supplied;
                 fence a region flush with the board outline to mean the
                 board edge)
    home       : {ref: (x_mm, y_mm, rot_deg)} original placements — required
                 by fixed(...) constraints
    Returns [Check] in constraint order. Missing refs / missing rect / missing
    home raise ValueError: those are caller bugs, not violations.
    """
    out = []
    for c in constraints:
        if c.kind == "fixed":
            if home is None:
                _err("fixed: home placements are required to check "
                     f"'{c}' (pass home=...)")
            hx, hy, hr = _place(home, c.kind, c.ref)
            x, y, r = _place(placements, c.kind, c.ref)
            disp = math.hypot(x - hx, y - hy)
            dang = _ang_diff(r, hr)
            pen = disp + dang / 90.0
            if pen <= 1e-9:
                out.append(Check(c, True, f"{c.ref} is at its fixed position",
                                 0.0))
            else:
                out.append(Check(c, False,
                                 f"{c.ref} moved {disp:.3f} mm / "
                                 f"{dang:.1f} deg from its fixed position",
                                 pen))
        elif c.kind == "keepout":
            kx, ky, kw, kh = c.rect
            kx1, ky1 = kx + kw, ky + kh
            pen, offenders = 0.0, []
            for ref, (x0, y0, x1, y1) in courtyards.items():
                ow = min(x1, kx1) - max(x0, kx)
                oh = min(y1, ky1) - max(y0, ky)
                if ow > 1e-9 and oh > 1e-9:
                    offenders.append(ref)
                    pen += min(ow, oh)  # penetration depth, mm
            if offenders:
                out.append(Check(c, False,
                                 f"courtyard of {', '.join(sorted(offenders))} "
                                 f"inside keepout ({_num(kx)},{_num(ky)},"
                                 f"{_num(kw)},{_num(kh)})", pen))
            else:
                out.append(Check(c, True, "no courtyard intersects keepout "
                                 f"({_num(kx)},{_num(ky)},{_num(kw)},{_num(kh)})",
                                 0.0))
        elif c.kind in ("adjacency_max_distance", "min_distance"):
            ax, ay, _ = _place(placements, c.kind, c.ref_a)
            bx, by, _ = _place(placements, c.kind, c.ref_b)
            d = math.hypot(ax - bx, ay - by)
            if c.kind == "adjacency_max_distance":
                ok = d <= c.mm + 1e-9
                pen = max(0.0, d - c.mm)
                word = "within" if ok else "exceeds"
            else:
                ok = d >= c.mm - 1e-9
                pen = max(0.0, c.mm - d)
                word = "respects" if ok else "violates"
            out.append(Check(c, ok,
                             f"{c.ref_a}-{c.ref_b} distance {d:.3f} mm "
                             f"{word} {_num(c.mm)} mm", pen))
        elif c.kind == "orientation_set":
            _, _, r = _place(placements, c.kind, c.ref)
            off = min(_ang_diff(r, a) for a in c.angles)
            allowed = "[" + ",".join(_num(a) for a in c.angles) + "]"
            if off <= 1e-6:
                out.append(Check(c, True,
                                 f"{c.ref} rotation {_num(r % 360.0)} "
                                 f"is in {allowed}", 0.0))
            else:
                out.append(Check(c, False,
                                 f"{c.ref} rotation {_num(r % 360.0)} not in "
                                 f"{allowed} (off by {off:.1f} deg)",
                                 off / 90.0))
        else:  # edge
            if rect is None:
                _err(f"edge: a fence rect is required to check '{c}' "
                     "(pass rect=...)")
            x0, y0, x1, y1 = _court(courtyards, c.kind, c.ref)
            rx, ry, rw, rh = rect
            gap = {"left": x0 - rx, "right": (rx + rw) - x1,
                   "top": y0 - ry, "bottom": (ry + rh) - y1}[c.side]
            ok = gap <= edge_tol_mm + 1e-9
            out.append(Check(c, ok,
                             f"{c.ref} courtyard is {gap:.3f} mm off the "
                             f"{c.side} edge (tolerance {_num(edge_tol_mm)})",
                             max(0.0, gap - edge_tol_mm)))
    return out
