"""Правила отнесения файлов к категориям для shardkeep analyze.

Правила и категории описаны в rules.toml (формат – в комментарии в начале файла). Пользовательский
файл правил проверяется раньше встроенного, его категории добавляются к встроенным или заменяют их.
"""

import fnmatch
import os
import re
import sys
from dataclasses import dataclass, field
from importlib import resources

from shardkeep.errors import UserError
from shardkeep.fsutil import read_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

RULE_KEYS = {"category", "ext", "not_ext", "name", "path", "dir", "not_dir", "min_size", "max_size"}
CATEGORY_KEYS = {"id", "title", "advice", "data"}
NOTICE_KEYS = {"path", "text"}
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?\s*$", re.IGNORECASE)
UNITS = {None: 1, "B": 1, "KB": 2**10, "MB": 2**20, "GB": 2**30, "TB": 2**40}
MULTI_EXT = (".tar.gz", ".tar.xz", ".tar.zst", ".tar.bz2")


def parse_size(v, where):
    if isinstance(v, int):
        return v
    m = SIZE_RE.match(str(v))
    if not m:
        raise UserError(f"{where}: cannot read size {v!r}; use a number of bytes or a value like \"50MB\"")
    return int(float(m.group(1)) * UNITS[m.group(2).upper() if m.group(2) else None])


def _glob_re(patterns):
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{fnmatch.translate(p.lower())})" for p in patterns))


def file_ext(name_lower):
    """Расширение файла для статистики: двойные расширения архивов и форматы TensorFlow учитываются."""
    if "tfrecord" in name_lower:
        return ".tfrecord"
    if "tfevents" in name_lower:
        return ".tfevents"
    for multi in MULTI_EXT:
        if name_lower.endswith(multi):
            return multi
    return os.path.splitext(name_lower)[1]


@dataclass
class Rule:
    category: str
    ext: object = None
    not_ext: object = None
    name: object = None
    path: object = None
    dir: object = None
    not_dir: object = None
    min_size: object = None
    max_size: object = None


@dataclass
class Category:
    id: str
    title: str
    advice: str = ""
    data: bool = False


@dataclass
class Notice:
    path: object
    text: str
    hit: bool = False


@dataclass
class Rules:
    categories: dict = field(default_factory=dict)
    rules: list = field(default_factory=list)
    notices: list = field(default_factory=list)

    ROOT_STATE = (frozenset(), frozenset())

    def dir_state(self, parent, dirname):
        """Состояние папки: какие правила выполнены по условиям dir и not_dir для нее и ее родителей."""
        pos, neg = parent
        lname = dirname.lower()
        add_pos = {i for i, r in enumerate(self.rules) if r.dir is not None and r.dir.match(lname)}
        add_neg = {i for i, r in enumerate(self.rules) if r.not_dir is not None and r.not_dir.match(lname)}
        if not add_pos and not add_neg:
            return parent
        return (pos | add_pos, neg | add_neg)

    def classify(self, name_lower, rel, size, state):
        pos, neg = state
        exts = {file_ext(name_lower), os.path.splitext(name_lower)[1]}
        rel_lower = None
        for i, r in enumerate(self.rules):
            if r.dir is not None and i not in pos:
                continue
            if r.not_dir is not None and i in neg:
                continue
            if r.ext is not None and not (exts & r.ext):
                continue
            if r.not_ext is not None and exts & r.not_ext:
                continue
            if r.min_size is not None and size < r.min_size:
                continue
            if r.max_size is not None and size > r.max_size:
                continue
            if r.name is not None and not r.name.match(name_lower):
                continue
            if r.path is not None:
                rel_lower = rel_lower or rel.lower()
                if not r.path.match(rel_lower):
                    continue
            return r.category
        return "other"

    def check_notices(self, rel):
        low = rel.lower()
        for n in self.notices:
            if not n.hit and n.path.match(low):
                n.hit = True


def _parse(text, where):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise UserError(f"{where}: invalid TOML: {exc}") from exc


def _check_keys(obj, allowed, where):
    extra = set(obj) - allowed
    if extra:
        raise UserError(f"{where}: unknown keys {', '.join(sorted(extra))}; allowed: {', '.join(sorted(allowed))}")


def _rules_from(doc, where):
    cats, rules, notices = [], [], []
    for k, c in enumerate(doc.get("category", [])):
        w = f"{where}, category {k + 1}"
        _check_keys(c, CATEGORY_KEYS, w)
        if "id" not in c or "title" not in c:
            raise UserError(f"{w}: id and title are required")
        cats.append(Category(c["id"], c["title"], c.get("advice", ""), bool(c.get("data", False))))
    for k, r in enumerate(doc.get("rule", [])):
        w = f"{where}, rule {k + 1}"
        _check_keys(r, RULE_KEYS, w)
        if "category" not in r:
            raise UserError(f"{w}: category is required")

        def lst(key):
            v = r.get(key)
            if v is None:
                return None
            if isinstance(v, str):
                v = [v]
            return [str(x) for x in v]

        exts = lst("ext")
        not_exts = lst("not_ext")
        rules.append(Rule(
            category=r["category"],
            ext={e.lower() for e in exts} if exts is not None else None,
            not_ext={e.lower() for e in not_exts} if not_exts is not None else None,
            name=_glob_re(lst("name")), path=_glob_re(lst("path")),
            dir=_glob_re(lst("dir")), not_dir=_glob_re(lst("not_dir")),
            min_size=parse_size(r["min_size"], w) if "min_size" in r else None,
            max_size=parse_size(r["max_size"], w) if "max_size" in r else None,
        ))
    for k, n in enumerate(doc.get("notice", [])):
        w = f"{where}, notice {k + 1}"
        _check_keys(n, NOTICE_KEYS, w)
        paths = n.get("path")
        if not paths or "text" not in n:
            raise UserError(f"{w}: path and text are required")
        notices.append(Notice(_glob_re([paths] if isinstance(paths, str) else paths), n["text"]))
    return cats, rules, notices


def load_rules(user_file=None, use_defaults=True):
    parts = []
    if user_file:
        parts.append(_rules_from(_parse(read_text(user_file), user_file), user_file))
    if use_defaults:
        text = resources.files("shardkeep.analyze").joinpath("rules.toml").read_text(encoding="utf-8")
        parts.append(_rules_from(_parse(text, "built-in rules.toml"), "built-in rules.toml"))
    rs = Rules()
    for cats, _, _ in reversed(parts):  # пользовательские категории заменяют встроенные с тем же id
        for c in cats:
            rs.categories[c.id] = c
    if "other" not in rs.categories:
        rs.categories["other"] = Category("other", "Other", "Check manually.", True)
    for _, rules, notices in parts:
        rs.rules += rules
        rs.notices += notices
    for r in rs.rules:
        if r.category not in rs.categories:
            raise UserError(f"Rule refers to unknown category {r.category!r}; define it with [[category]]")
    return rs
