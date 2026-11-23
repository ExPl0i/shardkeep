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
    "check": "one check before deleting the source data: archives, source folders and upload",
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
         profile=a.profile, use_jxl=True if a.jxl else None)
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


def run_analyze(a):
    from shardkeep.analyze import analyze
    analyze(a.root, out=a.out, depth=a.depth, top=a.top, dup_mode=a.dup_mode, dup_min_mb=a.dup_min_mb,
            sample_mb=a.sample_mb, sample_files=a.sample_files, small_kb=a.small_kb, min_report_mb=a.min_report_mb,
            no_git=a.no_git, excludes=a.exclude, rules_file=a.rules, default_rules=not a.no_default_rules,
            csv_delimiter=a.csv_delimiter)
    return 0


def add_analyze(sub):
    p = sub.add_parser("analyze", help=COMMANDS["analyze"], description=(
        "Analyze a folder before freeing disk space: sizes by folder and file category, duplicates, state of git "
        "repositories, compression estimate and recommendations. Read-only: nothing is changed or deleted."))
    p.add_argument("root", help="folder to analyze")
    p.add_argument("--out", help="report folder (default: shardkeep_report_<name>_<date> in the current folder)")
    p.add_argument("--depth", type=int, default=2, help="folder depth for the statistics (default 2)")
    p.add_argument("--top", type=int, default=300, help="how many largest files to list (default 300)")
    p.add_argument("--dup-mode", choices=["full", "quick", "off"], default="full",
                   help="duplicate search: full (full hash), quick (partial hash) or off")
    p.add_argument("--dup-min-mb", type=float, default=1.0, help="minimum file size for duplicate search, MB")
    p.add_argument("--sample-mb", type=int, default=384,
                   help="how many MB to read for the compression estimate (0: no estimate)")
    p.add_argument("--sample-files", type=int, default=12, help="sample files per folder and category")
    p.add_argument("--small-kb", type=int, default=512, help="files smaller than this are counted as small, KB")
    p.add_argument("--min-report-mb", type=int, default=50, help="folders smaller than this get no details, MB")
    p.add_argument("--no-git", action="store_true", help="do not inspect git repositories")
    p.add_argument("--exclude", action="append", default=[], help="name or path pattern to skip (repeatable)")
    p.add_argument("--rules", help="your own TOML file with categories and rules (checked before the built-in ones)")
    p.add_argument("--no-default-rules", action="store_true", help="use only the rules from --rules")
    p.add_argument("--csv-delimiter", default=",", help="CSV column separator (default \",\"; use \";\" for Excel "
                   "with a comma as the decimal separator)")
    p.set_defaults(func=run_analyze)


def run_compare_zip(a):
    from shardkeep.compare_zip import compare_zip
    return 0 if compare_zip(a.zip, a.folder, crc=a.crc, test=a.test).complete else 1


def add_compare_zip(sub):
    p = sub.add_parser("compare-zip", help=COMMANDS["compare-zip"], description=(
        "Check that a zip archive contains every file of an extracted folder, before deleting one of them. "
        "Files are matched by name and path; the archive may have an extra top-level folder. Read-only."))
    p.add_argument("zip", help="zip archive")
    p.add_argument("folder", help="extracted folder")
    p.add_argument("--crc", action="store_true",
                   help="also compare CRC32 checksums (reads every file of the folder, the archive is not extracted)")
    p.add_argument("--test", action="store_true", help="also test archive integrity (decompresses it in memory)")
    p.set_defaults(func=run_compare_zip)


def _target(p, required=True):
    g = p.add_mutually_exclusive_group(required=required)
    g.add_argument("--hf", metavar="USER/DATASET", help="Hugging Face dataset repository")
    g.add_argument("--rclone", metavar="REMOTE:PATH", help="rclone target, for example yadisk:backup/dataset")


def run_upload(a):
    from shardkeep.card import load_settings
    from shardkeep.remote import check_ready
    check_ready(a.out, force=a.force)
    if a.hf:
        from shardkeep.remote import hf
        private = False if a.public else True if a.private else load_settings(a.out, {})["private"]
        hf.upload(a.out, a.hf, private=private)
    else:
        from shardkeep.remote import rclone
        rclone.upload(a.out, a.rclone)
    print(f"\nNext step: check the upload\n  shardkeep check-upload \"{a.out}\" "
          + (f"--hf {a.hf}" if a.hf else f"--rclone {a.rclone}"))
    return 0


