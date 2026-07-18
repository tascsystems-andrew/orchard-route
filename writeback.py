"""L0 output: emit a routed COPY of a .kicad_pcb that KiCad will open.

The original board file is never touched: the file's text is read, the
router's tracks and vias are appended as (segment ...) / (via ...) nodes just
before the final closing paren, and the result is written to a DIFFERENT
path. write_routed_copy refuses an out_path that resolves into the source
board's own directory — routed copies belong in the router's out/ tree, not
next to the user's live project.

Two format facts, checked empirically against the Voxy boards, drive the
emitter:

- KiCad 10 (version 20260206) segments/vias reference nets BY NAME only —
  (net "Audio Input P1"), (net "") for the unconnected net — because the
  root net table is gone. Legacy files (root declares (net N "name")) keep
  numeric (net N) references; board.py preserves those file codes in
  Board.nets, so the caller's net_code passes straight through. Which style
  to emit is decided per file from its own root, never assumed.
- Every KiCad 8+ node carries a (uuid "..."); fresh uuid4s are emitted iff
  the file's own segment/via nodes carry them (falling back to the version
  header when the file has no tracks yet to inspect).

Emitted nodes copy the KiCad 10 pretty-printer shape exactly — one attribute
per line, tab indentation, attribute order start/end/width/layer/net/uuid
(segments) and at/size/drill/layers/net/uuid (vias) — and numbers are
rounded to 6 decimals with trailing zeros stripped, as pcbnew writes them.
Vias span the file's outermost copper layers (F.Cu .. B.Cu).

CLI: python writeback.py BOARD.kicad_pcb OUT.kicad_pcb [--pitch 0.5]
     [--layers F.Cu,B.Cu]
"""
import os
import uuid

from board import parse_sexpr, QStr


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


def write_routed_copy(board_path, out_path, tracks, vias, nets,
                      track_width_mm=0.25, via_size_mm=0.6, via_drill_mm=0.3):
    """Append the router's copper to a copy of board_path, written to out_path.

    tracks: (x1_mm, y1_mm, x2_mm, y2_mm, layer_name, net_code) as produced by
    pathfinder.paths_to_tracks; vias: (x_mm, y_mm, net_code); nets: Board.nets
    (net_code -> name). The source file is read-only; out_path must not
    resolve to the source file or into its directory.
    """
    src = os.path.realpath(board_path)
    dst = os.path.realpath(out_path)
    if dst == src or os.path.dirname(dst) == os.path.dirname(src):
        raise ValueError(
            f"refusing to write {out_path!r} into the source board's own "
            f"directory — routed copies go elsewhere (e.g. out/)")

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

    parts = []
    for x1, y1, x2, y2, layer, code in tracks:
        parts.append(
            "\t(segment\n"
            f"\t\t(start {_fmt(x1)} {_fmt(y1)})\n"
            f"\t\t(end {_fmt(x2)} {_fmt(y2)})\n"
            f"\t\t(width {_fmt(track_width_mm)})\n"
            f"\t\t(layer {_quote(layer)})\n"
            f"\t\t{net_attr(code)}\n"
            + uuid_line() +
            "\t)\n")
    for x, y, code in vias:
        parts.append(
            "\t(via\n"
            f"\t\t(at {_fmt(x)} {_fmt(y)})\n"
            f"\t\t(size {_fmt(via_size_mm)})\n"
            f"\t\t(drill {_fmt(via_drill_mm)})\n"
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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Route a KiCad board and write a routed copy")
    ap.add_argument("board")
    ap.add_argument("out")
    ap.add_argument("--pitch", type=float, default=0.5)
    ap.add_argument("--layers", default="F.Cu,B.Cu")
    args = ap.parse_args(argv)
    layers = [s.strip() for s in args.layers.split(",") if s.strip()]

    from pathfinder import route_board, paths_to_tracks
    brd, lat, res = route_board(args.board, pitch_mm=args.pitch,
                                layer_names=layers)
    tracks, vias = paths_to_tracks(lat, res.net_paths)
    write_routed_copy(args.board, args.out, tracks, vias, brd.nets)
    print(f"wrote       : {args.out}")
    print(f"tracks      : {len(tracks)} appended ({len(brd.tracks)} already in file)")
    print(f"vias        : {len(vias)} appended ({len(brd.vias)} already in file)")
    if res.failed:
        print(f"failed nets : {len(res.failed)} (copy contains the routed subset)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
