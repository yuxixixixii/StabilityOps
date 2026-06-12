#!/usr/bin/env python3
"""Compare generated repair patches with developer patches as auxiliary evidence."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "runs_remote" / "experiments"
OUT_DIR = ROOT / "docs" / "generated"

RUNS = {
    "stabilityops": {
        "label": "StabilityOps",
        "run_id": "stabilityops_generic_api_level_merged_v2",
    },
    "direct": {
        "label": "Direct Free-form",
        "run_id": "direct_freeform_qwen3_full721_rerun10_v1",
    },
    "flakyfix_style": {
        "label": "Category-guided Free-form",
        "run_id": "flakyfix_style_qwen3_full721_rerun10_v1",
    },
}

GENERIC_INTENTS = {"helper_method", "nondex_config", "sleep_or_timeout"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def read_metadata() -> dict[str, dict[str, str]]:
    path = ROOT / "data" / "metadata" / "idoft_verified_feasible.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["sample_id"]: row for row in csv.DictReader(handle)}


def normalize_path(path: str) -> str:
    path = path.strip()
    for prefix in ["a/", "b/"]:
        if path.startswith(prefix):
            path = path[len(prefix) :]
    return path


def patch_files(patch: str) -> set[str]:
    files: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = normalize_path(line[4:])
            if path != "/dev/null":
                files.add(path)
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                files.add(normalize_path(parts[3]))
    return files


def is_test_file(path: str) -> bool:
    lower = path.lower()
    return "/src/test/" in lower or "/test/" in lower or lower.endswith("test.java") or lower.endswith("tests.java")


def normalize_changed_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\s+", "", line)
    line = re.sub(r"//.*$", "", line)
    return line.lower()


def changed_lines(patch: str, sign: str | None = None) -> set[str]:
    lines: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("+++", "---", "diff ", "index ", "@@", "From ", "Date:", "Subject:")):
            continue
        if sign and not line.startswith(sign):
            continue
        if not sign and not (line.startswith("+") or line.startswith("-")):
            continue
        if line.startswith("+") or line.startswith("-"):
            normalized = normalize_changed_line(line[1:])
            if normalized:
                lines.add(normalized)
    return lines


def jaccard(left: set[str], right: set[str]) -> float | None:
    if not left and not right:
        return None
    union = left | right
    if not union:
        return None
    return len(left & right) / len(union)


def patch_intents(patch: str) -> set[str]:
    lower = patch.lower()
    intents: set[str] = set()
    if any(token in lower for token in ["json.parse", "fromjson", "readtree", "jsonassert", "jsonobject", "jsonarray"]):
        intents.add("json_semantic_assertion")
    if any(token in lower for token in ["new hashset", "containsinanyorder", "containsexactlyinanyorder", "contains exactly in any order"]):
        intents.add("order_insensitive_assertion")
    if "arrays.sort" in lower or "comparator.comparing" in lower or "getdeclaredmethods" in lower or "getdeclaredfields" in lower:
        intents.add("reflection_order_stabilization")
    if "@ordinal" in lower or "public @interface ordinal" in lower:
        intents.add("explicit_field_order")
    if re.search(r"\.\w+\s*=\s*0\s*;", lower) or ".clear();" in lower or re.search(r"\.\w+\s*=\s*null\s*;", lower):
        intents.add("static_state_reset")
    if any(token in lower for token in ["droptable", ".remove(", ".delete(", "deleteonexit", "cleanup", "tearDown".lower()]):
        intents.add("resource_cleanup")
    if "timezone" in lower or "locale" in lower:
        intents.add("timezone_locale_reset")
    if "thread.sleep" in lower or "timeout" in lower:
        intents.add("sleep_or_timeout")
    if "nondex" in lower:
        intents.add("nondex_config")
    if re.search(r"\b(private|public|protected)\s+(static\s+)?[\w<>\[\], ?]+\s+\w+\s*\(", patch):
        intents.add("helper_method")
    return intents


def target_files_from_metadata(row: dict[str, str]) -> set[str]:
    candidates = row.get("test_file_candidates_json") or ""
    files: set[str] = set()
    try:
        parsed = json.loads(candidates)
        for item in parsed:
            text = str(item)
            marker = "/repo/"
            files.add(text.split(marker, 1)[1] if marker in text else text)
    except Exception:
        pass
    return files


def load_dev_patch(row: dict[str, str]) -> str:
    path = ROOT / row["patch_cache_path"]
    return path.read_text(encoding="utf-8", errors="replace")


def compare_row(result: dict[str, Any], metadata: dict[str, dict[str, str]], run_key: str) -> dict[str, Any] | None:
    sample_id = str(result.get("sample_id") or "")
    meta = metadata.get(sample_id)
    if not meta:
        return None
    generated_patch = str(result.get("patch") or "")
    if not generated_patch.strip():
        return None
    developer_patch = load_dev_patch(meta)
    gen_files = patch_files(generated_patch)
    dev_files = patch_files(developer_patch)
    target_files = target_files_from_metadata(meta)
    gen_changed = changed_lines(generated_patch)
    dev_changed = changed_lines(developer_patch)
    gen_added = changed_lines(generated_patch, "+")
    dev_added = changed_lines(developer_patch, "+")
    gen_deleted = changed_lines(generated_patch, "-")
    dev_deleted = changed_lines(developer_patch, "-")
    gen_intents = patch_intents(generated_patch)
    dev_intents = patch_intents(developer_patch)
    gen_specific_intents = gen_intents - GENERIC_INTENTS
    dev_specific_intents = dev_intents - GENERIC_INTENTS

    return {
        "run_key": run_key,
        "sample_id": sample_id,
        "category": result.get("category"),
        "repo_slug": meta.get("repo_slug"),
        "post_fix_consistent_pass": bool(result.get("post_fix_consistent_pass")),
        "unsafe_patch": bool(result.get("unsafe_patch")),
        "gen_file_count": len(gen_files),
        "dev_file_count": len(dev_files),
        "same_file": bool(gen_files & dev_files),
        "same_target_file": bool(gen_files & target_files),
        "gen_test_only": bool(gen_files) and all(is_test_file(path) for path in gen_files),
        "dev_test_involved": any(is_test_file(path) for path in dev_files),
        "changed_line_jaccard": jaccard(gen_changed, dev_changed),
        "added_line_jaccard": jaccard(gen_added, dev_added),
        "deleted_line_jaccard": jaccard(gen_deleted, dev_deleted),
        "generated_intents": sorted(gen_intents),
        "developer_intents": sorted(dev_intents),
        "generated_specific_intents": sorted(gen_specific_intents),
        "developer_specific_intents": sorted(dev_specific_intents),
        "intent_match": bool(gen_specific_intents & dev_specific_intents),
        "matched_intents": sorted(gen_specific_intents & dev_specific_intents),
    }


def mean(values: list[float]) -> float | None:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values) / len(values)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    def group_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(rows)
        if not total:
            return {"samples": 0}
        return {
            "samples": total,
            "success_count": sum(row["post_fix_consistent_pass"] for row in rows),
            "same_file_rate": sum(row["same_file"] for row in rows) / total,
            "same_target_file_rate": sum(row["same_target_file"] for row in rows) / total,
            "generated_test_only_rate": sum(row["gen_test_only"] for row in rows) / total,
            "developer_test_involved_rate": sum(row["dev_test_involved"] for row in rows) / total,
            "intent_match_rate": sum(row["intent_match"] for row in rows) / total,
            "mean_changed_line_jaccard": mean([row["changed_line_jaccard"] for row in rows]),
            "mean_added_line_jaccard": mean([row["added_line_jaccard"] for row in rows]),
            "mean_deleted_line_jaccard": mean([row["deleted_line_jaccard"] for row in rows]),
        }

    by_run = {}
    for run_key in RUNS:
        rows = [row for row in records if row["run_key"] == run_key]
        by_run[run_key] = {
            "all_materialized": group_summary(rows),
            "successful_only": group_summary([row for row in rows if row["post_fix_consistent_pass"]]),
        }

    by_category = {}
    for run_key in RUNS:
        by_category[run_key] = {}
        rows = [row for row in records if row["run_key"] == run_key and row["post_fix_consistent_pass"]]
        for category in sorted({str(row["category"]) for row in rows}):
            by_category[run_key][category] = group_summary([row for row in rows if str(row["category"]) == category])

    intent_pairs = Counter()
    for row in records:
        if row["run_key"] == "stabilityops" and row["post_fix_consistent_pass"]:
            for intent in row["matched_intents"]:
                intent_pairs[intent] += 1
    return {
        "by_run": by_run,
        "by_category_successful": by_category,
        "stabilityops_successful_matched_intents": dict(intent_pairs.most_common()),
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def build_markdown(summary: dict[str, Any], records: list[dict[str, Any]]) -> str:
    main_rows = []
    for key, spec in RUNS.items():
        metrics = summary["by_run"][key]["successful_only"]
        main_rows.append(
            [
                spec["label"],
                str(metrics.get("samples", 0)),
                pct(metrics.get("same_file_rate")),
                pct(metrics.get("same_target_file_rate")),
                pct(metrics.get("intent_match_rate")),
                pct(metrics.get("mean_changed_line_jaccard")),
            ]
        )

    cat_rows = []
    for category, metrics in summary["by_category_successful"]["stabilityops"].items():
        cat_rows.append(
            [
                category,
                str(metrics.get("samples", 0)),
                pct(metrics.get("same_file_rate")),
                pct(metrics.get("same_target_file_rate")),
                pct(metrics.get("intent_match_rate")),
                pct(metrics.get("mean_changed_line_jaccard")),
            ]
        )

    examples = []
    stability_success = [
        row for row in records if row["run_key"] == "stabilityops" and row["post_fix_consistent_pass"]
    ]
    for selector in [
        lambda row: row["same_file"] and row["intent_match"] and (row["changed_line_jaccard"] or 0) > 0.05,
        lambda row: row["same_file"] and row["intent_match"] and (row["changed_line_jaccard"] or 0) <= 0.05,
        lambda row: row["same_file"] and not row["intent_match"],
    ]:
        match = next((row for row in stability_success if selector(row)), None)
        if match:
            examples.append(
                [
                    match["sample_id"],
                    str(match["category"]),
                    ", ".join(match["developer_intents"]) or "unknown",
                    ", ".join(match["generated_intents"]) or "unknown",
                    pct(match["changed_line_jaccard"]),
                ]
            )

    return "\n\n".join(
        [
            "# Developer Patch Similarity",
            "该分析只把 developer patch 作为参考，不把它当作唯一正确 oracle。很多 IDoFT PR 同时修改多个 flaky tests，因此文本重叠指标偏噪声，适合放在 supplementary/appendix。",
            "## Successful Patches",
            markdown_table(
                [
                    "Method",
                    "Successful materialized patches",
                    "Same file",
                    "Same target file",
                    "Repair-intent match",
                    "Mean changed-line Jaccard",
                ],
                main_rows,
            ),
            "## StabilityOps By Category",
            markdown_table(
                [
                    "Category",
                    "Successful patches",
                    "Same file",
                    "Same target file",
                    "Repair-intent match",
                    "Mean changed-line Jaccard",
                ],
                cat_rows,
            ),
            "## StabilityOps Matched Intent Distribution",
            markdown_table(
                ["Intent", "Count"],
                [[key, str(value)] for key, value in summary["stabilityops_successful_matched_intents"].items()],
            ),
            "## Example Rows",
            markdown_table(
                ["Sample", "Category", "Developer intents", "StabilityOps intents", "Changed-line Jaccard"],
                examples,
            ),
            "## Interpretation",
            "如果 same-file / intent-match 较高但 changed-line Jaccard 很低，说明 StabilityOps 常与 developer patch 处理同类稳定性问题，但使用了不同的具体实现。这可以作为辅助可信度证据；不建议作为主指标。",
        ]
    ) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    metadata = read_metadata()
    records: list[dict[str, Any]] = []
    for run_key, spec in RUNS.items():
        rows = read_jsonl(EXPERIMENTS / spec["run_id"] / "results.jsonl")
        for row in rows:
            record = compare_row(row, metadata, run_key)
            if record:
                records.append(record)
    summary = summarize(records)
    records_path = OUT_DIR / "developer_patch_similarity_records.jsonl"
    summary_json_path = OUT_DIR / "developer_patch_similarity_summary.json"
    summary_md_path = OUT_DIR / "developer_patch_similarity_summary.md"
    with records_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_md_path.write_text(build_markdown(summary, records), encoding="utf-8")
    print(summary_md_path)
    print(summary_json_path)
    print(records_path)


if __name__ == "__main__":
    main()
