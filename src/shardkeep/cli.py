"""Командная строка shardkeep: одна команда и подкоманды."""

import argparse
import sys

from shardkeep import __version__
from shardkeep.errors import UserError
from shardkeep.profiles import PROFILES

COMMANDS = {
    "analyze": "analyze a folder: sizes, file categories, duplicates, git repositories, compression estimate",
    "pack": "pack a folder into compressed shards",
    "verify": "verify archives against their manifests",
    "unpack": "extract archives",
    "compare-zip": "check that a zip archive matches an extracted folder",
    "upload": "upload archives to Hugging Face or through rclone",
    "check-upload": "compare uploaded archives with the local ones",
    "card": "create or update the dataset card",
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
         long_mode=False if a.no_long else None, shard_gb=a.shard_gb, excludes=a.exclude, include_env=a.include_env,
         profile=a.profile)
    from shardkeep.card import copy_restore_script, write_card
    copy_restore_script(a.out)
    write_card(a.out, card_overrides(a), mode="create")
    print(f"\nDone. Next step: verify the archives\n"
          f'  shardkeep verify "{a.out}" --src "{a.src}"' + (f" --name {a.name}" if a.name else ""))
    return 0


def card_overrides(a):
    o = {"title": a.title, "hf_repo": a.hf_repo, "github_url": a.github_url, "lang": a.lang}
    for key in ("license", "license_name", "license_link"):
        o[key] = getattr(a, key, None)
    if getattr(a, "tag", None):
        o["tags"] = a.tag
    if getattr(a, "license_file", None):
        from shardkeep.fsutil import read_text
        o["license_text"] = read_text(a.license_file)
    if getattr(a, "public", False):
        o["private"] = False
    elif getattr(a, "private", False):
        o["private"] = True
    return o


def run_card(a):
    from shardkeep.card import copy_restore_script, write_card
    from shardkeep.layout import list_names, settings_file
    from shardkeep.pack import write_summary
    import os
    for name in list_names(a.out):
        if os.path.exists(settings_file(a.out, name)):
            write_summary(a.out, name)
    copy_restore_script(a.out)
    status = write_card(a.out, card_overrides(a), mode="update", force=a.force, template=a.template)
    return 1 if status == "no-markers" else 0


def add_card_options(p, full):
    p.add_argument("--title", help="dataset card title")
    p.add_argument("--hf-repo", help="Hugging Face dataset repository, for example user/dataset")
    p.add_argument("--github-url", help="URL of the code repository the data belongs to")
    p.add_argument("--lang", choices=["en", "ru"], help="dataset card language (default en)")
    if not full:
        return
    p.add_argument("--license", help="license identifier for the card metadata, for example cc-by-4.0 or other")
    p.add_argument("--license-name", help="license name when --license is other")
    p.add_argument("--license-link", help="link to the license terms")
    p.add_argument("--license-file", help="text file with license terms to put into the card")
    p.add_argument("--tag", action="append", help="tag for the card metadata (repeatable)")
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--private", action="store_true", help="the dataset is private (default)")
    vis.add_argument("--public", action="store_true", help="the dataset is public")
    p.add_argument("--template", help="custom card template (placeholders {{title}}, {{sets}}, {{restore}} and others)")
    p.add_argument("--force", action="store_true",
                   help="rewrite the whole card from the template (the old one is kept as README.md.bak)")


def add_card(sub):
    p = sub.add_parser("card", help=COMMANDS["card"], description=(
        "Create the dataset card README.md in the archive folder, or update the parts between the "
        "<!-- shardkeep:begin ... --> and <!-- shardkeep:end ... --> markers. Text outside the markers is never "
        "changed. Settings are remembered in card_settings.json."))
    p.add_argument("out", help="archive folder")
    add_card_options(p, full=True)
    p.set_defaults(func=run_card)


def run_verify(a):
    from shardkeep.verify import verify
    return 0 if verify(a.out, name=a.name, src=a.src, allow_skipped=a.allow_skipped).ok else 1


def run_unpack(a):
    from shardkeep.unpack import unpack
    unpack(a.out, a.dest, name=a.name, patterns=a.pattern, flat=a.flat)
    return 0


def add_pack(sub):
    p = sub.add_parser("pack", help=COMMANDS["pack"], description=COMMANDS["pack"])
    p.add_argument("src", help="folder with the data")
    p.add_argument("out", help="folder for the archives (preferably on another disk)")
    p.add_argument("--name", help="set name (default: folder name)")
    p.add_argument("--shard-gb", type=float,
                   help="source data per shard, GB (default 4)")
    p.add_argument("--profile", choices=list(PROFILES),
                   help="compression profile (default auto): "
                        + "; ".join(f"{k}: {v.title}" for k, v in PROFILES.items()))
    p.add_argument("--codec", choices=["auto", "zstd", "xz"], default="auto",
                   help="zstd (default) or xz: compresses as well, but several times slower")
    p.add_argument("--level", type=int,
                   help="level for well-compressible files: zstd 1-22, xz 0-9 (default: from the profile)")
    p.add_argument("--threads", type=int, default=0,
                   help="zstd threads (0: one per CPU core, limited by free memory)")
    p.add_argument("--no-long", action="store_true", help="disable zstd long-distance matching")
    p.add_argument("--exclude", action="append", default=[], help="name or path pattern to skip (repeatable)")
    p.add_argument("--include-env", action="store_true",
                   help="do not skip __pycache__, .git, venv and similar folders")
    add_card_options(p, full=False)
    p.set_defaults(func=run_pack)


def add_verify(sub):
    p = sub.add_parser("verify", help=COMMANDS["verify"], description=COMMANDS["verify"])
    p.add_argument("out", help="archive folder")
    p.add_argument("--name", help="verify only this set")
    p.add_argument("--src", help="source folder: check that all its files are packed and unchanged since packing")
    p.add_argument("--allow-skipped", action="store_true",
                   help="do not treat files that could not be read during packing as an error")
    p.set_defaults(func=run_verify)


def add_unpack(sub):
    p = sub.add_parser("unpack", help=COMMANDS["unpack"], description=COMMANDS["unpack"])
    p.add_argument("out", help="archive folder")
    p.add_argument("dest", help="destination folder")
    p.add_argument("--name", help="extract only this set")
    p.add_argument("--pattern", action="append",
                   help='extract only files matching a pattern, for example "*/scene_001*" (repeatable)')
    p.add_argument("--flat", action="store_true", help="do not create a subfolder named after the set")
    p.set_defaults(func=run_unpack)


ADDERS = {"pack": add_pack, "verify": add_verify, "unpack": add_unpack, "card": add_card}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="shardkeep",
        description="Pack large data folders into compressed shards, verify, upload and restore them.",
    )
    ap.add_argument("--version", action="version", version=f"shardkeep {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command")
    for name, help_text in COMMANDS.items():
        if name in ADDERS:
            ADDERS[name](sub)
        else:
            p = sub.add_parser(name, help=help_text, description=help_text)
            p.set_defaults(func=_not_ready)
    return ap


def _not_ready(a):
    raise UserError(f"Command {a.cmd} is not implemented yet.")


def main(argv=None):
    setup_console()
    a = build_parser().parse_args(argv)
    try:
        code = a.func(a)
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = exc.code
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue.", file=sys.stderr)
        code = 130
    sys.exit(code or 0)
