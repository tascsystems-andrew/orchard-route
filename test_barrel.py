"""Through-hole via full-barrel legalization (_barrel_legalize).

A plated via occupies its (x,y) column on EVERY copper layer, but the lattice
models a via as adjacent-layer hops, so a via that changes only F->In1 never
reserves the In2/B barrel. A foreign track on a skipped layer then runs through
the emitted barrel — node occupancy is blind to it (measured as -0.425 mm
track-via shorts on Voxy region 1). _barrel_legalize drops such a via's
connection, fail-clean. These checks pin the four cases that matter."""
from lattice import build_lattice
from pathfinder import _barrel_legalize, _barrel_block_step, _Conn

LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
W, H, L = 6, 3, 4
lat = build_lattice(W, H, L, pitch_mm=0.6, layer_names=LAYERS)


def nd(x, y, l):
    return lat.node(x, y, l)


def via_conn(net, x, y, l0, l1):
    """A connection whose path is a single via at (x,y) from layer l0 to l1."""
    a, b = nd(x, y, l0), nd(x, y, l1)
    return _Conn(net=net, a_nodes=(a,), b_nodes=(b,), path=[a, b])


def track_conn(net, y, l, x0, x1):
    """A same-layer track from (x0,y) to (x1,y) on layer l."""
    path = [nd(x, y, l) for x in range(x0, x1 + 1)]
    return _Conn(net=net, a_nodes=(path[0],), b_nodes=(path[-1],), path=path)


fails = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(name)


# 1. Foreign copper on a SKIPPED barrel layer (via F->In1, foreign on In2) drops.
c = via_conn(1, 2, 1, 0, 1)
owner = {nd(2, 1, 2): 2}                       # net 2 on In2, the barrel layer skipped
dropped = _barrel_legalize(lat, [c], owner)
check("skipped-layer barrel crossing drops the via connection",
      dropped == 1 and c.path is None, f"dropped={dropped}")

# 2. Same net in the column is NOT a crossing (that is the via doing its job).
c = via_conn(1, 2, 1, 0, 1)
owner = {nd(2, 1, 2): 1}                        # same net 1 lower in the column
dropped = _barrel_legalize(lat, [c], owner)
check("same-net column is kept", dropped == 0 and c.path is not None)

# 3. An empty column keeps the via.
c = via_conn(1, 2, 1, 0, 3)                     # full-stack via, nothing around it
dropped = _barrel_legalize(lat, [c], {})
check("uncontested via is kept", dropped == 0 and c.path is not None)

# 4. Cross-connection: a foreign TRACK threading the barrel drops the VIA, not
#    the track (the track is legal on its own layer; the barrel is the intruder).
via = via_conn(1, 2, 1, 0, 1)                   # net 1 via F->In1 at (2,1)
trk = track_conn(2, 1, 2, 1, 3)                 # net 2 track on In2 through (2,1)
dropped = _barrel_legalize(lat, [via, trk], {})
check("foreign track through barrel drops the via, keeps the track",
      dropped == 1 and via.path is None and trk.path is not None,
      f"dropped={dropped}")

# 5. A via that only touches layers with no foreign copper survives even when a
#    foreign net sits in a DIFFERENT column (no false positive from bucketing).
c = via_conn(1, 2, 1, 0, 1)
owner = {nd(4, 1, 2): 2}                        # net 2 in column x=4, not x=2
dropped = _barrel_legalize(lat, [c], owner)
check("foreign copper in a different column does not drop", dropped == 0)

# 6. extra_allow (input-board pad overlap the router opened for this net) is
#    honored — a via in an allowed column is NOT dropped even with foreign copper
#    there, matching net_mask's overlap policy (else legal nets fail on nested
#    SMD-on-THT pads).
c = via_conn(1, 2, 1, 0, 1)
owner = {nd(2, 1, 2): 2}                        # foreign net 2 in the via's column
drop_no_allow = _barrel_legalize(lat, [via_conn(1, 2, 1, 0, 1)], owner)
drop_allow = _barrel_legalize(lat, [c], owner,
                              extra_allow={1: [nd(2, 1, 0)]})  # net 1 allowed there
check("extra_allow column is exempt (but drops without it)",
      drop_no_allow == 1 and drop_allow == 0 and c.path is not None)

# 7. The block-step (negotiation-integrated reroute) rips the contested via,
#    records its column as a per-net keep-out, and invalidates the cached mask
#    so the loop reroutes it around the barrel instead of dropping it.
c = via_conn(1, 2, 1, 0, 1)
owner = {nd(2, 1, 2): 2}
barrel_block, masks = {}, {1: "stale-cached-mask"}
moved = _barrel_block_step(lat, [c], owner, None, barrel_block, masks)
col = 1 * W + 2                                  # planar (y=1, x=2)
check("block-step rips, records the column, invalidates the mask",
      moved == 1 and c.path is None
      and col in barrel_block.get(1, set()) and 1 not in masks)

print(f"\nRESULT: {'PASS' if not fails else 'FAIL'} "
      f"({7 - len(fails)}/7)")
raise SystemExit(1 if fails else 0)
