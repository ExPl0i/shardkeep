"""Hugging Face: загрузка в датасет и сверка по списку файлов репозитория.

Нужен пакет huggingface_hub (pip install "shardkeep[hf]") и вход в аккаунт: hf auth login
или переменная окружения HF_TOKEN. Лучше ставить его в отдельную виртуальную среду: версия 2.x
конфликтует с некоторыми пакетами (например, tokenizers требует версию ниже 2.0).
"""

from shardkeep.errors import UserError
from shardkeep.remote import RemoteFile, batches, compare, describe, local_files

REPO_TYPE = "dataset"


def _api():
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise UserError('Hugging Face support needs the huggingface_hub package: pip install "shardkeep[hf]". '
                        "Install it in a separate virtual environment if it conflicts with your other packages.") \
            from exc
    return HfApi()


def _not_found_errors():
    try:
        from huggingface_hub.errors import RepositoryNotFoundError
        return (RepositoryNotFoundError,)
    except ImportError:  # pragma: no cover
        return ()


def list_remote(repo, api=None):
    api = api or _api()
    remote = {}
    try:
        items = list(api.list_repo_tree(repo, repo_type=REPO_TYPE, recursive=True))
    except _not_found_errors() as exc:
        raise UserError(f"Dataset {repo} not found on Hugging Face, or you have no access to it "
                        "(log in with: hf auth login)") from exc
    for item in items:
        size = getattr(item, "size", None)
        if size is None:  # папка
            continue
        lfs = getattr(item, "lfs", None)
        sha256 = getattr(lfs, "sha256", None) if lfs is not None else None
        git_sha1 = None if lfs is not None else getattr(item, "blob_id", None)
        remote[item.path] = RemoteFile(item.path, size, sha256=sha256, git_sha1=git_sha1)
    return remote


def check(out, repo, compute_hashes=True, api=None):
    return compare(f"Hugging Face dataset {repo}", local_files(out), list_remote(repo, api), compute_hashes)


def upload(out, repo, private=True, api=None):
    """Загружает недостающие и измененные файлы порциями; каждая порция – отдельный коммит."""
    api = api or _api()
    exists = True
    try:
        info = api.repo_info(repo, repo_type=REPO_TYPE)
    except _not_found_errors():
        exists = False
        info = None
    if not exists:
        print(f"Creating {'private' if private else 'PUBLIC'} dataset {repo} on Hugging Face")
        api.create_repo(repo, repo_type=REPO_TYPE, private=private, exist_ok=True)
    else:
        is_private = getattr(info, "private", None)
        if is_private is not None and bool(is_private) != bool(private):
            print(f"Warning: dataset {repo} already exists and is {'private' if is_private else 'PUBLIC'}; "
                  "its visibility is not changed. Change it in the dataset settings on huggingface.co if needed.")
    local = local_files(out)
    pending = local
    if exists:
        res = compare(repo, local, list_remote(repo, api), compute_hashes=True)
        todo = res.problem_paths()
        pending = [f for f in local if f.rel in todo]
    if not pending:
        print(f"Everything is already uploaded to {repo}.")
        return 0
    print(f"Uploading to {repo}: {describe(pending)}")
    parts = list(batches(pending))
    for k, batch in enumerate(parts, 1):
        print(f"  part {k}/{len(parts)}: {describe(batch)}")
        api.upload_folder(repo_id=repo, repo_type=REPO_TYPE, folder_path=out,
                          allow_patterns=[f.rel for f in batch],
                          commit_message=f"shardkeep upload {k}/{len(parts)}")
    print(f"Done: https://huggingface.co/datasets/{repo}")
    return len(pending)
