#!/usr/bin/env python3
"""Summarize Maven build-smoke results and classify common failures."""

import argparse
import csv
import json
from pathlib import Path


def classify_log(text):
    lowered = text.lower()
    if "non-resolvable parent pom" in lowered or "non-resolvable import pom" in lowered:
        if "401 unauthorized" in lowered or "authentication failed" in lowered:
            return "dependency_private_or_auth"
        return "dependency_resolution"
    if "could not transfer artifact" in lowered or "could not find artifact" in lowered:
        return "dependency_resolution"
    if "compilation failure" in lowered or "compilation error" in lowered:
        return "compilation_failure"
    if "not exists" in lowered and ("target/test-classes" in lowered or "src/test/resources" in lowered):
        return "missing_test_resource"
    if "there are test failures" in lowered or "tests in error" in lowered:
        return "test_failure_or_error"
    if "no tests" in lowered:
        return "test_selector_or_no_tests"
    return "unknown_failure"


def first_interesting_lines(text, limit=3):
    patterns = (
        "[ERROR]",
        "<<< FAILURE!",
        "Tests run:",
        "Tests in error:",
        "Tests in failure:",
        "BUILD FAILURE",
    )
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(pattern in line for pattern in patterns):
            lines.append(line)
        if len(lines) >= limit:
            break
    return " | ".join(lines)


def resolve_log_path(log_path, logs_root):
    path = Path(log_path)
    if path.exists():
        return path
    if logs_root:
        candidate = Path(logs_root) / path.name
        if candidate.exists():
            return candidate
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True, type=Path)
    parser.add_argument("--logs-root", type=Path)
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    rows = []
    for line in args.input_jsonl.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            log_file = resolve_log_path(record.get("log_path", ""), args.logs_root)
            log_text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
            status = "passed" if record.get("build_smoke_ok") else classify_log(log_text)
            rows.append(
                {
                    "sample_id": record.get("sample_id", ""),
                    "category": record.get("primary_category", ""),
                    "build_smoke_ok": bool(record.get("build_smoke_ok")),
                    "failure_class": "" if record.get("build_smoke_ok") else status,
                    "elapsed_seconds": record.get("elapsed_seconds", ""),
                    "module_path": record.get("module_path", ""),
                    "test_identifier": record.get("test_identifier", ""),
                    "log_path": str(log_file),
                    "summary": "" if record.get("build_smoke_ok") else first_interesting_lines(log_text),
                }
            )

    counts = {}
    for row in rows:
        key = "passed" if row["build_smoke_ok"] else row["failure_class"]
        counts[key] = counts.get(key, 0) + 1

    summary = {
        "input_rows": len(rows),
        "passed": sum(1 for row in rows if row["build_smoke_ok"]),
        "failed": sum(1 for row in rows if not row["build_smoke_ok"]),
        "counts": counts,
    }

    if args.output_csv:
        args.output_csv.parent.mkdir(parents=True, exist_ok=True)
        with args.output_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
            if rows:
                writer.writeheader()
                writer.writerows(rows)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
