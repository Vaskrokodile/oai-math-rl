"""TaskSpec: a single verifiable proving task for the OAI-math env.

Two task kinds:
  - "challenge": the 418 repo ComparatorChallenges (frontier theorems)
  - "subgoal":   excised-decl tasks generated from OAI proof corpus

A task presents the agent a directory containing `Solution.lean` — the
challenge file with the target decl's proof replaced by a TODO marker.
The agent must restore a kernel-valid proof; grading is two-tier:
  fast:  `lake env lean` compile + #print axioms + signature identity
  audit: full comparator run (export diff + kernel replay + axiom whitelist)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

AXIOMS_DEFAULT = ["propext", "Quot.sound", "Classical.choice"]
SORRY_MARKER = "sorry -- TODO: prove this"


def _first_sorried_header(text: str) -> str:
    """Canonical signature of the first sorried theorem/lemma in a challenge."""
    m = re.search(
        r"(?ms)(theorem|lemma)\s+[A-Za-z0-9_'\.]+\s*(.*?)(:=\s*by|=\s*by|:=)\s*sorry",
        text)
    if not m:
        return ""
    head = m.group(0)
    head = head[: head.rfind(":= by") if ":= by" in head else head.rfind("sorry")]
    return re.sub(r"\s+", " ", head).strip()


@dataclass
class TaskSpec:
    task_id: str
    kind: str                       # "challenge" | "subgoal"
    challenge_module: str           # module name as lake target
    theorem_names: list[str]
    definition_names: list[str] = field(default_factory=list)
    permitted_axioms: list[str] = field(default_factory=lambda: list(AXIOMS_DEFAULT))
    solution_module: str = ""       # reference module (answer key, never exposed)
    solution_imports: list[str] = field(default_factory=list)
    challenge_text: str = ""        # verbatim challenge .lean content
    expected_header: str = ""       # canonical decl signature (fast-tier identity check)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_repo(cls, lean_root: Path, name: str) -> "TaskSpec":
        cfg = json.loads((lean_root / "ComparatorChallenges" / f"{name}.json").read_text())
        text = (lean_root / "ComparatorChallenges" / f"{name}.lean").read_text()
        return cls(
            task_id=name, kind="challenge",
            challenge_module=cfg["challenge_module"],
            theorem_names=cfg["theorem_names"],
            definition_names=cfg.get("definition_names", []),
            permitted_axioms=cfg["permitted_axioms"],
            solution_module=cfg["solution_module"],
            solution_imports=cfg.get("solution_imports", []),
            challenge_text=text,
            expected_header=_first_sorried_header(text),
        )

    @classmethod
    def subgoal(cls, lean_root: Path, file: str, decl_line: int,
                decl_lines: int, decl_name: str, task_id: str) -> "TaskSpec":
        """Build a subgoal task by excising a decl's proof from file `file`."""
        src = (lean_root / file).read_text().splitlines()
        s, e = decl_line - 1, decl_line - 1 + decl_lines
        header_lines = src[s:e]
        # ':=' must be found at paren depth 0 — named args (F := v) inside
        # statements would otherwise truncate the header mid-expression
        joined = "\n".join(header_lines)
        depth, idx = 0, -1
        for i, c in enumerate(joined):
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth = max(0, depth - 1)
            elif depth == 0 and joined[i:i + 2] == ":=":
                idx = i
                break
        head = joined[:idx].rstrip() if idx >= 0 else joined
        challenge = "\n".join(src[:s]) + f"\n{head} := by\n  {SORRY_MARKER}\n" + "\n".join(src[e:])
        mod = file[:-5].replace("/", ".")
        return cls(
            task_id=task_id, kind="subgoal",
            challenge_module="Solution",
            theorem_names=[decl_name if decl_name.startswith("OAI") else f"OAI.{decl_name}"],
            solution_module=mod,
            challenge_text=challenge,
            expected_header=head,
            metadata={"source_file": file, "decl_line": decl_line,
                      "decl_lines": decl_lines},
        )

    def comparator_config(self, solution_module: str = "Solution") -> dict:
        return {
            "challenge_module": self.challenge_module,
            "solution_module": solution_module,
            "theorem_names": self.theorem_names,
            "definition_names": self.definition_names,
            "permitted_axioms": self.permitted_axioms,
            "enable_nanoda": False,
        }

    def task_prompt(self) -> str:
        return (
            "Prove the following Lean 4 theorem. The file below is your working "
            "draft — replace every `sorry` with a real proof. You may add helper "
            "lemmas, definitions, and imports that are available in this "
            "environment. Do not modify the statement of the listed theorems.\n\n"
            f"```lean\n{self.challenge_text}\n```"
        )
