"""Runtime utilities for StabilityOps DSL experiments."""

from __future__ import annotations

import csv
import difflib
import ast
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TEST_FIELD = "Fully-Qualified Test Name (packageName.ClassName.methodName)"


ID_TRANSFORMS = {
    "ID_LIST_ORDER_INSENSITIVE",
    "ID_ASSERTJ_LIST_ORDER_INSENSITIVE",
    "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT",
    "ID_JSON_READTREE_ASSERT",
    "ID_JSON_READTREE_ASSERT_TRY_CATCH",
    "ID_JSON_API_PARSE_ASSERT",
    "ID_JSON_API_METHOD_ASSERTS",
    "ID_JSON_MISSING_TYPE_SETTER",
    "ID_SORT_REFLECTION_RESULTS",
    "ID_SORT_DECLARED_MEMBERS_BY_NAME",
}

NIO_TRANSFORMS = {
    "NIO_STATIC_FIELD_RESET",
    "NIO_STATIC_FIELD_RESET_INFER",
}

OD_TRANSFORMS = {
    "OD_DATABASE_FIXTURE_RESET_SETUP",
    "OD_JSON_GLOBAL_FORMAT_STATE_RESET",
    "OD_RESTORE_ENV_AFTER_MUTATION",
    "OD_RESOURCE_REMOVE_PATH",
}

OD_VIC_TRANSFORMS = {
    "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE",
    "OD_VIC_JOB_REGISTRY_RESET_BEFORE",
    "OD_VIC_RESOURCE_REMOVE_PATH",
    "OD_VIC_SCHEMA_DROP_AFTER",
    "OD_VIC_DATABASE_TABLE_CLEANUP",
}

def canonical_transform_name(transform: str) -> str:
    return str(transform or "").strip().upper()


def allowed_transforms_for_sample(sample: dict[str, str]) -> list[str]:
    category = str(sample.get("PrimaryCategory") or sample.get("Category") or "").strip().upper()
    if category == "ID":
        transforms = ID_TRANSFORMS
    elif category == "NIO":
        transforms = NIO_TRANSFORMS
    elif category == "OD":
        transforms = OD_TRANSFORMS
    elif category == "OD-VIC":
        transforms = OD_VIC_TRANSFORMS
    else:
        transforms = set()
    return sorted([*transforms, "NO_SAFE_TRANSFORM"])


@dataclass(frozen=True)
class ModelEndpoint:
    alias: str
    model: str
    base_url: str
    api_key: str = "EMPTY"
    concurrency: int = 1


@dataclass(frozen=True)
class ExperimentConfig:
    dataset: Path
    output_root: Path
    prompts_dir: Path
    methods: list[str]
    models: list[ModelEndpoint]
    post_fix_reruns: int
    validation_workers: int
    mvn: Path
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: int
    llm_seed: int | None
    dry_run: bool
    fake_llm: bool
    skip_validation: bool
    cleanup_worktrees: bool
    limit: int | None
    context_mode: str
    full_patch_repair_attempts: int
    use_validation_summary: bool
    repair_output_mode: str
    skip_auxiliary_reasoning: bool
    transform_action_repair_attempts: int


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path, dry_run: bool = False, fake_llm: bool = False, limit: int | None = None) -> ExperimentConfig:
    raw = read_json(path)
    models = [
        ModelEndpoint(
            alias=item["alias"],
            model=item["model"],
            base_url=item["base_url"].rstrip("/"),
            api_key=item.get("api_key", "EMPTY"),
            concurrency=int(item.get("concurrency", 1)),
        )
        for item in raw["models"]
    ]
    return ExperimentConfig(
        dataset=Path(raw["dataset"]),
        output_root=Path(raw.get("output_root", "runs/experiments")),
        prompts_dir=Path(raw.get("prompts_dir", "prompts")),
        methods=list(raw["methods"]),
        models=models,
        post_fix_reruns=int(raw.get("post_fix_reruns", 10)),
        validation_workers=int(raw.get("validation_workers", 2)),
        mvn=Path(raw.get("mvn", "tools/apache-maven-3.8.8/bin/mvn")),
        llm_temperature=float(raw.get("llm", {}).get("temperature", 0.2)),
        llm_max_tokens=int(raw.get("llm", {}).get("max_tokens", 4096)),
        llm_timeout=int(raw.get("llm", {}).get("timeout_seconds", 180)),
        llm_seed=raw.get("llm", {}).get("seed"),
        dry_run=bool(raw.get("dry_run", False) or dry_run),
        fake_llm=bool(raw.get("fake_llm", False) or fake_llm),
        skip_validation=bool(raw.get("skip_validation", False) or dry_run),
        cleanup_worktrees=bool(raw.get("cleanup_worktrees", True)),
        limit=limit if limit is not None else raw.get("limit"),
        context_mode=str(raw.get("context_mode", "rich")),
        full_patch_repair_attempts=int(raw.get("full_patch_repair_attempts", 0)),
        use_validation_summary=bool(raw.get("use_validation_summary", False)),
        repair_output_mode=str(raw.get("repair_output_mode", "diff")),
        skip_auxiliary_reasoning=bool(raw.get("skip_auxiliary_reasoning", False)),
        transform_action_repair_attempts=int(raw.get("transform_action_repair_attempts", 0)),
    )


