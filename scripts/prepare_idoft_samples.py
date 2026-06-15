#!/usr/bin/env python3
"""Prepare IDoFT samples by cloning, checking out commits, and locating tests.

Developer patches are evaluation-only artifacts. They are not downloaded by
default because the repair pipeline must run only on pre-fix information.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TEST_FIELD = "Fully-Qualified Test Name (packageName.ClassName.methodName)"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout


def append_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_rows(path: Path, limit: int | None) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[:limit] if limit else rows


def download(url: str, path: Path, timeout: int = 120) -> tuple[bool, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url)
    try:
        with urlopen(req, timeout=timeout) as response:
            data = response.read()
        path.write_bytes(data)
        return True, f"downloaded {len(data)} bytes"
    except (HTTPError, URLError, TimeoutError) as exc:
        return False, repr(exc)


def candidate_test_paths(repo_dir: Path, module_path: str, test_class: str, simple_class: str) -> list[Path]:
    module_dir = repo_dir if module_path in {"", "."} else repo_dir / module_path
    package_path = Path(*test_class.split("."))
    candidates = [
        module_dir / "src/test/java" / package_path.with_suffix(".java"),
        module_dir / "src/test/groovy" / package_path.with_suffix(".groovy"),
        module_dir / "src/test/scala" / package_path.with_suffix(".scala"),
    ]
    found = [path for path in candidates if path.exists()]
    if found:
        return found
    if module_dir.exists():
        found = list(module_dir.rglob(f"{simple_class}.java"))
        found.extend(module_dir.rglob(f"{simple_class}.groovy"))
        found.extend(module_dir.rglob(f"{simple_class}.scala"))
    return found


def patch_touches_test(patch_path: Path) -> bool:
    if not patch_path.exists():
        return False
    text = patch_path.read_text(encoding="utf-8", errors="replace")
    return "src/test/" in text or "test/" in text.lower()


def prepare_row(row: dict[str, str], args: argparse.Namespace, index: int, total: int) -> dict[str, object]:
    sample_id = row["sample_id"]
    repo_url = row["Project URL"].strip()
    sha = row["SHA Detected"].strip()
    module_path = row.get("Module Path", "").strip() or "."
    repo_dir = args.workdir / sample_id / "repo"
    cache_repo_dir = args.repo_cache_dir / row["repo_slug"].replace("/", "__") if args.repo_cache_dir else None
    patch_path = args.patch_dir / f"{sample_id}.patch"
    log_dir = args.log_dir / sample_id
    log_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    print(f"[{index}/{total}] {sample_id} clone/checkout {repo_url} @ {sha}", flush=True)

    result: dict[str, object] = {
        "sample_id": sample_id,
        "repo_url": repo_url,
        "sha_detected": sha,
        "module_path": module_path,
        "test_identifier": row.get(TEST_FIELD, ""),
        "primary_category": row.get("PrimaryCategory", row.get("Category", "")),
        "repo_dir": str(repo_dir),
    }

    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if cache_repo_dir and cache_repo_dir.exists():
            print(f"[{index}/{total}] {sample_id} copy cached repo {cache_repo_dir}", flush=True)
            shutil.copytree(cache_repo_dir, repo_dir, symlinks=True)
            result["clone_ok"] = True
            result["clone_source"] = "repo_cache"
            (log_dir / "clone.log").write_text(f"copied from cache: {cache_repo_dir}\n", encoding="utf-8")
        elif args.no_network:
            result["clone_ok"] = False
            result["clone_source"] = "none"
            result["error"] = "clone_skipped_no_network_and_missing_cache"
            result["elapsed_seconds"] = round(time.time() - started, 2)
            return result
        else:
            code, output = run(["git", "clone", "--no-tags", "--filter=blob:none", repo_url, str(repo_dir)], timeout=args.clone_timeout)
            (log_dir / "clone.log").write_text(output, encoding="utf-8")
            result["clone_ok"] = code == 0
            result["clone_source"] = "network"
            if code != 0:
                result["error"] = "clone_failed"
                result["elapsed_seconds"] = round(time.time() - started, 2)
                return result
    else:
        result["clone_ok"] = True
        result["clone_source"] = "existing_workdir"

    code, output = run(["git", "checkout", sha], cwd=repo_dir, timeout=args.git_timeout)
    (log_dir / "checkout.log").write_text(output, encoding="utf-8")
    result["checkout_ok"] = code == 0
    if code != 0:
        result["error"] = "checkout_failed"
        result["elapsed_seconds"] = round(time.time() - started, 2)
        return result

    module_dir = repo_dir if module_path == "." else repo_dir / module_path
    result["module_exists"] = module_dir.exists()

    test_paths = candidate_test_paths(repo_dir, module_path, row["test_class"], row["test_simple_class"])
    result["test_file_found"] = bool(test_paths)
    result["test_file_candidates"] = [str(path) for path in test_paths[:10]]

    patch_url = row.get("PR Link", "").rstrip("/") + ".patch" if row.get("PR Link", "").strip() else ""
    if args.download_developer_patches:
        result["developer_patch_url"] = patch_url
        result["developer_patch_path"] = str(patch_path)
        cached_patch = args.patch_cache_dir / f"{sample_id}.patch" if args.patch_cache_dir else None
        if cached_patch and cached_patch.exists():
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached_patch, patch_path)
            result["developer_patch_download_ok"] = True
            result["developer_patch_download_message"] = f"copied from patch cache: {cached_patch}"
            result["developer_patch_touches_test"] = patch_touches_test(patch_path)
        elif patch_url and not args.no_network:
            print(f"[{index}/{total}] {sample_id} download developer patch {patch_url}", flush=True)
            ok, msg = download(patch_url, patch_path, timeout=args.download_timeout)
            result["developer_patch_download_ok"] = ok
            result["developer_patch_download_message"] = msg
            result["developer_patch_touches_test"] = patch_touches_test(patch_path)
        elif patch_url and args.no_network:
            result["developer_patch_download_ok"] = False
            result["developer_patch_download_message"] = "developer patch download skipped by --no-network and cache missing"
            result["developer_patch_touches_test"] = False
        else:
            result["developer_patch_download_ok"] = False
            result["developer_patch_download_message"] = "missing PR link"
            result["developer_patch_touches_test"] = False
    else:
        result["developer_patch_download_ok"] = False
        result["developer_patch_download_message"] = (
            "skipped; pass --download-developer-patches for evaluation-only artifacts"
        )

    result["elapsed_seconds"] = round(time.time() - started, 2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--workdir", default=Path("data/worktrees/idoft"), type=Path)
    parser.add_argument("--patch-dir", default=Path("data/patches/idoft"), type=Path)
    parser.add_argument("--repo-cache-dir", type=Path, help="Directory containing cached repos named as owner__repo.")
    parser.add_argument("--patch-cache-dir", type=Path, help="Directory containing cached patches named as sample_id.patch.")
    parser.add_argument("--log-dir", default=Path("runs/logs/prepare_idoft"), type=Path)
    parser.add_argument("--output-jsonl", default=Path("runs/prepare_idoft_samples.jsonl"), type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-network", action="store_true", help="Do not clone or download patches over the network.")
    parser.add_argument("--clone-timeout", type=int, default=900)
    parser.add_argument("--git-timeout", type=int, default=300)
    parser.add_argument("--download-timeout", type=int, default=120)
    parser.add_argument(
        "--download-developer-patches",
        action="store_true",
        help="Download PR/developer patches for offline evaluation only. Never use these artifacts in repair prompts.",
    )
    args = parser.parse_args()

    rows = load_rows(args.metadata, args.limit)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text("", encoding="utf-8")

    total = len(rows)
    for index, row in enumerate(rows, start=1):
        try:
            result = prepare_row(row, args, index, total)
        except Exception as exc:  # Keep long preparation jobs moving.
            result = {
                "sample_id": row.get("sample_id", f"row_{index}"),
                "error": "unhandled_exception",
                "exception": repr(exc),
            }
        append_jsonl(args.output_jsonl, result)
        print(
            "[{}/{}] {} clone={} checkout={} module={} test_file={} developer_patch={} touches_test={} error={}".format(
                index,
                total,
                result.get("sample_id"),
                result.get("clone_ok"),
                result.get("checkout_ok"),
                result.get("module_exists"),
                result.get("test_file_found"),
                result.get("developer_patch_download_ok"),
                result.get("developer_patch_touches_test", False),
                result.get("error", ""),
            ),
            flush=True,
        )

    print(f"wrote {args.output_jsonl}", flush=True)


if __name__ == "__main__":
    main()
