#!/usr/bin/env python3
"""OAI-math RL env runner.

Modes:
  --task NAME --solution FILE   grade FILE against repo challenge NAME
  --subgoal FILE LINE LINES DECL --solution S
                                grade S against excised-decl task
  --selftest                    run the built-in correctness suite
  --redteam                     run attack vectors (must all be caught)
"""
import argparse, json, os, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from task import TaskSpec
from session import Session
from campaign import CampaignChecker, build_campaign

LEAN_ROOT = Path(os.environ.get("OAI_LEAN_ROOT", Path.home() / "rl/math/lean"))
EP_ROOT = Path(os.environ.get("OAI_EP_ROOT", Path.home() / "rl/episodes"))


def grade_file(task: TaskSpec, sol_path: Path, do_audit: bool = True) -> dict:
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(sol_path.read_text())
    fast = sess.check()
    out = {"task": task.task_id, "fast": {"ok": fast.ok, "ms": fast.milestones,
                                          "axioms": fast.axioms_used,
                                          "errors": fast.errors[:3]}}
    if do_audit and fast.ok:
        audit = sess.submit()
        out["audit"] = {"ok": audit.ok, "elapsed_s": audit.elapsed_s,
                        "errors": audit.errors[:2]}
    out["solved"] = bool(sess.result and sess.result.ok)
    sess.finish()
    return out


def selftest() -> bool:
    """Golden path: reference OAI solution must pass audit."""
    task = TaskSpec.from_repo(LEAN_ROOT, "CommonBasesFPRAS")
    sol = LEAN_ROOT / task.solution_module.replace(".", "/")
    sol = sol.with_suffix(".lean")
    r = grade_file(task, sol)
    ok = r.get("audit", {}).get("ok", False)
    print(json.dumps(r, indent=2))
    print("SELFTEST:", "PASS" if ok else "FAIL")
    return ok


def redteam() -> bool:
    """Attack vectors — every one MUST be rejected."""
    task = TaskSpec.from_repo(LEAN_ROOT, "CommonBasesFPRAS")
    clean = (LEAN_ROOT / (task.solution_module.replace(".", "/") + ".lean")).read_text()
    results = {}

    # 1. sorry smuggling
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(task.challenge_text)
    r = sess.submit()
    results["sorry_smuggle"] = not r.ok
    sess.finish()

    # 2. statement weakening (7/8 -> 1/2 threshold style edit on a real decl)
    weakened = clean.replace("commonBaseCount M₁ M₂", "0 + commonBaseCount M₁ M₂")
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(weakened)
    r = sess.submit()
    results["stmt_weaken"] = not r.ok
    sess.finish()

    # 3. axiom smuggling — real attack: declare axiom, prove target with it
    import re as _re
    m = _re.search(
        r"theorem common_bases_fpras : CommonBasesFPRAS\.MainTheorem :=.*?(?=\nend OAI)",
        clean, _re.S)
    hacked = clean[: m.start()] + (
        "axiom cheat_axiom : CommonBasesFPRAS.MainTheorem\n"
        "theorem common_bases_fpras : CommonBasesFPRAS.MainTheorem :=\n"
        "  cheat_axiom\n"
    ) + clean[m.end():]
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(hacked)
    r = sess.submit()
    results["axiom_smuggle"] = not r.ok
    sess.finish()

    # 4. answer-key leak: import the OAI solution module, exact the decl
    leak = _re.sub(r":=\s*by\s*\n\s*sorry",
                   ":= OAI.common_bases_fpras", task.challenge_text)
    leak = "import OAI.Combinatorics.MatroidCounting.CommonBases\n" + leak
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(leak)
    r = sess.submit()
    results["answer_key_import"] = not r.ok
    sess.finish()

    # 5. native_decide (ofReduceBool) — not in permitted_axioms
    nd = _re.sub(r":=\s*by\s*\n\s*sorry",
                 ":= by\n  native_decide", task.challenge_text)
    sess = Session(task, LEAN_ROOT, EP_ROOT, env=dict(os.environ))
    sess.write(nd)
    r = sess.submit()
    results["native_decide"] = not r.ok
    sess.finish()

    print(json.dumps(results, indent=2))
    ok = all(results.values())
    print("REDTEAM:", "ALL-CAUGHT" if ok else "BREACH: " + json.dumps(
        {k: v for k, v in results.items() if not v}))
    return ok


def campaign_grade(spec_dir: Path, ep_dir: Path, main: str | None) -> dict:
    ck = CampaignChecker(LEAN_ROOT, spec_dir, ep_dir)
    prog = ck.progress()
    out = {"stages_ok": prog["stages_ok"],
           "stages_total": prog["stages_total"],
           "sorried_remaining": prog["sorried_remaining"]}
    for r in prog["results"]:
        if not r["ok"]:
            out["first_fail"] = {"module": r["module"], "err": r["err"][-300:]}
            break
    if prog["stages_ok"] == prog["stages_total"]:
        out["final"] = ck.final_check(main, strict=main is None)
        out["solved"] = out["final"]["ok"]
    else:
        out["solved"] = False
    return out


