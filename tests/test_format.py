"""Расширения формата 2: параметры частей, смена параметров при продолжении, непрочитанные файлы,
полнота набора и сверка с исходной папкой."""

import builtins
import hashlib
import json
import os

import pytest

from conftest import assert_same_tree, write_file
from shardkeep import codecs, layout
from shardkeep import pack as pack_mod
from shardkeep.errors import UserError
from shardkeep.pack import pack
from shardkeep.unpack import unpack
from shardkeep.verify import verify

SMALL = 64 / 2**20


def sha256_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_shard_meta_written(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=5, threads=1, shard_gb=SMALL)
    shards = layout.shard_list(out, "s")
    total_files = 0
    for sh in shards:
        info = layout.read_shard_meta(out, "s", sh.index)
        assert info["format"] == 2
        assert info["level"] == 5 and info["codec"] == "zstd" and info["long"] is True
        assert info["sha256"] == sha256_file(sh.path)
        assert info["compressed_bytes"] == os.path.getsize(sh.path)
        assert info["files"] == len(layout.read_manifest(sh.manifest))
        assert info["skipped"] == []
        total_files += info["files"]
    assert total_files == len(files)
    summary = json.load(open(layout.summary_file(out, "s"), encoding="utf-8"))
    assert summary["complete"] is True and summary["levels"] == [5]
    settings = json.load(open(layout.settings_file(out, "s"), encoding="utf-8"))
    assert settings["format"] == 2 and settings["excludes"] == [] and settings["include_env"] is False


def _interrupt_after(monkeypatch, n_ok):
    real = pack_mod.open_writer
    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        if calls["n"] > n_ok:
            raise KeyboardInterrupt
        return real(*a, **kw)

    monkeypatch.setattr(pack_mod, "open_writer", fake)


def test_resume_keeps_saved_level_by_default(tmp_path, src_tree, monkeypatch):
    src, _ = src_tree
    out = tmp_path / "out"
    _interrupt_after(monkeypatch, 1)
    with pytest.raises(KeyboardInterrupt):
        pack(src, out, name="s", level=7, threads=1, shard_gb=SMALL)
    monkeypatch.undo()
    pack(src, out, name="s", threads=1)  # уровень не указан: берется сохраненный 7, а не 19
    levels = {layout.read_shard_meta(out, "s", sh.index)["level"] for sh in layout.shard_list(out, "s")}
    assert levels == {7}


def test_resume_with_other_level_warns_and_records(tmp_path, src_tree, monkeypatch, capsys):
    src, files = src_tree
    out = tmp_path / "out"
    _interrupt_after(monkeypatch, 1)
    with pytest.raises(KeyboardInterrupt):
        pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    monkeypatch.undo()
    capsys.readouterr()
    s = pack(src, out, name="s", level=1, threads=1, shard_gb=1.0)
    text = capsys.readouterr().out
    assert "начат с уровнем сжатия 3" in text
    assert "размер части 1.0 ГБ не применяется" in text
    assert s["levels"] == [1, 3]
    shards = layout.shard_list(out, "s")
    assert layout.read_shard_meta(out, "s", 0)["level"] == 3
    assert layout.read_shard_meta(out, "s", shards[-1].index)["level"] == 1
    settings = json.load(open(layout.settings_file(out, "s"), encoding="utf-8"))
    assert settings["level"] == 3 and settings["sessions"][-1]["level"] == 1
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "s", files)


# ---------------------------------------------------------------- непрочитанные файлы
class FlakyFile:
    """Файл, чтение которого обрывается после первых байтов."""

    def __init__(self, f, mode):
        self.f = f
        self.mode = mode
        self.calls = 0

    def read(self, n=-1):
        self.calls += 1
        if self.calls > 1:
            if self.mode == "error":
                raise OSError("ошибка чтения устройства")
            return b""  # файл стал короче
        return self.f.read(min(n, 100) if n and n > 0 else 100)

    def fileno(self):
        return self.f.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.f.close()


