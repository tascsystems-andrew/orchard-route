"""L1: the copper geometry contract — what pitch does this board's copper need?

The lattice models WHERE copper goes. It does not, by itself, model HOW BIG
that copper is: a node is a point, but the copper that lands on it is a track
of `track_width` mm or a via of `via_size` mm. Every clearance violation the
router creates traces back to that gap. This module is the one place that
turns (track_width, clearance, via_size, pitch) into the four numbers that
decide what the router is allowed to emit, and it exists so the tool can
STATE its own geometric limits on every run instead of discovering them in
DRC afterwards.

The arithmetic, all centre-to-centre unless said otherwise:

  orthogonal track-track   two tracks on adjacent nodes, both axis-aligned:
                           edge gap = pitch - track_width, so a legal board
                           needs   pitch >= track_width + clearance.

  diagonal track-track     a 45-degree segment passes a diagonally-adjacent
                           node at pitch/sqrt(2). Foreign copper centred
                           there leaves edge gap pitch/sqrt(2) - track_width,
                           so 45s are legal only when
                           pitch >= sqrt(2) * (track_width + clearance).

  via-track                via edge to track edge:
                           via_size/2 + track_width/2 + clearance.

  via-via                  via edge to via edge:
                           via_size + clearance.

A 0.6 mm via does not fit a 0.5 mm grid beside anything: 0.3 + 0.125 + 0.2 =
0.625 > 0.5, and 0.6 + 0.2 = 0.8 > 0.5. That is why via exclusion is a
neighbourhood claim in the router's usage model rather than a node property.
"""
import math
from dataclasses import dataclass


def _mm(v):
    """A copper dimension, printed without losing digits that matter.

    Two decimals for the ordinary case (0.25, 0.60) and full precision when
    rounding would misstate the number: a fab profile's 0.1524 mm minimum
    track printed as `0.15` invites a hand-check against copper that is not
    0.15 mm. The derived `needs` figures stay at 2dp — they are comparisons,
    not the numbers anyone measures."""
    return f"{v:.2f}" if abs(round(v, 2) - v) < 1e-9 \
        else f"{v:.6f}".rstrip("0")


def halo_offsets(pitch_mm, radius_mm):
    """Planar (dx, dy) grid offsets whose node centres lie within radius_mm.

    Always includes (0, 0). Returned sorted, so the router's node claims are
    deterministic run to run."""
    if radius_mm <= 0 or pitch_mm <= 0:
        return [(0, 0)]
    k = int(math.floor(radius_mm / pitch_mm + 1e-9))
    r2 = (radius_mm + 1e-9) ** 2
    return sorted((dx, dy)
                  for dx in range(-k, k + 1)
                  for dy in range(-k, k + 1)
                  if (dx * dx + dy * dy) * pitch_mm * pitch_mm <= r2)


@dataclass(frozen=True)
class NetClass:
    """One project net class's resolved copper, for the nets this run routes.

    Clearance is per CLASS, not per board. A tube amp's plate nodes need
    0.4-1.0 mm of creepage (IPC-2221B, coated) while its logic is fine at
    0.15 mm; a router that resolves ONE clearance and applies it everywhere
    turns a declared HV class into decoration. Every number the contract
    prints about spacing is derived from these.
    """
    name: str
    clearance_mm: float
    track_width_mm: float
    via_size_mm: float
    net_count: int = 0

    @property
    def orthogonal_pitch_mm(self):
        """Smallest pitch at which two of THIS class's adjacent-node tracks
        clear each other."""
        return self.track_width_mm + self.clearance_mm

    @property
    def track_exclusion_mm(self):
        """Radius this class's copper claims around its own centreline: no
        FOREIGN centreline may come closer, or the edge gap falls below the
        class's clearance. Identical in form to lattice.pad_ring_nodes'
        inflate (clearance + width/2) — a moving ring around a track rather
        than a static one around a pad."""
        return self.clearance_mm + self.track_width_mm / 2.0

    @property
    def via_exclusion_mm(self):
        """Radius this class's VIA claims, on every layer."""
        return self.via_size_mm / 2.0 + self.track_width_mm / 2.0 + \
            self.clearance_mm

    def enforced_separation_mm(self, pitch_mm):
        """What the exclusion halo actually delivers on this grid: the halo
        claims every node within track_exclusion_mm, so the nearest node
        foreign copper may occupy is the next grid step beyond it. The grid
        can only quantise UPWARD, which is why an impossible class is
        over-spaced rather than under-spaced — and why it costs routability
        instead of producing a violation."""
        if pitch_mm <= 0:
            return 0.0
        return (math.floor(self.track_exclusion_mm / pitch_mm + 1e-9) + 1) \
            * pitch_mm


