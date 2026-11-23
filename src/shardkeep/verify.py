"""Проверка архивов по манифестам."""

import datetime as dt
import hashlib
import os
import tarfile
from dataclasses import dataclass, field

from shardkeep import layout
from shardkeep.codecs import open_reader
from shardkeep.fsutil import long_path, os_path, write_text
from shardkeep.textfmt import num


@dataclass
class VerifyResult:
    ok: bool = True
    files: int = 0
    problems: list = field(default_factory=list)

    def fail(self, msg):
        self.ok = False
        self.problems.append(msg)


def check_shard(shard):
    """Читает часть целиком и сверяет каждый файл с манифестом. Возвращает (список проблем, число файлов)."""
    expected = layout.read_manifest(shard.manifest)
    seen = set()
    bad = []
    try:
        with open_reader(shard.path) as rd:
            with tarfile.open(fileobj=rd, mode="r|") as tar:
                for m in tar:
                    if not m.isfile():
                        continue
                    h = hashlib.sha256()
                    fo = tar.extractfile(m)
                    while True:
                        b = fo.read(8 << 20)
                        if not b:
                            break
                        h.update(b)
                    exp = expected.get(m.name)
                    if exp is None:
                        bad.append(f"лишний файл {m.name}")
                    elif exp["sha256"] != h.hexdigest() or exp["size"] != m.size:
                        bad.append(f"не совпадает {m.name}")
                    seen.add(m.name)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"архив поврежден: {exc}")
    missing = set(expected) - seen
    if missing:
        bad.append(f"в архиве нет {len(missing)} файлов из манифеста")
    return bad, len(seen), expected


def check_source(src, expected):
    """Число файлов из манифеста, которые в исходной папке изменились или исчезли."""
    src_os = long_path(src)
    changed = 0
    for p, r in expected.items():
        try:
            st = os.stat(os_path(src_os, p))
            if st.st_size != r["size"] or int(st.st_mtime) != r["mtime"]:
                changed += 1
        except OSError:
            changed += 1
    return changed


def verify(out, name=None, src=None, report=True):
    res = VerifyResult()
    for nm in layout.list_names(out, name):
        shards = layout.shard_list(out, nm)
        print(f"Проверка набора {nm}: частей {len(shards)}")
        n_files = 0
        for shard in shards:
            bad, n, expected = check_shard(shard)
            n_files += n
            print(f"  {shard.filename}: {'OK' if not bad else 'ОШИБКА'}, файлов {num(n)}")
            for b in bad[:10]:
                print(f"    {b}")
            if bad:
                res.fail(f"{shard.path}: " + "; ".join(bad[:20]))
            if src:
                changed = check_source(src, expected)
                if changed:
                    msg = f"{shard.filename}: {changed} исходных файлов изменились или исчезли после упаковки"
                    print("    " + msg)
                    res.fail(msg)
        res.files += n_files
        print(f"  проверено файлов: {num(n_files)}")

    if report:
        write_text(os.path.join(out, "verify_report.txt"),
                   f"{dt.datetime.now():%d.%m.%Y %H:%M} {'OK' if res.ok else 'ОШИБКИ'}\n" + "\n".join(res.problems))
    if res.ok:
        print("\nВсе архивы целы и совпадают с манифестами. Исходные данные можно удалять "
              "после загрузки архивов в хранилище.")
    else:
        print("\nЕсть ошибки (см. verify_report.txt). Исходные данные удалять нельзя.")
    return res
