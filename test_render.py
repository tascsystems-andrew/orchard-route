"""Tests for render.py: board-free fake scene, geometry conversion, real board.

Run: .venv/bin/python test_render.py
Self-contained — builds a hand-made RouteResult rather than waiting on
pathfinder.py (being written concurrently against the same interface).
"""
from dataclasses import dataclass, field
import os
import xml.etree.ElementTree as ET

from board import Pad
from lattice import build_lattice
from render import RouteResult, paths_to_tracks, render_from_cli, render_svg

OUT = "/Users/andrew/Code/mlx-router/out/test_render.svg"
HIFI = "/Users/andrew/Documents/Guitar/Voxy/Voxy/hifi tube pre.kicad_pcb"


@dataclass
class FakeBoard:
    origin_mm: tuple = (0.0, 0.0)
    size_mm: tuple = (11.0, 11.0)
    pads: list = field(default_factory=list)


def make_scene():
    lat = build_lattice(12, 12, 2, pitch_mm=1.0, origin_mm=(0.0, 0.0),
                        layer_names=["F.Cu", "B.Cu"])
    n = lat.node
    # Net 1: east on F.Cu (horizontal layer), via, south on B.Cu (vertical layer).
    p1 = [n(1, 1, 0), n(2, 1, 0), n(3, 1, 0), n(4, 1, 0),
          n(4, 1, 1), n(4, 2, 1), n(4, 3, 1), n(4, 4, 1), n(4, 5, 1)]
    # Net 2: short hop crossing layers the other way.
    p2 = [n(7, 8, 0), n(8, 8, 0), n(9, 8, 0), n(9, 8, 1), n(9, 7, 1), n(9, 6, 1)]
    result = RouteResult(
        net_paths={1: [p1], 2: [p2]},
        failed=[(3, "no legal path: target pad enclosed by net 1 keepout")],
        conflicts=[],
        iterations=4,
        overuse_curve=[12, 5, 1, 0],
        wirelength_mm=13.0,
        via_count=2,
        seconds={"sssp": 0.02, "backtrace": 0.001},
    )
    brd = FakeBoard(pads=[
        Pad(1.0, 1.0, ["F.Cu"], 1, "N1", 1.2, 0.8, False, 0.0),
        Pad(4.0, 5.0, ["B.Cu"], 1, "N1", 1.2, 0.8, False, 0.0),
        Pad(7.0, 8.0, ["F.Cu", "B.Cu"], 2, "N2", 1.6, 1.6, True, 0.8),
        Pad(9.0, 6.0, ["B.Cu"], 2, "N2", 1.2, 0.8, False, 0.0),
        Pad(2.0, 9.0, ["F.Cu"], 3, "N3", 1.0, 1.0, False, 0.0),
        Pad(10.0, 2.0, ["F.Cu"], 3, "N3", 1.0, 1.0, False, 0.0),
    ])
    return brd, lat, result


def test_paths_to_tracks():
    brd, lat, result = make_scene()
    tracks, vias = paths_to_tracks(lat, result.net_paths)
    # Each path is one merged segment per layer: 2 nets x 2 layers = 4 tracks.
    assert len(tracks) == 4, tracks
    assert (1.0, 1.0, 4.0, 1.0, "F.Cu", 1) in tracks
    assert (4.0, 1.0, 4.0, 5.0, "B.Cu", 1) in tracks
    assert vias == [(4.0, 1.0, 1), (9.0, 8.0, 2)], vias
    print("paths_to_tracks: PASS")


def test_render_fake_scene():
    brd, lat, result = make_scene()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    render_svg(brd, lat, result, OUT, title="fake 12x12x2 scene")
    assert os.path.exists(OUT)

    tree = ET.parse(OUT)  # raises if not well-formed XML
    tags = [el.tag.split("}")[-1] for el in tree.iter()]
    n_lines = sum(1 for t in tags if t in ("line", "polyline"))
    n_circles = tags.count("circle")
    assert n_lines >= 2, f"expected >= 2 line/polyline, got {n_lines}"
    assert n_circles >= 1, f"expected >= 1 circle, got {n_circles}"
    size = os.path.getsize(OUT)
    print(f"render fake scene: PASS  ({size} bytes, {n_lines} line/polyline, "
          f"{n_circles} circle, {tags.count('rect')} rect, {tags.count('text')} text)")


def test_render_from_cli():
    if not os.path.exists(HIFI):
        print("render_from_cli: SKIP (hifi board not found)")
        return
    out = "/Users/andrew/Code/mlx-router/out/test_render_board.svg"
    render_from_cli(HIFI, out, 1.0, ["F.Cu", "B.Cu"])
    ET.parse(out)
    print(f"render_from_cli: PASS  ({os.path.getsize(out)} bytes)")


if __name__ == "__main__":
    test_paths_to_tracks()
    test_render_fake_scene()
    test_render_from_cli()