@dataclass(frozen=True)
class CopperGeometry:
    """Resolved copper dimensions for one board + pitch, and what they permit.

    track_width_mm / via_size_mm are the WIDEST the writer will emit (widths
    are per net class; the widest one sets the spacing the grid must support).
    clearance_mm is the project's Default-class clearance — the number KiCad's
    DRC will actually enforce.

    `classes` carries the SAME numbers per project net class, because a board
    with an HV class has no single clearance: the global clearance_mm above
    stays the Default class's (what DRC enforces on everything unclaimed),
    while spacing is decided per class. When it is empty the two views agree
    and every method here reduces to the old global arithmetic.

    copper_source / clearance_source are PROVENANCE, and they are part of the
    contract, not decoration. A geometry that states 0.15 mm while something
    downstream enforces 0.20 mm is worse than one that fails: the whole point
    of printing this line every run is that its numbers are checkable. So the
    resolver records where each number came from and summary() says it.
    """
    pitch_mm: float
    track_width_mm: float
    clearance_mm: float
    via_size_mm: float
    copper_source: str = ""
    clearance_source: str = ""
    classes: tuple = ()   # NetClass per project net class with routed nets.
                          # Empty for hand-built geometries and for projects
                          # with no readable classes — then every number below
                          # falls back to the single global one, exactly as
                          # before per-class clearance existed.

    # ── net classes ─────────────────────────────────────────────────────
    @property
    def governing_class(self):
        """The class whose clearance+width demands the coarsest pitch. That
        is the one the contract's required-pitch arithmetic must be stated
        for: the widest class governs, because a grid that cannot carry it
        cannot carry the board."""
        if not self.classes:
            return None
        return max(self.classes, key=lambda c: (c.orthogonal_pitch_mm, c.name))

    @property
    def multi_class(self):
        """True when the project's classes do NOT all ask the same clearance
        — the case a single global number cannot describe."""
        return len({round(c.clearance_mm, 9) for c in self.classes}) > 1

    def impossible_classes(self):
        """[(NetClass, required_pitch_mm)] for every class whose clearance
        this pitch cannot honour in the one-node track model, worst first.
        Empty when the grid carries every class."""
        out = [(c, c.orthogonal_pitch_mm) for c in self.classes
               if self.pitch_mm < c.orthogonal_pitch_mm - 1e-9]
        return sorted(out, key=lambda cn: -cn[1])

    @property
    def max_clearance_at_pitch_mm(self):
        """The widest clearance this pitch can carry at the widest track:
        pitch - track_width. Above it, no class fits."""
        return self.pitch_mm - self.track_width_mm

    # ── required pitches ────────────────────────────────────────────────
    @property
    def base_orthogonal_pitch_mm(self):
        """The DEFAULT-class rule: track_width + clearance, ignoring any
        wider class. Kept separate because the diagonal gate reads it — a
        45-degree cut is checked against real occupancy (which includes every
        net's exclusion halo) at smoothing time, so one HV class must not
        switch the whole board to 90-degree geometry."""
        return self.track_width_mm + self.clearance_mm

    @property
    def orthogonal_pitch_mm(self):
        """Smallest pitch at which two axis-aligned tracks on adjacent nodes
        still clear each other — stated for the WIDEST class, since that is
        the one that governs whether this grid can carry this board."""
        need = self.base_orthogonal_pitch_mm
        if self.classes:
            need = max(need, max(c.orthogonal_pitch_mm for c in self.classes))
        return need

    @property
    def diagonal_pitch_mm(self):
        """Smallest pitch at which a 45-degree segment clears foreign copper
        on a diagonally-adjacent node."""
        return math.sqrt(2.0) * self.base_orthogonal_pitch_mm

    # ── via exclusion radii ─────────────────────────────────────────────
    @property
    def via_track_exclusion_mm(self):
        """No FOREIGN track centreline may come this close to a via centre."""
        return self.via_size_mm / 2.0 + self.track_width_mm / 2.0 + \
            self.clearance_mm

    @property
    def via_via_exclusion_mm(self):
        """No FOREIGN via centre may come this close to a via centre."""
        return self.via_size_mm + self.clearance_mm

    @property
    def via_exclusion_mm(self):
        """The single radius the router claims around a via.

        The router's usage model is symmetric: a via claims every node within
        this radius on EVERY layer for its own net, and a node claimed by two
        nets is an overuse the negotiation resolves. Symmetry means the
        via-track rule sets the radius (via_track_exclusion_mm) and the
        via-via rule comes out CONSERVATIVE for free — two vias of different
        nets must be far enough apart that their claim disks share no node,
        which is stricter than via_via_exclusion_mm. See
        via_via_enforced_mm for the number that costs routability.
        """
        return self.via_track_exclusion_mm

    def halo_offsets(self):
        """Planar (dx, dy) node offsets a via claims: every grid offset whose
        centre lies within via_exclusion_mm of the via's node. The router
        applies these on EVERY layer (a via's barrel is on every layer)."""
        return halo_offsets(self.pitch_mm, self.via_exclusion_mm)

    def via_via_enforced_mm(self):
        """Centre-to-centre separation the symmetric claim actually enforces
        between two DIFFERENT nets' vias on this pitch: the smallest grid
        distance at which two claim disks share no node. Computed from the
        offset set itself, not from a formula, so it cannot drift from what
        the router does."""
        offs = set(self.halo_offsets())
        if not offs:
            return 0.0
        span = max(max(abs(dx), abs(dy)) for dx, dy in offs)
        best = float("inf")
        for sx in range(0, 2 * span + 2):
            for sy in range(0, 2 * span + 2):
                if sx == 0 and sy == 0:
                    continue
                if any((dx - sx, dy - sy) in offs for dx, dy in offs):
                    continue      # disks share a node: still a conflict
                best = min(best, math.hypot(sx, sy) * self.pitch_mm)
        return best

    # ── verdicts ────────────────────────────────────────────────────────
    @property
    def orthogonal_ok(self):
        return self.pitch_mm >= self.orthogonal_pitch_mm - 1e-9

    @property
    def diagonals_ok(self):
        return self.pitch_mm >= self.diagonal_pitch_mm - 1e-9

    def clearance_text(self):
        """The clearance field of the contract line.

        ONE number when every class agrees; otherwise the per-class numbers,
        because a board with an HV class has no single clearance and printing
        one is the lie. Format: `0.15 default / 1 HV_PA (2 classes)`. The
        first token stays the Default-class number, so the line reads the
        same way it always did for the boards that have only one class."""
        if not self.multi_class:
            return _mm(self.clearance_mm)
        others = [c for c in sorted(self.classes,
                                    key=lambda c: (c.clearance_mm, c.name))
                  if abs(c.clearance_mm - self.clearance_mm) > 1e-9]
        txt = f"{_mm(self.clearance_mm)} default"
        for c in others[:3]:
            txt += f" / {_mm(c.clearance_mm)} {c.name}"
        if len(others) > 3:
            txt += f" / +{len(others) - 3} more"
        return f"{txt} ({len(self.classes)} classes)"

    def summary(self):
        """One line stating the contract, for every run's stats block."""
        src = []
        if self.copper_source:
            src.append(f"copper {self.copper_source}")
        if self.clearance_source:
            src.append(f"clearance {self.clearance_source}")
        gov = self.governing_class
        # Name the class the required pitch is stated FOR, or the reader
        # cannot tell whose clearance produced the number.
        gov_note = ""
        if gov is not None and self.multi_class:
            gov_note = f", widest class {gov.name}"
        return (
            f"pitch {self.pitch_mm:.2f}mm | "
            f"track {_mm(self.track_width_mm)} "
            f"clearance {self.clearance_text()} "
            f"via {_mm(self.via_size_mm)} | "
            f"orthogonal {'OK' if self.orthogonal_ok else 'VIOLATED'} "
            f"(needs {self.orthogonal_pitch_mm:.2f}{gov_note}) | "
            f"diagonals {'ON' if self.diagonals_ok else 'OFF'} "
            f"(need {self.diagonal_pitch_mm:.2f}) | "
            f"vias exclude r={self.via_exclusion_mm:.2f}mm "
            f"(via-via enforced {self.via_via_enforced_mm():.2f}, "
            f"needs {self.via_via_exclusion_mm:.2f})"
            + (" | source: " + "; ".join(src) if src else ""))

    def warnings(self):
        """Loud, numeric complaints about a pitch too fine for its own copper.
        Empty when the geometry is self-consistent."""
        out = []
        if not self.orthogonal_ok:
            gov = self.governing_class
            gt = gov.track_width_mm if gov is not None else self.track_width_mm
            gc = gov.clearance_mm if gov is not None else self.clearance_mm
            whose = f" (net class {gov.name})" if gov is not None \
                and self.multi_class else ""
            out.append(
                f"ILLEGAL GEOMETRY: pitch {self.pitch_mm:.3f}mm is finer than "
                f"track_width {gt:.3f} + clearance "
                f"{gc:.3f} = {self.orthogonal_pitch_mm:.3f}mm{whose} — "
                f"two tracks on adjacent nodes leave "
                f"{self.pitch_mm - gt:.3f}mm, "
                f"{gc:.3f}mm required. Every orthogonal "
                f"neighbour pair the router emits is a DRC clearance "
                f"violation. Widen the pitch or narrow the net class.")
        # Per-class honest failure. A class this pitch cannot carry is NOT
        # quietly routed at the Default class's clearance: it is named, its
        # working pitch is stated, and its copper is held apart by an
        # exclusion halo that the grid can only quantise upward — which costs
        # routability and still cannot express the exact number.
        if len(self.classes) > 1:
            for cls, need in self.impossible_classes():
                out.append(
                    f"NET CLASS {cls.name} CANNOT BE HONOURED AT THIS PITCH: "
                    f"its {cls.clearance_mm:.3f}mm clearance with a "
                    f"{cls.track_width_mm:.3f}mm track needs pitch >= "
                    f"{need:.3f}mm and this run uses {self.pitch_mm:.3f}mm — "
                    f"at {self.pitch_mm:.3f}mm the widest clearance any class "
                    f"can ask for is {self.pitch_mm - cls.track_width_mm:.3f}mm. "
                    f"Its {cls.net_count} net(s) are NOT routed at the Default "
                    f"clearance: they claim a {cls.track_exclusion_mm:.3f}mm "
                    f"exclusion halo, which this grid rounds up to "
                    f"{cls.enforced_separation_mm(self.pitch_mm):.3f}mm of "
                    f"enforced separation and pays for in routability. Route "
                    f"these nets by hand, or re-run at "
                    f"--pitch {need:.3f}.")
        if not self.diagonals_ok:
            out.append(
                f"diagonals disabled: pitch {self.pitch_mm:.3f}mm < "
                f"sqrt(2) * ({self.track_width_mm:.3f} + "
                f"{self.clearance_mm:.3f}) = {self.diagonal_pitch_mm:.3f}mm — "
                f"a 45-degree cut would pass a diagonally-adjacent node at "
                f"{self.pitch_mm / math.sqrt(2.0):.3f}mm, leaving "
                f"{self.pitch_mm / math.sqrt(2.0) - self.track_width_mm:.3f}mm "
                f"edge gap against {self.clearance_mm:.3f}mm required. "
                f"Emitting 90-degree geometry instead.")
        return out


