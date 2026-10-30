#!/usr/bin/env python3
"""
Sync checkpoints / data shards between a GPU box and Hugging Face Hub.

Token is read from the HF_TOKEN environment variable (never pass it as an argument).
Repos are created PRIVATE unless you pass --public.

  push   LOCAL_DIR REPO [--type model|dataset] [--large] [--keep N] [--public]
  pull   REPO LOCAL_DIR [--type model|dataset]
  watch  LOCAL_DIR REPO [--every SECONDS] [--keep N] ...   (push in a loop)
  latest LOCAL_DIR                                          (print newest checkpoint path)

Checkpoint naming convention (so `latest` and --keep work):
  step_000500/   checkpoint-500/   ckpt_500.pt   (a number after step/checkpoint/ckpt)
Write new checkpoints to a ".tmp_*" name first, then rename to the final name, so a sync
never uploads a half-written checkpoint (".tmp*" is ignored).
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

try:
    from huggingface_hub.errors import RepositoryNotFoundError
except ImportError:  # older huggingface_hub
    from huggingface_hub.utils import RepositoryNotFoundError

STEP_RE = re.compile(r"^(?:step|checkpoint|ckpt)[-_]?(\d+)(?:\.\w+)?$")

# Safety net: never upload stimulus videos or partial files by accident.
DEFAULT_IGNORE = [
    "*.mp4", "*.mkv", "*.avi", "*.mov", "*.webm", "*.m4v",
    ".tmp*", "*.tmp", "__pycache__", "*.pyc", ".cache",
]


def token() -> str:
    tok = os.environ.get("HF_TOKEN")
    if not tok:
        sys.exit("HF_TOKEN is not set in the environment.")
    return tok


def step_of(name: str):
    m = STEP_RE.match(name)
    return int(m.group(1)) if m else None


def latest(local_dir: str):
    base = Path(local_dir)
    if not base.is_dir():
        return None
    best, best_step = None, -1
    for child in base.iterdir():
        s = step_of(child.name)
        if s is not None and s > best_step:
            best, best_step = child, s
    return best


def prune_remote(api: HfApi, repo: str, repo_type: str, keep: int) -> None:
    """Keep only the `keep` newest step-numbered checkpoints on the Hub."""
    tops = {}  # name -> is_dir
    for path in api.list_repo_files(repo, repo_type=repo_type):
        top = path.split("/")[0]
        if step_of(top) is not None:
            tops[top] = tops.get(top, False) or "/" in path
    ordered = sorted(tops, key=step_of, reverse=True)
    for old in ordered[keep:]:
        print(f"[hf_sync] deleting old remote checkpoint {old}", flush=True)
        if tops[old]:
            api.delete_folder(old, repo_id=repo, repo_type=repo_type,
                              commit_message=f"prune {old}")
        else:
            api.delete_file(old, repo_id=repo, repo_type=repo_type,
                            commit_message=f"prune {old}")


def push(a) -> None:
    api = HfApi(token=token())
    api.create_repo(a.repo, repo_type=a.type, private=not a.public, exist_ok=True)
    ignore = DEFAULT_IGNORE + (a.ignore or [])
    if a.keep:
        # don't re-upload old local checkpoints that remote pruning would delete again
        olds = sorted((c.name for c in Path(a.local).iterdir() if step_of(c.name) is not None),
                      key=step_of, reverse=True)[a.keep:]
        for name in olds:
            ignore += [name, f"{name}/*"]
    if a.large:
        # resumable, handles many/big files (datasets, shards)
        api.upload_large_folder(repo_id=a.repo, folder_path=a.local,
                                repo_type=a.type, ignore_patterns=ignore)
    else:
        api.upload_folder(repo_id=a.repo, folder_path=a.local, repo_type=a.type,
                          ignore_patterns=ignore,
                          commit_message=f"sync {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if a.keep:
        prune_remote(api, a.repo, a.type, a.keep)
    print(f"[hf_sync] pushed {a.local} -> {a.repo}", flush=True)


def pull(a) -> None:
    Path(a.local).mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(repo_id=a.repo, repo_type=a.type, local_dir=a.local,
                          token=token(), max_workers=8)
    except RepositoryNotFoundError:
        print(f"[hf_sync] repo {a.repo} not found - nothing to pull (first run?)")
        return
    print(f"[hf_sync] pulled {a.repo} -> {a.local}", flush=True)


def watch(a) -> None:
    while True:
        try:
            if Path(a.local).is_dir() and any(Path(a.local).iterdir()):
                push(a)
        except Exception as e:  # keep looping on transient network/API errors
            print(f"[hf_sync] push failed: {e!r}", flush=True)
        time.sleep(a.every)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, local_first):
        if local_first:
            sp.add_argument("local"); sp.add_argument("repo")
        else:
            sp.add_argument("repo"); sp.add_argument("local")
        sp.add_argument("--type", choices=["model", "dataset"], default="model")

    sp = sub.add_parser("push"); common(sp, True)
    sp.add_argument("--large", action="store_true", help="resumable upload for big/many files")
    sp.add_argument("--keep", type=int, default=0, help="keep only N newest step checkpoints remotely")
    sp.add_argument("--public", action="store_true", help="create repo public (default private)")
    sp.add_argument("--ignore", action="append", help="extra ignore pattern (repeatable)")
    sp.set_defaults(fn=push)

    sp = sub.add_parser("pull"); common(sp, False); sp.set_defaults(fn=pull)

    sp = sub.add_parser("watch"); common(sp, True)
    sp.add_argument("--every", type=int, default=600)
    sp.add_argument("--large", action="store_true")
    sp.add_argument("--keep", type=int, default=0)
    sp.add_argument("--public", action="store_true")
    sp.add_argument("--ignore", action="append")
    sp.set_defaults(fn=watch)

    sp = sub.add_parser("latest"); sp.add_argument("local")
    sp.set_defaults(fn=lambda a: (lambda r: (print(r), None)[1] if r else sys.exit(1))(latest(a.local)))

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
