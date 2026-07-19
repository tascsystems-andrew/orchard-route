# Prompt for the Voxy design thread

Copy everything below the line into that session.

---

I want to try routing this board with Orchard Route, a GPU autorouter at
`~/Code/mlx-router` (public: github.com/tascsystems-andrew/orchard-route). It runs
from the CLI — use Bash, there's no MCP tool for it yet.

**Read `~/Code/mlx-router/AGENTS.md` first.** It's written for you specifically: it
explains the phases, how to read the output, what each failure reason means, and the
tool's current honest limitations. Follow it.

## Step 1 — the net class decision (do this before routing, and ask me)

Measurement on this exact board established that its copper geometry doesn't fit a
fine routing grid: a 0.6 mm via on a 0.5 mm grid physically overlaps its neighbours —
different nets shorting, not a clearance nit. The fix is copper sizes, not router
settings. Recommended `Default` class:

| setting | now | recommended | why |
|---|---|---|---|
| track_width | 0.2 | 0.2 (unchanged) | already fine |
| clearance | 0.2 | **0.15** | lets the via halo collapse to one grid node |
| via_diameter | 0.6 | **0.45** | JLC's cheapest legal via |
| via_drill | 0.3 | **0.25** | pairs with 0.45 — see below |

0.45 mm via / 0.25 mm drill sits *exactly* on JLCPCB's no-surcharge boundary (their
rule surcharges 0.25 mm holes only when via diameter is under 0.45 mm). Going to
0.40 mm would cost extra. Note this board **cannot** be routed at PCBWay's standard
price at a fine pitch — they require a 0.15 mm annular ring vs JLC's 0.05 mm, forcing
a 0.60 mm via that needs a 0.656 mm grid. If PCBWay is the target, say so and route
at 0.7 mm pitch instead.

**Before changing anything, check this with me:** dropping global clearance from
0.2 to 0.15 mm is fine for logic and low-level audio, but this board has nets whose
names suggest real voltage (`AC Plate P1`, `Bias Level PA1`, `+24v`). Any net above
~50 V wants its own net class with much wider clearance — creepage, not current, is
the rule there. Look at the schematic, tell me which nets actually carry voltage, and
propose classes for them (e.g. an `HV` class) rather than blanket-lowering everything.
Use Konnect to inspect the schematic. Then edit the `.kicad_pro` (it's JSON) or walk
me through Board Setup → Net Classes.

## Step 2 — route it

```sh
cd ~/Code/mlx-router
.venv/bin/python pathfinder.py "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb" \
  --pitch 0.6 --layers F.Cu,B.Cu --fab jlcpcb-standard --svg out/voxy.svg
```

Expect roughly 407 of 409 nets and a few minutes of runtime. Read the `geometry` and
`fab` lines it prints — that's the tool declaring its own limits, and if it says
`VIOLATED` or `diagonals OFF`, report it rather than glossing over it.

Then write a routed **copy** (the original is never modified — write-back refuses to
write into the source board's directory):

```sh
.venv/bin/python writeback.py "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb" \
  out/voxy-routed.kicad_pcb --pitch 0.6 --layers F.Cu,B.Cu --fab jlcpcb-standard
```

## Step 3 — verify honestly

**Do not quote raw `kicad-cli` DRC totals.** Two traps, both discovered the hard way:

1. `kicad-cli pcb drc` stops counting at 499 per rule type — 1,968 violations and 499
   violations print identically. Check every count against 199/200/499 before quoting it.
2. This board has **1,355 DRC violations of its own before any routing** (silkscreen,
   solder mask, and ~198 intentional pad overlaps). Always DRC the untouched original
   too and subtract, or you'll report Andrew's own WIP state as the router's bugs.

For the copper the router actually emitted, use the uncapped checker:

```sh
.venv/bin/python scripts/copper_audit.py \
  "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb" out/voxy-routed.kicad_pcb
```

Target is **zero**. Report the number it gives, not a summary of it.

## Step 4 (optional) — try the placement tool

This is the interesting one. `region.py` places *and* routes a group of components
inside a fence and returns ranked candidates:

```sh
.venv/bin/python region.py "/Users/andrew/Documents/Guitar/Voxy/Voxy/Voxy-arduino.kicad_pcb" \
  --components <refs> --region x,y,w,h \
  --constraint "fixed(<anchor>)" --constraint "min_distance(<a>,<b>,4)" \
  --k 5 --out out/region-test/
```

Pick a functional block (one gain stage, the switching section, a supply chain), fence
it, and constrain it the way you'd describe it out loud. Constraints available:
`fixed`, `keepout`, `adjacency_max_distance`, `min_distance`, `orientation_set`, `edge`
— it errors loudly on anything else, so don't invent forms. Each candidate comes back
as an openable `.kicad_pcb` plus an SVG. **They are proposals to inspect, not changes**
— nothing is applied unless we decide to.

Read the `diagnostics` block; it's the feedback loop. It names which constraint is
binding and by how much slack, which side of the fence is under pressure and by how
many mm, and which nets cross the boundary.

## Known limitations — disclose these when you report

- **Q2 and Q3 have custom pad shapes** (SOT-89 heat tabs) the parser doesn't read, so
  the router doesn't see that copper. The tool flags them itself in `geometry_warnings`.
- **Pre-existing tracks in the board are not obstacles** — the router sees pads only.
  On a partly-routed board, expect violations against copper that was already there.
- Two copper layers only; inner layers aren't modelled yet.
- The routed copy is a *proposal*. Open it, look at it, and tell me whether it's any
  good before anyone talks about ordering.

Report back with: the net-class question from Step 1, the route's own stats block, the
uncapped audit number, and your honest read of whether the result looks like something
a person would have drawn.
