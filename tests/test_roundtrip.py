import os

import pytest

from conftest import assert_same_mtimes, assert_same_tree, write_file
from shardkeep import codecs, layout
from shardkeep.cli import main
from shardkeep.pack import pack
from shardkeep.unpack import unpack
from shardkeep.verify import verify

SMALL = 64 / 2**20  # 64 КБ исходных данных на часть: получается несколько частей


@pytest.mark.parametrize("codec", ["zstd", "xz"])
def test_pack_verify_unpack_roundtrip(tmp_path, src_tree, codec):
    src, files = src_tree
    out = tmp_path / "out"
    s = pack(src, out, name="set1", codec=codec, level=3, threads=1, shard_gb=SMALL)
    assert s["shards"] > 1
    assert s["files"] == len(files)
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "restored")
    assert_same_tree(tmp_path / "restored" / "set1", files)
    assert_same_mtimes(tmp_path / "restored" / "set1", src)


def test_env_dirs_skipped_and_included(tmp_path, src_tree):
    src, files = src_tree
    pack(src, tmp_path / "a", name="s", level=1, threads=1)
    pack(src, tmp_path / "b", name="s", level=1, threads=1, include_env=True)
    paths_a = set(layout.read_manifest(layout.manifest_file(tmp_path / "a", "s", 0)))
    paths_b = set(layout.read_manifest(layout.manifest_file(tmp_path / "b", "s", 0)))
    assert "__pycache__/x.pyc" not in paths_a
    assert {"__pycache__/x.pyc", "code/.git/HEAD"} <= paths_b


def test_exclude_pattern(tmp_path, src_tree):
    src, files = src_tree
    pack(src, tmp_path / "out", name="s", level=1, threads=1, excludes=["*.png", "media/*"])
    paths = set(layout.read_manifest(layout.manifest_file(tmp_path / "out", "s", 0)))
    assert not any(p.endswith(".png") or p.startswith("media/") for p in paths)
    assert "tables/part_0.csv" in paths


def test_unpack_pattern_and_flat(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=1, threads=1, shard_gb=SMALL)
    n = unpack(out, tmp_path / "r", patterns=["tables/*"], flat=True)
    assert n == 5
    assert sorted(os.listdir(tmp_path / "r" / "tables")) == [f"part_{i}.csv" for i in range(5)]


def test_verify_src_detects_changed_file(tmp_path, src_tree):
    src, _ = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=1, threads=1)
    write_file(src, "tables/part_1.csv", b"changed")
    assert not verify(out, src=src).ok


@pytest.mark.skipif(codecs._stdzstd is None or codecs._zstd is None,
                    reason="нужны оба варианта zstd: пакет zstandard и модуль compression.zstd")
def test_stdlib_zstd_interoperates(tmp_path, src_tree, monkeypatch):
    src, files = src_tree
    out = tmp_path / "out"
    monkeypatch.setattr(codecs, "_zstd", None)  # упаковка модулем compression.zstd
    pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    monkeypatch.undo()  # проверка и распаковка пакетом zstandard
    assert verify(out).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "s", files)


def test_cli_roundtrip(tmp_path, src_tree, capsys):
    src, files = src_tree
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as e:
        main(["pack", str(src), str(out), "--name", "s", "--level", "1", "--threads", "1"])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        main(["verify", str(out), "--src", str(src)])
    assert e.value.code == 0
    with pytest.raises(SystemExit) as e:
        main(["unpack", str(out), str(tmp_path / "r")])
    assert e.value.code == 0
    assert_same_tree(tmp_path / "r" / "s", files)


def test_cli_reports_user_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["pack", str(tmp_path / "missing"), str(tmp_path / "out")])
    assert e.value.code == 2
    assert "Folder not found" in capsys.readouterr().err
