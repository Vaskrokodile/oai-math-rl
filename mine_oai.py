#!/usr/bin/env python3
"""Mine the OAI.lean library into a declaration-level corpus.

For every .lean file under OAI/, extracts top-level declarations:
  {file, module, name, kind, namespace, doc, header, body, is_private, line}

Header = statement up to `:=` / `:= by` / `where`; body = remainder.
Output: out/oai_decls.jsonl (one decl per line), out/oai_files.jsonl (per-file
stats: imports, decl count, bytes).

Line-oriented, comment-aware, no Lean required. Approximation error is bounded:
decl boundaries are col-0 keyword starts; nested `end`/`where` inside proofs are
indented and safe.
"""
import json, re, sys, os
from pathlib import Path

LEAN = Path(sys.argv[1] if len(sys.argv) > 1 else "../math/lean")
OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

DECL_KW = re.compile(
    r"^(?:public\s+|private\s+|protected\s+|noncomputable\s+|scoped\s+|local\s+)*"
    r"(theorem|lemma|def|instance|structure|class|inductive|abbrev|example|"
    r"axiom|opaque)\b"
)
TOPLEVEL_KW = re.compile(
    r"^(import|namespace|section|end|open|variable|universe|set_option|"
    r"attribute|initialize|#|syntax|macro|elab|notation|infix|prefix|postfix|"
    r"deriving|mutual|export|partial\s+def|run_cmd)\b"
)
NAME_RE = re.compile(
    r"^(?:public\s+|private\s+|protected\s+|noncomputable\s+|scoped\s+|local\s+)*"
    r"(?:theorem|lemma|def|instance|structure|class|inductive|abbrev|example|"
    r"axiom|opaque)\s+([A-Za-z_][A-Za-z0-9_'.«»\u2070-\u2189]*)"
)


def module_name(relpath: Path) -> str:
    return str(relpath.with_suffix("")).replace("/", ".")


def parse_file(path: Path, rel: Path):
    """Yield (kind, name, doc, start, end) decl spans; also collect imports."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    imports = []
    decls = []
    comment_depth = 0
    i, n = 0, len(lines)
    pending_doc = []
    cur_ns = []
    while i < n:
        line = lines[i]
        stripped = line.strip()
        col0 = line[:1] not in (" ", "\t") and stripped != ""

        # track block comments
        if comment_depth > 0:
            comment_depth += line.count("/-") - line.count("-/")
            i += 1
            continue
        if stripped.startswith("/-") or stripped.startswith("--"):
            if stripped.startswith("/-"):
                comment_depth += line.count("/-") - line.count("-/")
                if stripped.startswith("/--"):
                    # docstring: gather whole block
                    doc = [line]
                    while comment_depth > 0 and i + 1 < n:
                        i += 1
                        doc.append(lines[i])
                        comment_depth += lines[i].count("/-") - lines[i].count("-/")
                    pending_doc = doc
            i += 1
            continue

        if col0:
            m = re.match(r"^import\s+(\S+)", line)
            if m:
                imports.append(m.group(1))
                pending_doc = []
                i += 1
                continue
            m = re.match(r"^namespace\s+(\S+)", line)
            if m:
                # dotted namespaces push as ONE block whose parts all count
                cur_ns.append(("ns", m.group(1)))
                pending_doc = []
                i += 1
                continue
            m = re.match(r"^section\b\s*(\S+)?", line)
            if m:
                cur_ns.append(("sec", m.group(1) or ""))
                pending_doc = []
                i += 1
                continue
            if re.match(r"^end\b", line):
                m = re.match(r"^end\s*(\S+)?", line)
                ename = m.group(1) if m else None
                if ename:
                    # pop innermost block with matching name (ns or section)
                    for k in range(len(cur_ns) - 1, -1, -1):
                        if cur_ns[k][1] == ename:
                            del cur_ns[k:]
                            break
                    else:
                        if cur_ns:
                            cur_ns.pop()
                elif cur_ns:
                    cur_ns.pop()
                pending_doc = []
                i += 1
                continue
            dm = DECL_KW.match(line)
            if dm:
                start = i
                kind = dm.group(1)
                nm = NAME_RE.match(line)
                name = nm.group(1) if nm else None
                i += 1
                # capture body until next col-0 decl/toplevel keyword
                while i < n:
                    l2 = lines[i]
                    s2 = l2.strip()
                    if comment_depth > 0:
                        comment_depth += l2.count("/-") - l2.count("-/")
                        i += 1
                        continue
                    if s2.startswith("/-"):
                        comment_depth += l2.count("/-") - l2.count("-/")
                        i += 1
                        continue
                    if (
                        l2[:1] not in (" ", "\t")
                        and s2 != ""
                        and (DECL_KW.match(l2) or TOPLEVEL_KW.match(l2)
                             or s2.startswith("--")
                             or s2.startswith("@["))
                    ):
                        break
                    i += 1
                decls.append((
                    kind, name, pending_doc, start, i,
                    ".".join(p for k2, p in cur_ns if k2 == "ns" and p)))
                pending_doc = []
                continue
            # any other col-0 line: reset doc unless attribute line
            if not stripped.startswith("@["):
                pending_doc = []
        i += 1
    return lines, imports, decls


def top_level_assign(text: str) -> int:
    """Index of the ':=' that opens a decl body — must sit at paren depth 0.
    Named-argument syntax '(F := v)' inside statements contains ':='; the
    naive first-match truncates headers mid-expression."""
    depth, i, n = 0, 0, len(text)
    while i < n:
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and text[i:i + 2] == ":=":
            return i
        i += 1
    return -1


def split_header_body(text_lines):
    """Split decl text into signature (through ':=' or 'where' or 'by') and body."""
    full = "\n".join(text_lines)
    k = top_level_assign(full)
    if k >= 0:
        return full[:k].strip(), full[k:].strip()
    return full.strip(), ""


def main():
    files = sorted(LEAN.glob("OAI/**/*.lean"))
    decl_out = open(OUT / "oai_decls.jsonl", "w")
    file_out = open(OUT / "oai_files.jsonl", "w")
    ndecl = 0
    for k, path in enumerate(files):
        rel = path.relative_to(LEAN)
        try:
            lines, imports, decls = parse_file(path, rel)
        except Exception as e:
            print(f"parse error {rel}: {e}", file=sys.stderr)
            continue
        file_out.write(json.dumps({
            "file": str(rel), "module": module_name(rel),
            "imports": imports, "n_decls": len(decls),
            "bytes": path.stat().st_size,
        }) + "\n")
        for kind, name, doc, s, e, ns in decls:
            text = lines[s:e]
            header, body = split_header_body(text)
            decl_out.write(json.dumps({
                "file": str(rel), "module": module_name(rel),
                "namespace": ns, "kind": kind, "name": name,
                "doc": "\n".join(doc) if doc else None,
                "header": header, "body": body,
                "line": s + 1, "n_lines": e - s,
            }) + "\n")
            ndecl += 1
        if k % 20000 == 0:
            print(f"{k}/{len(files)} files, {ndecl} decls", flush=True)
    decl_out.close(); file_out.close()
    print(f"done: {len(files)} files, {ndecl} decls")


if __name__ == "__main__":
    main()
