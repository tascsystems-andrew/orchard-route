"""Tests for the manufacturing contract (fab.py).

Every limit asserted here is transcribed from the profile's cited source, and
every pitch arithmetic assertion is computed by hand from geometry.py's rules
rather than read back from fab.py — the point of the module is to be checkable
against a fab house's web page, so the tests must be checkable the same way.
"""
import datetime
import json
import os
import tempfile

import fab
from geometry import CopperGeometry

FAILED = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ── loading ─────────────────────────────────────────────────────────────
def test_loading():
    print("=== profile loading ===")
    names = fab.list_profiles()
    for required in ("none", "jlcpcb-standard", "jlcpcb-extended",
                     "pcbway-standard", "pcbway-extended"):
        check(f"{required} present", required in names)
    check("none is listed first", names[0] == "none")

    p = fab.load_profile("jlcpcb-standard")
    check("name round-trips", p.name == "jlcpcb-standard")
    check("house is JLCPCB", p.house == "JLCPCB")
    check("tier is standard", p.tier == "standard")
    check("verified_on is the research date", p.verified_on == "2026-07-19",
          p.verified_on)
    check("constrains", p.constrains)

    # The load-bearing numbers, from the cited pages.
    check("JLC standard track 0.10", abs(p.min_track_mm - 0.10) < 1e-12)
    check("JLC standard clearance 0.10", abs(p.min_clearance_mm - 0.10) < 1e-12)
    check("JLC standard via pad 0.45", abs(p.min_via_diameter_mm - 0.45) < 1e-12)
    check("JLC standard via drill 0.30", abs(p.min_via_drill_mm - 0.30) < 1e-12)

    w = fab.load_profile("pcbway-standard")
    check("PCBWay standard track 6 mil", abs(w.min_track_mm - 0.1524) < 1e-9,
          f"{w.min_track_mm:.4f}")
    check("PCBWay standard via drill 0.30", abs(w.min_via_drill_mm - 0.30) < 1e-12)
    check("PCBWay standard ring 0.15", abs(w.min_annular_ring_mm - 0.15) < 1e-12)
    check("PCBWay standard via pad = drill + 2*ring",
          abs(w.min_via_diameter_mm - (0.30 + 2 * 0.15)) < 1e-12,
          f"{w.min_via_diameter_mm}")

    n = fab.load_profile("none")
    check("none does not constrain", not n.constrains)
    check("none from empty string", not fab.load_profile("").constrains)
    check("none from None", not fab.load_profile(None).constrains)

    try:
        fab.load_profile("jclpcb-standrad")
        check("unknown profile raises", False)
    except fab.UnknownProfile as e:
        check("unknown profile raises", True)
        check("error names the alternatives", "jlcpcb-standard" in str(e))


def test_every_number_is_cited():
    print("=== every number carries a source ===")
    for name in fab.list_profiles():
        p = fab.load_profile(name)
        if not p.constrains:
            continue
        missing = [f for f in fab._LIMIT_FIELDS
                   if getattr(p, f) is not None and not p.source_for(f)]
        check(f"{name}: all limits cited", not missing, str(missing))
        check(f"{name}: source is a URL",
              p.source_for("min_track_mm").startswith("https://"))
        check(f"{name}: has surcharge notes", bool(p.notes.strip()))


def test_user_extension_is_data():
    print("=== a user can add a house without touching logic ===")
    payload = {"acme-standard": {
        "house": "Acme", "tier": "standard", "layers": "1-2",
        "verified_on": "2026-07-19",
        "min_track_mm": 0.3, "min_clearance_mm": 0.3,
        "min_via_diameter_mm": 0.9, "min_via_drill_mm": 0.5,
        "min_annular_ring_mm": 0.2,
        "sources": {"*": "jlc-cap"}, "notes": "made up for the test"}}
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "extra.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            added = fab.load_profiles_file(path)
            check("profile added", added == ["acme-standard"], str(added))
            p = fab.load_profile("acme-standard")
            check("its numbers load", abs(p.min_track_mm - 0.3) < 1e-12)
            check("it is listed", "acme-standard" in fab.list_profiles())
            geo = CopperGeometry(pitch_mm=1.0, track_width_mm=0.2,
                                 clearance_mm=0.4, via_size_mm=1.0)
            v = fab.check(geo, p)
            check("check() works on it unchanged",
                  [x.field for x in v] == ["min_track_mm"],
                  str([x.field for x in v]))
        finally:
            fab.PROFILE_DATA.pop("acme-standard", None)
    check("cleanup", "acme-standard" not in fab.list_profiles())


