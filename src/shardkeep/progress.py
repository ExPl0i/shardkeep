"""Строка прогресса с оценкой оставшегося времени для долгих операций.

Без сторонних пакетов. В терминале строка перерисовывается на месте через «\\r» (без управляющих
последовательностей ANSI: консоль PowerShell 5.1 их по умолчанию не понимает). Если вывод
перенаправлен в файл или идет в CI, строка печатается не чаще раза в минуту, чтобы журнал
показывал ход работы и не разрастался.

Скорость считается по последней минуте работы, поэтому оценка времени быстро подстраивается,
когда, например, после хорошо сжимаемых данных начинаются уже сжатые. При продолжении
прерванной работы уже сделанная часть (initial) в скорость не входит.

Строку раз в полсекунды обновляет фоновый поток, поэтому прошедшее время и оценка идут, даже пока
основной поток занят (например, ждет многопоточный zstd). Для таких случаев есть track(): фоновый поток
сам опрашивает функцию, которая возвращает сделанный объем.

Отключается параметром shardkeep --no-progress или переменной окружения SHARDKEEP_NO_PROGRESS=1.
"""

import os
import shutil
import sys
import threading
import time
from collections import deque

from shardkeep.textfmt import human, num

ENABLED = os.environ.get("SHARDKEEP_NO_PROGRESS", "") in ("", "0")
TTY_INTERVAL = 0.2       # секунд между перерисовками в терминале
LOG_INTERVAL = 60.0      # секунд между строками, если вывод не в терминал
WINDOW = 60.0            # секунд: по этому отрезку считается скорость
SAMPLE_EVERY = 0.5
TICK = 0.5               # секунд между опросами фонового потока
BAR_WIDTH = 20

_active = []             # открытые строки прогресса, последняя – видимая
_lock = threading.RLock()


