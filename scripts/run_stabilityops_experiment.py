#!/usr/bin/env python3
"""Run the public StabilityOps DSL repair pipeline.

This runner intentionally supports only the paper-facing StabilityOps setting:
known flaky-test repair with a known category, one LLM call that emits a typed
DSL action, deterministic guarded patch materialization, safety filtering, and
rerun validation.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stabilityops.runtime import (  # noqa: E402
    TEST_FIELD,
    ExperimentConfig,
    ModelEndpoint,
    allowed_transforms_for_sample,
    append_jsonl,
    bounded_text,
    build_sample_bundle,
    call_openai_compatible,
    extract_focus_snippets,
    fake_llm_json,
    leakage_scan,
    load_config,
    load_prompt,
    load_samples,
    parse_json_object,
    patch_from_transform_action,
    read_jsonl,
    render_prompt,
    validate_patch,
)


METHOD = "stabilityops_dsl"
PROMPT_NAME = "stabilityops_typed_action"


def task_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("sample_id")), str(row.get("model_alias")), str(row.get("method")))


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


def resolve_tool_paths(config: ExperimentConfig) -> ExperimentConfig:
    mvn = Path(config.mvn).expanduser()
    if not mvn.is_absolute():
        mvn = (ROOT / mvn).resolve()
    return replace(config, mvn=str(mvn))


def compact_sample_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "sample_id",
        "dataset",
        "category",
        "project",
        "language",
        "module_path",
        "test_identifier",
        "test_method",
        "maven_test_selector",
        "test_file_repo_relative",
        "editable_files",
        "target_method_start_line",
        "target_method_end_line",
        "target_method_numbered_code",
        "target_method_code",
        "imports_code",
        "source_focus_snippets",
    ]
    return {key: bundle.get(key, "") for key in keys}


def deterministic_context(sample: dict[str, str], bundle: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    category = str(sample.get("PrimaryCategory") or sample.get("Category") or "unknown")
    target_path = str(bundle.get("test_file_repo_relative") or bundle.get("test_file") or "")
    snippets: list[dict[str, str]] = []

    target_method = str(bundle.get("target_method_code") or "")
    if target_method:
        snippets.append(
            {
                "path": target_path,
                "reason": "editable target flaky test method",
                "content": bounded_text(target_method, max_chars=10000),
            }
        )

    numbered = str(bundle.get("target_method_numbered_code") or "")
    if numbered:
        snippets.append(
            {
                "path": target_path,
                "reason": "line-numbered editable target method for typed action parameters",
                "content": bounded_text(numbered, max_chars=12000),
            }
        )

    test_code = str(bundle.get("test_code") or "")
    for item in extract_focus_snippets(test_code, category)[:6]:
        snippets.append(
            {
                "path": str(item.get("path") or target_path),
                "reason": "non-editable evidence snippet: " + str(item.get("reason", "")),
                "content": bounded_text(str(item.get("content", "")), max_chars=4000),
            }
        )

    if category == "ID":
        for item in (bundle.get("source_focus_snippets") or [])[:6]:
            snippets.append(
                {
                    "path": str(item.get("path", "")),
                    "reason": "source-level order/JSON evidence",
                    "content": bounded_text(str(item.get("content", "")), max_chars=4000),
                }
            )

    context_plan = {
        "mode": "deterministic_category_context",
        "known_category": category,
        "retrieved_context": [item["reason"] for item in snippets],
        "policy": "Use snippets only as evidence. Patch materialization is restricted to guarded StabilityOps operators.",
    }
    return context_plan, snippets[:12]


def build_action_payload(sample: dict[str, str], bundle: dict[str, Any]) -> dict[str, Any]:
    category = str(sample.get("PrimaryCategory") or sample.get("Category") or "unknown")
    context_plan, context_snippets = deterministic_context(sample, bundle)
    allowed_transforms = allowed_transforms_for_sample(sample)
    return {
        "sample": compact_sample_payload(bundle),
        "context_plan": context_plan,
        "context_snippets": context_snippets,
        "information_flow": [
            {
                "component": "Deterministic Category Context Retriever",
                "input": ["known flaky category", "target test method", "category-specific code evidence"],
                "output": "bounded context snippets for typed DSL action selection",
            },
            {
                "component": "LLM Action Proposer",
                "input": ["sample", "context snippets", "allowed transforms"],
                "output": "stability_spec plus typed transform_action; no patch text",
            },
        ],
        "patch_instructions": {
            "primary_target_file": bundle.get("test_file_repo_relative", ""),
            "known_category": category,
            "allowed_transforms": allowed_transforms,
            "repair_output_mode": "transform",
            "required_format": "StabilityOps DSL transform_action",
            "do_not_generate_patch": True,
            "empty_action_policy": "Use NO_SAFE_TRANSFORM when no listed transform is supported by visible evidence.",
        },
        "constraints": {
            "do_not_use_developer_patch": True,
            "no_free_form_patch": True,
            "minimal_patch": True,
            "allowed_transforms": allowed_transforms,
            "allowed_transform_policy": "transform_action.transform must be one of allowed_transforms.",
        },
    }


class StabilityOpsRunner:
    def __init__(self, config: ExperimentConfig, run_id: str):
        if config.methods != [METHOD]:
            raise ValueError(f"public StabilityOps runner only supports methods=[{METHOD!r}]")
        if config.repair_output_mode != "transform":
            raise ValueError("public StabilityOps runner requires repair_output_mode='transform'")
        self.config = config
        self.run_id = run_id
        self.run_dir = config.output_root / run_id
        self.results_path = self.run_dir / "results.jsonl"
        self.llm_calls_path = self.run_dir / "llm_calls.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self.lock = threading.Lock()
        self.validation_semaphore = threading.Semaphore(max(1, config.validation_workers))
        self.prompt = load_prompt(config.prompts_dir, PROMPT_NAME)

    def completed_keys(self) -> set[tuple[str, str, str]]:
        return {task_key(row) for row in read_jsonl(self.results_path)}

    def llm_action(
        self,
        endpoint: ModelEndpoint,
        sample: dict[str, str],
        bundle: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        rendered = render_prompt(self.prompt, payload)
        leakage = leakage_scan(rendered)
        prompt_path = (
            self.run_dir
            / "rendered_prompts"
            / safe_name(endpoint.alias)
            / safe_name(str(sample.get("sample_id")))
            / f"{PROMPT_NAME}.txt"
        )
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(rendered, encoding="utf-8")

        if self.config.fake_llm:
            repair_json = fake_llm_json(PROMPT_NAME, sample)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "elapsed_seconds": 0.0}
            raw = json.dumps(repair_json, ensure_ascii=False)
        else:
            raw, usage = call_openai_compatible(
                endpoint=endpoint,
                messages=[{"role": "user", "content": rendered}],
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
                timeout=self.config.llm_timeout,
                seed=self.config.llm_seed,
            )
            repair_json = parse_json_object(raw)

        meta = {
            "usage": usage,
            "leakage_scan": leakage,
            "prompt_name": PROMPT_NAME,
            "rendered_prompt_path": str(prompt_path),
            "raw_response_preview": bounded_text(raw, max_chars=4000),
        }
        append_jsonl(
            self.llm_calls_path,
            {
                "run_id": self.run_id,
                "sample_id": sample.get("sample_id"),
                "model_alias": endpoint.alias,
                "model": endpoint.model,
                "method": METHOD,
                **meta,
            },
        )
        return repair_json, meta

    def run_one(self, sample: dict[str, str], endpoint: ModelEndpoint) -> dict[str, Any]:
        bundle = build_sample_bundle(sample)
        payload = build_action_payload(sample, bundle)
        context_plan = payload["context_plan"]
        context_snippets = payload["context_snippets"]
        repair_json: dict[str, Any]
        llm_meta: dict[str, Any]
        try:
            repair_json, llm_meta = self.llm_action(endpoint, sample, bundle, payload)
        except Exception as exc:
            repair_json = {
                "stability_spec": {},
                "transform_action": {"transform": "NO_SAFE_TRANSFORM", "target_file": ""},
                "notes": {"rationale": f"LLM action generation failed: {exc!r}", "risks": ["llm_error"]},
            }
            llm_meta = {"usage": {}, "leakage_scan": {}, "error": repr(exc)}

        patch, conversion = patch_from_transform_action(sample, repair_json)
        repair_json = dict(repair_json)
        repair_json["patch"] = patch
        repair_json["transform_action_conversion"] = conversion

        if isinstance(conversion, dict) and conversion.get("ok") is False:
            validation = {
                "patch_normalization": {"normalized": False, "reason": "transform_rejected"},
                "patch_applicability_findings": [],
                "unsafe_patch": False,
                "unsafe_findings": [],
                "compile_passed": False,
                "target_single_run_passed": False,
                "target_single_run_outcome": "SKIPPED",
                "post_fix_rerun_budget": self.config.post_fix_reruns,
                "post_fix_runs": 0,
                "post_fix_failures": 0,
                "post_fix_outcomes": [],
                "post_fix_outcomes_consistent": False,
                "post_fix_consistent_pass": False,
                "decision": "safety_rejected",
                "error_class": str(conversion.get("error_class") or "transform_conversion_failed"),
                "error": str(conversion.get("error") or "")[:2000],
            }
        else:
            with self.validation_semaphore:
                validation = validate_patch(
                    sample=sample,
                    patch=patch,
                    run_dir=self.run_dir,
                    model_alias=endpoint.alias,
                    method=METHOD,
                    mvn=self.config.mvn,
                    reruns=self.config.post_fix_reruns,
                    skip_validation=self.config.skip_validation,
                    cleanup=self.config.cleanup_worktrees,
                    trusted_transform=str(conversion.get("transform") or "") if isinstance(conversion, dict) else "",
                )

        row = {
            "run_id": self.run_id,
            "sample_id": sample.get("sample_id"),
            "repo_slug": sample.get("repo_slug"),
            "category": sample.get("PrimaryCategory", sample.get("Category", "unknown")),
            "test_identifier": sample.get("validated_test_identifier") or sample.get(TEST_FIELD),
            "model_alias": endpoint.alias,
            "model": endpoint.model,
            "method": METHOD,
            "context_plan_json": context_plan,
            "context_snippets_count": len(context_snippets),
            "repair_json": repair_json,
            "patch": patch,
            "transform_action_conversion_json": conversion,
            "llm_usage": llm_meta.get("usage", {}),
            "prompt_leakage_scan": llm_meta.get("leakage_scan", {}),
            **validation,
        }
        return row

    def write_summary(self) -> None:
        rows = read_jsonl(self.results_path)
        repaired = sum(1 for row in rows if row.get("decision") == "repaired")
        plausible = sum(1 for row in rows if row.get("compile_passed") and row.get("target_single_run_passed"))
        unsafe = sum(1 for row in rows if row.get("unsafe_patch"))
        rejected = sum(1 for row in rows if row.get("decision") == "safety_rejected")
        summary = {
            "run_id": self.run_id,
            "rows": len(rows),
            "method": METHOD,
            "repaired": repaired,
            "plausible": plausible,
            "unsafe": unsafe,
            "safety_rejected": rejected,
            "repair_success_rate": repaired / len(rows) if rows else 0.0,
            "unsafe_patch_rate": unsafe / len(rows) if rows else 0.0,
        }
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def run(self, resume: bool) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        samples = load_samples(self.config.dataset, self.config.limit)
        completed = self.completed_keys() if resume else set()
        tasks = [
            (sample, endpoint)
            for sample in samples
            for endpoint in self.config.models
            if (sample["sample_id"], endpoint.alias, METHOD) not in completed
        ]
        max_workers = max(1, sum(max(1, endpoint.concurrency) for endpoint in self.config.models))
        print(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "samples": len(samples),
                    "tasks": len(tasks),
                    "method": METHOD,
                    "models": [endpoint.alias for endpoint in self.config.models],
                    "prompt": PROMPT_NAME,
                    "skip_validation": self.config.skip_validation,
                    "post_fix_reruns": self.config.post_fix_reruns,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.run_one, sample, endpoint): (sample, endpoint) for sample, endpoint in tasks}
            for index, future in enumerate(as_completed(futures), start=1):
                sample, endpoint = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    row = {
                        "run_id": self.run_id,
                        "sample_id": sample.get("sample_id"),
                        "repo_slug": sample.get("repo_slug"),
                        "category": sample.get("PrimaryCategory", sample.get("Category", "unknown")),
                        "model_alias": endpoint.alias,
                        "model": endpoint.model,
                        "method": METHOD,
                        "repair_json": {},
                        "patch": "",
                        "unsafe_patch": False,
                        "compile_passed": False,
                        "target_single_run_passed": False,
                        "post_fix_runs": 0,
                        "post_fix_failures": 0,
                        "post_fix_consistent_pass": False,
                        "decision": "error",
                        "error_class": "runner_exception",
                        "error": repr(exc),
                        "traceback": traceback.format_exc(),
                    }
                with self.lock:
                    append_jsonl(self.results_path, row)
                print(
                    f"[{index}/{len(tasks)}] sample={sample.get('sample_id')} "
                    f"decision={row.get('decision')} error={row.get('error_class', '')}",
                    flush=True,
                )
        self.write_summary()
        print(f"summary={self.summary_path}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fake-llm", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--validation-workers", type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config, dry_run=args.dry_run, fake_llm=args.fake_llm, limit=args.limit)
    config = resolve_tool_paths(config)
    if args.skip_validation:
        config = replace(config, skip_validation=True)
    if args.validation_workers is not None:
        config = replace(config, validation_workers=args.validation_workers)
    runner = StabilityOpsRunner(config, args.run_id)
    if not args.resume:
        shutil.rmtree(runner.run_dir, ignore_errors=True)
    runner.run(resume=args.resume)


if __name__ == "__main__":
    main()
