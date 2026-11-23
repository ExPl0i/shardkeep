"""Упаковка папки в сжатые части tar с манифестами."""

import datetime as dt
import hashlib
import json
import os
import tarfile
import time

from shardkeep import __version__, layout
from shardkeep.codecs import DEFAULT_LEVEL, WINDOW_LOG, CountingWriter, HashingReader, open_writer, pick_codec
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path, read_json, walk_files, write_json
from shardkeep.plan import by_shard, load_plan, make_plan, save_plan
from shardkeep.textfmt import fmt_time, human, num, pct

FORMAT_VERSION = 2
DEFAULT_SHARD_GB = 4.0
TOOL = f"shardkeep {__version__}"


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


class _ReadFailed(Exception):
    """Файл не дочитан после того, как его заголовок попал в tar: часть нужно переписать без него."""

    def __init__(self, path, exc):
        super().__init__(path)
        self.path = path
        self.exc = exc


def pack(src, out, name=None, codec="auto", level=None, threads=0, long_mode=None, shard_gb=None,
         excludes=(), include_env=False):
    """Упаковывает папку src в набор name внутри папки out. Повторный запуск продолжает упаковку.

    Параметры со значением None при продолжении берутся из сохраненных настроек набора.
    """
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        raise UserError(f"Папка не найдена: {src}")
    name = name or os.path.basename(src.rstrip("\\/"))
    os.makedirs(layout.data_dir(out, name), exist_ok=True)
    os.makedirs(layout.manifest_dir(out, name), exist_ok=True)

    plan_path = layout.plan_file(out, name)
    meta_path = layout.settings_file(out, name)
    if os.path.exists(plan_path):
        meta = read_json(meta_path)
        codec, level, long_mode = _resume_settings(meta, name, codec, level, long_mode, shard_gb, excludes,
                                                   include_env)
        meta.setdefault("sessions", []).append({"started": _now(), "level": level, "long": long_mode,
                                                "tool": TOOL})
        write_json(meta_path, meta)
        plan = load_plan(plan_path)
        print(f"Продолжение упаковки по сохраненному плану: {num(len(plan))} файлов")
    else:
        codec = pick_codec(codec)
        level = level if level is not None else DEFAULT_LEVEL[codec]
        long_mode = True if long_mode is None else bool(long_mode)
        shard_gb = shard_gb or DEFAULT_SHARD_GB
        print(f"Обход {src} ...")
        files = walk_files(src, excludes, include_env)
        plan = make_plan(files, int(shard_gb * 2**30))
        meta = {"name": name, "source": src, "codec": codec, "level": level, "long": long_mode,
                "shard_gb": shard_gb, "created": _now(),
                "format": FORMAT_VERSION, "tool": TOOL, "window_log": WINDOW_LOG if long_mode else None,
                "excludes": list(excludes), "include_env": bool(include_env)}
        write_json(meta_path, meta)
        save_plan(plan_path, plan)  # план последним: признак начатого набора

    threads = threads or (os.cpu_count() or 1)
    groups = by_shard(plan)
    total = sum(e["n"] for e in plan)
    print(f"Набор {name}: {num(len(plan))} файлов, {human(total)}, частей: {len(groups)}; "
          f"сжатие {codec} уровня {level}, потоков {threads if codec == 'zstd' else 1}")

    src_os = long_path(src)
    t_start = time.monotonic()
    done_bytes = processed_bytes = 0
    for i, entries in enumerate(groups):
        sf = layout.shard_file(out, name, i, codec)
        mf = layout.manifest_file(out, name, i)
        raw = sum(e["n"] for e in entries)
        if os.path.exists(sf) and os.path.exists(mf):
            done_bytes += raw
            continue
        t0 = time.monotonic()
        params = {"codec": codec, "level": level, "long": long_mode, "threads": threads}
        info = write_shard(src_os, entries, sf, mf, layout.shard_meta_file(out, name, i), params)
        done_bytes += raw
        processed_bytes += raw
        dt_s = time.monotonic() - t0
        speed = raw / dt_s if dt_s > 0 else 0
        overall = processed_bytes / max(time.monotonic() - t_start, 1e-9)
        eta = (total - done_bytes) / overall if overall > 0 else 0
        comp = info["compressed_bytes"]
        skipped = len(info["skipped"])
        print(f"  часть {i + 1}/{len(groups)}: {human(raw)} -> {human(comp)} "
              f"({pct(comp, raw)}), {human(speed)}/с, осталось примерно {fmt_time(eta)}"
              + (f", не удалось прочитать файлов: {skipped}" if skipped else ""))

    return write_summary(out, name)


