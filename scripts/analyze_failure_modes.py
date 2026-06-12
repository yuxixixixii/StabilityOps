#!/usr/bin/env python3
"""Classify StabilityOps result failures into diagnosable buckets."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def transform_action(row: dict[str, Any]) -> dict[str, Any]:
    repair_json = row.get("repair_json")
    if isinstance(repair_json, dict):
        action = repair_json.get("transform_action")
        if isinstance(action, dict):
            return action
    return {}


def transform_name(row: dict[str, Any]) -> str:
    return str(transform_action(row).get("transform") or "").strip()


def transform_conversion(row: dict[str, Any]) -> dict[str, Any]:
    repair_json = row.get("repair_json")
    if isinstance(repair_json, dict):
        conversion = repair_json.get("transform_action_conversion")
        if isinstance(conversion, dict):
            return conversion
    conversion = row.get("transform_action_conversion_json")
    return conversion if isinstance(conversion, dict) else {}


def repair_success(row: dict[str, Any]) -> bool:
    if transform_name(row) == "NO_SAFE_TRANSFORM":
        return False
    try:
        runs = int(row.get("post_fix_runs") or 0)
        budget = int(row.get("post_fix_rerun_budget") or runs)
        failures = int(row.get("post_fix_failures") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        row.get("compile_passed")
        and row.get("target_single_run_passed")
        and runs == budget
        and failures == 0
        and not row.get("unsafe_patch")
    )


def failure_bucket(row: dict[str, Any]) -> str:
    if repair_success(row):
        return "success"
    conversion = transform_conversion(row)
    error_class = str(row.get("error_class") or "")
    transform = transform_name(row)
    if transform == "NO_SAFE_TRANSFORM" or error_class in {"no_safe_transform", "no_safe_or_applicable_transform"}:
        return "no_safe_or_unsupported_operator"
    if conversion and conversion.get("ok") is False:
        reason = " ".join(str(conversion.get(key) or "") for key in ["error_class", "error", "reason"])
        visibility_markers = [
            "not_visible",
            "wrapper_unavailable",
            "cannot_find",
            "missing",
            "line_span",
            "outside_target_method",
            "not variable",
            "not found",
        ]
        if any(marker in reason for marker in visibility_markers):
            return "context_or_visibility_insufficient"
        return "operator_selected_but_guard_failed"
    if row.get("unsafe_patch"):
        return "unsafe_materialized_patch"
    if (
        error_class
        in {
            "target_single_run_failed",
            "post_fix_rerun_failed",
            "validation_failed",
            "patch_applicability_blocked",
        }
        or row.get("target_single_run_passed") is False
        or int(row.get("post_fix_failures") or 0) > 0
    ):
        return "materialized_patch_validation_failed"
    if row.get("decision") == "safety_rejected":
        return "safety_rejected_other"
    return "other_error"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    categories = ["ALL"] + sorted({str(row.get("category") or "unknown") for row in rows})
    by_category: dict[str, Any] = {}
    for category in categories:
        group = rows if category == "ALL" else [row for row in rows if str(row.get("category") or "unknown") == category]
        failures = [row for row in group if not repair_success(row)]
        buckets = Counter(failure_bucket(row) for row in failures)
        by_category[category] = {
            "total": len(group),
            "success": sum(1 for row in group if repair_success(row)),
            "fail": len(failures),
            "failure_buckets": {
                key: {"count": value, "failure_percent": round(value / len(failures), 4) if failures else 0.0}
                for key, value in buckets.most_common()
            },
        }
    top_error_by_category: dict[str, Any] = {}
    top_transform_by_category: dict[str, Any] = {}
    for category in categories:
        group = rows if category == "ALL" else [row for row in rows if str(row.get("category") or "unknown") == category]
        failures = [row for row in group if not repair_success(row)]
        error_counter: Counter[str] = Counter()
        transform_counter: Counter[str] = Counter()
        for row in failures:
            conversion = transform_conversion(row)
            error_counter[str(conversion.get("error_class") or row.get("error_class") or "NONE")] += 1
            transform_counter[transform_name(row) or "?"] += 1
        top_error_by_category[category] = dict(error_counter.most_common(20))
        top_transform_by_category[category] = dict(transform_counter.most_common(20))
    return {
        "rows": len(rows),
        "by_category": by_category,
        "top_error_by_category": top_error_by_category,
        "top_transform_by_category": top_transform_by_category,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--exclude-category", action="append", default=[])
    args = parser.parse_args()

    excluded = {str(item) for item in args.exclude_category}
    rows = [row for row in read_jsonl(args.results) if str(row.get("category") or "") not in excluded]
    summary = summarize(rows)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary["by_category"].get("ALL", {}), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
