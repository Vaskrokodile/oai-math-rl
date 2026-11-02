"""Two-tier grading for the OAI-math env.

fast_tier (per check, seconds):
  1. `lake env lean Solution.lean` must elaborate with zero errors
  2. No `sorry`/`admit`/`sorryAx` in solution (textual gate; axiom replay
     is the real check)
  3. `#print axioms <thm>` ⊆ permitted_axioms
  4. Signature identity vs canonical header (cheap proxy for the
     comparator's export-level declaration equality)

audit_tier (per submit, ~1min, flock-serialized):
  Agent file is dropped at ComparatorChallenges/AgentSub/<tid>.lean
  (module ComparatorChallenges.AgentSub.<tid> — covered by the existing
  `ComparatorChallenges.+` glob, no lakefile edit needed), then
  `lake env comparator cfg.json` runs the real check: landrun-sandboxed
  build, env export, statement identity by declaration diff, axiom
  whitelist, kernel replay.
"""
from __future__ import annotations

import fcntl
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from task import TaskSpec

AUDIT_LOCK = Path("/tmp/oai_comparator_audit.lock")
AGENT_SUBDIR = "ComparatorChallenges/AgentSub"


@dataclass
class GradeResult:
    ok: bool
    tier: str
    milestones: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    axioms_used: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0


def _run(cmd, cwd, env=None, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                       text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _norm_sig(s: str) -> str:
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"/-.*?-/", "", s, flags=re.S)
    return re.sub(r"\s+", " ", s).strip()


def _extract_sig(src: str, decl_name: str) -> str | None:
    leaf = decl_name.split(".")[-1]
    m = re.search(
        rf"(?m)(?:theorem|lemma)\s+{re.escape(leaf)}\b(.*?)(?::=\s*by|:=|=\s*by)",
        src, re.S)
    return _norm_sig(m.group(1)) if m else None


def fast_tier(task: TaskSpec, lean_root: Path, workdir: Path,
              timeout: int = 300) -> GradeResult:
    """Compile + axioms + signature check on workdir/Solution.lean.

    Takes a shared lock: audit-tier `lake build` writes .lake and must not
    race concurrent olean reads."""
    t0 = time.time()
    AUDIT_LOCK.touch(exist_ok=True)
    lock = open(AUDIT_LOCK)
    fcntl.flock(lock, fcntl.LOCK_SH)
    try:
        return _fast_tier_inner(task, lean_root, workdir, timeout)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _fast_tier_inner(task, lean_root, workdir, timeout) -> GradeResult:
    t0 = time.time()
    sol = workdir / "Solution.lean"
    res = GradeResult(ok=False, tier="fast")
    if not sol.exists():
        res.errors.append("Solution.lean missing")
        return res
    src = sol.read_text()

    if re.search(r"\bsorry\b|\badmit\b|sorryAx", src):
        res.milestones["no_sorry_text"] = False
        res.errors.append("solution still contains sorry/admit")
        res.elapsed_s = time.time() - t0
        return res
    res.milestones["no_sorry_text"] = True

    # import policy: agent may only import what the challenge file imports
    # (+ declared solution_imports). Anything else can smuggle the answer
    # key (OAI.*) or uncontrolled deps.
    allowed = set(task.metadata.get("challenge_imports", []))
    if task.challenge_text:
        allowed |= set(re.findall(r"(?m)^import\s+(\S+)", task.challenge_text))
    allowed |= set(task.solution_imports)
    got_imports = set(re.findall(r"(?m)^import\s+(\S+)", src))
    extra = got_imports - allowed
    res.milestones["imports_ok"] = not extra
    if extra:
        res.errors.append(f"imports outside allowed set: {sorted(extra)}")
        res.elapsed_s = time.time() - t0
        return res

    rc, out, err = _run(["lake", "env", "lean", str(sol)], cwd=lean_root,
                        timeout=timeout)
    res.milestones["compiles"] = (rc == 0)
    if not res.milestones["compiles"]:
        res.errors.append((err or out)[-4000:])
        res.elapsed_s = time.time() - t0
        return res

    if task.expected_header:
        leaf = task.theorem_names[0].split(".")[-1]
        got = _extract_sig(src, leaf)
        want_body = task.expected_header
        m = re.match(r"\s*(?:theorem|lemma)\s+\S+\s*(.*)", want_body, re.S)
        want = _norm_sig(m.group(1) if m else want_body)
        res.milestones["sig_match"] = (got is not None and got == want)
        if not res.milestones["sig_match"]:
            res.errors.append(f"statement mismatch\nwant: {want}\ngot:  {got}")
    else:
        res.milestones["sig_match"] = True

    # axiom probe: solution source + #print axioms appended (agent file is
    # not a lake module; compiling it standalone resolves its decls locally)
    probe = workdir / "_AxCheck.lean"
    probe.write_text(
        src + "\n"
        + "\n".join(f"#print axioms {t}" for t in task.theorem_names)
        + "\n")
    rc, out, err = _run(
        ["lake", "env", "lean", str(probe)],
        cwd=lean_root, timeout=timeout)
    axioms = set()
    for m in re.finditer(r"depends on axioms:\s*\[([^\]]*)\]", out):
        axioms.update(a.strip() for a in m.group(1).split(",") if a.strip())
    if "sorryAx" in out or "uses 'sorryAx'" in out:
        axioms.add("sorryAx")
    bad = axioms - set(task.permitted_axioms)
    res.axioms_used = sorted(axioms)
    res.milestones["axioms_ok"] = (rc == 0 and not bad)
    if not res.milestones["axioms_ok"]:
        res.errors.append(f"axioms outside permitted set: {sorted(bad)}; raw: {out[-500:]}")

    res.ok = all(res.milestones.values())
    res.elapsed_s = time.time() - t0
    return res