def load_samples(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[:limit] if limit else rows


def load_prompt(prompts_dir: Path, name: str) -> str:
    return (prompts_dir / f"{name}.md").read_text(encoding="utf-8")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def run_command(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> tuple[int, str, bool, float]:
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, False, time.time() - started
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, output + "\n[TIMEOUT]\n", True, time.time() - started


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("LLM response does not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON response is not an object")
    return value


def fake_llm_json(prompt_name: str, sample: dict[str, str]) -> dict[str, Any]:
    if prompt_name == "stabilityops_typed_action":
        return {
            "stability_spec": {
                "required_invariant": "no deterministic repair invariant is inferred in fake mode",
                "evidence_lines": [],
            },
            "transform_action": {
                "transform": "NO_SAFE_TRANSFORM",
                "target_file": "",
            },
            "notes": {
                "rationale": "dry-run fake backend does not select real transforms",
                "risks": ["no transform generated"],
            },
        }
    return {
        "stability_spec": {
            "required_invariant": "unsupported fake prompt",
            "evidence_lines": [],
        },
        "transform_action": {
            "transform": "NO_SAFE_TRANSFORM",
            "target_file": "",
        },
        "notes": {
            "rationale": f"fake backend does not support prompt {prompt_name!r}",
            "risks": ["unsupported_fake_prompt"],
        },
    }


def call_openai_compatible(
    endpoint: ModelEndpoint,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: int,
    seed: int | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = {
        "model": endpoint.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if seed is not None:
        payload["seed"] = int(seed)
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint.base_url}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {endpoint.api_key}",
        },
        method="POST",
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM HTTP {exc.code}: {body[:1000]}") from exc
    result = json.loads(raw)
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    usage["elapsed_seconds"] = round(time.time() - started, 3)
    return content, usage


def test_selector(test_identifier: str) -> str:
    if "#" in test_identifier:
        class_name, method = test_identifier.split("#", 1)
    else:
        class_name, method = test_identifier.rsplit(".", 1)
    return f"{class_name.rsplit('.', 1)[-1]}#{method}"


def sample_test_method(sample: dict[str, str]) -> str:
    method = sample.get("test_method") or ""
    if method:
        return method.split("[", 1)[0]
    identifier = sample.get("validated_test_identifier") or sample.get(TEST_FIELD) or ""
    if "." in identifier:
        return identifier.rsplit(".", 1)[-1].split("[", 1)[0]
    if "#" in identifier:
        return identifier.rsplit("#", 1)[-1].split("[", 1)[0]
    return ""


def find_test_file(sample: dict[str, str]) -> Path | None:
    candidates_raw = sample.get("test_file_candidates_json", "[]")
    try:
        candidates = json.loads(candidates_raw)
    except json.JSONDecodeError:
        candidates = []
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    remote_repo = Path(sample.get("remote_repo_dir", ""))
    module = sample.get("validated_module_path") or sample.get("Module Path", ".") or "."
    module_dir = remote_repo if module == "." else remote_repo / module
    simple = sample.get("test_simple_class", "")
    if module_dir.exists() and simple:
        matches = list(module_dir.rglob(f"{simple}.java"))
        return matches[0] if matches else None
    return None


def bounded_text(text: str, max_chars: int = 24000) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return text[:head] + "\n\n...[truncated]...\n\n" + text[-tail:]


def extract_java_method(test_code: str, method_name: str, context_lines: int = 8) -> str:
    method = extract_java_method_info(test_code, method_name, context_lines=context_lines)
    return method["code"]


def extract_java_imports(test_code: str) -> str:
    imports: list[str] = []
    for line in test_code.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("import static "):
            imports.append(line)
    return "\n".join(imports)


def extract_java_method_info(test_code: str, method_name: str, context_lines: int = 8) -> dict[str, Any]:
    if not test_code or not method_name:
        return {"code": "", "start_line": None, "end_line": None, "numbered_code": ""}
    lines = test_code.splitlines()
    signature = re.compile(rf"\b{re.escape(method_name)}\s*\(")
    method_start = None
    for index, line in enumerate(lines):
        if signature.search(line):
            method_start = index
            break
    if method_start is None:
        return {"code": "", "start_line": None, "end_line": None, "numbered_code": ""}

    brace_depth = 0
    seen_open = False
    method_end = min(len(lines), method_start + 80)
    for index in range(method_start, len(lines)):
        brace_depth += lines[index].count("{")
        if "{" in lines[index]:
            seen_open = True
        brace_depth -= lines[index].count("}")
        if seen_open and brace_depth <= 0 and index > method_start:
            method_end = index + 1
            break
    context_start = max(0, method_start - context_lines)
    context_end = min(len(lines), method_end + context_lines)
    code_lines = lines[context_start:context_end]
    numbered = "\n".join(f"{line_number:5d}: {line}" for line_number, line in enumerate(code_lines, start=context_start + 1))
    return {
        "code": "\n".join(code_lines),
        "start_line": method_start + 1,
        "end_line": method_end,
        "context_start_line": context_start + 1,
        "context_end_line": context_end,
        "numbered_code": numbered,
    }


def extract_focus_snippets(test_code: str, category: str) -> list[dict[str, str]]:
    patterns = {
        "ID": r"HashMap|HashSet|Map<|Set<|stream\(|sorted\(|toString\(|json|serialize|assert",
        "OD": r"static|Before|After|setUp|tearDown|@Before|@After|shared|reset|clear|assert|System\.set|setenv|defaultTimeZone|defaultLocale",
        "OD-Vic": r"static|Before|After|setUp|tearDown|@Before|@After|shared|reset|clear|assert|JobRegistry|zkRegCenter|SchemaUtils|TableUtils|createDao|RuntimeExceptionDao|LoggerFactory",
        "NIO": r"static|ClassRule|Rule|bytesOut|System\.setOut|System\.setErr|cache|reset|clear|iterations|values|testCases|assert",
    }
    pattern = re.compile(patterns.get(category, r"assert|@Test|Before|After"), re.I)
    lines = test_code.splitlines()
    snippets: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        if pattern.search(line):
            start = max(0, index - 5)
            end = min(len(lines), index + 6)
            snippets.append(
                {
                    "path": "target_test_file",
                    "reason": f"matches {category} context pattern",
                    "content": "\n".join(lines[start:end]),
                }
            )
        if len(snippets) >= 8:
            break
    return snippets


def extract_source_focus_snippets(sample: dict[str, str], category: str) -> list[dict[str, str]]:
    if category != "ID":
        return []
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return []
    pattern = re.compile(
        r"getDeclared(?:Methods|Fields|Constructors)\s*\(|getMember(?:Methods|Fields|Constructors)\s*\(|"
        r"assertThat\s+[^;\n]+,\s*(?:is|equalTo)\s*\(|JsonSlurper|JSON\.toJSONString|JSONPath|JSONObject|JSONArray",
        re.I,
    )
    snippets: list[dict[str, str]] = []
    suffixes = {".java", ".groovy"}
    for path in repo_dir.rglob("*"):
        if len(snippets) >= 8:
            break
        if not path.is_file() or path.suffix not in suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not pattern.search(line):
                continue
            start = max(0, index - 4)
            end = min(len(lines), index + 5)
            numbered = "\n".join(
                f"{line_number:5d}: {content}"
                for line_number, content in enumerate(lines[start:end], start=start + 1)
            )
            snippets.append(
                {
                    "path": repo_relative_path(repo_dir, path),
                    "reason": "ID source-level order/JSON evidence",
                    "content": numbered,
                }
            )
            break
    return snippets


def build_sample_bundle(sample: dict[str, str]) -> dict[str, Any]:
    test_path = find_test_file(sample)
    test_code = test_path.read_text(encoding="utf-8", errors="replace") if test_path else ""
    category = sample.get("PrimaryCategory", sample.get("Category", "unknown"))
    test_method = sample_test_method(sample)
    # The editable target method must not include surrounding context. The
    # executor validates action line spans against start_line/end_line, so
    # leaking nearby enum values or helper code into numbered_code can make
    # the LLM select lines that are visible but intentionally non-editable.
    method_info = extract_java_method_info(test_code, test_method, context_lines=0)
    remote_repo = Path(sample.get("remote_repo_dir", ""))
    test_file_repo_relative = ""
    if test_path and remote_repo:
        try:
            test_file_repo_relative = test_path.resolve().relative_to(remote_repo.resolve()).as_posix()
        except ValueError:
            test_file_repo_relative = test_path.name
    return {
        "sample_id": sample.get("sample_id"),
        "dataset": "idoft_verified_feasible",
        "category": category,
        "project": sample.get("repo_slug"),
        "language": "Java",
        "module_path": sample.get("validated_module_path") or sample.get("Module Path"),
        "test_identifier": sample.get("validated_test_identifier") or sample.get(TEST_FIELD),
        "test_method": test_method,
        "maven_test_selector": sample.get("validated_maven_test_selector") or sample.get("maven_test_selector"),
        "test_file": str(test_path) if test_path else "",
        "test_file_repo_relative": test_file_repo_relative,
        "editable_files": [
            {
                "role": "target_flaky_test_file",
                "repo_relative_path": test_file_repo_relative,
                "absolute_source_path": str(test_path) if test_path else "",
            }
        ] if test_path else [],
        "target_method_code": bounded_text(method_info["code"], max_chars=10000),
        "target_method_start_line": method_info["start_line"],
        "target_method_end_line": method_info["end_line"],
        "target_method_numbered_code": bounded_text(method_info["numbered_code"], max_chars=12000),
        "imports_code": bounded_text(extract_java_imports(test_code), max_chars=4000),
        "test_code": bounded_text(test_code),
        "source_focus_snippets": extract_source_focus_snippets(sample, str(category)),
    }


def render_prompt(prompt_text: str, payload: dict[str, Any]) -> str:
    return prompt_text.rstrip() + "\n\n## Input\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def leakage_scan(prompt: str) -> dict[str, Any]:
    findings = []
    suspicious = ["diff --git", "\n+++", "\n---", "@@ ", "Subject: [PATCH]"]
    for item in suspicious:
        if item in prompt:
            findings.append(item)
    return {"leakage_suspected": bool(findings), "findings": findings}


UNSAFE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("skip_or_disable_test", re.compile(r"^\+.*(@Disabled|@Ignore|pytest\.mark\.skip|\.skip\(|skipTest\()", re.I)),
    ("deleted_test_annotation", re.compile(r"^-.*(@Test\s*(?:\(|$)|def\s+test_|^\s*-\s*it\s*\(|^\s*-\s*test\s*\()")),
    ("trivial_assertion", re.compile(r"^\+.*\b(assertTrue\s*\(\s*true\s*\)|assert\s+True|expect\(true\))", re.I)),
    ("fixed_sleep", re.compile(r"^\+.*\b(Thread\.sleep|time\.sleep|sleep\(|setTimeout\()", re.I)),
    ("large_timeout", re.compile(r"^\+.*\b(timeout|Timeout)\b.*\b([3-9]\d{4,}|\d{6,})\b")),
]


ASSERTION_PATTERN = re.compile(r"\b(assert|assertThat|assertEquals|assertTrue|expect\()", re.I)


def scan_patch(text: str) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not text.strip():
        findings.append({"rule": "empty_patch", "line": 0, "content": ""})
    hunk_removed_assertions: list[tuple[int, str]] = []
    hunk_added_assertion = False

    def flush_hunk() -> None:
        nonlocal hunk_removed_assertions, hunk_added_assertion
        if hunk_removed_assertions and not hunk_added_assertion:
            for line_number, content in hunk_removed_assertions:
                findings.append({"rule": "deleted_assertion", "line": line_number, "content": content[:240]})
        hunk_removed_assertions = []
        hunk_added_assertion = False

    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("@@") or line.startswith("diff --git "):
            flush_hunk()
        for rule_name, pattern in UNSAFE_RULES:
            if pattern.search(line):
                findings.append({"rule": rule_name, "line": line_number, "content": line[:240]})
        if line.startswith("-") and not line.startswith("---") and ASSERTION_PATTERN.search(line):
            hunk_removed_assertions.append((line_number, line))
        elif line.startswith("+") and not line.startswith("+++") and ASSERTION_PATTERN.search(line):
            hunk_added_assertion = True
    flush_hunk()
    return {"unsafe": bool(findings), "findings": findings}


def changed_files_from_patch(text: str) -> list[str]:
    files: list[str] = []
    for line in text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                files.append(path[2:] if path.startswith("b/") else path)
        elif line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            files.append(path[2:] if path.startswith("b/") else path)
    deduped: list[str] = []
    for item in files:
        if item not in deduped:
            deduped.append(item)
    return deduped


def patch_substantive_changes(text: str) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    added: list[str] = []
    for line in text.splitlines():
        if line.startswith("---") or line.startswith("+++") or line.startswith("diff --git") or line.startswith("@@"):
            continue
        if line.startswith("-"):
            removed.append(re.sub(r"\s+", "", line[1:]))
        elif line.startswith("+"):
            added.append(re.sub(r"\s+", "", line[1:]))
    return removed, added


CLASS_LEVEL_TRUSTED_TRANSFORMS = {
    "OD_DATABASE_FIXTURE_RESET_SETUP",
    "OD_JSON_GLOBAL_FORMAT_STATE_RESET",
    "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE_CLASS",
}

NON_TARGET_TRUSTED_TRANSFORMS = {
    "ID_SORT_DECLARED_MEMBERS_BY_NAME",
}


def scan_patch_applicability(sample: dict[str, str], text: str, trusted_transform: str | None = None) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    if not text.strip():
        return {"blocked": True, "findings": [{"rule": "empty_patch", "line": 0, "content": ""}]}

    target_file = target_test_relative_path(sample)
    changed_files = changed_files_from_patch(text)
    trusted_transform = (trusted_transform or "").upper()
    allow_non_target_file = trusted_transform in NON_TARGET_TRUSTED_TRANSFORMS
    if target_file and changed_files:
        for changed in changed_files:
            if changed != target_file and not allow_non_target_file:
                findings.append({"rule": "changes_non_target_file", "line": 0, "content": changed, "blocking": True})

    removed, added = patch_substantive_changes(text)
    if (removed or added) and sorted(removed) == sorted(added):
        findings.append({"rule": "formatting_only_patch", "line": 0, "content": "", "blocking": True})

    test_path = find_test_file(sample)
    method_info = {"start_line": None, "end_line": None}
    if test_path:
        test_code = test_path.read_text(encoding="utf-8", errors="replace")
        method_info = extract_java_method_info(test_code, sample_test_method(sample))
    start_line = method_info.get("start_line")
    end_line = method_info.get("end_line")

    allow_class_level_edits = trusted_transform in CLASS_LEVEL_TRUSTED_TRANSFORMS
    helper_pattern = re.compile(r"^\+\s*(?:@\w+(?:\.\w+)*\s+)?(private|public|protected)\s+(?:static\s+)?[\w<>\[\], ?]+\s+\w+\s*\(")
    helper_name_pattern = re.compile(
        r"^\+\s*(?:@\w+(?:\.\w+)*\s+)?(?:private|public|protected)\s+(?:static\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\("
    )
    field_pattern = re.compile(r"^\+\s*(private|public|protected)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\], ?]+\s+\w+\s*(?:=|;)")
    hunk_pattern = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")
    target_method_name = sample_test_method(sample)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("@@"):
            if "<" in line or ">" in line:
                findings.append({"rule": "placeholder_hunk_header", "line": line_number, "content": line[:240], "blocking": True})
            match = hunk_pattern.match(line)
            if match and start_line and end_line:
                old_start = int(match.group(1))
                if old_start < int(start_line) - 8 or old_start > int(end_line) + 8:
                    findings.append(
                        {
                            "rule": "hunk_header_outside_target_method",
                            "line": line_number,
                            "content": line[:240],
                            "target_start_line": start_line,
                            "target_end_line": end_line,
                            "blocking": False,
                        }
                    )
        if line.startswith("+") and not line.startswith("+++"):
            if re.match(r"^\+import\s+", line) and not allow_class_level_edits:
                findings.append({"rule": "adds_import", "line": line_number, "content": line[:240], "blocking": True})
            elif helper_pattern.search(line) and not allow_class_level_edits:
                helper_match = helper_name_pattern.match(line)
                helper_name = helper_match.group(1) if helper_match else ""
                if helper_name != target_method_name:
                    findings.append({"rule": "adds_helper_method", "line": line_number, "content": line[:240], "blocking": True})
            elif field_pattern.search(line) and not allow_class_level_edits:
                findings.append({"rule": "adds_class_field", "line": line_number, "content": line[:240], "blocking": True})
    return {"blocked": any(item.get("blocking") for item in findings), "findings": findings}


def target_test_relative_path(sample: dict[str, str]) -> str:
    test_path = find_test_file(sample)
    remote_repo = Path(sample.get("remote_repo_dir", ""))
    if not test_path or not remote_repo:
        return ""
    try:
        return test_path.resolve().relative_to(remote_repo.resolve()).as_posix()
    except ValueError:
        return test_path.name


def normalize_edit_block(text: Any) -> str:
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")


def edit_action_line_span(action: dict[str, Any]) -> tuple[int | None, int | None]:
    start_raw = (
        action.get("start_line")
        or action.get("old_start_line")
        or action.get("line_start")
        or action.get("from_line")
    )
    end_raw = action.get("end_line") or action.get("old_end_line") or action.get("line_end") or action.get("to_line")
    try:
        start = int(start_raw) if start_raw not in (None, "") else None
        end = int(end_raw) if end_raw not in (None, "") else start
    except (TypeError, ValueError):
        return None, None
    return start, end


def line_span_from_action(action: dict[str, Any]) -> tuple[int | None, int | None]:
    return edit_action_line_span(action)


def validate_action_target(
    sample: dict[str, str],
    action: dict[str, Any],
) -> tuple[Path | None, str, str | None]:
    target_file = target_test_relative_path(sample)
    action_file = str(action.get("target_file") or action.get("file") or "").strip()
    if target_file and action_file and action_file != target_file:
        return None, target_file, f"target_file={action_file!r} expected={target_file!r}"
    test_path = find_test_file(sample)
    if not test_path or not test_path.exists():
        return None, target_file, f"missing target test file: {test_path or ''}"
    return test_path, target_file, None


def unified_diff_for_revised(original: str, revised: str, rel_path: str) -> tuple[str, dict[str, Any]]:
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            revised.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )
    if not diff_lines:
        return "", {"ok": False, "error_class": "empty_generated_diff", "error": ""}
    return f"diff --git a/{rel_path} b/{rel_path}\n" + "".join(diff_lines), {"ok": True}


def repo_relative_path(repo_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_dir.resolve()).as_posix()
    except ValueError:
        return path.name


