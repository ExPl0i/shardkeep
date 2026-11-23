"""Командная строка shardkeep: одна команда и подкоманды."""

import argparse
import sys

from shardkeep import __version__
from shardkeep.errors import UserError

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


# ----------------------------------------------------------------------------
def run_pack(a):
    from shardkeep.pack import pack
    pack(a.src, a.out, name=a.name, codec=a.codec, level=a.level, threads=a.threads,
         long_mode=not a.no_long, shard_gb=a.shard_gb, excludes=a.exclude, include_env=a.include_env)
    print(f"\nГотово. Следующий шаг: проверка архивов\n"
          f'  shardkeep verify "{a.out}" --src "{a.src}"' + (f" --name {a.name}" if a.name else ""))
    return 0


def run_verify(a):
    from shardkeep.verify import verify
    return 0 if verify(a.out, name=a.name, src=a.src).ok else 1


def run_unpack(a):
    from shardkeep.unpack import unpack
    unpack(a.out, a.dest, name=a.name, patterns=a.pattern, flat=a.flat)
    return 0


def add_pack(sub):
    p = sub.add_parser("pack", help=COMMANDS["pack"], description=COMMANDS["pack"])
    p.add_argument("src", help="папка с данными")
    p.add_argument("out", help="папка для архивов (лучше на другом диске)")
    p.add_argument("--name", help="имя набора (по умолчанию имя папки)")
    p.add_argument("--shard-gb", type=float, default=4.0,
                   help="объем исходных данных в одной части, ГБ (по умолчанию 4)")
    p.add_argument("--codec", choices=["auto", "zstd", "xz"], default="auto")
    p.add_argument("--level", type=int, help="уровень сжатия: zstd 1–22 (по умолчанию 19), xz 0–9 (по умолчанию 9)")
    p.add_argument("--threads", type=int, default=0, help="потоков zstd (0 – все ядра)")
    p.add_argument("--no-long", action="store_true", help="отключить режим длинных совпадений zstd")
    p.add_argument("--exclude", action="append", default=[], help="шаблон имени или пути для пропуска")
    p.add_argument("--include-env", action="store_true",
                   help="не пропускать __pycache__, .git, venv и подобные папки")
    p.set_defaults(func=run_pack)


def add_verify(sub):
    p = sub.add_parser("verify", help=COMMANDS["verify"], description=COMMANDS["verify"])
    p.add_argument("out", help="папка с архивами")
    p.add_argument("--name", help="проверить только этот набор")
    p.add_argument("--src", help="исходная папка: проверить, что файлы не менялись после упаковки")
    p.set_defaults(func=run_verify)


def add_unpack(sub):
    p = sub.add_parser("unpack", help=COMMANDS["unpack"], description=COMMANDS["unpack"])
    p.add_argument("out", help="папка с архивами")
    p.add_argument("dest", help="куда распаковать")
    p.add_argument("--name", help="распаковать только этот набор")
    p.add_argument("--pattern", action="append",
                   help='извлечь только файлы по шаблону, например "*/scene_001*" (можно несколько)')
    p.add_argument("--flat", action="store_true", help="не создавать подпапку с именем набора")
    p.set_defaults(func=run_unpack)


ADDERS = {"pack": add_pack, "verify": add_verify, "unpack": add_unpack}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="shardkeep",
        description="Упаковка больших папок с данными в сжатые части, проверка, загрузка и восстановление.",
    )
    ap.add_argument("--version", action="version", version=f"shardkeep {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="команда")
    for name, help_text in COMMANDS.items():
        if name in ADDERS:
            ADDERS[name](sub)
        else:
            p = sub.add_parser(name, help=help_text, description=help_text)
            p.set_defaults(func=_not_ready)
    return ap


def _not_ready(a):
    raise UserError(f"Команда {a.cmd} пока не реализована.")


def main(argv=None):
    setup_console()
    a = build_parser().parse_args(argv)
    try:
        code = a.func(a)
    except UserError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        code = exc.code
    except KeyboardInterrupt:
        print("\nПрервано. Повторный запуск той же команды продолжит работу.", file=sys.stderr)
        code = 130
    sys.exit(code or 0)
