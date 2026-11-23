"""Упаковка папки в сжатые части tar с манифестами."""

import datetime as dt
import hashlib
import json
import os
import tarfile
import time

from shardkeep import layout
from shardkeep.codecs import DEFAULT_LEVEL, CountingWriter, HashingReader, open_writer, pick_codec
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path, read_json, walk_files, write_json
from shardkeep.plan import by_shard, load_plan, make_plan, save_plan
from shardkeep.textfmt import fmt_time, human, num, pct


def pack(src, out, name=None, codec="auto", level=None, threads=0, long_mode=True, shard_gb=4.0,
         excludes=(), include_env=False):
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        raise UserError(f"Папка не найдена: {src}")
    name = name or os.path.basename(src.rstrip("\\/"))
    codec = pick_codec(codec)
    level = level if level is not None else DEFAULT_LEVEL[codec]
    threads = threads or (os.cpu_count() or 1)
    os.makedirs(layout.data_dir(out, name), exist_ok=True)
    os.makedirs(layout.manifest_dir(out, name), exist_ok=True)

    plan_path = layout.plan_file(out, name)
    meta_path = layout.settings_file(out, name)
    if os.path.exists(plan_path):
        meta = read_json(meta_path)
        if meta["codec"] != codec:
            raise UserError(f"Набор {name} уже начат с кодеком {meta['codec']}; укажите --codec {meta['codec']}")
        plan = load_plan(plan_path)
        print(f"Продолжение упаковки по сохраненному плану: {num(len(plan))} файлов")
    else:
        print(f"Обход {src} ...")
        files = walk_files(src, excludes, include_env)
        plan = make_plan(files, int(shard_gb * 2**30))
        meta = {"name": name, "source": src, "codec": codec, "level": level, "long": bool(long_mode),
                "shard_gb": shard_gb, "created": dt.datetime.now().isoformat(timespec="seconds")}
        write_json(meta_path, meta)
        save_plan(plan_path, plan)  # план последним: признак начатого набора

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
        comp, missing = _write_shard(src_os, entries, sf, mf, codec, level, threads, long_mode)
        done_bytes += raw
        processed_bytes += raw
        dt_s = time.monotonic() - t0
        speed = raw / dt_s if dt_s > 0 else 0
        overall = processed_bytes / max(time.monotonic() - t_start, 1e-9)
        eta = (total - done_bytes) / overall if overall > 0 else 0
        print(f"  часть {i + 1}/{len(groups)}: {human(raw)} -> {human(comp)} "
              f"({pct(comp, raw)}), {human(speed)}/с, осталось примерно {fmt_time(eta)}"
              + (f", пропущено файлов: {missing}" if missing else ""))

    return write_summary(out, name, codec, level)


def _write_shard(src_os, entries, sf, mf, codec, level, threads, long_mode):
    tmp_sf, tmp_mf = sf + ".partial", mf + ".partial"
    writer = open_writer(tmp_sf, codec, level, threads, long_mode)
    cw = CountingWriter(writer)
    records, missing = [], 0
    try:
        tar = tarfile.open(fileobj=cw, mode="w|", format=tarfile.PAX_FORMAT)
        for e in entries:
            p = os_path(src_os, e["p"])
            try:
                with open(p, "rb") as f:
                    st = os.fstat(f.fileno())
                    ti = tarfile.TarInfo(e["p"])
                    ti.size = st.st_size
                    ti.mtime = int(st.st_mtime)
                    ti.mode = 0o644
                    hr = HashingReader(f, hashlib.sha256())
                    tar.addfile(ti, hr)
            except OSError as exc:
                missing += 1
                print(f"\n  не удалось прочитать {e['p']}: {exc}")
                continue
            records.append({"path": e["p"], "size": st.st_size, "mtime": int(st.st_mtime),
                            "sha256": hr.h.hexdigest()})
        tar.close()
    finally:
        writer.close()
    comp = os.path.getsize(tmp_sf)
    with open(tmp_mf, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(_json_line(r))
    os.replace(tmp_sf, sf)
    os.replace(tmp_mf, mf)  # манифест последним: признак готовой части
    return comp, missing


def _json_line(obj):
    return json.dumps(obj, ensure_ascii=False) + "\n"


def write_summary(out, name, codec, level):
    files = raw = comp = shards = 0
    i = 0
    while True:
        mf = layout.manifest_file(out, name, i)
        if not os.path.exists(mf):
            break
        for r in layout.read_manifest(mf).values():
            files += 1
            raw += r["size"]
        comp += os.path.getsize(layout.shard_file(out, name, i, codec))
        shards += 1
        i += 1
    s = {"name": name, "codec": codec, "level": level, "shards": shards, "files": files,
         "raw_bytes": raw, "compressed_bytes": comp,
         "updated": dt.datetime.now().isoformat(timespec="seconds")}
    write_json(layout.summary_file(out, name), s)
    print(f"Итого {name}: {num(files)} файлов, {human(raw)} -> {human(comp)} ({pct(comp, raw)}), частей {shards}")
    return s
