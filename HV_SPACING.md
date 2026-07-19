# HV_SPACING — can the Voxy-arduino control/driver board skip conformal coating?

Answer to a specific engineering question on a specific board, with the numbers
traced to sources. Companion to ANALOG_SCORING.md §M8 (`hv_clearance`), which
this document supersedes numerically: M8's "0.6 / 1.25 / 2.5 mm" is the
IPC-2221B **clearance** column only, and clearance is not the binding
requirement on this board.

**Status of every number below:** VERIFIED means read off a cited source.
DERIVED means computed here from verified inputs, with the arithmetic shown.
UNVERIFIED means I could not confirm it from a primary or reputable source and
it is flagged rather than filled in. The project rule applies: a confident
wrong number is worse than an admitted gap.

---

## 0. The board, as measured

Corrected 2026-07-19 by the designer. The original brief said "600 V"; the
mechanism is right but the arithmetic was low.

| net group | nets | DC / working | peak |
|---|---|---|---|
| logic, relay supply, low-level audio | ~450 | 3.3 / 5 / 24 V | same |
| mid nodes | `VMID` (1M/1M off B+), cascode mids | ~150 V DC | ~150 V |
| HV rails and plates | `B+`, `C+`, `D+`, `Plate Audio Out P1/T1/T2`, `Net-(C8/C9/C28-Pad2)` | ~360 V DC | ~360–400 V |
| switching drains | `Output PA1`, `Output PA2` | ~360 V DC average | **~720 V** |

The drains are IRFBG30 drains tied to an output-transformer primary whose
centre tap sits at B+. In push-pull, when one device pulls its drain toward
0 V the transformer flies the other drain to roughly **2 × B+ ≈ 720 V**, and
the instantaneous **drain-to-drain** difference reaches the same 720 V. Two
independent facts on the board corroborate this reading:

- The **IRFBG30 is a 1000 V device** (V<sub>DSS</sub> = 1000 V, R<sub>DS(on)</sub> = 5.0 Ω,
  I<sub>D</sub> = 3.1 A) — VERIFIED,
  [Vishay datasheet, doc 91124](https://www.vishay.com/docs/91124/irfbg30.pdf).
  A 1000 V part on a 360 V rail is a 2.8× rating; a designer expecting only
  360 V on the drain would not reach for it. 1000 V over a 720 V peak is a
  ~280 V overshoot budget, which is the designer's own implicit statement of
  how far above 2×B+ the leakage spike is expected to go.
- C75/C76, 22 nF **630 V** snubbers. Snubbers exist because primary leakage
  inductance rings the drain *above* 2×B+ at turn-off. Their presence is
  evidence the swing is real.

> **FLAG — verify against the schematic, not this document.** A 630 V cap is
> *below* the 720 V drain peak. That is fine if C75/C76 sit drain-to-B+ (they
> then see only ~360 V) and a problem if they sit drain-to-ground or
> drain-to-drain (720 V across a 630 V part). I traced connectivity summaries,
> not the schematic, so I cannot tell which. **Check this** — it is a
> component-rating question independent of everything else here.

**True worst-case peak is not 720 V.** It is 720 V plus the leakage-inductance
overshoot the snubber fails to absorb, bounded above only by the FET's
avalanche rating. A defensible conservative envelope is therefore **1000 V**,
and §1 gives that row too. The tables below use 720 V as the design peak
because that is what the topology guarantees; treat 1000 V as the number to
use if you want the copper to survive a snubber that has aged or a cap that
has opened.

---

## 1. The actual required spacings

### 1.1 The distinction that decides this question

Two different physical failures, two different standards, two different
governing voltages. This is not pedantry — it is the whole answer.

| | CLEARANCE | CREEPAGE |
|---|---|---|
| what it is | shortest distance **through air** between two conductors | shortest distance **across the solid insulating surface** |
| failure mode | air ionises, arc, instantaneous | conductive carbonised track grows across contaminated surface, over months |
| timescale | short term | long term |
| **governed by** | **peak / transient voltage** | **RMS or DC working voltage** |
| driven by | air pressure (altitude), temperature | pollution degree, humidity, material CTI |
| standard | IPC-2221B Table 6-1; IEC 60664-1 Table F.2 | IEC 60664-1 Table F.4 |
| **coating helps?** | barely | **yes, a lot** |

VERIFIED, TI Power Supply Design Seminar SLUP421, *Demystifying clearance and
creepage distance for high-voltage end equipment*, slide 4
([ti.com/lit/pdf/SLUP421](https://www.ti.com/lit/pdf/SLUP421)), which states
clearance is "dimensioned for: transient overvoltages … pollution degree …
altitude" and creepage is "dimensioned for: root-mean square (RMS) working
voltage … pollution degree … material group", citing IEC 60664-1. Also
VERIFIED at [Standard Clarity's IEC 60664-1
notes](https://standardclarity.com/calculators/creepage-clearance/): clearance
follows "the peak of that transient — the rated impulse voltage", creepage
follows "the long-term rms or dc working voltage".

**Working voltage** is defined in IEC 60664-1 §3.5 as the *highest RMS value of
the AC or DC voltage across the insulation* — VERIFIED via SLUP421 slide 14
footnote.

**Which voltage does IPC-2221B use?** Table 6-1's own column header is
"Voltage Between Conductors (**DC or AC Peaks**)" — VERIFIED at
[sfcircuits](https://www.sfcircuits.com/pcb-school/pcb-line-spacing-clearance-creepage)
and corroborated by the search-indexed statement that IPC-2221B "spacing values
are evaluated with respect to peak DC or AC voltage". So **IPC-2221B is a peak
number throughout**, including the >500 V formula. It offers no
working-voltage relief and no creepage table at all — IPC-2221 "addresses
clearance (spacing through air), not creepage (surface distances along
insulation)" (VERIFIED,
[Altium](https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design)).

**The crux, stated plainly.** Yes, the peak-vs-working distinction is real and
standard, and yes it would in principle let the drains take a large clearance
at 720 V peak and a smaller creepage at a lower working voltage. **But on this
board the relief is much smaller than it looks**, for a reason specific to a
guitar power amp — see §1.3. Do not plan the layout around the hoped-for
relief until you have read that section.

### 1.2 Two more things that are true of *this* board's geometry

1. **Spacing is a property of a PAIR, not of a net.** Every table below is
   indexed by the *potential difference* between two conductors. `Output PA1`
   next to `B+` is a 360 V pair, not a 720 V pair. This is the single largest
   lever you have and §3.2 builds on it.
2. **On a bare 2-layer outer layer, the creepage path and the clearance path
   are the same path.** Two traces on F.Cu separated by 3 mm of soldermasked
   laminate have a 3 mm air gap *and* a 3 mm surface gap. So the binding
   requirement for any pair is simply **max(clearance, creepage)** — until you
   change the geometry (a slot) or the surface (a coating), which is exactly
   what §2 and §3 are about.

### 1.3 Creepage design voltages for the drains — the honest arithmetic

DERIVED. The drain sits at 360 V DC with an AC swing on top. Working voltage is
the RMS of the *total* waveform, √(V<sub>DC</sub>² + V<sub>AC,rms</sub>²).

| pair | clean sine at full output | **driven into clipping** (square-ish) |
|---|---|---|
| drain ↔ ground / logic | √(360² + 254.6²) = **441 V rms** | **509 V rms** |
| drain ↔ drain | 720 V<sub>pk</sub> differential, no DC → **509 V rms** | **720 V rms** |
| drain ↔ B+ | 360 V<sub>pk</sub> differential → **255 V rms** | **360 V rms** |

**A guitar power amp spends its life clipped.** That is the point of it. As the
drain waveform approaches a square wave its RMS approaches its peak, and the
creepage relief you were hoping for largely evaporates for the drain-to-drain
pair specifically. Use the **right-hand column**. The relief is still real for
drain-to-ground (509 V rms vs 720 V peak) and for drain-to-B+ (360 V rms), and
those are the pairs that actually let you shrink the board.

### 1.4 IPC-2221B Table 6-1 — CLEARANCE, peak-driven

VERIFIED (values) at
[sfcircuits](https://www.sfcircuits.com/pcb-school/pcb-line-spacing-clearance-creepage),
which reproduces the table in full. Cross-checked against
[Altium](https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design),
[smpspowersupply](https://www.smpspowersupply.com/ipc2221pcbclearance.html) and
[protoexpress](https://www.protoexpress.com/blog/ipc-2221-circuit-board-design/).
All are SECONDARY — they reproduce a copyrighted IPC table. The standard itself
is IPC-2221B (Nov 2012); its table of contents, which confirms §6.3 "Electrical
Clearance" (p. 56), Table 6-1 "Electrical Conductor Spacing" (p. 57) and the A5
"External Conductors, with Conformal Coating (Any Elevation)" column (p. 58),
is at [electronics.org/TOC/IPC-2221B.pdf](https://www.electronics.org/TOC/IPC-2221B.pdf).

Columns: **B1** internal · **B2** external uncoated, sea level–3050 m · **B3**
external uncoated >3050 m · **B4** external with permanent polymer coating ·
**A5** external with conformal coating over the assembly · **A6** external
component lead/termination uncoated · **A7** external component lead, coated.

| V band (DC or AC peak) | B1 | B2 | B3 | B4 | A5 | A6 | A7 |
|---|---|---|---|---|---|---|---|
| 0–15 | 0.05 | 0.1 | 0.1 | 0.05 | 0.13 | 0.13 | 0.13 |
| 16–30 | 0.05 | 0.1 | 0.1 | 0.05 | 0.13 | 0.25 | 0.13 |
| 31–50 | 0.1 | 0.6 | 0.6 | 0.13 | 0.13 | 0.4 | 0.13 |
| 51–100 | 0.1 | 0.6 | 1.5 | 0.13 | 0.13 | 0.5 | 0.13 |
| **101–150** | 0.2 | **0.6** | 3.2 | **0.4** | 0.4 | 0.8 | 0.4 |
| 151–170 | 0.2 | 1.25 | 3.2 | 0.4 | 0.4 | 0.8 | 0.4 |
| 171–250 | 0.2 | 1.25 | 6.4 | 0.4 | 0.4 | 0.8 | 0.4 |
| 251–300 | 0.2 | 1.25 | 12.5 | 0.4 | 0.4 | 0.8 | 0.8 |
| **301–500** | 0.25 | **2.5** | 12.5 | **0.8** | 0.8 | 1.5 | 0.8 |
| **>500** | 0.0025/V | **0.005/V** | 0.025/V | **0.00305/V** | 0.00305/V | 0.00305/V | 0.00305/V |

> **AMBIGUITY — sources disagree on how to apply the >500 V row, and it
> matters for exactly the column this question turns on.**
>
> - **Reading A, "increment":** spacing = (301–500 value) + (V − 500) × rate.
>   Altium gives a worked example in this form: internal at 580 V →
>   `0.25 mm + (580−500) × 0.0025 = 0.45 mm`. smpspowersupply gives the same
>   form for external uncoated: `2.5 + (V−500) × 0.005`.
> - **Reading B, "multiplier":** spacing = V × rate. TI SLUP421 slide 24
>   tabulates IPC-2221B at 800 V as 4.0 mm uncoated (= 800 × 0.005) and
>   2.44 mm coated (= 800 × 0.00305), and at 1000 V as 5.0 / 3.05 mm. That is
>   unambiguously Reading B.
>
> For **B2 (uncoated) the two readings coincide**, because 500 × 0.005 =
> 2.5 mm = the 301–500 value. Lucky. For **B4/A5 (coated) they do not**:
> 500 × 0.00305 = 1.525 mm ≠ 0.8 mm, so at 720 V the two readings give
> **1.47 mm (A) vs 2.20 mm (B)**. **Take the conservative 2.20 mm.**

DERIVED for this board:

| pair | peak | B1 internal | **B2 ext. uncoated** | **B4/A5 ext. coated** |
|---|---|---|---|---|
| 150 V node ↔ gnd | 150 V | 0.20 | **0.60** | **0.40** |
| 360 V rail ↔ gnd | 360–400 V | 0.25 | **2.50** | **0.80** |
| drain ↔ B+ | 360 V | 0.25 | 2.50 | 0.80 |
| drain ↔ gnd/logic | 720 V | 1.80 † | **3.60** | **2.20** † |
| drain ↔ drain | 720 V | 1.80 † | 3.60 | 2.20 † |
| *(worst-case envelope)* | *1000 V* | *2.50 †* | *5.00* | *3.05 †* |

† conservative Reading B. Reading A would give 0.80 / 1.47 / 0.80 / 1.72 mm.

### 1.5 IPC-9592B — the other peak-driven clearance rule

IPC-9592B (*Requirements for Power Conversion Devices for the Computer and
Telecommunications Industries*) gives one continuous formula rather than bands:

> **spacing = 0.6 mm + V<sub>PK</sub> × 0.005 mm/V**

VERIFIED, TI SLUP421 slide 24, which prints both the formula and a table that
checks out against it exactly (400 V → 2.6 mm, 800 V → 4.6 mm, 1000 V →
5.6 mm). Explicitly **peak**-driven. It is the applicable body for
power-conversion hardware, which a MOSFET push-pull driver stage is.

DERIVED: 150 V → **1.35 mm** · 360 V → **2.40 mm** · 400 V → 2.60 mm ·
720 V → **4.20 mm** · 1000 V → 5.60 mm.

Note this is *more* demanding than IPC-2221B B2 below ~500 V and comparable
above. It has no coated column.

### 1.6 IEC 60664-1 Table F.4 — CREEPAGE, working-voltage-driven

VERIFIED anchor points, TI SLUP421 slide 17 (reproducing IEC 60664-1
Table F.4, functional/basic insulation). "It is possible to use linear
interpolation between the nearest two points" — VERIFIED, same slide.

| V<sub>RMS</sub> | PD1 (all groups) | PD2 MG I | PD2 MG II | **PD2 MG IIIa** |
|---|---|---|---|---|
| 63 | 0.20 | 0.63 | 0.90 | 1.25 |
| 400 | 1.0 | 2.0 | 2.8 | 4.0 |
| 800 | 2.4 | 4.0 | 5.6 | 8.0 |
| 1000 | 3.2 | 5.0 | 7.1 | 10.0 |

- **Material group.** CTI ≥ 600 → I; 400 ≤ CTI < 600 → II; 175 ≤ CTI < 400 →
  IIIa; <175 or unspecified → IIIb. **"Most FR4 PCBs" are Material Group
  IIIa** — VERIFIED, SLUP421 slide 6. *UNVERIFIED:* JLCPCB does not publish a
  CTI for its standard FR-4 anywhere I could find. **Assume IIIa.** If the CTI
  is unspecified the standard's own default is the *worse* group IIIb.
- **Pollution degree.** PD1 = "no pollution or only dry, nonconductive
  pollution", example given: "**sealed components (coated PCB)**, clean room".
  PD2 = "temporarily becomes conductive because of occasional condensation",
  example: enclosure per IEC 62368-1, lab, office. PD3 = "subject to
  conductive pollution … industrial, unheated factory rooms". VERIFIED,
  SLUP421 slide 7. **A combo amp inside a wooden cab is PD2 at rest.** A
  gigging amp sitting on a wet stage floor in smoke, hauled between a cold van
  and a warm room, is arguably PD3. PD3 creepage values are UNVERIFIED here —
  TI's abridged table omits the PD3 column — but they are *larger* than PD2,
  so PD2 is not the conservative case for a gigging amp. Flagging, not
  guessing.
- **Interpolated for this board** (DERIVED, linear between the anchors above;
  the real Table F.4 has intermediate rows at 80/100/125/160/200/250/320/500/
  630 V which I could not verify, so I interpolate across the verified anchors
  — this is *slightly conservative*, e.g. it yields 1.96 mm at 150 V where the
  real 160 V row is 2.0 mm, and 3.67 mm at 360 V where 320 V/400 V rows would
  bracket ~3.6 mm):

| pair | working V<sub>RMS</sub> | **PD2 / MG IIIa (bare)** | PD1 (qualified coating) | ratio |
|---|---|---|---|---|
| 150 V node ↔ gnd | 150 | **1.96** | 0.41 | 4.8× |
| 360 V ↔ 150 V | 210 | 2.45 | 0.55 | 4.5× |
| drain ↔ B+ (clipped) | 360 | 3.67 | 0.91 | 4.0× |
| 360 V rail ↔ gnd | 360 | **3.67** | 0.91 | 4.0× |
| drain ↔ gnd/logic (clipped) | 509 | **5.09** | 1.38 | 3.7× |
| drain ↔ drain (clipped) | 720 | **7.20** | 2.12 | 3.4× |

### 1.7 The binding number — max(clearance, creepage), bare 2-layer outer layer

**This is the table that answers the question.** DERIVED as the maximum of
§1.4 B2 / §1.5 / §1.6 PD2-MG-IIIa for the uncoated case, and of §1.4 B4-A5 /
§1.6 PD1 for the coated case.

| pair | **UNCOATED, bare** | driven by | **COATED (qualified)** | driven by |
|---|---|---|---|---|
| 150 V ↔ gnd/logic | **2.0 mm** | creepage @150 V rms | **0.5 mm** | creepage PD1 (0.41), IPC B4 (0.40) |
| 360 V ↔ 150 V | 2.5 mm | creepage @210 V rms | 0.6 mm | creepage PD1 |
| 360 V rail/plate ↔ gnd/logic | **3.7 mm** | creepage @360 V rms | **1.0 mm** | creepage PD1 (0.91) |
| drain ↔ B+ / adjacent HV | 3.7 mm | creepage @360 V rms | 1.0 mm | creepage PD1 |
| drain ↔ gnd/logic | **5.1 mm** | creepage @509 V rms | **2.2 mm** | **IPC-2221B A5 @720 V pk** |
| drain ↔ drain | **7.2 mm** | creepage @720 V rms | **2.2 mm** | **IPC-2221B A5 @720 V pk** |

Read the "driven by" column. **In the uncoated build, creepage is the binding
requirement on every single row** — IPC-2221B's clearance numbers, the ones
ANALOG_SCORING.md §M8 currently quotes, are never the largest. In the coated
build the drain rows flip and *clearance* becomes binding, because coating
buys a lot of creepage and almost no clearance. That flip is the reason coating
stops paying past a point, and it is the reason §3's slot alternative is
interesting.

---

## 2. What coating actually buys, and what it costs you

### 2.1 The number

From §1.7: **coating removes roughly 3.3× to 4× of required spacing.** The
drain corridor goes from 7.2 mm to 2.2 mm; the 360 V rails go from 3.7 mm to
1.0 mm. Two independent mechanisms stack:

1. **IEC route:** a coated board can be assessed at **pollution degree 1**.
   VERIFIED, SLUP421 slide 7 gives "sealed components (coated PCB)" as *the*
   PD1 example, and slide 14 footnote states "Coated PCB can help reduce to
   pollution degree 1 per IEC 62109-1 (follow IEC 60664-3) or IEC 62368
   reduce the CPG distance using Table G.13." That is the 3.4–4.8× column in
   §1.6.
2. **IPC route:** the B4/A5 columns instead of B2. 0.8 mm instead of 2.5 mm at
   360 V; ~2.2 mm instead of 3.6 mm at 720 V.

### 2.2 Six caveats, and one of them is decisive

1. **Solder mask is not the same thing, and the standard changed under you.**
   IPC-2221**C** "explicitly replaced the earlier vague reference to *permanent
   polymer coating*" with **solder mask** for the B4 column — VERIFIED,
   [Altium](https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design).
   IPC-2221**B**, the revision ANALOG_SCORING.md cites, says "permanent
   polymer coating", and the long-standing industry reading (the one that doc
   repeats) is that solder mask does *not* qualify. So under IPC-2221C your
   stock JLC green mask arguably already earns the B4 column for free; under
   IPC-2221B it does not. **Neither reading gets you the IEC PD1 credit** —
   that requires a qualified coating under IEC 60664-3, and solder mask is not
   one. Since §1.7 shows creepage binds everywhere in the uncoated case, the
   IPC-2221C solder-mask concession **does not change the answer at all**.
   Do not let it fool you.
2. **The binding gaps on an SMD board are at component terminations, and mask
   never covers those.** Solder mask is pulled back from every pad. The gap
   between a 1206's two terminals, or between two TO-220 leads, is bare
   laminate plus solder fillet no matter what mask you specify. Only a
   **conformal coating applied over the assembled board** (column A5) covers
   them. If you are going to invoke a coating credit at all, this is why it
   must be A5, not B4.
3. **It must actually be there, everywhere, on a clean board.** Coating that
   does not continuously cover the gap earns nothing across that gap. Coating
   applied over flux residue traps an electrolyte under a moisture barrier —
   worse than bare. Hand brushing gives variable thickness, bubbles at
   component shadows, and thin spots at the sharp copper edges where the field
   is highest. IPC-CC-830 is the qualification spec for coating *materials*
   ([overview](https://resources.pcb.cadence.com/blog/2020-understanding-ipc-conformal-coating-standards));
   it says nothing about whether *your* application of it is any good. The IEC
   PD1 credit under IEC 60664-3 requires qualified Type 1/Type 2 protection
   with adhesion and thermal-cycling testing — **a hobbyist brush coat is not
   formally entitled to the PD1 numbers**, whatever its physical merit.
   State that to yourself honestly before you design to the coated column.
4. **Temperature.** Acrylic conformal coating has a service ceiling around
   **125 °C**; silicone reaches about **200 °C** (VERIFIED as a general
   material-class comparison —
   [andwinpcb](https://www.andwinpcb.com/acrylic-vs-silicone-vs-urethane-conformal-coating-complete-selection-guide-for-pcb-protection/),
   SECONDARY/vendor). A board sharing a chassis with valve envelopes and hot
   plate resistors is a place where the cheap, reworkable, easy-to-apply
   acrylic is exactly the wrong choice, and the one that survives — silicone —
   is the one that is worst to rework.
5. **You are going to poke at this board.** That is the stated context. Coating
   makes probing require scraping, makes desoldering fume-y and messy, and
   makes every modification a coat-scrape-modify-recoat cycle where the recoat
   is the step that gets skipped — leaving an uncoated gap in a board designed
   to coated spacings. **A coated board designed to coated spacings is a board
   whose safety depends on a maintenance step you will eventually skip.** This
   is the strongest argument against the coated design point on *this*
   board, and it is a human-factors argument, not a standards one.
6. **Coating is not touch-safe insulation.** It reduces tracking; it does not
   make a 720 V node safe to touch. Nothing in §1 or §2 changes the fact that
   this board must be de-energised and bled before hands go near it.

---

## 3. The alternatives, evaluated for this board

### 3.1 Slots / cutouts — the strongest option, and JLC can cut them

**What the standard says.** A groove counts toward the creepage path only if it
is at least X wide; narrower than X it is *bridged* and creepage is measured
straight across as if the groove were not there. VERIFIED, IEC 60664-1
§6.2 Ed 2.0 via SLUP421 slide 22, on a slide titled "PCB cutout to increase the
PCB creepage":

| pollution degree | minimum groove width X |
|---|---|
| 1 | 0.25 mm |
| **2** | **1.0 mm** |
| 3 | 1.5 mm |

**What JLC will cut at standard price.** VERIFIED,
[JLCPCB PCB capabilities](https://jlcpcb.com/capabilities/pcb-capabilities):

- **Minimum non-plated slot: 1.0 mm.** ← the one you want (a plated slot is
  copper; it isolates nothing).
- Minimum plated slot: 0.5 mm (2-layer). Slot length ≥ 2× width.
- Copper clearance from routed board edges: ≥ 0.2 mm.
- Min track/space, standard 2-layer, 1 oz: 0.10 / 0.10 mm.
- **Surcharge: not stated for slots or milled cutouts** on that page.
  The repo's own `fab.py jlcpcb-standard` profile (verified 2026-07-19) lists
  the surcharge triggers that apply to copper geometry — via hole < 0.3 mm,
  trace/space 2–3 mil, >150k drill holes/m² — and slots are not among them.
  **UNVERIFIED that milled internal slots are free**; JLC's
  [extra-charge article](https://jlcpcb.com/help/article/in-what-cases-will-there-be-charged-extra)
  is the page to re-read at order time. Treat "free" as likely but unconfirmed.

**The coincidence that decides it: JLC's 1.0 mm minimum non-plated slot is
exactly the IEC PD2 minimum groove width.** You are at the boundary with zero
margin, on both the fab tolerance and the pollution-degree assumption. **Cut
1.5 mm slots**, not 1.0 mm: it buys routing tolerance, and it satisfies the
PD3 threshold too, which is the one that actually applies to an amp that gets
gigged.

**Does a slot reset creepage to the clearance value?** For a *groove* (blind,
one side) the path follows the contour down and up, so a groove of depth d
adds ~2d. For a **full through-slot**, the surface between the two conductors
is severed — there is no continuous solid surface to track along, and the
shortest path between the conductors is straight across the slot **through
air**, i.e. a pure clearance. **UNVERIFIED as an explicit sentence in a
primary source** — I could not find IEC or IPC stating the reset in those
words. What *is* verified is the ≥X groove rule and the definition of creepage
as a path "along the surface of a solid insulating material" (SLUP421 slide 4),
from which the reset follows directly, and it is standard SMPS practice
(SLUP421's slide 22 exists precisely to show a cutout used this way).
Treat the reset as the strong industry reading, not as a quoted requirement.

**Sizing the slot, then.** If the slot converts the requirement to clearance,
the number to satisfy is §1.4 B2 / §1.5 across the air gap — but those are
*bare-board spacing* numbers that bundle in surface effects, not pure air
breakdown. Pure air at sea level per IEC 60664-1 Table F.2 needs 0.5 mm at a
1.5 kV<sub>PK</sub> transient and 0.2 mm at 0.5 kV<sub>PK</sub>, pollution degree 2 (VERIFIED,
SLUP421 slide 16) — 720 V across a 1.5 mm air gap is not close to breakdown.
**A 1.5 mm through-slot plus 0.5 mm copper-to-slot-edge on each side ≈ 2.5 mm
total replaces the 7.2 mm bare-creepage drain-to-drain corridor.** That beats
coating (2.2 mm) once you count the coating's caveats, and it is permanent,
inspectable, and survives you modifying the board.

Two honest concerns with slots:

- **A routed slot edge is freshly exposed glass fibre**, which wicks moisture
  and is a worse surface than the soldermasked laminate face. This matters less
  than it sounds because a through-slot's shortest path is air, not that
  surface — but keep copper ≥ 0.5 mm back from the slot edge (JLC's 0.2 mm
  minimum is a *manufacturing* number, not a safety margin) and do not run an
  HV net along a slot edge for its full length.
- **A slot is board area you cannot route through on either layer.** On a
  2-layer board that is a real floorplanning cost, and it means the slot must
  be planned during placement, not discovered during routing.

**Verdict: slots are practical between the FET drains and adjacent nets, and
are the best single mitigation available.** They are most valuable as *one or
two long slots isolating the whole drain/OT-primary island* from the rest of
the board — not as many short slots between individual net pairs, which cost
the same area and buy less.

### 3.2 Layout zoning — free, and it is most of the answer

The 720 V requirement applies to a **pair**, and there are exactly **two** nets
at 720 V. Everything else on the board is at 360 V or below, and ~450 nets are
below 24 V. So:

- Put `Output PA1`, `Output PA2`, the OT primary connection, C75/C76 and the
  FET drain pads in **one compact island at one edge of the board**, physically
  adjacent to the transformer.
- The only 7.2 mm gap on the whole board is then PA1↔PA2, over a short run —
  and if they are on opposite sides of a slot, it is not even that.
- Nothing else needs to be within 5.1 mm of a drain, because nothing else needs
  to be near the island at all.
- Keep the 360 V rails in a second zone adjacent to it, and the logic/relay/
  audio in a third. Signal flows one way; so should voltage. This is the same
  rule ANALOG_SCORING.md §M7 `stage_order_monotonicity` states for noise, and
  it happens to be the HV rule too.

**Zoning is what makes the uncoated numbers affordable.** See §3.3.

### 3.3 Increased spacing alone — what it actually costs in mm

DERIVED. A parallel pair of drains needs a corridor of
`7.2 (between them) + 5.1 (skirt) + 5.1 (skirt) = 17.4 mm` of sterilised board
width for as long as they run together. On the 300 mm dimension:

| drain-pair run length | sterilised area, uncoated | same, coated (2.2/2.2/2.2 → 6.6 mm) | same, slotted (~2.5 mm) |
|---|---|---|---|
| 300 mm (crosses the board) | 5,220 mm² | 1,980 mm² | 750 mm² |
| 100 mm | 1,740 mm² | 660 mm² | 250 mm² |
| **30 mm (zoned island)** | **522 mm²** | 198 mm² | 75 mm² |

A 522 mm² keep-out is a ~23 × 23 mm patch. On a board with a 300 mm dimension
that is a small single-digit percentage of the area, and it costs **nothing**
in money, rework, or maintenance.

**This is the crux of the recommendation:** the uncoated numbers are only
expensive if the HV crosses the board. Zoned, they are cheap. The right lever
is §3.2, and it is free.

The 360 V rails are the wider-reaching cost — `B+`, `C+`, `D+` and three plate
nodes have to get from the supply to their loads, and each needs 3.7 mm to
anything at a different potential. Two mitigations: run them **as a bundle**
(rail-to-rail is a much smaller potential difference — B+ to C+ might be 20 V,
needing 0.6 mm, not 3.7 mm), and let the 3.7 mm apply only at the bundle's
outer boundary. A 4-wide 360 V bundle with 0.6 mm internal gaps and a 3.7 mm
skirt is ~11 mm wide, not ~19 mm.

### 3.4 Guard traces / grounded copper — honest answer: **it does not reduce the spacing**

A grounded guard between a 720 V drain and a logic net splits one gap into
two. But the drain-to-guard gap now carries the *full* drain-to-ground
potential, so it still needs the full 5.1 mm. The guard-to-logic gap needs
~0.1 mm. Total: 5.2 mm — **the same board width you needed without the guard.**

**A guard trace buys zero millimetres. Do not use it as a spacing mitigation.**

It does buy something else, and on *this* board that something is worth having:
**fault determinism.** Without a guard, a tracking failure from a 360 V plate
node finds whatever is nearest — which on this board could be a logic net, and
logic nets connect to an Arduino, and the audio ground connects to the input
jack sleeve, and the input jack sleeve connects to the guitar strings in the
player's hands. With a grounded guard between the HV zone and the logic zone,
the *first* thing any tracking fault reaches is ground, and the fault becomes a
blown fuse instead of B+ on the strings. **Put a grounded guard band along the
boundary between the HV zone and the logic zone**, size it to carry fault
current (not a hair-thin trace), and bond it properly — but count its width as
part of the spacing budget, not as a discount on it.

### 3.5 Other standard practice worth adopting

- **No sharp copper corners or acute angles on HV nets.** Field concentrates at
  points and initiates corona and tracking. Round the corners; avoid acute
  angle intersections. (Standard HV layout practice; I did not locate a primary
  clause requiring it — treat as craft, not law.)
- **Keep HV back from the board edge.** Routed board edges are exposed glass.
  JLC's 0.2 mm edge clearance is a manufacturing minimum, not an HV number. Use
  ≥ 2–3 mm for the 360 V nets and more for the drains.
- **Watch hole-to-hole on HV nets.** Sustained DC across FR-4 between plated
  holes grows conductive anodic filaments through the glass bundles. JLC's
  0.45 mm hole-to-hole is a drill number. A safe CAF spacing for 360 V DC is
  **UNVERIFIED** here — the governing bodies are IPC-9691 and IPC-TM-650
  method 2.6.25, which I did not obtain. Directionally: keep HV vias well
  apart and avoid running HV vias in a row parallel to the glass weave.
- **Bleeder resistors and a discharge path.** Non-negotiable on a board with
  360 V reservoir caps that you intend to put your hands in. Add a test point
  where you can confirm it is discharged.
- **Check the component-level ratings, not just the copper.** Two specific
  worries on this board, both **UNVERIFIED against your BOM**:
  - Standard 1206 thick-film resistors typically carry a **200 V maximum
    working voltage** rating, independent of power. The `VMID` divider is
    1M/1M off ~360 V → **180 V per resistor**, which is at the limit with
    almost no margin. If any single 1206 on the board sees more than ~180 V,
    split it into two in series. Standard practice in HV SMD design.
  - A 1206's own terminal-to-terminal gap is roughly **1.6–1.8 mm** — below
    the 2.2 mm creepage needed at 180 V rms and far below the 3.7 mm needed at
    360 V. **Any component with 360 V across its own body needs a longer
    package** (2010, 2512) **or series parts.** No amount of routing clearance
    fixes a part whose own terminals are too close together.
  - The TO-220 lead pitch is 2.54 mm, fixed by the package, with drain at
    720 V peak and gate near 0 V. You are not required to beat the package's
    own qualification — TI SLUP421 slide 25 tabulates real vendors shipping
    650 V parts in TO-220-3 with **1.11 mm** of high-voltage spacing — but the
    PCB copper leading away from those pins should meet the board number, and
    a short slot between the drain and gate pads under the package is common
    SMPS practice and cheap here.
- **HiPot as an alternative to spacing.** For *functional* insulation, IEC
  permits meeting the creepage/clearance tables **or** passing an electric
  strength routine test — VERIFIED, SLUP421 slide 26, citing IEC 60664-1
  §5.2.2.1/5.1.3.3, IEC 60950-1 §5.3.4, IEC 62368-1 §B.4.4. Noted for
  completeness. It is not a route a hobbyist should take: it means testing
  every board you build, on equipment you do not have.

---

## 4. Recommendation, with a decision rule

### The honest headline

**You can skip conformal coating on this board. You cannot skip it *and* keep
the drains where a general-purpose autorouter would put them.**

Coating is not required by any number in §1. What is required is that the
copper meet §1.7's uncoated column, and the only reason that column looks
frightening is that its worst rows apply to **two nets**. Zone those two nets
and the cost collapses (§3.3: 522 mm², about a 23 mm square). That is a far
better trade than a coating whose safety credit depends on a hand application
you will breach the first time you modify the board (§2.2 caveat 5).

### Decision rule — you may skip coating **if and only if all six hold**

1. **`Output PA1` and `Output PA2` are confined to one compact island** at the
   transformer end of the board, with **no logic, relay, or low-level audio net
   entering that island** — nothing crosses it, nothing routes through it.
2. **PA1↔PA2 separation ≥ 7.2 mm** everywhere, **or** they are separated by a
   **≥ 1.5 mm non-plated through-slot** with ≥ 0.5 mm copper-to-slot-edge on
   each side.
3. **Every drain net keeps ≥ 5.1 mm to any net at or near ground**, including
   pads, vias, silkscreen-adjacent copper, and the board edge — or is separated
   from it by a slot as in (2).
4. **Every ~360 V net (`B+`, `C+`, `D+`, the three plate nodes, the HV side of
   the coupling caps) keeps ≥ 3.7 mm to anything at a materially different
   potential.** Rails at similar potentials may bundle tightly; the 3.7 mm
   applies at the bundle boundary.
5. **A grounded guard band runs along the whole HV-zone / logic-zone
   boundary**, wide enough to carry a fault, properly bonded — counted as board
   width, not as a spacing discount (§3.4). This is what keeps a tracking
   failure off the guitar strings.
6. **No component has more voltage across its own body than its package
   permits** — no 1206 above ~180 V, nothing whose own terminal gap is below
   the §1.7 number for the voltage across it, series parts where needed
   (§3.5).

### If you cannot hold all six — the "unless you do X" sentence, undiluted

**If the drains cannot be zoned into an island — if `Output PA1` or
`Output PA2` has to cross the board or pass within 5.1 mm of any grounded or
logic net — then you need either a ≥ 1.5 mm non-plated through-slot along the
entire length of that approach, or a qualified conformal coating over the
assembled board. Not solder mask. Not "I'll be careful." One of those two, for
the entire length of the approach.** A 720 V node with 1 mm to a 5 V logic net
under a soldermask that has a pinhole at a via tent is how B+ gets onto an
Arduino pin, and from there onto the ground the input jack shares with the
guitar in your hands.

If you must coat: use **silicone**, not acrylic, because of the valve heat
(§2.2 caveat 4); apply it to a board cleaned of flux; and **write the fact that
the board is designed to coated spacings on the silkscreen**, so that the
future version of you doing a mod at 1 a.m. knows that scraping the coating off
and not putting it back is not a cosmetic decision.

### And regardless of all of the above

Bleeders, a discharge test point, and the habit of measuring before touching.
None of §1–§3 makes a 360 V reservoir cap safe to touch, and this document is
about the board surviving; you surviving is a separate discipline.

---

## 5. What this means for Orchard Route

### 5.1 Net classes to declare

Two full sets. The numbers are the §1.7 binding values, rounded up to a clean
grid-friendly figure.

**Set A — uncoated build (recommended, paired with the §4 zoning rule)**

| class | nets | clearance | track width | note |
|---|---|---|---|---|
| `HV_SWING` | `Output PA1`, `Output PA2` | **7.2 mm** | 0.5 mm | drain↔drain governs; 5.1 mm would do to everything else, but the router carries one number per class |
| `HV_360` | `B+`, `C+`, `D+`, `Plate Audio Out P1/T1/T2`, `Net-(C8/C9/C28-Pad2)` | **3.7 mm** | 0.4 mm | creepage @360 V rms, PD2/MG IIIa |
| `HV_150` | `VMID`, cascode mid-nodes | **2.0 mm** | 0.3 mm | creepage @150 V rms |
| `Default` | logic, 24 V relay, low-level audio | 0.2 mm | 0.25 mm | 2× JLC's 0.1 mm floor |

**Set A′ — uncoated, with the drain island slotted (best area/cost point)**

`HV_SWING` drops to **1.5 mm** *inside the slotted island only*; the slot
carries the isolation to everything outside it. This is a constraint the router
cannot express today (there is no slot or board-cutout model) — declare the
island as a region with `HV_SWING` at 1.5 mm and treat the slot as a
placement-time, human-authored feature. **Disclose that in any report:** the
router is honoring 1.5 mm on the assumption a slot exists that it cannot see.

**Set B — qualified conformal coating over the assembly**

| class | clearance | driven by |
|---|---|---|
| `HV_SWING` | **2.2 mm** | IPC-2221B A5 @720 V peak, conservative Reading B |
| `HV_360` | **1.0 mm** | creepage PD1 (0.91) |
| `HV_150` | **0.5 mm** | creepage PD1 (0.41) / IPC B4 (0.40) |
| `Default` | 0.2 mm | |

Set B is offered for completeness. **Do not adopt it without also adopting the
§2.2 caveats**, particularly that a hand-applied coat is not formally entitled
to the PD1 numbers and that the board will eventually be modified.

### 5.2 Halo cost per class — the layout price, made visible

Per-net-class clearance is enforced via exclusion halos: a net claims every
grid node within `clearance + track_width` of its centreline, and those nodes
are unavailable to every other net. Cost is therefore quadratic in clearance.

**Two counts are given, and they differ — this is a real discrepancy worth
knowing.** `geometry.halo_offsets()` as shipped claims a **Euclidean disk**;
the ring-count ("square") figure is the Chebyshev bound — the corridor width in
node-rings, which is what you feel when reading a layout. The brief's stated
"1.0 mm → 24 nodes / 2.5 mm → 80 nodes" are the **square** figures; the shipped
disk gives 20 and 68 for those cases. Both are reported below; the square
number is the conservative one to plan with.

Computed by calling `geometry.halo_offsets()` directly, not by formula.

**At 0.6 mm pitch:**

| class (set) | clearance | track W | halo radius | rings | disk nodes | square nodes |
|---|---|---|---|---|---|---|
| `Default` | 0.20 | 0.25 | 0.45 | 0 | **0** | 0 |
| `HV_150` coated (B) | 0.50 | 0.30 | 0.80 | 1 | **4** | 8 |
| `HV_360` coated (B) | 1.00 | 0.40 | 1.40 | 2 | **20** | 24 |
| `HV_SWING` slotted (A′) | 1.50 | 0.50 | 2.00 | 3 | **36** | 48 |
| `HV_150` uncoated (A) | 2.00 | 0.30 | 2.30 | 3 | **44** | 48 |
| `HV_SWING` coated (B) | 2.20 | 0.50 | 2.70 | 4 | **68** | 80 |
| *IPC-2221B B2 only, 360 V* | *2.50* | *0.40* | *2.90* | *4* | *68* | *80* |
| `HV_360` uncoated (A) | 3.70 | 0.40 | 4.10 | 6 | **144** | 168 |
| *IPC-9592B, 720 V peak* | *4.20* | *0.50* | *4.70* | *7* | *192* | *224* |
| `HV_SWING` drain↔other (A) | 5.10 | 0.50 | 5.60 | 9 | **276** | 360 |
| `HV_SWING` drain↔drain (A) | 7.20 | 0.50 | 7.70 | 12 | **516** | 624 |

**At 0.5 mm pitch** (AGENTS.md's recommendation for a 1206-class board):

| class | clearance | halo radius | rings | disk nodes | square nodes |
|---|---|---|---|---|---|
| `Default` | 0.20 | 0.45 | 0 | 0 | 0 |
| `HV_150` coated | 0.50 | 0.80 | 1 | 8 | 8 |
| `HV_360` coated | 1.00 | 1.40 | 2 | 20 | 24 |
| `HV_SWING` slotted | 1.50 | 2.00 | 4 | 48 | 80 |
| `HV_150` uncoated | 2.00 | 2.30 | 4 | 68 | 80 |
| `HV_SWING` coated | 2.20 | 2.70 | 5 | 96 | 120 |
| `HV_360` uncoated | 3.70 | 4.10 | 8 | 212 | 288 |
| `HV_SWING` drain↔other | 5.10 | 5.60 | 11 | 400 | 528 |
| `HV_SWING` drain↔drain | 7.20 | 7.70 | 15 | 748 | 960 |

### 5.3 What the table is telling the tool

- **`Default` is free.** At 0.2 mm clearance and 0.25 mm width the halo radius
  is 0.45 mm, below one pitch — zero extra nodes claimed. The 450-odd logic
  nets cost nothing.
- **The coated set is nearly free too.** `HV_360` at 1.0 mm claims 20 nodes; on
  a board with thousands of nodes, unnoticeable.
- **The uncoated `HV_360` class at 3.7 mm claims 144 nodes per node of path**
  and is the number that will actually shape the layout. Six nets at that cost
  is where the board area goes, not the two drains.
- **`HV_SWING` uncoated is not a routing problem at all.** A 516-node halo at
  0.6 mm pitch means the router cannot thread that net through anything; any
  candidate it produces will be a straight run through empty board. **The right
  encoding is a keep-out region and a hand-placed island, not a clearance
  number for the negotiator to fight.** Telling the router to route a 7.2 mm
  net through a populated region is asking it to fail slowly instead of failing
  immediately.
- **This is the case for M8 being a legality tier, not a score** — as
  ANALOG_SCORING.md §3 already argues. A candidate that violates the §1.7
  numbers must never outrank one that does not, at any wirelength.

### 5.4 Corrections this document makes to ANALOG_SCORING.md §M8

1. §M8's numbers (0.6 / 1.25 / 2.5 mm) are the IPC-2221B **B2 clearance**
   column. **Clearance is not the binding requirement on an uncoated tube-amp
   board** — IEC 60664-1 creepage at PD2/MG IIIa exceeds it at every voltage on
   this board (3.7 vs 2.5 mm at 360 V; 7.2 vs 3.6 mm at 720 V). M8 as written
   under-specifies by ~1.5–2×.
2. §M8 says "solder mask does not qualify as polymer coating under IPC
   (standard industry reading)". That was true of IPC-2221B and **IPC-2221C
   explicitly changed it** to name solder mask as the B4 condition. The
   sentence should be dated to the revision. It does not change the conclusion,
   because solder mask earns no IEC pollution-degree credit and creepage binds
   anyway — but the doc should say *why* it does not change the conclusion
   rather than resting on a claim the newer revision contradicts.
3. §M8's `s_req(V_class)` takes one voltage per class. **The requirement is a
   property of a pair, not of a net** (§1.2), and on this board the pairwise
   view is worth 2× of board area — drain↔B+ needs 3.7 mm where drain↔ground
   needs 5.1 mm. A `clearance_min(class_a, class_b, mm)` matrix, which
   ANALOG_SCORING.md §2 already proposes, expresses this correctly; the
   single-number-per-class form does not.
4. §M8 should carry a **working-voltage** field alongside the peak, because
   creepage and clearance read different columns (§1.1). One number per class
   cannot drive both.

---

## 6. Source ledger

**Primary standards** (referenced; the tables themselves are copyrighted and
are reproduced here only as the specific values needed for this board):

- **IPC-2221B**, *Generic Standard on Printed Board Design*, Nov 2012. §6.3
  Electrical Clearance (p. 56), Table 6-1 Electrical Conductor Spacing (p. 57).
  Table of contents: <https://www.electronics.org/TOC/IPC-2221B.pdf> (VERIFIED
  — I extracted this TOC and confirmed the section/table/page numbering and the
  A5 conformal-coating column heading; the TOC does **not** contain the table
  values, which came from the secondary sources below).
- **IEC 60664-1**, *Insulation coordination for equipment within low-voltage
  supply systems*. §3.5 (working voltage), §6.2 (groove dimension X),
  Table F.2 (clearance), Table F.4 (creepage). Not obtained directly.
- **IPC-9592B**, *Requirements for Power Conversion Devices*. Spacing formula
  0.6 + V<sub>PK</sub> × 0.005. Not obtained directly.
- **IEC 62368-1**, which **superseded IEC/UL 60065 (audio equipment) and
  IEC/UL 60950-1 with effect from 20 December 2020** in both the EU (date of
  withdrawal) and North America (UL/CSA effective date) — so 60065 is the
  wrong standard to reach for on a new amp design.
  ([Altium summary](https://resources.altium.com/p/iec-62368-1-replace-60950-1-and-60065-safety-standards),
  [Eurofins FAQ](https://metlabs.com/product-safety/faqs-iec-62368-1-replacing-iec-60950-1-iec-60065/)).
  Relevant tables: 17/10 (uncoated), G.13 (coated). Not obtained directly.
- **IEC 60664-3**, protection by coating — the route to the PD1 credit. Not
  obtained directly.
- **IPC-CC-830**, conformal coating material qualification.
- **IPC-9691 / IPC-TM-650 2.6.25**, CAF — named as the governing bodies for the
  hole-to-hole question I could not answer.

**Reputable secondary, and the workhorse of this document:**

- **TI Power Supply Design Seminar SLUP421**, Wei Zhang & Thomas LaBella,
  *Demystifying clearance and creepage distance for high-voltage end
  equipment* — <https://www.ti.com/lit/pdf/SLUP421>. Fetched and text-extracted
  2026-07-19. Source of: the CPG/CLR definitions and governing voltages
  (slide 4), material-group CTI ranges and "most FR4 PCBs" = IIIa (slide 6),
  pollution-degree definitions with "coated PCB" as the PD1 example (slide 7),
  the working-voltage definition footnote and the coating→PD1 note (slide 14),
  IEC 60664-1 Table F.2 clearance values (slide 16), Table F.4 creepage values
  (slide 17), the groove-width X table citing IEC 60664-1 §6.2 (slide 22), the
  IPC-9592B formula and the IPC/IEC comparison table (slide 24), real vendor
  package spacings (slide 25), and the HiPot alternative (slide 26).
  Vendor-published seminar material, but it cites clause and table numbers
  throughout and is internally consistent — the highest-quality source I could
  obtain without buying the standards.
- IPC-2221B Table 6-1 values, full table:
  <https://www.sfcircuits.com/pcb-school/pcb-line-spacing-clearance-creepage>
  (SECONDARY).
- Altium, *Using an IPC-2221 Calculator for High Voltage Design*:
  <https://resources.altium.com/p/using-an-ipc-2221-calculator-for-high-voltage-design>
  (SECONDARY) — source of the B1–B4 column definitions, the IPC-2221C solder-
  mask change, the worked >500 V "increment" example, and the statement that
  IPC-2221 does not cover creepage.
- <https://www.smpspowersupply.com/ipc2221pcbclearance.html> (SECONDARY) —
  corroborates the >500 V increment form for the uncoated column.
- <https://www.protoexpress.com/blog/ipc-2221-circuit-board-design/> (SECONDARY)
  — corroborates the per-volt rates, **except** it prints the internal-conductor
  rate as 0.025 mm/V where Altium's worked example and sfcircuits both give
  0.0025 mm/V. **Treated as a typo in protoexpress**; 0.0025 used.
- **JLCPCB PCB capabilities**: <https://jlcpcb.com/capabilities/pcb-capabilities>
  (PRIMARY for the fab) — min non-plated slot 1.0 mm, min plated slot 0.5 mm
  (2-layer), slot length ≥ 2× width, copper-to-routed-edge ≥ 0.2 mm,
  track/space 0.10/0.10 mm at 1 oz on the standard 2-layer service.
  Surcharge article: <https://jlcpcb.com/help/article/in-what-cases-will-there-be-charged-extra>.
  Cross-checked against this repo's `fab.py jlcpcb-standard` profile, verified
  2026-07-19.
- **Vishay IRFBG30 datasheet**, doc 91124:
  <https://www.vishay.com/docs/91124/irfbg30.pdf> (PRIMARY) — V<sub>DSS</sub> 1000 V.
- Conformal coating temperature classes (acrylic ~125 °C, silicone ~200 °C):
  <https://www.andwinpcb.com/acrylic-vs-silicone-vs-urethane-conformal-coating-complete-selection-guide-for-pcb-protection/>
  (SECONDARY / vendor). IPC-CC-830 overview:
  <https://resources.pcb.cadence.com/blog/2020-understanding-ipc-conformal-coating-standards>.
- IEC 60664-1 peak-vs-RMS framing corroboration:
  <https://standardclarity.com/calculators/creepage-clearance/> (SECONDARY).

**Explicitly UNVERIFIED, flagged in place, not filled in:**

1. JLCPCB's CTI / material group for standard FR-4 — assumed IIIa (§1.6).
2. Whether milled internal slots incur a JLCPCB surcharge — probably not, not
   confirmed (§3.1).
3. IEC 60664-1 Table F.4's intermediate voltage rows — interpolated across
   verified anchors instead (§1.6).
4. IEC 60664-1 PD3 creepage values (§1.6).
5. An explicit primary-source sentence stating that a through-slot resets
   creepage to clearance — derived from the definitions, standard practice,
   not quoted (§3.1).
6. CAF / hole-to-hole spacing for 360 V DC (§3.5).
7. Whether C75/C76 sit drain-to-B+ (fine at 630 V) or drain-to-ground /
   drain-to-drain (under-rated at 720 V) — **check this** (§0).
8. The actual voltage across each 1206 on the board, and each part's own
   working-voltage rating (§3.5).
9. Which of IPC-2221B's two >500 V readings the standard intends — both given,
   conservative used (§1.4).

**One non-standards caveat, stated once:** this is engineering analysis of a
hobby build, not a compliance assessment. None of these standards legally binds
a one-off amp you build for yourself. They are used here because they encode
the best available consensus on what spacing keeps a 720 V node from finding a
path it should not, and because the person who will be inside this chassis with
the power on is the person reading this.