def synthesize_declared_members_sort_by_name(sample: dict[str, str]) -> tuple[str, dict[str, Any]] | None:
    """Generic guarded operator for declaration-order reflection instability.

    This operator avoids repository-slug checks. It applies only when a source
    file contains a direct declared-member reflection result that is consumed
    without an intervening deterministic sort.
    """
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return None

    candidate_patterns = [
        {
            "member": "METHOD",
            "glob": "*.java",
            "consume": re.compile(
                r"^(?P<indent>\s*)(?P<sink>[A-Za-z_][\w.]*)\.add\((?P<receiver>[A-Za-z_][\w.]*)\.getDeclaredMethods\(\)\);\s*$",
                re.M,
            ),
            "replacement": lambda indent, sink, receiver: (
                f"{indent}java.lang.reflect.Method[] declaredMethods = {receiver}.getDeclaredMethods();\n"
                f"{indent}java.util.Arrays.sort(declaredMethods, java.util.Comparator.comparing(java.lang.reflect.Method::getName));\n"
                f"{indent}{sink}.add(declaredMethods);"
            ),
        },
        {
            "member": "FIELD",
            "glob": "*.java",
            "assign": re.compile(
                r"^(?P<indent>\s*)(?P<prefix>(?:final\s+)?(?:java\.lang\.reflect\.)?Field\[\]\s+)(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<receiver>[A-Za-z_][\w.]*)\.getDeclaredFields\(\);\s*$",
                re.M,
            ),
            "replacement": lambda indent, prefix, var, receiver: (
                f"{indent}{prefix}{var} = {receiver}.getDeclaredFields();\n"
                f"{indent}java.util.Arrays.sort({var}, java.util.Comparator.comparing(java.lang.reflect.Field::getName));"
            ),
        },
        {
            "member": "CONSTRUCTOR",
            "glob": "*.java",
            "assign": re.compile(
                r"^(?P<indent>\s*)(?P<prefix>(?:final\s+)?(?:java\.lang\.reflect\.)?Constructor(?:<[^>]+>)?\[\]\s+)(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<receiver>[A-Za-z_][\w.]*)\.getDeclaredConstructors\(\);\s*$",
                re.M,
            ),
            "replacement": lambda indent, prefix, var, receiver: (
                f"{indent}{prefix}{var} = {receiver}.getDeclaredConstructors();\n"
                f"{indent}java.util.Arrays.sort({var}, java.util.Comparator.comparing(java.lang.reflect.Constructor::getName));"
            ),
        },
    ]

    search_roots: list[Path] = []
    test_path = find_test_file(sample)
    if test_path and test_path.exists():
        search_roots.append(test_path)
    for root_name in ["src/main/java", "src/test/java"]:
        root = repo_dir / root_name
        if root.exists():
            search_roots.extend(root.rglob("*.java"))
    if len(search_roots) <= 1:
        # Multi-module projects often keep sources under module/src/main/java.
        # Scan the repository, but keep the guard tied to declared-member APIs
        # and deterministic local rewrites rather than repository identity.
        search_roots.extend(repo_dir.rglob("*.java"))

    seen: set[Path] = set()
    for path in search_roots:
        if path in seen or not path.exists() or path.is_dir():
            continue
        seen.add(path)
        original = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if "getDeclared" not in original:
            continue
        if "Arrays.sort(" in original or "java.util.Arrays.sort(" in original:
            continue
        for spec in candidate_patterns:
            if "consume" in spec:
                match = spec["consume"].search(original)
                if not match:
                    continue
                revised = original[: match.start()] + spec["replacement"](**match.groupdict()) + original[match.end() :]
            else:
                match = spec["assign"].search(original)
                if not match:
                    continue
                var = match.group("var")
                # Require local evidence that the reflected member array is consumed later.
                tail = original[match.end() :]
                if not re.search(rf"\b{re.escape(var)}\b", tail):
                    continue
                revised = original[: match.start()] + spec["replacement"](**match.groupdict()) + original[match.end() :]
            rel_path = repo_relative_path(repo_dir, path)
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            if not patch.strip() or not meta.get("ok"):
                continue
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_SORT_DECLARED_MEMBERS_BY_NAME",
                    "target_file": rel_path,
                    "guard": f"declared {spec['member'].lower()} reflection result sorted by name",
                }
            )
            return patch, meta
    return None


def synthesize_env_restore_after_mutation(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        start_line, _ = line_span_from_action(action)
        method_start, method_end = enclosing_method_bounds_for_line(original, start_line)
    if not method_start or not method_end:
        return "", {"ok": False, "error_class": "missing_target_method_bounds", "error": ""}
    lines = original.splitlines()
    method_lines = lines[int(method_start) - 1 : int(method_end)]
    method_text = "\n".join(method_lines)

    env_name = str(action.get("variable") or action.get("env_name") or "").strip()
    saved_var = str(action.get("saved_var") or action.get("value") or "").strip()
    receiver = str(action.get("receiver") or "").strip()
    saved_match = re.search(
        r"\b(?:String|final\s+String)\s+([A-Za-z_]\w*)\s*=\s*(?:(?P<receiver>[A-Za-z_][\w.]*)\.)?getenv\s*\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"\s*\)",
        method_text,
    )
    if saved_match:
        saved_var = saved_var or saved_match.group(1)
        receiver = receiver or (saved_match.group("receiver") or "")
        env_name = env_name or saved_match.group(3)
    if not env_name or not saved_var:
        return "", {"ok": False, "error_class": "missing_env_restore_parameters", "error": ""}
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", env_name) or not re.match(r"^[A-Za-z_]\w*$", saved_var):
        return "", {"ok": False, "error_class": "unsafe_env_restore_parameters", "error": ""}

    mutation_receiver = receiver
    mutation_line_abs = 0
    assertion_line_abs = 0
    for offset, line in enumerate(method_lines):
        abs_line = int(method_start) + offset
        setenv_match = re.search(rf"(?:(?P<receiver>[A-Za-z_][\w.]*)\.)?setenv\s*\(\s*\"{re.escape(env_name)}\"\s*,", line)
        putenv_match = re.search(rf"(?:(?P<receiver>[A-Za-z_][\w.]*)\.)?putenv\s*\(\s*\"{re.escape(env_name)}=", line)
        if setenv_match or putenv_match:
            mutation_line_abs = abs_line
            mutation_receiver = mutation_receiver or ((setenv_match or putenv_match).group("receiver") or "")
        if saved_var in line and env_name in line and re.search(r"\bassert(?:Not)?Equals\b", line):
            assertion_line_abs = abs_line
    if not mutation_line_abs:
        return "", {"ok": False, "error_class": "env_mutation_not_visible", "error": env_name}
    if mutation_receiver and not IDENTIFIER_EXPR_RE.match(mutation_receiver):
        return "", {"ok": False, "error_class": "unsafe_env_receiver", "error": mutation_receiver}
    restore = (
        f"{mutation_receiver}.setenv({java_string_literal(env_name)}, {saved_var}, 1);"
        if mutation_receiver
        else f"setenv({java_string_literal(env_name)}, {saved_var}, 1);"
    )
    if restore in method_text:
        return "", {"ok": False, "error_class": "duplicate_env_restore", "error": restore}
    insert_after = action.get("insert_after_line") or assertion_line_abs or mutation_line_abs
    scoped_action = dict(action)
    scoped_action["insert_after_line"] = insert_after
    patch, meta = synthesize_insert_statement_after_line(
        original,
        sample,
        scoped_action,
        rel_path,
        "OD_RESTORE_ENV_AFTER_MUTATION",
        restore,
        allow_duplicate=False,
    )
    if meta.get("ok"):
        meta.update({"variable": env_name, "saved_var": saved_var, "receiver": mutation_receiver})
    return patch, meta


def method_bounds_for_sample(original: str, sample: dict[str, str]) -> tuple[int | None, int | None]:
    method_info = extract_java_method_info(original, sample_test_method(sample), context_lines=0)
    return method_info.get("start_line"), method_info.get("end_line")


def enclosing_method_bounds_for_line(original: str, line_no: int | None) -> tuple[int | None, int | None]:
    """Infer a Java/Groovy method body around a selected line.

    This is a fallback for inherited/parameterized test metadata where the
    dataset method name is not present in the editable target file, but the LLM
    selected a concrete assertion span inside the file.
    """
    if not line_no:
        return None, None
    lines = original.splitlines()
    if line_no < 1 or line_no > len(lines):
        return None, None
    control_keywords = {"if", "for", "while", "switch", "catch", "try", "else", "do", "synchronized"}
    signature_index = None
    signature_pattern = re.compile(
        r"\b(?:public|protected|private|static|final|void|def|[\w<>\[\],.?]+\s+)+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws\s+[^{]+)?\{?"
    )
    for index in range(line_no - 1, -1, -1):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = signature_pattern.search(stripped)
        if not match:
            continue
        if match.group("name") in control_keywords:
            continue
        signature_index = index
        break
    if signature_index is None:
        return None, None

    brace_depth = 0
    seen_open = False
    for index in range(signature_index, len(lines)):
        brace_depth += lines[index].count("{")
        if "{" in lines[index]:
            seen_open = True
        brace_depth -= lines[index].count("}")
        if seen_open and brace_depth <= 0 and index > signature_index:
            return signature_index + 1, index + 1
    return signature_index + 1, min(len(lines), signature_index + 80)


def validate_line_span(
    span_start: int | None,
    span_end: int | None,
    method_start: int | None,
    method_end: int | None,
) -> str | None:
    if not span_start or not span_end:
        return "missing line span"
    if not method_start or not method_end:
        return "missing target method bounds"
    if span_start < int(method_start) or span_end > int(method_end) or span_start > span_end:
        return f"line span {span_start}-{span_end} outside target method {method_start}-{method_end}"
    return None


def java_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def class_name_from_class_literal(value: str) -> str | None:
    """Return the simple class name from a Java class literal expression."""
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Za-z_][\w.]*)\.class", text)
    if not match:
        return None
    return match.group(1).split(".")[-1]


def java_type_visible(original: str, type_name: str) -> bool:
    """Conservative visibility check for typed DSL parameters.

    The executor should refuse to materialize a patch if a typed parameter
    references a class that is not already visible in the target test file.
    """
    simple = str(type_name or "").strip().split(".")[-1]
    if not simple or not re.match(r"^[A-Za-z_]\w*$", simple):
        return False
    patterns = [
        rf"\b(class|interface|enum)\s+{re.escape(simple)}\b",
        rf"\b{re.escape(simple)}\s*\.class\b",
        rf"\b{re.escape(simple)}\s*[<,>\s)]",
    ]
    return any(re.search(pattern, original) for pattern in patterns)


def receiver_root(receiver: str) -> str:
    text = str(receiver or "").strip()
    if text.startswith("this."):
        text = text[5:]
    return text.split(".")[0]


def receiver_visible_before_line(
    original: str,
    sample: dict[str, str],
    receiver: str,
    line_no: int | None,
) -> tuple[bool, str]:
    root = receiver_root(receiver)
    if not root or not re.match(r"^[A-Za-z_]\w*$", root):
        return False, "invalid_receiver"
    if not line_no:
        return False, "missing_insert_line"

    lines = original.splitlines()
    method_start, _ = method_bounds_for_sample(original, sample)
    class_prefix = "\n".join(lines[: max(int(method_start or 1) - 1, 0)])
    method_prefix = "\n".join(lines[int(method_start or 1) - 1 : max(int(line_no) - 1, 0)])
    declaration = rf"\b[A-Za-z_][\w<>\[\].?,\s]*\s+{re.escape(root)}\s*(?:=|;|,|\))"
    if re.search(declaration, method_prefix):
        return True, "local_or_parameter"
    if re.search(declaration, class_prefix):
        return True, "field"
    if re.search(rf"\b{re.escape(root)}\b", method_prefix):
        return True, "used_before_insert"
    return False, "receiver_not_visible_before_insert"


def resource_receiver_applicable(original: str, sample: dict[str, str], receiver: str) -> tuple[bool, str]:
    """Guard OD resource cleanup against accidental DAO/mock rewrites."""
    root = receiver_root(receiver)
    lowered = root.lower()
    if lowered in {"dao", "rtdao", "connection", "connectionsource", "conn", "savepoint"}:
        return False, "inapplicable_resource_remove_receiver"

    method_start, method_end = method_bounds_for_sample(original, sample)
    lines = original.splitlines()
    method_text = "\n".join(lines[int(method_start or 1) - 1 : int(method_end or 0)])
    class_text = "\n".join(lines[: int(method_start or 1) - 1])
    combined = class_text + "\n" + method_text
    receiver_tokens = ("zk", "zookeeper", "registry", "regcenter", "center", "curator", "client")
    if any(token in lowered for token in receiver_tokens):
        return True, "receiver_name_matches_resource_handle"
    if re.search(rf"\b{re.escape(root)}\b.*\.(forPath|persistSequential|persistEphemeralSequential|isExisted)\(", combined):
        return True, "receiver_used_as_external_resource_handle"
    return False, "inapplicable_resource_remove_receiver"


def project_mentions_jackson(sample: dict[str, str], original: str) -> bool:
    """Return whether Jackson is already part of the target project context."""
    if "com.fasterxml.jackson" in original or "ObjectMapper" in original or "JsonNode" in original:
        return True
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return False
    build_files: list[Path] = []
    for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        build_files.extend(repo_dir.rglob(name))
        if len(build_files) >= 40:
            break
    for build_file in build_files[:40]:
        try:
            text = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "com.fasterxml.jackson" in text or "jackson-databind" in text:
            return True
    return False


def project_mentions_object_json_parser(sample: dict[str, str], original: str) -> bool:
    """Return whether an object-level JSON parser is already part of the target project context."""
    if "com.google.gson" in original or "Gson" in original:
        return True
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return False
    build_files: list[Path] = []
    for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        build_files.extend(repo_dir.rglob(name))
        if len(build_files) >= 40:
            break
    for build_file in build_files[:40]:
        try:
            text = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "com.google.code.gson" in text or "gson" in text:
            return True
    return False