# ── check() ─────────────────────────────────────────────────────────────
def test_check_catches_each_violation_type():
    print("=== check() catches each violation type ===")
    p = fab.load_profile("jlcpcb-standard")

    legal = CopperGeometry(pitch_mm=0.5, track_width_mm=0.2,
                           clearance_mm=0.2, via_size_mm=0.45)
    check("legal board is clean", fab.check(legal, p, via_drill_mm=0.3) == [],
          str([str(v) for v in fab.check(legal, p, via_drill_mm=0.3)]))

    def fields(geo, drill=None):
        return sorted(v.field for v in fab.check(geo, p, via_drill_mm=drill))

    thin = CopperGeometry(pitch_mm=0.5, track_width_mm=0.08,
                          clearance_mm=0.2, via_size_mm=0.45)
    check("thin track flagged", fields(thin) == ["min_track_mm"], str(fields(thin)))

    tight = CopperGeometry(pitch_mm=0.5, track_width_mm=0.2,
                           clearance_mm=0.09, via_size_mm=0.45)
    # 0.09 is below the 0.10 copper minimum AND below the 0.20 via gaps.
    check("tight clearance flags all three clearance rules",
          fields(tight) == ["min_clearance_mm", "min_via_to_track_mm",
                            "min_via_to_via_mm"], str(fields(tight)))

    smallvia = CopperGeometry(pitch_mm=0.5, track_width_mm=0.2,
                              clearance_mm=0.2, via_size_mm=0.35)
    check("small via pad flagged", fields(smallvia) == ["min_via_diameter_mm"],
          str(fields(smallvia)))

    check("small drill flagged",
          fields(legal, drill=0.2) == ["min_via_drill_mm"],
          str(fields(legal, drill=0.2)))

    # ring = (0.45 - 0.40)/2 = 0.025 < 0.05, and 0.40 < 0.30 is false, so the
    # ring rule fires alone.
    check("thin annular ring flagged",
          fields(legal, drill=0.40) == ["min_annular_ring_mm"],
          str(fields(legal, drill=0.40)))

    check("drill omitted -> drill+ring rules SKIPPED, not assumed",
          fields(legal) == [], str(fields(legal)))

    # via-to-via is the rule that separates the two houses' clearances.
    w = fab.load_profile("pcbway-standard")
    mid = CopperGeometry(pitch_mm=0.8, track_width_mm=0.2,
                         clearance_mm=0.2, via_size_mm=0.6)
    wf = sorted(v.field for v in fab.check(mid, w))
    check("PCBWay flags 0.2 clearance on via-to-via",
          wf == ["min_via_to_via_mm"], str(wf))
    check("JLC does NOT flag the same board", fab.check(mid, p) == [])


def test_violation_messages_are_readable():
    print("=== violation messages state number, limit and source ===")
    p = fab.load_profile("jlcpcb-standard")
    geo = CopperGeometry(pitch_mm=0.5, track_width_mm=0.08,
                         clearance_mm=0.2, via_size_mm=0.45)
    v = fab.check(geo, p)[0]
    msg = str(v)
    check("names the value", "0.08" in msg, msg)
    check("names the limit", "0.1mm" in msg, msg)
    check("names the profile", "jlcpcb-standard" in msg)
    check("names the source URL", "https://jlcpcb.com" in msg)
    check("Violation carries the numbers",
          abs(v.value_mm - 0.08) < 1e-12 and abs(v.limit_mm - 0.10) < 1e-12)
    warns = fab.violation_warnings([v], p)
    check("warning block is loud", "FAB VIOLATION" in warns[0], warns[0])
    check("warning says nothing was changed", "NOT changed" in warns[0])
    check("warning names the escape hatch", "--fab-enforce" in warns[0])