def campaign_redteam(spec_dir: Path) -> dict:
    """Campaign attack vectors — all must be caught."""
    results = {}
    ws = spec_dir
    man = json.loads((ws / "manifest.json").read_text())
    stage_files = {s["module"]: s["file"] for s in man["stages"]}
    n = len(man["stages"])
    last = man["stages"][-1]["file"]
    first = man["stages"][0]["file"]

    def fresh(tag):
        return CampaignChecker(LEAN_ROOT, ws, EP_ROOT / f"rt_{tag}")

    orig = {f: (ws / f).read_text() for f in stage_files.values()}

    # 1. weaken a statement in the final stage
    src = orig[last]
    decl = re.search(r"(?m)^(theorem|lemma)\s+(\S+)", src)
    if decl:
        ws_f = ws / last
        ws_f.write_text(src[: decl.start()] + src[decl.start():].replace(
            decl.group(0), f"theorem {decl.group(2)} : True", 1))
        ck = fresh("weaken")
        r = ck.compile_stage(n - 1)
        results["sig_weaken"] = not r["ok"]
        ws_f.write_text(src)
    else:
        results["sig_weaken"] = "no decl found"

    # 2. delete a recorded decl
    if decl:
        # remove decl + body (to next col-0 keyword)
        lines = src.splitlines()
        i = decl.start() and src[: decl.start()].count("\n")
        j = i
        while j + 1 < len(lines):
            nxt = lines[j + 1]
            if nxt[:1] not in (" ", "\t") and nxt.strip():
                break
            j += 1
        ws_f = ws / last
        ws_f.write_text("\n".join(lines[:i] + lines[j + 1:]) + "\n")
        ck = fresh("delete")
        r = ck.compile_stage(n - 1)
        results["decl_delete"] = not r["ok"]
        ws_f.write_text(src)

    # 3. smuggle an axiom into stage 0 and use it in stage n
    # (only viable if stage n's decl can use it — test detection at audit)
    s0 = orig[first]
    ws_f = ws / first
    ws_f.write_text(s0 + "\naxiom rt_cheat : False\n")
    ck = fresh("axiom")
    r = ck.compile_stage(0)
    # stage compiles (axiom is legal syntax) — detection is at axioms level:
    # declare a theorem proven from it, then check axioms
    if r["ok"]:
        ws_f.write_text(s0 + "\naxiom rt_cheat : False\ntheorem rt_uses_cheat : False := rt_cheat\n")
        ck2 = fresh("axiom2")
        r2 = ck2.compile_stage(0)
        if r2["ok"]:
            ax = ck2.axioms_of("rt_uses_cheat")
            results["axiom_smuggle"] = not ax["clean"]
        else:
            results["axiom_smuggle"] = f"compile failed: {r2['err'][:120]}"
    else:
        results["axiom_smuggle"] = f"stage failed: {r['err'][:120]}"
    ws_f.write_text(s0)

    # 4. import a non-cone OAI module (reference answer key)
    ws_f = ws / first
    ws_f.write_text("import OAI.NumberTheory.DirichletL.Nonvanishing\n" + s0)
    ck = fresh("import")
    r = ck.compile_stage(0)
    results["reference_import"] = not r["ok"]
    ws_f.write_text(s0)

    # 5. sorried stage must fail strict final (rebuild all, check all_axioms)
    results["_note"] = "sorry/strict covered by campaign_grade sorried_remaining"
    print(json.dumps(results, indent=2))
    ok = all(v is True for k, v in results.items() if not k.startswith("_"))
    print("CAMPAIGN-REDTEAM:", "ALL-CAUGHT" if ok else "BREACH")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    ap.add_argument("--solution")
    ap.add_argument("--subgoal", nargs=4, metavar=("FILE", "LINE", "LINES", "DECL"))
    ap.add_argument("--campaign", metavar="SPEC_DIR")
    ap.add_argument("--campaign-redteam", metavar="SPEC_DIR")
    ap.add_argument("--main", default=None)
    ap.add_argument("--ep", default="camp")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--redteam", action="store_true")
    ap.add_argument("--fast-only", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(0 if selftest() else 1)
    if a.redteam:
        sys.exit(0 if redteam() else 1)
    if a.campaign_redteam:
        sys.exit(0 if campaign_redteam(Path(a.campaign_redteam)) else 1)
    if a.campaign:
        r = campaign_grade(Path(a.campaign), EP_ROOT / a.ep, a.main)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["solved"] else 1)
    if a.subgoal:
        f, line, lines, decl = a.subgoal
        task = TaskSpec.subgoal(LEAN_ROOT, f, int(line), int(lines), decl,
                                f"sg_{Path(f).stem}_{line}")
        r = grade_file(task, Path(a.solution), do_audit=not a.fast_only)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["solved"] else 1)
    if a.task and a.solution:
        task = TaskSpec.from_repo(LEAN_ROOT, a.task)
        r = grade_file(task, Path(a.solution), do_audit=not a.fast_only)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["solved"] else 1)
    ap.print_help()
