#!/usr/bin/env python3
"""Verifier worker pool for the OAI-math env.

N rollout workers, each owns an episode dir under EP_ROOT/<worker>/.
Fast tier is read-only on .lake -> fully parallel.
Audit tier is flock-serialized (comparator builds into .lake).

Usage:
  python3 pool.py --tasks tasks.jsonl --workers 24 --mode qa
    qa mode: for each task, materialize challenge, run fast tier on the
    *sorried* file (must compile with sorry-warning), then restore the
    reference proof and run audit (must pass) -> ships validated tasks.
  --mode rollout: run a rollout driver (external) per task; pool only
    provides isolated dirs + the lock discipline.
"""
import argparse, json, os, re, subprocess, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from task import TaskSpec
from grade import fast_tier, audit_tier

LEAN_ROOT = Path(os.environ.get("OAI_LEAN_ROOT", Path.home() / "rl/math/lean"))
EP_ROOT = Path(os.environ.get("OAI_EP_ROOT", Path.home() / "rl/episodes"))


def qa_one(rec: dict) -> dict:
    """Validate a single subgoal/challenge task end-to-end."""
    t0 = time.time()
    try:
        if rec["type"] == "challenge":
            task = TaskSpec.from_repo(LEAN_ROOT, rec["name"])
        else:
            task = TaskSpec.subgoal(
                LEAN_ROOT, rec["file"], rec["decl_line"], rec["decl_lines"],
                rec["decl"], rec["task_id"])
    except Exception as e:
        return {"task_id": rec.get("task_id", rec.get("name")), "ok": False,
                "stage": "build_spec", "err": str(e)}
    wd = EP_ROOT / "qa" / task.task_id
    wd.mkdir(parents=True, exist_ok=True)
    sol = wd / "Solution.lean"
    sol.write_text(task.challenge_text)

    # gate 1: sorried challenge must still elaborate (sorry warning ok)
    r = subprocess.run(["lake", "env", "lean", str(sol)], cwd=LEAN_ROOT,
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or "error" in r.stderr.lower():
        return {"task_id": task.task_id, "ok": False, "stage": "elab",
                "err": (r.stderr or r.stdout)[-1500:]}

    # gate 2: reference proof (from the original file) must pass audit
    if rec["type"] == "challenge":
        src_path = LEAN_ROOT / (task.solution_module.replace(".", "/") + ".lean")
    else:
        src_path = LEAN_ROOT / task.metadata["source_file"]
    src = src_path.read_text()
    sol.write_text(src)
    audit = audit_tier(task, LEAN_ROOT, wd, env=dict(os.environ), timeout=3600)
    return {"task_id": task.task_id, "ok": audit.ok, "stage": "audit",
            "audit_s": round(audit.elapsed_s, 1),
            "err": audit.errors[:1],
            "total_s": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--mode", default="qa", choices=["qa"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="pool_results.jsonl")
    a = ap.parse_args()

    recs = [json.loads(l) for l in open(a.tasks)]
    if a.limit:
        recs = recs[: a.limit]
    out = open(a.out, "w")
    n_ok = 0
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(qa_one, r): r for r in recs}
        for k, fut in enumerate(as_completed(futs)):
            res = fut.result()
            n_ok += res["ok"]
            out.write(json.dumps(res) + "\n")
            out.flush()
            if k % 25 == 0:
                print(f"{k}/{len(recs)} done, {n_ok} ok", flush=True)
    print(f"done: {n_ok}/{len(recs)} tasks validated")


if __name__ == "__main__":
    main()