def patch_open(monkeypatch, target, behavior):
    real_open = builtins.open

    def fake_open(p, mode="r", *a, **kw):
        if str(p).replace("\\", "/").endswith(target):
            if behavior == "deny":
                raise PermissionError(13, "Отказано в доступе", str(p))
            return FlakyFile(real_open(p, mode, *a, **kw), behavior)
        return real_open(p, mode, *a, **kw)

    monkeypatch.setattr(pack_mod, "open", fake_open, raising=False)


@pytest.mark.parametrize("behavior", ["deny", "error", "shrink"])
def test_unreadable_file_recorded(tmp_path, src_tree, monkeypatch, behavior, capsys):
    src, files = src_tree
    out = tmp_path / "out"
    target = "tables/part_2.csv"
    patch_open(monkeypatch, target, behavior)
    s = pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    monkeypatch.undo()
    assert s["skipped_files"] == 1
    assert s["files"] == len(files) - 1
    skipped = [x for sh in layout.shard_list(out, "s") for x in layout.read_shard_meta(out, "s", sh.index)["skipped"]]
    assert [x["path"] for x in skipped] == [target]

    capsys.readouterr()
    res = verify(out, src=src)
    assert not res.ok
    assert any("не удалось прочитать при упаковке" in p for p in res.problems)
    assert not any("которых нет в архивах" in p for p in res.problems)  # не дублируется

    res = verify(out, src=src, allow_skipped=True)
    assert res.ok and res.warnings
    unpack(out, tmp_path / "r")
    expected = {k: v for k, v in files.items() if k != target}
    assert_same_tree(tmp_path / "r" / "s", expected)


# ---------------------------------------------------------------- полнота набора
@pytest.fixture
def packed(tmp_path, src_tree):
    src, files = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    return src, out


def test_new_source_file_detected(packed):
    src, out = packed
    write_file(src, "tables/new_part.csv", b"1;2;3")
    res = verify(out, src=src)
    assert not res.ok
    assert any("1 файлов, которых нет в архивах" in p and "tables/new_part.csv" in p for p in res.problems)


def test_excluded_files_not_reported_as_new(tmp_path, src_tree):
    src, _ = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", level=1, threads=1, excludes=["*.png"])
    assert verify(out, src=src).ok


def test_missing_shard_file_detected(packed):
    _, out = packed
    sh = layout.shard_list(out, "s")[1]
    os.remove(sh.path)
    res = verify(out)
    assert not res.ok
    assert any("нет файла части" in p for p in res.problems)
    assert any("не завершена" in p for p in res.problems)


def test_incomplete_set_detected(packed):
    _, out = packed
    sh = layout.shard_list(out, "s")[-1]
    os.remove(sh.manifest)
    res = verify(out)
    assert not res.ok
    assert any("не дописана" in p for p in res.problems)
    assert any("не завершена" in p for p in res.problems)


def test_recompressed_shard_detected(packed):
    """Часть с тем же содержимым, но другим сжатием: содержимое верно, а файл не тот, что загружался."""
    _, out = packed
    sh = layout.shard_list(out, "s")[0]
    with codecs.open_reader(sh.path) as r:
        data = r.read()
    w = codecs.open_writer(sh.path, "zstd", 1, 1, False)
    w.write(data)
    w.close()
    res = verify(out)
    assert not res.ok
    assert any("отличается от записанного при упаковке" in p for p in res.problems)


def test_src_with_several_sets(tmp_path, src_tree):
    src, _ = src_tree
    other = tmp_path / "other"
    write_file(other, "a.txt", b"a")
    out = tmp_path / "out"
    pack(src, out, name="main", level=1, threads=1)
    pack(other, out, name="other", level=1, threads=1)
    assert verify(out, src=src).ok  # сверяется только набор, упакованный из src
    with pytest.raises(UserError):
        verify(out, src=tmp_path)
