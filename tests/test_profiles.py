import json
import random

import pytest

from conftest import assert_same_tree, write_file
from shardkeep import layout, profiles
from shardkeep import pack as pack_mod
from shardkeep.fsutil import long_path, walk_files
from shardkeep.pack import pack
from shardkeep.plan import make_plan
from shardkeep.profiles import HIGH, LOW, PROFILES, choose_threads, classify_extensions, zstd_memory_per_thread
from shardkeep.sysinfo import available_memory
from shardkeep.unpack import unpack
from shardkeep.verify import verify

SMALL = 64 / 2**20


def make_mixed(root):
    rng = random.Random(5)
    for i in range(6):
        write_file(root, f"a/{i}.json", json.dumps(list(range(i * 3000, i * 3000 + 20000))).encode())
        write_file(root, f"a/{i}.dat", rng.randbytes(90_000))  # расширение не из таблицы, но не сжимается
        write_file(root, f"b/{i}.jpg", b"\xff\xd8" + b"\x00" * 50)  # мелкие: решение по расширению


def test_classify_by_trial(tmp_path):
    make_mixed(tmp_path)
    files = walk_files(tmp_path)
    classes, stats = classify_extensions(long_path(tmp_path), files, "trial")
    assert classes[".json"] == HIGH
    assert classes[".dat"] == LOW and stats[".dat"][1] > 0.9
    assert classes[".jpg"] == LOW and stats[".jpg"][1] is None  # группа слишком мала для пробы


def test_classify_by_extension_reads_nothing(tmp_path, monkeypatch):
    make_mixed(tmp_path)
    monkeypatch.setattr(profiles, "_trial_ratio", lambda *a: pytest.fail("файлы не должны читаться"))
    classes, _ = classify_extensions(long_path(tmp_path), walk_files(tmp_path), "ext")
    assert classes == {".json": HIGH, ".dat": HIGH, ".jpg": LOW}


def test_plan_separates_classes():
    files = [("a/1.json", 10, 0), ("a/2.png", 10, 0), ("b/3.json", 10, 0), ("b/4.png", 10, 0)]
    plan = make_plan(files, 1000, {".json": HIGH, ".png": LOW})
    shards = {}
    for e in plan:
        shards.setdefault(e["s"], set()).add(e.get("c", HIGH))
    assert sorted(len(v) for v in shards.values()) == [1, 1]  # в каждой части один класс
    assert [e["p"] for e in plan if e.get("c") == LOW] == ["a/2.png", "b/4.png"]


def test_plan_without_classes_unchanged():
    files = [("b/1.png", 10, 0), ("a/2.json", 10, 0)]
    assert [e["p"] for e in make_plan(files, 1000)] == ["a/2.json", "b/1.png"]
    assert all("c" not in e for e in make_plan(files, 1000))


@pytest.mark.parametrize("profile", ["auto", "media"])
def test_auto_and_media_profiles(tmp_path, profile):
    src = tmp_path / "src"
    make_mixed(src)
    out = tmp_path / "out"
    pack(src, out, name="m", profile=profile, level=6, threads=1, shard_gb=SMALL)
    expected_low = {".dat", ".jpg"} if profile == "auto" else {".jpg"}
    for sh in layout.shard_list(out, "m"):
        info = layout.read_shard_meta(out, "m", sh.index)
        exts = {p[p.rfind("."):] for p in layout.read_manifest(sh.manifest)}
        if exts <= expected_low:
            assert info["level"] == 1 and info["long"] is False
        else:
            assert not exts & expected_low
            assert info["level"] == 6 and info["long"] is True
    settings = json.load(open(layout.settings_file(out, "m"), encoding="utf-8"))
    assert settings["profile"] == profile and settings["level"] == 6 and settings["level_low"] == 1
    assert verify(out, src=src).ok


def test_uniform_profiles(tmp_path, src_tree):
    src, files = src_tree
    for name, prof in PROFILES.items():
        if prof.zstd_low is not None or name == "max":
            continue
        out = tmp_path / name
        pack(src, out, name="s", profile=name, threads=1)
        levels = {layout.read_shard_meta(out, "s", sh.index)["level"] for sh in layout.shard_list(out, "s")}
        assert levels == {prof.zstd_high}


def test_max_profile_roundtrip(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    s = pack(src, out, name="s", profile="max", threads=1)
    assert s["levels"] == [22]
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "s", files)


def test_resume_keeps_classes(tmp_path, monkeypatch):
    src = tmp_path / "src"
    make_mixed(src)
    out = tmp_path / "out"
    real = pack_mod.open_writer
    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(pack_mod, "open_writer", fake)
    with pytest.raises(KeyboardInterrupt):
        pack(src, out, name="m", level=4, threads=1, shard_gb=SMALL)
    monkeypatch.undo()
    monkeypatch.setattr(profiles, "_trial_ratio", lambda *a: pytest.fail("при продолжении классы берутся из плана"))
    pack(src, out, name="m", threads=1)
    levels = sorted({layout.read_shard_meta(out, "m", sh.index)["level"] for sh in layout.shard_list(out, "m")})
    assert levels == [1, 4]


def test_unknown_profile(tmp_path):
    from shardkeep.errors import UserError
    with pytest.raises(UserError):
        pack(tmp_path, tmp_path / "out", profile="nope")


# ---------------------------------------------------------------- память и потоки
def test_memory_estimate_grows_with_level():
    low = zstd_memory_per_thread(1, False, 8)
    high = zstd_memory_per_thread(22, True, 8)
    assert high > 1 << 30  # уровень 22 с окном 128 МБ: больше 1 ГБ на поток
    assert low < 64 << 20
    assert zstd_memory_per_thread(22, True, 1) < high


def test_threads_limited_by_memory(monkeypatch):
    monkeypatch.setattr(profiles.os, "cpu_count", lambda: 16)
    n, note = choose_threads("zstd", 22, True, 0, avail=5 << 30)
    assert 1 <= n < 16 and note
    n, note = choose_threads("zstd", 22, True, 12, avail=5 << 30)
    assert n == 12 and note  # явно заданное число соблюдается, но с предупреждением
    assert choose_threads("zstd", 3, False, 0, avail=64 << 30) == (16, None)
    assert choose_threads("xz", 9, False, 0, avail=1 << 30) == (1, None)
    n, _ = choose_threads("zstd", 22, True, 0, avail=1 << 20)
    assert n == 1


def test_available_memory():
    m = available_memory()
    assert m is None or m > 0
