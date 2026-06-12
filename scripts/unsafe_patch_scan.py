#!/usr/bin/env python3
"""Heuristic scanner for unsafe flaky-test repair patches."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RULES: list[tuple[str, re.Pattern[str]]] = [
    ("skip_or_disable_test", re.compile(r"^\+.*(@Disabled|@Ignore|pytest\.mark\.skip|\.skip\(|skipTest\()", re.I)),
    ("deleted_test_annotation", re.compile(r"^-.*(@Test|def test_|it\(|test\()", re.I)),
    ("deleted_assertion", re.compile(r"^-.*\b(assert|assertThat|assertEquals|assertTrue|expect\()", re.I)),
    ("trivial_assertion", re.compile(r"^\+.*\b(assertTrue\s*\(\s*true\s*\)|assert\s+True|expect\(true\))", re.I)),
    ("fixed_sleep", re.compile(r"^\+.*\b(Thread\.sleep|time\.sleep|sleep\(|setTimeout\()", re.I)),
    ("large_timeout", re.compile(r"^\+.*\b(timeout|Timeout)\b.*\b([3-9]\d{4,}|\d{6,})\b")),
]


def scan_patch(text: str) -> dict[str, object]:
    findings: list[dict[str, object]] = []
    if not text.strip():
        findings.append({"rule": "empty_patch", "line": 0, "content": ""})
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in RULES:
            if pattern.search(line):
                findings.append({"rule": rule_name, "line": line_number, "content": line[:240]})
    return {"unsafe": bool(findings), "findings": findings}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--patch", required=True, type=Path)
    args = parser.parse_args()

    result = scan_patch(args.patch.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
