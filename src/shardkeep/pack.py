"""Упаковка папки в сжатые части tar с манифестами."""

import datetime as dt
import hashlib
import json
import os
import tarfile

from shardkeep import __version__, jxl, layout
from shardkeep.codecs import DEFAULT_LEVEL, WINDOW_LOG, CountingWriter, HashingReader, open_writer, pick_codec
from shardkeep.errors import UserError
from shardkeep.fsutil import long_path, os_path, read_json, walk_files, write_json
from shardkeep.plan import by_shard, load_plan, make_plan, save_plan
from shardkeep.progress import Progress, echo
from shardkeep.profiles import DEFAULT_PROFILE, LOW, PROFILES, choose_threads, classify_extensions, describe_classes
from shardkeep.textfmt import human, num, pct

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
         excludes=(), include_env=False, profile=None, use_jxl=None):
    """Упаковывает папку src в набор name внутри папки out. Повторный запуск продолжает упаковку.

    use_jxl – переводить JPEG в JPEG XL без потерь (нужны cjxl и djxl, см. shardkeep.jxl).
    profile – профиль сжатия (см. shardkeep.profiles.PROFILES); level задает уровень для хорошо
    сжимаемых файлов. Параметры со значением None при продолжении берутся из сохраненных настроек набора.
    """
    src = os.path.abspath(src)
    if not os.path.isdir(src):
        raise UserError(f"Folder not found: {src}")
    if profile is not None and profile not in PROFILES:
        raise UserError(f"Unknown compression profile: {profile}. Available: {', '.join(PROFILES)}")
    name = name or os.path.basename(src.rstrip("\\/"))
    os.makedirs(layout.data_dir(out, name), exist_ok=True)
    os.makedirs(layout.manifest_dir(out, name), exist_ok=True)

    plan_path = layout.plan_file(out, name)
    meta_path = layout.settings_file(out, name)
    if os.path.exists(plan_path):
        meta = read_json(meta_path)
        codec, level, long_mode = _resume_settings(meta, name, codec, level, long_mode, shard_gb, excludes,
                                                   include_env, profile)
        level_low = meta.get("level_low")
        if use_jxl is not None and bool(use_jxl) != bool(meta.get("jxl")):
            print(f"Warning: --jxl is ignored: set {name} continues with the saved setting "
                  f"(JPEG XL {'on' if meta.get('jxl') else 'off'}).")
        use_jxl = bool(meta.get("jxl"))
        meta.setdefault("sessions", []).append({"started": _now(), "level": level, "long": long_mode,
                                                "tool": TOOL})
        write_json(meta_path, meta)
        plan = load_plan(plan_path)
        print(f"Resuming from the saved plan: {num(len(plan))} files")
    else:
        prof = PROFILES[profile or DEFAULT_PROFILE]
        codec = pick_codec(codec)
        high, level_low = prof.levels(codec)
        level = high if level is None else level
        long_mode = prof.long if long_mode is None else bool(long_mode)
        shard_gb = shard_gb or DEFAULT_SHARD_GB
        use_jxl = bool(use_jxl)
        if use_jxl:
            jxl.require_tools()
        print(f"Scanning {src} ...")
        with Progress("scanning", unit="files") as bar:
            files = walk_files(src, excludes, include_env, on_file=lambda size: bar.update())
        classes = None
        if prof.classify and level_low is not None:
            classes, stats = classify_extensions(long_path(src), files, prof.classify)
            for line in describe_classes(stats, classes):
                print(line)
            if not any(c == LOW for c in classes.values()):
                classes, level_low = None, None
        else:
            level_low = None
        plan = make_plan(files, int(shard_gb * 2**30), classes)
        meta = {"name": name, "source": src, "codec": codec, "level": level, "long": long_mode,
                "shard_gb": shard_gb, "created": _now(),
                "format": FORMAT_VERSION, "tool": TOOL, "profile": prof.name, "level_low": level_low,
                "window_log": WINDOW_LOG if long_mode else None,
                "excludes": list(excludes), "include_env": bool(include_env), "jxl": use_jxl}
        write_json(meta_path, meta)
        save_plan(plan_path, plan)  # план последним: признак начатого набора

    groups = by_shard(plan)
    total = sum(e["n"] for e in plan)
    thread_cache = {}
    jxl_tools = None
    if use_jxl:
        jxl_tools = jxl.require_tools()
        print(f"JPEG XL: JPEG files are converted losslessly ({jxl.tool_version(jxl_tools[0]) or 'cjxl'})")

    def shard_params(entries):
        low_bytes = sum(e["n"] for e in entries if e.get("c") == LOW)
        is_low = level_low is not None and low_bytes * 2 > sum(e["n"] for e in entries)
        lvl, lng = (level_low, False) if is_low else (level, long_mode)
        if (lvl, lng) not in thread_cache:
            n, note = choose_threads(codec, lvl, lng, threads)
            thread_cache[(lvl, lng)] = n
            if note:
                echo(note)
        return {"codec": codec, "level": lvl, "long": lng, "threads": thread_cache[(lvl, lng)],
                "jxl_tools": jxl_tools, "workdir": layout.data_dir(out, name)}

    levels_txt = f"level {level}" + (f", already compressed files level {level_low}" if level_low is not None else "")
    print(f"Set {name}: {num(len(plan))} files, {human(total)}, {len(groups)} shards; "
          f"{codec} {levels_txt}" + (", long-distance matching" if long_mode and codec == "zstd" else ""))

    src_os = long_path(src)
    done = [os.path.exists(layout.shard_file(out, name, i, codec)) and os.path.exists(layout.manifest_file(out, name, i))
            for i in range(len(groups))]
    done_bytes = sum(e["n"] for i, entries in enumerate(groups) if done[i] for e in entries)
    with Progress(f"pack {name}", total=total, initial=done_bytes) as bar:
        for i, entries in enumerate(groups):
            raw = sum(e["n"] for e in entries)
            if done[i]:
                continue
            sf = layout.shard_file(out, name, i, codec)
            mf = layout.manifest_file(out, name, i)
            bar.set_postfix(f"shard {i + 1}/{len(groups)}")
            params = shard_params(entries)
            started = bar.n
            info = write_shard(src_os, entries, sf, mf, layout.shard_meta_file(out, name, i), params, bar)
            bar.set(started + raw)  # точное значение, даже если файлы изменились после составления плана
            comp = info["compressed_bytes"]
            skipped = len(info["skipped"])
            echo(f"  shard {i + 1}/{len(groups)}: {human(raw)} -> {human(comp)} ({pct(comp, raw)}), "
                 f"level {params['level']}, {params['threads']} threads"
                 + (f", unreadable files: {skipped}" if skipped else ""))

    return write_summary(out, name)


