"""Compare the vendored content copy (code/content/) with the Story Designer's live directory.

Usage: python3 tools/check_content_drift.py [--upstream DIR] [--sync]
Exit 0 = identical, 1 = drift (lists added/removed/changed files), 2 = upstream missing.
`--sync` copies upstream over the vendored copy (review the diff and commit it yourself)."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDORED = os.path.join(ROOT, "content")
UPSTREAM = os.environ.get("RPG_CONTENT_UPSTREAM", "/home/box/rpg-project/content")


def _files(d):
    out = {}
    for base, _, names in os.walk(d):
        for n in names:
            if n.startswith(".") or n.endswith((".pyc", "~")):
                continue
            p = os.path.join(base, n)
            out[os.path.relpath(p, d)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def drift(upstream=UPSTREAM, vendored=VENDORED):
    """None if upstream is missing, else {"added": [...], "removed": [...], "changed": [...]}."""
    if not os.path.isdir(upstream):
        return None
    up, ven = _files(upstream), _files(vendored)
    return {"added": sorted(set(up) - set(ven)), "removed": sorted(set(ven) - set(up)),
            "changed": sorted(k for k in set(up) & set(ven) if up[k] != ven[k])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", default=UPSTREAM)
    ap.add_argument("--sync", action="store_true")
    a = ap.parse_args(argv)
    d = drift(a.upstream)
    if d is None:
        print(f"upstream {a.upstream} not found")
        return 2
    if a.sync:
        shutil.rmtree(VENDORED)
        shutil.copytree(a.upstream, VENDORED)
        print("vendored copy replaced from upstream; review `git diff content/` and commit")
        return 0
    if not any(d.values()):
        print("vendored content matches upstream")
        return 0
    for k, v in d.items():
        for f in v:
            print(f"{k}: {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
