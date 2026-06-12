#!/usr/bin/env python3
"""Run StabilityOps DSL and baseline repair experiments."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stability_agent.runtime import (  # noqa: E402
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
    find_test_file,
    leakage_scan,
    load_config,
    load_prompt,
    load_samples,
    parse_json_object,
    patch_from_edit_action,
    patch_from_transform_action,
    read_jsonl,
    render_prompt,
    run_command,
    target_test_relative_path,
    unified_diff_for_revised,
    validate_patch,
)
from stability_agent.heuristic_ops import heuristic_repair_json  # noqa: E402


PROMPT_BY_METHOD = {
    "direct_llm_repair": "direct_llm_repair",
    "category_guided_repair": "category_guided_repair",
    "flakyfix_original_repair": "flakyfix_original_repair",
    "intent_only_repair": "intent_only_repair",
    "full_stability_intent_agent": "intent_constrained_repair",
    "stabilityops_dsl": "intent_constrained_repair",
    "heuristic_stabilityops": "intent_constrained_repair",
}

STABILITYOPS_METHODS = {"full_stability_intent_agent", "stabilityops_dsl"}
HEURISTIC_METHODS = {"heuristic_stabilityops"}
TRANSFORM_METHODS = {*STABILITYOPS_METHODS, *HEURISTIC_METHODS}
INTENT_AWARE_METHODS = {"intent_only_repair", *STABILITYOPS_METHODS}


def flakyfix_changed_lines_from_patch(sample: dict[str, str]) -> tuple[str, str]:
    """Extract target-test additions/deletions in the style used by FlakyFix labels."""
    patch_path = Path(sample.get("patch_cache_path") or sample.get("patch_path") or "")
    if not patch_path.exists():
        return "", ""
    target_file = target_test_relative_path(sample)
    deleted: list[str] = []
    added: list[str] = []
    in_target_file = not target_file
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            in_target_file = (f" b/{target_file}" in line or f" a/{target_file}" in line) if target_file else True
            continue
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("@@"):
            continue
        if not in_target_file:
            continue
        if line.startswith("-"):
            deleted.append("- " + line[1:])
        elif line.startswith("+"):
            added.append("+ " + line[1:])
    return "\n".join(deleted).strip(), "\n".join(added).strip()


def flakyfix_auto_label_from_patch(sample: dict[str, str]) -> dict[str, Any]:
    """Replicate FlakyFix's rule-based Auto_labels generation on the target-test patch."""
    deleted_lines, added_lines = flakyfix_changed_lines_from_patch(sample)
    deleted_tokens = deleted_lines.replace("- ", "")
    added_tokens = added_lines.replace("+ ", "")
    added_tokens_l = added_tokens.lower()
    deleted_tokens_l = deleted_tokens.lower()
    added_lines_l = added_lines.lower()
    deleted_lines_l = deleted_lines.lower()
    labels: list[str] = []

    def add(label: str) -> None:
        if label and label not in labels:
            labels.append(label)

    if added_tokens_l.replace(" ", "") == deleted_lines_l.replace(" ", "") and added_tokens_l:
        add("no_change")
    if (
        re.search(r"=\s*0;\s*", added_tokens_l)
        or (re.search(r"=\s*0\s*;", added_lines_l) and re.search(r"=\s*0", added_tokens_l))
        or re.search(r"\.(reset|clear|remove|purge).*?\(.*?\)", added_lines_l)
    ):
        add("reset_variable")
    if (
        "sortfield" in added_lines_l
        or "sort_properties" in added_lines_l
        or "sorted" in added_lines_l
        or ".sort" in added_lines_l
        or "order by" in added_tokens_l
    ):
        add("reorder_data")
    if re.search(r"jsonparser\s*\(\s*\)\s*;", added_lines_l) or "jsonparser." in added_lines_l:
        add("change_data_format")
    if (
        ("map" in deleted_lines_l and "hashmap" in added_lines_l and "hash" in added_tokens_l)
        or ("map" in deleted_lines_l and "linkedhashmap" in added_lines_l and "linkedhash" in added_tokens_l)
        or ("hashmap" in deleted_lines_l and "linkedhashmap" in added_lines_l and "linked" in added_tokens_l)
        or ("set" in deleted_lines_l and "hashset" in added_lines_l and "hash" in added_tokens_l)
        or ("set" in deleted_lines_l and "linkedhashset" in added_lines_l and "linkedhash" in added_tokens_l)
        or ("hashset" in deleted_lines_l and "linkedhashset" in added_lines_l and "linked" in added_tokens_l)
        or (re.search(r"(map|set)(\s|<)", added_lines_l) and not re.search(r"(map|set)(\s|<)", deleted_lines_l))
        or re.search(r"new\s(map|set|hashmap|hashset|linkedhashmap|linkedhashset|treemap|treeset)", added_lines_l)
        or ".aslist" in added_lines_l
    ):
        add("change_data_structure")
    if re.search(r"\.getmethods\s*\(\s*\)", deleted_lines_l) and re.search(r"=\s*getmethods\s*\(", added_lines_l):
        add("call_static_method")
    if ("assertequals" in deleted_lines_l and "match.that(s).isequalto" in added_lines_l) or ".matches" in added_lines_l:
        add("string_matching")
    if (
        "assertequals" in deleted_lines_l
        and (
            re.search(r"asserttrue\(.*?\.equals", added_lines_l)
            or re.search(r"asserttrue\(.*?\.contains", added_lines_l)
            or re.search(r"asserttrue\(.*?==", added_lines_l)
        )
    ) or (
        "assertequals" in added_lines_l
        and (re.search(r"asserttrue\(.*?\.equals", deleted_lines_l) or re.search(r"asserttrue\(.*?==", deleted_lines_l))
    ) or ("containsexactly" in deleted_lines_l and "containsonly" in added_lines_l) or (
        "containsexactly" in added_lines_l and "containsonly" in deleted_lines_l
    ):
        add("change_assertion")
    if (
        ("assertequals" in deleted_lines_l or "assertthat" in deleted_lines_l or "asserttrue" in deleted_lines_l)
        and (
            "assertjsonstringequals" in added_lines_l
            or "jsonassert.assertequals" in added_lines_l
            or "assertjsonequalsnonstrict" in added_lines_l
        )
    ):
        add("change_assertion")
    if "ziptomap" in added_lines_l:
        add("change_assertion")
    if (
        "assertjsonequalsnonstrict" in added_lines_l
        or ("containsexactly" in deleted_lines_l and re.search(r"contains\s*\(", added_lines_l))
        or ("contains" in deleted_lines_l and "containsinanyorder" in added_lines_l)
        or ("containsexactly" in deleted_lines_l and "containsexactlyinanyorder" in added_lines_l)
        or ("isof" in deleted_lines_l and "hascontentinanyorder" in added_lines_l)
    ):
        add("change_condition")
    if re.search(r"try\s*{", added_tokens_l):
        add("handle_exceptions")
    if re.search(r"throws\s*\D*exception", deleted_lines_l) and not re.search(r"throws\s*\D*exception", added_lines_l):
        add("handle_exceptions")
    if re.search(r"throws\s*\D*exception", added_lines_l) and not re.search(r"throws\s*\D*exception", deleted_lines_l):
        add("handle_exceptions")
    if re.search(r"catch\s*\(\s*\D+exception", deleted_lines_l) and re.search(r"catch\s*\(\s*exception", added_lines_l):
        add("handle_exceptions")
    if re.search(r"catch\s*\(\s*\D+exception", added_lines_l) and re.search(r"catch\s*\(\s*exception", deleted_lines_l):
        add("handle_exceptions")
    if (
        sorted(list(deleted_tokens_l.replace(" ", ""))) == sorted(list(added_tokens_l.replace(" ", "")))
        and added_tokens_l.replace(" ", "")
        and deleted_lines_l.replace(" ", "")
    ):
        add("reorder_parameters")
    if "select *" in deleted_lines_l and "select *" not in added_lines_l:
        add("reorder_data")
    if re.search(r"(\&\&|\|\|)", deleted_tokens_l) or re.search(r"(\&\&|\|\|)", added_tokens_l):
        add("change_condition")
    if "thread.sleep" in added_tokens_l:
        add("sleep")
    if "timezone" in added_tokens_l:
        add("change_timezone")
    if "timeout" in added_tokens_l:
        add("set_timeout")
    rgx_added = re.search(r"(\s+|\.)assert[a-z]+\s*\(", added_lines_l)
    rgx_deleted = re.search(r"(\s+|\.)assert[a-z]+\s*\(", deleted_lines_l)
    if rgx_added and rgx_deleted and rgx_added[0] != rgx_deleted[0] and "change_assertion" not in labels:
        add("change_assertion")

    return {
        "label": ",".join(labels) if labels else "misc",
        "deleted_lines": deleted_lines,
        "added_lines": added_lines,
    }