def _resume_settings(meta, name, codec, level, long_mode, shard_gb, excludes, include_env, profile=None):
    """Параметры для продолжения упаковки; предупреждает, если они отличаются от сохраненных."""
    saved_codec = meta["codec"]
    if codec not in (None, "auto") and codec != saved_codec:
        raise UserError(f"Set {name} was started with codec {saved_codec}; use --codec {saved_codec} "
                        "or pack into a new set")
    saved_level = meta.get("level", DEFAULT_LEVEL[saved_codec])
    saved_long = meta.get("long", True)
    if profile is not None and profile != meta.get("profile"):
        print(f"Warning: profile {profile} is ignored: set {name} continues from its saved plan"
              + (f" with profile {meta['profile']}." if meta.get("profile") else "."))
    if level is not None and level != saved_level:
        print(f"Warning: set {name} was started with compression level {saved_level}; the remaining shards "
              f"will use level {level}. The level of every shard is recorded next to its manifest.")
    if long_mode is not None and bool(long_mode) != saved_long:
        print(f"Warning: long-distance matching is {'on' if long_mode else 'off'} for the remaining shards "
              f"of set {name}, but was {'on' if saved_long else 'off'} for the finished ones.")
    if shard_gb and shard_gb != meta.get("shard_gb"):
        print(f"Warning: shard size {shard_gb} GB is ignored: set {name} continues from its saved plan "
              f"with {meta.get('shard_gb')} GB shards.")
    if (excludes and list(excludes) != meta.get("excludes", [])) or (include_env and not meta.get("include_env")):
        print(f"Warning: --exclude and --include-env are ignored: set {name} continues "
              "from its saved plan.")
    return (saved_codec,
            saved_level if level is None else level,
            saved_long if long_mode is None else bool(long_mode))


def write_shard(src_os, entries, sf, mf, meta_f, params, bar=None):
    """Пишет одну часть. Файл, чтение которого оборвалось на середине, исключается, и часть пишется заново."""
    skip = {}
    started = bar.n if bar else 0
    while True:
        try:
            return _write_shard_once(src_os, entries, sf, mf, meta_f, params, skip, bar)
        except _ReadFailed as rf:
            echo(f"  reading {rf.path} failed midway ({rf.exc}); rewriting the shard without this file")
            skip[rf.path] = str(rf.exc)
            if bar:
                bar.set(started)


