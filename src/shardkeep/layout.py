"""Расположение файлов в папке с архивами.

  OUT/data/<набор>/<набор>-NNNNN.tar.zst   части (или .tar.xz)
  OUT/manifest/<набор>/<набор>-NNNNN.jsonl  манифест части: путь, размер, время изменения, SHA-256
  OUT/manifest/<набор>/plan.jsonl.gz        план упаковки
  OUT/manifest/<набор>/pack_settings.json   параметры набора
  OUT/manifest/<набор>/summary.json         сводка

Подробное описание формата: docs/format.md.
"""

import json
import os
from dataclasses import dataclass

from shardkeep.codecs import EXT_BY_CODEC
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


@dataclass
class Shard:
    path: str       # файл части
    manifest: str   # манифест части

    @property
    def filename(self):
        return os.path.basename(self.path)


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
            result.append(Shard(os.path.join(d, fn), mf))
    return result


def read_manifest(path):
    """Записи манифеста части: {путь: запись}."""
    with open(path, encoding="utf-8") as f:
        return {r["path"]: r for r in (json.loads(line) for line in f if line.strip())}
