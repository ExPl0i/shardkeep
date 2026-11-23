"""План упаковки: распределение файлов по частям.

Файлы сортируются по папке первого уровня и расширению, чтобы похожие данные
лежали рядом и сжимались лучше. Запись плана: {"s": номер части, "p": путь,
"n": размер, "m": время изменения}; у уже сжатых файлов также "c": "low".
"""

import gzip
import json
import os


def sort_key(item):
    rel = item[0]
    top = rel.split("/", 1)[0] if "/" in rel else ""
    ext = os.path.splitext(rel)[1].lower()
    return (top, ext, rel)


def make_plan(files, shard_bytes, classes=None):
    """Распределяет файлы по частям.

    classes – {расширение: "high" | "low"}: если задано, уже сжатые файлы (класс "low") собираются
    в отдельные части, а их записи в плане получают поле "c": "low".
    """
    def cls_of(rel):
        return classes.get(os.path.splitext(rel)[1].lower(), "high") if classes else None

    if classes:
        def key(item):
            return (cls_of(item[0]) == "low",) + sort_key(item)
    else:
        key = sort_key
    plan = []
    shard, acc, prev_cls = 0, 0, None
    for rel, size, mtime in sorted(files, key=key):
        cls = cls_of(rel)
        if acc > 0 and (acc + size > shard_bytes or cls != prev_cls):
            shard += 1
            acc = 0
        e = {"s": shard, "p": rel, "n": size, "m": mtime}
        if cls == "low":
            e["c"] = "low"
        plan.append(e)
        acc += size
        prev_cls = cls
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
