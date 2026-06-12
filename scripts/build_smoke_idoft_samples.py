#!/usr/bin/env python3
"""Run Maven build/test smoke checks for prepared IDoFT samples."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def load_rows(path: Path, limit: int | None) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[:limit] if limit else rows


def test_selector(test_identifier: str) -> str:
    if "#" in test_identifier:
        class_name, method = test_identifier.split("#", 1)
    else:
        class_name, method = test_identifier.rsplit(".", 1)
    simple_class = class_name.rsplit(".", 1)[-1]
    return f"{simple_class}#{method}"


def run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, bool]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, False
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, output + "\n[TIMEOUT]\n", True


def build_one(
    row: dict[str, object],
    index: int,
    total: int,
    mvn_path: Path,
    log_dir: Path,
    timeout: int,
    extra_mvn_args: list[str],
) -> dict[str, object]:
    started = time.time()
    sample_id = str(row["sample_id"])
    repo_dir = Path(str(row["repo_dir"]))
    module_path = str(row.get("module_path", ".") or ".")
    module_dir = repo_dir if module_path == "." else repo_dir / module_path
    selector = test_selector(str(row["test_identifier"]))
    log_path = log_dir / f"{sample_id}.log"

    cmd = [
        str(mvn_path),
        "-q",
        "-DskipITs",
        "-DskipIT",
        "-DskipCheckstyle",
        "-Dcheckstyle.skip",
        "-Drat.skip=true",
        "-Dlicense.skip=true",
        "-DskipTests=false",
        f"-Dtest={selector}",
        "test",
    ]
    cmd.extend(extra_mvn_args)

    print(f"[{index}/{total}] build smoke sample={sample_id} module={module_path} test={selector}", flush=True)
    if not module_dir.exists():
        result = {
            "sample_id": sample_id,
            "build_smoke_ok": False,
            "error": "module_missing",
            "module_dir": str(module_dir),
            "elapsed_seconds": round(time.time() - started, 2),
        }
    else:
        code, output, timed_out = run(cmd, cwd=module_dir, timeout=timeout)
        log_path.write_text(output, encoding="utf-8", errors="replace")
        result = {
            "sample_id": sample_id,
            "primary_category": row.get("primary_category"),
            "repo_dir": str(repo_dir),
            "module_path": module_path,
            "module_dir": str(module_dir),
            "test_identifier": row.get("test_identifier"),
            "maven_test_selector": selector,
            "command": " ".join(cmd),
            "returncode": code,
            "timed_out": timed_out,
            "build_smoke_ok": code == 0,
            "log_path": str(log_path),
            "elapsed_seconds": round(time.time() - started, 2),
        }
    print(
        f"[{index}/{total}] done ok={result.get('build_smoke_ok')} "
        f"returncode={result.get('returncode')} timeout={result.get('timed_out')} log={result.get('log_path', '')}",
        flush=True,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared", required=True, type=Path)
    parser.add_argument("--output-jsonl", required=True, type=Path)
    parser.add_argument("--mvn", default="tools/apache-maven-3.8.8/bin/mvn")
    parser.add_argument("--log-dir", default=Path("runs/logs/build_smoke"), type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--jobs", type=int, default=1, help="Number of Maven smoke checks to run concurrently.")
    parser.add_argument("--extra-mvn-arg", action="append", default=[])
    args = parser.parse_args()

    mvn_path = Path(args.mvn)
    if not mvn_path.is_absolute():
        mvn_path = Path.cwd() / mvn_path

    rows = load_rows(args.prepared, args.limit)
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text("", encoding="utf-8")
    args.log_dir.mkdir(parents=True, exist_ok=True)

    total = len(rows)
    indexed_rows = list(enumerate(rows, start=1))
    if args.jobs <= 1:
        results = [
            build_one(row, index, total, mvn_path, args.log_dir, args.timeout, args.extra_mvn_arg)
            for index, row in indexed_rows
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = [
                executor.submit(build_one, row, index, total, mvn_path, args.log_dir, args.timeout, args.extra_mvn_arg)
                for index, row in indexed_rows
            ]
            for future in as_completed(futures):
                results.append(future.result())

    by_sample_id = {result["sample_id"]: result for result in results}
    with args.output_jsonl.open("a", encoding="utf-8") as handle:
        for row in rows:
            result = by_sample_id[str(row["sample_id"])]
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    print(f"wrote {args.output_jsonl}", flush=True)


if __name__ == "__main__":
    main()
