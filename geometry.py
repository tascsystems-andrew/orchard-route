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
class CopperGeometry:
    """Resolved copper dimensions for one board + pitch, and what they permit.

    track_width_mm / via_size_mm are the WIDEST the writer will emit (widths
    are per net class; the widest one sets the spacing the grid must support).
    clearance_mm is the project's Default-class clearance — the number KiCad's
    DRC will actually enforce.
    """
    pitch_mm: float
    track_width_mm: float
    clearance_mm: float
    via_size_mm: float

    # ── required pitches ────────────────────────────────────────────────
    @property
    def orthogonal_pitch_mm(self):
        """Smallest pitch at which two axis-aligned tracks on adjacent nodes
        still clear each other."""
        return self.track_width_mm + self.clearance_mm

    @property
    def diagonal_pitch_mm(self):
        """Smallest pitch at which a 45-degree segment clears foreign copper
        on a diagonally-adjacent node."""
        return math.sqrt(2.0) * self.orthogonal_pitch_mm

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

    def summary(self):
        """One line stating the contract, for every run's stats block."""
        return (
            f"pitch {self.pitch_mm:.2f}mm | "
            f"track {self.track_width_mm:.2f} clearance {self.clearance_mm:.2f} "
            f"via {self.via_size_mm:.2f} | "
            f"orthogonal {'OK' if self.orthogonal_ok else 'VIOLATED'} "
            f"(needs {self.orthogonal_pitch_mm:.2f}) | "
            f"diagonals {'ON' if self.diagonals_ok else 'OFF'} "
            f"(need {self.diagonal_pitch_mm:.2f}) | "
            f"vias exclude r={self.via_exclusion_mm:.2f}mm "
            f"(via-via enforced {self.via_via_enforced_mm():.2f}, "
            f"needs {self.via_via_exclusion_mm:.2f})")

    def warnings(self):
        """Loud, numeric complaints about a pitch too fine for its own copper.
        Empty when the geometry is self-consistent."""
        out = []
        if not self.orthogonal_ok:
            out.append(
                f"ILLEGAL GEOMETRY: pitch {self.pitch_mm:.3f}mm is finer than "
                f"track_width {self.track_width_mm:.3f} + clearance "
                f"{self.clearance_mm:.3f} = {self.orthogonal_pitch_mm:.3f}mm — "
                f"two tracks on adjacent nodes leave "
                f"{self.pitch_mm - self.track_width_mm:.3f}mm, "
                f"{self.clearance_mm:.3f}mm required. Every orthogonal "
                f"neighbour pair the router emits is a DRC clearance "
                f"violation. Widen the pitch or narrow the net class.")
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


def resolve_board_geometry(board_path, pitch_mm, nets, clearance_mm=None,
                           track_width_mm=None, via_size_mm=None):
    """CopperGeometry for a real board: the WIDEST track and via any net class
    resolves to (capped at the pitch for tracks, exactly as writeback caps at
    emit time — copper wider than the pitch is refused there), and the
    project's Default-class clearance, which is the number DRC enforces.

    writeback is imported lazily: it is the L0 output module and this is L1;
    the dependency is one-way at call time only, never at import time.
    Explicit arguments override anything resolved from the project.
    """
    from lattice import DEFAULT_CLEARANCE_MM, default_copper_rules
    from writeback import (DEFAULT_TRACK_MM, DEFAULT_VIA_MM,
                           load_net_class_widths, project_file_for)

    pro_clearance, pro_width = default_copper_rules(board_path)
    clearance = clearance_mm if clearance_mm is not None else pro_clearance
    if clearance <= 0:
        clearance = DEFAULT_CLEARANCE_MM

    width, via = pro_width, DEFAULT_VIA_MM
    pro = project_file_for(board_path)
    if pro and nets:
        try:
            widths = load_net_class_widths(pro, nets)
        except (OSError, ValueError):
            widths = {}
        if widths:
            width = max(w for w, _, _ in widths.values())
            via = max(v for _, v, _ in widths.values())
    if not width:
        width = DEFAULT_TRACK_MM
    width = min(width, pitch_mm)      # writeback's emit-time cap

    return CopperGeometry(
        pitch_mm=float(pitch_mm),
        track_width_mm=float(track_width_mm if track_width_mm is not None
                             else width),
        clearance_mm=float(clearance),
        via_size_mm=float(via_size_mm if via_size_mm is not None else via))
