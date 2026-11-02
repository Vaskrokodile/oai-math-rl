#!/usr/bin/env python3
"""Enrich out/index.jsonl with family + paper linkage via lean/docs/*.md.

Each docs/NNN.md corresponds to result family NNN (same numbering as
CONTENTS.md) and links to ComparatorChallenges/<name>.lean and
preprints/<slug>/... paths. Produces out/tasks.jsonl with fields:
  family, doc_file, papers[], challenge, theorem_names, challenge_text,
  imports, permitted_axioms, solution_module, solution_decls
"""
import json, re
from pathlib import Path

LEAN = Path("../math/lean")
OUT = Path(__file__).parent / "out"

# doc -> challenges + papers
challenge2fam = {}
fam_papers = {}
fam_doc = {}
for dpath in sorted(LEAN.glob("docs/*.md")):
    fam = int(dpath.stem)
    txt = dpath.read_text(encoding="utf-8", errors="replace")
    fam_doc[fam] = txt
    for m in re.finditer(r"ComparatorChallenges/([A-Za-z0-9_]+)\.lean", txt):
        challenge2fam.setdefault(m.group(1), set()).add(fam)
    for m in re.finditer(r"preprints/([A-Za-z0-9_\-]+)/", txt):
        fam_papers.setdefault(fam, set()).add(m.group(1))

rows = []
for line in open(OUT / "index.jsonl"):
    r = json.loads(line)
    fams = sorted(challenge2fam.get(r["challenge"], set()))
    papers = sorted({p for f in fams for p in fam_papers.get(f, set())})
    ctext = (LEAN / r["challenge_file"]).read_text(encoding="utf-8", errors="replace")
    rows.append({
        "task_id": r["challenge"],
        "families": fams,
        "papers": papers,
        "challenge_module": r["challenge_module"],
        "solution_module": r["solution_module"],
        "theorem_names": r["theorem_names"],
        "permitted_axioms": r["permitted_axioms"],
        "challenge_imports": r["challenge_imports"],
        "challenge_text": ctext,
        "n_sorried_decls": len(r["challenge_decls"]),
        "solution_decls": r["solution_decls"],
    })

with open(OUT / "tasks.jsonl", "w") as fh:
    for r in rows:
        fh.write(json.dumps(r) + "\n")
nofam = [r["task_id"] for r in rows if not r["families"]]
print(f"{len(rows)} tasks, {len(nofam)} without family link: {nofam[:10]}")
