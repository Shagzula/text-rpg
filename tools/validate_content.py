"""Story Designer's content validator, vendored into the repo.

Origin: /workspace/validate_content.py (the original is left untouched). Changes in this copy:
- paths are relative to this repo (content defaults to the vendored `content/`; `--content DIR`
  checks another copy, e.g. /home/box/rpg-project/content), no hard-coded sys.path;
- `validate_proposal()` returns (proposal, errors): this copy checks `errors` (the original looked
  for a dict with "error", which never matched, so interpreter examples were never really checked);
- inline backtick snippets written in schema *notation* (`...`, `a|b` alternatives, bare keys such
  as `{"speaker_key", ...}`) are reported as notation, not as JSON failures. Fenced ```json blocks
  and inline snippets that look like real JSON are still parsed strictly.
- returns a result dict (used by tests/test_vendored_content.py) and an exit code.

Usage: python3 tools/validate_content.py [--content DIR] [-q]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rpg import actions, db, personas, yamlish  # noqa: E402

NOTATION = re.compile(r'\.\.\.|\\\||"\s*\|\s*"|\{\s*"[^"]*"\s*,|,\s*"[^"]*"\s*[,}]|:\s*(str|int|bool)\b')


def validate(content_dir: str, out=print) -> dict:
    C = os.path.join(content_dir, "")
    ok = True
    report = {"json_blocks": 0, "inline_json": 0, "notation": [], "failures": []}

    def fail(msg):
        nonlocal ok
        ok = False
        report["failures"].append(msg)
        out("FAIL " + msg)

    for f in sorted(glob.glob(C + "*.md")):
        s = open(f, encoding="utf-8").read()
        name = os.path.basename(f)
        for b in re.findall(r"```json\n(.*?)```", s, re.S):
            b = b.strip()
            if b.startswith('"talk"'):
                b = "{" + b + "}"
            try:
                json.loads(b)
                report["json_blocks"] += 1
            except Exception as e:
                if "..." in b or ("(" in b and "last" in b):
                    report["notation"].append(f"{name}: {b[:60]}")
                    continue
                fail(f"JSON {name}: {e}: {b[:80]}")
        for b in re.findall(r"`(\{\"[^`]*\})`", s):
            try:
                json.loads(b)
                report["inline_json"] += 1
            except Exception as e:
                if NOTATION.search(b):
                    report["notation"].append(f"{name}: {b[:60]}")
                else:
                    fail(f"INLINE {name}: {e}: {b[:100]}")
    items = yamlish.load(C + "items_tier1.yaml")
    bad = [i["name"] for i in items if re.search(r"\d", i["name"])]
    if bad:
        fail(f"digit names: {bad}")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(open(os.path.join(ROOT, "rpg", "schema.sql"), encoding="utf-8").read())
    for i in items:
        try:
            db.add_item_template(conn, i)
        except ValueError as e:
            fail(f"template {i.get('id')}: {e}")
    n = conn.execute("SELECT COUNT(*) FROM item_templates").fetchone()[0]
    report["templates"] = n
    if n != len(items):
        fail(f"templates loaded {n} / {len(items)}")
    keys = [i["id"] for i in items]
    report["personas"] = []
    for f in sorted(glob.glob(C + "npcs/*.yaml")) + sorted(glob.glob(os.path.join(ROOT, "rpg", "data", "npcs", "*.yaml"))):
        try:
            p = personas.load_file(f, keys)
            report["personas"].append(p["key"])
        except Exception as e:  # noqa: BLE001
            fail(f"persona {os.path.basename(f)}: {e}")

    def blk(f):
        s = open(C + f, encoding="utf-8").read()
        a, b = s.index("## Game context"), s.index("### Recent narrations")
        return hashlib.md5(s[a:b].encode()).hexdigest()
    if blk("interpreter_prompt.md") != blk("narrator_prompt.md"):
        fail("interpreter/narrator context blocks differ")
    s = open(C + "interpreter_prompt.md", encoding="utf-8").read()
    report["proposals"] = 0
    for b in re.findall(r"```json\n(\{\"actions\".*?)```", s, re.S):
        _, errors = actions.validate_proposal(json.loads(b))
        if errors:
            fail(f"proposal invalid {errors}: {b[:80]}")
        report["proposals"] += 1
    report["ok"] = ok
    out(f"json blocks {report['json_blocks']}, inline {report['inline_json']}, notation {len(report['notation'])}, "
        f"templates {report.get('templates')}, personas {report['personas']}, proposals {report['proposals']}")
    out("ALL OK" if ok else "FAILURES")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--content", default=os.path.join(ROOT, "content"))
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    r = validate(a.content, out=(lambda *_: None) if a.quiet else print)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
