"""Rollout session: agent workspace + checkpoints + action loop.

A session gives the agent:
  <ep_dir>/Solution.lean   — working proof file (seeded from challenge_text)
  actions:
    write(text)      — replace Solution.lean
    check()          — fast_tier grade -> milestone dict + errors
    submit()         — fast_tier gate, then audit_tier (comparator)
    checkpoint(tag)  — snapshot file state (time-travel)
    restore(tag)     — restore snapshot
    finish()         — end episode, flush log

Episode log (jsonl) records every action + grade for trajectory mining.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from task import TaskSpec
from grade import GradeResult, fast_tier, audit_tier, shaped_reward


def _git_rev(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def env_manifest(lean_root: Path) -> dict:
    """Reproducibility record: toolchain, repo commit, challenge hash."""
    tool = (lean_root / "lean-toolchain").read_text().strip() \
        if (lean_root / "lean-toolchain").exists() else ""
    return {"lean_toolchain": tool,
            "repo_commit": _git_rev(lean_root.parent),
            "lean_root": str(lean_root)}


class Session:
    def __init__(self, task: TaskSpec, lean_root: Path, ep_root: Path,
                 env: dict, max_audits: int = 10):
        self.task = task
        self.lean_root = Path(lean_root)
        self.env = env
        self.max_audits = max_audits
        self.audits_used = 0
        self.dir = Path(ep_root) / task.task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "checkpoints").mkdir(exist_ok=True)
        self.sol = self.dir / "Solution.lean"
        self.sol.write_text(task.challenge_text)
        self.logf = open(self.dir / "episode.jsonl", "a")
        manifest = env_manifest(self.lean_root)
        manifest["challenge_sha"] = hashlib.sha256(
            task.challenge_text.encode()).hexdigest()[:16]
        manifest["task_id"] = task.task_id
        (self.dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        self.t0 = time.time()
        self.done = False
        self.result: GradeResult | None = None
        self.n_checks = 0
        self.last_fast: GradeResult | None = None

    def reward(self) -> float:
        return shaped_reward(self.last_fast, self.result,
                             self.n_checks, self.audits_used)

    def _log(self, kind: str, payload: dict):
        self.logf.write(json.dumps(
            {"t": round(time.time() - self.t0, 3), "kind": kind, **payload}) + "\n")
        self.logf.flush()

    def write(self, text: str):
        self.sol.write_text(text)
        self._log("write", {"bytes": len(text)})

    def checkpoint(self, tag: str = "auto") -> str:
        ts = int(time.time())
        name = f"{tag}_{ts}"
        (self.dir / "checkpoints" / f"{name}.lean").write_text(self.sol.read_text())
        self._log("checkpoint", {"name": name})
        return name

    def restore(self, name: str):
        p = self.dir / "checkpoints" / f"{name}.lean"
        if not p.exists():
            raise FileNotFoundError(name)
        self.sol.write_text(p.read_text())
        self._log("restore", {"name": name})

    def check(self) -> GradeResult:
        self.n_checks += 1
        r = fast_tier(self.task, self.lean_root, self.dir)
        self.last_fast = r
        self._log("check", {"ok": r.ok, "ms": r.milestones,
                            "axioms": r.axioms_used,
                            "errors": r.errors[:3], "elapsed_s": r.elapsed_s})
        return r

    def submit(self) -> GradeResult:
        self.n_checks += 1
        fast = fast_tier(self.task, self.lean_root, self.dir)
        self.last_fast = fast
        if not fast.ok:
            self._log("submit", {"gate": "fast_failed",
                                 "errors": fast.errors[:3]})
            return fast
        if self.audits_used >= self.max_audits:
            r = GradeResult(ok=False, tier="audit")
            r.errors.append("audit budget exhausted")
            return r
        self.audits_used += 1
        r = audit_tier(self.task, self.lean_root, self.dir, self.env)
        self._log("submit", {"ok": r.ok, "elapsed_s": r.elapsed_s,
                             "errors": r.errors[:3]})
        if r.ok:
            self.result = r
            self.done = True
            self._log("solved", {"elapsed_s": r.elapsed_s})
        return r

    def finish(self) -> dict:
        rew = self.reward()
        self._log("finish", {"solved": bool(self.result and self.result.ok),
                             "reward": rew,
                             "total_s": round(time.time() - self.t0, 1)})
        self.logf.close()
        return {"task_id": self.task.task_id,
                "solved": bool(self.result and self.result.ok),
                "reward": rew,
                "audits": self.audits_used,
                "checks": self.n_checks}