def synthesize_list_order_insensitive(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    indexed_get_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*(?P<expected>.+?)\s*,\s*(?P<collection>[\w.]+)\.get\s*\(\s*\d+\s*\)\s*\)\s*;\s*$"
    )
    indexed_get_accessor_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*(?P<expected>.+?)\s*,\s*"
        r"(?P<collection>[\w.]+)\.get\s*\(\s*\d+\s*\)(?P<accessor>(?:\.[A-Za-z_]\w*(?:\(\))?)+)\s*\)\s*;\s*$"
    )
    expected_list_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*(?P<expected>(?:Arrays\.asList|java\.util\.Arrays\.asList|Lists\.newArrayList|ImmutableList\.of|List\.of|java\.util\.List\.of)\s*\(.+\))\s*,\s*(?P<actual>[^;]+?)\s*\)\s*;\s*$"
    )
    assert_array_equals_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertArrayEquals\s*\(\s*(?P<expected>.+?)\s*,\s*(?P<actual>.+?)\s*\)\s*;\s*$"
    )
    for line in lines:
        match = indexed_get_accessor_pattern.match(line)
        if match:
            accessor = match.group("accessor").strip()
            if not re.fullmatch(r"(?:\.[A-Za-z_]\w*(?:\(\))?)+", accessor):
                return [], {"ok": False, "error_class": "unsafe_indexed_accessor", "error": accessor}
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('collection')}.stream().anyMatch(item -> java.util.Objects.equals({match.group('expected')}, item{accessor})));"
            )
            changed += 1
            continue
        match = indexed_get_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('collection')}.contains({match.group('expected')}));"
            )
            changed += 1
            continue
        match = expected_list_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertEquals(new java.util.HashSet({match.group('expected')}), new java.util.HashSet({match.group('actual')}));"
            )
            changed += 1
            continue
        match = assert_array_equals_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertEquals(new java.util.HashSet(java.util.Arrays.asList({match.group('expected')})), new java.util.HashSet(java.util.Arrays.asList({match.group('actual')})));"
            )
            changed += 1
            continue
        generated.append(line)
    if changed == 0:
        return [], {"ok": False, "error_class": "no_order_sensitive_get_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_LIST_ORDER_INSENSITIVE"}


def synthesize_assertj_order_insensitive(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    list_equal_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:Assertions\.)?)assertThat\s*\(\s*(?P<actual>.+?)\s*\)\s*"
        r"\.\s*isEqualTo\s*\(\s*(?P<expected>(?:Lists\.newArrayList|Arrays\.asList|java\.util\.Arrays\.asList)\s*\(.+\))\s*\)\s*;\s*$"
    )
    contains_exactly_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:Assertions\.)?)assertThat\s*\(\s*(?P<actual>.+?)\s*\)\s*"
        r"\.\s*containsExactly\s*\(\s*(?P<items>.+)\s*\)\s*;\s*$"
    )
    contains_elements_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:Assertions\.)?)assertThat\s*\(\s*(?P<actual>.+?)\s*\)\s*"
        r"\.\s*containsExactlyElementsOf\s*\(\s*(?P<expected>.+)\s*\)\s*;\s*$"
    )
    hamcrest_contains_pattern = re.compile(
        r"^(?P<indent>\s*)assertThat\s*\(\s*(?P<actual>.+?)\s*,\s*(?:org\.hamcrest\.Matchers\.)?contains\s*\(\s*(?P<items>.+)\s*\)\s*\)\s*;\s*$"
    )
    hamcrest_is_list_pattern = re.compile(
        r"^(?P<indent>\s*)assertThat\s*\(\s*(?P<actual>.+?)\s*,\s*is\s*\(\s*(?P<expected>(?:Arrays\.asList|java\.util\.Arrays\.asList|Lists\.newArrayList|ImmutableList\.of|List\.of|java\.util\.List\.of)\s*\(.+\))\s*\)\s*\)\s*;\s*$"
    )
    for line in lines:
        match = list_equal_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            expected = match.group("expected").strip()
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertThat(new java.util.HashSet((java.util.Collection<?>) {actual})).isEqualTo(new java.util.HashSet({expected}));"
            )
            changed += 1
            continue
        match = contains_exactly_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            items = match.group("items").strip()
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertThat(new java.util.HashSet((java.util.Collection<?>) {actual})).isEqualTo(new java.util.HashSet(java.util.Arrays.asList({items})));"
            )
            changed += 1
            continue
        match = contains_elements_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            expected = match.group("expected").strip()
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertThat(new java.util.HashSet((java.util.Collection<?>) {actual})).isEqualTo(new java.util.HashSet({expected}));"
            )
            changed += 1
            continue
        match = hamcrest_contains_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            items = match.group("items").strip()
            generated.append(
                f"{match.group('indent')}assertThat({actual}, org.hamcrest.Matchers.containsInAnyOrder({items}));"
            )
            changed += 1
            continue
        match = hamcrest_is_list_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            expected = match.group("expected").strip()
            generated.append(
                f"{match.group('indent')}assertEquals(new java.util.HashSet({expected}), new java.util.HashSet((java.util.Collection<?>) {actual}));"
            )
            changed += 1
            continue
        generated.append(line)
    if not changed:
        return [], {"ok": False, "error_class": "no_assertj_order_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_ASSERTJ_LIST_ORDER_INSENSITIVE", "changed_assertions": changed}


def synthesize_query_string_order_insensitive(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*\"(?P<expected>[^\"]*[&=][^\"]*)\"\s*,\s*(?P<actual>[^;]+?)\s*\)\s*;\s*$"
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            if ".assertQueryString(" in line:
                return [], {"ok": False, "error_class": "unsupported_query_fluent_assertion", "error": line.strip()}
            if line.strip():
                return [], {"ok": False, "error_class": "unsupported_query_assertion_line", "error": line.strip()}
            generated.append(line)
            continue
        expected = match.group("expected").strip()
        actual = match.group("actual").strip()
        if not expected or "&" not in expected:
            return [], {"ok": False, "error_class": "query_expected_not_compound", "error": expected}
        expected_literal = java_string_literal(expected)
        generated.append(
            f"{match.group('indent')}{match.group('prefix')}assertEquals(new java.util.HashSet(java.util.Arrays.asList({expected_literal}.split(\"&\"))), new java.util.HashSet(java.util.Arrays.asList({actual}.split(\"&\"))));"
        )
        changed += 1
    if not changed:
        return [], {"ok": False, "error_class": "no_query_string_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT", "changed_assertions": changed}


def split_assert_equals_arguments(block: str) -> tuple[str, str, str] | None:
    start = block.find("assertEquals")
    if start < 0:
        return None
    open_index = block.find("(", start)
    close_index = block.rfind(");")
    if open_index < 0 or close_index < open_index:
        return None
    body = block[open_index + 1 : close_index]
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    comma_index = None
    for index, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and (in_string or in_char):
            escaped = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if in_char:
            if ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "'":
            in_char = True
            continue
        if ch in "([{<":
            depth += 1
            continue
        if ch in ")]}>":
            depth = max(depth - 1, 0)
            continue
        if ch == "," and depth == 0:
            comma_index = index
            break
    if comma_index is None:
        return None
    indent_match = re.match(r"^(\s*)", block)
    return indent_match.group(1) if indent_match else "", body[:comma_index].strip(), body[comma_index + 1 :].strip()


def split_top_level_two_args(body: str) -> tuple[str, str] | None:
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for index, ch in enumerate(body):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and (in_string or in_char):
            escaped = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if in_char:
            if ch == "'":
                in_char = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "'":
            in_char = True
            continue
        if ch in "([{<":
            depth += 1
            continue
        if ch in ")]}>":
            depth = max(depth - 1, 0)
            continue
        if ch == "," and depth == 0:
            return body[:index].strip(), body[index + 1 :].strip()
    return None


def split_assert_that_json_arguments(block: str) -> tuple[str, str, str] | None:
    start = block.find("assertThat")
    if start < 0:
        return None
    open_index = block.find("(", start)
    close_index = block.rfind(");")
    if open_index < 0 or close_index < open_index:
        return None
    body = block[open_index + 1 : close_index]
    split = split_top_level_two_args(body)
    if not split:
        return None
    actual, matcher = split
    matcher = matcher.strip()
    matcher_match = re.fullmatch(
        r"(?:org\.hamcrest\.Matchers\.)?(?:is|equalTo)\s*\(\s*(?P<expected>.+)\s*\)",
        matcher,
        flags=re.S,
    )
    if not matcher_match:
        return None
    indent_match = re.match(r"^(\s*)", block)
    return indent_match.group(1) if indent_match else "", matcher_match.group("expected").strip(), actual.strip()


def split_groovy_assert_that_json_arguments(block: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^(?P<indent>\s*)assertThat\s+(?P<actual>.+?)\s*,\s*(?:is|equalTo)\s*\(\s*(?P<expected>.+?)\s*\)\s*$",
        block.strip() and block or "",
    )
    if not match:
        return None
    return match.group("indent"), match.group("expected").strip(), match.group("actual").strip()


def assert_equals_prefix(block: str) -> str:
    match = re.match(r"^(?P<indent>\s*)(?P<prefix>(?:[A-Za-z_][\w.]*\.)?)assertEquals\s*\(", block.strip() and block or "")
    return match.group("prefix") if match else ""


def java_string_literal_value(expr: str) -> str | None:
    text = str(expr or "").strip()
    if not re.fullmatch(r'"(?:\\.|[^"\\])*"', text, flags=re.S):
        return None
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return None


def json_readtree_expr_allowed(expr: str) -> tuple[bool, str]:
    literal = java_string_literal_value(expr)
    if literal is None:
        return True, ""
    try:
        json.loads(literal)
    except json.JSONDecodeError:
        return False, "json_string_literal_not_strict_json"
    return True, ""


def json_api_parse_template(original: str, sample: dict[str, str]) -> tuple[str | None, str]:
    if "com.alibaba.fastjson2.JSON" in original:
        return "com.alibaba.fastjson2.JSON.parse({expr})", ""
    if "com.alibaba.fastjson.JSON" in original:
        return "com.alibaba.fastjson.JSON.parse({expr})", ""
    if "JSON." in original or re.search(r"import\s+com\.alibaba\.fastjson2?\.JSON\s*;", original):
        if re.search(r"import\s+com\.alibaba\.fastjson2\.JSON\s*;", original):
            return "com.alibaba.fastjson2.JSON.parse({expr})", ""
        if re.search(r"import\s+com\.alibaba\.fastjson\.JSON\s*;", original):
            return "com.alibaba.fastjson.JSON.parse({expr})", ""
        return "JSON.parse({expr})", ""
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    json_api_v2 = False
    json_api_v1 = False
    if repo_dir.exists():
        source_hits = 0
        for source in repo_dir.rglob("*.java"):
            if source_hits >= 80:
                break
            try:
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            source_hits += 1
            json_api_v2 = json_api_v2 or "com.alibaba.fastjson2" in text
            json_api_v1 = json_api_v1 or "com.alibaba.fastjson" in text
            if json_api_v2 or json_api_v1:
                break
        for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
            for build_file in list(repo_dir.rglob(name))[:40]:
                try:
                    text = build_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                json_api_v2 = json_api_v2 or "fastjson2" in text or "com.alibaba.fastjson2" in text
                json_api_v1 = json_api_v1 or "fastjson" in text or "com.alibaba.fastjson" in text
    if json_api_v2:
        return "com.alibaba.fastjson2.JSON.parse({expr})", ""
    if json_api_v1:
        return "com.alibaba.fastjson.JSON.parse({expr})", ""
    return None, "json_api_parser_not_visible"


def synthesize_json_semantic_assert(lines: list[str], wrapper_template: str, transform_name: str) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    for line in lines:
        if not line.strip():
            generated.append(line)
            continue
        parsed = split_assert_equals_arguments(line)
        if not parsed:
            parsed = split_assert_that_json_arguments(line)
            if not parsed:
                parsed = split_groovy_assert_that_json_arguments(line)
                if not parsed:
                    generated.append(line)
                    continue
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        if not expected_ok or not actual_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error,
                "error": expected if not expected_ok else actual,
            }
        if split_groovy_assert_that_json_arguments(line):
            generated.append(
                f"{indent}assert new groovy.json.JsonSlurper().parseText({actual}) == new groovy.json.JsonSlurper().parseText({expected})"
            )
            changed += 1
            continue
        prefix = assert_equals_prefix(line)
        if not prefix and "assertThat" in line:
            prefix = "org.junit.Assert."
        generated.append(
            f"{indent}{prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});"
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        parsed = split_assert_equals_arguments(block)
        if not parsed:
            parsed = split_assert_that_json_arguments(block)
            if not parsed:
                parsed = split_groovy_assert_that_json_arguments(block)
                if not parsed:
                    return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        if not expected_ok or not actual_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error,
                "error": expected if not expected_ok else actual,
            }
        if split_groovy_assert_that_json_arguments(block):
            generated = [
                f"{indent}assert new groovy.json.JsonSlurper().parseText({actual}) == new groovy.json.JsonSlurper().parseText({expected})"
            ]
            changed = 1
            return generated, {"ok": True, "transform": transform_name, "changed_assertions": changed}
        prefix = assert_equals_prefix(block)
        if not prefix and "assertThat" in block:
            prefix = "org.junit.Assert."
        generated = [
            f"{indent}{prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});"
        ]
        changed = 1
    return generated, {"ok": True, "transform": transform_name, "changed_assertions": changed}


