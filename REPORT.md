# Reverse-Engineering Report: openai/math

*What the repo actually is, how it was produced, and how to exploit it for
frontier-level math datasets + RL environments.*

## 1. What this repo is

The repo is the **raw output dump of OpenAI's internal frontier math model** —
not a curated dataset. Per README: ~4,000 open research problems were posed to
an unnamed internal model at ~3h of ChatGPT-Pro thinking compute each; outputs
were aggregated into **373 result families → 721 manuscripts**, of which ~42%
of top-line results were additionally **formalized in Lean 4** and shipped with
a verification harness.

The recovered generation pipeline (from `reasoning_traces/*.tex` verbatim
prompts):

```
Pose open problem P to model, fixed template:
  "Please give a complete rigorous solution to the following problem.
   Even if the problem is 'open', the intention is that you should
   resolve it and present a full solution.
   Your solution can be either a complete, rigorous resolution or a
   well-presented, rigorous refutation. Brute-force enumerations and
   computer-assisted proofs are strongly discouraged in the final writeup.
   # Problem
   <P>
   [Administrative details redacted.]"
        │
        ▼  ~3h thinking compute
   informal manuscript (LaTeX paper)
        │
        ▼  editorial aggregation
   373 result families (CONTENTS.md / overview.pdf)
        │
        ▼  separate formalization pass (Lean 4 agent — 'method: agent'
           in formalization.yaml)
   OAI/ shadow-library + ComparatorChallenges/ statement files
        │
        ▼  verification
   comparator (Lean FRO's trustless proof judge, built for AIMO)
```

## 2. Asset inventory (measured, not marketing)

