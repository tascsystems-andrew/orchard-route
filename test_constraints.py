"""L3 validation: the closed constraint vocabulary, parse + check.

Every one of the six forms is exercised from both surfaces (CLI string,
structured dict), every documented error path is provoked (unknown name,
malformed call, wrong arity, non-numeric args, self-distance, bad side, bad
angle, unknown ref, extra/missing dict keys), and every checker is judged
against hand-placed rectangles where ok/violation and the penalty magnitude
are computable by eye. No board file, no GPU — pure CPU, stdlib only.

Run: .venv/bin/python test_constraints.py
"""
from constraints import (Constraint, parse_constraint, parse_constraints,
                         evaluate_constraints, VALID_SET_MSG, SIGNATURES)

failures = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
    if not cond:
        failures.append(msg)


def raises(spec, *needles, known_refs=None):
    """Parse must fail with ValueError mentioning every needle."""
    try:
        parse_constraint(spec, known_refs=known_refs)
    except ValueError as e:
        missing = [n for n in needles if n not in str(e)]
        check(not missing,
              f"{spec!r} rejected; message has {needles}"
              + (f" (MISSING {missing} in {e})" if missing else ""))
        return
    check(False, f"{spec!r} rejected (no error raised)")


def eval_one(c, placements, courtyards, **kw):
    return evaluate_constraints([c], placements, courtyards, **kw)[0]