def synthesize_json_semantic_assert_try_catch(
    lines: list[str],
    wrapper_template: str,
    transform_name: str,
) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    for line in lines:
        if not line.strip():
            generated.append(line)
            continue
        parsed = split_assert_equals_arguments(line)
        if not parsed:
            parsed = split_assert_that_json_arguments(line)
            if not parsed:
                generated.append(line)
                continue
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        if not expected_ok or not actual_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error,
                "error": expected if not expected_ok else actual,
            }
        prefix = assert_equals_prefix(line)
        if not prefix and "assertThat" in line:
            prefix = "org.junit.Assert."
        generated.extend(
            [
                f"{indent}try {{",
                f"{indent}    {prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});",
                f"{indent}}} catch (Exception e) {{",
                f"{indent}    throw new AssertionError(e);",
                f"{indent}}}",
            ]
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        parsed = split_assert_equals_arguments(block)
        if not parsed:
            parsed = split_assert_that_json_arguments(block)
            if not parsed:
                return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        if not expected_ok or not actual_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error,
                "error": expected if not expected_ok else actual,
            }
        prefix = assert_equals_prefix(block)
        if not prefix and "assertThat" in block:
            prefix = "org.junit.Assert."
        generated = [
            f"{indent}try {{",
            f"{indent}    {prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});",
            f"{indent}}} catch (Exception e) {{",
            f"{indent}    throw new AssertionError(e);",
            f"{indent}}}",
        ]
        changed = 1
    return generated, {"ok": True, "transform": transform_name, "changed_assertions": changed}


def synthesize_json_api_parse_assert(lines: list[str], wrapper_template: str) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    for line in lines:
        if not line.strip():
            generated.append(line)
            continue
        parsed = split_assert_equals_arguments(line)
        if not parsed:
            parsed = split_assert_that_json_arguments(line)
            if not parsed:
                generated.append(line)
                continue
        indent, expected, actual = parsed
        prefix = assert_equals_prefix(line)
        if not prefix and "assertThat" in line:
            prefix = "org.junit.Assert."
        generated.append(
            f"{indent}{prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});"
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        parsed = split_assert_equals_arguments(block)
        if not parsed:
            parsed = split_assert_that_json_arguments(block)
            if not parsed:
                return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        prefix = assert_equals_prefix(block)
        if not prefix and "assertThat" in block:
            prefix = "org.junit.Assert."
        generated = [
            f"{indent}{prefix}assertEquals({wrapper_template.format(expr=expected)}, {wrapper_template.format(expr=actual)});"
        ]
        changed = 1
    return generated, {"ok": True, "transform": "ID_JSON_API_PARSE_ASSERT", "changed_assertions": changed}


def expression_looks_jsonish(expr: str) -> bool:
    text = str(expr or "").strip()
    literal = java_string_literal_value(text)
    if literal is not None:
        stripped = literal.strip()
        return stripped.startswith("{") or stripped.startswith("[")
    return any(token in text for token in ["JSON.toJSONString", "JSONPath", "JSONObject", "JSONArray"])


def synthesize_json_api_method_json_asserts(
    original: str,
    sample: dict[str, str],
    rel_path: str,
) -> tuple[str, dict[str, Any]] | None:
    wrapper_template, wrapper_error = json_api_parse_template(original, sample)
    if not wrapper_template:
        return None
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return None
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    changed = 0
    revised_lines = list(lines_keepends)
    for index in range(int(method_start) - 1, int(method_end)):
        line = lines_plain[index]
        parsed = split_assert_equals_arguments(line)
        if not parsed:
            continue
        indent, expected, actual = parsed
        if not (expression_looks_jsonish(expected) and expression_looks_jsonish(actual)):
            continue
        prefix = assert_equals_prefix(line)
        revised_lines[index] = (
            f"{indent}{prefix}assertEquals("
            f"{wrapper_template.format(expr=expected)}, "
            f"{wrapper_template.format(expr=actual)});\n"
        )
        changed += 1
    if not changed:
        return None
    revised = "".join(revised_lines)
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    if not patch.strip() or not meta.get("ok"):
        return None
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_JSON_API_METHOD_ASSERTS",
            "target_file": rel_path,
            "changed_assertions": changed,
            "guard": "visible JSON API assertions in target method",
        }
    )
    return patch, meta


def class_declaration_insert_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if re.search(r"\bclass\s+\w+", line) and "{" in line:
            return index + 1
    return None


def synthesize_class_setup_method(original: str, setup_method: str, rel_path: str, marker: str) -> tuple[str, dict[str, Any]]:
    if marker in original:
        return "", {"ok": False, "error_class": "duplicate_setup_transform", "error": marker}
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    insert_index = class_declaration_insert_index(lines_plain)
    if insert_index is None:
        return "", {"ok": False, "error_class": "missing_class_declaration", "error": ""}
    insert_text = "\n" + setup_method.rstrip() + "\n"
    revised = "".join(lines_keepends[:insert_index]) + insert_text + "".join(lines_keepends[insert_index:])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update({"mode": "transform_to_unified_diff", "target_file": rel_path})
    return patch, meta


IDENTIFIER_EXPR_RE = re.compile(r"^[A-Za-z_][\w.]*$")
FIELD_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
RESET_OPERATIONS = {
    "ASSIGN_ZERO": "{receiver}.{field} = 0;",
    "ASSIGN_FALSE": "{receiver}.{field} = false;",
    "ASSIGN_NULL": "{receiver}.{field} = null;",
    "CLEAR_COLLECTION": "{receiver}.{field}.clear();",
}


def typed_reset_statement(reset: Any) -> tuple[str | None, dict[str, str] | None, str]:
    """Convert a typed reset action into executor-generated Java code."""
    if not isinstance(reset, dict):
        return None, None, "reset_item_not_object"
    receiver = str(reset.get("receiver") or "").strip()
    field = str(reset.get("field") or "").strip()
    operation = str(reset.get("operation") or "").strip().upper()
    if not IDENTIFIER_EXPR_RE.match(receiver):
        return None, None, "unsafe_reset_receiver"
    if not FIELD_NAME_RE.match(field):
        return None, None, "unsafe_reset_field"
    if operation not in RESET_OPERATIONS:
        return None, None, "unsupported_reset_operation"
    typed = {"receiver": receiver, "field": field, "operation": operation}
    statement = RESET_OPERATIONS[operation].format(**typed)
    return statement, typed, ""


def infer_static_reset_operation(original: str, receiver: str, field: str) -> tuple[str | None, str]:
    declaration_pattern = re.compile(
        rf"\bstatic\b(?P<mods>[^;\n={{}}]*?)\s+(?P<type>[A-Za-z_][\w.<>?,\s\[\]]*)\s+{re.escape(field)}\b",
        re.M,
    )
    candidates = [match for match in declaration_pattern.finditer(original)]
    if not candidates:
        return None, "static_field_declaration_not_visible"
    for match in candidates:
        declaration_prefix = match.group(0)
        if " final " in f" {declaration_prefix} ":
            continue
        type_name = re.sub(r"\s+", "", match.group("type"))
        simple_type = type_name.rsplit(".", 1)[-1]
        if simple_type in {"int", "long", "short", "byte", "double", "float"}:
            return "ASSIGN_ZERO", ""
        if simple_type == "boolean":
            return "ASSIGN_FALSE", ""
        if any(token in simple_type for token in ["List", "Set", "Map", "Collection", "Queue", "Deque"]):
            return "CLEAR_COLLECTION", ""
        if simple_type in {"String", "Object"} or simple_type[:1].isupper():
            return "ASSIGN_NULL", ""
    return None, "no_resettable_static_field_declaration"


def synthesize_static_field_reset_infer(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    raw_fields = action.get("reset_fields") or action.get("fields") or []
    if isinstance(raw_fields, dict):
        raw_fields = [raw_fields]
    if not isinstance(raw_fields, list) or not raw_fields:
        return "", {"ok": False, "error_class": "missing_inferred_reset_fields", "error": ""}
    resets: list[dict[str, str]] = []
    errors: list[str] = []
    for item in raw_fields:
        if not isinstance(item, dict):
            errors.append("reset_field_not_object")
            continue
        receiver = str(item.get("receiver") or "").strip()
        field = str(item.get("field") or "").strip()
        if not IDENTIFIER_EXPR_RE.match(receiver):
            errors.append("unsafe_reset_receiver")
            continue
        if not FIELD_NAME_RE.match(field):
            errors.append("unsafe_reset_field")
            continue
        operation, error = infer_static_reset_operation(original, receiver, field)
        if operation:
            resets.append({"receiver": receiver, "field": field, "operation": operation})
        else:
            errors.append(error)
    if not resets:
        return "", {"ok": False, "error_class": "cannot_infer_static_reset_operation", "error": ",".join(errors)}
    inferred_action = dict(action)
    inferred_action.pop("reset_statements", None)
    inferred_action.pop("statements", None)
    inferred_action["resets"] = resets
    patch, meta = synthesize_static_field_reset(original, sample, inferred_action, rel_path)
    if meta.get("ok"):
        meta.update({"transform": "NIO_STATIC_FIELD_RESET_INFER", "inferred_resets": resets})
    return patch, meta


def synthesize_static_field_reset(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return "", {"ok": False, "error_class": "missing_target_method_bounds", "error": ""}
    if action.get("reset_statements") or action.get("statements"):
        return "", {
            "ok": False,
            "error_class": "raw_java_statement_not_allowed",
            "error": "NIO_STATIC_FIELD_RESET requires typed resets, not reset_statements",
        }
    raw_resets = action.get("resets") or action.get("typed_resets") or []
    if isinstance(raw_resets, dict):
        raw_resets = [raw_resets]
    statements: list[str] = []
    typed_resets: list[dict[str, str]] = []
    reset_errors: list[str] = []
    for item in raw_resets if isinstance(raw_resets, list) else []:
        statement, typed, error = typed_reset_statement(item)
        if statement and typed:
            statements.append(statement)
            typed_resets.append(typed)
        else:
            reset_errors.append(error)
    if not statements:
        return "", {
            "ok": False,
            "error_class": "missing_typed_resets",
            "error": ",".join(error for error in reset_errors if error),
        }
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    signature_index = int(method_start) - 1
    insert_after = None
    for index in range(signature_index, int(method_end)):
        if "{" in lines_plain[index]:
            insert_after = index + 1
            break
    if insert_after is None:
        return "", {"ok": False, "error_class": "missing_method_open_brace", "error": ""}
    existing_method = "\n".join(lines_plain[int(method_start) - 1 : int(method_end)])
    if all(statement in existing_method for statement in statements):
        return "", {"ok": False, "error_class": "duplicate_reset_statements", "error": ""}
    base_indent = re.match(r"^(\s*)", lines_plain[signature_index]).group(1) + "    "
    insertion = "".join(f"{base_indent}{statement}\n" for statement in statements)
    revised = "".join(lines_keepends[:insert_after]) + insertion + "".join(lines_keepends[insert_after:])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "NIO_STATIC_FIELD_RESET",
            "target_file": rel_path,
            "insert_after_line": insert_after,
            "resets": typed_resets,
            "generated_reset_statements": statements,
        }
    )
    return patch, meta


