"""shardkeep analyze: разбор папки перед освобождением места (только чтение)."""

import datetime as dt
import os
import time

from shardkeep.analyze.report import build_report
from shardkeep.analyze.rules import load_rules
from shardkeep.analyze.scan import Scanner, estimate_compression, find_duplicates, inspect_repos
from shardkeep.errors import UserError
from shardkeep.textfmt import human, num


def analyze(root, out=None, depth=2, top=300, dup_mode="full", dup_min_mb=1.0, sample_mb=384, sample_files=12,
            small_kb=512, min_report_mb=50, no_git=False, excludes=(), rules_file=None, default_rules=True, csv_delimiter=","):
    if not os.path.isdir(root):
        raise UserError(f"Folder not found: {root}")
    if not out:
        name = os.path.basename(os.path.abspath(root).rstrip("\\/")) or "root"
        out = os.path.abspath(f"shardkeep_report_{name}_{dt.datetime.now():%Y%m%d_%H%M}")
    rules = load_rules(rules_file, default_rules)
    t0 = time.monotonic()
    print(f"Scanning {root}")
    sc = Scanner(root, rules, depth, top, int(dup_min_mb * 2**20), small_kb * 1024, list(excludes),
                 sample_files if sample_mb > 0 else 0)
    sc.scan()
    print(f"Found {num(sc.total_files)} files, {human(sc.total_bytes)}")
    dups = find_duplicates(sc, dup_mode)
    repos = [] if no_git else inspect_repos(sc)
    if repos:
        print(f"Git repositories checked: {len(repos)}")
    per_group, per_cat, npz = estimate_compression(sc, sample_mb * 2**20)
    opts = {"dup_mode": dup_mode, "dup_min_mb": dup_min_mb, "small_kb": small_kb, "min_report_mb": min_report_mb,
            "no_git": no_git, "csv_delimiter": csv_delimiter}
    build_report(out, opts, sc, dups, repos, per_group, per_cat, npz, time.monotonic() - t0)
    print(f"Done. Report: {os.path.join(out, 'report.md')}")
    return out