def test_none_profile_changes_nothing():
    print("=== the `none` profile imposes nothing ===")
    n = fab.load_profile("none")
    absurd = CopperGeometry(pitch_mm=0.1, track_width_mm=0.001,
                            clearance_mm=0.001, via_size_mm=0.002)
    check("check() is empty", fab.check(absurd, n, via_drill_mm=0.001) == [])
    check("recommend() returns None", fab.recommend(n, 0.5) is None)
    check("no stale warning", n.stale_warning() is None)
    out = fab.reconcile(absurd, n, 0.5, via_drill_mm=0.001, enforce=True)
    check("reconcile changes nothing even with enforce", out.changes == [])
    check("reconcile reports no violations", out.violations == [])
    check("reconcile leaves every override unset",
          (out.track_mm, out.clearance_mm, out.via_size_mm,
           out.via_drill_mm) == (None, None, None, None))
    check("summary_line says so", "no manufacturing constraints"
          in fab.summary_line(absurd, n))
    c, t, v, notes = fab.fill_defaults("/nonexistent.kicad_pcb", n, 0.5,
                                       None, None, None)
    check("fill_defaults is a no-op", (c, t, v, notes) == (None, None, None, []))


# ── recommend() ─────────────────────────────────────────────────────────
def _pitch_ok(rec, pitch):
    """geometry.py's two rules, restated by hand."""
    return (rec.track_mm + rec.clearance_mm <= pitch + 1e-9 and
            rec.via_size_mm / 2 + rec.track_mm / 2 + rec.clearance_mm
            <= pitch + 1e-9)


def test_recommend_is_fab_legal_and_fits_the_pitch():
    print("=== recommend() returns legal copper that fits the pitch ===")
    p = fab.load_profile("jlcpcb-standard")
    rec = fab.recommend(p, 0.5)
    check("unpacks as 4 numbers", len(tuple(rec)) == 4)
    t, c, v, d = rec
    check("track is the profile floor", abs(t - 0.10) < 1e-12)
    # clearance is raised to the via-to-via / via-to-track gap, which is the
    # binding rule for a router that emits ONE clearance for all copper.
    check("clearance raised to the via gap 0.20", abs(c - 0.20) < 1e-12, str(c))
    check("via is the profile floor", abs(v - 0.45) < 1e-12)
    check("drill is the profile floor", abs(d - 0.30) < 1e-12)

    check("recommendation is itself fab-legal",
          fab.check(CopperGeometry(pitch_mm=0.5, track_width_mm=t,
                                   clearance_mm=c, via_size_mm=v),
                    p, via_drill_mm=d) == [])
    # 0.10 + 0.20 = 0.30 <= 0.5 ; 0.225 + 0.05 + 0.20 = 0.475 <= 0.5
    check("orthogonal rule satisfied by hand", t + c <= 0.5 + 1e-9,
          f"{t + c:.4f}")
    check("via halo rule satisfied by hand",
          abs(v / 2 + t / 2 + c - 0.475) < 1e-9, f"{v / 2 + t / 2 + c:.4f}")
    check("_pitch_ok agrees", _pitch_ok(rec, 0.5))

    check("required_pitch_mm matches the halo term",
          abs(fab.required_pitch_mm(t, c, v) - 0.475) < 1e-9)

    for name in ("jlcpcb-standard", "jlcpcb-extended", "pcbway-standard",
                 "pcbway-extended", "jlcpcb-standard-4layer",
                 "pcbway-standard-4layer"):
        prof = fab.load_profile(name)
        need = fab.required_pitch_mm(
            *[x for x in (fab.recommend(prof, 10.0))][:3])
        r = fab.recommend(prof, need)
        check(f"{name}: legal at its own required pitch",
              fab.check(CopperGeometry(pitch_mm=need, track_width_mm=r.track_mm,
                                       clearance_mm=r.clearance_mm,
                                       via_size_mm=r.via_size_mm),
                        prof, via_drill_mm=r.via_drill_mm) == []
              and _pitch_ok(r, need), f"needs {need:.4f} mm")