def run_check_upload(a):
    from shardkeep.remote import print_check
    if a.hf:
        from shardkeep.remote import hf
        res = hf.check(a.out, a.hf, compute_hashes=not a.size_only)
    else:
        from shardkeep.remote import rclone
        res = rclone.check(a.out, a.rclone, compute_hashes=not a.size_only)
    print_check(res)
    return 0 if res.ok else 1


def run_check(a):
    from shardkeep.check import check
    ok = check(a.out, name=a.name, src=a.src, hf=a.hf, rclone=a.rclone, allow_skipped=a.allow_skipped,
               compute_hashes=not a.size_only)
    return 0 if ok else 1


def add_upload(sub):
    p = sub.add_parser("upload", help=COMMANDS["upload"], description=(
        "Upload the archive folder to a Hugging Face dataset (created if needed) or to any rclone target. "
        "Files that are already uploaded with the same content are skipped, so an interrupted upload can be "
        "resumed by running the same command again. Log in to Hugging Face first: hf auth login."))
    p.add_argument("out", help="archive folder")
    _target(p)
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--private", action="store_true", help="create a private dataset (default)")
    vis.add_argument("--public", action="store_true", help="create a public dataset")
    p.add_argument("--force", action="store_true", help="upload even if some sets are not fully packed")
    p.set_defaults(func=run_upload)


def add_check_upload(sub):
    p = sub.add_parser("check-upload", help=COMMANDS["check-upload"], description=(
        "Compare the uploaded files with the local archive folder: file list, sizes and, where the storage "
        "provides it, SHA-256 (Hugging Face always does for archives). Nothing is downloaded or changed."))
    p.add_argument("out", help="archive folder")
    _target(p)
    p.add_argument("--size-only", action="store_true", help="compare sizes only, do not read local files")
    p.set_defaults(func=run_check_upload)


def add_check(sub):
    p = sub.add_parser("check", help=COMMANDS["check"], description=(
        "Run every check that matters before deleting the source data: archives are intact and match their "
        "manifests, every source folder is fully packed and unchanged (the folder saved with each set, or --src), "
        "and the archives are uploaded (--hf or --rclone). Prints a final verdict and writes check_report.txt."))
    p.add_argument("out", help="archive folder")
    p.add_argument("--name", help="check only this set")
    p.add_argument("--src", help="source folder (default: the folder each set was packed from)")
    _target(p, required=False)
    p.add_argument("--allow-skipped", action="store_true",
                   help="do not treat files that could not be read during packing as an error")
    p.add_argument("--size-only", action="store_true", help="compare uploaded files by size only")
    p.set_defaults(func=run_check)


def run_verify(a):
    from shardkeep.verify import verify
    return 0 if verify(a.out, name=a.name, src=a.src, allow_skipped=a.allow_skipped).ok else 1


def run_unpack(a):
    from shardkeep.unpack import unpack
    unpack(a.out, a.dest, name=a.name, patterns=a.pattern, flat=a.flat, keep_jxl=a.keep_jxl)
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
    p.add_argument("--jxl", action="store_true",
                   help="convert JPEG files to JPEG XL losslessly (about 20%% smaller, restored byte for byte; needs "
                        "cjxl and djxl from libjxl for packing and restoring)")
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
    p.add_argument("--keep-jxl", action="store_true",
                   help="extract JPEG XL files as .jxl instead of restoring the original JPEG files (no djxl needed)")
    p.set_defaults(func=run_unpack)


ADDERS = {"upload": add_upload, "check-upload": add_check_upload, "check": add_check,
          "analyze": add_analyze, "compare-zip": add_compare_zip, "pack": add_pack, "verify": add_verify, "unpack": add_unpack, "card": add_card}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="shardkeep",
        description="Pack large data folders into compressed shards, verify, upload and restore them.",
    )
    ap.add_argument("--version", action="version", version=f"shardkeep {__version__}")
    ap.add_argument("--no-progress", action="store_true",
                    help="do not show progress lines (also: environment variable SHARDKEEP_NO_PROGRESS=1)")
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
    if a.no_progress:
        from shardkeep import progress
        progress.ENABLED = False
    try:
        code = a.func(a)
    except UserError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        code = exc.code
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to continue.", file=sys.stderr)
        code = 130
    sys.exit(code or 0)
