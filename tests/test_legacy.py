"""Обратная совместимость: архивы, созданные исходным pack_shards.py, читаются новой версией,
а архивы новой версии читаются исходным скриптом."""

import argparse
import os

import pytest

from conftest import assert_same_mtimes, assert_same_tree, legacy_pack, make_tree
from shardkeep import layout
from shardkeep.pack import pack
from shardkeep.unpack import unpack
from shardkeep.verify import verify

SMALL = 64 / 2**20


@pytest.mark.parametrize("codec,no_long", [("zstd", False), ("zstd", True), ("xz", False)])
def test_new_version_reads_legacy_archives(tmp_path, legacy, codec, no_long):
    src = tmp_path / "src"
    files = make_tree(src)
    out = tmp_path / "out"
    legacy_pack(legacy, src, out, name="old", codec=codec, no_long=no_long)
    assert len(layout.shard_list(out, "old")) > 1
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "old", files)
    assert_same_mtimes(tmp_path / "r" / "old", src)


def test_new_version_continues_legacy_pack(tmp_path, legacy, monkeypatch):
    """Упаковку, начатую исходным скриптом и прерванную, можно закончить новой версией."""
    src = tmp_path / "src"
    files = make_tree(src)
    out = tmp_path / "out"
    legacy_pack(legacy, src, out, name="old", codec="zstd")
    shards = layout.shard_list(out, "old")
    for s in shards[2:]:  # имитация прерывания: удаляем последние части
        os.remove(s.path)
        os.remove(s.manifest)
    pack(src, out, name="old", codec="zstd", level=3, threads=1, shard_gb=SMALL)
    assert len(layout.shard_list(out, "old")) == len(shards)
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "old", files)


@pytest.mark.parametrize("codec", ["zstd", "xz"])
def test_legacy_script_reads_new_archives(tmp_path, legacy, codec):
    src = tmp_path / "src"
    files = make_tree(src, long_paths=False)  # исходный unpack не поддерживает длинные пути Windows
    out = tmp_path / "out"
    pack(src, out, name="new", codec=codec, level=3, threads=1, shard_gb=SMALL)
    legacy.cmd_verify(argparse.Namespace(out=str(out), name=None, src=str(src)))
    legacy.cmd_unpack(argparse.Namespace(out=str(out), dest=str(tmp_path / "r"), name=None, pattern=None,
                                         flat=False))
    assert_same_tree(tmp_path / "r" / "new", files)