def test_recommend_explains_an_impossible_pitch():
    print("=== recommend() explains when no geometry fits ===")
    w = fab.load_profile("pcbway-standard")
    try:
        fab.recommend(w, 0.5)
        check("PCBWay standard cannot make 0.5 mm pitch", False)
    except fab.FabPitchError as e:
        check("PCBWay standard cannot make 0.5 mm pitch", True)
        # 0.60/2 + 0.1524/2 + 0.2794 = 0.30 + 0.0762 + 0.2794 = 0.6556
        check("names the pitch that WOULD work",
              abs(e.required_pitch_mm - 0.6556) < 1e-4,
              f"{e.required_pitch_mm:.4f}")
        check("message names the pitch", "0.656" in str(e), str(e))
        check("message names the house", "PCBWay" in str(e))
        check("message offers the extended tier", "extended" in str(e))
        # And it is honest: the recommendation DOES fit at that pitch.
        check("the named pitch actually works",
              _pitch_ok(fab.recommend(w, e.required_pitch_mm),
                        e.required_pitch_mm))

    check("JLC standard DOES make 0.5 mm pitch",
          fab.recommend(fab.load_profile("jlcpcb-standard"), 0.5) is not None)


# ── staleness ───────────────────────────────────────────────────────────
def test_stale_date_warning():
    print("=== a stale profile says so ===")
    p = fab.load_profile("jlcpcb-standard")
    fresh = datetime.date(2026, 7, 20)
    check("fresh profile is not stale", not p.is_stale(today=fresh))
    check("fresh profile has no warning", p.stale_warning(today=fresh) is None)
    check("age is 1 day", p.verified_age_days(today=fresh) == 1)

    old = datetime.date(2027, 7, 20)      # 366 days later
    check("year-old profile IS stale", p.is_stale(today=old))
    warn = p.stale_warning(today=old)
    check("stale warning exists", warn is not None)
    check("stale warning is loud", "STALE FAB PROFILE" in warn, warn)
    check("stale warning names the date", "2026-07-19" in warn)
    check("stale warning names the age", "366 days" in warn, warn)
    check("stale warning points at the source", "https://" in warn)

    # Boundary: exactly at the limit is fine, one day past is not.
    at = datetime.date(2026, 7, 19) + datetime.timedelta(days=fab.STALE_AFTER_DAYS)
    check("exactly at the limit is fresh", not p.is_stale(today=at))
    check("one day past the limit is stale",
          p.is_stale(today=at + datetime.timedelta(days=1)))

    # An unparseable date must be treated as unverified, never as fresh.
    bad = fab.FabProfile(name="broken", house="X", tier="standard",
                         layers="1-2", verified_on="soon", min_track_mm=0.2,
                         sources={"*": "jlc-cap"})
    check("unparseable date is stale", bad.is_stale())
    check("unparseable date warns", "unverified" in (bad.stale_warning() or ""),
          str(bad.stale_warning()))
    check("none profile never warns even if ancient",
          fab.load_profile("none").stale_warning(today=old) is None)


# ── summary line + reconcile ────────────────────────────────────────────
def test_summary_line():
    print("=== the printed contract line ===")
    p = fab.load_profile("jlcpcb-standard")
    geo = CopperGeometry(pitch_mm=0.5, track_width_mm=0.2,
                         clearance_mm=0.15, via_size_mm=0.45)
    line = fab.summary_line(geo, p)
    check("names the profile", line.startswith("jlcpcb-standard | "), line)
    check("track OK with its minimum", "track 0.2 OK (min 0.1)" in line, line)
    check("via OK with its minimum", "via 0.45 OK (min 0.45)" in line, line)
    check("clearance OK with its minimum",
          "clearance 0.15 OK (min 0.1)" in line, line)
    check("prints the verified date", line.endswith("verified 2026-07-19"), line)

    bad = CopperGeometry(pitch_mm=0.5, track_width_mm=0.05,
                         clearance_mm=0.15, via_size_mm=0.45)
    line = fab.summary_line(bad, p)
    check("a violation reads FAIL", "track 0.05 FAIL (min 0.1)" in line, line)


