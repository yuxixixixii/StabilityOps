#!/usr/bin/env python3
"""Aggregate StabilityOps DSL experiment metrics from JSONL results."""

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


def as_bool(row: dict[str, Any], key: str) -> bool:
    return bool(row.get(key))


def failure_rate(failures: Any, runs: Any) -> float | None:
    try:
        failures_float = float(failures)
        runs_float = float(runs)
    except (TypeError, ValueError):
        return None
    if runs_float <= 0:
        return None
    return failures_float / runs_float


def numeric_usage_value(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_llm_usage(row: dict[str, Any]) -> dict[str, float]:
    """Aggregate all LLM usage objects recorded for one result row."""
    usage_keys = [
        "llm_usage",
        "patch_repair_llm_usage",
        "transform_action_revision_llm_usage",
    ]
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    elapsed_seconds = 0.0
    calls = 0
    for key in usage_keys:
        usage = row.get(key)
        if not isinstance(usage, dict) or not usage:
            continue
        prompt = numeric_usage_value(usage, "prompt_tokens")
        completion = numeric_usage_value(usage, "completion_tokens")
        total = numeric_usage_value(usage, "total_tokens") or prompt + completion
        try:
            elapsed = float(usage.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            elapsed = 0.0
        if prompt == 0 and completion == 0 and total == 0 and elapsed == 0.0:
            continue
        calls += 1
        prompt_tokens += prompt
        completion_tokens += completion
        total_tokens += total
        elapsed_seconds += elapsed
    return {
        "calls": calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "elapsed_seconds": elapsed_seconds,
    }


def normalize_category(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "implementation-dependent": "id",
        "implementation dependent": "id",
        "order-dependent": "od",
        "order dependent": "od",
        "non-isolated": "nio",
        "non isolated": "nio",
    }
    return aliases.get(text, text)


def first_intent_category(row: dict[str, Any]) -> Any:
    intent_json = row.get("intent_json")
    if isinstance(intent_json, dict):
        intents = intent_json.get("stability_intents") or []
        if intents and isinstance(intents[0], dict):
            return intents[0].get("mapped_category")
    return None


def selected_intent_category(row: dict[str, Any]) -> Any:
    selected = row.get("selected_intent_json")
    if isinstance(selected, dict):
        return selected.get("mapped_category")
    return None


def transform_conversion(row: dict[str, Any]) -> dict[str, Any]:
    repair_json = row.get("repair_json")
    if isinstance(repair_json, dict):
        conversion = repair_json.get("transform_action_conversion")
        if isinstance(conversion, dict):
            return conversion
    conversion = row.get("transform_action_conversion_json")
    return conversion if isinstance(conversion, dict) else {}


def transform_action_name(row: dict[str, Any]) -> str:
    repair_json = row.get("repair_json")
    if isinstance(repair_json, dict):
        action = repair_json.get("transform_action") or repair_json.get("repair_transform")
        if isinstance(action, dict):
            return str(action.get("transform") or action.get("name") or "").strip().upper()
    return ""


def concrete_operator_name(row: dict[str, Any]) -> str:
    transform = transform_action_name(row)
    return transform if transform and transform != "NO_SAFE_TRANSFORM" else ""


def is_no_safe_transform(row: dict[str, Any]) -> bool:
    return transform_action_name(row) == "NO_SAFE_TRANSFORM"


def is_safety_rejected(row: dict[str, Any]) -> bool:
    if is_no_safe_transform(row):
        return True
    conversion = transform_conversion(row)
    if conversion and conversion.get("ok") is False:
        return True
    patch = str(row.get("patch") or "")
    if row.get("unsafe_patch") and not patch.strip():
        return True
    return False


def is_unsafe_materialized_patch(row: dict[str, Any]) -> bool:
    patch = str(row.get("patch") or "")
    return bool(row.get("unsafe_patch") and patch.strip() and not is_safety_rejected(row))


def unsafe_finding_rules(row: dict[str, Any]) -> list[str]:
    findings = row.get("unsafe_findings")
    if not isinstance(findings, list):
        return []
    rules: list[str] = []
    for finding in findings:
        if isinstance(finding, dict) and finding.get("rule"):
            rules.append(str(finding["rule"]))
        elif finding:
            rules.append(str(finding))
    return rules


def rejection_reason(row: dict[str, Any]) -> str | None:
    if is_no_safe_transform(row):
        return "no_safe_transform"
    conversion = transform_conversion(row)
    if conversion and conversion.get("ok") is False:
        error_class = str(conversion.get("error_class") or "transform_conversion_failed")
        error = str(conversion.get("error") or "").strip()
        return f"{error_class}: {error[:120]}" if error else error_class
    if row.get("unsafe_patch"):
        rules = unsafe_finding_rules(row)
        if rules:
            return ",".join(sorted(set(rules)))
        if not str(row.get("patch") or "").strip():
            return "empty_patch"
        return "unsafe_materialized_patch"
    return None


def conversion_error_class(row: dict[str, Any]) -> str:
    conversion = transform_conversion(row)
    if conversion and conversion.get("ok") is False:
        return str(conversion.get("error_class") or "transform_conversion_failed")
    return str(row.get("error_class") or "")


def conversion_error_text(row: dict[str, Any]) -> str:
    conversion = transform_conversion(row)
    parts = [
        str(conversion.get("error_class") or ""),
        str(conversion.get("error") or ""),
        str(row.get("error_class") or ""),
    ]
    return " ".join(part for part in parts if part).lower()


def conversion_passed(row: dict[str, Any]) -> bool:
    transform = concrete_operator_name(row)
    if not transform:
        return False
    conversion = transform_conversion(row)
    if conversion and conversion.get("ok") is False:
        return False
    return True


def materialized_patch(row: dict[str, Any]) -> bool:
    patch = str(row.get("patch") or "")
    return bool(patch.strip() and not is_safety_rejected(row))


def coverage_gap_bucket(row: dict[str, Any]) -> str:
    """Classify failures into paper-facing diagnosability buckets."""
    if post_fix_consistent_pass(row):
        return "repaired"
    if is_no_safe_transform(row):
        return "dsl_coverage_gap:no_safe_transform"

    error_class = conversion_error_class(row)
    error_text = conversion_error_text(row)

    if transform_conversion(row).get("ok") is False:
        if error_class == "category_disallowed_transform":
            return "action_selection_error:category_disallowed_transform"
        if "line_span" in error_class or "missing target method" in error_text:
            return "action_parameter_gap:line_span_or_target_scope"
        if "missing_" in error_class and ("parameter" in error_class or "parameters" in error_text):
            return "action_parameter_gap:missing_typed_parameter"
        if "cannot_infer" in error_class or "invalid" in error_class:
            return "action_parameter_gap:invalid_or_uninferrable_parameter"
        if "not_visible" in error_text or "unavailable" in error_text or "not visible" in error_text:
            return "context_visibility_gap"
        if error_class.startswith("unsupported_") or error_class.startswith("no_"):
            return "dsl_coverage_gap:unsupported_target_shape"
        if error_class.startswith("inapplicable_") or "guard" in error_class:
            return "operator_guard_gap"
        return f"executor_rejection:{error_class or 'unknown'}"

    if is_unsafe_materialized_patch(row):
        return "unsafe_materialized_patch"
    if as_bool(row, "unsafe_patch"):
        return "safety_rejection:unsafe_or_empty_patch"
    if materialized_patch(row):
        if not as_bool(row, "compile_passed"):
            return "validation_failure:compile"
        if not as_bool(row, "target_single_run_passed"):
            return "validation_failure:target_single_run"
        try:
            if int(row.get("post_fix_failures", 0)) > 0:
                return "validation_failure:rerun"
        except (TypeError, ValueError):
            pass
        return "validation_failure:unknown"
    return "unknown_failure"


def post_fix_outcomes_consistent(row: dict[str, Any]) -> bool:
    if row.get("post_fix_outcomes_consistent") is not None:
        return bool(row.get("post_fix_outcomes_consistent"))
    outcomes = row.get("post_fix_outcomes")
    if isinstance(outcomes, list) and outcomes:
        return len(set(str(outcome).upper() for outcome in outcomes)) == 1
    try:
        runs = int(row.get("post_fix_runs", 0))
        failures = int(row.get("post_fix_failures", 0))
        budget = int(row.get("post_fix_rerun_budget", runs))
    except (TypeError, ValueError):
        return False
    return runs == budget and failures in {0, runs}


def post_fix_consistent_pass(row: dict[str, Any]) -> bool:
    if is_no_safe_transform(row):
        return False
    if row.get("post_fix_consistent_pass") is not None:
        return bool(row.get("post_fix_consistent_pass"))
    outcomes = row.get("post_fix_outcomes")
    if isinstance(outcomes, list) and outcomes:
        return bool(
            as_bool(row, "compile_passed")
            and as_bool(row, "target_single_run_passed")
            and all(str(outcome).upper() == "PASS" for outcome in outcomes)
            and not as_bool(row, "unsafe_patch")
        )
    try:
        runs = int(row.get("post_fix_runs", 0))
        failures = int(row.get("post_fix_failures", 0))
        budget = int(row.get("post_fix_rerun_budget", runs))
    except (TypeError, ValueError):
        return False
    unsafe_patch = as_bool(row, "unsafe_patch")
    return bool(
        as_bool(row, "compile_passed")
        and as_bool(row, "target_single_run_passed")
        and runs == budget
        and failures == 0
        and not unsafe_patch
    )


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_model_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_category_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        method = str(row.get("method", "unknown"))
        model = str(row.get("model_alias", row.get("model", "unknown")))
        category = str(row.get("category", row.get("expected_category", "unknown")))
        by_method[method].append(row)
        by_model_method[f"{model}::{method}"].append(row)
        by_category_method[f"{category}::{method}"].append(row)

    def summarize_group(group_rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(group_rows)
        repaired = 0
        plausible = 0
        unsafe = 0
        safety_rejected = 0
        unsafe_materialized = 0
        consistent_outcome = 0
        consistent_pass = 0
        intent_labeled = 0
        intent_correct = 0
        selected_intent_labeled = 0
        selected_intent_correct = 0
        intent_confusion: dict[str, int] = defaultdict(int)
        selected_intent_confusion: dict[str, int] = defaultdict(int)
        reductions: list[float] = []

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        total_llm_elapsed_seconds = 0.0
        total_llm_calls = 0
        token_rows = 0
        validation_seconds: list[float] = []
        error_counts: dict[str, int] = defaultdict(int)
        rejection_reasons: dict[str, int] = defaultdict(int)
        operator_counts: Counter[str] = Counter()
        operator_repaired: Counter[str] = Counter()
        operator_conversion_passed: Counter[str] = Counter()
        operator_materialized: Counter[str] = Counter()
        operator_safety_rejected: Counter[str] = Counter()
        operator_validation_failed: Counter[str] = Counter()
        coverage_gap_counts: Counter[str] = Counter()
        coverage_gap_by_category: Counter[str] = Counter()
        coverage_gap_by_operator: Counter[str] = Counter()
        concrete_operator_selected = 0
        concrete_operator_conversion_passed = 0
        concrete_operator_materialized = 0

        for row in group_rows:
            compile_passed = as_bool(row, "compile_passed")
            target_passed = as_bool(row, "target_single_run_passed")
            unsafe_patch = as_bool(row, "unsafe_patch")
            row_consistent_outcome = post_fix_outcomes_consistent(row)
            row_consistent_pass = post_fix_consistent_pass(row)
            transform = transform_action_name(row) or "NO_ACTION"
            concrete_transform = concrete_operator_name(row)
            gap_bucket = coverage_gap_bucket(row)

            operator_counts[transform] += 1
            coverage_gap_counts[gap_bucket] += 1
            category = str(row.get("category", row.get("expected_category", "unknown")))
            coverage_gap_by_category[f"{category}::{gap_bucket}"] += 1
            coverage_gap_by_operator[f"{transform}::{gap_bucket}"] += 1
            if concrete_transform:
                concrete_operator_selected += 1
                if conversion_passed(row):
                    concrete_operator_conversion_passed += 1
                    operator_conversion_passed[concrete_transform] += 1
                if materialized_patch(row):
                    concrete_operator_materialized += 1
                    operator_materialized[concrete_transform] += 1
                if row_consistent_pass:
                    operator_repaired[concrete_transform] += 1
                if is_safety_rejected(row):
                    operator_safety_rejected[concrete_transform] += 1
                if materialized_patch(row) and not row_consistent_pass:
                    operator_validation_failed[concrete_transform] += 1

            if compile_passed and target_passed and not is_no_safe_transform(row):
                plausible += 1
            if row_consistent_pass:
                repaired += 1
            if row_consistent_outcome:
                consistent_outcome += 1
            if row_consistent_pass:
                consistent_pass += 1
            if unsafe_patch:
                unsafe += 1
            if is_safety_rejected(row):
                safety_rejected += 1
            if is_unsafe_materialized_patch(row):
                unsafe_materialized += 1
            error_class = str(row.get("error_class", ""))
            if error_class:
                error_counts[error_class] += 1
            reason = rejection_reason(row)
            if reason:
                rejection_reasons[reason] += 1

            method = str(row.get("method", ""))
            if method in {"intent_only_repair", "full_stability_intent_agent", "stabilityops_dsl"}:
                predicted = row.get("predicted_category")
                if predicted is None:
                    predicted = first_intent_category(row)
                selected_predicted = selected_intent_category(row)
                expected = row.get("expected_category")
                if expected is None:
                    expected = row.get("category")
                if predicted is not None and expected is not None:
                    intent_labeled += 1
                    expected_norm = normalize_category(expected)
                    predicted_norm = normalize_category(predicted)
                    intent_confusion[f"{expected_norm}->{predicted_norm}"] += 1
                    if predicted_norm == expected_norm:
                        intent_correct += 1
                if selected_predicted is not None and expected is not None:
                    selected_intent_labeled += 1
                    expected_norm = normalize_category(expected)
                    selected_norm = normalize_category(selected_predicted)
                    selected_intent_confusion[f"{expected_norm}->{selected_norm}"] += 1
                    if selected_norm == expected_norm:
                        selected_intent_correct += 1

            pre_rate = failure_rate(row.get("pre_fix_failures"), row.get("pre_fix_runs"))
            post_rate = failure_rate(row.get("post_fix_failures"), row.get("post_fix_runs"))
            if pre_rate is not None and post_rate is not None:
                reductions.append(pre_rate - post_rate)

            usage = aggregate_llm_usage(row)
            if usage["calls"]:
                total_prompt_tokens += int(usage["prompt_tokens"])
                total_completion_tokens += int(usage["completion_tokens"])
                total_tokens += int(usage["total_tokens"])
                total_llm_elapsed_seconds += float(usage["elapsed_seconds"])
                total_llm_calls += int(usage["calls"])
                token_rows += 1
            try:
                validation_seconds.append(float(row.get("target_single_run_elapsed_seconds")))
            except (TypeError, ValueError):
                pass

        return {
            "samples": total,
            "repair_success_rate": repaired / total if total else 0.0,
            "post_fix_outcome_consistency_rate": consistent_outcome / total if total else 0.0,
            "post_fix_consistent_pass_rate": consistent_pass / total if total else 0.0,
            "plausible_patch_rate": plausible / total if total else 0.0,
            "unsafe_patch_rate": unsafe / total if total else 0.0,
            "safety_rejection_rate": safety_rejected / total if total else 0.0,
            "unsafe_materialized_patch_rate": unsafe_materialized / total if total else 0.0,
            "operator_coverage_rate": concrete_operator_selected / total if total else 0.0,
            "operator_applicability_rate": (
                concrete_operator_conversion_passed / concrete_operator_selected if concrete_operator_selected else 0.0
            ),
            "operator_materialization_rate": concrete_operator_materialized / total if total else 0.0,
            "operator_selection_distribution": dict(sorted(operator_counts.items(), key=lambda item: (-item[1], item[0]))),
            "coverage_gap_distribution": dict(sorted(coverage_gap_counts.items(), key=lambda item: (-item[1], item[0]))),
            "top_coverage_gaps": [
                {"gap": gap, "count": count}
                for gap, count in sorted(
                    ((gap, count) for gap, count in coverage_gap_counts.items() if gap != "repaired"),
                    key=lambda item: (-item[1], item[0]),
                )[:20]
            ],
            "coverage_gap_by_category": dict(sorted(coverage_gap_by_category.items(), key=lambda item: (-item[1], item[0]))),
            "coverage_gap_by_operator": dict(sorted(coverage_gap_by_operator.items(), key=lambda item: (-item[1], item[0]))),
            "per_operator_metrics": {
                operator: {
                    "selected": count,
                    "conversion_passed": operator_conversion_passed.get(operator, 0),
                    "materialized": operator_materialized.get(operator, 0),
                    "repaired": operator_repaired.get(operator, 0),
                    "safety_rejected": operator_safety_rejected.get(operator, 0),
                    "validation_failed": operator_validation_failed.get(operator, 0),
                    "applicability_rate": operator_conversion_passed.get(operator, 0) / count if count else 0.0,
                    "materialization_rate": operator_materialized.get(operator, 0) / count if count else 0.0,
                    "success_rate": operator_repaired.get(operator, 0) / count if count else 0.0,
                }
                for operator, count in sorted(
                    ((op, c) for op, c in operator_counts.items() if op not in {"NO_ACTION", "NO_SAFE_TRANSFORM"}),
                    key=lambda item: (-item[1], item[0]),
                )
            },
            "first_intent_accuracy": intent_correct / intent_labeled if intent_labeled else None,
            "selected_intent_accuracy": selected_intent_correct / selected_intent_labeled if selected_intent_labeled else None,
            "intent_confusion": dict(sorted(intent_confusion.items())),
            "selected_intent_confusion": dict(sorted(selected_intent_confusion.items())),
            "mean_failure_rate_reduction": sum(reductions) / len(reductions) if reductions else None,
            "total_llm_calls": total_llm_calls,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_llm_tokens": total_tokens,
            "total_llm_elapsed_seconds": total_llm_elapsed_seconds,
            "mean_prompt_tokens": total_prompt_tokens / token_rows if token_rows else None,
            "mean_completion_tokens": total_completion_tokens / token_rows if token_rows else None,
            "mean_total_tokens": total_tokens / token_rows if token_rows else None,
            "tokens_per_repair_success": total_tokens / repaired if repaired else None,
            "completion_tokens_per_repair_success": total_completion_tokens / repaired if repaired else None,
            "prompt_tokens_per_repair_success": total_prompt_tokens / repaired if repaired else None,
            "mean_target_single_run_seconds": sum(validation_seconds) / len(validation_seconds) if validation_seconds else None,
            "error_counts": dict(sorted(error_counts.items())),
            "rejection_reason_distribution": dict(sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))),
            "top_rejection_reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(rejection_reasons.items(), key=lambda item: (-item[1], item[0]))[:20]
            ],
        }

    summary: dict[str, Any] = {
        "by_method": {key: summarize_group(value) for key, value in sorted(by_method.items())},
        "by_model_method": {key: summarize_group(value) for key, value in sorted(by_model_method.items())},
        "by_category_method": {key: summarize_group(value) for key, value in sorted(by_category_method.items())},
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    rows = read_jsonl(args.results)
    summary = summarize(rows)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
