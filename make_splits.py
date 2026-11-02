#!/usr/bin/env python3
"""Contamination-safe splits for the OAI-math corpus.

Subgoal tasks: split BY FILE — declarations in the same file share imports,
namespaces, local notation and context, so letting them straddle splits
leaks. File is the atomic unit.

Frontier challenges: split BY RESULT FAMILY — challenges in one family are
the same result cluster (shared paper context, often shared lemmas).

Deterministic: assignment = sha1(group_key) mod 100 -> train/val/test
(96/2/2 for subgoals; 80/10/10 for the small frontier set).

Emits:
  out/splits.jsonl   {task_id, group, split}
  out/split_stats.json
"""
import hashlib, json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).parent / "out"


def bucket(key: str, bounds=(96, 98)) -> str:
    h = int(hashlib.sha1(key.encode()).hexdigest(), 16) % 100
    return "train" if h < bounds[0] else ("val" if h < bounds[1] else "test")


def main():
    stats = Counter()
    with open(OUT / "splits.jsonl", "w") as w:
        for line in open(OUT / "subgoal_index.jsonl"):
            r = json.loads(line)
            g = r["file"]
            s = bucket(g)
            stats[("subgoal", s)] += 1
            w.write(json.dumps({"task_id": r["id"], "group": g,
                                "kind": "subgoal", "split": s}) + "\n")
        tasks = OUT / "tasks.jsonl"
        if tasks.exists():
            for line in open(tasks):
                r = json.loads(line)
                g = r.get("family") or r.get("result_family") or r["name"]
                s = bucket(g, bounds=(80, 90))
                stats[("challenge", s)] += 1
                w.write(json.dumps({"task_id": r.get("name"), "group": g,
                                    "kind": "challenge", "split": s}) + "\n")
    (OUT / "split_stats.json").write_text(json.dumps(
        {f"{k[0]}/{k[1]}": v for k, v in sorted(stats.items())}, indent=2))
    print({f"{k[0]}/{k[1]}": v for k, v in sorted(stats.items())})


if __name__ == "__main__":
    main()
