#!/usr/bin/env python3
"""Task-signal scorer v0 — interpretable features for expected RL contribution.

Rationale (per research spec): a task carries dense signal when success
requires a real approach rather than a lucky guess. Proxies we can compute
*before* spending a rollout:

  proof_bytes     — size of reference proof: longer => more structure to learn
  cone_oai_files  — dependency depth: deeper => more compositional planning
  n_ctx           — lemma shelf size: context the policy must learn to use
  mathlib_only    — verifiability cost: cheap-to-grade tasks are more usable
  decl_lines      — total decl size (statement complexity)

score = geomean of normalized components; 'effort' and 'cost' reported
separately so the scheduler can trade signal density vs verify cost.

Outputs tasks_scored.jsonl sorted by score.
"""
import json, math, sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / "rl/out")
IN = OUT / "subgoal_index.jsonl"
FILES = OUT / "oai_files.jsonl"


def main():
    # module -> imports for cone computation
    mods = {}
    for l in open(FILES):
        r = json.loads(l)
        mods[r["module"]] = r["imports"]

    import functools
    @functools.lru_cache(maxsize=None)
    def cone(m):
        if m not in mods:
            return frozenset()
        s = {m}
        for i in mods[m]:
            if i.startswith("OAI."):
                s |= cone(i)
        return frozenset(s)

    rows = []
    for l in open(IN):
        r = json.loads(l)
        mod = r["file"][:-5].replace("/", ".")
        c = cone(mod)
        ext = {
            i.split(".")[0]
            for m in c
            for i in mods[m]
            if not i.startswith(("OAI.", "Mathlib", "Init", "Lean", "Std"))
        }
        effort = (
            0.45 * math.log1p(r["proof_body_bytes"])
            + 0.25 * math.log1p(len(c))
            + 0.15 * math.log1p(r["decl_line"])
            + 0.15 * math.log1p(r["decl_lines"])
        )
        cost = math.log1p(len(c)) + (0 if not ext else 2)
        rows.append({
            **r,
            "cone_files": len(c),
            "cone_ext": sorted(ext),
            "mathlib_only": len(c) == 1 and not ext,
            "effort": round(effort, 3),
            "cost": round(cost, 3),
            "score": round(effort / (1 + cost), 4),
        })
    rows.sort(key=lambda r: -r["score"])
    out = OUT / "tasks_scored.jsonl"
    with open(out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"{len(rows)} scored -> {out}")
    # distribution summary
    import statistics as st
    sc = [r["score"] for r in rows]
    print("score: median", round(st.median(sc), 3),
          "p90", sorted(sc)[int(len(sc) * .9)],
          "p99", sorted(sc)[int(len(sc) * .99)])
    mo = sum(1 for r in rows if r["mathlib_only"])
    print("mathlib_only tasks:", mo)


if __name__ == "__main__":
    main()