def clean_flakyfix_generation(text: str) -> str:
    """Mirror FlakyFix's light post-processing for fenced Java generations."""
    case = str(text or "").strip()
    if case.startswith("```java\n"):
        case = case[len("```java\n") :]
    elif case.startswith("```\n"):
        case = case[len("```\n") :]
    if case.endswith("```"):
        case = case[: -len("```")]
    return case.strip()


def preserve_target_method_indent(original_method: str, revised_method: str) -> str:
    """Align full-method generations with the indentation of the replaced method."""
    original_lines = original_method.splitlines()
    revised_lines = revised_method.splitlines()
    original_first = next((line for line in original_lines if line.strip()), "")
    revised_first = next((line for line in revised_lines if line.strip()), "")
    original_indent = re.match(r"\s*", original_first).group(0) if original_first else ""
    revised_indent = re.match(r"\s*", revised_first).group(0) if revised_first else ""
    if not original_indent or revised_first.startswith(original_indent):
        return revised_method
    if revised_indent and len(revised_indent) >= len(original_indent):
        return revised_method
    return "\n".join((original_indent + line if line.strip() else line) for line in revised_lines)


def flakyfix_generation_to_patch(sample: dict[str, str], bundle: dict[str, Any], generation: str) -> tuple[str, dict[str, Any]]:
    test_path = find_test_file(sample)
    rel_path = target_test_relative_path(sample)
    if not test_path or not test_path.exists() or not rel_path:
        return "", {"ok": False, "error_class": "missing_target_test_file", "error": str(test_path or "")}
    original_file = test_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    original_method = str(bundle.get("target_method_code") or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    revised_method = clean_flakyfix_generation(generation).replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not revised_method:
        return "", {"ok": False, "error_class": "empty_flakyfix_generation", "error": ""}
    if not original_method or original_method not in original_file:
        return "", {"ok": False, "error_class": "missing_original_target_method", "error": ""}
    revised_method = preserve_target_method_indent(original_method, revised_method)
    revised_file = original_file.replace(original_method, revised_method, 1)
    patch, meta = unified_diff_for_revised(original_file, revised_file, rel_path)
    meta.update(
        {
            "mode": "flakyfix_full_test_method_to_unified_diff",
            "target_file": rel_path,
            "generated_chars": len(revised_method),
        }
    )
    return patch, meta


@dataclass
class AgentState:
    """Shared blackboard passed between agents for one sample/model task."""

    sample: dict[str, str]
    bundle: dict[str, Any]
    intent_json: dict[str, Any] | None = None
    intent_review_json: dict[str, Any] | None = None
    stability_spec_json: dict[str, Any] | None = None
    context_plan_json: dict[str, Any] | None = None
    selected_intent_index: int = 0
    selected_intent: dict[str, Any] | None = None
    context_snippets: list[dict[str, str]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    def add_message(self, agent: str, output_key: str, output: Any, input_keys: list[str] | None = None) -> None:
        self.messages.append(
            {
                "agent": agent,
                "input_keys": input_keys or [],
                "output_key": output_key,
                "output_preview": bounded_text(json.dumps(output, ensure_ascii=False, sort_keys=True), max_chars=2000),
            }
        )


def task_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("sample_id")), str(row.get("model_alias", row.get("model", ""))), str(row.get("method", "")))