def resolve_net_classes(board_path, nets, widths=None, clearance_mm=None,
                        track_width_mm=None, via_size_mm=None):
    """(net_code -> clearance_mm, tuple[NetClass]) from the project file.

    ONE read of the project's net classes, reusing writeback's resolver — the
    same machinery that already decides which class a net's track width comes
    from, so a net's clearance and its width can never disagree about its
    class. Returns ({}, ()) when the board has no project file: then there is
    exactly one clearance and the global model is the honest one.

    `widths` is the run's resolved net_code -> (track, via, drill) restricted
    to the nets it will actually route. The class table is built from those
    nets only: a class with no routed net emits no copper, and letting it
    widen every halo on the board would cost routability for copper that is
    never written (the same rule _widths_of_routed_nets applies to widths).
    The per-net clearance map covers EVERY net, because an unrouted net's
    pads still grow clearance rings.
    """
    from writeback import (DEFAULT_TRACK_MM, DEFAULT_VIA_MM,
                           load_net_class_clearances, load_net_class_names,
                           project_file_for)
    pro = project_file_for(board_path)
    if not pro or not nets:
        return {}, ()
    fallback = 0.2 if clearance_mm is None else float(clearance_mm)
    try:
        clears = load_net_class_clearances(pro, nets, fallback)
        names = load_net_class_names(pro, nets)
    except (OSError, ValueError):
        return {}, ()

    codes = [c for c in (widths if widths else nets) if c > 0]
    grouped = {}
    for code in codes:
        name = names.get(code, "Default")
        tw, vs = (widths or {}).get(
            code, (track_width_mm or DEFAULT_TRACK_MM,
                   via_size_mm or DEFAULT_VIA_MM, 0.0))[:2]
        clr = clears.get(code, fallback)
        got = grouped.get(name)
        if got is None:
            grouped[name] = [clr, tw, vs, 1]
        else:
            # A class is one clearance by construction; widths can still vary
            # per net through --width-map, so the class takes its WIDEST.
            got[1] = max(got[1], tw)
            got[2] = max(got[2], vs)
            got[3] += 1
    classes = tuple(sorted(
        (NetClass(name=n, clearance_mm=v[0], track_width_mm=v[1],
                  via_size_mm=v[2], net_count=v[3])
         for n, v in grouped.items()), key=lambda c: c.name))
    return clears, classes