def test_reconcile_warns_but_does_not_change():
    print("=== reconcile: warn by default, snap only on request ===")
    p = fab.load_profile("jlcpcb-standard")
    thin = CopperGeometry(pitch_mm=0.5, track_width_mm=0.05,
                          clearance_mm=0.05, via_size_mm=0.30)

    out = fab.reconcile(thin, p, 0.5)
    check("violations found", len(out.violations) >= 3, str(len(out.violations)))
    check("not ok", not out.ok)
    check("nothing changed", out.changes == [])
    check("no override proposed",
          (out.track_mm, out.clearance_mm, out.via_size_mm) == (None, None, None))

    out = fab.reconcile(thin, p, 0.5, enforce=True)
    check("enforce changed the copper", len(out.changes) == 3, str(out.changes))
    check("track snapped to 0.10", abs(out.track_mm - 0.10) < 1e-12)
    check("clearance snapped to 0.20", abs(out.clearance_mm - 0.20) < 1e-12)
    check("via snapped to 0.45", abs(out.via_size_mm - 0.45) < 1e-12)
    check("each change names old and new",
          all("->" in c for c in out.changes), str(out.changes))
    check("each change names the profile",
          all("jlcpcb-standard" in c for c in out.changes))
    check("enforced result is fab-legal",
          fab.check(CopperGeometry(pitch_mm=0.5, track_width_mm=out.track_mm,
                                   clearance_mm=out.clearance_mm,
                                   via_size_mm=out.via_size_mm), p) == [])
    check("enforced result still fits the pitch", not out.notes, str(out.notes))

    # Enforcing at a pitch the house cannot serve must SAY so, not silently
    # produce copper that no longer fits its grid.
    w = fab.load_profile("pcbway-standard")
    out = fab.reconcile(thin, w, 0.5, enforce=True)
    check("PCBWay at 0.5 refuses to snap", out.changes == [], str(out.changes))
    check("and explains why", any("could not snap" in n for n in out.notes),
          str(out.notes))
    check("violations still stand", len(out.violations) >= 3)

    out = fab.reconcile(thin, w, 0.7, enforce=True)
    check("PCBWay at 0.7 does snap", len(out.changes) == 3, str(out.changes))
    check("and the result fits 0.7", not out.notes, str(out.notes))


def test_enforce_rescues_an_oversize_via():
    print("=== enforce: buildable copper that does not fit its grid ===")
    p = fab.load_profile("jlcpcb-standard")

    # Andrew's real case: KiCad's stock net class. Every number is fab-legal;
    # the via halo (0.30 + 0.10 + 0.20 = 0.60) simply does not fit 0.5 mm.
    stock = CopperGeometry(pitch_mm=0.5, track_width_mm=0.2,
                           clearance_mm=0.2, via_size_mm=0.6)
    check("KiCad stock is fab-legal at JLC standard",
          fab.check(stock, p, via_drill_mm=0.3) == [])
    check("but needs a 0.6 mm pitch",
          abs(fab.required_pitch_mm(0.2, 0.2, 0.6) - 0.6) < 1e-12)

    out = fab.reconcile(stock, p, 0.5, via_drill_mm=0.3, enforce=True)
    check("nothing is snapped (0.45 via would still need 0.525)",
          out.changes == [], str(out.changes))
    note = " ".join(out.notes)
    check("but enforce explains the shortfall",
          "needs a 0.6mm pitch" in note, note)
    check("names the via that would not help", "0.45" in note and "0.525" in note)
    check("names the pitch that works", "route at 0.6mm pitch" in note.lower(),
          note)
    check("refuses to narrow track/clearance on its own",
          "NOT narrowed automatically" in note)

    # A narrower track makes the cheapest legal via a genuine rescue:
    # 0.45/2 + 0.1/2 + 0.2 = 0.475 <= 0.5, while 0.6 needs 0.55.
    fine = CopperGeometry(pitch_mm=0.5, track_width_mm=0.1,
                          clearance_mm=0.2, via_size_mm=0.6)
    check("0.6 via still misses at track 0.1",
          fab.required_pitch_mm(0.1, 0.2, 0.6) > 0.5)
    out = fab.reconcile(fine, p, 0.5, via_drill_mm=0.3, enforce=True)
    check("the via IS shrunk", len(out.changes) >= 1, str(out.changes))
    check("shrunk to the cheapest legal via", abs(out.via_size_mm - 0.45) < 1e-12)
    check("track untouched", out.track_mm is None)
    check("clearance untouched", out.clearance_mm is None)
    check("the change explains itself",
          "buildable but its halo" in out.changes[0], out.changes[0])
    check("result now fits the pitch",
          fab.required_pitch_mm(0.1, 0.2, out.via_size_mm) <= 0.5 + 1e-9)
    check("result is still fab-legal",
          fab.check(CopperGeometry(pitch_mm=0.5, track_width_mm=0.1,
                                   clearance_mm=0.2,
                                   via_size_mm=out.via_size_mm),
                    p, via_drill_mm=out.via_drill_mm or 0.3) == [])
    check("no leftover complaint", not out.notes, str(out.notes))

    # Without enforce, none of this happens.
    out = fab.reconcile(fine, p, 0.5, via_drill_mm=0.3)
    check("no enforce -> no change", out.changes == [] and
          out.via_size_mm is None)


