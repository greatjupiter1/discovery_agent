#!/usr/bin/env python3
"""Merge two state-like JSON files — used by the workflow commit-back
when a plain rebase conflicts (two runs finishing in parallel).

Usage: merge_state.py OURS TARGET

- If either file has a "productions" list (state.json): TARGET keeps
  its entries and gains every OURS production whose identity key is
  not already present — a run's production is re-applied onto the
  latest state, never lost to a rebase conflict.
- Otherwise both files are treated as flat dicts (verify_cache.json):
  union, keeping the entry with the later "verified_at" on conflict.

TARGET is written atomically. Stdlib only.
"""
import json
import os
import sys


def _key(p):
    return tuple(str(p.get(k) or "").strip().lower()
                 for k in ("category", "type", "field", "topic",
                           "language", "title"))


def main():
    ours_path, target_path = sys.argv[1], sys.argv[2]
    with open(ours_path, encoding="utf-8") as f:
        ours = json.load(f)
    target = {}
    if os.path.exists(target_path):
        with open(target_path, encoding="utf-8") as f:
            target = json.load(f)
    if "productions" in ours or "productions" in target:
        merged = list(target.get("productions", []))
        seen = {_key(p) for p in merged}
        for p in ours.get("productions", []):
            if _key(p) not in seen:
                merged.append(p)
                seen.add(_key(p))
        target["productions"] = merged
    else:
        for k, v in ours.items():
            cur = target.get(k)
            if cur is None or str(v.get("verified_at", "")) \
                    >= str(cur.get("verified_at", "")):
                target[k] = v
    tmp = target_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(target, f, ensure_ascii=False, indent=1)
    os.replace(tmp, target_path)
    print(f"merge_state: merged {ours_path} into {target_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