def audit_tier(task: TaskSpec, lean_root: Path, workdir: Path,
               env: dict, timeout: int = 3600) -> GradeResult:
    """Full comparator run, flock-serialized (comparator writes .lake)."""
    t0 = time.time()
    res = GradeResult(ok=False, tier="audit")
    sol = workdir / "Solution.lean"
    src = sol.read_text()

    tid = re.sub(r"[^A-Za-z0-9_]", "_", task.task_id)
    subdir = lean_root / AGENT_SUBDIR

    AUDIT_LOCK.touch(exist_ok=True)
    with open(AUDIT_LOCK) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            subdir.mkdir(exist_ok=True)
            staged = []
            # challenge module: repo challenges exist already; subgoal tasks
            # must stage the excised file as a module too
            if task.challenge_module == "Solution" or task.kind == "subgoal":
                ch_name = f"ComparatorChallenges.AgentSub.{tid}_Ch"
                p = subdir / f"{tid}_Ch.lean"
                p.write_text(task.challenge_text)
                staged.append(p)
                cfg = task.comparator_config(
                    solution_module=f"ComparatorChallenges.AgentSub.{tid}_Sol")
                cfg["challenge_module"] = ch_name
            else:
                cfg = task.comparator_config(
                    solution_module=f"ComparatorChallenges.AgentSub.{tid}")
            sol_mod = cfg["solution_module"]
            target = subdir / f"{sol_mod.rsplit('.', 1)[-1]}.lean"
            target.write_text(src)
            staged.append(target)
            cfgpath = subdir / f"{tid}.json"
            cfgpath.write_text(json.dumps(cfg, indent=2))
            try:
                rc, out, err = _run(["lake", "env", "comparator", str(cfgpath)],
                                    cwd=lean_root, env=env, timeout=timeout)
            finally:
                for p in staged:
                    p.unlink(missing_ok=True)
                cfgpath.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

    res.milestones["comparator_ok"] = ("Your solution is okay" in out)
    if not res.milestones["comparator_ok"]:
        res.errors.append((out + err)[-4000:])
    res.ok = res.milestones["comparator_ok"]
    res.elapsed_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Reward shaping
#
# Credits are monotone in the milestone ladder and every milestone is a strict
# prerequisite of the audit — you cannot accumulate reward by faking early
# stages, because the final credit requires the kernel-verified comparator
# pass, which implies all of them anyway. Import legality is a gate, not a
# credit (no reward for merely not cheating). Costs are real-resource
# penalties: audits are serialized kernel replays (~minutes), fast checks are
# parallel elaborations (~seconds).
# ---------------------------------------------------------------------------

W_NO_SORRY = 0.05       # text gate only — the axiom check is what matters
W_COMPILE = 0.20        # file elaborates cleanly
W_SIG = 0.10            # statement preserved (proxy; audit is decisive)
W_AXIOMS = 0.25         # axiom whitelist over the agent's own env
W_AUDIT = 0.40          # comparator kernel replay — the only real credit
C_CHECK = 0.002         # per fast check
C_AUDIT = 0.02          # per audit consumed


def shaped_reward(fast: GradeResult | None, audit: GradeResult | None,
                  n_checks: int = 0, n_audits: int = 0) -> float:
    r = 0.0
    if fast is not None:
        ms = fast.milestones
        r += W_NO_SORRY * bool(ms.get("no_sorry_text"))
        r += W_COMPILE * bool(ms.get("compiles"))
        r += W_SIG * bool(ms.get("sig_match"))
        r += W_AXIOMS * bool(ms.get("axioms_ok"))
    if audit is not None and audit.ok:
        r = max(r, W_NO_SORRY + W_COMPILE + W_SIG + W_AXIOMS) + W_AUDIT
    r -= C_CHECK * n_checks + C_AUDIT * n_audits
    return round(r, 4)
