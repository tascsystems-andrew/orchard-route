# Multi-Board Support in KiCad Itself — Scope Study

**Question:** We built external multi-board *routing* (detect disjoint `Edge.Cuts` outlines in one
`.kicad_pcb`, route/fab each as its own board). How big a lift is it to put multi-board support into
**KiCad itself**?

**Scope of this doc:** Honest, sourced engineering scoping as of **July 2026**. Current release is
**KiCad 10.0** (10.0.0 released 2026-03-20, 10.0.1 in April 2026). Effort magnitudes marked
"*(my estimate)*" are my engineering judgement, **not** KiCad statements — the project publishes no
effort numbers for this. Everything attributed to KiCad is cited. A list of things I could not source
is at the end.

---

## Bottom line (read this first)

"Multi-board" means three very different things, and they are not close in cost. **Native panelization
(tier B) is explicitly out of KiCad's scope** — the core team closed the panelization-tool request as
`status::out-of-scope` and leaves it to KiKit. **True multi-board projects with cross-board nets
(tier C) is a multi-release core redesign** that nobody is building: it breaks KiCad's foundational
`project → one root schematic → one netlist → one board` assumption, and the only open tracker item
(#15272) is an unscheduled `wishlist`. The one genuinely tractable, genuinely wanted thing is **tier A:
teaching the *existing* tools to treat disjoint `Edge.Cuts` outlines in one file as separate boards for
DRC / plot / fab** — which is exactly what your external tool already does. For Andrew's actual need
(fab a few disjoint boards from one file), **staying external is the right call.** If he wanted to
upstream anything, tier A is the only candidate worth proposing — and even that must clear KiCad's
"accepted issue before you write code" gate first.

---

## The three things "multi-board" can mean

| | What it is | KiCad today | Who owns it |
|---|---|---|---|
| **A. Disjoint outlines → separate boards** | Treat multiple closed `Edge.Cuts` loops in *one* `.kicad_pcb` as N boards for DRC, plotting, and fab output | Tolerated/drawn, **not a first-class concept**; no per-board DRC/plot | External (KiKit `extract`, your tool) |
| **B. Panelization** | Many copies/boards in one **manufacturing panel** with tabs / mouse-bites / v-cuts / rails | **Not native**; request closed **out-of-scope** | External (KiKit) |
| **C. Multi-board projects** | Multiple *interconnected* PCBs in one project, cross-board nets, one system schematic → many boards | **Not supported**; open wishlist, no milestone | Would be **core-team redesign** |

These are routinely lumped together as "multi-board," which is why the question feels bigger than it is:
tier A is small, tier B is deliberately not-KiCad's-job, tier C is the multi-year one.

---

## 1. What KiCad supports TODAY (v10.0)

**(a) Native panelization — NO, still external.** KiCad 10's release notes list time-domain tuning,
design variants, pin/gate swap, a graphical DRC rule editor, and importers — **no panelization, panels,
tabs, mouse-bites, or v-cuts.** [KiCad 10.0 release notes](https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/).
The de-facto tool remains **KiKit** (v-cuts, mouse-bites, tab routing, frame rails; CLI + Python;
installed via the Plugin and Content Manager) — [KiKit](https://github.com/yaqwsx/KiKit),
[KiKit v1 release writeup](https://blog.honzamrazek.cz/2021/12/panelization-automation-for-kicad-made-easy-kikit-v1-finally-released/).

**(b) Multi-PCB / multi-board projects — NO.** KiKit's own docs state it plainly:
"KiCAD does not support multiple board per project, nor boards with shared schematics."
[KiKit multiboard workflow](https://yaqwsx.github.io/KiKit/v1.4/multiboard/). There is no cross-board
net concept. The closest *reuse* features KiCad has added are **multi-channel design** (KiCad 9 —
replicate an identical sub-circuit and its rules *within one board*) and **design variants** (KiCad 10 —
"different versions of a single project that share a schematic but have property changes"). Both operate
inside the single-schematic/single-board model; neither is multi-board.
[KiCad 9.0 notes](https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/),
[KiCad 10.0 notes](https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/).

**(c) Disjoint outlines in one `.kicad_pcb` — tolerated, not a supported "board" concept.** You *can*
draw multiple closed `Edge.Cuts` loops side by side and KiCad renders them (a 3D-viewer bug where slots
on the *second* outline didn't render was fixed for 7.0 — issue #7993, `status::fix-released`,
milestone 7.0). But that fix only made the second outline *draw*; it created no board-separation
semantics. The blessed workflow is still "draw all boards into one file, then **KiKit `extract`** each
one before manufacturing."
[Issue #7993](https://gitlab.com/kicad/code/kicad/-/issues/7993),
[KiKit multiboard](https://yaqwsx.github.io/KiKit/v1.4/multiboard/).

---

## 2. Roadmap / official position

**Panelization is officially out of scope.** The native-panelization request,
[issue #2180 "Pcbnew: Create panelization tool"](https://gitlab.com/kicad/code/kicad/-/issues/2180),
is **Closed** with labels **`priority::wishlist`, `status::out-of-scope`** (opened 2018, last touched
2024-09-28). That is the strongest official signal you get: the core team declined to own panelization
and points users to external tooling.
*(Verified via GitLab API: state=closed, labels include `status::out-of-scope`. The maintainer's exact
closing words are behind an authenticated notes endpoint I couldn't read — see "Could not source.")*

**Multi-board projects are an unscheduled wishlist, not a plan.**
[Issue #15272 "[Feature Request] Master project for multiple boards"](https://gitlab.com/kicad/code/kicad/-/issues/15272)
is **Open**, labels **`feature-request`, `priority::wishlist`, `status::new`**, **milestone: none**, last
updated 2026-01-16 — i.e. acknowledged, unowned, unscheduled. (Its concrete ask is even narrower than
tier C: shared *library paths* across sibling board projects.) *(Verified via GitLab API.)*

**No multi-board / "system schematic" epic on the roadmap.** KiCad's official roadmap moved to the
[GitLab epics/roadmap](https://gitlab.com/groups/kicad/-/roadmap); the public epic and roadmap listings
are JS-rendered and did not expose any multi-board / multi-PCB / system-schematic / panelization epic to
me. I found **no** core-team commitment to native multi-board in the KiCad 9, 10, or planned-11 notes,
nor in the FOSDEM 2024/2025 KiCad status talks surfaced in search
([FOSDEM 2025 KiCad Project Status](https://archive.fosdem.org/2025/schedule/event/fosdem-2025-4152-kicad-project-status/)).
*(Absence-of-evidence: I could not read the JS-rendered epic list; I did not find a multi-board epic, but
cannot prove none exists — treat as "none found," not "none exists.")*

**Where the official energy actually went: the IPC API.** KiCad 9 introduced a **stable IPC API**
explicitly designed so external tools survive internal refactors, expanding to the schematic editor and
headless `kicad-cli` in KiCad 11. [KiCad IPC API dev docs](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/).
The strategic message is consistent: KiCad is investing in **letting external tools do this** (KiKit,
your router) rather than absorbing panelization/multi-board into core.

---

## 3. Why tier C is architecturally hard (grounded in KiCad's model)

KiCad's data model is **`project (.kicad_pro) → one root schematic sheet → one flattened netlist → one
`.kicad_pcb`**. The schematic hierarchy exists to be *flattened* into a single netlist that maps 1:1
onto a single board. Multi-channel design and design variants were both built *inside* that invariant
(replicate/annotate within one board), which is precisely why they were feasible and multi-board is not.
The lead developer's handling of the multi-channel tooling shows the seam: replication is done by
sub-sheet/rule-area mapping within the one board, and "topological mismatch" is the failure mode when
that mapping breaks — a single-board mechanism stretched, not a multi-board one.
[Devlist, multichannel design, S. Hillbrand, 2025-06-16](https://www.mail-archive.com/devlist@kicad.org/msg00770.html).

A real tier-C feature has to break the 1:1 assumption across essentially every subsystem:

- **Project format (`.kicad_pro`):** must model N boards + N (or shared) schematics + inter-board
  connectivity. Today it assumes one board.
- **Schematic / netlist model:** needs a "system schematic" above the board level and cross-board nets
  (connectors as first-class inter-board links). This is the deep one — the netlist flattener assumes one
  target board.
- **Board editor (Pcbnew):** each board is its own coordinate space, stackup, net set, and design-rule
  context; the editor currently assumes exactly one.
- **DRC:** must scope per board (and *not* flag inter-board clearance as a short), plus a new class of
  inter-board/connector-mating checks.
- **ERC:** cross-board nets must not read as unconnected/undriven; ERC assumes one flattened sheet set.
- **Plot / fab output & jobsets:** per-board gerbers, drills, BOM, place files — output is currently
  keyed to the single `.kicad_pcb`.
- **3D viewer & UI:** show/assemble multiple boards in relative 3D position; board switching, per-board
  properties, project tree — all new UI surface.

Architecture references: [KiCad developer docs](https://dev-docs.kicad.org/),
[Code Design Guidelines](https://dev-docs.kicad.org/en/rules-guidelines/code-policy/) (which, notably,
insists on keeping UI out of core `SCH_*`/`PCB_*` objects and cross-platform parity — constraints that
make a change this wide *more* expensive, not less).

---

## 4. Effort tiers with honest ranges

Magnitudes below are **my engineering estimate** for someone already fluent in the KiCad codebase, not
KiCad-published numbers.

| Tier | Feature | Subsystems touched | Rough magnitude *(my estimate)* | External contribution or core redesign? | Prior/in-progress work |
|---|---|---|---|---|---|
| **A** | Disjoint `Edge.Cuts` loops in one file → separate boards for **DRC / plot / fab** | DRC scoping, plot/jobset "board region" selection, fab export; **no** schematic/netlist change | **Weeks** — small if bounded to "region = closed outline, emit per-region outputs, don't cross-flag DRC." A weekend only for the plot/extract slice; the DRC-scoping slice is the real work. | **Plausibly external / a focused MR**, *if* the core team accepts the concept first. This is the tier a determined hobbyist could land. | Your tool already does the routing/fab half; KiKit's `extract` does the file-split half. No core feature exists. |
| **B** | Native **panelization** (tabs, mouse-bites, v-cuts, rails, arrays) | New panel data model + editor UI + fab output; DRC exceptions for tabs/rails | **Multiple person-months** for a decent GUI; the geometry/output engine alone is large | **Neither, realistically** — core team declared it **out-of-scope** ([#2180](https://gitlab.com/kicad/code/kicad/-/issues/2180)). An MR would likely be declined on scope, not merit. | **KiKit** is the mature, blessed answer. Don't rebuild it inside KiCad. |
| **C** | True **multi-board projects** with cross-board nets / system schematic | Project format, schematic+netlist model, Pcbnew, DRC, ERC, plot/jobsets, 3D, UI — **everything in §3** | **Multi-release core effort** (person-years, spanning major versions) | **Core-team-owned redesign.** Not a hobbyist deliverable; not an isolated MR. Tied to the schematic/netlist/project rework, not shippable piecemeal. | Only an unscheduled wishlist ([#15272](https://gitlab.com/kicad/code/kicad/-/issues/15272)). A third-party FreeCAD↔KiCad multi-board sync experiment exists *on top of* the IPC API — i.e. built **external**, reinforcing that path. |

---

## 5. Contribution reality

KiCad **does** accept large external contributions — but through a gate, and license terms matter:

- **Accepted-issue-first is mandatory.** Per the **Feature Contribution Policy**: *"KiCad does not accept
  feature-addition or behavior-change merge requests from new contributors without an issue that the lead
  development team has already accepted."* You open an issue, ask to work on it, and *"wait for a
  response. If the team agrees, they will assign the issue to you."* Unsolicited feature MRs *"the team
  may close … and ask you to open an issue first."*
  [Feature Contribution Policy](https://dev-docs.kicad.org/en/rules-guidelines/feature-proposals/),
  [Getting Started](https://dev-docs.kicad.org/en/getting-started/index.html).
- **Big changes go to the devlist first.** *"Developing any larger change, such as a new feature, should
  be discussed on the developers mailing list before substantial work is done."*
  [CONTRIBUTING.md](https://raw.githubusercontent.com/KiCad/kicad-source-mirror/master/CONTRIBUTING.md).
- **Licensing:** contributions ship under **GPLv3-or-later**; KiCad relies on copyright headers and
  copyright law. [KiCad licenses](https://www.kicad.org/about/licenses/),
  [Code Design Guidelines](https://dev-docs.kicad.org/en/rules-guidelines/code-policy/). *(I did not find
  an explicit statement of "no CLA" vs "CLA required" — see "Could not source." Best current
  understanding: contributors retain copyright and license under GPLv3+, no formal CLA, but that specific
  claim is unverified.)*
- **Culture:** start small, cross-platform parity required, keep UI out of core objects.
  [Developer Culture](https://dev-docs.kicad.org/en/rules-guidelines/culture/index.html).

**Could a hobbyist land tier A?** Realistically yes — *if* the concept passes the issue gate first. It's
bounded, doesn't touch the schematic/netlist model, and solves a genuinely recurring pain (the
multiple-outlines-in-one-file question recurs constantly on the forums). Tier B would be declined on
scope; tier C is not an outside-contributor deliverable at all.

---

## 6. Pragmatic verdict for Andrew

You already have the thing you actually need: external multi-board **routing + fab** for a few disjoint
boards in one `.kicad_pcb`. Given that:

- **Tier C: don't.** It's a core redesign of KiCad's project/schematic/netlist model, unscheduled, and
  wildly out of proportion to "fab a few disjoint boards." Nothing about your use case needs cross-board
  nets or a system schematic.
- **Tier B: don't.** KiKit already owns it and KiCad has ruled it out of scope. Rebuilding it upstream is
  a lose/lose.
- **Tier A: the only candidate worth a moment's thought.** It's the smallest useful contribution that
  would genuinely help others, it aligns with what your tool already proves works, and it's the one that
  could realistically be upstreamed. But it's still gated: you'd open an issue proposing "per-board-region
  DRC/plot/fab for disjoint `Edge.Cuts` outlines," get it *accepted and assigned* before writing code, and
  accept a good chance the team steers you back to "that's what KiKit/`extract` is for." Whether that
  gauntlet is worth it depends purely on how much you want the credit/community benefit vs. shipping your
  own tool today.

**Recommendation:** stay external. It matches KiCad's own strategic direction (invest in the IPC API and
external tooling, not in absorbing this into core). If the itch to upstream persists, spend one hour
filing a tier-A issue and gauging the maintainers' appetite **before** writing a line of code — that's
the cheapest possible probe and exactly the process KiCad asks for.

---

## Could not source / unverified (flagged honestly)

1. **Exact maintainer wording** on why #2180 (panelization) is out-of-scope. The `status::out-of-scope`
   label + closed state are verified via the GitLab API; the closing *comment text* sits behind an
   authenticated notes endpoint that returned HTTP 401. Classification is solid; verbatim rationale is not.
2. **CLA policy.** GPLv3+ licensing of contributions is verified. Whether KiCad requires a formal CLA or
   contributors simply retain copyright under GPL is **not explicitly stated** on the pages I read — my
   "no CLA, retain copyright" reading is unverified.
3. **All person-week/month/year effort figures are my estimates.** KiCad publishes no effort numbers for
   these features. Treat magnitudes as engineering judgement, not project statements.
4. **"No multi-board epic on the roadmap"** is *none-found*, not *proven-none*: the GitLab epics/roadmap
   listing is JS-rendered and I couldn't enumerate it programmatically.
5. **KiCad 11 specifics** (IPC API → schematic, headless `kicad-cli`) come from KiCad dev-docs and search
   summaries describing in-progress work; 11 is unreleased as of July 2026, so those are plans, not shipped.
