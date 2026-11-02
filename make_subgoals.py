#!/usr/bin/env python3
"""Generate Comparator-format subgoal challenges from the OAI proof corpus.

For each mined theorem/lemma decl D in file F (oai_decls.jsonl), a subgoal
challenge is: F verbatim with ONLY D's proof body replaced by `:= by sorry`.
This preserves imports, namespaces, opens, variables, earlier and later decls,
so the sorried statement elaborates identically to the original.

The paired .json follows the Comparator spec (solution_module = F's module,
theorem_names = [qualified name], classical-3 permitted axioms).

Difficulty proxies: position in file, n_ctx_decls (earlier decls in same file),
proof_lines, file size. Certificate-heavy files can be capped.

Usage:
  python3 make_subgoals.py [--index-only] [--max-per-file N] [--materialize N]
"""
import json, re, sys, random, os
from pathlib import Path

LEAN = Path(os.environ.get("OAI_LEAN_ROOT",
                           Path(__file__).parent / "../math/lean")).resolve()
OUT = Path(__file__).parent / "out"
SG = OUT / "subgoals"
SG.mkdir(exist_ok=True)

AXIOMS = ["propext", "Quot.sound", "Classical.choice"]


def decl_span(header: str, body: str):
    """We stored header = text before ':=' and body = ':=' + rest.
    Challenge body replacement: '{header} := by\\n  sorry'."""
    return header


def main():
    index_only = "--index-only" in sys.argv
    mpf = 8
    materialize = 2000
    for i, a in enumerate(sys.argv):
        if a == "--max-per-file":
            mpf = int(sys.argv[i + 1])
        if a == "--materialize" or a == "--n":
            materialize = int(sys.argv[i + 1])

    by_file = {}
    with open(OUT / "oai_decls.jsonl") as fh:
        for line in fh:
            d = json.loads(line)
            if (d["kind"] in ("theorem", "lemma") and d["name"] and d["body"]
                    and not d["header"].lstrip().startswith("private")):
                by_file.setdefault(d["file"], []).append(d)

    idx = open(OUT / "subgoal_index.jsonl", "w")
    file_cache = {}
    n = 0
    for fpath, decls in by_file.items():
        decls.sort(key=lambda d: d["line"])
        n_ctx = 0
        # count earlier decls of any kind for context size — approximate via line
        for d in decls:
            qual = f'{d["namespace"]}.{d["name"]}' if d["namespace"] else d["name"]
            qual = qual if qual.startswith("OAI") else f"OAI.{qual}"
            cid = re.sub(r"[^A-Za-z0-9]+", "_", f"{d['module']}__{d['name']}")[:180]
            idx.write(json.dumps({
                "id": cid, "decl": qual, "file": fpath, "kind": d["kind"],
                "decl_line": d["line"], "decl_lines": d["n_lines"],
                "proof_body_bytes": len(d["body"]),
            }) + "\n")
            n += 1
    idx.close()
    print(f"indexed {n} provable decls in subgoal_index.jsonl")
    if index_only:
        return

    # materialize a sampled subset
    rows = [json.loads(l) for l in open(OUT / "subgoal_index.jsonl")]
    random.seed(0)
    by_file2 = {}
    for r in rows:
        by_file2.setdefault(r["file"], []).append(r)
    picked = []
    for fpath, rs in by_file2.items():
        rs = rs[:mpf] if len(rs) > mpf else rs
        picked.extend(rs)
    random.shuffle(picked)
    picked = picked[:materialize]

    # need decl start/end LINES for splicing — re-mine the specific files
    import importlib, mine_oai
    by_file3 = {}
    for r in picked:
        by_file3.setdefault(r["file"], set()).add(r["decl"])

    done = 0
    for fpath, quals in by_file3.items():
        p = LEAN / fpath
        if not p.exists():
            continue
        lines, imports, decls = mine_oai.parse_file(p, Path(fpath))
        src_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for kind, name, doc, s, e, ns in decls:
            if kind not in ("theorem", "lemma"):
                continue
            mod = str(fpath)[:-5].replace("/", ".")
            nsq = f"{ns}.{name}" if ns else name
            qual = nsq if nsq.startswith("OAI") else f"OAI.{nsq}"
            if qual not in quals:
                continue
            text = src_lines[s:e]
            header, body = mine_oai.split_header_body(text)
            # splice: keep prefix (through docstring) + sorried decl + suffix
            challenge = (
                "\n".join(src_lines[:s])
                + f"\n{header} := by\n  sorry\n"
                + "\n".join(src_lines[e:])
            )
            cid = re.sub(r"[^A-Za-z0-9]+", "_", f"{mod}__{qual}")[:180]
            (SG / f"{cid}.lean").write_text(challenge + "\n")
            (SG / f"{cid}.json").write_text(json.dumps({
                "challenge_module": f"Subgoals.{cid}",
                "solution_module": mod,
                "theorem_names": [qual],
                "definition_names": [],
                "permitted_axioms": AXIOMS,
                "enable_nanoda": False,
            }, indent=2) + "\n")
            done += 1
    print(f"materialized {done} challenge pairs into {SG}")


if __name__ == "__main__":
    main()
