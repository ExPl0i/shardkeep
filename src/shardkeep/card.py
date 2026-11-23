"""Карточка датасета (README.md в папке с архивами) и фрагмент README для репозитория с кодом.

Карточка создается из шаблона. Части, которые программа пересчитывает (таблица наборов и команды
восстановления), стоят между метками <!-- shardkeep:begin имя --> и <!-- shardkeep:end имя -->.
При обновлении меняется только текст между метками, все остальное остается как есть.
"""

import json
import os
import re
import shutil
from importlib import resources

from shardkeep import layout
from shardkeep.errors import UserError
from shardkeep.fsutil import read_json, read_text, write_json, write_text
from shardkeep.textfmt import human, num

CARD = "README.md"
SNIPPET = "GITHUB_README_SNIPPET.md"
SETTINGS = "card_settings.json"
RESTORE_SCRIPT = "pack_shards.py"
LANGS = ("en", "ru")
REGION_RE = re.compile(r"(<!-- shardkeep:begin (?P<name>[\w-]+) -->\n)(?P<body>.*?)(\n<!-- shardkeep:end (?P=name) -->)",
                       re.DOTALL)
PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w-]+)\s*\}\}")

TEXT = {
    "en": {
        "default_title": "Dataset",
        "description_code": "Data for the code repository {github_url}.",
        "description": "Data archive packed into compressed shards with manifests.",
        "sets_intro": ("Each set is split into shards: tar archives compressed with {codecs}, stored in `data/<set>/`. "
                       "For every shard, `manifest/<set>/` holds a list of files with sizes, modification times "
                       "and SHA-256 checksums."),
        "table_head": "| Set | Files | Source size | In archives | Shards | Compression |",
        "total": "Total size of the archives: {size}.",
        "incomplete": " (packing not finished: {done} of {planned} shards)",
        "login": "hf auth login",
        "restore_after": ("Each set is extracted to `restored/<set>`. To extract one set, add `--name <set>`; "
                          "to extract only some files, add `--pattern \"folder/*\"`.\n\n"
                          "To download a single set, pass `--include \"data/<set>/*\" --include \"manifest/<set>/*\" "
                          "--include pack_shards.py` to `hf download`.\n\n"
                          "Without the script, a shard can be extracted with standard tools: "
                          "`zstd -d -c <shard>.tar.zst | tar -x`. With shardkeep installed: "
                          "`shardkeep unpack data_backup restored`."),
        "license_heading": "## License",
        "snippet_heading": "## Data",
        "snippet_where": "The project data is stored in a {visibility} Hugging Face dataset: {url}",
        "private": "private",
        "public": "public",
        "snippet_restore": "Restore:",
        "snippet_license": "License terms of the data: {link}",
    },
    "ru": {
        "default_title": "Данные",
        "description_code": "Данные к репозиторию с кодом: {github_url}",
        "description": "Архив данных, упакованный в сжатые части с манифестами.",
        "sets_intro": ("Каждый набор упакован в одну или несколько частей: архивы tar со сжатием {codecs} в папке "
                       "`data/<набор>/`. Для каждой части в папке `manifest/<набор>/` лежит список файлов "
                       "с размерами, временем изменения и контрольными суммами SHA-256."),
        "table_head": "| Набор | Файлов | Исходный объем | В архивах | Частей | Сжатие |",
        "total": "Общий объем архивов: {size}.",
        "incomplete": " (упаковка не завершена: готово {done} из {planned} частей)",
        "login": "hf auth login",
        "restore_after": ("Каждый набор распаковывается в папку `restored/<набор>`. Распаковать один набор: "
                          "параметр `--name <набор>`. Извлечь отдельные файлы: параметр `--pattern \"папка/*\"`.\n\n"
                          "Скачать один набор без остальных можно параметрами `--include \"data/<набор>/*\" "
                          "--include \"manifest/<набор>/*\" --include pack_shards.py` команды `hf download`.\n\n"
                          "Без скрипта часть архива распаковывается стандартными программами: "
                          "`zstd -d -c <часть>.tar.zst | tar -x`. Если установлен shardkeep: "
                          "`shardkeep unpack data_backup restored`."),
        "license_heading": "## Лицензия",
        "snippet_heading": "## Данные",
        "snippet_where": "Данные проекта хранятся в {visibility} датасете Hugging Face: {url}",
        "private": "приватном",
        "public": "публичном",
        "snippet_restore": "Восстановление:",
        "snippet_license": "Условия лицензии данных: {link}",
    },
}

