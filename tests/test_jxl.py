"""JPEG XL: перевод при упаковке, проверка и побайтное восстановление (на поддельных cjxl и djxl)."""

import os
import random
import stat
import subprocess
import sys

import pytest

from conftest import MTIME, assert_same_tree, read_tree, write_file
from shardkeep import layout
from shardkeep.card import copy_restore_script
from shardkeep.cli import main
from shardkeep.errors import UserError
from shardkeep.pack import pack
from shardkeep.unpack import unpack
from shardkeep.verify import verify

FAKE = os.path.join(os.path.dirname(__file__), "fake_jxl.py")


def make_tool(folder, name):
    if os.name == "nt":
        p = folder / f"{name}.cmd"
        p.write_text(f'@"{sys.executable}" "{FAKE}" {name} %*\r\n', encoding="utf-8")
    else:
        p = folder / name
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {name} "$@"\n', encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


@pytest.fixture
def tools_on_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_tool(bin_dir, "cjxl")
    make_tool(bin_dir, "djxl")
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return bin_dir


def no_tools(monkeypatch, tmp_path):
    empty = tmp_path / "empty_bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


@pytest.fixture
def photos(tmp_path):
    rng = random.Random(3)
    src = tmp_path / "src"
    files = {}

    def add(rel, data):
        files[rel] = data
        write_file(src, rel, data)

    for i in range(8):
        add(f"photos/день_{i}/img_{i}.jpg", b"\xff\xd8\xff\xe0" + bytes([i]) * 20000 + b"\xff\xd9")
    add("photos/broken.JPG", b"BROKEN" + b"a" * 5000)          # cjxl не справляется
    add("photos/odd.jpeg", b"MISMATCH" + b"b" * 5000)          # не восстанавливается побайтно
    add("photos/noise.jpg", rng.randbytes(30000))              # .jxl не меньше исходного
    add("notes/readme.txt", b"text " * 1000)
    return src, files


def test_pack_verify_unpack(tmp_path, photos, tools_on_path):
    src, files = photos
    out = tmp_path / "out"
    s = pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    assert s["jxl_files"] == 8 and s["jxl_saved_bytes"] > 100000
    converted, fallback = {}, 0
    for sh in layout.shard_list(out, "p"):
        info = layout.read_shard_meta(out, "p", sh.index)
        converted.update(info.get("jxl", {}))
        fallback += info.get("jxl_fallback", 0)
        manifest = layout.read_manifest(sh.manifest)
        for path in info.get("jxl", {}):
            assert manifest[path]["size"] == len(files[path])  # в манифесте исходный размер
    assert fallback == 3
    assert set(converted) == {p for p in files if "/img_" in p}
    assert all(v["member"] == k + ".jxl" for k, v in converted.items())
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "p", files)
    got = read_tree(tmp_path / "r" / "p")
    assert all(got[p][1] == MTIME for p in converted)


def test_verify_detects_bad_jxl_data(tmp_path, photos, tools_on_path):
    import json
    src, _ = photos
    out = tmp_path / "out"
    pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    for sh in layout.shard_list(out, "p"):
        mp = layout.shard_meta_file(out, "p", sh.index)
        info = json.load(open(mp, encoding="utf-8"))
        if info.get("jxl"):
            first = sorted(info["jxl"])[0]
            info["jxl"][first]["sha256"] = "0" * 64
            json.dump(info, open(mp, "w", encoding="utf-8"))
            break
    res = verify(out)
    assert not res.ok and any("content mismatch" in p for p in res.problems)


def test_without_djxl(tmp_path, photos, tools_on_path, monkeypatch):
    src, files = photos
    out = tmp_path / "out"
    pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    no_tools(monkeypatch, tmp_path)
    res = verify(out)
    assert res.ok and any("djxl is not installed" in w for w in res.warnings)
    with pytest.raises(UserError, match="cjxl and djxl"):
        unpack(out, tmp_path / "r")
    unpack(out, tmp_path / "k", keep_jxl=True)
    names = set(read_tree(tmp_path / "k" / "p"))
    assert "photos/день_0/img_0.jpg.jxl" in names and "photos/broken.JPG" in names


def test_pattern_uses_original_names(tmp_path, photos, tools_on_path):
    src, files = photos
    out = tmp_path / "out"
    pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    n = unpack(out, tmp_path / "r", patterns=["photos/день_1/*"])
    assert n == 1
    assert read_tree(tmp_path / "r" / "p") == {"photos/день_1/img_1.jpg": (files["photos/день_1/img_1.jpg"], MTIME)}


def test_tools_required(tmp_path, photos, monkeypatch):
    src, _ = photos
    no_tools(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["pack", str(src), str(tmp_path / "out"), "--jxl"])
    assert e.value.code == 2


def test_resume_keeps_jxl_setting(tmp_path, photos, tools_on_path, capsys):
    src, _ = photos
    out = tmp_path / "out"
    pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    capsys.readouterr()
    pack(src, out, name="p", threads=1, use_jxl=False)
    assert "--jxl is ignored" in capsys.readouterr().out


def test_restore_script(tmp_path, photos, tools_on_path, monkeypatch):
    src, files = photos
    out = tmp_path / "out"
    pack(src, out, name="p", level=3, threads=1, use_jxl=True)
    copy_restore_script(out)
    script = str(out / "pack_shards.py")

    def run(*args):
        return subprocess.run([sys.executable, script, *args], capture_output=True, text=True, encoding="utf-8")

    r = run("verify", str(out), "--src", str(src), "--name", "p")
    assert r.returncode == 0, r.stdout + r.stderr
    r = run("unpack", str(out), str(tmp_path / "r"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert_same_tree(tmp_path / "r" / "p", files)

    no_tools(monkeypatch, tmp_path)
    r = run("unpack", str(out), str(tmp_path / "r2"))
    assert r.returncode != 0 and "Install djxl" in r.stderr
    r = run("unpack", str(out), str(tmp_path / "r3"), "--keep-jxl")
    assert r.returncode == 0
    r = run("verify", str(out))
    assert r.returncode == 0 and "djxl is not installed" in r.stdout
