"""L0 output: emit a routed or component-moved COPY of a .kicad_pcb.

The original board file is never touched: the file's text is read, edited in
memory only, and written to a DIFFERENT path. Both writers refuse an
out_path that resolves into the source board's own directory — output copies
belong in the router's out/ tree, not next to the user's live project.

- write_routed_copy appends the router's (segment ...) / (via ...) nodes
  just before the final closing paren.
- write_moved_copy rewrites named footprints' (at x y [rot]) nodes in place
  (the region solver's placement candidates), leaving every other byte of
  the file untouched. It handles both the KiCad 6+ (footprint ...) and the
  KiCad 5 (module ...) form, since board.py parses both.

Three format facts, checked empirically against the Voxy boards, drive the
emitters:

- KiCad 10 (version 20260206) segments/vias reference nets BY NAME only —
  (net "Audio Input P1"), (net "") for the unconnected net — because the
  root net table is gone. Legacy files (root declares (net N "name")) keep
  numeric (net N) references; board.py preserves those file codes in
  Board.nets, so the caller's net_code passes straight through. Which style
  to emit is decided per file from its own root, never assumed.
- Every KiCad 8+ node carries a (uuid "..."); fresh uuid4s are emitted iff
  the file's own segment/via nodes carry them (falling back to the version
  header when the file has no tracks yet to inspect).
- Pad (at x y [angle]) angles are ABSOLUTE: the footprint's rotation is
  baked into every pad's own angle (board.py documents the same quirk from
  the read side), while the pad's x/y offset stays footprint-local. Checked
  in both generations: the hifi board's Valve_ECC-83-2 at -90 carries pads
  at 216/252/288/324 (the footprint's -90 folded into the library angles),
  and the KiCad 5 pico-vga R_0402s at 90 carry their
  angle-0 library pads as (at ±0.485 0 90). A zero angle is OMITTED, not
  written as 0 — the same valve writes pad 5 as (at -3.4373 -13.631278).
  So rotating a footprint means rewriting every pad's angle field by the
  same delta (re-omitting exact zeros), and moving without rotating must
  leave the pads' nodes alone.

Emitted nodes copy the KiCad 10 pretty-printer shape exactly — one attribute
per line, tab indentation, attribute order start/end/width/layer/net/uuid
(segments) and at/size/drill/layers/net/uuid (vias) — and numbers are
rounded to 6 decimals with trailing zeros stripped, as pcbnew writes them.
Vias span the file's outermost copper layers (F.Cu .. B.Cu).

Track widths and via sizes are per net. KiCad keeps net classes in the
PROJECT file, not the board: BOARD.kicad_pro (JSON) carries
net_settings.classes and the net->class maps (see load_net_class_widths).
The CLI reads the sibling .kicad_pro when present, lets --width-map override
it per net-name glob, and caps track widths at the lattice pitch (a 0.8 mm
trace on a 0.5 mm grid overlaps its neighbors) unless --max-width says
otherwise.

CLI: python writeback.py BOARD.kicad_pcb OUT.kicad_pcb [--pitch 0.5]
     [--layers F.Cu,B.Cu] [--width-map "GLOB=W[:VIA:DRILL],..."]
     [--max-width MM]
"""
import fnmatch
import json
import os
import re
import uuid
from dataclasses import dataclass, field

from board import parse_sexpr, QStr

# Emitter defaults, unchanged from the original single-width writeback: what
# a net gets when no project class and no --width-map entry claims it.
DEFAULT_TRACK_MM = 0.25
DEFAULT_VIA_MM = 0.6
DEFAULT_DRILL_MM = 0.3


def _kids(node, tag):
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            yield c