class ExperimentRunner:
    def __init__(self, config: ExperimentConfig, run_id: str) -> None:
        self.config = config
        self.run_id = run_id
        self.run_dir = config.output_root / run_id
        self.results_path = self.run_dir / "results.jsonl"
        self.failed_path = self.run_dir / "failed_tasks.jsonl"
        self.llm_calls_path = self.run_dir / "llm_calls.jsonl"
        self.summary_path = self.run_dir / "summary.json"
        self.lock = threading.Lock()
        self.validation_semaphore = threading.Semaphore(max(1, config.validation_workers))
        self.prompts: dict[str, str] = {}

    def load_prompts(self) -> None:
        names = {
            "stability_intent_reasoning",
            "stability_intent_critic",
            "stability_specification",
            "context_planning",
            "direct_llm_repair",
            "category_guided_repair",
            "intent_only_repair",
            "intent_constrained_repair",
            "intent_constrained_edit_action",
            "intent_constrained_transform",
            "transform_action_revision",
            "patch_apply_repair",
            "validation_summary",
        }
        self.prompts = {name: load_prompt(self.config.prompts_dir, name) for name in names}

    def completed_keys(self) -> set[tuple[str, str, str]]:
        return {task_key(row) for row in read_jsonl(self.results_path)}

    def prepare_heuristic_sample_checkout(self, sample: dict[str, str]) -> tuple[dict[str, str], Path | None]:
        """Create a sample-SHA checkout for deterministic operator baselines.

        The verified-feasible metadata may point at a reusable local repo cache
        rather than a sample-specific worktree. Heuristic operator selection
        reads source before validation, so it must see the same revision that
        validation copies and patches.
        """
        sha = str(sample.get("SHA Detected") or "").strip()
        source = Path(sample.get("remote_repo_dir", ""))
        if not sha or not source.exists():
            return sample, None

        sample_id = str(sample.get("sample_id") or "unknown")
        safe_sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", sample_id)
        checkout_parent = self.run_dir / "source_checkouts" / safe_sample
        checkout = checkout_parent / "repo"
        if checkout_parent.exists():
            shutil.rmtree(checkout_parent, ignore_errors=True)
        checkout_parent.mkdir(parents=True, exist_ok=True)

        clone_cmd = ["git", "clone", "--shared", "--no-checkout", str(source.resolve()), str(checkout)]
        code, output, _timed_out, _elapsed = run_command(clone_cmd, timeout=180)
        if code != 0:
            shutil.rmtree(checkout_parent, ignore_errors=True)
            return sample, None
        checkout_cmd = ["git", "-c", "advice.detachedHead=false", "checkout", "--force", sha]
        code, checkout_output, _timed_out, _elapsed = run_command(checkout_cmd, cwd=checkout, timeout=180)
        if code != 0:
            log_dir = self.run_dir / "source_checkout_logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"{safe_sample}.log").write_text(output + "\n\n" + checkout_output, encoding="utf-8", errors="replace")
            shutil.rmtree(checkout_parent, ignore_errors=True)
            return sample, None

        prepared = dict(sample)
        prepared["remote_repo_dir"] = checkout.as_posix()
        module = prepared.get("validated_module_path") or prepared.get("Module Path") or "."
        module_dir = checkout if module == "." else checkout / module
        simple = prepared.get("test_simple_class") or ""
        candidates: list[str] = []
        if simple and module_dir.exists():
            candidates = [path.as_posix() for path in module_dir.rglob(f"{simple}.java")]
        if not candidates and simple:
            candidates = [path.as_posix() for path in checkout.rglob(f"{simple}.java")]
        if candidates:
            prepared["test_file_candidates_json"] = json.dumps(candidates, ensure_ascii=False)
        return prepared, checkout_parent

    def llm_json(
        self,
        prompt_name: str,
        endpoint: ModelEndpoint,
        sample: dict[str, str],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        prompt = render_prompt(self.prompts[prompt_name], payload)
        leakage = leakage_scan(prompt)
        started = time.time()
        safe_sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample.get("sample_id", "unknown")))
        safe_prompt = re.sub(r"[^A-Za-z0-9_.-]+", "_", prompt_name)
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", endpoint.alias)
        prompt_path = self.run_dir / "rendered_prompts" / safe_model / safe_sample / f"{safe_prompt}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        if self.config.fake_llm:
            parsed = fake_llm_json(prompt_name, sample)
            raw_text = json.dumps(parsed, ensure_ascii=False)
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "elapsed_seconds": 0.0}
        else:
            raw_text, usage = call_openai_compatible(
                endpoint=endpoint,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
                timeout=self.config.llm_timeout,
                seed=self.config.llm_seed,
            )
            try:
                parsed = parse_json_object(raw_text)
            except Exception as exc:
                call_record = {
                    "run_id": self.run_id,
                    "sample_id": sample.get("sample_id"),
                    "model_alias": endpoint.alias,
                    "model": endpoint.model,
                    "prompt_name": prompt_name,
                    "elapsed_seconds": round(time.time() - started, 3),
                    "usage": usage,
                    "leakage_scan": leakage,
                    "rendered_prompt_path": str(prompt_path),
                    "parse_error": repr(exc),
                    "raw_response_preview": raw_text[:4000],
                }
                with self.lock:
                    append_jsonl(self.llm_calls_path, call_record)
                raise
        call_record = {
            "run_id": self.run_id,
            "sample_id": sample.get("sample_id"),
            "model_alias": endpoint.alias,
            "model": endpoint.model,
            "prompt_name": prompt_name,
            "elapsed_seconds": round(time.time() - started, 3),
            "usage": usage,
            "leakage_scan": leakage,
            "rendered_prompt_path": str(prompt_path),
            "raw_response_preview": raw_text[:2000],
        }
        with self.lock:
                append_jsonl(self.llm_calls_path, call_record)
        return parsed, {"usage": usage, "leakage_scan": leakage}

    def llm_text(
        self,
        prompt_name: str,
        endpoint: ModelEndpoint,
        sample: dict[str, str],
        messages: list[dict[str, str]],
        prompt_for_log: str,
        fake_response: str = "",
    ) -> tuple[str, dict[str, Any]]:
        leakage = leakage_scan(prompt_for_log)
        started = time.time()
        safe_sample = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sample.get("sample_id", "unknown")))
        safe_prompt = re.sub(r"[^A-Za-z0-9_.-]+", "_", prompt_name)
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", endpoint.alias)
        prompt_path = self.run_dir / "rendered_prompts" / safe_model / safe_sample / f"{safe_prompt}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt_for_log, encoding="utf-8")
        if self.config.fake_llm:
            raw_text = fake_response
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "elapsed_seconds": 0.0}
        else:
            raw_text, usage = call_openai_compatible(
                endpoint=endpoint,
                messages=messages,
                temperature=self.config.llm_temperature,
                max_tokens=self.config.llm_max_tokens,
                timeout=self.config.llm_timeout,
                seed=self.config.llm_seed,
            )
        call_record = {
            "run_id": self.run_id,
            "sample_id": sample.get("sample_id"),
            "model_alias": endpoint.alias,
            "model": endpoint.model,
            "prompt_name": prompt_name,
            "elapsed_seconds": round(time.time() - started, 3),
            "usage": usage,
            "leakage_scan": leakage,
            "rendered_prompt_path": str(prompt_path),
            "raw_response_preview": raw_text[:2000],
        }
        with self.lock:
            append_jsonl(self.llm_calls_path, call_record)
        return raw_text, {"usage": usage, "leakage_scan": leakage}

    def direct_context_payload(self, sample: dict[str, str], bundle: dict[str, Any]) -> dict[str, Any]:
        return {
            "sample": bundle,
            "patch_instructions": {
                "primary_target_file": bundle.get("test_file_repo_relative", ""),
                "required_format": "complete unified diff with diff --git, --- a/<path>, +++ b/<path>, and @@ hunk headers",
                "headerless_hunks_are_invalid": True,
            },
            "constraints": {
                "do_not_use_developer_patch": True,
                "return_unified_diff_json_only": True,
            },
        }

    def run_flakyfix_original_repair(
        self,
        sample: dict[str, str],
        endpoint: ModelEndpoint,
        bundle: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        label_meta = flakyfix_auto_label_from_patch(sample)
        flaky_code = str(bundle.get("target_method_code") or "")
        label = str(label_meta.get("label") or "misc")
        prompt = (
            "This test case is Flaky: "
            + flaky_code
            + "This test can be fixed by changing following in the code: "
            + label
            + "Just Provide the full fixed code of this test case only without any other text description"
        )
        messages = [
            {"role": "system", "content": "You are a helpful assistant for fixing flaky tests."},
            {"role": "user", "content": prompt},
        ]
        raw_text, repair_meta = self.llm_text(
            "flakyfix_original_repair",
            endpoint,
            sample,
            messages,
            "SYSTEM: You are a helpful assistant for fixing flaky tests.\n\nUSER: " + prompt,
            fake_response=flaky_code,
        )
        patch, conversion = flakyfix_generation_to_patch(sample, bundle, raw_text)
        repair_json = {
            "patch": patch,
            "flakyfix_auto_label": label,
            "raw_generation": clean_flakyfix_generation(raw_text),
            "repair_rationale": "Original FlakyFix-style prompt output converted from full fixed test code to unified diff for validation.",
            "safety_notes": [],
            "flakyfix_label_meta": label_meta,
            "flakyfix_generation_conversion": conversion,
        }
        return repair_json, repair_meta, patch

    def intent_context_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        compact = dict(bundle)
        target_method = str(bundle.get("target_method_code", ""))
        if target_method:
            compact["test_code"] = target_method
            compact["test_code_scope"] = "target_method_only"
        else:
            compact["test_code"] = bounded_text(str(bundle.get("test_code", "")), max_chars=8000)
            compact["test_code_scope"] = "truncated_test_file"
        return compact

    def repair_context_payload(self, bundle: dict[str, Any]) -> dict[str, Any]:
        compact = dict(bundle)
        target_method = str(bundle.get("target_method_code", ""))
        target_method_numbered = str(bundle.get("target_method_numbered_code", ""))
        full_test_code = str(bundle.get("test_code", ""))
        category = str(bundle.get("category", "unknown"))
        if self.config.agent_context_mode == "targeted":
            # ID and OD-Vic operators are mostly line-anchored. Extra snippets
            # increased target-method line-span mistakes in full-dataset runs.
            include_focus_snippets = category in {"NIO", "OD"}
            snippets = extract_focus_snippets(full_test_code, category) if include_focus_snippets else []
            snippet_text = "\n\n".join(
                f"// non-editable context snippet: {item['reason']}\n{item['content']}" for item in snippets[:6]
            )
            source_snippets = compact.get("source_focus_snippets") or []
            source_text = "\n\n".join(
                f"// source-level context ({item.get('path', '')}): {item.get('reason', '')}\n{item.get('content', '')}"
                for item in source_snippets[:6]
            )
            parts = []
            if target_method:
                parts.append("// target flaky test method\n" + target_method)
            if snippet_text:
                parts.append("// category-focused surrounding snippets\n" + snippet_text)
            if source_text:
                parts.append("// guarded source-level evidence\n" + source_text)
            compact["test_code"] = bounded_text("\n\n".join(parts) or full_test_code, max_chars=16000)
            compact["test_code_scope"] = (
                "target_method_plus_non_editable_category_snippets"
                if include_focus_snippets or source_text
                else "target_method_only"
            )
            compact["target_method_numbered_code"] = bounded_text(target_method_numbered, max_chars=12000)
            editable_files = compact.get("editable_files") or []
            if editable_files:
                compact["primary_patch_target"] = editable_files[0].get("repo_relative_path", "")
            return compact
        snippets = extract_focus_snippets(full_test_code, category)
        snippet_text = "\n\n".join(
            f"// snippet: {item['reason']}\n{item['content']}" for item in snippets[:6]
        )
        parts = []
        if target_method:
            parts.append("// target flaky test method\n" + target_method)
        if snippet_text:
            parts.append("// category-focused surrounding snippets\n" + snippet_text)
        compact["test_code"] = bounded_text("\n\n".join(parts) or full_test_code, max_chars=14000)
        compact["test_code_scope"] = "target_method_plus_category_snippets"
        editable_files = compact.get("editable_files") or []
        if editable_files:
            compact["primary_patch_target"] = editable_files[0].get("repo_relative_path", "")
        return compact

    def fallback_intent(self, sample: dict[str, str]) -> dict[str, Any]:
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        return {
            "functional_intent": f"Exercise {sample.get(TEST_FIELD, sample.get('sample_id'))}.",
            "stability_intents": [
                {
                    "intent": f"The known flaky test should remain deterministic across repeated runs for category {category}.",
                    "violation_hypothesis": "The LLM did not return parseable intent JSON; using the dataset category as a low-confidence fallback.",
                    "evidence": [f"known_flaky_category={category}", "intent_parse_fallback"],
                    "repair_principle": "Generate only a minimal safe patch consistent with the known flaky category.",
                    "mapped_category": category,
                    "confidence": "low",
                }
            ],
            "fallback": True,
        }

    def fallback_context_plan(self, sample: dict[str, str]) -> dict[str, Any]:
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        return {
            "selected_intent_index": 0,
            "context_plan": [f"target method and category-focused snippets for {category}"],
            "retrieval_queries": [str(category), str(sample.get("test_method", ""))],
            "context_budget_policy": "small",
            "excluded_context": [],
            "fallback": True,
        }

    def fallback_intent_review(self, sample: dict[str, str], intent_json: dict[str, Any] | None) -> dict[str, Any]:
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        intents = (intent_json or {}).get("stability_intents") or []
        reviews = []
        for index, _intent in enumerate(intents[:3]):
            reviews.append(
                {
                    "intent_index": index,
                    "supporting_evidence": [f"known_flaky_category={category}"],
                    "counter_evidence": [],
                    "missing_evidence": ["critic_parse_fallback"],
                    "repairability": "medium" if index == 0 else "low",
                    "safety_risk": "medium",
                    "verdict": "keep" if index == 0 else "weaken",
                }
            )
        return {
            "intent_reviews": reviews or [
                {
                    "intent_index": 0,
                    "supporting_evidence": [f"known_flaky_category={category}"],
                    "counter_evidence": [],
                    "missing_evidence": ["missing_reasoning_intents", "critic_parse_fallback"],
                    "repairability": "low",
                    "safety_risk": "medium",
                    "verdict": "keep",
                }
            ],
            "selected_intent_index": 0,
            "selection_rationale": "Critic did not return parseable JSON; selecting the first available stability intent.",
            "fallback": True,
        }

    def fallback_stability_spec(self, sample: dict[str, str], selected_intent: dict[str, Any] | None) -> dict[str, Any]:
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        return {
            "root_cause": category,
            "stability_spec": (selected_intent or {}).get(
                "intent",
                f"The target flaky test should be deterministic across repeated runs for category {category}.",
            ),
            "allowed_patch_transforms": [
                "make the target assertion deterministic while preserving its semantic coverage",
            ],
            "forbidden_patch_transforms": [
                "patch another test method",
                "skip or disable the test",
                "delete core assertions",
                "add unrelated helper methods or imports",
                "change only whitespace or formatting",
            ],
            "patch_scope": "target_method_only",
            "validation_obligations": ["patch applies", "target test passes", "10 reruns pass", "unsafe scan passes"],
            "confidence": "low",
            "fallback": True,
        }

    def selected_intent_from_state(self, state: AgentState, use_review: bool) -> tuple[int, dict[str, Any] | None]:
        intents = (state.intent_json or {}).get("stability_intents") or []
        raw_index: Any = 0
        if use_review and state.intent_review_json:
            raw_index = state.intent_review_json.get("selected_intent_index", 0)
        elif state.context_plan_json:
            raw_index = state.context_plan_json.get("selected_intent_index", 0)
        try:
            selected_index = int(raw_index or 0)
        except (TypeError, ValueError):
            selected_index = 0
        if selected_index < 0 or selected_index >= len(intents):
            selected_index = 0
        selected_intent = intents[selected_index] if intents else None
        return selected_index, selected_intent

    def run_reasoning_agent(self, state: AgentState, endpoint: ModelEndpoint) -> None:
        try:
            state.intent_json, _intent_meta = self.llm_json(
                "stability_intent_reasoning",
                endpoint,
                state.sample,
                {"sample": self.intent_context_payload(state.bundle), "expected_output": "stability_intents_json"},
            )
        except ValueError:
            state.intent_json = self.fallback_intent(state.sample)
        state.add_message(
            agent="Stability Intent Reasoning Agent",
            input_keys=["sample"],
            output_key="intent_json",
            output=state.intent_json,
        )

    def run_critic_agent(self, state: AgentState, endpoint: ModelEndpoint) -> None:
        try:
            state.intent_review_json, _review_meta = self.llm_json(
                "stability_intent_critic",
                endpoint,
                state.sample,
                {
                    "sample": self.intent_context_payload(state.bundle),
                    "stability_intent_reasoning": state.intent_json,
                },
            )
        except ValueError:
            state.intent_review_json = self.fallback_intent_review(state.sample, state.intent_json)
        state.selected_intent_index, state.selected_intent = self.selected_intent_from_state(state, use_review=True)
        state.add_message(
            agent="Stability Intent Critic Agent",
            input_keys=["sample", "intent_json"],
            output_key="intent_review_json",
            output=state.intent_review_json,
        )
        state.add_message(
            agent="Stability Intent Selector",
            input_keys=["intent_json", "intent_review_json"],
            output_key="selected_intent",
            output={"selected_intent_index": state.selected_intent_index, "selected_intent": state.selected_intent},
        )

    def run_context_agent(self, state: AgentState, endpoint: ModelEndpoint) -> None:
        category_only_context = self.config.skip_intent_agents and self.config.repair_output_mode == "transform"
        context_payload: dict[str, Any] = {"sample": self.intent_context_payload(state.bundle)}
        if not category_only_context:
            context_payload.update(
                {
                    "stability_intent_reasoning": state.intent_json,
                    "stability_intent_review": state.intent_review_json,
                    "selected_stability_intent": state.selected_intent,
                    "stability_specification": state.stability_spec_json,
                }
            )
        try:
            state.context_plan_json, _plan_meta = self.llm_json(
                "context_planning",
                endpoint,
                state.sample,
                context_payload,
            )
        except ValueError:
            state.context_plan_json = self.fallback_context_plan(state.sample)
        if not state.selected_intent:
            state.selected_intent_index, state.selected_intent = self.selected_intent_from_state(state, use_review=False)
        state.context_snippets = self.retrieve_context(
            state.sample,
            state.bundle,
            state.context_plan_json,
            state.selected_intent,
        )
        state.add_message(
            agent=(
                "Category-Aware Context Planner"
                if category_only_context
                else "Intent-Guided Context Agent"
            ),
            input_keys=(
                ["sample", "known_category"]
                if category_only_context
                else ["sample", "intent_json", "intent_review_json", "selected_intent", "stability_spec_json"]
            ),
            output_key="context_plan_json",
            output=state.context_plan_json,
        )
        state.add_message(
            agent="Deterministic Context Retriever",
            input_keys=["context_plan_json", "known_category"] if category_only_context else ["context_plan_json", "selected_intent"],
            output_key="context_snippets",
            output={"count": len(state.context_snippets), "reasons": [item.get("reason") for item in state.context_snippets]},
        )

    def retrieve_context(
        self,
        sample: dict[str, str],
        bundle: dict[str, Any],
        context_plan: dict[str, Any] | None,
        selected_intent: dict[str, Any] | None,
    ) -> list[dict[str, str]]:
        test_code = str(bundle.get("test_code", ""))
        target_method = str(bundle.get("target_method_code", ""))
        mode = self.config.agent_context_mode
        snippets = []
        if mode == "targeted" and target_method:
            category = str(sample.get("PrimaryCategory", sample.get("Category", "unknown")))
            snippets.append(
                {
                    "path": str(bundle.get("test_file_repo_relative") or bundle.get("test_file", "")),
                    "reason": "target flaky test method only",
                    "content": bounded_text(target_method, max_chars=10000),
                }
            )
            numbered = str(bundle.get("target_method_numbered_code", ""))
            if numbered:
                snippets.append(
                    {
                        "path": str(bundle.get("test_file_repo_relative") or bundle.get("test_file", "")),
                        "reason": "line-numbered target method for hunk anchoring",
                        "content": bounded_text(numbered, max_chars=12000),
                    }
                )
            if category in {"NIO", "OD"}:
                for item in extract_focus_snippets(test_code, category)[:6]:
                    snippets.append(
                        {
                            "path": item.get("path", str(bundle.get("test_file_repo_relative") or bundle.get("test_file", ""))),
                            "reason": "non-editable context snippet: " + str(item.get("reason", "")),
                            "content": bounded_text(str(item.get("content", "")), max_chars=4000),
                        }
                    )
            if category == "ID":
                for item in (bundle.get("source_focus_snippets") or [])[:6]:
                    snippets.append(
                        {
                            "path": str(item.get("path", "")),
                            "reason": "source-level context snippet: " + str(item.get("reason", "")),
                            "content": bounded_text(str(item.get("content", "")), max_chars=4000),
                        }
                    )
        elif mode == "compact" and target_method:
            snippets.append(
                {
                    "path": str(bundle.get("test_file_repo_relative") or bundle.get("test_file", "")),
                    "reason": "target flaky test method with local context",
                    "content": bounded_text(target_method, max_chars=10000),
                }
            )
        elif test_code:
            snippets.append(
                {
                    "path": str(bundle.get("test_file_repo_relative") or bundle.get("test_file", "")),
                    "reason": "target flaky test file",
                    "content": bounded_text(test_code, max_chars=10000),
                }
            )
        elif test_code:
            snippets.append(
                {
                    "path": str(bundle.get("test_file_repo_relative") or bundle.get("test_file", "")),
                    "reason": "target flaky test file",
                    "content": bounded_text(test_code, max_chars=18000),
                }
            )
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        focus_limit = 4 if mode == "compact" else 8
        if mode != "targeted":
            snippets.extend(extract_focus_snippets(test_code, category)[:focus_limit])
        if context_plan:
            snippets.append(
                {
                    "path": "context_plan",
                    "reason": "LLM context planning output",
                    "content": json.dumps(context_plan, ensure_ascii=False, indent=2),
                }
            )
        if selected_intent:
            snippets.append(
                {
                    "path": "selected_intent",
                    "reason": "selected stability intent",
                    "content": json.dumps(selected_intent, ensure_ascii=False, indent=2),
                }
            )
        return snippets[:8] if mode == "compact" else snippets[:12]

    def run_spec_agent(self, state: AgentState, endpoint: ModelEndpoint) -> None:
        try:
            state.stability_spec_json, _spec_meta = self.llm_json(
                "stability_specification",
                endpoint,
                state.sample,
                {
                    "sample": self.intent_context_payload(state.bundle),
                    "selected_stability_intent": state.selected_intent,
                    "stability_intent_review": state.intent_review_json,
                },
            )
        except ValueError:
            state.stability_spec_json = self.fallback_stability_spec(state.sample, state.selected_intent)
        state.add_message(
            agent="Stability Specification Agent",
            input_keys=["sample", "selected_intent", "intent_review_json"],
            output_key="stability_spec_json",
            output=state.stability_spec_json,
        )

    def run_method(
        self,
        sample: dict[str, str],
        endpoint: ModelEndpoint,
        bundle: dict[str, Any],
        method: str,
        intent_json: dict[str, Any] | None,
        intent_review_json: dict[str, Any] | None,
        selected_intent: dict[str, Any] | None,
        context_plan_json: dict[str, Any] | None,
        context_snippets: list[dict[str, str]] | None,
        stability_spec_json: dict[str, Any] | None,
        agent_trace: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
        allowed_transforms = allowed_transforms_for_sample(sample)
        payload: dict[str, Any]
        if method == "direct_llm_repair":
            payload = self.direct_context_payload(sample, self.repair_context_payload(bundle))
        elif method == "category_guided_repair":
            payload = self.direct_context_payload(sample, self.repair_context_payload(bundle))
            payload["category_label"] = category
        elif method == "flakyfix_original_repair":
            payload = {}
        elif method == "intent_only_repair":
            payload = self.direct_context_payload(sample, self.repair_context_payload(bundle))
            payload["stability_intent"] = selected_intent or intent_json
            payload["stability_specification"] = stability_spec_json
        elif method in STABILITYOPS_METHODS or method in HEURISTIC_METHODS:
            stability_intents = (intent_json or {}).get("stability_intents") or []
            has_intent_payload = bool(selected_intent or stability_intents or intent_review_json or stability_spec_json)
            compact_trace = [
                {
                    "agent": item.get("agent"),
                    "input_keys": item.get("input_keys", []),
                    "output_key": item.get("output_key"),
                }
                for item in (agent_trace or [])
            ]
            payload = {
                "sample": self.repair_context_payload(bundle),
                "context_plan": context_plan_json,
                "context_snippets": context_snippets or [],
                "agent_information_flow": compact_trace,
                "patch_instructions": {
                    "primary_target_file": bundle.get("test_file_repo_relative", ""),
                    "known_category": category,
                    "allowed_transforms": allowed_transforms,
                    "repair_output_mode": self.config.repair_output_mode,
                    "required_format": (
                        "StabilityOps DSL transform_action"
                        if self.config.repair_output_mode == "transform"
                        else "exact edit_action old_code/new_code"
                        if self.config.repair_output_mode == "edit_action"
                        else "complete unified diff with diff --git, --- a/<path>, +++ b/<path>, and @@ hunk headers"
                    ),
                    "headerless_hunks_are_invalid": self.config.repair_output_mode not in {"edit_action", "transform"},
                    "empty_patch_policy": "Do not return an empty action when primary_target_file is non-empty and target_method_code is present.",
                },
                "constraints": {
                    "do_not_use_developer_patch": True,
                    "minimal_patch": True,
                    "return_unified_diff_json_only": self.config.repair_output_mode != "edit_action",
                    "allowed_transforms": allowed_transforms,
                    "allowed_transform_policy": "The transform_action.transform must be one of allowed_transforms. If no listed transform applies, return NO_SAFE_TRANSFORM.",
                },
            }
            if has_intent_payload:
                payload.update(
                    {
                        "stability_specification": stability_spec_json,
                        "alternative_stability_intents": [
                            intent for intent in stability_intents if intent is not selected_intent
                        ][:2],
                        "stability_intent_review": intent_review_json,
                    }
                )
                if selected_intent:
                    payload["selected_stability_intent"] = selected_intent
        else:
            raise ValueError(f"unsupported method: {method}")

        prompt_name = PROMPT_BY_METHOD[method]
        if method in STABILITYOPS_METHODS and self.config.repair_output_mode == "edit_action":
            prompt_name = "intent_constrained_edit_action"
        elif method in TRANSFORM_METHODS and self.config.repair_output_mode == "transform":
            prompt_name = "intent_constrained_transform"
        if method == "flakyfix_original_repair":
            repair_json, repair_meta, _patch = self.run_flakyfix_original_repair(sample, endpoint, bundle)
        elif method in HEURISTIC_METHODS:
            repair_json = heuristic_repair_json(sample, bundle)
            repair_meta = {"usage": {}, "leakage_scan": {}}
        else:
            try:
                repair_json, repair_meta = self.llm_json(prompt_name, endpoint, sample, payload)
            except ValueError as exc:
                repair_json = {
                    "patch": "",
                    "changed_files": [],
                    "repair_rationale": f"LLM did not return parseable repair JSON: {exc!r}",
                    "safety_notes": ["treated_as_empty_patch"],
                }
                repair_meta = {"usage": {}, "leakage_scan": {}}
        edit_action_conversion = None
        transform_action_conversion = None
        transform_action_revision_attempted = False
        transform_action_revision_json = None
        transform_action_revision_meta: dict[str, Any] = {"usage": {}, "leakage_scan": {}}
        transform_action_revision_history: list[dict[str, Any]] = []
        planner_stability_spec_json = stability_spec_json
        planner_trace = list(agent_trace or [])
        if method in STABILITYOPS_METHODS and self.config.repair_output_mode == "edit_action":
            patch, edit_action_conversion = patch_from_edit_action(sample, repair_json)
            if not patch.strip():
                repair_json = dict(repair_json)
                repair_json["patch"] = ""
                repair_json["edit_action_conversion"] = edit_action_conversion
        elif method in TRANSFORM_METHODS and self.config.repair_output_mode == "transform":
            spec_from_planner = repair_json.get("stability_spec") or repair_json.get("stability_specification")
            if isinstance(spec_from_planner, dict):
                planner_stability_spec_json = spec_from_planner
            planner_trace.append(
                {
                    "agent": "Stability-Aware Action Planner",
                    "input_keys": (
                        [
                            "sample",
                            "selected_stability_intent",
                            "stability_intent_review",
                            "context_plan_json",
                            "context_snippets",
                        ]
                        if selected_intent or intent_review_json
                        else [
                            "sample",
                            "known_category",
                            "allowed_transforms",
                            "context_plan_json",
                            "context_snippets",
                        ]
                    ),
                    "output_key": "stability_spec_json + transform_action",
                    "output_preview": bounded_text(
                        json.dumps(
                            {
                                "stability_spec": planner_stability_spec_json,
                                "transform_action": repair_json.get("transform_action"),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        max_chars=2000,
                    ),
                }
            )
            patch, transform_action_conversion = patch_from_transform_action(sample, repair_json)
            repair_json = dict(repair_json)
            repair_json["patch"] = patch
            repair_json["transform_action_conversion"] = transform_action_conversion
            for attempt in range(1, self.config.transform_action_repair_attempts + 1):
                if method in HEURISTIC_METHODS:
                    break
                if not (
                    isinstance(transform_action_conversion, dict)
                    and transform_action_conversion.get("ok") is False
                ):
                    break
                if self.config.fake_llm:
                    break
                transform_action_revision_attempted = True
                revision_payload = {
                    "sample": self.repair_context_payload(bundle),
                    "stability_specification": planner_stability_spec_json,
                    "context_plan": context_plan_json,
                    "context_snippets": context_snippets or [],
                    "original_repair_json": repair_json,
                    "executor_rejection": transform_action_conversion,
                    "attempt": attempt,
                    "patch_instructions": {
                        "primary_target_file": bundle.get("test_file_repo_relative", ""),
                        "required_format": "StabilityOps DSL transform_action only",
                        "do_not_generate_patch": True,
                    },
                    "constraints": {
                        "same_dsl_boundary": True,
                        "no_free_form_java": True,
                        "no_unified_diff": True,
                        "use_no_safe_transform_if_still_unsafe": True,
                    },
                }
                try:
                    transform_action_revision_json, transform_action_revision_meta = self.llm_json(
                        "transform_action_revision",
                        endpoint,
                        sample,
                        revision_payload,
                    )
                except ValueError as exc:
                    transform_action_revision_json = {
                        "stability_spec": planner_stability_spec_json,
                        "transform_action": {"transform": "NO_SAFE_TRANSFORM"},
                        "notes": {
                            "rationale": f"Action revision agent did not return parseable JSON: {exc!r}",
                            "risks": ["transform_action_revision_parse_failed"],
                        },
                    }
                candidate_patch, candidate_conversion = patch_from_transform_action(sample, transform_action_revision_json)
                transform_action_revision_history.append(
                    {
                        "attempt": attempt,
                        "input_rejection": transform_action_conversion,
                        "revised_transform_action": transform_action_revision_json.get("transform_action")
                        if isinstance(transform_action_revision_json, dict)
                        else None,
                        "candidate_conversion": candidate_conversion,
                    }
                )
                planner_trace.append(
                    {
                        "agent": "Transform Action Revision Agent",
                        "input_keys": [
                            "sample",
                            "stability_specification",
                            "original_transform_action",
                            "executor_rejection",
                        ],
                        "output_key": "revised transform_action",
                        "output_preview": bounded_text(
                            json.dumps(
                                {
                                    "attempt": attempt,
                                    "transform_action": transform_action_revision_json.get("transform_action")
                                    if isinstance(transform_action_revision_json, dict)
                                    else None,
                                    "transform_action_conversion": candidate_conversion,
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            max_chars=2000,
                        ),
                    }
                )
                repair_json = dict(transform_action_revision_json)
                patch = candidate_patch
                transform_action_conversion = candidate_conversion
                repair_json["patch"] = patch
                repair_json["transform_action_conversion"] = transform_action_conversion
                if isinstance(transform_action_revision_json.get("stability_spec"), dict):
                    planner_stability_spec_json = transform_action_revision_json["stability_spec"]
                if isinstance(transform_action_conversion, dict) and transform_action_conversion.get("ok") is not False:
                    break
        else:
            patch = str(repair_json.get("patch", ""))

        transform_rejected = (
            method in TRANSFORM_METHODS
            and self.config.repair_output_mode == "transform"
            and isinstance(transform_action_conversion, dict)
            and transform_action_conversion.get("ok") is False
        )
        if transform_rejected:
            patch = ""
            repair_json = dict(repair_json)
            repair_json["patch"] = ""
            error_class = str(transform_action_conversion.get("error_class") or "transform_conversion_failed")
            error = str(transform_action_conversion.get("error") or "")
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
                "error_class": error_class,
                "error": error[:2000],
            }
        else:
            with self.validation_semaphore:
                validation = validate_patch(
                    sample=sample,
                    patch=patch,
                    run_dir=self.run_dir,
                    model_alias=endpoint.alias,
                    method=method,
                    mvn=self.config.mvn,
                    reruns=self.config.post_fix_reruns,
                    skip_validation=self.config.skip_validation,
                    cleanup=self.config.cleanup_worktrees,
                    trusted_transform=(transform_action_conversion or {}).get("transform"),
                )
        initial_patch = patch
        initial_validation = validation
        patch_repair_json = None
        patch_repair_meta: dict[str, Any] = {"usage": {}, "leakage_scan": {}}
        patch_repair_attempted = False
        if (
            method in STABILITYOPS_METHODS
            and self.config.full_patch_repair_attempts > 0
            and validation.get("error_class") == "patch_apply_failed"
            and patch.strip()
            and not self.config.fake_llm
        ):
            for attempt in range(1, self.config.full_patch_repair_attempts + 1):
                patch_repair_attempted = True
                repair_payload = {
                    "sample": self.repair_context_payload(bundle),
                    "context_plan": context_plan_json,
                    "original_repair_json": repair_json,
                    "original_patch": patch,
                    "patch_apply_error": validation.get("error", ""),
                    "attempt": attempt,
                    "patch_instructions": {
                        "primary_target_file": bundle.get("test_file_repo_relative", ""),
                        "required_format": "complete unified diff with exact source lines from sample.test_code",
                        "reject_placeholder_hunks": True,
                        "same_intended_change": True,
                    },
                }
                if selected_intent:
                    repair_payload["selected_stability_intent"] = selected_intent
                try:
                    patch_repair_json, patch_repair_meta = self.llm_json(
                        "patch_apply_repair",
                        endpoint,
                        sample,
                        repair_payload,
                    )
                except ValueError as exc:
                    patch_repair_json = {
                        "patch": "",
                        "changed_files": [],
                        "repair_rationale": f"Patch repair agent did not return parseable JSON: {exc!r}",
                        "safety_notes": ["patch_repair_parse_failed"],
                    }
                    break
                candidate_patch = str(patch_repair_json.get("patch", ""))
                if not candidate_patch.strip():
                    break
                with self.validation_semaphore:
                    candidate_validation = validate_patch(
                        sample=sample,
                        patch=candidate_patch,
                        run_dir=self.run_dir,
                        model_alias=endpoint.alias,
                        method=f"{method}__patch_repair_{attempt}",
                        mvn=self.config.mvn,
                        reruns=self.config.post_fix_reruns,
                        skip_validation=self.config.skip_validation,
                        cleanup=self.config.cleanup_worktrees,
                    )
                patch = candidate_patch
                repair_json = patch_repair_json
                validation = candidate_validation
                if validation.get("error_class") != "patch_apply_failed":
                    break
        validation_summary = None
        if (
            method in STABILITYOPS_METHODS
            and self.config.use_validation_summary_agent
            and not self.config.fake_llm
        ):
            summary_payload = {
                "sample_id": sample.get("sample_id"),
                "method": method,
                "validation": validation,
                "unsafe_findings": validation.get("unsafe_findings", []),
            }
            try:
                validation_summary, _summary_meta = self.llm_json("validation_summary", endpoint, sample, summary_payload)
            except Exception as exc:
                validation_summary = {"error": repr(exc)}

        return {
            "run_id": self.run_id,
            "sample_id": sample.get("sample_id"),
            "repo_slug": sample.get("repo_slug"),
            "category": category,
            "model_alias": endpoint.alias,
            "model": endpoint.model,
            "method": method,
            "intent_json": intent_json,
            "intent_review_json": intent_review_json,
            "selected_intent_json": selected_intent,
            "context_plan_json": context_plan_json,
            "stability_spec_json": planner_stability_spec_json,
            "context_snippets_count": len(context_snippets or []),
            "agent_trace_json": planner_trace,
            "repair_json": repair_json,
            "patch": patch,
            "edit_action_conversion_json": edit_action_conversion,
            "transform_action_conversion_json": transform_action_conversion,
            "initial_patch": initial_patch if patch_repair_attempted else "",
            "initial_validation_json": initial_validation if patch_repair_attempted else None,
            "patch_repair_attempted": patch_repair_attempted,
            "patch_repair_json": patch_repair_json,
            "transform_action_revision_attempted": transform_action_revision_attempted,
            "transform_action_revision_json": transform_action_revision_json,
            "transform_action_revision_history_json": transform_action_revision_history,
            "repair_rationale": repair_json.get("repair_rationale", "")
            or (repair_json.get("notes") or {}).get("rationale", ""),
            "safety_notes": repair_json.get("safety_notes", [])
            or (repair_json.get("notes") or {}).get("risks", []),
            "llm_usage": repair_meta.get("usage", {}),
            "patch_repair_llm_usage": patch_repair_meta.get("usage", {}),
            "transform_action_revision_llm_usage": transform_action_revision_meta.get("usage", {}),
            "prompt_leakage_scan": repair_meta.get("leakage_scan", {}),
            "validation_summary_json": validation_summary,
            **validation,
        }

    def run_sample_model(self, sample: dict[str, str], endpoint: ModelEndpoint, methods: list[str]) -> list[dict[str, Any]]:
        source_checkout_parent: Path | None = None
        if methods and all(method in HEURISTIC_METHODS for method in methods):
            sample, source_checkout_parent = self.prepare_heuristic_sample_checkout(sample)
        try:
            bundle = build_sample_bundle(sample)
            return self._run_sample_model_prepared(sample, endpoint, methods, bundle)
        finally:
            if source_checkout_parent and self.config.cleanup_worktrees:
                shutil.rmtree(source_checkout_parent, ignore_errors=True)

    def _run_sample_model_prepared(
        self,
        sample: dict[str, str],
        endpoint: ModelEndpoint,
        methods: list[str],
        bundle: dict[str, Any],
    ) -> list[dict[str, Any]]:
        state = AgentState(sample=sample, bundle=bundle)
        skip_intent_agents = (
            self.config.skip_intent_agents
            and any(method in STABILITYOPS_METHODS for method in methods)
            and self.config.repair_output_mode == "transform"
        )
        needs_intent = (
            any(method in INTENT_AWARE_METHODS for method in methods)
            and not skip_intent_agents
        )
        if needs_intent:
            print(f"[agent] sample={sample.get('sample_id')} model={endpoint.alias} reasoning", flush=True)
            self.run_reasoning_agent(state, endpoint)
        if any(method in STABILITYOPS_METHODS for method in methods):
            if not skip_intent_agents:
                print(f"[agent] sample={sample.get('sample_id')} model={endpoint.alias} critic_selector", flush=True)
                self.run_critic_agent(state, endpoint)
            if self.config.repair_output_mode != "transform" and not skip_intent_agents:
                print(f"[agent] sample={sample.get('sample_id')} model={endpoint.alias} stability_spec", flush=True)
                self.run_spec_agent(state, endpoint)
            print(f"[agent] sample={sample.get('sample_id')} model={endpoint.alias} context", flush=True)
            self.run_context_agent(state, endpoint)
        elif needs_intent:
            state.selected_intent_index, state.selected_intent = self.selected_intent_from_state(state, use_review=False)
            state.add_message(
                agent="Intent-Only Baseline Selector",
                input_keys=["intent_json"],
                output_key="selected_intent",
                output={"selected_intent_index": state.selected_intent_index, "selected_intent": state.selected_intent},
            )

        rows = []
        for method in methods:
            try:
                rows.append(
                    self.run_method(
                        sample,
                        endpoint,
                        bundle,
                        method,
                        state.intent_json,
                        state.intent_review_json if method in STABILITYOPS_METHODS else None,
                        state.selected_intent if method in INTENT_AWARE_METHODS else None,
                        state.context_plan_json if method in STABILITYOPS_METHODS else None,
                        state.context_snippets if method in STABILITYOPS_METHODS else None,
                        state.stability_spec_json if method in INTENT_AWARE_METHODS else None,
                        state.messages if method in STABILITYOPS_METHODS else None,
                    )
                )
            except Exception as exc:
                rows.append(
                    {
                        "run_id": self.run_id,
                        "sample_id": sample.get("sample_id"),
                        "repo_slug": sample.get("repo_slug"),
                        "category": sample.get("PrimaryCategory", sample.get("Category", "unknown")),
                        "model_alias": endpoint.alias,
                        "model": endpoint.model,
                        "method": method,
                        "intent_json": state.intent_json,
                        "intent_review_json": state.intent_review_json if method in STABILITYOPS_METHODS else None,
                        "selected_intent_json": state.selected_intent if method in INTENT_AWARE_METHODS else None,
                        "context_plan_json": state.context_plan_json if method in STABILITYOPS_METHODS else None,
                        "stability_spec_json": state.stability_spec_json if method in INTENT_AWARE_METHODS else None,
                        "context_snippets_count": len(state.context_snippets) if method in STABILITYOPS_METHODS else 0,
                        "agent_trace_json": state.messages if method in STABILITYOPS_METHODS else [],
                        "repair_json": {},
                        "patch": "",
                        "unsafe_patch": False,
                        "compile_passed": False,
                        "target_single_run_passed": False,
                        "post_fix_runs": 0,
                        "post_fix_failures": 0,
                        "decision": "error",
                        "error_class": "method_exception",
                        "error": repr(exc),
                    }
                )
        return rows

    def write_summary(self) -> None:
        rows = read_jsonl(self.results_path)
        summary: dict[str, Any] = {"run_id": self.run_id, "rows": len(rows), "by_method_model": {}}
        for row in rows:
            key = f"{row.get('model_alias')}::{row.get('method')}"
            bucket = summary["by_method_model"].setdefault(
                key,
                {"rows": 0, "repaired": 0, "plausible": 0, "unsafe": 0, "errors": {}},
            )
            bucket["rows"] += 1
            if row.get("decision") == "repaired":
                bucket["repaired"] += 1
            if row.get("compile_passed") and row.get("target_single_run_passed"):
                bucket["plausible"] += 1
            if row.get("unsafe_patch"):
                bucket["unsafe"] += 1
            error_class = str(row.get("error_class", ""))
            if error_class:
                bucket["errors"][error_class] = bucket["errors"].get(error_class, 0) + 1
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if not self.config.dry_run:
            self.append_experiment_log(summary)

    def append_experiment_log(self, summary: dict[str, Any]) -> None:
        log_path = Path("docs/experiment_log.md")
        if not log_path.exists():
            return
        lines = [
            "",
            f"## 自动记录：Agent 实验 {self.run_id}",
            "",
            "记录类型：agent_experiment",
            "",
            "```text",
            f"run_id: {self.run_id}",
            f"dataset: {self.config.dataset}",
            f"run_dir: {self.run_dir}",
            f"results: {self.results_path}",
            f"summary: {self.summary_path}",
            f"models: {', '.join(endpoint.alias for endpoint in self.config.models)}",
            f"methods: {', '.join(self.config.methods)}",
            f"post_fix_reruns: {self.config.post_fix_reruns}",
            f"validation_workers: {self.config.validation_workers}",
            f"agent_context_mode: {self.config.agent_context_mode}",
            f"full_patch_repair_attempts: {self.config.full_patch_repair_attempts}",
            f"use_validation_summary_agent: {self.config.use_validation_summary_agent}",
            f"skip_intent_agents: {self.config.skip_intent_agents}",
            f"rows: {summary.get('rows')}",
            "```",
            "",
            "方法/模型摘要：",
            "",
            "```json",
            json.dumps(summary.get("by_method_model", {}), ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def run(self, resume: bool) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.load_prompts()
        samples = load_samples(self.config.dataset, self.config.limit)
        completed = self.completed_keys() if resume else set()
        tasks: list[tuple[dict[str, str], ModelEndpoint, list[str]]] = []
        for sample in samples:
            for endpoint in self.config.models:
                pending_methods = [
                    method
                    for method in self.config.methods
                    if (sample["sample_id"], endpoint.alias, method) not in completed
                ]
                if pending_methods:
                    tasks.append((sample, endpoint, pending_methods))

        max_workers = max(1, sum(max(1, endpoint.concurrency) for endpoint in self.config.models))
        print(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "samples": len(samples),
                    "tasks": len(tasks),
                    "methods": self.config.methods,
                    "models": [endpoint.alias for endpoint in self.config.models],
                    "max_workers": max_workers,
                    "dry_run": self.config.dry_run,
                    "fake_llm": self.config.fake_llm,
                    "skip_validation": self.config.skip_validation,
                    "agent_context_mode": self.config.agent_context_mode,
                    "skip_intent_agents": self.config.skip_intent_agents,
                    "run_dir": str(self.run_dir),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.run_sample_model, sample, endpoint, methods): (sample, endpoint, methods)
                for sample, endpoint, methods in tasks
            }
            for index, future in enumerate(as_completed(futures), start=1):
                try:
                    result_rows = future.result()
                    with self.lock:
                        for row in result_rows:
                            append_jsonl(self.results_path, row)
                    print(f"[{index}/{len(futures)}] wrote {len(result_rows)} result rows", flush=True)
                except Exception as exc:
                    sample, endpoint, methods = futures[future]
                    failure = {
                        "run_id": self.run_id,
                        "sample_id": sample.get("sample_id"),
                        "model_alias": endpoint.alias,
                        "model": endpoint.model,
                        "methods": methods,
                        "error": repr(exc),
                    }
                    result_rows = [
                        {
                            "run_id": self.run_id,
                            "sample_id": sample.get("sample_id"),
                            "repo_slug": sample.get("repo_slug"),
                            "category": sample.get("PrimaryCategory", sample.get("Category", "unknown")),
                            "model_alias": endpoint.alias,
                            "model": endpoint.model,
                            "method": method,
                            "intent_json": None,
                            "intent_review_json": None,
                            "selected_intent_json": None,
                            "context_plan_json": None,
                            "context_snippets_count": 0,
                            "agent_trace_json": [],
                            "repair_json": {},
                            "patch": "",
                            "unsafe_patch": False,
                            "compile_passed": False,
                            "target_single_run_passed": False,
                            "post_fix_runs": 0,
                            "post_fix_failures": 0,
                            "decision": "error",
                            "error_class": "unhandled_exception",
                            "error": repr(exc),
                        }
                        for method in methods
                    ]
                    with self.lock:
                        append_jsonl(self.failed_path, failure)
                        for row in result_rows:
                            append_jsonl(self.results_path, row)
                    print(f"[{index}/{len(futures)}] failed {exc!r}; wrote {len(result_rows)} error rows", flush=True)
        self.write_summary()
        print(f"summary={self.summary_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fake-llm", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--validation-workers", type=int)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    config = load_config(args.config, dry_run=args.dry_run, fake_llm=args.fake_llm, limit=args.limit)
    if args.skip_validation or args.validation_workers is not None:
        config = replace(
            config,
            skip_validation=bool(config.skip_validation or args.skip_validation),
            validation_workers=args.validation_workers if args.validation_workers is not None else config.validation_workers,
        )
    runner = ExperimentRunner(config, args.run_id)
    runner.run(resume=args.resume)


if __name__ == "__main__":
    main()