def synthesize_reflection_sort(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return "", {"ok": False, "error_class": "missing_target_method_bounds", "error": ""}
    array_variable = str(action.get("array_variable") or action.get("receiver") or "").strip()
    sort_key = str(action.get("sort_key") or action.get("key") or "").strip().upper()
    sort_specs = {
        "METHOD_NAME": r"\bget(?:Declared|Member)?Methods\s*\(",
        "FIELD_NAME": r"\bget(?:Declared|Member)?Fields\s*\(",
        "CONSTRUCTOR_NAME": r"\bget(?:Declared|Member)?Constructors\s*\(",
    }
    if not FIELD_NAME_RE.match(array_variable):
        return "", {"ok": False, "error_class": "unsafe_reflection_sort_variable", "error": array_variable}
    if sort_key not in sort_specs:
        return "", {"ok": False, "error_class": "unsupported_reflection_sort_key", "error": sort_key}
    call_pattern = sort_specs[sort_key]
    lines_plain = original.splitlines()
    method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
    method_text = "\n".join(method_lines)
    if array_variable not in method_text or not re.search(call_pattern, method_text):
        return "", {"ok": False, "error_class": "inapplicable_reflection_sort", "error": sort_key}

    insert_after_raw = action.get("insert_after_line") or action.get("after_line") or action.get("line")
    try:
        insert_after = int(insert_after_raw)
    except (TypeError, ValueError):
        insert_after = 0
        assignment_pattern = re.compile(rf"\b{re.escape(array_variable)}\b.*{call_pattern}")
        for offset, line in enumerate(method_lines):
            if assignment_pattern.search(line):
                insert_after = int(method_start) + offset
                break
    if not insert_after:
        return "", {"ok": False, "error_class": "missing_reflection_sort_insert_line", "error": array_variable}
    span_error = validate_line_span(insert_after, insert_after, method_start, method_end)
    if span_error:
        return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
    selected_line = lines_plain[insert_after - 1] if 0 <= insert_after - 1 < len(lines_plain) else ""
    if array_variable not in selected_line:
        return "", {"ok": False, "error_class": "reflection_sort_insert_line_not_variable_assignment", "error": selected_line.strip()}

    indexed_use = re.search(rf"\b{re.escape(array_variable)}\s*\[\s*\d+\s*\]", method_text)
    if not indexed_use and not re.search(rf"\b{re.escape(array_variable)}\b.*(?:assert|contains|equals)", method_text):
        return "", {"ok": False, "error_class": "reflection_sort_no_order_sensitive_use", "error": array_variable}

    statement = f"java.util.Arrays.sort({array_variable}, java.util.Comparator.comparing(item -> item.getName()));"
    action = dict(action)
    action["insert_after_line"] = insert_after
    patch, meta = synthesize_insert_statement_after_line(original, sample, action, rel_path, "ID_SORT_REFLECTION_RESULTS", statement)
    if meta.get("ok"):
        meta.update({"array_variable": array_variable, "sort_key": sort_key})
    return patch, meta


def synthesize_insert_statement_after_line(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
    transform: str,
    statement: str,
    allow_duplicate: bool = False,
) -> tuple[str, dict[str, Any]]:
    insert_after_raw = action.get("insert_after_line") or action.get("after_line") or action.get("line")
    try:
        insert_after = int(insert_after_raw)
    except (TypeError, ValueError):
        method_start, _ = method_bounds_for_sample(original, sample)
        insert_after = int(method_start or 0)
    method_start, method_end = method_bounds_for_sample(original, sample)
    span_error = validate_line_span(insert_after, insert_after, method_start, method_end)
    if span_error:
        if transform in {
            "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE",
            "OD_VIC_JOB_REGISTRY_RESET_BEFORE",
            "OD_VIC_RESOURCE_REMOVE_PATH",
            "OD_RESOURCE_REMOVE_PATH",
        }:
            corrected_line = method_open_line(original, sample)
            corrected_error = validate_line_span(corrected_line, corrected_line, method_start, method_end)
            if corrected_line and not corrected_error:
                insert_after = corrected_line
            else:
                return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
        else:
            return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_text = "\n".join(lines_plain[int(method_start or 1) - 1 : int(method_end or 0)])
    if not allow_duplicate and statement.strip() in method_text:
        return "", {"ok": False, "error_class": "duplicate_insert_statement", "error": statement.strip()}
    prev_line = lines_plain[insert_after - 1] if 0 <= insert_after - 1 < len(lines_plain) else ""
    indent = re.match(r"^(\s*)", prev_line).group(1)
    revised = "".join(lines_keepends[:insert_after]) + indent + statement.strip() + "\n" + "".join(lines_keepends[insert_after:])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": transform,
            "target_file": rel_path,
            "insert_after_line": insert_after,
        }
    )
    return patch, meta


def method_open_line(original: str, sample: dict[str, str]) -> int | None:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return None
    lines = original.splitlines()
    for index in range(int(method_start) - 1, int(method_end)):
        if "{" in lines[index]:
            return index + 1
    return None


