"""Отдельный скрипт восстановления pack_shards.py, который кладется рядом с архивами."""

import ast
import os
import subprocess
import sys

import pytest

from conftest import assert_same_tree, legacy_pack, make_tree, write_file
from shardkeep import layout
from shardkeep.card import copy_restore_script
from shardkeep.pack import pack

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "src", "shardkeep", "restore_script.py")
SMALL = 64 / 2**20


def run_script(out, *args, block_zstandard=False):
    script = os.path.join(str(out), "pack_shards.py")
    if block_zstandard:
        code = ("import sys, runpy; sys.modules['zstandard'] = None; sys.argv = sys.argv[1:]; "
                "runpy.run_path(sys.argv[0], run_name='__main__')")
        cmd = [sys.executable, "-c", code, script, *args]
    else:
        cmd = [sys.executable, script, *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")


def test_python38_syntax():
    src = open(SCRIPT, encoding="utf-8").read()
    ast.parse(src, feature_version=(3, 8))
    assert "import shardkeep" not in src and "from shardkeep" not in src


@pytest.fixture
def packed(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    copy_restore_script(out)
    return src, files, out


def test_verify_and_unpack(packed, tmp_path):
    src, files, out = packed
    r = run_script(out, "verify", str(out), "--src", str(src), "--name", "s")
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_script(out, "unpack", str(out), str(tmp_path / "r"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert_same_tree(tmp_path / "r" / "s", files)


def test_detects_problems(packed):
    src, _, out = packed
    write_file(src, "brand_new.txt", b"x")
    r = run_script(out, "verify", str(out), "--src", str(src), "--name", "s")
    assert r.returncode == 1 and "not in the archives" in r.stdout
    shard = layout.shard_list(out, "s")[0].path
    data = bytearray(open(shard, "rb").read())
    data[len(data) // 2] ^= 0xFF
    open(shard, "wb").write(bytes(data))
    r = run_script(out, "verify", str(out))
    assert r.returncode == 1 and "ERROR" in r.stdout
    os.remove(layout.shard_list(out, "s")[-1].path)
    r = run_script(out, "verify", str(out))
    assert "shard file is missing" in r.stdout


def test_reads_legacy_archives(tmp_path, legacy):
    src = tmp_path / "src"
    make_tree(src)
    out = tmp_path / "out"
    legacy_pack(legacy, src, out, name="old")
    copy_restore_script(out)  # заменяет копию исходного скрипта
    r = run_script(out, "verify", str(out), "--src", str(src), "--name", "old")
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_script(out, "unpack", str(out), str(tmp_path / "r"), "--pattern", "tables/*", "--flat")
    assert r.returncode == 0
    assert sorted(os.listdir(tmp_path / "r" / "tables")) == [f"part_{i}.csv" for i in range(5)]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="нужен модуль compression.zstd (Python 3.14+)")
def test_works_without_zstandard_package(packed, tmp_path):
    _, files, out = packed
    r = run_script(out, "unpack", str(out), str(tmp_path / "r"), block_zstandard=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert_same_tree(tmp_path / "r" / "s", files)


def test_refuses_pack(packed):
    _, _, out = packed
    r = run_script(out, "pack", "a", "b")
    assert r.returncode != 0 and "pip install shardkeep" in r.stderr