def _fmt(v):
    """6-decimal rounding, trailing zeros stripped — pcbnew's number style."""
    s = f"{round(float(v), 6):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _quote(name):
    return '"' + str(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _refuse_source_dir(board_path, out_path):
    """The input board is READ-ONLY (AGENTS.md hard rule 1). Refuse any
    out_path that is the source file or lands in its directory; returns the
    resolved destination path. Shared by every writer in this module."""
    src = os.path.realpath(board_path)
    dst = os.path.realpath(out_path)
    if dst == src or os.path.dirname(dst) == os.path.dirname(src):
        raise ValueError(
            f"refusing to write {out_path!r} into the source board's own "
            f"directory — output copies go elsewhere (e.g. out/)")
    return dst


def _file_facts(root):
    """(net_by_name, with_uuid, (outer_cu_first, outer_cu_last)) from the
    parsed file itself — mirror of board.py's format detection."""
    declared = False
    for n in _kids(root, "net"):
        if any(isinstance(a, str) and not isinstance(a, QStr)
               and a.lstrip("-").isdigit() for a in n[1:]):
            declared = True
            break
    net_by_name = not declared

    with_uuid = None
    for tag in ("segment", "via"):
        for node in _kids(root, tag):
            with_uuid = any(True for _ in _kids(node, "uuid"))
            break
        if with_uuid is not None:
            break
    if with_uuid is None:  # no tracks yet: KiCad 8 (20240108) introduced uuid
        version = 0
        for v in _kids(root, "version"):
            version = int(v[1])
            break
        with_uuid = version >= 20240108

    copper = []
    for layers in _kids(root, "layers"):
        for entry in layers[1:]:
            if isinstance(entry, list) and len(entry) >= 2 \
                    and str(entry[1]).endswith(".Cu"):
                copper.append(str(entry[1]))
        break
    span = (copper[0], copper[-1]) if copper else ("F.Cu", "B.Cu")
    return net_by_name, with_uuid, span


def project_file_for(board_path):
    """Sibling BOARD.kicad_pro for BOARD.kicad_pcb, or None if absent."""
    pro = os.path.splitext(board_path)[0] + ".kicad_pro"
    return pro if os.path.isfile(pro) else None


def load_net_class_widths(pro_path, nets,
                          track_width_mm=DEFAULT_TRACK_MM,
                          via_size_mm=DEFAULT_VIA_MM,
                          via_drill_mm=DEFAULT_DRILL_MM):
    """net_code -> (track_width_mm, via_size_mm, via_drill_mm) for every net,
    from the .kicad_pro's net classes.

    Format, checked empirically against the Voxy-family projects (all of
    which carry a single "Default" class, null assignments, [] patterns):
    net_settings.classes is a list of class dicts with name / track_width /
    via_diameter / via_drill (plus clearance etc. we don't need), and nets
    map to classes two ways — net_settings.netclass_assignments (net name ->
    class name, or -> list of class names; null when empty) and
    net_settings.netclass_patterns ([{"pattern": glob, "netclass": name}]).

    Resolution per net: explicit assignment wins over patterns; among several
    candidate classes the lowest "priority" number wins (KiCad: lower number
    = higher priority; Default carries INT_MAX), ties broken by listed order;
    no class at all means the Default class. Any value a class omits (or
    stores as <= 0) falls back to the Default class, then to the function
    defaults — so a project with no usable classes yields a clean map of
    function defaults. Malformed JSON raises; the caller decides.
    Pattern matching is fnmatch.fnmatchcase: KiCad net names are
    case-sensitive on every platform.
    """
    resolved, default_cls = _resolve_net_classes(pro_path, nets)
    fallback = (track_width_mm, via_size_mm, via_drill_mm)

    def value(cls, key, i):
        for src in (cls, default_cls):
            v = src.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
        return fallback[i]

    return {code: (value(cls, "track_width", 0),
                   value(cls, "via_diameter", 1),
                   value(cls, "via_drill", 2))
            for code, cls in resolved.items()}


def _resolve_net_classes(pro_path, nets):
    """Shared class resolution: (net_code -> winning class dict, Default
    class dict). The winning dict is the Default class (possibly {}) for
    nets no assignment or pattern claims. Resolution rules are documented on
    load_net_class_widths, the original consumer."""
    with open(pro_path, encoding="utf-8") as f:
        pro = json.load(f)
    ns = pro.get("net_settings") or {}
    classes = [c for c in (ns.get("classes") or []) if isinstance(c, dict)]
    by_name = {str(c.get("name")): c for c in classes}
    default_cls = by_name.get("Default", {})

    assignments = ns.get("netclass_assignments") or {}
    patterns = [p for p in (ns.get("netclass_patterns") or [])
                if isinstance(p, dict)]

    def candidates(name):
        a = assignments.get(name)
        names = [a] if isinstance(a, str) else \
            [n for n in a if isinstance(n, str)] if isinstance(a, list) else []
        found = [by_name[n] for n in names if n in by_name]
        if not found:
            found = [by_name[str(p.get("netclass"))] for p in patterns
                     if str(p.get("netclass")) in by_name and p.get("pattern")
                     and fnmatch.fnmatchcase(name, str(p["pattern"]))]
        return found

    out = {}
    for code, name in nets.items():
        cands = candidates(str(name))
        out[code] = min(cands, key=lambda c: c.get("priority", 2**31 - 1)) \
            if cands else default_cls
    return out, default_cls


def load_net_class_names(pro_path, nets):
    """net_code -> net class NAME for every net in nets, resolved exactly
    like load_net_class_widths (same shared machinery — one vocabulary, no
    drift). Nets no class claims report "Default", including on projects
    that never defined a Default class. The region solver's placement search
    weights HPWL per class through this map."""
    resolved, _ = _resolve_net_classes(pro_path, nets)
    return {code: str(cls["name"]) if "name" in cls else "Default"
            for code, cls in resolved.items()}


def parse_width_map(spec):
    """--width-map "GLOB=width[:via_size:via_drill],..." ->
    [(glob, track_mm, via_mm_or_None, drill_mm_or_None)] in listed order."""
    entries = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        glob, eq, val = raw.rpartition("=")
        parts = val.split(":")
        if not eq or not glob or len(parts) not in (1, 3):
            raise ValueError(
                f"bad --width-map entry {raw!r}: want GLOB=width or "
                f"GLOB=width:via_size:via_drill")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ValueError(f"bad --width-map entry {raw!r}: "
                             f"non-numeric width") from None
        if any(n <= 0 for n in nums):
            raise ValueError(f"bad --width-map entry {raw!r}: "
                             f"widths must be > 0")
        entries.append((glob, nums[0],
                        nums[1] if len(nums) == 3 else None,
                        nums[2] if len(nums) == 3 else None))
    return entries


def apply_width_map(widths, nets, entries,
                    track_width_mm=DEFAULT_TRACK_MM,
                    via_size_mm=DEFAULT_VIA_MM,
                    via_drill_mm=DEFAULT_DRILL_MM):
    """Overlay parsed --width-map entries onto a net_code -> triple dict.

    Entries apply in listed order, so a later glob overrides an earlier one
    ("*=0.3,GND=0.8" widens GND, not the reverse). A width-only entry keeps
    whatever via pair the net had already resolved to (project class or
    function defaults). Nets matching no glob keep their existing triple, or
    stay absent so the emitter's scalar defaults apply. Returns a new dict.
    """
    out = dict(widths)
    for code, name in nets.items():
        for glob, tw, vs, vd in entries:
            if fnmatch.fnmatchcase(str(name), glob):
                base = out.get(code, (track_width_mm, via_size_mm,
                                      via_drill_mm))
                out[code] = (tw, vs if vs is not None else base[1],
                             vd if vd is not None else base[2])
    return out


def cap_track_widths(widths, nets, max_width_mm):
    """Cap track widths at max_width_mm; returns (capped dict, sorted names
    of the nets that were capped).

    The CLI default cap is the lattice pitch: a trace wider than the pitch
    spills onto the neighboring grid line's copper, so a project class meant
    for free-form routing would silently short lattice neighbors. Via sizes
    are deliberately NOT capped — the stock 0.6 mm via already exceeds the
    0.5 mm default pitch, and vias land on pad/grid sites the router itself
    keeps nets apart on; capping them would change long-standing default
    output for no clearance gain."""
    capped, hit = dict(widths), []
    for code, (tw, vs, vd) in widths.items():
        if tw > max_width_mm + 1e-9:
            capped[code] = (max_width_mm, vs, vd)
            hit.append(str(nets.get(code, code)))
    return capped, sorted(hit)


def write_routed_copy(board_path, out_path, tracks, vias, nets,
                      track_width_mm=DEFAULT_TRACK_MM,
                      via_size_mm=DEFAULT_VIA_MM,
                      via_drill_mm=DEFAULT_DRILL_MM, widths=None):
    """Append the router's copper to a copy of board_path, written to out_path.

    tracks: (x1_mm, y1_mm, x2_mm, y2_mm, layer_name, net_code) as produced by
    pathfinder.paths_to_tracks; vias: (x_mm, y_mm, net_code); nets: Board.nets
    (net_code -> name). widths: optional net_code -> (track_width_mm,
    via_size_mm, via_drill_mm) overriding the scalar defaults per net —
    build it from load_net_class_widths / apply_width_map; nets absent from
    the dict fall back to the scalars. The source file is read-only;
    out_path must not resolve to the source file or into its directory.
    """
    dst = _refuse_source_dir(board_path, out_path)

    with open(board_path, encoding="utf-8") as f:
        text = f.read()
    root = parse_sexpr(text)
    if root[0] != "kicad_pcb":
        raise ValueError(f"{board_path}: not a kicad_pcb file")
    net_by_name, with_uuid, (cu_top, cu_bot) = _file_facts(root)

    def net_attr(code):
        if code not in nets:
            raise ValueError(f"net code {code} not in nets dict")
        return f"(net {_quote(nets[code])})" if net_by_name else f"(net {code})"

    def uuid_line():
        return f"\t\t(uuid \"{uuid.uuid4()}\")\n" if with_uuid else ""

    widths = widths or {}
    default_triple = (track_width_mm, via_size_mm, via_drill_mm)

    parts = []
    for x1, y1, x2, y2, layer, code in tracks:
        parts.append(
            "\t(segment\n"
            f"\t\t(start {_fmt(x1)} {_fmt(y1)})\n"
            f"\t\t(end {_fmt(x2)} {_fmt(y2)})\n"
            f"\t\t(width {_fmt(widths.get(code, default_triple)[0])})\n"
            f"\t\t(layer {_quote(layer)})\n"
            f"\t\t{net_attr(code)}\n"
            + uuid_line() +
            "\t)\n")
    for x, y, code in vias:
        _, via_mm, drill_mm = widths.get(code, default_triple)
        parts.append(
            "\t(via\n"
            f"\t\t(at {_fmt(x)} {_fmt(y)})\n"
            f"\t\t(size {_fmt(via_mm)})\n"
            f"\t\t(drill {_fmt(drill_mm)})\n"
            f"\t\t(layers {_quote(cu_top)} {_quote(cu_bot)})\n"
            f"\t\t{net_attr(code)}\n"
            + uuid_line() +
            "\t)\n")

    body = text.rstrip()
    if not body.endswith(")"):
        raise ValueError(f"{board_path}: does not end with a closing paren")
    cut = len(body) - 1  # index of the kicad_pcb closer in the original text
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text[:cut] + "".join(parts) + text[cut:])