def fmt_duration(sec):
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _isatty(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _bar_chars(stream):
    enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    return ("█", "░") if enc.startswith("utf") else ("#", ".")


class Progress:
    """Прогресс одной операции.

    total – объем работы (None, если заранее неизвестен: тогда показываются счетчик и скорость без оценки
    времени); unit – "B" для байтов или слово для штук ("files"); initial – уже сделанная часть.
    """

    def __init__(self, label, total=None, unit="B", initial=0, stream=None, enabled=None, clock=time.monotonic,
                 ticker=True):
        self.label = label
        self.total = total
        self.unit = unit
        self.n = initial
        self.initial = initial
        self.stream = stream or sys.stdout
        self.enabled = ENABLED if enabled is None else enabled
        self.tty = _isatty(self.stream)
        self.clock = clock
        self.start = clock()
        self.postfix = ""
        self.samples = deque([(self.start, initial)])
        self._last_draw = 0.0 if self.tty else self.start
        self._last_sample = self.start
        self._width = 0
        self.closed = False
        self._source = None
        self._stop = threading.Event()
        self._thread = None
        if self.enabled:
            with _lock:
                if _active:
                    _active[-1]._clear()
                _active.append(self)
            if ticker:
                self._thread = threading.Thread(target=self._run, name="shardkeep-progress", daemon=True)
                self._thread.start()

    def _run(self):
        while not self._stop.wait(TICK):
            try:
                self.poll()
                self._tick()
            except Exception:  # noqa: BLE001 – строка прогресса не должна ронять работу
                pass

    def track(self, source, base=None, limit=None):
        """Фоновый поток будет брать сделанный объем из source(): n = base + source(), но не больше limit.
        track(None) отключает опрос."""
        with _lock:
            self._source = (source, self.n if base is None else base, limit) if source else None

    def poll(self):
        with _lock:
            if self._source is None:
                return
            source, base, limit = self._source
            v = source()
            if v is None:
                return
            n = base + v
            if limit is not None:
                n = min(n, limit)
            self.n = max(self.n, n)

    # ------------------------------------------------------------------ счет
    def update(self, n=1):
        self.n += n
        self._tick()

    def set(self, n):
        self.n = n
        self._tick()

    def add_total(self, n):
        self.total = (self.total or 0) + n

    def set_postfix(self, text):
        self.postfix = text
        self._tick(force=self.tty)

    def _tick(self, force=False):
        if not self.enabled or self.closed:
            return
        with _lock:
            self._tick_locked(force)

    def _tick_locked(self, force):
        now = self.clock()
        if now - self._last_sample >= SAMPLE_EVERY:
            self._last_sample = now
            self.samples.append((now, self.n))
            while len(self.samples) > 2 and now - self.samples[0][0] > WINDOW:
                self.samples.popleft()
        interval = TTY_INTERVAL if self.tty else LOG_INTERVAL
        if force or now - self._last_draw >= interval:
            self._last_draw = now
            self._draw(now)

    # ------------------------------------------------------------------ оценки
    def rate(self, now=None):
        now = self.clock() if now is None else now
        t0, n0 = self.samples[0]
        if now - t0 < 1.0:  # окно еще короткое: средняя скорость с начала
            t0, n0 = self.start, self.initial
        dt = now - t0
        return (self.n - n0) / dt if dt > 0 else 0.0

    def eta(self, now=None):
        if self.total is None:
            return None
        r = self.rate(now)
        left = max(self.total - self.n, 0)
        if left == 0:
            return 0.0
        return left / r if r > 0 else None

    def _fmt(self, v):
        return human(v) if self.unit == "B" else f"{num(int(v))} {self.unit}"

    def _fmt_rate(self, r):
        return f"{human(r)}/s" if self.unit == "B" else f"{r:,.1f} {self.unit}/s"

    def text(self, now=None):
        now = self.clock() if now is None else now
        parts = [self.label]
        r = self.rate(now)
        if self.total:
            frac = min(max(self.n / self.total, 0.0), 1.0)
            full, empty = _bar_chars(self.stream)
            filled = int(round(frac * BAR_WIDTH))
            parts.append("[" + full * filled + empty * (BAR_WIDTH - filled) + "]")
            parts.append(f"{100 * frac:5.1f}%")
            parts.append(f"{self._fmt(self.n)} / {self._fmt(self.total)}")
        else:
            parts.append(self._fmt(self.n))
        if r > 0:
            parts.append(self._fmt_rate(r))
        eta = self.eta(now)
        if self.total is not None:
            parts.append(f"ETA {fmt_duration(eta)}" if eta is not None else "ETA --:--:--")
        parts.append(f"elapsed {fmt_duration(now - self.start)}")
        if self.postfix:
            parts.append(self.postfix)
        return "  ".join(parts)

    # ------------------------------------------------------------------ вывод
    def _columns(self):
        try:
            return max(shutil.get_terminal_size((100, 20)).columns - 1, 20)
        except (OSError, ValueError):
            return 99

    def _draw(self, now):
        with _lock:
            if _active and _active[-1] is not self:
                return  # видна только последняя открытая строка
            line = "  " + self.text(now)
            try:
                if self.tty:
                    line = line[: self._columns()]
                    pad = " " * max(self._width - len(line), 0)
                    self.stream.write("\r" + line + pad)
                    self._width = len(line)
                else:
                    self.stream.write(line + "\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def _clear(self):
        if self.tty and self._width:
            try:
                self.stream.write("\r" + " " * self._width + "\r")
                self.stream.flush()
            except (OSError, ValueError):
                pass
            self._width = 0

    def redraw(self):
        if self.enabled and not self.closed and self.tty:
            self._draw(self.clock())

    def close(self):
        if self.closed:
            return
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.closed = True
        if not self.enabled:
            return
        with _lock:
            self._clear()
            if self in _active:
                _active.remove(self)
            if _active:
                _active[-1].redraw()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def echo(msg="", file=None):
    """print(), который не ломает видимую строку прогресса: стирает ее, печатает сообщение и рисует заново."""
    with _lock:
        bar = _active[-1] if _active else None
        if bar is not None:
            bar._clear()
        print(msg, file=file or sys.stdout, flush=True)
        if bar is not None:
            bar.redraw()


class CallbackReader:
    """Обертка над файлом: сообщает о каждом прочитанном куске в cb(число байт)."""

    def __init__(self, f, cb):
        self.f = f
        self.cb = cb

    def read(self, n=-1):
        b = self.f.read(n)
        if b:
            self.cb(len(b))
        return b
