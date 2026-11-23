#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Копия исходной версии pack_shards.py (до пакета shardkeep), очищенная от путей и имен
# конкретного проекта. Логика упаковки и формат архивов не изменены.
# Тесты используют ее, чтобы создавать архивы старого формата и проверять обратную совместимость.
# Не редактировать.
"""
pack_shards.py – упаковка папки с данными в сжатые шарды (tar + zstd) для загрузки
в Hugging Face или облачное хранилище, проверка архивов и восстановление.

Зачем шарды: сотни тысяч мелких файлов нельзя нормально загрузить ни в Hugging Face
(не более 10 000 файлов в одной папке), ни в облако (каждый файл передается отдельным
запросом). Шард – это архив примерно одинакового размера (по умолчанию 4 ГБ исходных данных).
Файлы внутри шардов сгруппированы по папке и расширению, поэтому похожие данные
лежат рядом и сжимаются лучше.

Для каждого шарда пишется манифест: путь, размер, время изменения и SHA-256 каждого файла.
Упаковку можно прервать и запустить повторно – готовые шарды пропускаются.

Требования: Python 3.8+. Для zstd нужен пакет zstandard (pip install zstandard)
или Python 3.14+ (модуль compression.zstd). Без них используется xz – сжимает
так же сильно, но работает в несколько раз медленнее и в один поток.

Команды:
  Упаковать папку:
    python pack_shards.py pack "D:\\data\\set1" "E:\\backup\\dataset" --name set1

  Проверить архивы (обязательно перед удалением исходных данных):
    python pack_shards.py verify "E:\\backup\\dataset" --src "D:\\data\\set1" --name set1

  Восстановить (все наборы или только --name, можно выбрать файлы шаблоном --pattern):
    python pack_shards.py unpack "E:\\backup\\dataset" "D:\\restore" --name set1

  Пересоздать карточку датасета Hugging Face и фрагмент README для GitHub:
    python pack_shards.py card "E:\\backup\\dataset" --hf-repo user/dataset --github-url https://github.com/user/repo --waymo

Структура результата:
  OUT/README.md                         карточка датасета Hugging Face
  OUT/GITHUB_README_SNIPPET.md          фрагмент для README репозитория GitHub
  OUT/pack_shards.py                    копия этого скрипта (для восстановления)
  OUT/data/<name>/<name>-00000.tar.zst  шарды
  OUT/manifest/<name>/...               план, манифесты шардов, сводка
"""

import argparse
import datetime as dt
import fnmatch
import gzip
import hashlib
import io
import json
import lzma
import os
import shutil
import sys
import tarfile
import time

try:
    from compression import zstd as _stdzstd  # Python 3.14+
except Exception:  # noqa: BLE001
    _stdzstd = None
try:
    import zstandard as _zstd
except Exception:  # noqa: BLE001
    _zstd = None

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ENV_DIR_NAMES = {"__pycache__", ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", "venv", ".venv",
                 "node_modules", ".git"}
EXT_BY_CODEC = {"zstd": ".tar.zst", "xz": ".tar.xz"}
WINDOW_LOG = 27  # окно 128 МБ: распаковывается любым zstd без дополнительных флагов


# ----------------------------------------------------------------------------
def human(n):
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024 or unit == "ТБ":
            return f"{int(n)} Б" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def num(n):
    return f"{n:,}".replace(",", " ")


def pct(part, total):
    return f"{100 * part / total if total else 0:.1f} %".replace(".", ",")