def resolve_board_geometry(board_path, pitch_mm, nets, clearance_mm=None,
                           track_width_mm=None, via_size_mm=None,
                           widths=None, max_width_mm=None,
                           widths_note="", clearance_note=""):
    """CopperGeometry for a real board: the WIDEST track and via any net
    resolves to, and the clearance DRC will actually enforce.

    `widths` is the caller's FULLY RESOLVED net_code -> (track, via, drill)
    map — project net classes plus --width-map plus any --max-width cap,
    i.e. exactly the numbers writeback is about to emit — restricted to the
    nets that will actually be routed. When it is given, the contract is
    derived from it and nothing is re-read from the project, so the modelled
    geometry cannot drift from the emitted copper. Widths differ per net, so
    there is no single global geometry: the halo and the contract take the
    WORST (largest) copper, never an average and never the Default class,
    and copper_source records that so the line says what it did.

    When `widths` is absent the project's net classes are read here, as
    before, for callers (tests, the region solver) that do not emit copper.

    A hardcoded default is the LAST resort for clearance, never a silent
    override of a resolvable number: whatever survives lands in
    clearance_source, which summary() prints.

    writeback is imported lazily: it is the L0 output module and this is L1;
    the dependency is one-way at call time only, never at import time.
    Explicit arguments override anything resolved from the project.
    """
    from lattice import DEFAULT_CLEARANCE_MM, default_copper_rules
    from writeback import (DEFAULT_TRACK_MM, DEFAULT_VIA_MM,
                           load_net_class_widths, project_file_for)

    pro_clearance, pro_width = default_copper_rules(board_path)
    pro_path = project_file_for(board_path)
    if clearance_mm is not None:
        clearance, clr_src = clearance_mm, (clearance_note or "caller argument")
    elif pro_path:
        clearance, clr_src = pro_clearance, "project Default net class"
    else:
        clearance, clr_src = pro_clearance, "built-in default (no project file)"
    if clearance <= 0:
        # default_copper_rules never returns <= 0, so this is a caller asking
        # for 0 (the clearance MODEL off). The contract still needs a number
        # for its arithmetic; say plainly that it is a fallback, not a rule
        # anyone stated.
        clearance = DEFAULT_CLEARANCE_MM
        clr_src = (f"built-in default {DEFAULT_CLEARANCE_MM} mm — caller "
                   f"asked for {clearance_mm}, nothing else resolvable")

    cap = pitch_mm if max_width_mm is None else max_width_mm
    if widths:
        width = max(w for w, _, _ in widths.values())
        via = max(v for _, v, _ in widths.values())
        src = widths_note or "resolved net widths"
        cop_src = (f"{src}, widest of {len(widths)} net(s)"
                   if len(widths) > 1 else src)
    else:
        width, via, cop_src = pro_width, DEFAULT_VIA_MM, "emitter defaults"
        if pro_path and nets:
            try:
                loaded = load_net_class_widths(pro_path, nets)
            except (OSError, ValueError):
                loaded = {}
            if loaded:
                width = max(w for w, _, _ in loaded.values())
                via = max(v for _, v, _ in loaded.values())
                cop_src = (f"project net classes, widest of {len(loaded)} net(s)"
                           if len(loaded) > 1 else "project net classes")
        if not width:
            width = DEFAULT_TRACK_MM
        width = min(width, cap)       # writeback's emit-time cap

    if track_width_mm is not None:
        width, cop_src = track_width_mm, "caller argument"
    if via_size_mm is not None:
        via = via_size_mm
        cop_src = "caller argument" if track_width_mm is not None \
            else cop_src + " (via from caller argument)"

    # The per-class view. Explicit caller arguments describe ONE geometry, so
    # they retire the class table rather than being mixed with it: a run told
    # "clearance is 0.3" has one clearance, and printing per-class numbers it
    # is not going to enforce would be the same lie in a new place.
    classes = ()
    if clearance_mm is None:
        _clears, classes = resolve_net_classes(
            board_path, nets, widths=widths, clearance_mm=clearance,
            track_width_mm=width, via_size_mm=via)

    return CopperGeometry(
        pitch_mm=float(pitch_mm),
        track_width_mm=float(width),
        clearance_mm=float(clearance),
        via_size_mm=float(via),
        copper_source=cop_src,
        clearance_source=clr_src,
        classes=classes)
