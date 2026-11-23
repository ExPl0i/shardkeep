"""План упаковки: распределение файлов по частям.

Файлы сортируются по папке первого уровня и расширению, чтобы похожие данные
лежали рядом и сжимались лучше. Запись плана: {"s": номер части, "p": путь,
"n": размер, "m": время изменения}.
"""

import gzip
import json
import os


def sort_key(item):
    rel = item[0]
    top = rel.split("/", 1)[0] if "/" in rel else ""
    ext = os.path.splitext(rel)[1].lower()
    return (top, ext, rel)


def make_plan(files, shard_bytes):
    files = sorted(files, key=sort_key)
    plan = []
    shard, acc = 0, 0
    for rel, size, mtime in files:
        if acc > 0 and acc + size > shard_bytes:
            shard += 1
            acc = 0
        plan.append({"s": shard, "p": rel, "n": size, "m": mtime})
        acc += size
    return plan


def save_plan(path, plan):
    tmp = path + ".partial"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as f:
        for e in plan:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def load_plan(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def by_shard(plan):
    n = (max(e["s"] for e in plan) + 1) if plan else 0
    groups = [[] for _ in range(n)]
    for e in plan:
        groups[e["s"]].append(e)
    return groups
