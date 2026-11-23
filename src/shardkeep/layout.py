"""Расположение файлов в папке с архивами.

  OUT/data/<набор>/<набор>-NNNNN.tar.zst   части (или .tar.xz)
  OUT/manifest/<набор>/<набор>-NNNNN.jsonl  манифест части: путь, размер, время изменения, SHA-256
  OUT/manifest/<набор>/<набор>-NNNNN.meta.json  параметры части (с формата 2)
  OUT/manifest/<набор>/plan.jsonl.gz        план упаковки
  OUT/manifest/<набор>/pack_settings.json   параметры набора
  OUT/manifest/<набор>/summary.json         сводка

Подробное описание формата: docs/format.md.
"""

import json
import os
import re
from dataclasses import dataclass, field

from shardkeep.codecs import EXT_BY_CODEC, codec_of
from shardkeep.errors import UserError


def data_dir(out, name):
    return os.path.join(out, "data", name)


def manifest_dir(out, name):
    return os.path.join(out, "manifest", name)


def shard_base(name, i):
    return f"{name}-{i:05d}"


def shard_file(out, name, i, codec):
    return os.path.join(data_dir(out, name), shard_base(name, i) + EXT_BY_CODEC[codec])


def manifest_file(out, name, i):
    return os.path.join(manifest_dir(out, name), shard_base(name, i) + ".jsonl")


def shard_meta_file(out, name, i):
    return os.path.join(manifest_dir(out, name), shard_base(name, i) + ".meta.json")


def read_shard_meta(out, name, i):
    """Параметры части или None для частей формата 1."""
    p = shard_meta_file(out, name, i)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def plan_file(out, name):
    return os.path.join(manifest_dir(out, name), "plan.jsonl.gz")


def settings_file(out, name):
    return os.path.join(manifest_dir(out, name), "pack_settings.json")


def summary_file(out, name):
    return os.path.join(manifest_dir(out, name), "summary.json")


def list_names(out, name=None):
    """Имена наборов в папке с архивами."""
    if name:
        return [name]
    root = os.path.join(out, "manifest")
    if not os.path.isdir(root):
        raise UserError(f"В папке {out} нет архивов shardkeep (не найдена папка manifest)")
    return sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))


SHARD_RE = re.compile(r"^(?P<name>.+)-(?P<i>\d{5,})\.tar\.(?P<ext>zst|xz)$")
MANIFEST_RE = re.compile(r"^(?P<name>.+)-(?P<i>\d{5,})\.jsonl$")


@dataclass
class Shard:
    path: str       # файл части
    manifest: str   # манифест части
    index: int = -1

    @property
    def filename(self):
        return os.path.basename(self.path)

    @property
    def codec(self):
        return codec_of(self.path)


def shard_list(out, name):
    """Готовые части набора: файлы в data/<набор>, для которых есть манифест."""
    d = data_dir(out, name)
    if not os.path.isdir(d):
        return []
    result = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".partial"):
            continue
        base = fn.split(".tar.")[0]
        mf = os.path.join(manifest_dir(out, name), base + ".jsonl")
        if os.path.exists(mf):
            m = SHARD_RE.match(fn)
            result.append(Shard(os.path.join(d, fn), mf, int(m.group("i")) if m else -1))
    return result


@dataclass
class SetState:
    """Состояние набора на диске: готовые части и все, что мешает считать набор полным."""
    name: str
    shards: list
    planned: object = None          # число частей по плану или None, если плана нет
    no_data: list = field(default_factory=list)       # манифест есть, файла части нет
    no_manifest: list = field(default_factory=list)   # файл части есть, манифеста нет (часть не дописана)
    partial: list = field(default_factory=list)       # временные файлы .partial

    @property
    def missing_indexes(self):
        if self.planned is None:
            return []
        have = {s.index for s in self.shards}
        return [i for i in range(self.planned) if i not in have]


def scan_set(out, name, planned=None):
    shards = shard_list(out, name)
    st = SetState(name, shards, planned)
    have_data = {s.manifest for s in shards}
    md = manifest_dir(out, name)
    if os.path.isdir(md):
        for fn in sorted(os.listdir(md)):
            if MANIFEST_RE.match(fn) and os.path.join(md, fn) not in have_data:
                st.no_data.append(fn)
    d = data_dir(out, name)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".partial"):
                st.partial.append(fn)
            elif SHARD_RE.match(fn) and not os.path.exists(os.path.join(md, fn.split(".tar.")[0] + ".jsonl")):
                st.no_manifest.append(fn)
    return st


def read_manifest(path):
    """Записи манифеста части: {путь: запись}."""
    with open(path, encoding="utf-8") as f:
        return {r["path"]: r for r in (json.loads(line) for line in f if line.strip())}