def synthesize_schema_cleanup_after_assert(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    schema_expr = str(action.get("schema_expr") or action.get("schema_class") or "Schema.class").strip()
    connection_expr = str(action.get("connection_expr") or "connectionSource").strip()
    if not re.match(r"^[A-Za-z_][\w.]*$", schema_expr) or not re.match(r"^[A-Za-z_][\w.]*$", connection_expr):
        return "", {"ok": False, "error_class": "unsafe_schema_cleanup_parameters", "error": ""}
    schema_type = class_name_from_class_literal(schema_expr)
    if not schema_type:
        return "", {"ok": False, "error_class": "schema_expr_not_class_literal", "error": schema_expr}
    if not java_type_visible(original, schema_type):
        return "", {"ok": False, "error_class": "schema_class_not_visible", "error": schema_type}
    if "SchemaUtils." not in original:
        return "", {"ok": False, "error_class": "schema_utils_not_visible", "error": ""}
    statement = f"SchemaUtils.dropSchema({connection_expr}, {schema_expr}, true);"
    insert_after = action.get("insert_after_line")
    if not insert_after:
        lines = original.splitlines()
        method_start, method_end = method_bounds_for_sample(original, sample)
        for line_no in range(int(method_start or 1), int(method_end or 0) + 1):
            if "SchemaUtils.createSchema" in lines[line_no - 1]:
                insert_after = line_no
                break
    action = dict(action)
    action["insert_after_line"] = insert_after
    return synthesize_insert_statement_after_line(
        original,
        sample,
        action,
        rel_path,
        "OD_VIC_SCHEMA_DROP_AFTER",
        statement,
        allow_duplicate=True,
    )


def infer_database_entity_class(original: str, sample: dict[str, str], action: dict[str, Any]) -> tuple[str | None, str]:
    explicit = str(action.get("entity_class") or action.get("schema_class") or action.get("schema_expr") or "").strip()
    explicit_type = class_name_from_class_literal(explicit) if explicit else None
    if explicit_type and java_type_visible(original, explicit_type):
        return explicit_type, "explicit"

    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        start_line, _ = line_span_from_action(action)
        method_start, method_end = enclosing_method_bounds_for_line(original, start_line)
    if not method_start or not method_end:
        return None, "missing_target_method_bounds"
    lines = original.splitlines()
    method_text = "\n".join(lines[int(method_start) - 1 : int(method_end)])
    patterns = [
        r"\bcreateDao\s*\(\s*([A-Za-z_][\w.]*)\.class\s*,\s*true\s*\)",
        r"\bDao\s*<\s*([A-Za-z_][\w.]*)\s*,",
        r"\bRuntimeExceptionDao\s*<\s*([A-Za-z_][\w.]*)\s*,",
        r"\bTableUtils\.(?:createTable|dropTable|clearTable)\s*\([^,]+,\s*([A-Za-z_][\w.]*)\.class",
    ]
    for pattern in patterns:
        match = re.search(pattern, method_text)
        if match:
            candidate = match.group(1).split(".")[-1]
            if java_type_visible(original, candidate):
                return candidate, "method_pattern"
    return None, "database_entity_class_not_visible"


def synthesize_database_drop_table_before(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    if not any(token in original for token in ["createDao(", "Dao<", "RuntimeExceptionDao<", "TableUtils."]):
        return "", {"ok": False, "error_class": "inapplicable_database_table_cleanup", "error": "database dao/table pattern not visible"}
    entity_class, reason = infer_database_entity_class(original, sample, action)
    if not entity_class:
        return "", {"ok": False, "error_class": "missing_database_entity_class", "error": reason}
    connection_expr = str(action.get("connection_expr") or "connectionSource").strip()
    if not IDENTIFIER_EXPR_RE.match(connection_expr):
        return "", {"ok": False, "error_class": "unsafe_connection_expr", "error": connection_expr}
    statement = (
        f"try {{ com.j256.ormlite.table.TableUtils.dropTable({connection_expr}, {entity_class}.class, true); }} "
        f"catch (java.sql.SQLException ignored) {{ }}"
    )
    insert_after = action.get("insert_after_line") or method_open_line(original, sample)
    if not insert_after:
        start_line, _ = line_span_from_action(action)
        method_start, _method_end = enclosing_method_bounds_for_line(original, start_line)
        insert_after = method_start
    scoped_action = dict(action)
    scoped_action["insert_after_line"] = insert_after
    patch, meta = synthesize_insert_statement_after_line(
        original,
        sample,
        scoped_action,
        rel_path,
        "OD_VIC_DATABASE_DROP_TABLE_BEFORE",
        statement,
        allow_duplicate=False,
    )
    if meta.get("ok"):
        meta.update({"entity_class": entity_class, "connection_expr": connection_expr})
    return patch, meta


def method_allows_json_processing_exception(lines: list[str]) -> bool:
    declaration = []
    for line in lines[:8]:
        declaration.append(line)
        if "{" in line:
            break
    text = " ".join(declaration)
    return "throws" in text and ("JsonProcessingException" in text or "Exception" in text)


def json_assert_wrapper_for_method(
    method_lines: list[str],
    selected_lines: list[str],
    sample: dict[str, str],
    original: str,
) -> tuple[str | None, str | None]:
    method_text = "\n".join(method_lines)
    selected_text = "\n".join(selected_lines)
    if "PRETTY_PRINT_GSON" in method_text or "PRETTY_PRINT_GSON" in selected_text:
        return "PRETTY_PRINT_GSON.fromJson({expr}, Object.class)", "ID_JSON_OBJECT_SEMANTIC_ASSERT"
    if "GSON" in method_text or "GSON" in selected_text:
        return "GSON.fromJson({expr}, Object.class)", "ID_JSON_OBJECT_SEMANTIC_ASSERT"
    if project_mentions_object_json_parser(sample, original):
        return "new com.google.gson.Gson().fromJson({expr}, Object.class)", "ID_JSON_OBJECT_SEMANTIC_ASSERT"
    if method_allows_json_processing_exception(method_lines) and "OBJECT_MAPPER" in method_text:
        return "OBJECT_MAPPER.readTree({expr})", "ID_JSON_READTREE_ASSERT"
    if method_allows_json_processing_exception(method_lines) and project_mentions_jackson(sample, original):
        return (
            "new com.fasterxml.jackson.databind.ObjectMapper().readTree({expr})",
            "ID_JSON_READTREE_ASSERT_FQCN_OBJECT_MAPPER",
        )
    return None, None


def patch_from_transform_action(sample: dict[str, str], repair_json: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Synthesize a patch from a restricted repair transform selected by the LLM."""
    action = repair_json.get("transform_action") or repair_json.get("repair_transform") or {}
    if not isinstance(action, dict):
        return "", {"ok": False, "error_class": "missing_transform_action", "error": "transform_action is not an object"}

    transform = canonical_transform_name(action.get("transform") or action.get("name") or "")
    if not transform:
        return "", {"ok": False, "error_class": "missing_transform_name", "error": ""}
    action = dict(action)
    action["transform"] = transform
    allowed_transforms = set(allowed_transforms_for_sample(sample))
    if transform not in allowed_transforms:
        return "", {
            "ok": False,
            "error_class": "category_disallowed_transform",
            "error": f"{transform} not allowed for category {sample.get('PrimaryCategory') or sample.get('Category')}",
            "transform": transform,
            "allowed_transforms": sorted(allowed_transforms),
        }
    if transform == "NO_SAFE_TRANSFORM":
        # NO_SAFE_TRANSFORM is a hard refusal. Do not infer or materialize a
        # fallback patch, otherwise the DSL boundary collapses into implicit
        # free-form repair.
        return "", {"ok": False, "error_class": "no_safe_transform", "error": str(action.get("reason") or "")}

    test_path, target_file, target_error = validate_action_target(sample, action)
    if target_error or not test_path:
        return "", {"ok": False, "error_class": "transform_wrong_target", "error": target_error or ""}

    original = test_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_start, method_end = method_bounds_for_sample(original, sample)
    rel_path = target_file or test_path.name

    explicit_sample_transforms = {
        "ID_JSON_API_METHOD_ASSERTS": lambda: synthesize_json_api_method_json_asserts(original, sample, rel_path),
        "ID_SORT_DECLARED_MEMBERS_BY_NAME": lambda: synthesize_declared_members_sort_by_name(sample),
    }
    if transform in explicit_sample_transforms:
        generated = explicit_sample_transforms[transform]()
        if generated:
            return generated
        return "", {
            "ok": False,
            "error_class": "explicit_transform_guard_failed",
            "error": transform,
            "transform": transform,
        }

    if transform == "OD_DATABASE_FIXTURE_RESET_SETUP":
        if "MybatisHelper.getSqlSession()" not in original:
            return "", {"ok": False, "error_class": "inapplicable_mybatis_setup", "error": "MybatisHelper not visible"}
        setup_method = """    @org.junit.Before
    public void setupDB() {
        SqlSession sqlSession = MybatisHelper.getSqlSession();
        try {
            java.sql.Connection conn = sqlSession.getConnection();
            java.io.Reader reader = org.apache.ibatis.io.Resources.getResourceAsReader("CreateDB.sql");
            org.apache.ibatis.jdbc.ScriptRunner runner = new org.apache.ibatis.jdbc.ScriptRunner(conn);
            runner.setLogWriter(null);
            runner.runScript(reader);
            reader.close();
        } catch (java.io.IOException e) {
        } finally {
            sqlSession.close();
        }
    }"""
        patch, meta = synthesize_class_setup_method(original, setup_method, rel_path, "void setupDB()")
        meta.update({"transform": transform})
        return patch, meta

    if transform == "OD_JSON_GLOBAL_FORMAT_STATE_RESET":
        if "extends TestCase" not in original or "JSON.defaultTimeZone" in original:
            return "", {"ok": False, "error_class": "duplicate_or_inapplicable_json_global_setup", "error": ""}
        timezone = str(action.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai"
        locale_expr = str(action.get("locale_expr") or "java.util.Locale.CHINA").strip() or "java.util.Locale.CHINA"
        if not re.match(r"^[A-Za-z_][\w.]*$", locale_expr):
            return "", {"ok": False, "error_class": "unsafe_locale_expr", "error": locale_expr}
        setup_method = f"""    @Override
    protected void setUp() throws Exception {{
        super.setUp();
        com.alibaba.fastjson.JSON.defaultTimeZone = java.util.TimeZone.getTimeZone({java_string_literal(timezone)});
        com.alibaba.fastjson.JSON.defaultLocale = {locale_expr};
    }}"""
        patch, meta = synthesize_class_setup_method(original, setup_method, rel_path, "JSON.defaultTimeZone")
        meta.update({"transform": transform, "timezone": timezone, "locale_expr": locale_expr})
        return patch, meta

    if transform in {"OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE", "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE_CLASS"}:
        subtype_class = str(action.get("subtype_class") or "TestChecker").strip()
        type_expr = str(action.get("type_expr") or f"{subtype_class}.TYPE").strip()
        factory_expr = str(action.get("factory_expr") or "HealthCheckerFactory").strip()
        if not all(re.match(r"^[A-Za-z_][\w.]*$", item) for item in [subtype_class, type_expr, factory_expr]):
            return "", {"ok": False, "error_class": "unsafe_register_subtype_parameters", "error": ""}
        if subtype_class not in original or factory_expr not in original:
            return "", {"ok": False, "error_class": "inapplicable_register_subtype", "error": ""}
        statement = f"{factory_expr}.registerSubType({subtype_class}.class, {type_expr});"
        if transform == "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE_CLASS":
            setup_method = f"""    @org.junit.BeforeClass
    public static void beforeClass() {{
        {statement}
    }}"""
            patch, meta = synthesize_class_setup_method(original, setup_method, rel_path, "static void beforeClass()")
            meta.update({"transform": transform, "subtype_class": subtype_class, "type_expr": type_expr})
            return patch, meta
        action = dict(action)
        action["insert_after_line"] = action.get("insert_after_line") or method_open_line(original, sample)
        return synthesize_insert_statement_after_line(original, sample, action, rel_path, transform, statement)

    if transform == "OD_VIC_JOB_REGISTRY_RESET_BEFORE":
        job_name = str(action.get("job_name") or action.get("value") or "").strip()
        if not job_name:
            method_text = "\n".join(lines_plain[int(method_start or 1) - 1 : int(method_end or 0)])
            match = re.search(r'"([^"]*job[^"]*)"', method_text)
            job_name = match.group(1) if match else "test_job"
        if not re.match(r"^[A-Za-z0-9_.:@/-]+$", job_name):
            return "", {"ok": False, "error_class": "unsafe_job_name", "error": job_name}
        statement = f"JobRegistry.getInstance().shutdown({java_string_literal(job_name)});"
        action = dict(action)
        action["insert_after_line"] = action.get("insert_after_line") or method_open_line(original, sample)
        return synthesize_insert_statement_after_line(original, sample, action, rel_path, transform, statement)

    if transform in {"OD_VIC_DATABASE_TABLE_CLEANUP", "OD_VIC_DATABASE_DROP_TABLE_BEFORE", "OD_VIC_DATABASE_SCHEMA_CLEANUP"}:
        patch, meta = synthesize_database_drop_table_before(original, sample, action, rel_path)
        if meta.get("ok"):
            meta["transform"] = "OD_VIC_DATABASE_TABLE_CLEANUP"
        return patch, meta

    if transform == "OD_RESTORE_ENV_AFTER_MUTATION":
        return synthesize_env_restore_after_mutation(original, sample, action, rel_path)

    if transform == "OD_VIC_SCHEMA_DROP_AFTER":
        patch, meta = synthesize_schema_cleanup_after_assert(original, sample, action, rel_path)
        if patch.strip() or meta.get("ok"):
            return patch, meta
        return patch, meta

    if transform == "NIO_STATIC_FIELD_RESET":
        patch, meta = synthesize_static_field_reset(original, sample, action, rel_path)
        return patch, meta

    if transform == "NIO_STATIC_FIELD_RESET_INFER":
        patch, meta = synthesize_static_field_reset_infer(original, sample, action, rel_path)
        return patch, meta

    if transform == "ID_SORT_REFLECTION_RESULTS":
        patch, meta = synthesize_reflection_sort(original, sample, action, rel_path)
        return patch, meta

    if transform in {"OD_RESOURCE_REMOVE_PATH", "OD_VIC_RESOURCE_REMOVE_PATH"}:
        receiver = str(action.get("receiver") or "zkRegCenter").strip()
        path_value = str(action.get("path") or action.get("resource_path") or "").strip()
        if not re.match(r"^[A-Za-z_][\w.]*$", receiver) or not path_value.startswith("/"):
            return "", {"ok": False, "error_class": "missing_resource_remove_parameters", "error": ""}
        action = dict(action)
        insert_after_raw = action.get("insert_after_line") or method_open_line(original, sample)
        try:
            insert_after = int(insert_after_raw)
        except (TypeError, ValueError):
            insert_after = method_open_line(original, sample)
        span_error = validate_line_span(insert_after, insert_after, method_start, method_end)
        if span_error:
            corrected_line = method_open_line(original, sample)
            corrected_error = validate_line_span(corrected_line, corrected_line, method_start, method_end)
            if corrected_line and not corrected_error:
                insert_after = corrected_line
            else:
                return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
        visible, visible_reason = receiver_visible_before_line(original, sample, receiver, insert_after)
        if not visible:
            return "", {"ok": False, "error_class": "resource_receiver_not_visible", "error": visible_reason}
        applicable, applicable_reason = resource_receiver_applicable(original, sample, receiver)
        if not applicable:
            return "", {"ok": False, "error_class": "inapplicable_resource_remove_receiver", "error": applicable_reason}
        statement = f"{receiver}.remove({java_string_literal(path_value)});"
        action["insert_after_line"] = insert_after
        patch, meta = synthesize_insert_statement_after_line(original, sample, action, rel_path, transform, statement)
        return patch, meta

    if transform == "ID_JSON_MISSING_TYPE_SETTER":
        insert_after_raw = action.get("insert_after_line") or action.get("after_line") or action.get("line")
        try:
            insert_after = int(insert_after_raw)
        except (TypeError, ValueError):
            return "", {"ok": False, "error_class": "missing_insert_after_line", "error": str(insert_after_raw)}
        span_error = validate_line_span(insert_after, insert_after, method_start, method_end)
        if span_error:
            return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
        receiver = str(action.get("receiver") or action.get("receiver_expr") or "").strip()
        type_value = str(action.get("type_value") or action.get("value") or "").strip()
        setter_code = str(action.get("setter_code") or "").strip()
        if setter_code:
            return "", {
                "ok": False,
                "error_class": "raw_java_statement_not_allowed",
                "error": "ID_JSON_MISSING_TYPE_SETTER requires receiver/type_value, not setter_code",
            }
        if receiver and type_value:
            if not IDENTIFIER_EXPR_RE.match(receiver):
                return "", {"ok": False, "error_class": "unsafe_type_setter_receiver", "error": receiver}
            insertion = f"{receiver}.setType({java_string_literal(type_value)});"
        else:
            return "", {"ok": False, "error_class": "missing_type_setter_parameters", "error": ""}
        prev_line = lines_plain[insert_after - 1] if 0 <= insert_after - 1 < len(lines_plain) else ""
        indent = re.match(r"^(\s*)", prev_line).group(1)
        new_line = insertion if insertion.startswith(indent) else indent + insertion
        if any(new_line.strip() == line.strip() for line in lines_plain[int(method_start or 1) - 1 : int(method_end or 0)]):
            return "", {"ok": False, "error_class": "duplicate_type_setter", "error": new_line.strip()}
        revised = "".join(lines_keepends[:insert_after]) + new_line + "\n" + "".join(lines_keepends[insert_after:])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": transform,
                "target_file": rel_path,
                "insert_after_line": insert_after,
            }
        )
        return patch, meta

    span_start, span_end = line_span_from_action(action)
    if (not method_start or not method_end) and span_start:
        inferred_start, inferred_end = enclosing_method_bounds_for_line(original, span_start)
        if inferred_start and inferred_end:
            method_start, method_end = inferred_start, inferred_end
    span_error = validate_line_span(span_start, span_end, method_start, method_end)
    if span_error:
        return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}
    selected_lines = lines_plain[int(span_start) - 1 : int(span_end)]
    old_block = "".join(lines_keepends[int(span_start) - 1 : int(span_end)])

    if transform == "ID_LIST_ORDER_INSENSITIVE":
        new_lines, meta = synthesize_list_order_insensitive(selected_lines)
    elif transform == "ID_ASSERTJ_LIST_ORDER_INSENSITIVE":
        new_lines, meta = synthesize_assertj_order_insensitive(selected_lines)
    elif transform == "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT":
        new_lines, meta = synthesize_query_string_order_insensitive(selected_lines)
    elif transform == "ID_JSON_READTREE_ASSERT":
        method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
        selected_text = "\n".join(selected_lines)
        if split_groovy_assert_that_json_arguments(selected_text):
            new_lines, meta = synthesize_json_semantic_assert(selected_lines, "", "ID_JSON_READTREE_ASSERT")
        else:
            wrapper_template, effective_transform = json_assert_wrapper_for_method(method_lines, selected_lines, sample, original)
            if not wrapper_template or not effective_transform:
                json_api_template, _json_api_error = json_api_parse_template(original, sample)
                if json_api_template:
                    new_lines, meta = synthesize_json_api_parse_assert(selected_lines, json_api_template)
                elif project_mentions_jackson(sample, original):
                    new_lines, meta = synthesize_json_semantic_assert_try_catch(
                        selected_lines,
                        "new com.fasterxml.jackson.databind.ObjectMapper().readTree({expr})",
                        "ID_JSON_READTREE_ASSERT_TRY_CATCH",
                    )
                else:
                    return "", {"ok": False, "error_class": "json_wrapper_unavailable", "error": ""}
            else:
                new_lines, meta = synthesize_json_semantic_assert(selected_lines, wrapper_template, effective_transform)
    elif transform == "ID_JSON_READTREE_ASSERT_TRY_CATCH":
        if not project_mentions_jackson(sample, original):
            return "", {"ok": False, "error_class": "jackson_not_visible", "error": ""}
        new_lines, meta = synthesize_json_semantic_assert_try_catch(
            selected_lines,
            "new com.fasterxml.jackson.databind.ObjectMapper().readTree({expr})",
            "ID_JSON_READTREE_ASSERT_TRY_CATCH",
        )
    elif transform == "ID_JSON_API_PARSE_ASSERT":
        wrapper_template, wrapper_error = json_api_parse_template(original, sample)
        if not wrapper_template:
            return "", {"ok": False, "error_class": wrapper_error or "json_api_parser_not_visible", "error": ""}
        new_lines, meta = synthesize_json_api_parse_assert(selected_lines, wrapper_template)
    else:
        return "", {"ok": False, "error_class": "unsupported_transform", "error": transform}
    if not meta.get("ok"):
        return "", meta

    new_block = "\n".join(new_lines)
    if old_block.endswith("\n"):
        new_block += "\n"
    revised = "".join(lines_keepends[: int(span_start) - 1]) + new_block + "".join(lines_keepends[int(span_end) :])
    patch, diff_meta = unified_diff_for_revised(original, revised, rel_path)
    diff_meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": transform,
            "target_file": rel_path,
            "edit_start_line": span_start,
            "edit_end_line": span_end,
            **meta,
        }
    )
    return patch, diff_meta


def patch_from_edit_action(sample: dict[str, str], repair_json: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Convert an exact target-method search/replace action into a unified diff."""
    action = repair_json.get("edit_action") or repair_json.get("repair_action") or {}
    if not isinstance(action, dict):
        return "", {"ok": False, "error_class": "missing_edit_action", "error": "edit_action is not an object"}

    target_file = target_test_relative_path(sample)
    action_file = str(action.get("target_file") or action.get("file") or "").strip()
    if target_file and action_file and action_file != target_file:
        return (
            "",
            {
                "ok": False,
                "error_class": "edit_action_wrong_file",
                "error": f"edit_action target_file={action_file!r} expected={target_file!r}",
            },
        )

    test_path = find_test_file(sample)
    if not test_path or not test_path.exists():
        return "", {"ok": False, "error_class": "missing_target_test_file", "error": str(test_path or "")}

    new_code = normalize_edit_block(action.get("new_code") or action.get("after"))
    if not new_code:
        return "", {"ok": False, "error_class": "empty_new_code", "error": "edit_action.new_code is empty"}

    original = test_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = original.splitlines(keepends=True)
    method_info = extract_java_method_info(original, sample_test_method(sample), context_lines=0)
    start_line = method_info.get("start_line")
    end_line = method_info.get("end_line")
    if not start_line or not end_line:
        return "", {"ok": False, "error_class": "missing_target_method", "error": sample_test_method(sample)}

    span_start, span_end = edit_action_line_span(action)
    if span_start and span_end:
        if span_start < int(start_line) or span_end > int(end_line) or span_start > span_end:
            return (
                "",
                {
                    "ok": False,
                    "error_class": "line_span_outside_target_method",
                    "error": f"{span_start}-{span_end}",
                    "target_start_line": start_line,
                    "target_end_line": end_line,
                },
            )
        old_block = "".join(lines[span_start - 1 : span_end])
        new_block = new_code
        if old_block.endswith("\n") and not new_block.endswith("\n"):
            new_block += "\n"
        revised = "".join(lines[: span_start - 1]) + new_block + "".join(lines[span_end:])
        rel_path = target_file or test_path.name
        diff_lines = list(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                revised.splitlines(keepends=True),
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                n=3,
            )
        )
        if not diff_lines:
            return "", {"ok": False, "error_class": "empty_generated_diff", "error": ""}
        patch = f"diff --git a/{rel_path} b/{rel_path}\n" + "".join(diff_lines)
        return (
            patch,
            {
                "ok": True,
                "mode": "edit_action_line_span_to_unified_diff",
                "target_file": rel_path,
                "target_start_line": start_line,
                "target_end_line": end_line,
                "edit_start_line": span_start,
                "edit_end_line": span_end,
                "new_code_chars": len(new_code),
            },
        )

    old_code = normalize_edit_block(action.get("old_code") or action.get("before"))
    if not old_code:
        return "", {"ok": False, "error_class": "missing_edit_anchor", "error": "provide start_line/end_line or old_code"}
    if old_code == new_code:
        return "", {"ok": False, "error_class": "no_effect_edit_action", "error": "old_code equals new_code"}

    method_text = "".join(lines[int(start_line) - 1 : int(end_line)])
    if old_code not in method_text:
        return (
            "",
            {
                "ok": False,
                "error_class": "old_code_not_in_target_method",
                "error": old_code[:1000],
                "target_start_line": start_line,
                "target_end_line": end_line,
            },
        )
    if method_text.count(old_code) > 1:
        return (
            "",
            {
                "ok": False,
                "error_class": "ambiguous_old_code",
                "error": "old_code appears multiple times in target method",
                "target_start_line": start_line,
                "target_end_line": end_line,
            },
        )

    new_method_text = method_text.replace(old_code, new_code, 1)
    revised = "".join(lines[: int(start_line) - 1]) + new_method_text + "".join(lines[int(end_line) :])
    rel_path = target_file or test_path.name
    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            revised.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            n=3,
        )
    )
    if not diff_lines:
        return "", {"ok": False, "error_class": "empty_generated_diff", "error": ""}
    patch = f"diff --git a/{rel_path} b/{rel_path}\n" + "".join(diff_lines)
    return (
        patch,
        {
            "ok": True,
            "mode": "edit_action_to_unified_diff",
            "target_file": rel_path,
            "target_start_line": start_line,
            "target_end_line": end_line,
            "old_code_chars": len(old_code),
            "new_code_chars": len(new_code),
        },
    )


