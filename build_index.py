#!/usr/bin/env python3
"""Build the master task index for the openai/math repo.

Joins:
  ComparatorChallenges/*.json  (task spec: theorem_names, permitted_axioms)
  ComparatorChallenges/*.lean  (challenge statement with sorry)
  lean/formalization.yaml      (paper -> comparator_config -> declaration -> file)
  lean/docs/NNN.md             (family-scope notes)
  preprints/*/                 (informal paper + tex source)

Output: out/index.jsonl — one record per (challenge, theorem_name) pair.
"""
import json, re, sys
from pathlib import Path

LEAN = Path(sys.argv[1] if len(sys.argv) > 1 else "../math/lean")
OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)


def parse_formalization(path: Path):
    """Minimal YAML scrape: map comparator_config -> [(declaration, file)].
    Also map challenge name -> solution file(s). The yaml is regular enough
    that a line parse beats pulling in pyyaml."""
    decls = {}  # config -> list of (decl, file)
    cur = None
    for line in path.read_text().splitlines():
        m = re.match(r"\s*- comparator_config: (\S+)", line)
        if m:
            cur = m.group(1)
            decls.setdefault(cur, [])
            continue
        m = re.match(r"\s*declaration: (\S+)", line)
        if m and cur is not None:
            decls[cur].append({"declaration": m.group(1)})
            continue
        m = re.match(r"\s*file: (\S+)", line)
        if m and cur is not None and decls[cur]:
            decls[cur][-1]["file"] = m.group(1)
    return decls


def parse_challenge_lean(path: Path):
    """Extract imports and sorried theorem statements from a challenge file."""
    src = path.read_text()
    imports = re.findall(r"(?m)^import\s+(\S+)", src)
    # theorem/lemma decls ending in `by sorry` or `:= by sorry`
    decls = []
    for m in re.finditer(
        r"(?ms)(/--.*?-/\s*)?(?:@\[[^\]]*\]\s*)*"
        r"(theorem|lemma)\s+([A-Za-z0-9_'\.]+)\s*(.*?)((?::=)|=\s*by)\s*sorry",
        src,
    ):
        decls.append(
            {
                "doc": (m.group(1) or "").strip() or None,
                "kind": m.group(2),
                "name": m.group(3),
                "signature": re.sub(r"\s+", " ", m.group(4)).strip(),
            }
        )
    return imports, decls


def main():
    formalization = parse_formalization(LEAN / "formalization.yaml")
    records = []
    for jpath in sorted(LEAN.glob("ComparatorChallenges/*.json")):
        spec = json.loads(jpath.read_text())
        name = jpath.stem
        lpath = jpath.with_suffix(".lean")
        imports, decls = ([], [])
        if lpath.exists():
            imports, decls = parse_challenge_lean(lpath)
        config_rel = f"ComparatorChallenges/{name}.json"
        sol = formalization.get(config_rel, [])
        records.append(
            {
                "challenge": name,
                "challenge_module": spec["challenge_module"],
                "solution_module": spec["solution_module"],
                "solution_imports": spec.get("solution_imports", []),
                "theorem_names": spec["theorem_names"],
                "definition_names": spec.get("definition_names", []),
                "permitted_axioms": spec["permitted_axioms"],
                "enable_nanoda": spec.get("enable_nanoda", False),
                "challenge_file": str(lpath.relative_to(LEAN)),
                "challenge_imports": imports,
                "challenge_decls": decls,
                "solution_decls": sol,
            }
        )
    out = OUT / "index.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    n_thm = sum(len(r["theorem_names"]) for r in records)
    n_chdecl = sum(len(r["challenge_decls"]) for r in records)
    n_sof = sum(len(r["solution_decls"]) for r in records)
    print(f"wrote {out}: {len(records)} challenges, {n_thm} theorems,")
    print(f"  {n_chdecl} sorried decls parsed from challenge .lean,")
    print(f"  {n_sof} solution declarations linked via formalization.yaml")
    # sanity: sorried decl count vs theorem_names count
    mism = [
        r["challenge"]
        for r in records
        if len(r["challenge_decls"]) != len(r["theorem_names"]) + len(r["definition_names"])
    ]
    print(f"  decl-count mismatches: {len(mism)} {mism[:10]}")


if __name__ == "__main__":
    main()