def long_path(p):
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)
    return p


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600} ч {sec % 3600 // 60:02d} мин" if sec >= 3600 else f"{sec // 60} мин {sec % 60:02d} с"


# ----------------------------------------------------------------------------
# Сжатие
# ----------------------------------------------------------------------------
def pick_codec(requested):
    if requested == "auto":
        return "zstd" if (_stdzstd or _zstd) else "xz"
    if requested == "zstd" and not (_stdzstd or _zstd):
        sys.exit("Для zstd установите пакет: pip install zstandard (или используйте --codec xz)")
    return requested


def open_writer(path, codec, level, threads, long_mode):
    """Открывает файл для потоковой записи со сжатием. Возвращает объект с write() и close()."""
    if codec == "zstd":
        if _zstd is not None:
            kw = {"threads": threads, "write_checksum": True}
            if long_mode:
                kw.update(enable_ldm=True, window_log=WINDOW_LOG)
            params = _zstd.ZstdCompressionParameters.from_level(level, **kw)
            fh = open(path, "wb")
            return _zstd.ZstdCompressor(compression_params=params).stream_writer(fh, closefd=True)
        P = _stdzstd.CompressionParameter
        opts = {P.compression_level: level, P.checksum_flag: 1}
        if threads > 1:
            opts[P.nb_workers] = threads
        if long_mode:
            opts[P.enable_long_distance_matching] = 1
            opts[P.window_log] = WINDOW_LOG
        return _stdzstd.ZstdFile(path, "wb", options=opts)
    preset = min(9, max(0, level))
    if level >= 10:
        preset = 9 | lzma.PRESET_EXTREME
    return lzma.open(path, "wb", preset=preset)


def open_reader(path):
    if path.endswith(".zst"):
        if _zstd is not None:
            fh = open(path, "rb")
            return _zstd.ZstdDecompressor(max_window_size=2**31).stream_reader(fh, read_across_frames=True,
                                                                              closefd=True)
        if _stdzstd is not None:
            return _stdzstd.ZstdFile(path, "rb")
        sys.exit("Для распаковки .zst установите пакет: pip install zstandard")
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    return open(path, "rb")


class HashingReader:
    """Считает SHA-256 файла во время чтения его архиватором."""

    def __init__(self, f):
        self.f = f
        self.h = hashlib.sha256()
        self.n = 0

    def read(self, n=-1):
        b = self.f.read(n)
        self.h.update(b)
        self.n += len(b)
        return b


class CountingWriter:
    def __init__(self, w):
        self.w = w
        self.n = 0

    def write(self, b):
        self.n += len(b)
        return self.w.write(b)

    def flush(self):
        if hasattr(self.w, "flush"):
            self.w.flush()


# ----------------------------------------------------------------------------
# План
# ----------------------------------------------------------------------------
def walk_files(src, excludes, include_env):
    root = long_path(src)
    out = []
    stack = [(root, ())]
    while stack:
        path, parts = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as exc:
            print(f"  пропущено {'/'.join(parts) or '.'}: {exc}")
            continue
        for e in entries:
            rel = "/".join(parts + (e.name,))
            if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(e.name, p) for p in excludes):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not include_env and e.name.lower() in ENV_DIR_NAMES:
                        continue
                    stack.append((e.path, parts + (e.name,)))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    out.append((rel, st.st_size, int(st.st_mtime)))
            except OSError as exc:
                print(f"  пропущено {rel}: {exc}")
    return out


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


def paths_for(out, name):
    return (os.path.join(out, "data", name), os.path.join(out, "manifest", name))


def shard_file(data_dir, name, i, codec):
    return os.path.join(data_dir, f"{name}-{i:05d}{EXT_BY_CODEC[codec]}")


def manifest_file(man_dir, name, i):
    return os.path.join(man_dir, f"{name}-{i:05d}.jsonl")


