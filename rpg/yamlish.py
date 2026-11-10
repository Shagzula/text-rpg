"""A small YAML-subset reader (stdlib only) for content files: block mappings and
lists by indentation, flow lists/maps ([a, b], {k: v}), quoted and plain scalars,
ints/floats, true/false/null, and # comments. Enough for content/npcs/*.yaml and
items_tier1.yaml; not a general YAML parser (no anchors, multi-docs, or block scalars)."""
from __future__ import annotations

import re
from typing import Any, List, Tuple


class YamlError(ValueError):
    pass


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(tok: str) -> Any:
    t = tok.strip()
    if t == "" or t in ("null", "~", "Null", "NULL"):
        return None
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        inner = t[1:-1]
        return inner.replace('\\"', '"') if t[0] == '"' else inner.replace("''", "'")
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d*", t):
        return float(t)
    return t


class _Flow:
    def __init__(self, text: str):
        self.s, self.i = text, 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1

    def value(self) -> Any:
        self.ws()
        ch = self.s[self.i] if self.i < len(self.s) else ""
        if ch == "[":
            self.i += 1
            out = []
            while True:
                self.ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return out
                out.append(self.value())
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
        if ch == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.s[self.i] == "}":
                    self.i += 1
                    return out
                key = self.token(":")
                self.i += 1  # ':'
                out[_scalar(key)] = self.value()
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
        return _scalar(self.token(",]}"))

    def token(self, stops: str) -> str:
        self.ws()
        start = self.i
        if self.i < len(self.s) and self.s[self.i] in "\"'":
            q = self.s[self.i]
            self.i += 1
            while self.i < len(self.s) and self.s[self.i] != q:
                self.i += 1
            self.i += 1
            return self.s[start:self.i]
        while self.i < len(self.s) and self.s[self.i] not in stops:
            self.i += 1
        return self.s[start:self.i]


def _value(text: str) -> Any:
    t = text.strip()
    if t.startswith(("[", "{")):
        try:
            return _Flow(t).value()
        except IndexError as e:
            raise YamlError(f"unterminated flow value: {t!r}") from e
    return _scalar(t)


def _split_key(line: str) -> Tuple[str, str]:
    m = re.match(r"""^("[^"]*"|'[^']*'|[^:#\s][^:]*?)\s*:(?:\s+(.*)|$)""", line)
    if not m:
        raise YamlError(f"expected 'key: value', got {line!r}")
    return _scalar(m.group(1)), (m.group(2) or "")


def loads(text: str) -> Any:
    lines: List[Tuple[int, str]] = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.strip() and line.strip() != "---":
            lines.append((len(line) - len(line.lstrip(" ")), line.strip()))
    if not lines:
        return None
    value, i = _block(lines, 0, lines[0][0])
    if i != len(lines):
        raise YamlError(f"unexpected indentation at {lines[i][1]!r}")
    return value


def _block(lines, i, indent):
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        out = []
        while i < len(lines) and lines[i][0] == indent and (lines[i][1].startswith("- ") or lines[i][1] == "-"):
            rest = lines[i][1][1:].strip()
            if not rest:
                item, i = _block(lines, i + 1, lines[i + 1][0])
            elif re.match(r"""^("[^"]*"|'[^']*'|[^\[{"'][^:]*?)\s*:(\s|$)""", rest) and not rest.startswith(("[", "{")):
                # "- key: value" starts a mapping item; its other keys sit at indent + 2
                sub = [(indent + 2, rest)]
                j = i + 1
                while j < len(lines) and lines[j][0] > indent:
                    sub.append(lines[j])
                    j += 1
                item, k = _block(sub, 0, indent + 2)
                if k != len(sub):
                    raise YamlError(f"bad list item near {rest!r}")
                i = j
            else:
                item, i = _value(rest), i + 1
            out.append(item)
        return out, i
    out = {}
    while i < len(lines) and lines[i][0] == indent:
        key, rest = _split_key(lines[i][1])
        if rest.strip():
            out[key] = _value(rest)
            i += 1
        elif i + 1 < len(lines) and lines[i + 1][0] > indent:
            out[key], i = _block(lines, i + 1, lines[i + 1][0])
        elif i + 1 < len(lines) and lines[i + 1][0] == indent and lines[i + 1][1].startswith("- "):
            out[key], i = _block(lines, i + 1, indent)  # list at the same indent as its key
        else:
            out[key] = None
            i += 1
    return out, i


def load(path) -> Any:
    from pathlib import Path
    return loads(Path(path).read_text(encoding="utf-8"))
