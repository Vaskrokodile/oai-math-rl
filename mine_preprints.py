#!/usr/bin/env python3
"""Extract informal (statement, proof) pairs from preprint LaTeX sources.

For each preprints/<slug>/build/**/*.tex file:
  - strips comments, resolves nothing (raw env text kept)
  - captures theorem-like environments: theorem, lemma, proposition,
    corollary, definition, conjecture, claim, remark
  - captures `proof` environments and attaches each proof to the nearest
    preceding theorem-like env in the same file (standard amsthm layout)

Output:
  out/preprint_theorems.jsonl — {slug, file, env, label, text, proof}
  out/preprint_files.jsonl    — {slug, file, n_envs, bytes}
"""
import json, re, sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "../math/preprints")
OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

ENV_RE = re.compile(
    r"\\begin\{(theorem|lemma|proposition|corollary|definition|conjecture|"
    r"claim|remark|proof|example|question|problem|assumption|fact|observation)"
    r"\*?\}(?:\[([^\]]*)\])?(.*?)(?=\\end\{\1\*?\})",
    re.DOTALL,
)
END_RE = re.compile(r"\\end\{(theorem|lemma|proposition|corollary|definition|"
                    r"conjecture|claim|remark|proof|example|question|problem|"
                    r"assumption|fact|observation)\*?\}")
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")


def strip_comments(src: str) -> str:
    out = []
    for line in src.splitlines():
        # remove unescaped % comments
        res, i = [], 0
        while i < len(line):
            c = line[i]
            if c == "\\" and i + 1 < len(line):
                res.append(line[i : i + 2]); i += 2; continue
            if c == "%":
                break
            res.append(c); i += 1
        out.append("".join(res))
    return "\n".join(out)


def main():
    tex_files = sorted(ROOT.glob("*/build/**/*.tex")) + sorted(ROOT.glob("*.tex"))
    n_pairs = n_envs = n_files = 0
    th_out = open(OUT / "preprint_theorems.jsonl", "w")
    f_out = open(OUT / "preprint_files.jsonl", "w")
    for path in tex_files:
        slug = path.parts[len(ROOT.parts) - len(path.parts) + 1] if "build" in path.parts else path.stem
        # safer slug: first path component under preprints/
        rel = path.relative_to(ROOT)
        slug = rel.parts[0] if len(rel.parts) > 1 else "(root)"
        try:
            src = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            print(f"skip {rel}: {e}", file=sys.stderr)
            continue
        n_files += 1
        envs = []
        for m in ENV_RE.finditer(src):
            env = m.group(1)
            opt = (m.group(2) or "").strip() or None
            text = m.group(3).strip()
            end = END_RE.search(src, m.end())
            lab = LABEL_RE.search(m.group(3))
            envs.append({
                "env": env, "title": opt, "text": text,
                "label": lab.group(1) if lab else None,
                "pos": m.start(),
            })
        # attach proofs to preceding non-proof env
        last = None
        for e in envs:
            if e["env"] == "proof":
                if last is not None and "proof" not in last:
                    last["proof"] = e["text"]
                    n_pairs += 1
            else:
                last = e
                n_envs += 1
        for e in envs:
            if e["env"] == "proof":
                continue
            e.pop("pos", None)
            th_out.write(json.dumps({"slug": slug, "file": str(rel), **e}) + "\n")
        f_out.write(json.dumps({"slug": slug, "file": str(rel),
                                "n_envs": len(envs), "bytes": path.stat().st_size}) + "\n")
    th_out.close(); f_out.close()
    print(f"{n_files} tex files, {n_envs} theorem-like envs, {n_pairs} proof pairs")


if __name__ == "__main__":
    main()