def _resume_settings(meta, name, codec, level, long_mode, shard_gb, excludes, include_env):
    """Параметры для продолжения упаковки; предупреждает, если они отличаются от сохраненных."""
    saved_codec = meta["codec"]
    if codec not in (None, "auto") and codec != saved_codec:
        raise UserError(f"Набор {name} уже начат с кодеком {saved_codec}; укажите --codec {saved_codec} "
                        "или упакуйте в новый набор")
    saved_level = meta.get("level", DEFAULT_LEVEL[saved_codec])
    saved_long = meta.get("long", True)
    if level is not None and level != saved_level:
        print(f"Внимание: набор {name} начат с уровнем сжатия {saved_level}, оставшиеся части будут сжаты "
              f"с уровнем {level}. Уровень каждой части записывается рядом с ее манифестом.")
    if long_mode is not None and bool(long_mode) != saved_long:
        print(f"Внимание: режим длинных совпадений для оставшихся частей набора {name} "
              f"{'включен' if long_mode else 'отключен'}, а для готовых частей "
              f"{'был включен' if saved_long else 'был отключен'}.")
    if shard_gb and shard_gb != meta.get("shard_gb"):
        print(f"Внимание: размер части {shard_gb} ГБ не применяется: набор {name} продолжается по сохраненному "
              f"плану с размером {meta.get('shard_gb')} ГБ.")
    if (excludes and list(excludes) != meta.get("excludes", [])) or (include_env and not meta.get("include_env")):
        print(f"Внимание: параметры --exclude и --include-env не применяются: набор {name} продолжается "
              "по сохраненному плану.")
    return (saved_codec,
            saved_level if level is None else level,
            saved_long if long_mode is None else bool(long_mode))


def write_shard(src_os, entries, sf, mf, meta_f, params):
    """Пишет одну часть. Файл, чтение которого оборвалось на середине, исключается, и часть пишется заново."""
    skip = {}
    while True:
        try:
            return _write_shard_once(src_os, entries, sf, mf, meta_f, params, skip)
        except _ReadFailed as rf:
            print(f"  чтение {rf.path} оборвалось ({rf.exc}); часть будет записана заново без этого файла")
            skip[rf.path] = str(rf.exc)


def _write_shard_once(src_os, entries, sf, mf, meta_f, params, skip):
    tmp_sf, tmp_mf = sf + ".partial", mf + ".partial"
    file_hash = hashlib.sha256()
    writer = open_writer(tmp_sf, params["codec"], params["level"], params["threads"], params["long"],
                         hasher=file_hash)
    cw = CountingWriter(writer)
    records = []
    skipped = [{"path": p, "error": err} for p, err in skip.items()]
    try:
        tar = tarfile.open(fileobj=cw, mode="w|", format=tarfile.PAX_FORMAT)
        for e in entries:
            if e["p"] in skip:
                continue
            p = os_path(src_os, e["p"])
            try:
                f = open(p, "rb")
            except OSError as exc:
                print(f"  не удалось прочитать {e['p']}: {exc}")
                skipped.append({"path": e["p"], "error": str(exc)})
                continue
            with f:
                st = os.fstat(f.fileno())
                ti = tarfile.TarInfo(e["p"])
                ti.size = st.st_size
                ti.mtime = int(st.st_mtime)
                ti.mode = 0o644
                hr = HashingReader(f, hashlib.sha256())
                try:
                    tar.addfile(ti, hr)
                except OSError as exc:
                    raise _ReadFailed(e["p"], exc) from exc
            records.append({"path": e["p"], "size": st.st_size, "mtime": int(st.st_mtime),
                            "sha256": hr.h.hexdigest()})
        tar.close()
    except BaseException:
        cw.abort()
        raise
    finally:
        writer.close()
    comp = os.path.getsize(tmp_sf)
    with open(tmp_mf, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    info = {"format": FORMAT_VERSION, "tool": TOOL, "created": _now(),
            "file": os.path.basename(sf), "codec": params["codec"], "level": params["level"],
            "long": params["long"], "window_log": WINDOW_LOG if params["long"] else None,
            "threads": params["threads"] if params["codec"] == "zstd" else 1,
            "files": len(records), "raw_bytes": sum(r["size"] for r in records),
            "compressed_bytes": comp, "sha256": file_hash.hexdigest(), "skipped": skipped}
    os.replace(tmp_sf, sf)
    write_json(meta_f, info)
    os.replace(tmp_mf, mf)  # манифест последним: признак готовой части
    return info


def write_summary(out, name):
    """Пересчитывает summary.json набора по готовым частям."""
    meta = read_json(layout.settings_file(out, name)) if os.path.exists(layout.settings_file(out, name)) else {}
    planned = None
    if os.path.exists(layout.plan_file(out, name)):
        plan = load_plan(layout.plan_file(out, name))
        planned = (max(e["s"] for e in plan) + 1) if plan else 0
    files = raw = comp = skipped = 0
    levels = set()
    shards = layout.shard_list(out, name)
    for shard in shards:
        for r in layout.read_manifest(shard.manifest).values():
            files += 1
            raw += r["size"]
        comp += os.path.getsize(shard.path)
        info = layout.read_shard_meta(out, name, shard.index)
        if info:
            levels.add(info["level"])
            skipped += len(info.get("skipped", []))
        elif "level" in meta:
            levels.add(meta["level"])
    s = {"name": name, "codec": meta.get("codec", shards[0].codec if shards else None),
         "level": meta.get("level"), "shards": len(shards), "files": files,
         "raw_bytes": raw, "compressed_bytes": comp, "updated": _now(),
         "format": FORMAT_VERSION, "levels": sorted(levels), "planned_shards": planned,
         "complete": planned is not None and len(shards) == planned, "skipped_files": skipped}
    write_json(layout.summary_file(out, name), s)
    print(f"Итого {name}: {num(files)} файлов, {human(raw)} -> {human(comp)} ({pct(comp, raw)}), "
          f"частей {len(shards)}" + (f" из {planned}" if planned is not None and planned != len(shards) else ""))
    if skipped:
        print(f"Внимание: не удалось прочитать файлов: {skipped}; их нет в архивах (список рядом с манифестами).")
    return s