# ----------------------------------------------------------------------------
# Упаковка
# ----------------------------------------------------------------------------
def cmd_pack(a):
    src = os.path.abspath(a.src)
    if not os.path.isdir(src):
        sys.exit(f"Папка не найдена: {src}")
    name = a.name or os.path.basename(src.rstrip("\\/"))
    codec = pick_codec(a.codec)
    level = a.level if a.level is not None else (19 if codec == "zstd" else 9)
    threads = a.threads or (os.cpu_count() or 1)
    data_dir, man_dir = paths_for(a.out, name)
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(man_dir, exist_ok=True)

    plan_path = os.path.join(man_dir, "plan.jsonl.gz")
    meta_path = os.path.join(man_dir, "pack_settings.json")
    if os.path.exists(plan_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if meta["codec"] != codec:
            sys.exit(f"Набор {name} уже начат с кодеком {meta['codec']}; укажите --codec {meta['codec']}")
        with gzip.open(plan_path, "rt", encoding="utf-8") as f:
            plan = [json.loads(line) for line in f]
        print(f"Продолжение упаковки по сохраненному плану: {num(len(plan))} файлов")
    else:
        print(f"Обход {src} ...")
        files = walk_files(src, a.exclude, a.include_env)
        plan = make_plan(files, int(a.shard_gb * 2**30))
        with gzip.open(plan_path, "wt", encoding="utf-8") as f:
            for e in plan:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        meta = {"name": name, "source": src, "codec": codec, "level": level, "long": not a.no_long,
                "shard_gb": a.shard_gb, "created": dt.datetime.now().isoformat(timespec="seconds")}
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)

    n_shards = (plan[-1]["s"] + 1) if plan else 0
    total = sum(e["n"] for e in plan)
    print(f"Набор {name}: {num(len(plan))} файлов, {human(total)}, шардов: {n_shards}; "
          f"сжатие {codec} уровня {level}, потоков {threads if codec == 'zstd' else 1}")

    by_shard = [[] for _ in range(n_shards)]
    for e in plan:
        by_shard[e["s"]].append(e)

    src_os = long_path(src)
    t_start = time.monotonic()
    done_bytes = 0
    processed_bytes = 0
    for i, entries in enumerate(by_shard):
        sf = shard_file(data_dir, name, i, codec)
        mf = manifest_file(man_dir, name, i)
        raw = sum(e["n"] for e in entries)
        if os.path.exists(sf) and os.path.exists(mf):
            done_bytes += raw
            continue
        t0 = time.monotonic()
        tmp_sf, tmp_mf = sf + ".partial", mf + ".partial"
        writer = open_writer(tmp_sf, codec, level, threads, not a.no_long)
        cw = CountingWriter(writer)
        tar = tarfile.open(fileobj=cw, mode="w|", format=tarfile.PAX_FORMAT)
        records, missing = [], 0
        for e in entries:
            p = os.path.join(src_os, *e["p"].split("/"))
            try:
                st = os.stat(p)
                with open(p, "rb") as f:
                    ti = tarfile.TarInfo(e["p"])
                    ti.size = st.st_size
                    ti.mtime = int(st.st_mtime)
                    ti.mode = 0o644
                    hr = HashingReader(f)
                    tar.addfile(ti, hr)
            except OSError as exc:
                missing += 1
                print(f"\n  не удалось прочитать {e['p']}: {exc}")
                continue
            records.append({"path": e["p"], "size": st.st_size, "mtime": int(st.st_mtime),
                            "sha256": hr.h.hexdigest()})
        tar.close()
        writer.close()
        comp = os.path.getsize(tmp_sf)
        with open(tmp_mf, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp_sf, sf)
        os.replace(tmp_mf, mf)  # манифест последним: признак готового шарда
        done_bytes += raw
        processed_bytes += raw
        dt_s = time.monotonic() - t0
        speed = raw / dt_s if dt_s > 0 else 0
        overall = processed_bytes / (time.monotonic() - t_start)
        eta = (total - done_bytes) / overall if overall > 0 else 0
        print(f"  шард {i + 1}/{n_shards}: {human(raw)} -> {human(comp)} "
              f"({pct(comp, raw)}), {human(speed)}/с, осталось примерно {fmt_time(eta)}"
              + (f", пропущено файлов: {missing}" if missing else ""))

    write_summary(a.out, name, codec, level)
    copy_self(a.out)
    write_card(a.out, a.hf_repo, a.github_url, a.waymo, a.title)
    print(f"\nГотово. Следующий шаг: проверка архивов\n"
          f'  python pack_shards.py verify "{a.out}" --name {name} --src "{src}"')


