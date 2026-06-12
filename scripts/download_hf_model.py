#!/usr/bin/env python3
"""Download a HuggingFace model snapshot and print the local snapshot path."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Qwen/Qwen3-Coder-30B-A3B-Instruct")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--revision", default="")
    parser.add_argument("--max-workers", type=int, default=4)
    args = parser.parse_args()

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is not installed. Install it with `pip install huggingface_hub` "
            "or run `bash scripts/create_vllm_env.sh` first."
        ) from exc

    kwargs = {
        "repo_id": args.model,
        "max_workers": args.max_workers,
        "local_files_only": False,
    }
    if args.cache_dir:
        kwargs["cache_dir"] = str(Path(args.cache_dir).expanduser())
    if args.revision:
        kwargs["revision"] = args.revision

    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "60")
    path = snapshot_download(**kwargs)
    print(path)


if __name__ == "__main__":
    main()