DEFAULTS = {"title": None, "hf_repo": None, "github_url": None, "lang": "en", "private": True,
            "license": "other", "license_name": None, "license_link": None, "license_text": None, "tags": []}


# ----------------------------------------------------------------------------
def load_settings(out, overrides):
    """Параметры карточки: аргументы командной строки, затем card_settings.json, затем значения по умолчанию."""
    path = os.path.join(out, SETTINGS)
    saved = read_json(path) if os.path.exists(path) else {}
    if saved.get("waymo") and not saved.get("license_text") and not overrides.get("license_text"):
        print("Warning: card_settings.json has the legacy \"waymo\" flag. License texts are no longer built in; "
              "pass the text with --license-file (an example is in examples/waymo-license.md of the shardkeep "
              "repository).")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in saved.items() if k in DEFAULTS and v is not None})
    cfg.update({k: v for k, v in overrides.items() if k in DEFAULTS and v is not None and v != []})
    if cfg["lang"] not in LANGS:
        raise UserError(f"Unknown card language: {cfg['lang']}. Available: {', '.join(LANGS)}")
    return cfg


def save_settings(out, cfg):
    path = os.path.join(out, SETTINGS)
    saved = read_json(path) if os.path.exists(path) else {}
    saved.update({k: cfg[k] for k in DEFAULTS})
    write_json(path, saved)


def summaries(out):
    result = []
    root = os.path.join(out, "manifest")
    if not os.path.isdir(root):
        return result
    for name in sorted(os.listdir(root)):
        p = layout.summary_file(out, name)
        if os.path.exists(p):
            result.append(read_json(p))
    return result


# ----------------------------------------------------------------------------
def _yaml(cfg, t):
    lines = [f"pretty_name: {json.dumps(cfg['title'] or t['default_title'], ensure_ascii=False)}",
             f"license: {cfg['license'] or 'other'}"]
    if cfg["license_name"]:
        lines.append(f"license_name: {cfg['license_name']}")
    if cfg["license_link"]:
        lines.append(f"license_link: {cfg['license_link']}")
    if cfg["tags"]:
        lines.append("tags:")
        lines += [f"- {tag}" for tag in cfg["tags"]]
    lines.append("viewer: false")
    return "\n".join(lines)


def _compression(s):
    codec = s.get("codec") or "zstd"
    levels = s.get("levels") or ([s["level"]] if s.get("level") is not None else [])
    return f"{codec} " + ", ".join(str(x) for x in sorted(levels, reverse=True)) if levels else codec


def _sets(sums, t):
    codecs = sorted({s.get("codec") or "zstd" for s in sums}) or ["zstd"]
    rows = [t["sets_intro"].format(codecs=" / ".join(codecs)), "", t["table_head"], "|---|---:|---:|---:|---:|---|"]
    for s in sums:
        shards = str(s["shards"])
        if s.get("planned_shards") not in (None, s["shards"]):
            shards += t["incomplete"].format(done=s["shards"], planned=s["planned_shards"])
        rows.append(f"| `{s['name']}` | {num(s['files'])} | {human(s['raw_bytes'])} | {human(s['compressed_bytes'])} "
                    f"| {shards} | {_compression(s)} |")
    rows += ["", t["total"].format(size=human(sum(s["compressed_bytes"] for s in sums)))]
    return "\n".join(rows)


def _restore_commands(cfg, t, dest):
    repo = cfg["hf_repo"] or "USER/DATASET"
    cmds = ["pip install -U huggingface_hub zstandard"]
    if cfg["private"]:
        cmds.append(t["login"])
    cmds += [f"hf download {repo} --repo-type dataset --local-dir data_backup"]
    return cmds, [f"python data_backup/{RESTORE_SCRIPT} verify data_backup",
                  f"python data_backup/{RESTORE_SCRIPT} unpack data_backup {dest}"]


def _restore(cfg, t):
    get, use = _restore_commands(cfg, t, "restored")
    return "\n".join(["```bash", *get, *use, "```", "", t["restore_after"]])


def regions(cfg, sums):
    t = TEXT[cfg["lang"]]
    return {"sets": _sets(sums, t), "restore": _restore(cfg, t)}