def test_fill_defaults_respects_the_project():
    print("=== fab fills only what the project leaves unsaid ===")
    p = fab.load_profile("jlcpcb-standard")
    with tempfile.TemporaryDirectory() as d:
        pcb = os.path.join(d, "b.kicad_pcb")
        open(pcb, "w").close()

        # No project file: the profile supplies everything.
        c, t, v, notes = fab.fill_defaults(pcb, p, 0.5, None, None, None)
        check("no project -> fab supplies clearance", abs(c - 0.20) < 1e-12)
        check("no project -> fab supplies track", abs(t - 0.10) < 1e-12)
        check("no project -> fab supplies via", abs(v - 0.45) < 1e-12)
        check("and says so", any("fab defaults from" in n for n in notes),
              str(notes))

        # A project that states a track width keeps it.
        with open(os.path.join(d, "b.kicad_pro"), "w", encoding="utf-8") as f:
            json.dump({"net_settings": {"classes": [
                {"name": "Default", "track_width": 0.35}]}}, f)
        c, t, v, notes = fab.fill_defaults(pcb, p, 0.5, None, None, None)
        check("project track wins (stays unset for the resolver)", t is None)
        check("unstated clearance still filled", abs(c - 0.20) < 1e-12)
        check("unstated via still filled", abs(v - 0.45) < 1e-12)

        # An explicit argument beats both.
        c, t, v, _ = fab.fill_defaults(pcb, p, 0.5, 0.11, 0.12, 0.13)
        check("explicit args win", (c, t, v) == (0.11, 0.12, 0.13))

        # A pitch the house cannot serve fills nothing and explains.
        w = fab.load_profile("pcbway-standard")
        c, t, v, notes = fab.fill_defaults(pcb, w, 0.5, None, None, None)
        check("impossible pitch fills nothing", (c, t, v) == (None, None, None))
        check("impossible pitch explains",
              any("not applied" in n for n in notes), str(notes))


def test_compare_table():
    print("=== the two-house comparison ===")
    lines = fab.compare(("jlcpcb-standard", "pcbway-standard"), pitch_mm=0.5)
    text = "\n".join(lines)
    check("both houses appear", "jlcpcb-standard" in text and
          "pcbway-standard" in text)
    check("every limit row appears",
          all(fab._LABELS[f] in text for f in fab._LIMIT_FIELDS))
    check("verified dates appear", "2026-07-19" in text)
    check("JLC fits 0.5 mm", "jlcpcb-standard: track" in text)
    check("PCBWay does not", "pcbway-standard: NO FIT" in text)


def main():
    test_loading()
    test_every_number_is_cited()
    test_user_extension_is_data()
    test_check_catches_each_violation_type()
    test_violation_messages_are_readable()
    test_none_profile_changes_nothing()
    test_recommend_is_fab_legal_and_fits_the_pitch()
    test_recommend_explains_an_impossible_pitch()
    test_stale_date_warning()
    test_summary_line()
    test_reconcile_warns_but_does_not_change()
    test_enforce_rescues_an_oversize_via()
    test_fill_defaults_respects_the_project()
    test_compare_table()
    print(f"\nRESULT: {'PASS' if not FAILED else 'FAIL ' + str(FAILED)}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
