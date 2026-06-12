#!/usr/bin/env python3
"""Build validation-funnel diagnostics for the paper."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "runs_remote" / "experiments"
OUT_DIR = ROOT / "docs" / "generated"

RUNS = {
    "stabilityops": {
        "label": "StabilityOps",
        "run_id": "stabilityops_generic_api_level_merged_v2",
        "method": "stabilityops_dsl",
    },
    "direct": {
        "label": "Direct Free-form",
        "run_id": "direct_freeform_qwen3_full721_rerun10_filterfix_v2",
        "method": "direct_llm_repair",
    },
    "category_guided": {
        "label": "Category-guided Free-form",
        "run_id": "flakyfix_style_qwen3_full721_rerun10_filterfix_v2",
        "method": "category_guided_repair",
    },
    "flakyfix_style": {
        "label": "FlakyFix-style",
        "run_id": "flakyfix_original_qwen3_full721_rerun10_v2",
        "method": "flakyfix_original_repair",
    },
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def pct(count: int, total: int) -> str:
    return f"{count / total * 100:.2f}%"


def fmt_count(count: int, total: int) -> str:
    return f"{count}/{total} ({pct(count, total)})"


def metric_count(rate: float, total: int) -> int:
    return int(round(rate * total))


def summarize_llm_run(spec: dict[str, str]) -> dict[str, Any]:
    run_dir = EXPERIMENTS / spec["run_id"]
    rows = read_jsonl(run_dir / "results.jsonl")
    eval_json = read_json(run_dir / "eval.json")
    metrics = eval_json["by_method"][spec["method"]]
    total = len(rows)

    candidate_patch = sum(bool(str(row.get("patch") or "").strip()) for row in rows)
    unsafe_materialized = sum(
        bool(row.get("unsafe_patch")) and bool(str(row.get("patch") or "").strip())
        for row in rows
    )
    final_repair = metric_count(float(metrics["repair_success_rate"]), total)
    target_pass_no_filter = sum(bool(row.get("target_single_run_passed")) for row in rows)
    unsafe_target_pass = sum(
        bool(row.get("unsafe_patch")) and bool(row.get("target_single_run_passed"))
        for row in rows
    )
    validation_failed_after_safety = candidate_patch - unsafe_materialized - final_repair

    return {
        "method": spec["label"],
        "total": total,
        "candidate_patch": candidate_patch,
        "candidate_patch_rate": candidate_patch / total,
        "safety_blocked_candidate": unsafe_materialized,
        "safety_blocked_candidate_rate": unsafe_materialized / total,
        "validation_failed_after_safety": validation_failed_after_safety,
        "validation_failed_after_safety_rate": validation_failed_after_safety / total,
        "target_pass_no_filter": target_pass_no_filter,
        "target_pass_no_filter_rate": target_pass_no_filter / total,
        "unsafe_target_pass": unsafe_target_pass,
        "unsafe_target_pass_rate": unsafe_target_pass / total,
        "final_accepted_repair": final_repair,
        "final_accepted_repair_rate": final_repair / total,
    }


def summarize_operator_only() -> dict[str, Any]:
    summary = read_json(OUT_DIR / "operator_only_baseline_summary.json")["summary"]
    rows = read_json(OUT_DIR / "operator_only_baseline_summary.json")["combined_rows"]
    total = int(summary["denominator"])
    candidate_patch = int(summary["materialized_patches"]["count"])
    unsafe_materialized = int(summary["unsafe_materialized"]["count"])
    final_repair = int(summary["repair_success"]["count"])
    target_pass_no_filter = sum(row.get("target_single_run_outcome") == "PASS" for row in rows)
    unsafe_target_pass = sum(
        bool(row.get("unsafe_patch")) and row.get("target_single_run_outcome") == "PASS"
        for row in rows
    )
    validation_failed_after_safety = candidate_patch - unsafe_materialized - final_repair

    return {
        "method": "Operator-only",
        "total": total,
        "candidate_patch": candidate_patch,
        "candidate_patch_rate": candidate_patch / total,
        "safety_blocked_candidate": unsafe_materialized,
        "safety_blocked_candidate_rate": unsafe_materialized / total,
        "validation_failed_after_safety": validation_failed_after_safety,
        "validation_failed_after_safety_rate": validation_failed_after_safety / total,
        "target_pass_no_filter": target_pass_no_filter,
        "target_pass_no_filter_rate": target_pass_no_filter / total,
        "unsafe_target_pass": unsafe_target_pass,
        "unsafe_target_pass_rate": unsafe_target_pass / total,
        "final_accepted_repair": final_repair,
        "final_accepted_repair_rate": final_repair / total,
    }


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def build_markdown(summary: dict[str, Any]) -> str:
    rows = []
    for row in summary["funnel"]:
        total = row["total"]
        rows.append(
            [
                row["method"],
                fmt_count(row["candidate_patch"], total),
                fmt_count(row["safety_blocked_candidate"], total),
                fmt_count(row["validation_failed_after_safety"], total),
                fmt_count(row["unsafe_target_pass"], total),
                fmt_count(row["final_accepted_repair"], total),
            ]
        )
    return "\n\n".join(
        [
            "# Validation Funnel And Safety-Filter Diagnostic",
            "Candidate patches are non-empty generated or executor-materialized patches. "
            "Safety-blocked candidates are materialized candidates rejected by the Patch Safety Filter. "
            "Unsafe target-pass is a bounded log diagnostic: it counts rejected candidates that nevertheless passed the target single run before safety acceptance. It is not a repair-success metric and not a rerun10 counterfactual for all rejected patches.",
            markdown_table(
                [
                    "Method",
                    "Candidate patch",
                    "Safety-blocked",
                    "Validation failed after safety",
                    "Unsafe target-pass",
                    "Final accepted repair",
                ],
                rows,
            ),
        ]
    ) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    funnel = [
        summarize_llm_run(RUNS["stabilityops"]),
        summarize_operator_only(),
        summarize_llm_run(RUNS["direct"]),
        summarize_llm_run(RUNS["category_guided"]),
        summarize_llm_run(RUNS["flakyfix_style"]),
    ]
    summary = {
        "source_runs": {name: spec["run_id"] for name, spec in RUNS.items()},
        "operator_only_source": "docs/generated/operator_only_baseline_summary.json",
        "funnel": funnel,
    }
    (OUT_DIR / "validation_funnel_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (OUT_DIR / "validation_funnel_summary.md").write_text(
        build_markdown(summary),
        encoding="utf-8",
    )
    print(OUT_DIR / "validation_funnel_summary.md")
    print(OUT_DIR / "validation_funnel_summary.json")


if __name__ == "__main__":
    main()
