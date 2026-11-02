# OAI-Math RL Environment & Dataset Pipeline

Reverse-engineered from [openai/math](https://github.com/openai/math) — the
internal-model research archive (721 manuscripts, 373 result families,
162K-file Lean 4 library, 418 Comparator challenges).

This repo contains the extraction pipeline and the adversarially-hardened RL
environments built on top of it. See **REPORT.md** for the full
reverse-engineering writeup, architecture, and validation status.

## Two environments

| Env | What |
|---|---|
| **Single-task** | One sorried Lean decl per episode. Fast gate (sorry text, import policy, elaboration, signature identity, axiom whitelist) → serialized Comparator audit (export diff + kernel replay + landrun sandbox). Sessions have checkpoints and shaped rewards. |
| **Campaign** (ultra-long-horizon) | A whole proof-cone workspace — every transitive OAI dependency with proofs excised. Stages compile into a private olean lib with the reference OAI **off `LEAN_PATH`**, so `import OAI.X` resolves to the agent's own earlier work. Per-stage signature identity + batch `#print axioms` + checkpoint/restore. |

## Layout

```
build_index.py    parse 418 Comparator challenge configs -> tasks.jsonl
mine_oai.py       extract 3.74M decls from 162K OAI files -> oai_decls.jsonl
mine_preprints.py extract 24K theorem-like envs from 8K TeX files
make_subgoals.py  excise decl bodies -> comparator-format task pairs
enrich_index.py   join tasks -> result families -> preprints
make_splits.py    file/family-grouped deterministic train/val/test splits
build_dataset.py  emit dedup'd SFT pairs + dep graph
env/
  task.py         TaskSpec (challenge | subgoal)
  grade.py        fast_tier + audit_tier + shaped_reward
  session.py      episode state, checkpoints, event log, manifest
  runner.py       CLI: --task / --subgoal / --campaign / --selftest / --redteam
  pool.py         parallel QA worker pool (fast ∥, audit serialized)
  campaign.py     campaign builder + CampaignChecker + time travel
  score.py        task-signal scoring
  env.sh          pod environment (elan toolchain on PATH)
```

## Verified on the pod

- Comparator end-to-end: `Your solution is okay` via kernel replay
- Red-team, single-task: sorry / stmt-weaken / axiom-smuggle / answer-key
  import / native_decide — **ALL-CAUGHT**
- Red-team, campaign: sig-weaken / decl-delete / axiom-smuggle /
  reference-import — **ALL-CAUGHT**

Data artifacts (`out/`, ~3.8 GB) are gitignored — they regenerate via the
pipeline scripts.