def write_summary(out, name, codec, level):
    data_dir, man_dir = paths_for(out, name)
    files = raw = comp = shards = 0
    i = 0
    while True:
        mf = manifest_file(man_dir, name, i)
        if not os.path.exists(mf):
            break
        with open(mf, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                files += 1
                raw += r["size"]
        comp += os.path.getsize(shard_file(data_dir, name, i, codec))
        shards += 1
        i += 1
    s = {"name": name, "codec": codec, "level": level, "shards": shards, "files": files,
         "raw_bytes": raw, "compressed_bytes": comp,
         "updated": dt.datetime.now().isoformat(timespec="seconds")}
    with open(os.path.join(man_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)
    print(f"Итого {name}: {num(files)} файлов, {human(raw)} -> {human(comp)} "
          f"({pct(comp, raw)}), шардов {shards}")
    return s


def copy_self(out):
    try:
        shutil.copy2(os.path.abspath(__file__), os.path.join(out, "pack_shards.py"))
    except (OSError, shutil.SameFileError):
        pass


# ----------------------------------------------------------------------------
# Проверка
# ----------------------------------------------------------------------------
def list_names(out, name):
    man_root = os.path.join(out, "manifest")
    if name:
        return [name]
    return sorted(d for d in os.listdir(man_root) if os.path.isdir(os.path.join(man_root, d)))


def shard_list(out, name):
    data_dir, man_dir = paths_for(out, name)
    result = []
    for fn in sorted(os.listdir(data_dir)):
        if fn.endswith(".partial"):
            continue
        base = fn.split(".tar.")[0]
        mf = os.path.join(man_dir, base + ".jsonl")
        if os.path.exists(mf):
            result.append((os.path.join(data_dir, fn), mf))
    return result


def cmd_verify(a):
    ok_all = True
    report = []
    for name in list_names(a.out, a.name):
        shards = shard_list(a.out, a.name or name)
        print(f"Проверка набора {name}: шардов {len(shards)}")
        n_files = 0
        for sf, mf in shards:
            with open(mf, encoding="utf-8") as f:
                expected = {r["path"]: r for r in map(json.loads, f)}
            seen = set()
            bad = []
            try:
                with open_reader(sf) as rd:
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
            n_files += len(seen)
            status = "OK" if not bad else "ОШИБКА"
            print(f"  {os.path.basename(sf)}: {status}, файлов {num(len(seen))}")
            for b in bad[:10]:
                print(f"    {b}")
            if bad:
                ok_all = False
                report.append(f"{sf}: " + "; ".join(bad[:20]))

            if a.src:
                src_os = long_path(a.src)
                changed = 0
                for p, r in expected.items():
                    try:
                        st = os.stat(os.path.join(src_os, *p.split("/")))
                        if st.st_size != r["size"] or int(st.st_mtime) != r["mtime"]:
                            changed += 1
                    except OSError:
                        changed += 1
                if changed:
                    ok_all = False
                    msg = f"{os.path.basename(sf)}: {changed} исходных файлов изменились или исчезли после упаковки"
                    print("    " + msg)
                    report.append(msg)
        print(f"  проверено файлов: {num(n_files)}")

    with open(os.path.join(a.out, "verify_report.txt"), "w", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%d.%m.%Y %H:%M} {'OK' if ok_all else 'ОШИБКИ'}\n" + "\n".join(report))
    if ok_all:
        print("\nВсе архивы целы и совпадают с манифестами. Исходные данные можно удалять "
              "после загрузки архивов в хранилище.")
    else:
        print("\nЕсть ошибки (см. verify_report.txt). Исходные данные удалять нельзя.")
        sys.exit(1)


# ----------------------------------------------------------------------------
# Восстановление
# ----------------------------------------------------------------------------
def cmd_unpack(a):
    dest = os.path.abspath(a.dest)
    os.makedirs(dest, exist_ok=True)
    for name in list_names(a.out, a.name):
        target = os.path.join(dest, name) if not a.flat else dest
        os.makedirs(target, exist_ok=True)
        shards = shard_list(a.out, name)
        print(f"Распаковка {name} в {target}: шардов {len(shards)}")
        for sf, _ in shards:
            n = 0
            with open_reader(sf) as rd:
                with tarfile.open(fileobj=rd, mode="r|") as tar:
                    for m in tar:
                        if a.pattern and not any(fnmatch.fnmatch(m.name, p) for p in a.pattern):
                            continue
                        try:
                            tar.extract(m, target, filter="data")
                        except TypeError:  # Python без параметра filter
                            if m.name.startswith(("/", "\\")) or ".." in m.name.split("/"):
                                continue
                            tar.extract(m, target)
                        n += 1
            print(f"  {os.path.basename(sf)}: извлечено {num(n)}")
    print("Готово.")


# ----------------------------------------------------------------------------
# Карточка датасета и фрагмент README
# ----------------------------------------------------------------------------
def write_card(out, hf_repo, github_url, waymo, title):
    sums = []
    man_root = os.path.join(out, "manifest")
    if os.path.isdir(man_root):
        for d in sorted(os.listdir(man_root)):
            p = os.path.join(man_root, d, "summary.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    sums.append(json.load(f))
    cfg_path = os.path.join(out, "card_settings.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
    hf_repo = hf_repo or cfg.get("hf_repo") or "USER/DATASET"
    github_url = github_url or cfg.get("github_url") or "https://github.com/USER/REPO"
    waymo = waymo or cfg.get("waymo", False)
    title = title or cfg.get("title") or "Данные проекта"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"hf_repo": hf_repo, "github_url": github_url, "waymo": waymo, "title": title},
                  f, ensure_ascii=False, indent=1)

    y = ["---", f"pretty_name: {title}"]
    if waymo:
        y += ["license: other", "license_name: waymo-dataset-license-agreement-non-commercial",
              "license_link: https://waymo.com/open/terms/",
              "tags:", "- autonomous-driving", "- motion-prediction", "- waymo-open-dataset"]
    else:
        y += ["license: other"]
    y += ["---", ""]

    rows = ["| Набор | Файлов | Исходный объем | В архивах | Шардов |", "|---|---:|---:|---:|---:|"]
    for s in sums:
        rows.append(f"| {s['name']} | {num(s['files'])} | {human(s['raw_bytes'])} | {human(s['compressed_bytes'])} | "
                    f"{s['shards']} |")

    body = [
        f"# {title}", "",
        f"Данные к репозиторию с кодом: {github_url}", "",
        "Данные упакованы в шарды tar со сжатием zstd (или xz). Для каждого шарда в папке `manifest` "
        "лежит список файлов с размерами и хешами SHA-256.", "",
        "## Состав", "", *rows, "",
        "## Скачивание и распаковка", "",
        "```bash",
        "pip install -U huggingface_hub zstandard",
        f"hf download {hf_repo} --repo-type dataset --local-dir data_backup",
        "python data_backup/pack_shards.py verify data_backup",
        "python data_backup/pack_shards.py unpack data_backup restored",
        "```", "",
        "Отдельный набор можно скачать без остальных с помощью параметра `--include` команды `hf download` "
        "(шаблоны `data/<набор>/*`, `manifest/<набор>/*` и `pack_shards.py`).", "",
    ]
    if waymo:
        body += [
            "## Лицензия", "",
            "Данные получены из Waymo Open Dataset и являются его модификацией. Они распространяются только "
            "пользователям, зарегистрированным в Waymo Open Dataset и принявшим условия лицензии "
            "(https://waymo.com/open/terms/), для некоммерческого использования. Репозиторий приватный.", "",
            "This work was made using the Waymo Open Dataset, provided by Waymo LLC under the Waymo Dataset "
            "License Agreement for Non-Commercial Use.", "",
        ]
    with open(os.path.join(out, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(y + body))

    snippet = [
        "## Данные", "",
        f"Данные проекта хранятся в приватном датасете Hugging Face: https://huggingface.co/datasets/{hf_repo}", "",
        "Восстановление:", "",
        "```bash",
        "pip install -U huggingface_hub zstandard",
        "hf auth login",
        f"hf download {hf_repo} --repo-type dataset --local-dir data_backup",
        "python data_backup/pack_shards.py unpack data_backup data",
        "```", "",
    ]
    if waymo:
        snippet += ["Данные являются производными от Waymo Open Dataset и предоставляются только "
                    "пользователям, принявшим лицензию Waymo Open Dataset (https://waymo.com/open/terms/).", ""]
    with open(os.path.join(out, "GITHUB_README_SNIPPET.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(snippet))
    print(f"Карточка датасета: {os.path.join(out, 'README.md')}")
    print(f"Загрузка в Hugging Face (приватный датасет):\n"
          f"  hf auth login\n  hf upload {hf_repo} \"{os.path.abspath(out)}\" . --repo-type dataset --private")


def cmd_card(a):
    for name in list_names(a.out, None):
        s_path = os.path.join(a.out, "manifest", name, "pack_settings.json")
        if os.path.exists(s_path):
            with open(s_path, encoding="utf-8") as f:
                m = json.load(f)
            write_summary(a.out, name, m["codec"], m["level"])
    copy_self(a.out)
    write_card(a.out, a.hf_repo, a.github_url, a.waymo, a.title)


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Упаковка данных в сжатые шарды, проверка и восстановление.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pack", help="упаковать папку в шарды")
    p.add_argument("src", help="папка с данными")
    p.add_argument("out", help="папка для архивов (лучше на другом диске)")
    p.add_argument("--name", help="имя набора (по умолчанию имя папки)")
    p.add_argument("--shard-gb", type=float, default=4.0, help="объем исходных данных в одном шарде, ГБ (по умолчанию 4)")
    p.add_argument("--codec", choices=["auto", "zstd", "xz"], default="auto")
    p.add_argument("--level", type=int, help="уровень сжатия: zstd 1–22 (по умолчанию 19), xz 0–9 (по умолчанию 9)")
    p.add_argument("--threads", type=int, default=0, help="потоков zstd (0 – все ядра)")
    p.add_argument("--no-long", action="store_true", help="отключить режим длинных совпадений zstd")
    p.add_argument("--exclude", action="append", default=[], help="шаблон имени или пути для пропуска")
    p.add_argument("--include-env", action="store_true", help="не пропускать __pycache__, .git, venv и подобные")
    p.add_argument("--hf-repo", help="репозиторий Hugging Face, например user/dataset")
    p.add_argument("--github-url", help="адрес репозитория GitHub с кодом")
    p.add_argument("--waymo", action="store_true", help="данные получены из Waymo Open Dataset (добавить условия лицензии)")
    p.add_argument("--title", help="заголовок карточки датасета")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("verify", help="проверить архивы по манифестам")
    p.add_argument("out")
    p.add_argument("--name")
    p.add_argument("--src", help="исходная папка: проверить, что файлы не менялись после упаковки")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("unpack", help="распаковать")
    p.add_argument("out")
    p.add_argument("dest")
    p.add_argument("--name")
    p.add_argument("--pattern", action="append", help="извлечь только файлы по шаблону, например \"*/scene_001*\"")
    p.add_argument("--flat", action="store_true", help="не создавать подпапку с именем набора")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("card", help="пересоздать карточку датасета и фрагмент README")
    p.add_argument("out")
    p.add_argument("--hf-repo")
    p.add_argument("--github-url")
    p.add_argument("--waymo", action="store_true")
    p.add_argument("--title")
    p.set_defaults(func=cmd_card)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
