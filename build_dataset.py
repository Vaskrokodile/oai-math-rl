#!/usr/bin/env python3
"""Emit the final dataset artifacts from mined corpora.

Produces in out/dataset/:
  sft_formal.jsonl      — Lean (prompt=header, completion=body) pairs, dedup'd
  sft_informal.jsonl    — LaTeX (theorem text, proof) pairs
  tasks_subgoals.jsonl  — graded subgoal task records w/ difficulty+signal features
  dep_graph.json        — module -> imports graph (OAI only)

Difficulty/signal features per subgoal:
  cone_oai_files   — # OAI files in the file's transitive OAI-import cone
  cone_ext         — external (non-OAI, non-Mathlib) deps in cone
  proof_bytes      — reference proof body size (difficulty proxy)
  depth_in_file    — decl position index within file
  n_ctx            — decls before target in file (usable lemma shelf)
"""
import json, re, sys, hashlib
from pathlib import Path
from functools import lru_cache

OUT = Path(__file__).parent / "out"
DS = OUT / "dataset"
DS.mkdir(exist_ok=True)

# ---------- dep graph ----------
files = {}
for l in open(OUT / "oai_files.jsonl"):
    r = json.loads(l)
    files[r["module"]] = {"file": r["file"], "imports": r["imports"]}

with open(DS / "dep_graph.json", "w") as fh:
    json.dump({m: v["imports"] for m, v in files.items()}, fh)

@lru_cache(maxsize=None)
def cone(mod):
    if mod not in files:
        return frozenset()
    s = {mod}
    for i in files[mod]["imports"]:
        if i.startswith("OAI."):
            s |= cone(i)
    return frozenset(s)

# ---------- subgoal tasks w/ features ----------
by_file_decls = {}
for l in open(OUT / "oai_decls.jsonl"):
    d = json.loads(l)
    by_file_decls.setdefault(d["file"], []).append(d)

task_out = open(DS / "tasks_subgoals.jsonl", "w")
n = 0
for fpath, decls in by_file_decls.items():
    mod = fpath[:-5].replace("/", ".")
    decls.sort(key=lambda d: d["line"])
    c = cone(mod)
    ext = sorted({
        i.split(".")[0]
        for m in c
        for i in files[m]["imports"]
        if not i.startswith(("OAI.", "Init", "Lean", "Std"))
        and not i.startswith("Mathlib")
    })
    pos = 0
    for d in decls:
        if d["kind"] in ("theorem", "lemma") and d["name"] and d["body"]:
            pos += 1
            nsq = f'{d["namespace"]}.{d["name"]}' if d["namespace"] else d["name"]
            task_out.write(json.dumps({
                "task_id": re.sub(r"[^A-Za-z0-9]+", "_", f"{mod}__{d['name']}")[:180],
                "type": "subgoal",
                "file": fpath,
                "module": mod,
                "decl": nsq if nsq.startswith("OAI") else f"OAI.{nsq}",
                "decl_line": d["line"],
                "decl_lines": d["n_lines"],
                "proof_bytes": len(d["body"]),
                "depth_in_file": pos,
                "n_ctx": sum(1 for x in decls if x["line"] < d["line"]),
                "cone_oai_files": len(c),
                "cone_ext": ext,
                "mathlib_only": len(c) == 1 and not ext,
            }) + "\n")
            n += 1
task_out.close()
print(f"tasks_subgoals: {n}")

# ---------- formal SFT pairs ----------
seen = set()
sf = open(DS / "sft_formal.jsonl", "w")
ns_ = 0
for fpath, decls in by_file_decls.items():
    for d in decls:
        if d["kind"] not in ("theorem", "lemma") or not d["body"]:
            continue
        h = hashlib.sha256((d["header"] + d["body"]).encode()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        sf.write(json.dumps({
            "module": d["module"], "name": d["name"], "file": d["file"],
            "kind": d["kind"],
            "prompt": (d["doc"] + "\n" if d["doc"] else "") + d["header"] + " := by",
            "completion": d["body"][2:].strip() if d["body"].startswith(":=") else d["body"],
        }) + "\n")
        ns_ += 1
sf.close()
print(f"sft_formal (dedup'd): {ns_}")

# ---------- informal SFT pairs ----------
inf = open(DS / "sft_informal.jsonl", "w")
ni = 0
for l in open(OUT / "preprint_theorems.jsonl"):
    r = json.loads(l)
    if "proof" not in r or len(r["text"]) < 30 or len(r["proof"]) < 30:
        continue
    inf.write(json.dumps({
        "slug": r["slug"], "env": r["env"], "label": r["label"],
        "prompt": f"Prove the following {r['env']}.\n\n{r['text']}",
        "completion": r["proof"],
    }) + "\n")
    ni += 1
inf.close()
print(f"sft_informal: {ni}")
