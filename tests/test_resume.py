import os

import pytest

from conftest import assert_same_tree
from shardkeep import layout, pack as pack_mod
from shardkeep.pack import pack
from shardkeep.unpack import unpack
from shardkeep.verify import verify

SMALL = 64 / 2**20


class Interrupt(BaseException):
    pass


def interrupt_after(monkeypatch, n_ok):
    """Прерывает упаковку в середине записи части номер n_ok + 1."""
    real = pack_mod.open_writer
    calls = {"n": 0}

    def fake(path, *a, **kw):
        calls["n"] += 1
        w = real(path, *a, **kw)
        if calls["n"] > n_ok:
            w.write(b"half-written data")
            raise Interrupt()
        return w

    monkeypatch.setattr(pack_mod, "open_writer", fake)


def test_resume_after_interruption(tmp_path, src_tree, monkeypatch):
    src, files = src_tree
    out = tmp_path / "out"
    interrupt_after(monkeypatch, 2)
    with pytest.raises(Interrupt):
        pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    monkeypatch.undo()

    done = layout.shard_list(out, "s")
    assert len(done) == 2
    stamps = {s.path: os.stat(s.path).st_mtime_ns for s in done}
    partials = [f for f in os.listdir(layout.data_dir(out, "s")) if f.endswith(".partial")]
    assert partials  # недописанная часть осталась временным файлом

    s = pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    assert s["files"] == len(files)
    for p, ns in stamps.items():
        assert os.stat(p).st_mtime_ns == ns  # готовые части не пересоздавались
    assert not [f for f in os.listdir(layout.data_dir(out, "s")) if f.endswith(".partial")]
    assert verify(out, src=src).ok
    unpack(out, tmp_path / "r")
    assert_same_tree(tmp_path / "r" / "s", files)


def test_resume_uses_saved_plan(tmp_path, src_tree, monkeypatch):
    """Файлы, появившиеся после начала упаковки, в начатый набор не попадают."""
    src, files = src_tree
    out = tmp_path / "out"
    interrupt_after(monkeypatch, 1)
    with pytest.raises(Interrupt):
        pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    monkeypatch.undo()
    (src / "new_file.txt").write_bytes(b"new")
    s = pack(src, out, name="s", level=3, threads=1, shard_gb=SMALL)
    assert s["files"] == len(files)


def test_resume_with_other_codec_refused(tmp_path, src_tree):
    from shardkeep.errors import UserError
    src, _ = src_tree
    out = tmp_path / "out"
    pack(src, out, name="s", codec="zstd", level=1, threads=1)
    with pytest.raises(UserError):
        pack(src, out, name="s", codec="xz", level=1)