def normalize_patch_for_git_apply(sample: dict[str, str], patch: str) -> tuple[str, dict[str, Any]]:
    """Make common LLM diff formats acceptable to git apply without changing semantics."""
    original = patch.strip("\n")
    if not original.strip():
        return patch, {"normalized": False, "reason": "empty_patch"}
    if original.startswith("diff --git ") or "\n--- " in original[:500]:
        return patch, {"normalized": False, "reason": "already_unified_diff"}
    if not original.lstrip().startswith("@@"):
        return patch, {"normalized": False, "reason": "unsupported_patch_format"}

    rel_path = target_test_relative_path(sample)
    if not rel_path:
        return patch, {"normalized": False, "reason": "missing_target_test_path"}
    normalized = "\n".join(
        [
            f"diff --git a/{rel_path} b/{rel_path}",
            f"--- a/{rel_path}",
            f"+++ b/{rel_path}",
            original,
            "",
        ]
    )
    return normalized, {
        "normalized": True,
        "reason": "added_unified_diff_file_header_to_headerless_hunk",
        "target_file": rel_path,
    }


def copy_worktree(sample: dict[str, str], destination: Path) -> tuple[bool, str]:
    source = Path(sample.get("remote_repo_dir", ""))
    if not source.exists():
        return False, f"missing remote_repo_dir: {source}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    return True, str(destination)


def apply_patch(repo_dir: Path, patch: str, patch_path: Path) -> tuple[bool, str]:
    if not patch.strip():
        return False, "empty_patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch, encoding="utf-8")
    attempts = [
        ["git", "apply", str(patch_path.resolve())],
        ["git", "apply", "--recount", str(patch_path.resolve())],
        ["git", "apply", "--recount", "--ignore-whitespace", str(patch_path.resolve())],
    ]
    outputs: list[str] = []
    for cmd in attempts:
        code, output, _timed_out, _elapsed = run_command(cmd, cwd=repo_dir, timeout=120)
        outputs.append("$ " + " ".join(cmd) + "\n" + output)
        if code == 0:
            return True, "\n\n".join(outputs)

    dry_run = ["patch", "-p1", "--fuzz=3", "--batch", "--dry-run", "-i", str(patch_path.resolve())]
    code, output, _timed_out, _elapsed = run_command(dry_run, cwd=repo_dir, timeout=120)
    outputs.append("$ " + " ".join(dry_run) + "\n" + output)
    if code == 0:
        apply_cmd = ["patch", "-p1", "--fuzz=3", "--batch", "-i", str(patch_path.resolve())]
        code, output, _timed_out, _elapsed = run_command(apply_cmd, cwd=repo_dir, timeout=120)
        outputs.append("$ " + " ".join(apply_cmd) + "\n" + output)
        if code == 0:
            return True, "\n\n".join(outputs)
    return False, "\n\n".join(outputs)


def run_maven_test(sample: dict[str, str], repo_dir: Path, mvn: Path, timeout: int) -> dict[str, Any]:
    module = sample.get("validated_module_path") or sample.get("Module Path", ".") or "."
    module_dir = repo_dir if module == "." else repo_dir / module
    selector = sample.get("validated_maven_test_selector") or sample.get("maven_test_selector") or test_selector(sample[TEST_FIELD])
    cmd = [
        str(mvn),
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
    code, output, timed_out, elapsed = run_command(cmd, cwd=module_dir, timeout=timeout)
    return {
        "ok": code == 0,
        "returncode": code,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 2),
        "command": " ".join(cmd),
        "output": output,
    }


def validate_patch(
    sample: dict[str, str],
    patch: str,
    run_dir: Path,
    model_alias: str,
    method: str,
    mvn: Path,
    reruns: int,
    skip_validation: bool,
    cleanup: bool,
    trusted_transform: str | None = None,
) -> dict[str, Any]:
    patch_for_validation, patch_normalization = normalize_patch_for_git_apply(sample, patch)
    unsafe = scan_patch(patch_for_validation)
    applicability = scan_patch_applicability(sample, patch_for_validation, trusted_transform=trusted_transform)
    combined_findings = list(unsafe["findings"]) + list(applicability["findings"])
    combined_unsafe = bool(unsafe["unsafe"] or applicability["blocked"])
    if skip_validation:
        return {
            "patch_normalization": patch_normalization,
            "patch_applicability_findings": applicability["findings"],
            "unsafe_patch": combined_unsafe,
            "unsafe_findings": combined_findings,
            "compile_passed": False,
            "target_single_run_passed": False,
            "target_single_run_outcome": "SKIPPED",
            "post_fix_rerun_budget": reruns,
            "post_fix_runs": 0,
            "post_fix_failures": 0,
            "post_fix_outcomes": [],
            "post_fix_outcomes_consistent": False,
            "post_fix_consistent_pass": False,
            "decision": "validation_skipped",
            "error_class": "validation_skipped",
        }

    sample_id = sample["sample_id"]
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", model_alias)
    worktree = run_dir / "worktrees" / safe_model / method / sample_id / "repo"
    patch_path = run_dir / "patches" / safe_model / method / f"{sample_id}.patch"
    log_dir = run_dir / "validation_logs" / safe_model / method / sample_id
    log_dir.mkdir(parents=True, exist_ok=True)

    if applicability["blocked"]:
        (log_dir / "patch_applicability_gate.json").write_text(
            json.dumps(applicability, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if cleanup:
            shutil.rmtree(worktree.parent, ignore_errors=True)
        return {
            "patch_normalization": patch_normalization,
            "patch_applicability_findings": applicability["findings"],
            "unsafe_patch": True,
            "unsafe_findings": combined_findings,
            "compile_passed": False,
            "target_single_run_passed": False,
            "target_single_run_outcome": "BLOCKED",
            "post_fix_rerun_budget": reruns,
            "post_fix_runs": 0,
            "post_fix_failures": 0,
            "post_fix_outcomes": [],
            "post_fix_outcomes_consistent": False,
            "post_fix_consistent_pass": False,
            "decision": "unsafe_patch",
            "error_class": "patch_applicability_blocked",
            "error": json.dumps(applicability["findings"], ensure_ascii=False)[:2000],
        }

    copied, message = copy_worktree(sample, worktree)
    if not copied:
        return {
            "patch_normalization": patch_normalization,
            "patch_applicability_findings": applicability["findings"],
            "unsafe_patch": combined_unsafe,
            "unsafe_findings": combined_findings,
            "compile_passed": False,
            "target_single_run_passed": False,
            "target_single_run_outcome": "ERROR",
            "post_fix_rerun_budget": reruns,
            "post_fix_runs": 0,
            "post_fix_failures": 0,
            "post_fix_outcomes": [],
            "post_fix_outcomes_consistent": False,
            "post_fix_consistent_pass": False,
            "decision": "build_failed",
            "error_class": "worktree_copy_failed",
            "error": message,
        }

    applied, apply_output = apply_patch(worktree, patch_for_validation, patch_path)
    (log_dir / "git_apply.log").write_text(apply_output, encoding="utf-8", errors="replace")
    if not applied:
        if cleanup:
            shutil.rmtree(worktree.parent, ignore_errors=True)
        return {
            "patch_normalization": patch_normalization,
            "patch_applicability_findings": applicability["findings"],
            "unsafe_patch": combined_unsafe,
            "unsafe_findings": combined_findings,
            "compile_passed": False,
            "target_single_run_passed": False,
            "target_single_run_outcome": "ERROR",
            "post_fix_rerun_budget": reruns,
            "post_fix_runs": 0,
            "post_fix_failures": 0,
            "post_fix_outcomes": [],
            "post_fix_outcomes_consistent": False,
            "post_fix_consistent_pass": False,
            "decision": "build_failed",
            "error_class": "patch_apply_failed",
            "error": apply_output[-2000:],
        }

    first = run_maven_test(sample, worktree, mvn, timeout=300)
    (log_dir / "target_single_run.log").write_text(first["output"], encoding="utf-8", errors="replace")
    post_failures = 0
    post_runs = 0
    post_fix_outcomes: list[str] = []
    if first["ok"] and not combined_unsafe:
        for index in range(1, reruns + 1):
            result = run_maven_test(sample, worktree, mvn, timeout=300)
            post_runs += 1
            post_fix_outcomes.append("PASS" if result["ok"] else "FAIL")
            (log_dir / f"rerun_{index:02d}.log").write_text(result["output"], encoding="utf-8", errors="replace")
            if not result["ok"]:
                post_failures += 1
    if cleanup:
        shutil.rmtree(worktree.parent, ignore_errors=True)

    post_fix_outcomes_consistent = bool(first["ok"] and post_runs == reruns and post_failures == 0)
    post_fix_consistent_pass = bool(post_fix_outcomes_consistent and not combined_unsafe)

    if combined_unsafe:
        decision = "unsafe_patch"
        error_class = "unsafe_patch"
    elif not first["ok"]:
        decision = "test_failed"
        error_class = "target_single_run_failed"
    elif post_failures:
        decision = "plausible_but_unstable"
        error_class = "post_fix_rerun_failed"
    elif post_fix_consistent_pass:
        decision = "repaired"
        error_class = ""
    else:
        decision = "plausible_but_unvalidated"
        error_class = "post_fix_rerun_incomplete"
    return {
        "patch_normalization": patch_normalization,
        "patch_applicability_findings": applicability["findings"],
        "unsafe_patch": combined_unsafe,
        "unsafe_findings": combined_findings,
        "compile_passed": bool(first["ok"]),
        "target_single_run_passed": bool(first["ok"]),
        "target_single_run_outcome": "PASS" if first["ok"] else "FAIL",
        "post_fix_rerun_budget": reruns,
        "post_fix_runs": post_runs,
        "post_fix_failures": post_failures,
        "post_fix_outcomes": post_fix_outcomes,
        "post_fix_outcomes_consistent": post_fix_outcomes_consistent,
        "post_fix_consistent_pass": post_fix_consistent_pass,
        "decision": decision,
        "error_class": error_class,
        "validation_log_dir": str(log_dir),
        "target_single_run_elapsed_seconds": first["elapsed_seconds"],
    }
