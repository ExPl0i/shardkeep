import io

import pytest

from shardkeep import progress
from shardkeep.cli import main
from shardkeep.progress import Progress, echo, fmt_duration
from shardkeep.remote import BatchClock


class TtyStream(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return Clock()


def test_fmt_duration():
    assert fmt_duration(0) == "0:00:00"
    assert fmt_duration(65) == "0:01:05"
    assert fmt_duration(3 * 3600 + 7) == "3:00:07"
    assert fmt_duration(-5) == "0:00:00"


def test_bar_eta_and_percent(clock):
    s = TtyStream()
    bar = Progress("pack s", total=1000, stream=s, enabled=True, clock=clock, ticker=False)
    for _ in range(10):
        clock.advance(1)
        bar.update(50)  # 50 Б/с
    text = bar.text()
    assert "50.0%" in text and "500 B / 1000 B" in text
    assert "50 B/s" in text and "ETA 0:00:10" in text and "elapsed 0:00:10" in text
    assert "█" * 10 in text  # половина полосы
    bar.close()
    assert s.getvalue().endswith("\r")  # строка стерта


def test_eta_follows_recent_speed(clock):
    bar = Progress("x", total=100_000, enabled=True, stream=TtyStream(), clock=clock, ticker=False)
    for _ in range(120):  # две минуты медленно: 10 Б/с
        clock.advance(1)
        bar.update(10)
    for _ in range(120):  # потом быстро: 500 Б/с
        clock.advance(1)
        bar.update(500)
    assert bar.rate() == pytest.approx(500, rel=0.05)  # скорость по последней минуте, а не средняя
    left = 100_000 - bar.n
    assert bar.eta() == pytest.approx(left / 500, rel=0.05)


def test_resumed_part_not_counted_in_speed(clock):
    bar = Progress("x", total=1000, initial=900, enabled=True, stream=TtyStream(), clock=clock, ticker=False)
    clock.advance(10)
    bar.update(10)  # за сеанс 1 Б/с
    assert bar.rate() == pytest.approx(1.0)
    assert bar.eta() == pytest.approx(90)


def test_unknown_total(clock):
    bar = Progress("scanning", unit="files", enabled=True, stream=TtyStream(), clock=clock, ticker=False)
    clock.advance(2)
    bar.update(1000)
    text = bar.text()
    assert "1,000 files" in text and "500.0 files/s" in text and "ETA" not in text


def test_no_speed_yet(clock):
    bar = Progress("x", total=10, enabled=True, stream=TtyStream(), clock=clock, ticker=False)
    assert "ETA --:--:--" in bar.text()


def test_non_tty_prints_rarely(clock):
    s = io.StringIO()
    bar = Progress("x", total=10_000, enabled=True, stream=s, clock=clock, ticker=False)
    for _ in range(100):
        clock.advance(1)
        bar.update(10)
    bar.close()
    lines = [ln for ln in s.getvalue().splitlines() if ln]
    assert 1 <= len(lines) <= 2  # не чаще раза в минуту
    assert "\r" not in s.getvalue()


def test_disabled(clock):
    s = TtyStream()
    bar = Progress("x", total=10, enabled=False, stream=s, clock=clock, ticker=False)
    clock.advance(1)
    bar.update(5)
    bar.close()
    assert s.getvalue() == ""


def test_echo_keeps_bar(monkeypatch, clock, capsys):
    monkeypatch.setattr(progress, "_isatty", lambda stream: True)
    bar = Progress("work", total=100, enabled=True, clock=clock, ticker=False)
    clock.advance(1)
    bar.update(10)
    echo("message")
    bar.close()
    out = capsys.readouterr().out
    before, after = out.split("message\n")
    assert "work" in before and before.rstrip(" ").endswith("\r")  # строка стерта перед сообщением
    assert "work" in after  # и нарисована заново


def test_long_line_cut_to_terminal(clock, monkeypatch):
    monkeypatch.setattr(progress.shutil, "get_terminal_size", lambda fallback: type("T", (), {"columns": 40})())
    s = TtyStream()
    bar = Progress("a very long label " * 5, total=10, enabled=True, stream=s, clock=clock, ticker=False)
    clock.advance(1)
    bar.update(1)
    assert max(len(part) for part in s.getvalue().split("\r")) <= 39


def test_batch_clock():
    c = Clock()
    bc = BatchClock(400, clock=c)
    c.advance(10)
    assert bc.add(100) == "100 B of 400 B uploaded, elapsed 0:00:10, ETA 0:00:30"


# ---------------------------------------------------------------- в командах
@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(progress, "_isatty", lambda stream: True)
    monkeypatch.setattr(progress, "ENABLED", True)
    monkeypatch.setattr(progress, "TTY_INTERVAL", 0.0)


def run(args):
    with pytest.raises(SystemExit) as e:
        main(args)
    return e.value.code


def test_commands_show_progress(tmp_path, src_tree, tty, capsys):
    src, _ = src_tree
    out = tmp_path / "out"
    assert run(["pack", str(src), str(out), "--level", "1", "--threads", "1", "--shard-gb", str(64 / 2**20)]) == 0
    text = capsys.readouterr().out
    assert "pack src  [" in text and "ETA" in text and "shard 1/" in text
    assert "scanning" in text
    assert run(["verify", str(out), "--src", str(src)]) == 0
    text = capsys.readouterr().out
    assert "verify  [" in text and "100.0%" in text
    assert run(["unpack", str(out), str(tmp_path / "r")]) == 0
    assert "unpack  [" in capsys.readouterr().out
    assert run(["analyze", str(src), "--out", str(tmp_path / "rep"), "--no-git"]) == 0
    text = capsys.readouterr().out
    assert "scanning" in text and "compression estimate" in text


def test_no_progress_flag(tmp_path, src_tree, tty, capsys):
    src, _ = src_tree
    assert run(["--no-progress", "pack", str(src), str(tmp_path / "out"), "--level", "1", "--threads", "1"]) == 0
    assert "ETA" not in capsys.readouterr().out