if __name__ == "__main__":
    print("=== parse: every form, string and dict, round-trip ===")
    pairs = [
        ("fixed(V1)", {"type": "fixed", "ref": "V1"}),
        ("keepout(2,3,10,5)",
         {"type": "keepout", "x": 2, "y": 3, "w": 10, "h": 5}),
        ("adjacency_max_distance(R4,V1,3)",
         {"type": "adjacency_max_distance", "ref_a": "R4", "ref_b": "V1",
          "mm": 3}),
        ("min_distance(R4,C8,5)",
         {"type": "min_distance", "ref_a": "R4", "ref_b": "C8", "mm": 5}),
        ("orientation_set(V1,[0,90])",
         {"type": "orientation_set", "ref": "V1", "angles": [0, 90]}),
        ("edge(J1,left)", {"type": "edge", "ref": "J1", "side": "left"}),
    ]
    for text, d in pairs:
        cs = parse_constraint(text)
        cd = parse_constraint(d)
        check(cs == cd, f"string and dict forms agree for {text}")
        check(parse_constraint(str(cs)) == cs, f"str() round-trips: {cs}")
    check(parse_constraint("  min_distance( R4 , C8 , 5.5 ) ").mm == 5.5,
          "whitespace tolerated, decimal mm kept")
    check(parse_constraint("orientation_set(V1,270,90,90)").angles
          == (90.0, 270.0),
          "unbracketed angle list accepted, deduped, sorted")
    check(parse_constraint({"type": "orientation_set", "ref": "V1",
                            "angles": 180}).angles == (180.0,),
          "dict form accepts a single bare angle")
    both = parse_constraints(["fixed(V1)",
                              {"type": "edge", "ref": "J1", "side": "top"}])
    check([c.kind for c in both] == ["fixed", "edge"],
          "parse_constraints handles a mixed string/dict list")
    c0 = parse_constraint("fixed(V1)")
    check(parse_constraint(c0) is c0, "an already-parsed Constraint passes through")

    print("=== parse errors: hard, and naming the valid set ===")
    for bad in ("min_dist(R4,C8,5)", {"type": "min_dist", "ref_a": "a",
                                      "ref_b": "b", "mm": 1}):
        raises(bad, "unknown constraint", VALID_SET_MSG)
    for sig in SIGNATURES:
        raises("nope(1)", sig)  # every signature is spelled out in the message
    raises("keep two apart", "malformed constraint", VALID_SET_MSG)
    raises("min_distance(R4,C8)", "expects 3 arguments", "got 2")
    raises("fixed()", "expects 1 argument", "got 0")
    raises("min_distance(R4,C8,x)", "'x' is not a number", "mm")
    raises("keepout(0,0,ten,5)", "'ten' is not a number")
    raises("keepout(0,0,-3,5)", "w and h must be > 0")
    raises("min_distance(R4,R4,5)", "must be different refs", "'R4' twice")
    raises("adjacency_max_distance(C8,C8,2)", "must be different refs")
    raises("min_distance(R4,C8,0)", "mm must be > 0")
    raises("orientation_set(V1,[0,45])", "angles must be from 0, 90, 180, 270")
    raises("orientation_set(V1,[])", "at least one angle")
    raises("orientation_set(V1,[0,90)", "unbalanced brackets")
    raises("edge(J1,norht)", "side must be one of left, right, top, bottom")
    raises({"type": "edge", "ref": "J1"}, "missing key(s) 'side'")
    raises({"type": "fixed", "ref": "V1", "why": "socket"},
           "unexpected key(s) 'why'")
    raises({"ref": "V1"}, "missing 'type'", VALID_SET_MSG)
    raises(["fixed", "V1"], "must be a string or dict", "list")
    raises("min_distance(R4,C8,5)", "unknown ref 'C8'", "known refs: R4, V1",
           known_refs={"R4", "V1"})
    raises("fixed(Z9)", "unknown ref 'Z9'", known_refs={"R4"})
    check(parse_constraint("min_distance(R4,C8,5)",
                           known_refs={"R4", "C8"}).kind == "min_distance",
          "known_refs accepts constraints naming only known refs")

    print("=== checkers: hand-placed rectangles ===")
    # A at (0,0), B at (6,8) -> center distance exactly 10; unit courtyards
    P = {"A": (0.0, 0.0, 0.0), "B": (6.0, 8.0, 90.0)}
    C = {"A": (-1.0, -1.0, 1.0, 1.0), "B": (5.0, 7.0, 7.0, 9.0)}
    HOME = dict(P)

    r = eval_one(parse_constraint("min_distance(A,B,10)"), P, C)
    check(r.ok and r.penalty == 0.0, f"min_distance 10 at d=10 ok ({r.reason})")
    r = eval_one(parse_constraint("min_distance(A,B,12)"), P, C)
    check(not r.ok and abs(r.penalty - 2.0) < 1e-9,
          f"min_distance 12 at d=10 violated, penalty 2.0 ({r.reason})")
    r = eval_one(parse_constraint("adjacency_max_distance(A,B,10)"), P, C)
    check(r.ok and r.penalty == 0.0, f"adjacency 10 at d=10 ok ({r.reason})")
    r = eval_one(parse_constraint("adjacency_max_distance(A,B,7.5)"), P, C)
    check(not r.ok and abs(r.penalty - 2.5) < 1e-9,
          f"adjacency 7.5 at d=10 violated, penalty 2.5 ({r.reason})")

    r = eval_one(parse_constraint("fixed(A)"), P, C, home=HOME)
    check(r.ok, f"fixed(A) unmoved ok ({r.reason})")
    moved = dict(P, A=(3.0, 4.0, 90.0))
    r = eval_one(parse_constraint("fixed(A)"), moved, C, home=HOME)
    check(not r.ok and abs(r.penalty - 6.0) < 1e-9,
          f"fixed(A) moved 5mm+90deg violated, penalty 5+1 ({r.reason})")
    try:
        eval_one(parse_constraint("fixed(A)"), P, C)
        check(False, "fixed without home raises")
    except ValueError as e:
        check("home placements are required" in str(e),
              f"fixed without home raises ({e})")

    ko = parse_constraint("keepout(10,10,4,4)")
    r = eval_one(ko, P, C)
    check(r.ok and r.penalty == 0.0, f"keepout clear of both courtyards ({r.reason})")
    C2 = dict(C, B=(9.0, 9.0, 12.0, 11.0))  # 2 wide x 1 deep into the keepout
    r = eval_one(ko, P, C2)
    check(not r.ok and abs(r.penalty - 1.0) < 1e-9 and "B" in r.reason,
          f"keepout penetration names B, penalty = depth 1.0 ({r.reason})")
    C3 = dict(C2, A=(9.5, 10.0, 11.0, 12.0))
    r = eval_one(ko, P, C3)
    check(not r.ok and "A, B" in r.reason,
          f"keepout reason lists every offender sorted ({r.reason})")
    r = eval_one(ko, P, dict(C, B=(6.0, 6.0, 10.0, 10.0)))
    check(r.ok, "courtyard exactly touching the keepout boundary is ok")

    r = eval_one(parse_constraint("orientation_set(B,[90,270])"), P, C)
    check(r.ok, f"orientation_set: 90 allowed ({r.reason})")
    r = eval_one(parse_constraint("orientation_set(B,[0,180])"), P, C)
    check(not r.ok and abs(r.penalty - 1.0) < 1e-9,
          f"orientation_set: 90 vs [0,180] off by 90 -> penalty 1.0 ({r.reason})")
    r = eval_one(parse_constraint("orientation_set(A,[0])"),
                 dict(P, A=(0.0, 0.0, 360.0)), C)
    check(r.ok, "rotation compared modulo 360 (360 == 0)")
    r = eval_one(parse_constraint("orientation_set(A,[270])"),
                 dict(P, A=(0.0, 0.0, -90.0)), C)
    check(r.ok, "negative rotation compared modulo 360 (-90 == 270)")

    RECT = (-2.0, -2.0, 20.0, 20.0)
    r = eval_one(parse_constraint("edge(A,left)"), P, C, rect=RECT)
    check(r.ok and r.penalty == 0.0,
          f"edge left: courtyard 1mm off the fence, within tol 1.0 ({r.reason})")
    r = eval_one(parse_constraint("edge(B,left)"), P, C, rect=RECT)
    check(not r.ok and abs(r.penalty - 6.0) < 1e-9,
          f"edge left: B is 7mm off, penalty 7-1 ({r.reason})")
    r = eval_one(parse_constraint("edge(B,right)"), P, C, rect=RECT,
                 edge_tol_mm=11.0)
    check(r.ok, "edge right with wide tolerance ok")
    r = eval_one(parse_constraint("edge(A,bottom)"), P, C, rect=RECT)
    check(not r.ok and abs(r.penalty - 16.0) < 1e-9,
          f"edge bottom: A is 17mm off, penalty 17-1 ({r.reason})")
    try:
        eval_one(parse_constraint("edge(A,top)"), P, C)
        check(False, "edge without rect raises")
    except ValueError as e:
        check("fence rect is required" in str(e), f"edge without rect raises ({e})")

    print("=== checker errors: missing refs are caller bugs ===")
    for c, kw in ((parse_constraint("min_distance(A,Z,5)"), {}),
                  (parse_constraint("fixed(Z)"), {"home": HOME}),
                  (parse_constraint("edge(Z,top)"), {"rect": RECT})):
        try:
            eval_one(c, P, C, **kw)
            check(False, f"{c}: missing ref Z raises")
        except ValueError as e:
            check("'Z' not in" in str(e), f"{c}: missing ref Z raises ({e})")

    print("=== ok verdicts still carry reasons, penalties sum cleanly ===")
    cs = parse_constraints(["min_distance(A,B,12)",
                            "adjacency_max_distance(A,B,7.5)"])
    rs = evaluate_constraints(cs, P, C)
    check(all(r.reason for r in rs), "every Check has a reason string")
    check(abs(sum(r.penalty for r in rs) - 4.5) < 1e-9,
          "penalties are magnitudes an annealer can sum (2.0 + 2.5)")

    print(f"\nRESULT: {'PASS' if not failures else 'FAIL'} "
          f"({len(failures)} failed check{'s' if len(failures) != 1 else ''})")
    raise SystemExit(1 if failures else 0)