def values(cfg, sums):
    t = TEXT[cfg["lang"]]
    v = {
        "yaml": _yaml(cfg, t),
        "title": cfg["title"] or t["default_title"],
        "description": (t["description_code"].format(github_url=cfg["github_url"]) if cfg["github_url"]
                        else t["description"]),
        "hf_repo": cfg["hf_repo"] or "USER/DATASET",
        "github_url": cfg["github_url"] or "",
        "license_section": (f"\n{t['license_heading']}\n\n{cfg['license_text'].strip()}\n"
                            if cfg["license_text"] else ""),
    }
    v.update(regions(cfg, sums))
    return v


def render(template, vals):
    unknown = set()

    def sub(m):
        key = m.group(1)
        if key in vals:
            return vals[key]
        unknown.add(key)
        return m.group(0)

    text = PLACEHOLDER_RE.sub(sub, template)
    if unknown:
        print("Warning: unknown placeholders in the card template: " + ", ".join(sorted(unknown)))
    return text


def update_regions(text, new_regions):
    """Заменяет текст между метками. Возвращает (новый текст, имена обновленных областей).

    Переводы строк файла сохраняются: карточка, сохраненная в Блокноте Windows, остается с CRLF.
    """
    found = []
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")

    def sub(m):
        name = m.group("name")
        if name not in new_regions:
            return m.group(0)
        found.append(name)
        return m.group(1) + new_regions[name] + m.group(4)

    text = REGION_RE.sub(sub, text)
    return (text.replace("\n", "\r\n") if crlf else text), found


def _template(cfg, template_path):
    if template_path:
        return read_text(template_path)
    return resources.files("shardkeep").joinpath("templates", f"card.{cfg['lang']}.md").read_text(encoding="utf-8")


def snippet(cfg):
    t = TEXT[cfg["lang"]]
    repo = cfg["hf_repo"] or "USER/DATASET"
    get, _ = _restore_commands(cfg, t, "data")
    lines = [t["snippet_heading"], "",
             t["snippet_where"].format(visibility=t["private"] if cfg["private"] else t["public"],
                                       url=f"https://huggingface.co/datasets/{repo}"),
             "", t["snippet_restore"], "", "```bash", *get,
             f"python data_backup/{RESTORE_SCRIPT} unpack data_backup data", "```", ""]
    if cfg["license_link"]:
        lines += [t["snippet_license"].format(link=cfg["license_link"]), ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
def write_card(out, overrides=None, mode="update", force=False, template=None):
    """Создает или обновляет карточку.

    mode="create": только если README.md нет (так вызывает pack).
    mode="update": обновить области между метками; с force=True перезаписать карточку целиком.
    Возвращает "created", "updated", "unchanged", "kept" (есть, не трогали) или "no-markers".
    """
    cfg = load_settings(out, dict(overrides or {}))
    sums = summaries(out)
    path = os.path.join(out, CARD)
    exists = os.path.exists(path)
    status = None
    if exists and mode == "create":
        status = "kept"
    elif exists and not force:
        old = read_text(path)
        new, found = update_regions(old, regions(cfg, sums))
        if not found:
            status = "no-markers"
            print(f"{path} has no shardkeep markers and was left unchanged. To keep it updated automatically, "
                  "add the lines <!-- shardkeep:begin sets --> and <!-- shardkeep:end sets --> around the sets "
                  "table, or rewrite the card with --force.")
        elif new == old:
            status = "unchanged"
        else:
            write_text(path, new)
            status = "updated"
            print(f"Dataset card updated (sections: {', '.join(found)}): {path}")
    else:
        if exists:
            shutil.copy2(path, path + ".bak")
            print(f"Previous card saved as {path}.bak")
        write_text(path, render(_template(cfg, template), values(cfg, sums)))
        status = "created"
        print(f"Dataset card created: {path}")
    snippet_path = os.path.join(out, SNIPPET)
    if mode == "update" or not os.path.exists(snippet_path):
        write_text(snippet_path, snippet(cfg))
    save_settings(out, cfg)
    return status


def copy_restore_script(out):
    """Кладет рядом с архивами отдельный скрипт восстановления pack_shards.py."""
    src = resources.files("shardkeep").joinpath("restore_script.py")
    write_text(os.path.join(out, RESTORE_SCRIPT), src.read_text(encoding="utf-8"))
