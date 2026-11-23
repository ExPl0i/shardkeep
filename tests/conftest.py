import argparse
import importlib.util
import json
import os
import random
import types

import pytest

from shardkeep.fsutil import long_path

LEGACY_PATH = os.path.join(os.path.dirname(__file__), "legacy", "pack_shards_v0.py")
MTIME = 1_700_000_000


@pytest.fixture(scope="session")
def legacy():
    """Исходная версия pack_shards.py: создает архивы старого формата."""
    spec = importlib.util.spec_from_file_location("pack_shards_v0", LEGACY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def legacy_pack(legacy, src, out, **kw):
    a = argparse.Namespace(src=str(src), out=str(out), name=kw.get("name"), codec=kw.get("codec", "auto"),
                           level=kw.get("level", 3), threads=kw.get("threads", 1), no_long=kw.get("no_long", False),
                           shard_gb=kw.get("shard_gb", 64 / 2**20), exclude=kw.get("exclude", []),
                           include_env=kw.get("include_env", False), hf_repo=None, github_url=None,
                           waymo=False, title=None)
    # Исходный скрипт делит объем на время упаковки. Часы Windows идут шагами примерно по 15 мс, и крошечная
    # тестовая часть успевает упаковаться «за ноль секунд»: деление на ноль. Подменяем часы равномерно
    # идущими, сам скрипт не меняем.
    real_time = legacy.time
    ticks = iter(range(1, 10**9))
    legacy.time = types.SimpleNamespace(monotonic=lambda: float(next(ticks)))
    try:
        legacy.cmd_pack(a)
    finally:
        legacy.time = real_time


def write_file(root, rel, data, mtime=MTIME):
    p = long_path(os.path.join(str(root), *rel.split("/")))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    os.utime(p, (mtime, mtime))
    return p


def make_tree(root, seed=1, long_paths=True):
    """Небольшая синтетическая папка с разными видами файлов. Возвращает {путь: байты}."""
    rng = random.Random(seed)
    files = {}

    def add(rel, data, mtime=MTIME):
        files[rel] = data
        write_file(root, rel, data, mtime)

    for i in range(12):
        rec = [{"id": j, "x": j * 0.5, "label": f"объект {j}"} for j in range(rng.randint(50, 400))]
        add(f"scenes/scene_{i:03d}/sample.json", json.dumps(rec, ensure_ascii=False).encode())
        add(f"scenes/scene_{i:03d}/render.png", rng.randbytes(rng.randint(1000, 30000)), MTIME + i)
    for i in range(5):
        add(f"tables/part_{i}.csv", "\n".join(f"{k};{k * k};строка" for k in range(2000)).encode())
    add("media/clip.mp4", rng.randbytes(120_000))
    add("empty.txt", b"")
    add("Папка с пробелами/файл ü ñ ё.txt", "текст".encode())
    add("root_file.bin", bytes(range(256)) * 50)
    # служебные папки: по умолчанию не упаковываются
    write_file(root, "__pycache__/x.pyc", b"cache")
    write_file(root, "code/.git/HEAD", b"ref: refs/heads/main\n")
    add("code/main.py", b"print('hello')\n")
    if long_paths:
        deep = "/".join(["очень_длинное_имя_папки_" + str(k) for k in range(10)])
        add(f"deep/{deep}/файл_с_длинным_путем.json", b'{"deep": true}')
    return files


def read_tree(root):
    """{относительный путь: (байты, время изменения)} для всех файлов папки."""
    root_os = long_path(str(root))
    out = {}
    for dirpath, _, names in os.walk(root_os):
        for n in names:
            p = os.path.join(dirpath, n)
            rel = os.path.relpath(p, root_os).replace("\\", "/")
            with open(p, "rb") as f:
                out[rel] = (f.read(), int(os.stat(p).st_mtime))
    return out


@pytest.fixture
def src_tree(tmp_path):
    src = tmp_path / "src"
    files = make_tree(src)
    return src, files


def assert_same_tree(restored, files):
    got = read_tree(restored)
    assert set(got) == set(files), (set(got) ^ set(files))
    for rel, data in files.items():
        assert got[rel][0] == data, rel


def assert_same_mtimes(restored, src):
    got, exp = read_tree(restored), read_tree(src)
    for rel, (_, mtime) in got.items():
        assert mtime == exp[rel][1], rel
