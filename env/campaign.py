"""Campaign tasks — ultra-long-horizon episodes over whole proof cones.

A campaign task hands the agent a *workspace of files*: the transitive
OAI-import cone of a target module, every file verbatim EXCEPT that all
theorem/lemma proofs are excised to `sorry`. The agent must re-prove the
entire dependency cone bottom-up — hours of compositional work that
mirrors real research-library development.

Grading:
  - per-file fast tier (compile + per-stage axioms)
  - campaign-final: `#print axioms <main>` must show ONLY permitted
    axioms — transitively impossible if any stage is cheated
  - optional audit: comparator on the top-level challenge
  - statement identity on every decl via canonical headers recorded at
    generation time (headers.jsonl)

Workspace layout:
  ws/
    stage_00_<File>.lean ... stage_kk_<File>.lean   (dep order, target last)
    manifest.json          — stages, decl headers, permitted axioms

NOTE for the verifier: stage files keep their *original* imports; they are
compiled in order into a private package dir so downstream stages see the
agent's own earlier proofs — not the reference OAI oleans. This is what
makes the campaign honest: the kernel replay chain is the agent's alone.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SORRY = "sorry -- TODO"


@dataclass
class CampaignSpec:
    task_id: str
    stages: list[dict]          # {file, module, n_sorried, headers:[{name,sig}]}
    main_theorem: str
    permitted_axioms: list[str]


def excise_proofs(src: str) -> tuple[str, list[dict]]:
    """Replace every theorem/lemma proof body with `by sorry`.
    Returns (new_src, headers). Line-oriented, same rules as mine_oai."""
    lines = src.splitlines()
    out, headers = [], []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        m = re.match(
            r"^(?:public\s+|private\s+|protected\s+|noncomputable\s+|scoped\s+|local\s+)*"
            r"(theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_'.]*)", line)
        if m:
            # capture decl span: until next col-0 decl/namespace/end/etc
            j = i
            span = [line]
            depth = 0
            while j + 1 < n:
                nxt = lines[j + 1]
                s2 = nxt.strip()
                if (nxt[:1] not in (" ", "\t") and s2
                        and re.match(
                            r"^(?:import|namespace|section\b|end\b|open\b|"
                            r"variable|universe|set_option|attribute|"
                            r"initialize|#|syntax|macro|elab|notation|"
                            r"deriving|export|run_cmd|--|/\-|@\[|"
                            r"(?:(?:public|private|protected|noncomputable|"
                            r"scoped|local)\s+)*(?:theorem|lemma|def|instance|"
                            r"structure|class|inductive|abbrev|example|"
                            r"axiom|opaque)\b)", nxt)):
                    break
                span.append(nxt)
                j += 1
            text = "\n".join(span)
            k = _top_level_assign(text)
            head = text[:k].rstrip() if k >= 0 else text
            headers.append({"name": m.group(2), "sig": re.sub(r"\s+", " ", head),
                            "sorry_line": len(out) + 2})
            out.append(f"{head} := by\n  {SORRY}")
            i = j + 1
        else:
            out.append(line)
            i += 1
    return "\n".join(out) + "\n", headers


def build_campaign(lean_root: Path, files: dict, target_module: str,
                   out_dir: Path, max_files: int = 12) -> CampaignSpec | None:
    """files = module -> {file, imports}; compute cone, emit sorried stages."""
    def cone(m):
        s = []
        seen = set()
        def go(x):
            if x in seen or not x.startswith("OAI.") or x not in files:
                return
            seen.add(x)
            for i in files[x]["imports"]:
                go(i)
            s.append(x)
        go(m)
        return s
    order = cone(target_module)
    if len(order) > max_files or target_module not in order:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stages = []
    for k, mod in enumerate(order):
        src = (lean_root / files[mod]["file"]).read_text()
        new, headers = excise_proofs(src)
        fname = f"stage_{k:02d}_" + files[mod]["file"].split("/")[-1]
        (out_dir / fname).write_text(new)
        stages.append({"file": fname, "module": mod,
                       "n_sorried": len(headers), "headers": headers})
    spec = CampaignSpec(
        task_id=f"camp_{target_module}",
        stages=stages,
        main_theorem="",   # filled by caller from challenge spec
        permitted_axioms=["propext", "Quot.sound", "Classical.choice"])
    (out_dir / "manifest.json").write_text(json.dumps({
        "task_id": spec.task_id, "stages": [
            {k: v for k, v in s.items() if k != "headers"} for s in stages],
        "permitted_axioms": spec.permitted_axioms}, indent=2))
    (out_dir / "headers.jsonl").write_text(
        "\n".join(json.dumps({"stage": i, **h})
                  for i, s in enumerate(stages) for h in s["headers"]))
    return spec


# ---------------------------------------------------------------------------
# Campaign verifier
#
# LEAN_PATH shadowing: stage k is compiled to
#   $SHADOW/OAI/Foo/Bar.olean   (module name = original, via `lean -R`)
# with LEAN_PATH = $SHADOW : <package libs only — project lib REMOVED>.
# Consequences:
#   * `import OAI.X` in stage k resolves to the AGENT's own stage olean iff X
#     is an earlier stage; to nothing otherwise (real OAI oleans are off-path)
#     -> bottom-up order enforced by the toolchain, reference unreachable.
#   * Mathlib/other packages resolve normally.
#   * `#print axioms <top>` at the end transitively covers the whole agent
#     chain — clean permitted-axiom list == every used decl really proved.
# ---------------------------------------------------------------------------

import fcntl, os, subprocess, time  # noqa: E402

TOOLCHAIN_BIN = Path.home() / ".elan/toolchains/leanprover--lean4---v4.34.1/bin"
AUDIT_LOCK = Path("/tmp/oai_audit.lock")


def lean_env(lean_root: Path, shadow_oleans: Path) -> dict:
    """env for campaign compiles: LEAN_PATH = shadow + package libs (no OAI)."""
    env = dict(os.environ)
    env["PATH"] = str(TOOLCHAIN_BIN) + ":" + env["PATH"]
    r = subprocess.run(["lake", "env", "env"], cwd=lean_root, env=env,
                       capture_output=True, text=True, timeout=120)
    lp = []
    for line in r.stdout.splitlines():
        if line.startswith("LEAN_PATH="):
            lp = line[len("LEAN_PATH="):].split(":")
    # strip the project lib (holds reference OAI oleans = the answer key);
    # keep package libs (mathlib & deps) + toolchain lib
    proj_lib = (lean_root / ".lake/build").resolve()
    kept = []
    for p in lp:
        if not p:
            continue
        try:
            if Path(p).resolve().is_relative_to(proj_lib):
                continue
        except OSError:
            continue
        kept.append(p)
    env["LEAN_PATH"] = ":".join([str(shadow_oleans)] + kept)
    return env


def _decls_in_src(src: str) -> dict[str, str]:
    """short decl name -> header text (before ':=') for theorem/lemma/def/etc."""
    out = {}
    lines = src.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line[:1] in (" ", "\t"):
            i += 1
            continue
        m = re.match(
            r"^(?:(?:public|private|protected|noncomputable|scoped|local)\s+)*"
            r"(theorem|lemma|def|instance|abbrev)\s+"
            r"([A-Za-z_][A-Za-z0-9_'.]*)", line)
        if not m:
            i += 1
            continue
        j = i
        span = [line]
        while j + 1 < n:
            nxt = lines[j + 1]
            if (nxt[:1] not in (" ", "\t") and nxt.strip()
                    and re.match(r"^(import|namespace|section\b|end\b|open\b|"
                                 r"variable|universe|set_option|attribute|"
                                 r"#|@\[|--|/\-)", nxt)):
                break
            span.append(nxt)
            j += 1
        text = "\n".join(span)
        k = _top_level_assign(text)
        out[m.group(2)] = text[:k].rstrip() if k >= 0 else text
        i = j + 1
    return out


def _top_level_assign(text: str) -> int:
    """Index of the ':=' that opens a decl body at paren depth 0."""
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


def _decls_at_lines(src: str, warn_lines: list[int]) -> list[str]:
    """Names of theorem/lemma decls whose spans contain each warning line."""
    lines = src.splitlines()
    spans = []  # (start0, end0, name)
    i, n = 0, len(lines)
    while i < n:
        m = re.match(
            r"^(?:public\s+|private\s+|protected\s+|noncomputable\s+|scoped\s+|"
            r"local\s+)*(theorem|lemma)\s+([A-Za-z_][A-Za-z0-9_'.]*)", lines[i])
        if not m:
            i += 1
            continue
        j = i
        while j + 1 < n:
            nxt = lines[j + 1]
            if (nxt[:1] not in (" ", "\t") and nxt.strip()
                    and re.match(r"^(import|namespace|section|end\b|open|"
                                 r"variable|universe|set_option|attribute|"
                                 r"#|@\[|--|/\-)", nxt)):
                break
            j += 1
        spans.append((i, j, m.group(2)))
        i = j + 1
    out = []
    for wl in warn_lines:
        for s, e, nm in spans:
            if s < wl <= e + 1:
                out.append(nm)
                break
    return out


class CampaignChecker:
    """Verifies a campaign workspace by shadow-compiling stages in order."""

    def __init__(self, lean_root: Path, spec_dir: Path, ep_dir: Path):
        self.lean_root = Path(lean_root)
        self.spec = json.loads((Path(spec_dir) / "manifest.json").read_text())
        self.ws = Path(spec_dir)
        self.ep = Path(ep_dir)
        self.shadow_src = self.ep / "shadow_src"
        self.shadow_lib = self.ep / "shadow_lib"
        for d in (self.shadow_src, self.shadow_lib):
            d.mkdir(parents=True, exist_ok=True)
        self.env = lean_env(self.lean_root, self.shadow_lib)
        self.built: list[str] = []        # modules successfully shadow-built
        self.decl_state: dict[str, dict] = {}  # decl -> {stage, axioms_ok}

    def _mod_to_path(self, mod: str, root: Path, ext: str) -> Path:
        return root / (mod.replace(".", "/") + ext)

    def _stage_headers(self, stage_idx: int) -> list[dict]:
        return [json.loads(l)
                for l in (self.ws / "headers.jsonl").read_text().splitlines()
                if json.loads(l)["stage"] == stage_idx]

    def _check_signatures(self, stage_idx: int, src: str) -> list[str]:
        """Every recorded decl must still exist with its canonical signature
        (whitespace-normalized). New decls are allowed; edits/removals are not."""
        errs = []
        found = _decls_in_src(src)
        for h in self._stage_headers(stage_idx):
            name, want = h["name"], re.sub(r"\s+", " ", h["sig"]).strip()
            got = found.get(name)
            if got is None:
                errs.append(f"decl removed/renamed: {name}")
            elif re.sub(r"\s+", " ", got).strip() != want:
                errs.append(f"statement changed: {name}")
        return errs

    def compile_stage(self, stage_idx: int, timeout: int = 1800,
                      check_sigs: bool = True) -> dict:
        """Compile one stage file (agent's ws version) into the shadow lib."""
        st = self.spec["stages"][stage_idx]
        mod = st["module"]
        src = (self.ws / st["file"]).read_text()
        if check_sigs:
            sig_errs = self._check_signatures(stage_idx, src)
            if sig_errs:
                return {"stage": stage_idx, "module": mod, "ok": False,
                        "sorried_decls": [], "elapsed_s": 0.0,
                        "err": "sig violations: " + "; ".join(sig_errs[:5])}
        lean_file = self._mod_to_path(mod, self.shadow_src, ".lean")
        olean = self._mod_to_path(mod, self.shadow_lib, ".olean")
        ilean = self._mod_to_path(mod, self.shadow_lib, ".ilean")
        lean_file.parent.mkdir(parents=True, exist_ok=True)
        olean.parent.mkdir(parents=True, exist_ok=True)
        lean_file.write_text(src)
        t0 = time.time()
        r = subprocess.run(
            ["lean", "-R", str(self.shadow_src), str(lean_file),
             "-o", str(olean), "-i", str(ilean)],
            env=self.env, capture_output=True, text=True, timeout=timeout)
        el = round(time.time() - t0, 1)
        err = (r.stderr or "") + (r.stdout or "")
        # lean warns "declaration uses 'sorry'" at the sorry's line — find the
        # decl currently containing that line in the agent's working file
        warn_lines = [int(m.group(1))
                      for m in re.finditer(r"^\S+\.lean:(\d+):\d+: warning:"
                                           r" declaration uses", err, re.M)]
        sorried = _decls_at_lines(src, warn_lines)
        ok = r.returncode == 0 and "error" not in err.lower()
        if ok:
            self.built.append(mod)
        return {"stage": stage_idx, "module": mod, "ok": ok,
                "sorried_decls": sorried, "elapsed_s": el,
                "err": err[-800:] if not ok else ""}

    def axioms_of(self, decl: str, timeout: int = 900) -> dict:
        """#print axioms <decl> against the shadow env (+ imported stage)."""
        stage_idx, mod = self._decl_module(decl)
        full = self._qualified(stage_idx, decl)
        probe = self.ep / "_axiom_probe.lean"
        probe.write_text(f"import {mod}\n#print axioms {full}\n")
        r = subprocess.run(["lean", str(probe)], env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        out = r.stdout + r.stderr
        m = re.search(r"depends on axioms: \[([^\]]*)\]", out)
        axs = [a.strip() for a in m.group(1).split(",")] if m else ["<unknown>"]
        return {"decl": full, "axioms": axs,
                "clean": set(axs) <= set(self.spec["permitted_axioms"])}

    def _qualified(self, stage_idx: int, decl: str) -> str:
        """Fully-qualified name of a decl inside its stage source file."""
        if "." in decl:
            return decl
        src = (self.ws / self.spec["stages"][stage_idx]["file"]).read_text()
        ns_stack: list[tuple[str, str]] = []
        for line in src.splitlines():
            if line[:1] in (" ", "\t"):
                continue
            m = re.match(r"^namespace\s+(\S+)", line)
            if m:
                ns_stack.append(("ns", m.group(1)))
                continue
            m = re.match(r"^section\b\s*(\S+)?", line)
            if m:
                ns_stack.append(("sec", m.group(1) or ""))
                continue
            m = re.match(r"^end\s*(\S+)?", line)
            if m:
                ename = m.group(1)
                if ename:
                    for k in range(len(ns_stack) - 1, -1, -1):
                        if ns_stack[k][1] == ename:
                            del ns_stack[k:]
                            break
                    else:
                        ns_stack and ns_stack.pop()
                elif ns_stack:
                    ns_stack.pop()
                continue
            if re.match(r"^(?:theorem|lemma|def|instance|abbrev)\s+" +
                        re.escape(decl) + r"\b", line):
                ns = ".".join(p for k, p in ns_stack if k == "ns" and p)
                return f"{ns}.{decl}" if ns else decl
        return decl

    def _decl_module(self, decl: str) -> tuple[int, str]:
        """Find which stage declares `decl` via headers.jsonl."""
        short = decl.rsplit(".", 1)[-1]
        for line in (self.ws / "headers.jsonl").read_text().splitlines():
            h = json.loads(line)
            if h["name"] == short or h["name"].endswith("." + short):
                return h["stage"], self.spec["stages"][h["stage"]]["module"]
        # fall back: last built stage's module
        idx = len(self.built) - 1 if self.built else 0
        return idx, self.spec["stages"][idx]["module"]

    def progress(self, timeout_per: int = 1800) -> dict:
        """Compile all stages in order; report per-stage ok + sorried counts.
        Stops at first failing stage (downstream can't build anyway)."""
        results = []
        for i in range(len(self.spec["stages"])):
            r = self.compile_stage(i, timeout_per)
            results.append(r)
            if not r["ok"]:
                break
        done = sum(1 for r in results if r["ok"])
        total_sorried = sum(s["n_sorried"] for s in self.spec["stages"])
        still_sorried = sum(len(r["sorried_decls"]) for r in results)
        return {"stages_ok": done, "stages_total": len(self.spec["stages"]),
                "sorried_remaining": still_sorried,
                "sorried_total": total_sorried,
                "results": results}

    def all_axioms(self, timeout: int = 3600) -> dict[str, list[str]]:
        """One probe file: #print axioms for EVERY recorded decl, batched.
        Covers the whole cone transitively — sorries anywhere are caught."""
        decls = []  # (stage_idx, short_name)
        for line in (self.ws / "headers.jsonl").read_text().splitlines():
            h = json.loads(line)
            decls.append((h["stage"], h["name"]))
        imports, prints = [], []
        full_names = []
        for stage_idx, nm in decls:
            mod = self.spec["stages"][stage_idx]["module"]
            full = self._qualified(stage_idx, nm)
            full_names.append(full)
            imports.append(f"import {mod}")
            prints.append(f"#print axioms {full}")
        probe = self.ep / "_axioms_all.lean"
        probe.write_text("\n".join(sorted(set(imports)) + prints) + "\n")
        r = subprocess.run(["lean", str(probe)], env=self.env,
                           capture_output=True, text=True, timeout=timeout)
        out = r.stdout + r.stderr
        res = {}
        blocks = re.findall(
            r"'([^']+)' depends on axioms: \[([^\]]*)\]", out)
        for i, full in enumerate(full_names):
            axs = None
            for nm, lst in blocks:
                if nm == full or nm.endswith("." + full) or full.endswith(
                        "." + nm):
                    axs = [a.strip() for a in lst.split(",")]
                    break
            res[full] = axs if axs is not None else ["<unknown>"]
        return res

    # -- time travel -------------------------------------------------------
    # A campaign is hours long; the whole workspace (all stage files) is the
    # state. Snapshots are full-directory copies — cheap at ~12 files — and a
    # restore returns the verifier to the matching shadow build by rebuilding
    # only the stages whose content changed.
    def checkpoint(self, tag: str = "auto") -> str:
        ts = int(time.time())
        name = f"{tag}_{ts}"
        dst = self.ep / "checkpoints" / name
        dst.mkdir(parents=True, exist_ok=True)
        for st in self.spec["stages"]:
            src = self.ws / st["file"]
            if src.exists():
                (dst / st["file"]).write_text(src.read_text())
        meta = {"name": name, "built": list(self.built)}
        (dst / "_meta.json").write_text(json.dumps(meta))
        return name

    def list_checkpoints(self) -> list[str]:
        d = self.ep / "checkpoints"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def restore(self, name: str) -> dict:
        dst = self.ep / "checkpoints" / name
        if not dst.exists():
            raise FileNotFoundError(name)
        changed = []
        for st in self.spec["stages"]:
            snap = dst / st["file"]
            ws_f = self.ws / st["file"]
            if snap.exists() and ws_f.read_text() != snap.read_text():
                changed.append(st["module"])
            if snap.exists():
                ws_f.write_text(snap.read_text())
        # invalidate shadow builds at/after the earliest changed stage
        if changed:
            first = min(i for i, s in enumerate(self.spec["stages"])
                        if s["module"] in changed)
            for m in self.built[first:]:
                ol = self._mod_to_path(m, self.shadow_lib, ".olean")
                il = self._mod_to_path(m, self.shadow_lib, ".ilean")
                ol.unlink(missing_ok=True)
                il.unlink(missing_ok=True)
            del self.built[first:]
        return {"restored": name, "invalidated": changed,
                "built_kept": len(self.built)}

    def final_check(self, main_theorem: str | None = None,
                    strict: bool = True, timeout: int = 3600) -> dict:
        """Campaign verdict. strict: every recorded decl axiom-clean.
        lenient: only main theorem clean (agent may find a shorter path)."""
        permitted = set(self.spec["permitted_axioms"])
        if strict:
            allax = self.all_axioms(timeout)
            bad = {d: a for d, a in allax.items() if not set(a) <= permitted}
            return {"ok": not bad, "strict": True,
                    "decls_clean": len(allax) - len(bad),
                    "decls_total": len(allax),
                    "bad_decls": dict(list(bad.items())[:10])}
        ax = self.axioms_of(main_theorem, timeout)
        return {"ok": ax["clean"], "strict": False,
                "axioms": ax["axioms"], "main": main_theorem}
