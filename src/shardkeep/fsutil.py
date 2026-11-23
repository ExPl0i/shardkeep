"""Работа с файловой системой: длинные пути Windows, обход папок, чтение и запись текста."""

import fnmatch
import json
import os

# Папки окружений и кэша, которые по умолчанию не упаковываются.
ENV_DIR_NAMES = {"__pycache__", ".ipynb_checkpoints", ".mypy_cache", ".pytest_cache", "venv", ".venv",
                 "node_modules", ".git"}


def long_path(p):
    """Абсолютный путь; в Windows с префиксом \\\\?\\, снимающим ограничение в 260 символов."""
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)
    return p


def os_path(root_os, rel):
    """Путь ОС для относительного пути с разделителем «/»."""
    return os.path.join(root_os, *rel.split("/"))


def is_excluded(rel, name, patterns):
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def walk_files(src, excludes=(), include_env=False, on_error=None, on_file=None):
    """Список (относительный путь, размер, время изменения) всех файлов папки.

    Символические ссылки пропускаются. Относительные пути используют разделитель «/».
    on_file(размер) вызывается для каждого найденного файла (для строки прогресса).
    """
    if on_error is None:
        from shardkeep.progress import echo as on_error
    root = long_path(src)
    out = []
    stack = [(root, ())]
    while stack:
        path, parts = stack.pop()
        try:
            with os.scandir(path) as it:
                entries = list(it)
        except OSError as exc:
            on_error(f"  skipped {'/'.join(parts) or '.'}: {exc}")
            continue
        for e in entries:
            rel = "/".join(parts + (e.name,))
            if excludes and is_excluded(rel, e.name, excludes):
                continue
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not include_env and e.name.lower() in ENV_DIR_NAMES:
                        continue
                    stack.append((e.path, parts + (e.name,)))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    out.append((rel, st.st_size, int(st.st_mtime)))
                    if on_file is not None:
                        on_file(st.st_size)
            except OSError as exc:
                on_error(f"  skipped {rel}: {exc}")
    return out


def read_text(path):
    """Читает текстовый файл в UTF-8, UTF-8 с BOM или UTF-16 с BOM.

    PowerShell 5.1 при перенаправлении «>» пишет UTF-16, а Set-Content -Encoding utf8 добавляет BOM.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8")


def write_text(path, text):
    """Пишет текст в UTF-8 без BOM с переводами строк «\\n»."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_json(path):
    return json.loads(read_text(path))


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
