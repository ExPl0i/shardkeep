"""Проверка архивов по манифестам."""

import datetime as dt
import hashlib
import os
import tarfile
from dataclasses import dataclass, field

from shardkeep import layout
from shardkeep.codecs import drain, open_reader
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path, read_json, walk_files, write_text
from shardkeep.plan import load_plan
from shardkeep.textfmt import num


@dataclass
class VerifyResult:
    ok: bool = True
    files: int = 0
    shards: int = 0
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def fail(self, msg):
        self.ok = False
        self.problems.append(msg)


def check_shard(shard, info=None):
    """Читает часть целиком и сверяет каждый файл с манифестом.

    info – параметры части (формат 2): дополнительно сверяются размер и SHA-256 самого файла части.
    Возвращает (список проблем, число файлов, записи манифеста).
    """
    expected = layout.read_manifest(shard.manifest)
    seen = set()
    bad = []
    file_hash = hashlib.sha256()
    try:
        with open_reader(shard.path, hasher=file_hash) as rd:
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
            drain(rd)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"архив поврежден: {exc}")
    else:
        if info and info.get("sha256"):
            if os.path.getsize(shard.path) != info.get("compressed_bytes") or file_hash.hexdigest() != info["sha256"]:
                bad.append("файл части отличается от записанного при упаковке (размер или SHA-256)")
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


def _load_settings(out, name):
    p = layout.settings_file(out, name)
    return read_json(p) if os.path.exists(p) else {}


def _planned(out, name):
    p = layout.plan_file(out, name)
    if not os.path.exists(p):
        return None
    plan = load_plan(p)
    return (max(e["s"] for e in plan) + 1) if plan else 0


def _sets_for_src(out, names, src):
    """Наборы, которые сверяются с исходной папкой src."""
    if len(names) == 1:
        return set(names)
    src_abs = os.path.normcase(os.path.abspath(src))
    match = {n for n in names
             if os.path.normcase(os.path.abspath(_load_settings(out, n).get("source", ""))) == src_abs}
    if not match:
        raise UserError("В папке несколько наборов, и ни один не упакован из указанной исходной папки. "
                        "Укажите набор параметром --name.")
    return match


def verify(out, name=None, src=None, report=True, allow_skipped=False):
    res = VerifyResult()
    names = layout.list_names(out, name)
    src_sets = _sets_for_src(out, names, src) if src else set()
    for nm in names:
        settings = _load_settings(out, nm)
        state = layout.scan_set(out, nm, _planned(out, nm))
        print(f"Проверка набора {nm}: частей {len(state.shards)}"
              + (f" из {state.planned}" if state.planned is not None else ""))
        _check_completeness(res, state)
        n_files = 0
        archived = set()
        skipped_paths = set()
        for shard in state.shards:
            info = layout.read_shard_meta(out, nm, shard.index)
            bad, n, expected = check_shard(shard, info)
            skipped = (info or {}).get("skipped", [])
            n_files += n
            archived.update(expected)
            skipped_paths.update(x["path"] for x in skipped)
            print(f"  {shard.filename}: {'OK' if not bad else 'ОШИБКА'}, файлов {num(n)}"
                  + (f", не прочитано при упаковке: {len(skipped)}" if skipped else ""))
            for b in bad[:10]:
                print(f"    {b}")
            if bad:
                res.fail(f"{shard.path}: " + "; ".join(bad[:20]))
            if skipped:
                msg = (f"{shard.filename}: {len(skipped)} файлов не удалось прочитать при упаковке, их нет в архиве: "
                       + ", ".join(s["path"] for s in skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
                print("    " + msg)
                if allow_skipped:
                    res.warnings.append(msg)
                else:
                    res.fail(msg)
            if nm in src_sets:
                changed = check_source(src, expected)
                if changed:
                    msg = f"{shard.filename}: {changed} исходных файлов изменились или исчезли после упаковки"
                    print("    " + msg)
                    res.fail(msg)
        if nm in src_sets:
            _check_new_files(res, src, settings, archived | skipped_paths, state)
        res.files += n_files
        res.shards += len(state.shards)
        print(f"  проверено файлов: {num(n_files)}")

    if report:
        lines = [f"{dt.datetime.now():%d.%m.%Y %H:%M} {'OK' if res.ok else 'ОШИБКИ'}"] + res.problems
        if res.warnings:
            lines += ["", "Предупреждения:"] + res.warnings
        write_text(os.path.join(out, "verify_report.txt"), "\n".join(lines) + "\n")
    if res.ok:
        print("\nВсе архивы целы и совпадают с манифестами. Исходные данные можно удалять "
              "после загрузки архивов в хранилище.")
    else:
        print("\nЕсть ошибки (см. verify_report.txt). Исходные данные удалять нельзя.")
    return res


def _check_completeness(res, state):
    if state.missing_indexes:
        msg = (f"набор {state.name}: упаковка не завершена, готово частей {len(state.shards)} из {state.planned}; "
               "запустите pack повторно с теми же параметрами")
        print("  " + msg)
        res.fail(msg)
    for fn in state.no_data:
        msg = f"набор {state.name}: есть манифест {fn}, но нет файла части"
        print("  " + msg)
        res.fail(msg)
    for fn in state.no_manifest:
        msg = f"набор {state.name}: часть {fn} не дописана (нет манифеста)"
        print("  " + msg)
        res.fail(msg)
    if state.partial:
        res.warnings.append(f"набор {state.name}: временные файлы незавершенной упаковки: " + ", ".join(state.partial))


def _check_new_files(res, src, settings, known, state):
    """Ищет в исходной папке файлы, которых нет ни в одном манифесте набора."""
    if state.planned is None or state.missing_indexes:
        return  # набор не полный: об этом уже сообщено
    files = walk_files(src, settings.get("excludes", []), settings.get("include_env", False),
                       on_error=lambda msg: None)
    new = sorted(rel for rel, _, _ in files if rel not in known)
    if new:
        msg = (f"набор {state.name}: в исходной папке {num(len(new))} файлов, которых нет в архивах: "
               + ", ".join(new[:5]) + (" ..." if len(new) > 5 else ""))
        print("  " + msg)
        res.fail(msg)
