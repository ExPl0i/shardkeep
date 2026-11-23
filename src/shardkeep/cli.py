"""Командная строка shardkeep: одна команда и подкоманды."""

import argparse
import sys

from shardkeep import __version__

COMMANDS = {
    "analyze": "разобрать папку: объем, категории файлов, дубликаты, git, оценка сжатия",
    "pack": "упаковать папку в сжатые части",
    "verify": "проверить архивы по манифестам",
    "unpack": "распаковать архивы",
    "compare-zip": "проверить, повторяет ли zip-архив распакованную папку",
    "upload": "загрузить архивы в Hugging Face или через rclone",
    "check-upload": "сверить загруженные архивы с локальными",
    "card": "создать или обновить карточку датасета",
}


def setup_console():
    """Вывод в UTF-8 независимо от кодировки консоли Windows."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def build_parser():
    ap = argparse.ArgumentParser(
        prog="shardkeep",
        description="Упаковка больших папок с данными в сжатые части, проверка, загрузка и восстановление.",
    )
    ap.add_argument("--version", action="version", version=f"shardkeep {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="команда")
    for name, help_text in COMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=_not_ready)
    return ap


def _not_ready(a):
    sys.exit(f"Команда {a.cmd} пока не реализована.")


def main(argv=None):
    setup_console()
    ap = build_parser()
    a = ap.parse_args(argv)
    return a.func(a)