| Asset | Size | Notes |
|---|---|---|
| `preprints/` | 751 dirs, 702MB | Full LaTeX source + PDF + BibTeX each. **23,990 theorem-like envs extracted, 19,909 with attached proofs.** |
| `OAI/` Lean lib | 162,417 files, 1.78GB src | **3,742,203 decls**: 1.63M theorems, 710K lemmas, 1.30M defs, 50K instances. Only 8 sorries. Kernel-verified. |
| `ComparatorChallenges/` | 418 pairs | 545 sorried theorem statements (self-contained, import Mathlib) + JSON verifier specs |
| `formalization.yaml` | 1833 lines | paper → comparator_config → decl → file join table |
| `docs/` | 245 files | family ↔ challenge linkage; every doc states formalization *scope* (what's covered vs not) |
| `reasoning_traces/` | 10 results | abridged research-CoT + verbatim generation prompts |
| `patches/` | 25 patches | forward-ports of 25 external Lean repos to lean4 v4.34.1 |
| `Internal/` | — | `oai_root_alias` attr machinery; OAI decls get root aliases |

Join graph (fully resolved, `out/tasks.jsonl`):
`family (doc NNN) ↔ papers ↔ challenge (.lean+.json) ↔ solution module ↔ decl`

## 3. The verification machinery (this is the actual prize)

The repo ships **Comparator** — a *trustless* proof judge built by Lean FRO for
the AIMO Kaggle competitions, i.e. purpose-built to grade **adversarial**
AI-generated Lean proofs:

- `challenge_module`: trusted statement file (sorries allowed).
- `solution_module`: untrusted candidate proof file.
- Guarantees on pass: (a) every `theorem_names` decl in Solution proves **the
  identical statement** as in Challenge — full declaration-identity check via
  `lean4export` environment diff, so weakening/reformulating is impossible;
  (b) proof uses **only** `permitted_axioms` (standard classical triple:
  propext, Quot.sound, Classical.choice — no `sorry`, no custom axioms);
  (c) Solution environment is **replayed into the kernel** (optionally a second
  kernel via `external_kernels` / `enable_nanoda`).
- Sandboxing: `landrun` (Linux LSM). On macOS, use `scripts/fake-landrun.sh`
  (dev) or run the whole env on Linux.
- `definition_names`: "definition holes" — the challenge can leave a `def`
  sorried and the solution must supply it (verified for name/type/universes/
  safety). Comparator README warns these are gameable (e.g. define the hole as
  the theorem itself) and need a second check — relevant for env design.
- Merged into Lean core as `lake comparator` + `lake check` in **v4.35.0** —
  this repo pins v4.34.1, so we use the standalone binary (or bump toolchain).

**This is a production-grade RL reward function with a security model already
designed for exactly our use case.** It was literally built so Kaggle could pay
money on AI proofs.

## 4. Exploitation strategy

### Env A — Frontier prover RL (the 418)
- Prompt: challenge .lean (statement + sorry) + allowed imports (Mathlib + the
  repo's 25 pinned deps).
- Action: candidate solution file.
- Reward: comparator verdict. Binary at top level; **shaped** by Lean compiler
  output (parse → elab → goals remaining via a REPL such as lean-repl/
  LeanInteract), and by statement-match partial credit.
- Hard-mode anti-leak: mount environment **without `OAI/`**; otherwise the
  model just re-exports the reference proof. Soft-mode: allow OAI modules as a
  retrieval library → trains "library-aware" proving.
- Difficulty: these are genuinely frontier (QRH, ETH-is-false, BSD...). Expect
  ~0% solve rate — this is the *aspiration* set, not the workhorse.

### Env B — Subgoal curriculum (2.34M tasks) ← **the workhorse**
Every theorem/lemma in OAI becomes a task by **body-excision**: file verbatim,
target decl's proof replaced with `sorry`. Context (imports, namespaces,
sibling lemmas incl. later ones, vars) stays identical → the sorried statement
elaborates identically, comparator-verifiable, zero trust issues.

- **2,338,891 provable decls indexed** (`out/subgoal_index.jsonl`), 476
  materialized pairs verified (`out/subgoals/`).
- Difficulty ladder is intrinsic: median proof 74 bytes (one-liner tactics),
  p90 ≈ 800B, p99 ≈ 2.8KB, max = multi-MB certificate proofs. Filter by
  `proof_body_bytes`, `n_ctx_decls`, depth in file, domain (OAI.<Area>).
- Reward shaping: fraction of goals closed, axiom compliance, statement
  identity. Dense enough for GRPO/PPO from scratch.
- **This is how you get RL scaling**: millions of on-distribution,
  machine-checkable tasks ordered by difficulty — the Lean analogue of
  procedural generation.

### Env C — Autoformalization RL (statement synthesis)
- Prompt: informal theorem text from a preprint .tex (19.9K pairs extracted,
  `out/preprint_theorems.jsonl`) + paper context.
- Action: a Lean *statement* (no proof needed).
- Verifier: export-compare candidate statement's elaborated form vs the OAI
  canonical statement (same machinery as comparator's statement-identity
  check; can reuse by treating candidate as challenge, canonical as solution
  with trivial `sorry`-free body, or via direct `lean4export` diff).
- Fixes the field's biggest problem: autoformalization evals are currently
  LLM-judged/fuzzy. Here ground truth exists for ~545 named results + every
  internal lemma (for which we also have... nothing informal — so restrict to
  doc-mapped formalized results + hand-align).

### Env D — Falsification / counterexample tasks
~30% of the collection is counterexample papers ("A counterexample to X").
Tasks: given conjecture C, produce a construction + proof of ¬C in Lean.
Reward: comparator on the negated statement. Trains refutation, which pure
theorem-proving RL never touches — and the OAI corpus contains complete
verified counterexamples (Kaplansky, Ryser, Sidorenko, Borsuk, ETH...).

### Env E — Definition-hole synthesis
Use comparator's `definition_names` mechanism to emit "fill in the object"
tasks: e.g. challenge defines `def counterexample : <Type> := sorry` + theorem
`prop_of counterexample`. Model must *construct* the witness. Only 1 in-repo
example (ElementaryPositivity) but the generator is trivial: pick any `def`
in OAI, hole it, keep its use-site theorem as the check. Massive untapped
task family for constructive reasoning.

### SFT corpora (cold-start data)
- **Formal**: 3.74M (header, body) decl pairs (`out/oai_decls.jsonl`) —
  statement→proof SFT, and statement-only prediction. Dedup + filter
  `private`/generated cert decls. Regenerate tactic-state trajectories on a
  Linux box via Lean REPL replay for step-level data.
- **Informal**: 19,909 (theorem, proof) pairs at *research* difficulty —
  nothing else public is at this level; plus 721 full papers for long-context
  document SFT.
- **Process**: 10 reasoning-trace documents + recovered prompt template →
  seed prompts and rubric for generating more research-CoT.
- **Metadata**: docs/*.md scope notes = human-readable alignment labels
  (paper-result ↔ formal-result, gaps marked).

### The prompt recipe (reusable asset)
The recovered template is directly reusable to manufacture *new* tasks: scrape
open problems (arXiv open-problem lists, conjecture wikis), wrap in the same
"resolve or refute, no brute force" template → SFT/RL prompt set that matches
the proven recipe. Their conversion: ~4000 posed → 373 families ≈ 9% yield at
frontier significance — a realistic difficulty prior.

## 5. Infra plan (practical)

- **Do not build on the 8GB Mac.** Mathlib cache alone ~5-10GB; full OAI build
  is much larger; comparator needs `landrun` (Linux). Use the Foundry MI350X
  pod or any Linux box: Lean is CPU-only; a 64-core pod runs ~50 parallel
  verifier workers easily.
- Toolchain: `elan` + `leanprover/lean4:v4.34.1` (pinned, `fixedToolchain`),
  `lake update`, `lake exe cache get` (mathlib oleans), build comparator +
  lean4export (+ nanoda for double-kernel checking on high-value tasks).
- Verifier service: pool of `lake env comparator <cfg>` workers; each task is
  one JSON + one solution file → stateless, horizontally scalable. Wrap in the
  systemd-run invocation from the Comparator README for defense in depth.
- OAI as prebuilt env: build `OAI.*` once into `.lake`, then per-task runs
  only recompile the candidate file (seconds) — comparator replays the
  env anyway for axiom checking.
- mmap gotcha documented in lean/README (`vm.max_map_count`,
  `GLIBC_TUNABLES`) — apply on the pod.

## 6. Anti-gaming / caveats

- **Leakage**: OAI is the answer key — mount policy decides task mode
  (hard: no OAI; lib-assisted: allow non-descendant OAI modules only —
  computable from the real dep graph via `lean4export`).
- **Statement weakening**: impossible by construction (declaration-identity
  diff) — this is why comparator exists.
- **Axiom smuggling**: blocked by permitted_axioms replay.
- **Definition holes**: gameable per Comparator's own README — add a
  "materiality" check (e.g. hole must not mention the theorem name; structural
  checks on the supplied term).
- **Sorry-hiding**: `sorryAx` is not in permitted_axioms → replay catches it.
- **Quality honesty**: ~58% of results have NO formalization; README says
  unformalized results "could have issues". Treat preprints as silver data —
  prefer the formalized 42% for ground truth. Some withdrawn papers exist
  (history.md) — parse each preprint's README for withdrawal notes before
  emitting tasks.
- **Provenance**: these proofs are one model's outputs — style is uniform
  (great for curriculum coherence; risk: dataset amplifies that model's
  idioms/blindspots). Mitigate by mixing mathlib-native data.

## 7. Deliverables produced (pipeline/)

| File | What |
|---|---|
| `out/tasks.jsonl` | 418 frontier tasks: family+papers+challenge text+spec+solution link |
| `out/index.jsonl` | raw challenge spec + parsed sorried decls |
| `out/oai_decls.jsonl` | 3,742,203 decls w/ header/body/doc/ns/span |
| `out/oai_files.jsonl` | per-file imports+stats (dep-graph seed) |
| `out/subgoal_index.jsonl` | 2,338,891 provable subgoal candidates |
| `out/subgoals/` | 476 materialized challenge pairs (verified splice) |
| `out/preprint_theorems.jsonl` | 23,990 informal envs, 19,909 w/ proofs |
| `build_index.py` `mine_oai.py` `make_subgoals.py` `mine_preprints.py` `enrich_index.py` | re-runnable extractors |

## 8. Built + validated on the pod (B200, 192 cores)

End-to-end verified, not just compiled:

| Component | Status | Evidence |
|---|---|---|
| Comparator+lean4export @ Lean 4.34.1 | ✅ | `lake env comparator` → "Your solution is okay" (CommonBasesFPRAS, 228s, kernel replay) |
| Two-tier grading | ✅ | fast gate (sorry text, import policy, elab, sig, axioms) → serialized audit |
| Single-task env | ✅ | session + checkpoints + shaped reward + manifest |
| **Red-team: ALL-CAUGHT** | ✅ | sorry-smuggle, stmt-weaken, axiom-smuggle, answer-key OAI import, native_decide |
| Campaign env (ultra-long-horizon) | ✅ | LEAN_PATH shadowing: stage N imports resolve to the agent's OWN earlier stage oleans; reference OAI is off-path → bottom-up enforced by toolchain |
| Campaign verifier | ✅ | per-stage sig identity (can't weaken/remove recorded decls); batch `#print axioms` over ALL recorded decls; strict/lenient final; checkpoint/restore invalidates only changed stages |
| **Campaign red-team: ALL-CAUGHT** | ✅ | sig-weaken, decl-delete, axiom-smuggle, reference-import |
| Worker pool | ✅ | parallel fast tier (shared lock), flock-serialized audits |
| Task scoring | ✅ | 1.74M scored; 60,989 Mathlib-only flagged as wave-1 |
| Splits | ✅ | file-grouped 1.67M/35K/39K train/val/test |
| SFT corpora | ✅ | 1,870,761 dedup'd formal pairs + 19,909 informal pairs |
| OAI library build | ⏳ | `lake build OAI` running — unlocks OAI-importing tasks (~98% of corpus) |

### Data scale (corrected numbers)

- 3,742,203 decls mined; **1,739,415 provable tasks** after private-decl
  filtering (privates get mangled `_private.*` names — unreferenceable).
- Namespace extraction fixed: `end` now pops the innermost named block
  (sections vs namespaces) — wrong qualified names previously failed
  comparator export, which is exactly the check working as intended.

### Honest 10/10 checklist

- [x] Verifier = kernel (comparator+lean4export), not regex/model
- [x] Statement identity by declaration export diff (not text)
- [x] Axiom whitelist transitively enforced (sorryAx/native_decide die)
- [x] Answer-key imports blocked (import policy + reference off LEAN_PATH)
- [x] Red-team suite: all attacks caught, both envs
- [x] Long-horizon: campaign workspaces + checkpoints + restore + progress
- [x] Reward shaped on prerequisite milestones (ungameable ladder) + costs
- [x] Contamination-safe splits (file/family groups, deterministic hash)
- [x] Reproducibility: toolchain+commit+challenge-hash manifest per episode
- [x] Dedup: 3,161,148 unique (statement+proof) pairs; 555,463 exact dup decls flagged
- [ ] OAI build done → QA pass across OAI-importing tasks
- [ ] Throughput benchmark at scale (audits/min under load)
- [ ] Task-signal calibration vs real rollouts (needs a policy in the loop)

### Trust model (current)

File-mode sessions: the agent's ONLY write surface is `Solution.lean` text.
All escapes must therefore ride inside the Lean source itself — which is why
the five attack classes (sorry, stmt, axiom, imports, native_decide) cover
the space. comparator's landrun sandbox contains the untrusted *build*; the
grader lives outside the episode dir. If we later give agents shell/tool
access inside episodes, isolation must be upgraded (per-episode landrun +
net-off + read-only .lake bind) — the checker code already assumes this
boundary and stays valid.
