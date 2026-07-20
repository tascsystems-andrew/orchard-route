# Finding: HV boards need PRIORITY-TIER routing (most-constrained net class first), and it's feasible today

**Date:** 2026-07-20. **From:** AI agent, out of the Voxy trial-board route.
**Companion to:** the placement findings (courtyard-model, placement-fidelity).

## The evidence — one-pass routing of an HV region cannot converge

Voxy region 1 (main analog, 890 pads, 254 nets) routed one-pass at pitch 0.6mm, 4 signal
layers, **per-net-class clearance on** (the good path — omit `--clearance`, `route_board`
resolves `net_clearance`/`net_exclusion` per net):

```
nets        : 254 routable | 70 fully routed | 184 with failures
geometry    : ... orthogonal VIOLATED (needs 7.70, widest class HV_SWING) ...
overuse     : [22305, 17669, ... 18405]   # never falls; pinned at ~19000
class space : 51 net(s) claim a track exclusion halo (up to 489 nodes/step)
```

The HV creepage halos are enormous — HV_SWING = 7.2mm clearance = a **489-node exclusion
per track node** at 0.6mm pitch. When all 254 nets negotiate at once, those halos saturate
the grid: overuse never falls, only 70/254 route. This is not a pitch bug or a placement bug
(placement is DRC-clean); it is that **a board mixing 7.2mm-creepage HV with 0.2mm logic
cannot be solved as one simultaneous negotiation** — the HV nets must claim their room while
the board is empty, exactly like rocks-first placement.

## The user's framing (correct) — flip the scheme

> "HV routes should be done first automatically (like large parts) so you have lots of room
> for them, then route audio and its grounds since its sensitive, then less sensitive things
> / everything else. The goal isnt to give up, its to succeed wildly."

Route in **descending order of constraint**: HV_SWING → HV_300 → HV_150 → Power → Default.
Each tier routes on a board where the previous (more-constrained) tiers are already committed
copper it must avoid.

## It is buildable TODAY on the internal API — no new solver needed

`route_lattice(lat, net_pads, node_owner=..., clearance=..., net_exclusion=...)` already
takes (a) a **net subset** via `net_pads`, and (b) a **claimed-node obstacle set** via
`node_owner`. And `RouteResult.net_paths : net_code -> [ [lattice node ids] ]` exposes each
routed net's occupied nodes. So a tier loop is:

```python
# reuse ALL of route_board's setup by monkeypatching route_lattice:
orig = pathfinder.route_lattice
def tiered(lat, net_pads, node_owner, clearance=None, net_exclusion=None, **kw):
    owner = dict(node_owner or {})
    merged = RouteResult(net_paths={}, failed=[], tracks=[], vias=[], ...)
    for tier in TIERS_MOST_CONSTRAINED_FIRST:          # by net class clearance
        sub = {n: p for n, p in net_pads.items() if class_of(n) in tier}
        if not sub: continue
        res = orig(lat, sub, owner, clearance=clearance, net_exclusion=net_exclusion, **kw)
        merge(merged, res)
        for code, paths in res.net_paths.items():      # commit this tier as obstacles
            for path in paths:
                for node in path:
                    owner.setdefault(node, code)
                    for h in halo(lat, node, net_clearance[code]):  # << the one subtle bit
                        owner.setdefault(h, code)
    return merged
pathfinder.route_lattice = tiered
pathfinder.route_board(board, region_index=1, pitch_mm=0.6, clearance_mm=None, ...)
```

**The one subtlety:** committing only `net_paths` nodes lets the next tier route 1 grid step
(0.6mm) from an HV track — legal against the *node* but violating the HV *net's* clearance.
So each committed node must also own its clearance **halo** (`clearance + track_width/2`, the
same radius `net_exclusion` uses live). `net_exclusion`'s live claims already compute this;
the cleanest fix is for `route_lattice` to optionally **return the exclusion-claimed nodes**
(not just `net_paths`), so the caller chains real halos instead of recomputing them from
lattice geometry. That is the single API add that makes tiered routing exact.

## Ask
1. A first-class **tiered / priority route** — `route_board(..., net_order=[classA, classB])`
   or a thin `tier_route()` — that routes class-groups most-constrained-first, chaining each
   tier's copper (path **and** exclusion halo) into `node_owner` for the next. The mechanism
   is all present; it needs (a) the loop and (b) `route_lattice` returning claimed halo nodes.
2. Until then the monkeypatch above works for a caller that recomputes halos from
   `net_clearance` + lattice coordinates.

Sparse LV regions already route one-pass clean (Voxy region 0 encoders: 23/23, overuse→0 at
0.6mm/4-layer). Tiering is specifically what unblocks the HV board.