def _write_shard_once(src_os, entries, sf, mf, meta_f, params, skip, bar=None):
    tmp_sf, tmp_mf = sf + ".partial", mf + ".partial"
    file_hash = hashlib.sha256()
    on_read = bar.update if bar else None
    writer = open_writer(tmp_sf, params["codec"], params["level"], params["threads"], params["long"],
                         hasher=file_hash)
    if bar is not None and writer.consumed() is not None:
        # zstandard: прогресс по сжатым байтам, которые компрессор сообщает фоновому потоку строки прогресса
        on_read = None
        bar.track(writer.consumed, base=bar.n, limit=bar.n + sum(e["n"] for e in entries))
    cw = CountingWriter(writer)
    records = []
    skipped = [{"path": p, "error": err} for p, err in skip.items()]
    jxl_map = {}
    batch = None
    try:
        if params.get("jxl_tools"):
            batch = jxl.Batch(params["jxl_tools"], src_os,
                              [e["p"] for e in entries if jxl.is_jpeg(e["p"]) and e["p"] not in skip],
                              params["workdir"], on_file=bar.update if bar else None)
            if bar is not None and on_read is None:
                bar.track(writer.consumed, base=bar.n, limit=bar._source[2] if bar._source else None)
        tar = tarfile.open(fileobj=cw, mode="w|", format=tarfile.PAX_FORMAT)
        for e in entries:
            if e["p"] in skip:
                continue
            conv = batch.done.get(e["p"]) if batch else None
            if conv is not None:
                member = e["p"] + jxl.MEMBER_SUFFIX
                ti = tarfile.TarInfo(member)
                ti.size = conv.size
                ti.mtime = conv.mtime
                ti.mode = 0o644
                with open(conv.jxl_path, "rb") as f:
                    hr = HashingReader(f, hashlib.sha256())
                    tar.addfile(ti, hr)
                jxl_map[e["p"]] = {"member": member, "size": conv.size, "sha256": hr.h.hexdigest()}
                records.append({"path": e["p"], "size": conv.orig_size, "mtime": conv.mtime,
                                "sha256": conv.orig_sha256})
                continue
            p = os_path(src_os, e["p"])
            try:
                f = open(p, "rb")
            except OSError as exc:
                echo(f"  cannot read {e['p']}: {exc}")
                skipped.append({"path": e["p"], "error": str(exc)})
                continue
            with f:
                st = os.fstat(f.fileno())
                ti = tarfile.TarInfo(e["p"])
                ti.size = st.st_size
                ti.mtime = int(st.st_mtime)
                ti.mode = 0o644
                hr = HashingReader(f, hashlib.sha256(), on_read)
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
        # Последнее задание zstd сжимается внутри close(): прогресс отслеживается до конца
        writer.close()
        if bar is not None:
            bar.track(None)
        if batch:
            batch.close()
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
    if batch:
        info["jxl"] = jxl_map
        info["jxl_fallback"] = len(batch.fallback)
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
    files = raw = comp = skipped = jxl_files = jxl_saved = 0
    levels = set()
    shards = layout.shard_list(out, name)
    for shard in shards:
        manifest = layout.read_manifest(shard.manifest)
        for r in manifest.values():
            files += 1
            raw += r["size"]
        comp += os.path.getsize(shard.path)
        info = layout.read_shard_meta(out, name, shard.index)
        if info:
            levels.add(info["level"])
            skipped += len(info.get("skipped", []))
            for path, v in (info.get("jxl") or {}).items():
                jxl_files += 1
                jxl_saved += manifest[path]["size"] - v["size"] if path in manifest else 0
        elif "level" in meta:
            levels.add(meta["level"])
    s = {"name": name, "codec": meta.get("codec", shards[0].codec if shards else None),
         "level": meta.get("level"), "shards": len(shards), "files": files,
         "raw_bytes": raw, "compressed_bytes": comp, "updated": _now(),
         "format": FORMAT_VERSION, "levels": sorted(levels), "planned_shards": planned,
         "complete": planned is not None and len(shards) == planned, "skipped_files": skipped}
    if meta.get("jxl") or jxl_files:
        s["jxl_files"] = jxl_files
        s["jxl_saved_bytes"] = jxl_saved
    write_json(layout.summary_file(out, name), s)
    print(f"Total {name}: {num(files)} files, {human(raw)} -> {human(comp)} ({pct(comp, raw)}), "
          f"{len(shards)} shards" + (f" of {planned}" if planned is not None and planned != len(shards) else ""))
    if jxl_files:
        print(f"JPEG XL: {num(jxl_files)} JPEG files converted, {human(jxl_saved)} saved before compression")
    if skipped:
        print(f"Warning: {skipped} files could not be read and are missing from the archives "
              "(listed in the .meta.json files next to the manifests).")
    return s