# ── component moves ──────────────────────────────────────────────────────────
# write_moved_copy needs byte spans, which board.parse_sexpr discards, so the
# file is re-tokenized here with the same token grammar and the interesting
# nodes keep their source offsets. Only footprint/module nodes, their (at ...)
# and their pads' (at ...) are recorded; everything else passes through
# byte-identical.

_SEXP_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _atom_text(tok):
    """Raw token -> python string: unquote + unescape, same rules board.py
    applies, so reference names compare equal across both parsers."""
    if not (len(tok) >= 2 and tok[0] == '"' and tok[-1] == '"'):
        return tok
    body = tok[1:-1]
    if "\\" not in body:
        return body
    out, i = [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


@dataclass
class _Node:
    """One parenthesized node with its source span and direct atoms/kids."""
    start: int
    end: int = -1
    atoms: list = field(default_factory=list)   # (raw_token, start, end)
    kids: list = field(default_factory=list)

    @property
    def tag(self):
        return self.atoms[0][0] if self.atoms else ""


def _parse_spans(text):
    """Root _Node of the first s-expression in text (the kicad_pcb node)."""
    stack, root = [], None
    for m in _SEXP_TOKENS.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append(_Node(start=m.start()))
        elif t == ")":
            if not stack:
                raise ValueError("unbalanced ')' in s-expression")
            node = stack.pop()
            node.end = m.end()
            if stack:
                stack[-1].kids.append(node)
            elif root is None:
                root = node
        else:
            if stack:
                stack[-1].atoms.append((t, m.start(), m.end()))
    if root is None:
        raise ValueError("no s-expression found")
    return root


def _at_info(node):
    """(span, insert_offset, vals, extras) for a node's own (at ...) kid.

    span is None when the node carries no (at ...) — then insert_offset says
    where a fresh one may be spliced in (before the first kid, KiCad puts
    (at) first anyway). vals are the leading numeric atoms; extras every
    atom after the numeric run (e.g. "unlocked"), preserved verbatim."""
    at = next((k for k in node.kids if k.tag == "at"), None)
    if at is None:
        insert = node.kids[0].start if node.kids else node.end - 1
        return None, insert, (), ()
    vals, extras = [], []
    for tok, _, _ in at.atoms[1:]:
        if not extras:
            try:
                vals.append(float(tok))
                continue
            except ValueError:
                pass
        extras.append(tok)
    return (at.start, at.end), None, tuple(vals), tuple(extras)


@dataclass
class FootprintRecord:
    """One (footprint ...) / (module ...) node, with the spans needed to
    rewrite its placement. Records come in board.py's _footprints order —
    every (footprint ...) node in file order, then every (module ...) node —
    so slicing board.load_board(...).pads by cumulative n_pads lines each
    record up with its parsed pads exactly."""
    ref: str            # reference designator ("" when the file has none)
    uref: str           # unique key: ref when unique on the board, else "ref#N"
    x_mm: float
    y_mm: float
    rot_deg: float
    n_pads: int
    at_span: tuple      # (start, end) of the footprint's own (at ...), or None
    at_insert: int      # splice offset used only when at_span is None
    at_extras: tuple    # non-numeric atoms trailing the numbers, kept verbatim
    pad_ats: tuple      # per pad: (span_or_None, insert_offset, vals, extras)


def _footprint_ref(fp):
    """Reference designator of a footprint node, across the format's three
    spellings: (property "Reference" "R4" ...) in KiCad 8+, (fp_text
    reference "R4" ...) in KiCad 6/7, (fp_text reference R4 ...) in 5."""
    for k in fp.kids:
        if k.tag == "property" and len(k.atoms) >= 3 \
                and _atom_text(k.atoms[1][0]) == "Reference":
            return _atom_text(k.atoms[2][0])
        if k.tag == "fp_text" and len(k.atoms) >= 3 \
                and k.atoms[1][0] == "reference":
            return _atom_text(k.atoms[2][0])
    return ""


def _footprint_records(root):
    records = []
    for tag in ("footprint", "module"):    # board.py's _footprints order
        for fp in (k for k in root.kids if k.tag == tag):
            at_span, at_insert, vals, extras = _at_info(fp)
            pads = tuple(_at_info(p) for p in fp.kids if p.tag == "pad")
            records.append(FootprintRecord(
                ref=_footprint_ref(fp), uref="",
                x_mm=vals[0] if len(vals) > 0 else 0.0,
                y_mm=vals[1] if len(vals) > 1 else 0.0,
                rot_deg=vals[2] if len(vals) > 2 else 0.0,
                n_pads=len(pads), at_span=at_span,
                at_insert=at_insert if at_insert is not None else -1,
                at_extras=extras, pad_ats=pads))
    counts = {}
    for r in records:
        counts[r.ref] = counts.get(r.ref, 0) + 1
    seen = {}
    for r in records:
        if counts[r.ref] == 1:
            r.uref = r.ref
        else:
            seen[r.ref] = seen.get(r.ref, 0) + 1
            r.uref = f"{r.ref}#{seen[r.ref]}"
    return records


def board_footprints(text):
    """[FootprintRecord] for a .kicad_pcb file's TEXT (not path — the moved
    and routed copies get scanned too). Duplicate reference designators are
    legal in KiCad and common on Andrew's boards ("5755" x3, "TP8" x19),
    so each record carries uref, a unique addressing key: the plain ref when
    unique, else ref#N with N 1-based in record order."""
    root = _parse_spans(text)
    if root.tag != "kicad_pcb":
        raise ValueError(f"not a kicad_pcb file (root is {root.tag!r})")
    return _footprint_records(root)


def resolve_footprint(records, key):
    """The one FootprintRecord key addresses, or ValueError. key is a plain
    ref (must be unique on the board) or the ref#N disambiguator; the error
    for an ambiguous plain ref spells out the valid disambiguators."""
    by_uref = {r.uref: r for r in records}
    if key in by_uref:
        return by_uref[key]
    n = sum(1 for r in records if r.ref == key)
    if n > 1:
        raise ValueError(
            f"ref {key!r} matches {n} footprints — disambiguate as "
            f"{key}#1 .. {key}#{n} (file order)")
    raise ValueError(
        f"ref {key!r} not found among the board's {len(records)} footprints")


def _norm_fp_rot(deg):
    """Footprint angle normalized the way pcbnew writes it: (-180, 180]."""
    a = round(deg % 360.0, 6) % 360.0
    return a - 360.0 if a > 180.0 else a


def _norm_pad_rot(deg):
    """Pad angle normalized the way pcbnew writes it: [0, 360)."""
    return round(deg % 360.0, 6) % 360.0


def _at_edit(span, insert, x, y, rot, extras):
    """One text edit (start, end, replacement) rewriting an (at ...) node.
    A zero angle is omitted, exactly as pcbnew writes it. When the node had
    no (at ...) at all (span None — pathological but board.py reads it as
    0,0,0) a fresh node is spliced in at the insert offset instead."""
    nums = [x, y] + ([rot] if rot != 0.0 else [])
    body = " ".join([_fmt(n) for n in nums] + [str(e) for e in extras])
    if span is not None:
        return span[0], span[1], f"(at {body})"
    return insert, insert, f"(at {body}) "


def write_moved_copy(board_path, out_path, placements):
    """Rewrite named footprints' placements in a copy of board_path.

    placements: {ref_or_uref: (x_mm, y_mm, rot_deg)} — board coordinates and
    KiCad's CCW/Y-down degrees, exactly what board.load_board reports and
    what the region solver's candidates carry. Every named footprint's
    (at ...) is rewritten in place; when the rotation changes, every pad's
    baked-in absolute angle is rewritten by the same delta (see the module
    docstring — pad x/y offsets are footprint-local and stay untouched).
    Property/text angles are NOT rebaked: board.py does not read them and
    they only affect silkscreen cosmetics; KiCad reorients them on the next
    interactive edit.

    The source file is read-only; out_path must not resolve to the source
    file or into its directory. Everything outside the rewritten (at ...)
    nodes is byte-identical to the source."""
    dst = _refuse_source_dir(board_path, out_path)
    with open(board_path, encoding="utf-8") as f:
        text = f.read()
    try:
        records = board_footprints(text)
    except ValueError as e:
        raise ValueError(f"{board_path}: {e}") from None

    edits = []
    for key, place in placements.items():
        rec = resolve_footprint(records, key)
        try:
            nx, ny, nrot = (float(v) for v in place)
        except (TypeError, ValueError):
            raise ValueError(
                f"placement for {key!r} must be (x_mm, y_mm, rot_deg), "
                f"got {place!r}") from None
        edits.append(_at_edit(rec.at_span, rec.at_insert,
                              nx, ny, _norm_fp_rot(nrot), rec.at_extras))
        delta = nrot - rec.rot_deg
        if _norm_pad_rot(delta) != 0.0:
            for span, insert, vals, extras in rec.pad_ats:
                ox = vals[0] if len(vals) > 0 else 0.0
                oy = vals[1] if len(vals) > 1 else 0.0
                pa = vals[2] if len(vals) > 2 else 0.0
                edits.append(_at_edit(span, insert, ox, oy,
                                      _norm_pad_rot(pa + delta), extras))

    for start, end, repl in sorted(edits, reverse=True):
        text = text[:start] + repl + text[end:]
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Route a KiCad board and write a routed copy")
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--pitch", type=float, default=0.5)
    ap.add_argument("--layers", default="F.Cu,B.Cu")
    ap.add_argument("--width-map", default="", metavar="GLOB=W[:VIA:DRILL],...",
                    help="per-net width overrides by net-name glob, applied "
                         "after project net classes; last matching glob wins")
    ap.add_argument("--no-via-exclusion", action="store_true",
                    help="stop vias claiming their clearance neighbourhood "
                         "(restores the pre-exclusion router)")
    ap.add_argument("--max-width", type=float, default=None,
                    help="cap emitted track widths at this many mm "
                         "(default: the lattice pitch)")
    ap.add_argument("--fab", default="none",
                    help="manufacturing profile to resolve emission defaults "
                         "from and check against (default none = no "
                         "constraints). See `python fab.py` for the list.")
    ap.add_argument("--fab-enforce", action="store_true",
                    help="snap copper geometry to the --fab profile's "
                         "cheapest legal values, naming every change "
                         "(without this, violations only warn)")
    args = ap.parse_args(argv)
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]

    import fab as fab_mod
    try:
        fab_profile = fab_mod.load_profile(args.fab)
    except fab_mod.UnknownProfile as e:
        ap.error(str(e))

    from pathfinder import route_board, paths_to_tracks
    brd, lat, res = route_board(args.board, pitch_mm=args.pitch,
                                layer_names=layers,
                                fab=args.fab, fab_enforce=args.fab_enforce,
                                via_exclusion=not args.no_via_exclusion)
    # Prefer the router's smoothed geometry (45-degree segments emit as plain
    # (segment) nodes with diagonal endpoints — KiCad accepts them); raw
    # lattice geometry is the fallback when smoothing was disabled.
    if res.tracks is not None:
        tracks, vias = res.tracks, res.vias
    else:
        tracks, vias = paths_to_tracks(lat, res.net_paths)

    pro = project_file_for(args.board)
    widths = load_net_class_widths(pro, brd.nets) if pro else {}

    # Emission defaults: the scalars write_routed_copy falls back to for any
    # net no class and no --width-map claims. A fab profile replaces them with
    # its cheapest legal copper — but only the fallback. A net class the user
    # wrote is a design decision and still wins; the fab check below is what
    # tells them if that decision is unbuildable.
    emit = [DEFAULT_TRACK_MM, DEFAULT_VIA_MM, DEFAULT_DRILL_MM]
    fab_lines = []          # printed with the geometry block, not here
    if fab_profile.constrains:
        try:
            rec = fab_mod.recommend(fab_profile, args.pitch)
            emit = [rec.track_mm, rec.via_size_mm, rec.via_drill_mm]
            if not widths:
                fab_lines.append(
                    f"fab defaults: track {_fmt(rec.track_mm)} "
                    f"via {_fmt(rec.via_size_mm)}/{_fmt(rec.via_drill_mm)} mm "
                    f"from {fab_profile.name} (no net classes in this project)")
        except fab_mod.FabPitchError:
            pass    # route_board already reported it on res.fab_warnings

    if args.width_map:
        widths = apply_width_map(widths, brd.nets,
                                 parse_width_map(args.width_map),
                                 track_width_mm=emit[0], via_size_mm=emit[1],
                                 via_drill_mm=emit[2])
    max_width = args.max_width if args.max_width is not None else args.pitch
    widths, capped = cap_track_widths(widths, brd.nets, max_width)
    if capped:
        print(f"WARNING     : track width capped at {_fmt(max_width)} mm "
              f"(grid overlap) for {len(capped)} net(s): {', '.join(capped)}")

    # Check what will actually be EMITTED, not just what the router modelled.
    # A fab floor is a MINIMUM, so the number that can violate it is the
    # NARROWEST track and the SMALLEST via any net resolves to — the opposite
    # end of the distribution from the one geometry.py cares about (which asks
    # whether the WIDEST copper fits the grid). Both checks are real; they
    # just interrogate different tails.
    emit_drill = min([d for _, _, d in widths.values()] or [emit[2]])
    fab_note, fab_warnings = None, []
    if fab_profile.constrains:
        from geometry import CopperGeometry
        emitted = CopperGeometry(
            pitch_mm=args.pitch,
            track_width_mm=min([w for w, _, _ in widths.values()] or [emit[0]]),
            clearance_mm=res.geometry.clearance_mm if res.geometry else 0.2,
            via_size_mm=min([v for _, v, _ in widths.values()] or [emit[1]]))
        emit_violations = fab_mod.check(emitted, fab_profile,
                                        via_drill_mm=emit_drill)
        fab_note = fab_mod.summary_line(emitted, fab_profile,
                                        via_drill_mm=emit_drill,
                                        violations=emit_violations)
        fab_warnings = fab_mod.violation_warnings(emit_violations, fab_profile)
    # route_board checked the copper it MODELLED; the block above checks the
    # copper about to be WRITTEN. When they reach the same verdict, say it
    # once — a duplicated warning teaches the reader to skim warnings.
    seen = set(fab_warnings)
    fab_warnings += [w for w in (getattr(res, "fab_warnings", None) or [])
                     if w not in seen]

    write_routed_copy(args.board, args.out, tracks, vias, brd.nets,
                      track_width_mm=emit[0], via_size_mm=emit[1],
                      via_drill_mm=emit[2], widths=widths)
    failed_nets = {n for n, _ in res.failed}
    routable = set(res.net_paths) | failed_nets
    print(f"nets        : {len(routable)} routable | "
          f"{len(routable - failed_nets)} fully routed | "
          f"{len(failed_nets)} with failures")
    if getattr(res, "geometry_note", None):
        print(f"geometry    : {res.geometry_note}")
    if fab_note:
        print(f"fab         : {fab_note}")
    for line in fab_lines:
        print(line)
    for w in (getattr(res, "geometry_warnings", None) or []):
        print(f"WARNING     : {w}")
    for w in fab_warnings:
        print(f"WARNING     : {w}")
    print(f"wrote       : {args.out}")
    print(f"net classes : {pro or 'none (emitter defaults)'}")
    print(f"tracks      : {len(tracks)} appended ({len(brd.tracks)} already in file)")
    print(f"vias        : {len(vias)} appended ({len(brd.vias)} already in file)")
    if res.failed:
        print(f"failed nets : {len(res.failed)} (copy contains the routed subset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
