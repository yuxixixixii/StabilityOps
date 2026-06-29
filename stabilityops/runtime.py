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
    "ID_STABLE_COLLECTION_CONSTRUCTION",
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


def applicable_transform_hints(sample: dict[str, str]) -> list[dict[str, str]]:
    """Expose syntax-backed operator candidates without granting edit authority."""
    category = str(sample.get("PrimaryCategory") or sample.get("Category") or "").strip().upper()
    if category != "ID":
        return [{"transform": name, "evidence": "category-allowed operator"} for name in allowed_transforms_for_sample(sample)]
    test_path = find_test_file(sample)
    if not test_path or not test_path.exists():
        return []
    original = test_path.read_text(encoding="utf-8", errors="replace")
    method_info = extract_java_method_info(original, sample_test_method(sample), context_lines=0)
    target_method = str(method_info.get("code") or "")
    scan_text = target_method or original
    scan_scope = "target method" if target_method else "target file"
    target_mentions_reflection = bool(
        re.search(r"getDeclared(?:Fields|Methods|Constructors)|reflectionToString|reflectionHashCode|ReflectionToString", scan_text)
    )
    target_has_unordered_construction = bool(
        re.search(r"new\s+(?:java\.util\.)?(?:HashMap|HashSet)\b|\bImmutableMap\.of\s*\(", scan_text)
    )
    target_has_iterator_membership = bool(
        re.search(r"\.iterator\s*\(\s*\)\s*\.next\s*\(\s*\)", scan_text)
        and re.search(r"\bassertEquals\s*\(", scan_text)
    )
    target_has_helper_map_iteration = False
    for helper_match in re.finditer(
        r"\b(?:final\s+)?(?:java\.util\.)?(?:Map|HashMap)\s*(?:<[^>]+>)?\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<helper>[A-Za-z_]\w*)\s*\(\s*\)\s*;",
        scan_text,
    ):
        var_name = helper_match.group("var")
        if re.search(rf"\b{re.escape(var_name)}\b\.(?:entrySet|values|keySet)\s*\(", scan_text):
            target_has_helper_map_iteration = True
            break
    source_context = (
        "\n".join(str(item.get("content") or "") for item in extract_source_focus_snippets(sample, category)[:4])
        if target_mentions_reflection
        else ""
    )
    hints: list[dict[str, str]] = []

    def add(transform: str, evidence: str, confidence: str = "medium") -> None:
        hints.append(
            {
                "transform": transform,
                "evidence": evidence,
                "scope": scan_scope,
                "confidence": confidence,
            }
        )

    if re.search(
        r"\b(?:List|Set|Map|Collection|HashMap|HashSet)\b|containsExactly|assertArrayEquals|"
        r"\.get\s*\(\s*\d+|Arrays\.asList|List\.of|ImmutableList\.of|Lists\.newArrayList|CollUtil\.newArrayList|"
        r"\[[^\]]*(?:\+\+|--)?\s*\w+\s*\]|\[\s*\d+\s*\]",
        scan_text,
    ):
        add("ID_LIST_ORDER_INSENSITIVE", f"collection or indexed assertion syntax is visible in {scan_scope}", "high")
    if target_has_iterator_membership:
        add(
            "ID_LIST_ORDER_INSENSITIVE",
            f"iterator().next() feeds an equality assertion in {scan_scope}; membership is the stable invariant",
            "high",
        )
    if "assertThat" in scan_text and re.search(r"containsExactly|contains\s*\(|isEqualTo|containsExactlyInAnyOrder", scan_text):
        add("ID_ASSERTJ_LIST_ORDER_INSENSITIVE", f"AssertJ/Hamcrest collection assertion syntax is visible in {scan_scope}", "high")
    if re.search(r"[?&][A-Za-z0-9_.%-]+=|query|Query|params\s*\(", scan_text):
        add("ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT", f"query-parameter construction or assertion syntax is visible in {scan_scope}", "medium")
    if re.search(r"JSON|Json|json|ObjectMapper|Gson|JSONObject|JSONArray|writeValueAsString|toJson|parseObject", scan_text):
        if len(re.findall(r"\b(?:assertEquals|assertThat)\s*\(", scan_text)) >= 2:
            hints.append(
                {
                    "transform": "ID_JSON_API_METHOD_ASSERTS",
                    "evidence": f"multiple JSON-related assertions are visible in {scan_scope}",
                    "scope": scan_scope,
                    "confidence": "high",
                }
            )
        hints.extend(
            [
                {
                    "transform": "ID_JSON_READTREE_ASSERT",
                    "evidence": f"structured JSON assertion or serialization syntax is visible in {scan_scope}",
                    "scope": scan_scope,
                    "confidence": "high",
                },
                {
                    "transform": "ID_JSON_API_PARSE_ASSERT",
                    "evidence": f"structured JSON assertion or serialization syntax is visible in {scan_scope}",
                    "scope": scan_scope,
                    "confidence": "medium",
                },
            ]
        )
    reflection_text = "\n".join([scan_text, source_context])
    if re.search(r"getDeclared(?:Fields|Methods|Constructors)|reflectionToString|reflectionHashCode|ReflectionToString", reflection_text):
        hints.append(
            {
                "transform": "ID_SORT_DECLARED_MEMBERS_BY_NAME",
                "evidence": "declared-member reflection order is consumed by visible target method or source context",
                "scope": "target method/source context",
                "confidence": "high",
            }
        )
    if re.search(r"\b[A-Za-z_]\w*\s*=\s*.*getDeclared(?:Fields|Methods|Constructors)\s*\(", scan_text):
        add("ID_SORT_REFLECTION_RESULTS", f"direct test-local reflection-result array syntax is visible in {scan_scope}", "high")
    if target_has_unordered_construction:
        add("ID_STABLE_COLLECTION_CONSTRUCTION", f"unordered collection construction is visible in {scan_scope}", "high")
    if target_has_helper_map_iteration:
        add(
            "ID_STABLE_COLLECTION_CONSTRUCTION",
            f"target method iterates a map returned by a test fixture helper in {scan_scope}",
            "high",
        )
    hints.append(
        {
            "transform": "NO_SAFE_TRANSFORM",
            "evidence": "required when no syntax-backed candidate satisfies its guard",
            "scope": scan_scope,
            "confidence": "fallback",
        }
    )
    priority = {
        "ID_SORT_DECLARED_MEMBERS_BY_NAME": 0 if target_mentions_reflection else 6,
        "ID_SORT_REFLECTION_RESULTS": 1,
        "ID_STABLE_COLLECTION_CONSTRUCTION": 1 if (target_has_unordered_construction or target_has_helper_map_iteration) else 7,
        "ID_JSON_API_METHOD_ASSERTS": 2,
        "ID_JSON_READTREE_ASSERT": 3,
        "ID_JSON_API_PARSE_ASSERT": 4,
        "ID_LIST_ORDER_INSENSITIVE": 1 if target_has_iterator_membership else 4,
        "ID_ASSERTJ_LIST_ORDER_INSENSITIVE": 5,
        "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT": 6,
        "NO_SAFE_TRANSFORM": 99,
    }
    ordered_hints = sorted(enumerate(hints), key=lambda item: (priority.get(item[1]["transform"], 50), item[0]))
    seen: set[str] = set()
    return [item for _, item in ordered_hints if not (item["transform"] in seen or seen.add(item["transform"]))]


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


def mask_java_non_code(test_code: str) -> str:
    """Mask comments and literals while preserving source offsets and newlines."""
    masked: list[str] = []
    state = "code"
    escaped = False
    index = 0
    while index < len(test_code):
        char = test_code[index]
        next_char = test_code[index + 1] if index + 1 < len(test_code) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                masked.extend((" ", " "))
                state = "line_comment"
                index += 2
                continue
            if char == "/" and next_char == "*":
                masked.extend((" ", " "))
                state = "block_comment"
                index += 2
                continue
            if char == '"':
                masked.append(" ")
                state = "string"
                escaped = False
            elif char == "'":
                masked.append(" ")
                state = "char"
                escaped = False
            else:
                masked.append(char)
        elif state == "line_comment":
            if char == "\n":
                masked.append("\n")
                state = "code"
            else:
                masked.append(" ")
        elif state == "block_comment":
            if char == "*" and next_char == "/":
                masked.extend((" ", " "))
                state = "code"
                index += 2
                continue
            masked.append("\n" if char == "\n" else " ")
        else:
            masked.append("\n" if char == "\n" else " ")
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif (state == "string" and char == '"') or (state == "char" and char == "'"):
                state = "code"
        index += 1
    return "".join(masked)


def extract_java_method_info(test_code: str, method_name: str, context_lines: int = 8) -> dict[str, Any]:
    if not test_code or not method_name:
        return {"code": "", "start_line": None, "end_line": None, "numbered_code": ""}
    lines = test_code.splitlines()
    masked_code = mask_java_non_code(test_code)
    declaration = re.compile(
        rf"(?m)^[ \t]*(?:(?:public|protected|private|static|final|synchronized|abstract|native|strictfp|default)\s+)*"
        rf"(?:<[^;{{}}]+>\s+)?[A-Za-z_$][\w$\.\[\]<>?,]*\s+{re.escape(method_name)}\s*"
        rf"\([^;{{}}]*\)\s*(?:throws\s+[^{{}}]+)?\{{"
    )
    match = declaration.search(masked_code)
    if not match:
        return {"code": "", "start_line": None, "end_line": None, "numbered_code": ""}
    method_start = masked_code.count("\n", 0, match.start())

    brace_depth = 0
    seen_open = False
    method_end = min(len(lines), method_start + 80)
    masked_lines = masked_code.splitlines()
    for index in range(method_start, len(lines)):
        scan_line = masked_lines[index] if index < len(masked_lines) else ""
        brace_depth += scan_line.count("{")
        if "{" in scan_line:
            seen_open = True
        brace_depth -= scan_line.count("}")
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
    "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE",
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
            "replacement": lambda original, indent, sink, receiver: (
                f"{indent}java.lang.reflect.Method[] declaredMethods = {receiver}.getDeclaredMethods();\n"
                f"{indent}java.util.Arrays.sort(declaredMethods, {reflection_comparator(original, 'METHOD')});\n"
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
            "replacement": lambda original, indent, prefix, var, receiver: (
                f"{indent}{prefix}{var} = {receiver}.getDeclaredFields();\n"
                f"{indent}java.util.Arrays.sort({var}, {reflection_comparator(original, 'FIELD')});"
            ),
        },
        {
            "member": "CONSTRUCTOR",
            "glob": "*.java",
            "assign": re.compile(
                r"^(?P<indent>\s*)(?P<prefix>(?:final\s+)?(?:java\.lang\.reflect\.)?Constructor(?:<[^>]+>)?\[\]\s+)(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<receiver>[A-Za-z_][\w.]*)\.getDeclaredConstructors\(\);\s*$",
                re.M,
            ),
            "replacement": lambda original, indent, prefix, var, receiver: (
                f"{indent}{prefix}{var} = {receiver}.getDeclaredConstructors();\n"
                f"{indent}java.util.Arrays.sort({var}, {reflection_comparator(original, 'CONSTRUCTOR')});"
            ),
        },
    ]

    search_roots: list[Path] = []
    test_path = find_test_file(sample)
    target_method_text = ""
    if test_path and test_path.exists():
        search_roots.append(test_path)
        test_original = test_path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        method_start, method_end = method_bounds_for_sample(test_original, sample)
        if method_start and method_end:
            target_method_text = "\n".join(test_original.splitlines()[int(method_start) - 1 : int(method_end)])
    for root_name in ["src/main/java", "src/test/java"]:
        root = repo_dir / root_name
        if root.exists():
            search_roots.extend(root.rglob("*.java"))
    if len(search_roots) <= 1:
        # Multi-module projects often keep sources under module/src/main/java.
        # Scan the repository, but keep the guard tied to declared-member APIs
        # and deterministic local rewrites rather than repository identity.
        search_roots.extend(repo_dir.rglob("*.java"))

    priority_classes: list[str] = []
    if "HashCodeBuilder.reflectionHashCode" in target_method_text or "new HashCodeBuilder" in target_method_text:
        priority_classes.append("HashCodeBuilder")
    if (
        "ToStringBuilder.reflectionToString" in target_method_text
        or "ReflectionToStringBuilder" in target_method_text
        or "reflectionToString(" in target_method_text
    ):
        priority_classes.append("ReflectionToStringBuilder")
    if "FieldUtils" in target_method_text:
        priority_classes.append("FieldUtils")
    if "MethodUtils" in target_method_text:
        priority_classes.append("MethodUtils")
    if "ConstructorUtils" in target_method_text:
        priority_classes.append("ConstructorUtils")

    def source_priority(path: Path) -> tuple[int, int, str]:
        stem = path.stem
        if stem in priority_classes:
            return 0, priority_classes.index(stem), path.as_posix()
        if stem and re.search(rf"\b{re.escape(stem)}\b", target_method_text):
            return 1, 0, path.as_posix()
        return 2, 0, path.as_posix()

    seen: set[Path] = set()
    for path in sorted(search_roots, key=source_priority):
        if path in seen or not path.exists() or path.is_dir():
            continue
        seen.add(path)
        original = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if "getDeclared" not in original:
            continue
        for spec in candidate_patterns:
            if "consume" in spec:
                match = spec["consume"].search(original)
                if not match:
                    continue
                revised = original[: match.start()] + spec["replacement"](original=original, **match.groupdict()) + original[match.end() :]
            else:
                match = spec["assign"].search(original)
                if not match:
                    continue
                var = match.group("var")
                # Require local evidence that the reflected member array is consumed later.
                tail = original[match.end() :]
                if not re.search(rf"\b{re.escape(var)}\b", tail):
                    continue
                if re.search(rf"(?:java\.util\.)?Arrays\.sort\s*\(\s*{re.escape(var)}\b", tail):
                    continue
                revised = original[: match.start()] + spec["replacement"](original=original, **match.groupdict()) + original[match.end() :]
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


def stable_collection_construction_evidence(method_text: str, tail_text: str, var_name: str) -> tuple[bool, str]:
    """Check whether a HashSet/HashMap construction flows into order-sensitive output."""
    if "SerializerFeature.WriteClassName" in method_text or re.search(r'"[^"]*(?:HashSet|Set|HashMap|Map)\[', method_text):
        return False, "serialized type marker is part of the asserted output"
    escaped = re.escape(var_name)
    if re.search(rf"\b(?:JSON|JSONObject|JSONArray)\.toJSONString\s*\([^;\n]*\b{escaped}\b", tail_text):
        return True, "collection serialized directly with JSON.toJSONString"
    if re.search(rf"\b{escaped}\b\.toString\s*\(\s*\)", tail_text) and re.search(r"\bassert(?:That|Equals|True)\b", tail_text):
        return True, "collection string form used by an assertion"
    if re.search(rf"\bCollectionUtils\.get\s*\(\s*{escaped}\s*,", tail_text):
        return True, "collection is indexed through CollectionUtils.get"
    if re.search(rf"\b[A-Za-z_][\w.]*\.(?:put|set|add)\s*\([^;\n]*\b{escaped}\b", tail_text) and re.search(
        r"Object\.entries|Match\.that|\.isEqualTo\s*\(\s*\"[\[{]|assert(?:That|Equals|True)\b",
        tail_text,
    ):
        return True, "collection is passed to framework code and later checked by order-sensitive assertion"
    if re.search(rf"\b[A-Za-z_]\w*\s*=\s*[^;\n]*\([^;\n]*\b{escaped}\b[^;\n]*\)\s*;", tail_text) and re.search(
        r"\bassert(?:That|Equals|True)\b|\.isEqualTo\s*\(\s*\"",
        tail_text,
    ):
        return True, "collection is passed through a derived value that is later asserted"
    if re.search(rf"\.toString\s*\(\s*{escaped}\s*\)", tail_text) and re.search(
        r"\.isEqualTo\s*\(\s*\"[\[{]|assert(?:That|Equals)\b",
        tail_text,
    ):
        return True, "collection is rendered to a string that is later asserted"
    if re.search(rf"\bArrays\.asList\s*\([^;\n]*\b{escaped}\b", tail_text) and re.search(
        r"\bassert(?:That|Equals|True)\b|\.isEqualTo\s*\(\s*\"",
        tail_text,
    ):
        return True, "collection is embedded in an aggregate whose printed form is asserted"
    if re.match(r"(?i)expected", var_name) and re.search(
        rf"\b(?:test|assert|verify|check)[A-Za-z_]\w*\s*\([^;\n]*\b{escaped}\b",
        tail_text,
    ):
        return True, "expected fixture collection is passed to a test helper"
    if re.search(rf"\bassertEquals\s*\(\s*{escaped}\s*,", tail_text) or re.search(
        rf"\bassertEquals\s*\([^;\n]+,\s*{escaped}\s*\)", tail_text
    ):
        return True, "collection compared by equality after indexed access"

    setter_match = re.search(rf"\b(?P<holder>[A-Za-z_]\w*)\.set[A-Za-z_]\w*\s*\(\s*{escaped}\s*\)\s*;", tail_text)
    if setter_match:
        holder = setter_match.group("holder")
        holder_tail = tail_text[setter_match.end() :]
        if re.search(rf"\b(?:JSON|JSONObject|JSONArray)\.toJSONString\s*\([^;\n]*\b{re.escape(holder)}\b", holder_tail):
            return True, "collection assigned into object that is serialized with JSON.toJSONString"
        if re.search(rf"\b{re.escape(holder)}\b\.toString\s*\(\s*\)", holder_tail) and re.search(
            r"\bassert(?:That|Equals|True)\b", holder_tail
        ):
            return True, "collection assigned into object whose string form is asserted"
        derived_match = re.search(
            rf"\b(?P<derived>[A-Za-z_]\w*)\s*=\s*[^;\n]*\([^;\n]*\b{re.escape(holder)}\b[^;\n]*\)\s*;",
            holder_tail,
        )
        if derived_match and re.search(
            rf"\bassert(?:That|Equals|True)\b[^;\n]*\b{re.escape(derived_match.group('derived'))}\b",
            holder_tail[derived_match.end() :],
        ):
            return True, "collection assigned into object whose derived string is asserted"

    if re.search(rf"\b{escaped}\b", tail_text) and "JSON.toJSONString" in method_text and re.search(
        r"\bassertEquals\s*\(\s*\"", method_text
    ):
        return True, "collection appears in JSON/string assertion method"
    return False, "collection does not visibly flow into an order-sensitive assertion"


def synthesize_stable_collection_helper_construction(
    original: str,
    method_text: str,
    rel_path: str,
) -> tuple[str, dict[str, Any]] | None:
    helper_refs: list[tuple[str, str]] = []
    assignment_pattern = re.compile(
        r"\b(?:final\s+)?(?:java\.util\.)?(?:Map|HashMap)\s*(?:<[^>]+>)?\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<helper>[A-Za-z_]\w*)\s*\(\s*\)\s*;"
    )
    for match in assignment_pattern.finditer(method_text):
        var_name = match.group("var")
        helper_name = match.group("helper")
        if re.search(rf"\b{re.escape(var_name)}\b\.(?:entrySet|values|keySet)\s*\(", method_text):
            helper_refs.append((var_name, helper_name))
    if not helper_refs:
        return None

    lines = original.splitlines()
    revised_lines = list(lines)
    changed = 0
    findings: list[dict[str, Any]] = []
    masked_original = mask_java_non_code(original)
    for var_name, helper_name in helper_refs:
        method_pattern = re.compile(
            rf"(?m)^\s*(?:private|protected|public)?\s*(?:static\s+)?"
            rf"(?:java\.util\.)?(?:Map|HashMap)\s*(?:<[^>]+>)?\s+"
            rf"{re.escape(helper_name)}\s*\(\s*\)\s*(?:throws\s+[^{{]+)?\{{"
        )
        helper_match = method_pattern.search(masked_original)
        if not helper_match:
            continue
        helper_start_line = masked_original[: helper_match.end()].count("\n") + 1
        helper_bounds = enclosing_method_bounds_for_line(original, helper_start_line)
        if not helper_bounds[0] or not helper_bounds[1]:
            continue
        helper_start, helper_end = int(helper_bounds[0]), int(helper_bounds[1])
        helper_lines = revised_lines[helper_start - 1 : helper_end]
        helper_text = "\n".join(helper_lines)
        construction_match = re.search(
            r"\b(?P<decl>(?:java\.util\.)?(?:Map|HashMap)\s*(?:<[^>]+>)?\s+"
            r"(?P<mapvar>[A-Za-z_]\w*)\s*=\s*)new\s+(?:java\.util\.)?HashMap"
            r"(?P<generic>\s*<[^;(){}=]*>)?\s*\((?P<args>[^;{}]*)\)\s*;",
            helper_text,
        )
        if not construction_match:
            continue
        map_var = construction_match.group("mapvar")
        if not re.search(rf"\b{re.escape(map_var)}\.put\s*\(", helper_text):
            continue
        if not re.search(rf"\breturn\s+{re.escape(map_var)}\s*;", helper_text):
            continue
        helper_revised_text = re.sub(
            r"\b(?P<decl>(?:java\.util\.)?(?:Map|HashMap)\s*(?:<[^>]+>)?\s+"
            rf"{re.escape(map_var)}\s*=\s*)new\s+(?:java\.util\.)?HashMap"
            r"(?P<generic>\s*<[^;(){}=]*>)?\s*\((?P<args>[^;{}]*)\)\s*;",
            lambda m: f"{m.group('decl')}new java.util.LinkedHashMap{m.group('generic') or ''}({m.group('args')});",
            helper_text,
            count=1,
        )
        if helper_revised_text == helper_text:
            continue
        revised_lines[helper_start - 1 : helper_end] = helper_revised_text.splitlines()
        changed += 1
        findings.append(
            {
                "var": var_name,
                "helper": helper_name,
                "reason": "target iterates map returned by a test fixture helper",
                "replacement": "LinkedHashMap",
            }
        )

    if not changed:
        return None
    revised = "\n".join(revised_lines) + ("\n" if original.endswith("\n") else "")
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_STABLE_COLLECTION_CONSTRUCTION",
            "target_file": rel_path,
            "changed_constructions": changed,
            "guard": "target method iterates a map returned by a fixture helper",
            "findings": findings,
        }
    )
    return patch, meta


def synthesize_stable_collection_construction(
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
    if "HashSet" not in method_text and "HashMap" not in method_text and "ImmutableMap.of" not in method_text:
        helper_patch = synthesize_stable_collection_helper_construction(original, method_text, rel_path)
        if helper_patch:
            return helper_patch
        return "", {"ok": False, "error_class": "no_hash_collection_construction", "error": ""}
    if "LinkedHashSet" in method_text or "LinkedHashMap" in method_text:
        return "", {"ok": False, "error_class": "stable_collection_already_visible", "error": ""}

    construction = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>final\s+)?"
        r"(?P<type>[A-Za-z_][\w.]*\s*(?:<[^;=]+>)?)\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*new\s+"
        r"(?P<ctor>(?:java\.util\.)?(?:HashSet|HashMap))"
        r"(?P<ctor_generic>\s*<[^;(){}=]*>)?\s*\((?P<args>[^;{}]*)\)\s*;\s*$"
    )
    double_brace_construction = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>final\s+)?"
        r"(?P<type>[A-Za-z_][\w.]*\s*(?:<[^;=]+>)?)\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*new\s+"
        r"(?P<ctor>(?:java\.util\.)?(?:HashSet|HashMap))"
        r"(?P<ctor_generic>\s*<[^;(){}=]*>)?\s*\((?P<args>[^;{}]*)\)\s*\{\{\s*$"
    )

    revised_method: list[str] = []
    changed = 0
    findings: list[dict[str, Any]] = []
    for offset, line in enumerate(method_lines):
        match = construction.match(line)
        is_double_brace = False
        if not match:
            match = double_brace_construction.match(line)
            is_double_brace = bool(match)
        if not match:
            revised_method.append(line)
            continue

        declared_type = match.group("type").strip()
        declared_base = re.sub(r"\s*<.*", "", declared_type).split(".")[-1]
        ctor_base = match.group("ctor").split(".")[-1]
        if ctor_base == "HashSet" and declared_base not in {"Set", "HashSet"}:
            revised_method.append(line)
            continue
        if ctor_base == "HashMap" and declared_base not in {"Map", "HashMap"}:
            revised_method.append(line)
            continue

        tail_text = "\n".join(method_lines[offset + 1 :])
        visible, reason = stable_collection_construction_evidence(method_text, tail_text, match.group("var"))
        if not visible:
            findings.append({"var": match.group("var"), "reason": reason})
            revised_method.append(line)
            continue

        stable_base = "LinkedHashSet" if ctor_base == "HashSet" else "LinkedHashMap"
        if declared_base in {"HashSet", "HashMap"}:
            stable_declared_type = re.sub(
                rf"\b(?:java\.util\.)?{declared_base}\b",
                f"java.util.{stable_base}",
                declared_type,
                count=1,
            )
        else:
            stable_declared_type = declared_type
        stable_ctor = f"java.util.{stable_base}{match.group('ctor_generic') or ''}"
        suffix = " {{" if is_double_brace else ";"
        revised_method.append(
            f"{match.group('indent')}{match.group('prefix') or ''}{stable_declared_type} "
            f"{match.group('var')} = new {stable_ctor}({match.group('args')}){suffix}"
        )
        changed += 1
        findings.append({"var": match.group("var"), "reason": reason, "replacement": stable_base})

    if re.search(r"\bassert(?:That|Equals|True)\b|\.isEqualTo\s*\(\s*\"[\[{]", method_text):
        inline_revised: list[str] = []
        for line in revised_method:
            new_line = re.sub(
                r"new\s+HashSet\s*(<[^;(){}=]*>)?\s*\(",
                lambda m: f"new java.util.LinkedHashSet{m.group(1) or ''}(",
                line,
            )
            if new_line != line:
                changed += 1
                findings.append({"var": "<inline>", "reason": "inline HashSet flows into asserted printed aggregate", "replacement": "LinkedHashSet"})
            inline_revised.append(new_line)
        revised_method = inline_revised

    if changed > 0 and re.search(r"(?i)\bexpected[A-Za-z_]*\b", method_text):
        fixture_revised: list[str] = []
        for line in revised_method:
            new_line = re.sub(
                r"new\s+HashMap\s*(<[^;(){}=]*>)?\s*\(\s*\)\s*\{\{",
                lambda m: f"new java.util.LinkedHashMap{m.group(1) or ''}() {{{{",
                line,
            )
            new_line = re.sub(
                r"new\s+HashSet\s*(<[^;(){}=]*>)?\s*\(\s*\)\s*\{\{",
                lambda m: f"new java.util.LinkedHashSet{m.group(1) or ''}() {{{{",
                new_line,
            )
            if new_line != line:
                changed += 1
                findings.append(
                    {
                        "var": "<nested-fixture>",
                        "reason": "nested expected fixture collection uses double-brace initialization",
                        "replacement": "LinkedHashMap/LinkedHashSet",
                    }
                )
            fixture_revised.append(new_line)
        revised_method = fixture_revised

    joined_method = "\n".join(revised_method)
    immutable_matches = list(re.finditer(r"\bImmutableMap\.of\s*\((?P<args>[^(){};]*)\)", joined_method))
    for immutable_match in reversed(immutable_matches):
        args = [part.strip() for part in immutable_match.group("args").split(",")]
        if len(args) < 2 or len(args) % 2 or any(not part for part in args):
            continue
        if any(re.search(r"[(){};]", part) for part in args):
            continue
        line_start = joined_method.rfind("\n", 0, immutable_match.start()) + 1
        line_text = joined_method[line_start : joined_method.find("\n", line_start) if "\n" in joined_method[line_start:] else len(joined_method)]
        indent = re.match(r"^\s*", line_text).group(0)
        line_number = int(method_start) + joined_method.count("\n", 0, immutable_match.start())
        stable_var = f"stabilityOpsOrderedMap{line_number}"
        declaration = f"{indent}final java.util.Map<Object, Object> {stable_var} = new java.util.LinkedHashMap<Object, Object>();\n"
        puts = "".join(
            f"{indent}{stable_var}.put({args[index]}, {args[index + 1]});\n"
            for index in range(0, len(args), 2)
        )
        joined_method = (
            joined_method[:line_start]
            + declaration
            + puts
            + joined_method[line_start : immutable_match.start()]
            + stable_var
            + joined_method[immutable_match.end() :]
        )
        changed += 1
        findings.append(
            {
                "var": stable_var,
                "reason": "literal ImmutableMap entries flow into an order-sensitive string assertion",
                "replacement": "LinkedHashMap",
            }
        )
    revised_method = joined_method.splitlines()

    if changed == 0:
        return "", {
            "ok": False,
            "error_class": "no_stable_collection_construction",
            "error": "; ".join(item.get("reason", "") for item in findings[:3]),
            "findings": findings,
        }

    revised_lines = list(lines)
    revised_lines[int(method_start) - 1 : int(method_end)] = revised_method
    revised = "\n".join(revised_lines) + ("\n" if original.endswith("\n") else "")
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_STABLE_COLLECTION_CONSTRUCTION",
            "target_file": rel_path,
            "changed_constructions": changed,
            "guard": "HashSet/HashMap construction flows into order-sensitive JSON/string assertion",
            "findings": findings,
        }
    )
    return patch, meta


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


def target_module_build_files(sample: dict[str, str]) -> list[Path]:
    """Return build files that can contribute dependencies to the target module."""
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return []
    module = str(sample.get("validated_module_path") or sample.get("Module Path") or ".").strip()
    module_dir = repo_dir if module in {"", "."} else repo_dir / module
    if not module_dir.exists():
        module_dir = repo_dir
    files: list[Path] = []
    current = module_dir.resolve()
    root = repo_dir.resolve()
    while True:
        for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
            candidate = current / name
            if candidate.exists():
                files.append(candidate)
        if current == root or root not in current.parents:
            break
        current = current.parent
    return files


def target_module_mentions(sample: dict[str, str], tokens: tuple[str, ...]) -> bool:
    for build_file in target_module_build_files(sample):
        try:
            text = build_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(token in text for token in tokens):
            return True
    return False


def target_module_direct_mentions(sample: dict[str, str], tokens: tuple[str, ...]) -> bool:
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return False
    module = str(sample.get("validated_module_path") or sample.get("Module Path") or ".").strip()
    module_dir = repo_dir if module in {"", "."} else repo_dir / module
    if not module_dir.exists():
        module_dir = repo_dir
    for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        candidate = module_dir / name
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(token in text for token in tokens):
            return True
    return False


def target_module_source_mentions(sample: dict[str, str], tokens: tuple[str, ...]) -> bool:
    repo_dir = Path(sample.get("remote_repo_dir", ""))
    if not repo_dir.exists():
        return False
    module = str(sample.get("validated_module_path") or sample.get("Module Path") or ".").strip()
    module_dir = repo_dir if module in {"", "."} else repo_dir / module
    if not module_dir.exists():
        return False
    for index, source in enumerate(module_dir.rglob("*.java")):
        if index >= 120:
            break
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(token in text for token in tokens):
            return True
    return False


def project_mentions_jackson(sample: dict[str, str], original: str) -> bool:
    """Return whether Jackson is visible to the target source module."""
    if (
        "com.fasterxml.jackson" in original
        or "ObjectMapper" in original
        or "JsonNode" in original
        or re.search(r"\b(?:JsonUtil|JsonUtils|JSONUtil|JsonMapper)\b", original)
    ):
        return True
    tokens = ("com.fasterxml.jackson", "jackson-databind")
    return target_module_direct_mentions(sample, tokens)


def project_mentions_object_json_parser(sample: dict[str, str], original: str) -> bool:
    """Return whether Gson is visible to the target source module."""
    if "com.google.gson" in original or "Gson" in original:
        return True
    build_tokens = ("com.google.code.gson", ">gson<", '"gson"')
    return target_module_direct_mentions(sample, build_tokens)


def source_mentions_org_json_object(original: str) -> bool:
    """Return whether the target source can construct org.json JSONObjects."""
    return bool(
        "org.json.JSONObject" in original
        or re.search(r"^\s*import\s+org\.json\.JSONObject\s*;", original, flags=re.M)
    )


def synthesize_indexed_list_sort_before_assertions(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
    method_text = "\n".join(method_lines)
    if ".get(" not in method_text or "assert" not in method_text:
        return "", {"ok": False, "error_class": "no_indexed_list_assertion", "error": ""}

    list_decl = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:final\s+)?)"
        r"(?P<type>(?:java\.util\.)?List\s*<[^;=]+>|List\s*<[^;=]+>)\s+"
        r"(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<source>[^;]+);\s*$"
    )
    indexed_assert = re.compile(
        r"assert(?:Equals|That|True)?[^(]*\([^;\n]*\b(?P<var>[A-Za-z_]\w*)\.get\s*\(\s*\d+\s*\)"
        r"\.(?P<getter>get[A-Za-z_]\w*)\s*\(\s*\)",
    )
    indexed_assignment = re.compile(
        r"^(?P<indent>\s*)(?:final\s+)?[A-Za-z_][\w.<>?,\s]*\s+(?P<alias>[A-Za-z_]\w*)\s*="
        r"\s*(?P<var>[A-Za-z_]\w*)\.get\s*\(\s*\d+\s*\)\s*;\s*$"
    )
    alias_getter_assert = re.compile(
        r"assert(?:Equals|That|True)?[^(]*\([^;\n]*\b(?P<alias>[A-Za-z_]\w*)\.(?P<getter>get[A-Za-z_]\w*)\s*\(\s*\)",
    )

    declarations: dict[str, tuple[int, str]] = {}
    for offset, line in enumerate(method_lines):
        match = list_decl.match(line)
        if match:
            declarations[match.group("var")] = (int(method_start) + offset, match.group("type").strip())

    getter_by_var: dict[str, str] = {}
    alias_to_var: dict[str, str] = {}
    for line in method_lines:
        assignment = indexed_assignment.match(line)
        if assignment:
            alias_to_var[assignment.group("alias")] = assignment.group("var")
        for match in indexed_assert.finditer(line):
            getter_by_var.setdefault(match.group("var"), match.group("getter"))
        for match in alias_getter_assert.finditer(line):
            var = alias_to_var.get(match.group("alias"))
            if var:
                getter_by_var.setdefault(var, match.group("getter"))

    insertion: tuple[int, str] | None = None
    for var, getter in getter_by_var.items():
        declaration = declarations.get(var)
        if not declaration:
            continue
        line_no, declared_type = declaration
        method_tail = "\n".join(lines_plain[line_no : int(method_end)])
        if re.search(rf"\b{re.escape(var)}\s*\.\s*(?:sort|sort\s*\()", method_tail) or f"Collections.sort({var}" in method_tail:
            continue
        decl_line = lines_plain[line_no - 1]
        indent = re.match(r"^(\s*)", decl_line).group(1)
        element_match = re.search(r"List\s*<\s*([^<>?,]+)\s*>", declared_type)
        element_type = (element_match.group(1).strip() if element_match else "Object") or "Object"
        if element_type == "?":
            continue
        statement = (
            f"{indent}java.util.Collections.sort({var}, new java.util.Comparator<{element_type}>() {{ "
            f"public int compare({element_type} left, {element_type} right) {{ "
            f"return String.valueOf(left.{getter}()).compareTo(String.valueOf(right.{getter}())); "
            f"}} }});"
        )
        insertion = (line_no, statement)
        break

    if not insertion:
        return "", {"ok": False, "error_class": "no_sortable_indexed_list_assertion", "error": ""}

    line_no, statement = insertion
    revised = "".join(lines_keepends[:line_no]) + statement + "\n" + "".join(lines_keepends[line_no:])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_LIST_ORDER_INSENSITIVE",
            "target_file": rel_path,
            "guard": "indexed list assertions use a visible getter and can be stabilized by sorting before assertions",
        }
    )
    return patch, meta


def synthesize_and_clause_order_insensitive_assertion(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    for index in range(int(method_start) - 1, int(method_end)):
        line = lines_plain[index]
        if "assertEquals" not in line or " AND " not in line:
            continue
        parsed = split_assert_equals_arguments(line)
        if not parsed:
            continue
        indent, expected, actual = parsed
        expected_literal = java_string_literal_value(expected)
        if expected_literal is None or expected_literal.count(" AND ") < 1:
            continue
        body = expected_literal.replace("<where>", "").replace("</where>", "").strip()
        body = re.sub(r"^\s*AND\s+", "", body)
        clauses = [part.strip() for part in re.split(r"\s+AND\s+", body) if part.strip()]
        if len(clauses) < 2:
            continue
        clause_array = "new String[] {" + ", ".join(java_string_literal(item) for item in clauses) + "}"
        actual_var = f"stabilityOpsActualSql{index + 1}"
        expected_set = f"new java.util.HashSet(java.util.Arrays.asList({clause_array}))"
        actual_set = (
            f"new java.util.HashSet(java.util.Arrays.asList({actual_var}.replace(\"<where>\", \"\")"
            f".replace(\"</where>\", \"\").replaceFirst(\"^\\\\s*AND\\\\s+\", \"\")"
            f".split(\"\\\\s+AND\\\\s+\")))"
        )
        replacement = (
            f"{indent}final String {actual_var} = {actual};\n"
            f"{render_equality_assertion(line, indent, expected_set, actual_set)}\n"
        )
        revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[index + 1 :])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": "ID_LIST_ORDER_INSENSITIVE",
                "target_file": rel_path,
                "guard": "AND-separated SQL conditions in literal expected string are compared order-insensitively",
            }
        )
        return patch, meta

    return "", {"ok": False, "error_class": "no_and_clause_assertion", "error": ""}


def synthesize_contains_fragments_order_insensitive(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
    span_start, span_end = line_span_from_action(action)
    assignment_start = None
    assignment_end = None
    target_var = ""
    search_blocks: list[tuple[int, int, list[str]]] = []
    if span_start and span_end:
        search_blocks.append((span_start, span_end, lines_plain[span_start - 1 : span_end]))
    for offset, line in enumerate(method_lines):
        search_blocks.append((int(method_start) + offset, int(method_start) + offset, [line]))

    for block_start, _block_end, initial_lines in search_blocks:
        line = initial_lines[0] if initial_lines else ""
        match = re.search(r"\b(?:final\s+)?String\s+([A-Za-z_]\w*)\s*=", line)
        if not match:
            continue
        block_lines = list(initial_lines)
        end_line = block_start + len(block_lines) - 1
        while end_line < int(method_end) and ";" not in block_lines[-1]:
            end_line += 1
            if end_line > int(method_end):
                break
            block_lines.append(lines_plain[end_line - 1])
        block_text = "\n".join(block_lines)
        if "+" not in block_text:
            continue
        literals = re.findall(r'"(?:\\.|[^"\\])*"', block_text)
        substantive = [literal for literal in literals if java_string_literal_value(literal) and java_string_literal_value(literal).strip()]
        if len(substantive) < 2:
            continue
        target_var = match.group(1)
        assignment_start = block_start
        assignment_end = end_line
        break

    if not target_var or assignment_start is None or assignment_end is None:
        return "", {"ok": False, "error_class": "no_concatenated_expected_string", "error": ""}

    assertion_re = re.compile(
        rf"^(?P<indent>\s*)(?P<prefix>(?:Assert\.|Assertions\.)?)assertTrue\s*\(\s*(?P<actual>[A-Za-z_][\w.]*)\.contains\s*\(\s*{re.escape(target_var)}\s*\)\s*\)\s*;\s*$"
    )
    for index in range(assignment_end, int(method_end)):
        match = assertion_re.match(lines_plain[index])
        if not match:
            continue
        block_text = "\n".join(lines_plain[assignment_start - 1 : assignment_end])
        literals = re.findall(r'"(?:\\.|[^"\\])*"', block_text)
        fragments = [literal for literal in literals if java_string_literal_value(literal) and java_string_literal_value(literal).strip()]
        if len(fragments) < 2:
            return "", {"ok": False, "error_class": "not_enough_contains_fragments", "error": target_var}
        replacement = "".join(
            f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('actual')}.contains({fragment}));\n"
            for fragment in fragments
        )
        revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[index + 1 :])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": "ID_LIST_ORDER_INSENSITIVE",
                "target_file": rel_path,
                "guard": "concatenated expected string is only used by contains, so fragments can be checked independently",
                "fragment_count": len(fragments),
            }
        )
        return patch, meta

    return "", {"ok": False, "error_class": "no_contains_assertion_for_concatenated_string", "error": target_var}


def synthesize_iterator_next_membership_assertion(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    iterator_assignment = re.compile(
        r"^(?P<indent>\s*)(?:final\s+)?[A-Za-z_][\w.<>?,\s]*\s+(?P<alias>[A-Za-z_]\w*)\s*="
        r"\s*(?P<source>.+?)\.iterator\s*\(\s*\)\s*\.next\s*\(\s*\)\s*;\s*$"
    )
    for index in range(int(method_start) - 1, int(method_end) - 1):
        assignment = iterator_assignment.match(lines_plain[index])
        if not assignment:
            continue
        alias = assignment.group("alias")
        source = assignment.group("source").strip()
        if any(token in source for token in ["new ", "remove", "delete", "clear"]):
            continue
        for assert_index in range(index + 1, min(index + 5, int(method_end))):
            line = lines_plain[assert_index]
            parsed = split_assert_equals_arguments(line)
            if not parsed or alias not in line:
                continue
            indent, expected, actual = parsed
            member_match = re.search(rf"\b{re.escape(alias)}\.(?P<accessor>[A-Za-z_]\w*)\s*\(\s*\)", actual)
            if not member_match:
                member_match = re.search(rf"\b{re.escape(alias)}\.(?P<accessor>[A-Za-z_]\w*)\s*\(\s*\)", expected)
                expected, actual = actual, expected
            if not member_match:
                continue
            accessor = member_match.group("accessor")
            if not re.match(r"^(?:get|is|has|[a-z][A-Za-z0-9_]*)", accessor):
                continue
            prefix = re.match(r"^(?P<indent>\s*)(?P<prefix>(?:Assert\.)?)assertEquals", line)
            assert_prefix = (prefix.group("prefix") if prefix else "") + "assertTrue"
            replacement = (
                f"{assignment.group('indent')}{assert_prefix}("
                f"java.util.stream.StreamSupport.stream({source}.spliterator(), false)"
                f".anyMatch(item -> {expected}.equals(item.{accessor}())));"
            )
            revised = (
                "".join(lines_keepends[:index])
                + replacement
                + "\n"
                + "".join(lines_keepends[index + 1 : assert_index])
                + "".join(lines_keepends[assert_index + 1 :])
            )
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_LIST_ORDER_INSENSITIVE",
                    "target_file": rel_path,
                    "guard": "iterator().next() result is only used to assert membership by a visible accessor",
                    "source": source,
                    "accessor": accessor,
                }
            )
            return patch, meta

    return "", {"ok": False, "error_class": "no_iterator_next_membership_assertion", "error": ""}


def synthesize_multiline_string_order_insensitive_assertion(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    span_start, span_end = line_span_from_action(action)
    candidate_expected_vars: list[str] = []
    candidate_blocks: list[str] = []
    if span_start and span_end:
        selected_text = "\n".join(lines_plain[span_start - 1 : span_end])
        var_match = re.search(r"\b(?:final\s+)?String\s+([A-Za-z_]\w*)\s*=", selected_text)
        if var_match:
            candidate_expected_vars.append(var_match.group(1))
            candidate_blocks.append(selected_text)
    filtered_vars = [
        var for var, block in zip(candidate_expected_vars, candidate_blocks)
        if "BR" in block or "\\n" in block or "\\r" in block
    ]
    if not filtered_vars:
        return "", {"ok": False, "error_class": "no_multiline_expected_string", "error": ""}

    for expected_var in filtered_vars:
        for index in range(int(method_start) - 1, int(method_end)):
            line = lines_plain[index]
            if "assertEquals" not in line or expected_var not in line:
                continue
            block = line
            end_index = index
            while ");" not in block and end_index + 1 < int(method_end):
                end_index += 1
                block += "\n" + lines_plain[end_index]
            parsed = split_assert_equals_arguments(block)
            if not parsed:
                continue
            indent, expected, actual = parsed
            if expected.strip() != expected_var:
                continue
            actual_expr = actual.split(",")[0].strip() if "," in actual else actual.strip()
            if expected_var not in block or not actual_expr:
                continue
            expected_lines_var = f"stabilityOpsExpectedLines{index + 1}"
            actual_lines_var = f"stabilityOpsActualLines{index + 1}"
            replacement = (
                f"{indent}final java.util.List<String> {expected_lines_var} = "
                f"new java.util.ArrayList<String>(java.util.Arrays.asList(String.valueOf({expected_var}).split(\"\\\\R\")));\n"
                f"{indent}final java.util.List<String> {actual_lines_var} = "
                f"new java.util.ArrayList<String>(java.util.Arrays.asList(String.valueOf({actual_expr}).split(\"\\\\R\")));\n"
                f"{indent}for (int stabilityOpsLine = 0; stabilityOpsLine < {expected_lines_var}.size(); stabilityOpsLine++) {{\n"
                f"{indent}    final String stabilityOpsValue = {expected_lines_var}.get(stabilityOpsLine);\n"
                f"{indent}    if (stabilityOpsValue.matches(\"\\\\s+(?:[A-Za-z_$][A-Za-z0-9_$]*=.*|[\\\\}}\\\\]]),$\")) {{\n"
                f"{indent}        {expected_lines_var}.set(stabilityOpsLine, stabilityOpsValue.substring(0, stabilityOpsValue.length() - 1));\n"
                f"{indent}    }}\n"
                f"{indent}}}\n"
                f"{indent}for (int stabilityOpsLine = 0; stabilityOpsLine < {actual_lines_var}.size(); stabilityOpsLine++) {{\n"
                f"{indent}    final String stabilityOpsValue = {actual_lines_var}.get(stabilityOpsLine);\n"
                f"{indent}    if (stabilityOpsValue.matches(\"\\\\s+(?:[A-Za-z_$][A-Za-z0-9_$]*=.*|[\\\\}}\\\\]]),$\")) {{\n"
                f"{indent}        {actual_lines_var}.set(stabilityOpsLine, stabilityOpsValue.substring(0, stabilityOpsValue.length() - 1));\n"
                f"{indent}    }}\n"
                f"{indent}}}\n"
                f"{indent}java.util.Collections.sort({expected_lines_var});\n"
                f"{indent}java.util.Collections.sort({actual_lines_var});\n"
                f"{render_equality_assertion(block, indent, expected_lines_var, actual_lines_var)}\n"
            )
            revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[end_index + 1 :])
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_LIST_ORDER_INSENSITIVE",
                    "target_file": rel_path,
                    "guard": "multiline top-level field lines are comma-normalized and compared without field-order assumptions",
                }
            )
            return patch, meta

    return "", {"ok": False, "error_class": "no_multiline_string_assertion", "error": ",".join(filtered_vars)}


def synthesize_map_to_string_order_insensitive_assertion(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    for index in range(int(method_start) - 1, int(method_end)):
        line = lines_plain[index]
        if ("assertEquals" not in line and ".isEqualTo" not in line) or ".toString()" not in line:
            continue
        block = line
        end_index = index
        while ");" not in block and end_index + 1 < int(method_end):
            end_index += 1
            block += "\n" + lines_plain[end_index]
        parsed = split_assert_equals_arguments(block) or split_assertj_is_equal_to_arguments(block)
        if not parsed:
            continue
        indent, expected, actual = parsed
        expected_literal = java_string_literal_value(expected)
        if not expected_literal:
            continue
        braced_map = expected_literal.startswith("{") and expected_literal.endswith("}")
        bracket_entries = bool(re.search(r"(?:^|,\s*)[^,=]+=\[[^]]*\]", expected_literal))
        if not braced_map and not bracket_entries:
            continue
        actual_expr = actual.split(",")[0].strip() if "," in actual else actual.strip()
        if ".toString()" not in actual_expr:
            continue
        expected_var = f"stabilityOpsExpectedEntries{index + 1}"
        actual_var = f"stabilityOpsActualEntries{index + 1}"
        if braced_map:
            expected_entries = f'{expected_var}.replace("{{", "").replace("}}", "").split(",\\\\s*")'
            actual_entries = f'{actual_var}.replace("{{", "").replace("}}", "").split(",\\\\s*")'
            guard = "map-style toString assertion is compared as an unordered entry set"
        else:
            entry_split = '",\\\\s+(?=[^,=]+=\\\\[)"'
            expected_entries = f"{expected_var}.split({entry_split})"
            actual_entries = f"{actual_var}.split({entry_split})"
            guard = "bracket-valued header/map entries are compared without entry-order assumptions"
        equality = render_equality_assertion(
            block,
            indent,
            f"new java.util.HashSet(java.util.Arrays.asList({expected_entries}))",
            f"new java.util.HashSet(java.util.Arrays.asList({actual_entries}))",
        )
        replacement = (
            f"{indent}final String {expected_var} = {expected};\n"
            f"{indent}final String {actual_var} = {actual_expr};\n"
            f"{equality}\n"
        )
        revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[end_index + 1 :])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": "ID_LIST_ORDER_INSENSITIVE",
                "target_file": rel_path,
                "guard": guard,
            }
        )
        return patch, meta

    return "", {"ok": False, "error_class": "no_map_to_string_assertion", "error": ""}


def synthesize_reflection_to_string_order_insensitive_assertion(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    generated_blocks: list[tuple[int, int, str]] = []

    for index in range(int(method_start) - 1, int(method_end)):
        line = lines_plain[index]
        if "assertEquals" not in line:
            continue
        block = line
        end_index = index
        while ");" not in block and end_index + 1 < int(method_end):
            end_index += 1
            block += "\n" + lines_plain[end_index]
        parsed = split_assert_equals_arguments(block)
        if not parsed:
            continue
        indent, expected, actual = parsed
        evidence = " ".join([block, expected, actual])
        if not (
            "ReflectionToStringBuilder" in evidence
            or "ToStringBuilder" in evidence
            or "toBaseString(" in evidence
            or re.search(r"\.toString\s*\(", evidence)
        ):
            continue
        if "[" not in evidence or "]" not in evidence or "=" not in evidence:
            continue
        if any(token in evidence for token in ["JSONObject", "JSONArray", "ObjectMapper", "readTree"]):
            continue
        suffix = index + 1
        expected_var = f"stabilityOpsExpectedToString{suffix}"
        actual_var = f"stabilityOpsActualToString{suffix}"
        expected_start = f"stabilityOpsExpectedStart{suffix}"
        actual_start = f"stabilityOpsActualStart{suffix}"
        expected_end = f"stabilityOpsExpectedEnd{suffix}"
        actual_end = f"stabilityOpsActualEnd{suffix}"
        expected_body = f"stabilityOpsExpectedFields{suffix}"
        actual_body = f"stabilityOpsActualFields{suffix}"
        expected_set = f"stabilityOpsExpectedFieldSet{suffix}"
        actual_set = f"stabilityOpsActualFieldSet{suffix}"
        split_regex = java_string_literal(",(?=[A-Za-z_$][A-Za-z0-9_$]*=)")
        replacement = (
            f"{indent}final String {expected_var} = String.valueOf({expected});\n"
            f"{indent}final String {actual_var} = String.valueOf({actual});\n"
            f"{indent}final int {expected_start} = {expected_var}.indexOf('[');\n"
            f"{indent}final int {actual_start} = {actual_var}.indexOf('[');\n"
            f"{indent}final int {expected_end} = {expected_var}.lastIndexOf(']');\n"
            f"{indent}final int {actual_end} = {actual_var}.lastIndexOf(']');\n"
            f"{indent}assertEquals({expected_var}.substring(0, {expected_start}), {actual_var}.substring(0, {actual_start}));\n"
            f"{indent}final String {expected_body} = {expected_var}.substring({expected_start} + 1, {expected_end});\n"
            f"{indent}final String {actual_body} = {actual_var}.substring({actual_start} + 1, {actual_end});\n"
            f"{indent}final java.util.Set<String> {expected_set} = new java.util.HashSet<String>(java.util.Arrays.asList({expected_body}.split({split_regex})));\n"
            f"{indent}final java.util.Set<String> {actual_set} = new java.util.HashSet<String>(java.util.Arrays.asList({actual_body}.split({split_regex})));\n"
            f"{indent}assertEquals({expected_set}, {actual_set});\n"
        )
        generated_blocks.append((index, end_index, replacement))

    if not generated_blocks:
        return "", {"ok": False, "error_class": "no_reflection_to_string_assertion", "error": ""}

    revised_parts: list[str] = []
    cursor = 0
    for start_offset, end_offset, replacement in generated_blocks:
        revised_parts.append("".join(lines_keepends[cursor:start_offset]))
        revised_parts.append(replacement)
        cursor = end_offset + 1
    revised_parts.append("".join(lines_keepends[cursor:]))
    revised = "".join(revised_parts)
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_LIST_ORDER_INSENSITIVE",
            "target_file": rel_path,
            "guard": "reflection toString field entries are compared without field-order assumptions",
            "changed_assertions": len(generated_blocks),
        }
    )
    return patch, meta


def collection_iterable_expression(original: str, expression: str) -> str | None:
    expr = expression.strip()
    if not expr or "[]" in expr or re.search(r"\bnew\s+[^;]+\[", expr):
        return None
    if expression_declared_as_map(original, expr):
        return f"{expr}.entrySet()"
    if re.fullmatch(
        r"(?:java\.util\.)?(?:Arrays\.asList|Collections\.(?:singletonList|singleton)|List\.of)\s*\(.+\)",
        expr,
        flags=re.S,
    ) or re.fullmatch(r"(?:ImmutableList|Lists|CollUtil)\.(?:of|newArrayList)\s*\(.+\)", expr, flags=re.S):
        return expr
    if re.fullmatch(r"[A-Za-z_]\w*", expr):
        if re.search(
            rf"\b(?:java\.util\.)?(?:List|Set|Collection|Iterable|Map|HashMap|HashSet|LinkedHashMap|LinkedHashSet)\s*(?:<[^;=]+>)?\s+{re.escape(expr)}\b",
            original,
        ):
            return f"{expr}.entrySet()" if expression_declared_as_map(original, expr) else expr
    return None


def variable_iterable_expression(original: str, variable: str) -> str | None:
    name = variable.strip()
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return None
    primitive_types = {
        "boolean",
        "byte",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
    }
    array_pattern = re.compile(
        rf"\b(?:final\s+)?(?P<type>[A-Za-z_][\w.]*)\s*\[\]\s+{re.escape(name)}\b"
    )
    array_match = array_pattern.search(original)
    if array_match and array_match.group("type") not in primitive_types:
        return f"java.util.Arrays.asList({name})"
    return collection_iterable_expression(original, name)


def multiset_count_block(
    assertion_block: str,
    indent: str,
    expected_iterable: str,
    actual_iterable: str,
    suffix: int,
) -> str:
    expected_counts = f"stabilityOpsExpectedCounts{suffix}"
    actual_counts = f"stabilityOpsActualCounts{suffix}"
    expected_item = f"stabilityOpsExpectedItem{suffix}"
    actual_item = f"stabilityOpsActualItem{suffix}"
    return (
        f"{indent}final java.util.Map<Object, Integer> {expected_counts} = new java.util.HashMap<Object, Integer>();\n"
        f"{indent}for (Object {expected_item} : (java.lang.Iterable<?>) ({expected_iterable})) {{\n"
        f"{indent}    final Integer count = {expected_counts}.get({expected_item});\n"
        f"{indent}    {expected_counts}.put({expected_item}, Integer.valueOf(count == null ? 1 : count.intValue() + 1));\n"
        f"{indent}}}\n"
        f"{indent}final java.util.Map<Object, Integer> {actual_counts} = new java.util.HashMap<Object, Integer>();\n"
        f"{indent}for (Object {actual_item} : (java.lang.Iterable<?>) ({actual_iterable})) {{\n"
        f"{indent}    final Integer count = {actual_counts}.get({actual_item});\n"
        f"{indent}    {actual_counts}.put({actual_item}, Integer.valueOf(count == null ? 1 : count.intValue() + 1));\n"
        f"{indent}}}\n"
        f"{render_equality_assertion(assertion_block, indent, expected_counts, actual_counts)}\n"
    )


def synthesize_indexed_sequence_multiset_assertion(
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

    span_start, span_end = line_span_from_action(action)
    if not span_start or not span_end:
        return "", {"ok": False, "error_class": "missing_line_span", "error": ""}
    span_error = validate_line_span(span_start, span_end, method_start, method_end)
    if span_error:
        return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    selected_lines = lines_plain[int(span_start) - 1 : int(span_end)]
    assertion_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:Assert\.|Assertions\.)?)assertEquals\s*\(\s*"
        r"(?P<expected>.+?)\s*,\s*(?P<actual>[A-Za-z_]\w*)\s*\[\s*(?P<index>(?:\+\+[A-Za-z_]\w*|[A-Za-z_]\w*\+\+|\d+))\s*\]\s*\)\s*;\s*$"
    )
    matches = []
    for offset, line in enumerate(selected_lines):
        match = assertion_pattern.match(line)
        if match:
            matches.append((offset, match))
    if len(matches) < 2:
        return "", {"ok": False, "error_class": "no_indexed_sequence_assertion", "error": ""}

    actual_vars = {match.group("actual") for _, match in matches}
    if len(actual_vars) != 1:
        return "", {"ok": False, "error_class": "mixed_indexed_sequence_targets", "error": ""}
    actual_var = next(iter(actual_vars))
    actual_iterable = variable_iterable_expression(original, actual_var)
    if not actual_iterable:
        return "", {"ok": False, "error_class": "indexed_sequence_target_not_iterable", "error": actual_var}

    first_line = matches[0][1]
    indent = first_line.group("indent")
    expected_values = [match.group("expected").strip() for _, match in matches]
    expected_iterable = "java.util.Arrays.asList(new Object[] {" + ", ".join(expected_values) + "})"
    replacement = multiset_count_block(
        selected_lines[matches[0][0]],
        indent,
        expected_iterable,
        actual_iterable,
        int(span_start),
    )
    revised = "".join(lines_keepends[: int(span_start) - 1]) + replacement + "".join(lines_keepends[int(span_end) :])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_LIST_ORDER_INSENSITIVE",
            "target_file": rel_path,
            "guard": "the selected span contains multiple assertions over the same indexed array or collection",
            "materialized_as": "indexed_sequence_multiset_equality",
            "assertion_count": len(matches),
        }
    )
    return patch, meta


def synthesize_expected_collection_multiset_assertion(
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

    span_start, span_end = line_span_from_action(action)
    if not span_start or not span_end:
        span_start, span_end = method_start, method_end
    span_error = validate_line_span(span_start, span_end, method_start, method_end)
    if span_error:
        return "", {"ok": False, "error_class": "line_span_outside_target_method", "error": span_error}

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    index = int(span_start) - 1
    while index < int(span_end):
        line = lines_plain[index]
        if "assertEquals" not in line and "assertThat" not in line:
            index += 1
            continue
        end_index = index
        block = line
        while ");" not in block and end_index + 1 < int(method_end):
            end_index += 1
            block += "\n" + lines_plain[end_index]
        parsed = split_assert_equals_arguments(block) or split_assert_that_json_arguments(block)
        if not parsed:
            index = end_index + 1
            continue
        indent, expected, actual = parsed
        expected_iterable = collection_iterable_expression(original, expected)
        actual_iterable = collection_iterable_expression(original, actual) or variable_iterable_expression(original, actual)
        if not expected_iterable or not actual_iterable:
            index = end_index + 1
            continue
        replacement = multiset_count_block(
            block,
            indent,
            expected_iterable,
            actual_iterable,
            index + 1,
        )
        revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[end_index + 1 :])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": "ID_LIST_ORDER_INSENSITIVE",
                "target_file": rel_path,
                "guard": "expected and actual assertion operands are both visible collections",
                "materialized_as": "collection_multiset_equality",
            }
        )
        return patch, meta
    return "", {"ok": False, "error_class": "no_guarded_collection_equality", "error": ""}


def synthesize_unordered_collection_equality(
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

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_text = "\n".join(lines_plain[int(method_start) - 1 : int(method_end)])
    unordered_evidence = bool(
        re.search(
            r"\b(?:HashMap|HashSet|LinkedHashMap|LinkedHashSet)\b|\.entrySet\s*\(|\.keySet\s*\(|getDeclared(?:Fields|Methods|Constructors|Annotations)\s*\(",
            method_text,
        )
    )
    if not unordered_evidence:
        return "", {"ok": False, "error_class": "unordered_collection_source_not_visible", "error": ""}

    for index in range(int(method_start) - 1, int(method_end)):
        if "assertEquals" not in lines_plain[index] and "assertThat" not in lines_plain[index]:
            continue
        end_index = index
        block = lines_plain[index]
        while ");" not in block and end_index + 1 < int(method_end):
            end_index += 1
            block += "\n" + lines_plain[end_index]
        parsed = split_assert_equals_arguments(block) or split_assert_that_json_arguments(block)
        if not parsed:
            continue
        indent, expected, actual = parsed
        expected_iterable = collection_iterable_expression(original, expected)
        actual_iterable = collection_iterable_expression(original, actual)
        if not expected_iterable or not actual_iterable:
            continue
        replacement = multiset_count_block(
            block,
            indent,
            expected_iterable,
            actual_iterable,
            index + 1,
        )
        revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[end_index + 1 :])
        patch, meta = unified_diff_for_revised(original, revised, rel_path)
        meta.update(
            {
                "mode": "transform_to_unified_diff",
                "transform": "ID_LIST_ORDER_INSENSITIVE",
                "target_file": rel_path,
                "guard": "both assertion operands are iterable and an unordered collection source is visible",
                "materialized_as": "multiset_equality",
            }
        )
        return patch, meta
    return "", {"ok": False, "error_class": "no_guarded_collection_equality", "error": ""}


def synthesize_list_order_insensitive(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0
    collection_expr = r"(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*(?:\(\))?)*)"
    indexed_var_assignments: dict[str, str] = {}
    indexed_assignment_pattern = re.compile(
        r"^(?P<indent>\s*)(?:final\s+)?(?:[A-Za-z_][\w.<>?,\s\[\]]+\s+)?(?P<var>[A-Za-z_]\w*)\s*=\s*"
        rf"(?P<collection>{collection_expr})\.get\s*\(\s*\d+\s*\)\s*;\s*$"
    )
    indexed_get_pattern = re.compile(
        rf"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*(?P<expected>.+?)\s*,\s*(?P<collection>{collection_expr})\.get\s*\(\s*\d+\s*\)\s*\)\s*;\s*$"
    )
    expected_list_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*(?P<expected>(?:Arrays\.asList|java\.util\.Arrays\.asList|Lists\.newArrayList|CollUtil\.newArrayList|ImmutableList\.of|List\.of|java\.util\.List\.of)\s*\(.+\))\s*,\s*(?P<actual>[^;]+?)\s*\)\s*;\s*$"
    )
    assert_array_equals_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertArrayEquals\s*\(\s*(?P<expected>.+?)\s*,\s*(?P<actual>.+?)\s*\)\s*;\s*$"
    )
    assert_true_var_equals_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertTrue\s*\(\s*(?P<var>[A-Za-z_]\w*)\.equals\s*\(\s*(?P<expected>.+?)\s*\)\s*\)\s*;\s*$"
    )
    assert_true_direct_get_equals_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertTrue\s*\(\s*"
        rf"(?P<collection>{collection_expr})\.get\s*\(\s*\d+\s*\)\.equals\s*\(\s*(?P<expected>.+?)\s*\)\s*\)\s*;\s*$"
    )
    assert_equals_indexed_expected_pattern = re.compile(
        r"^(?P<indent>\s*)(?P<prefix>(?:\w+\.)?)assertEquals\s*\(\s*"
        rf"(?P<collection>{collection_expr})\.get\s*\(\s*\d+\s*\)\s*,\s*(?P<expected>.+?)\s*\)\s*;\s*$"
    )
    for line in lines:
        match = indexed_assignment_pattern.match(line)
        if match:
            indexed_var_assignments[match.group("var")] = match.group("collection")
            generated.append(line)
            continue
        match = indexed_get_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('collection')}.contains({match.group('expected')}));"
            )
            changed += 1
            continue
        match = assert_equals_indexed_expected_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('collection')}.contains({match.group('expected')}));"
            )
            changed += 1
            continue
        match = assert_true_direct_get_equals_pattern.match(line)
        if match:
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertTrue({match.group('collection')}.contains({match.group('expected')}));"
            )
            changed += 1
            continue
        match = assert_true_var_equals_pattern.match(line)
        if match:
            var = match.group("var")
            if var in indexed_var_assignments:
                generated.append(
                    f"{match.group('indent')}{match.group('prefix')}assertTrue({indexed_var_assignments[var]}.contains({match.group('expected')}));"
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
            parsed_array_assert = split_assert_call_two_args(line, "assertArrayEquals")
            if not parsed_array_assert:
                generated.append(line)
                continue
            _indent, expected_arg, actual_arg = parsed_array_assert
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertEquals(new java.util.HashSet(java.util.Arrays.asList({expected_arg})), new java.util.HashSet(java.util.Arrays.asList({actual_arg})));"
            )
            changed += 1
            continue
        generated.append(line)
    if changed == 0:
        return [], {"ok": False, "error_class": "no_order_sensitive_get_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_LIST_ORDER_INSENSITIVE"}


def expression_declared_as_map(original: str, expression: str) -> bool:
    name = expression.strip()
    if not re.fullmatch(r"[A-Za-z_]\w*", name):
        return False
    return bool(
        re.search(
            rf"\b(?:java\.util\.)?(?:Map|HashMap|LinkedHashMap|SortedMap|TreeMap)\s*<[^;=]+>\s+{re.escape(name)}\b",
            original,
        )
    )


def synthesize_assertj_order_insensitive(lines: list[str], original: str = "") -> tuple[list[str], dict[str, Any]]:
    generated: list[str] = []
    changed = 0

    def unordered_collection_expr(expr: str) -> str:
        return f"new java.util.HashSet<Object>((java.util.Collection<?>) ({expr}))"

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
                f"{match.group('indent')}{match.group('prefix')}assertThat({unordered_collection_expr(actual)}).isEqualTo({unordered_collection_expr(expected)});"
            )
            changed += 1
            continue
        match = contains_exactly_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            items = match.group("items").strip()
            method = "containsOnly" if expression_declared_as_map(original, actual) else "containsExactlyInAnyOrder"
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertThat({actual}).{method}({items});"
            )
            changed += 1
            continue
        match = contains_elements_pattern.match(line)
        if match:
            actual = match.group("actual").strip()
            expected = match.group("expected").strip()
            generated.append(
                f"{match.group('indent')}{match.group('prefix')}assertThat({unordered_collection_expr(actual)}).isEqualTo({unordered_collection_expr(expected)});"
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
    hamcrest_pattern = re.compile(
        r"^(?P<indent>\s*)assertThat\s*\(\s*(?P<actual>.+?)\s*,\s*(?:is|equalTo)\s*\(\s*\"(?P<expected>[^\"]*[&=][^\"]*)\"\s*\)\s*\)\s*;\s*$"
    )
    for line in lines:
        match = pattern.match(line)
        if not match:
            match = hamcrest_pattern.match(line)
        if not match:
            generated.append(line)
            continue
        expected = match.group("expected").strip()
        actual = match.group("actual").strip()
        if not expected or "&" not in expected:
            return [], {"ok": False, "error_class": "query_expected_not_compound", "error": expected}
        indent = match.group("indent")
        suffix = changed + 1
        actual_var = f"stabilityOpsActualQuery{suffix}"
        expected_params_var = f"stabilityOpsExpectedParams{suffix}"
        actual_params_var = f"stabilityOpsActualParams{suffix}"
        generated.append(f"{indent}final String {actual_var} = String.valueOf({actual});")
        expected_query = expected
        actual_query = actual_var
        if "?" in expected:
            expected_base, expected_query = expected.split("?", 1)
            generated.append(f"{indent}{assertion_truth_call(line)}({actual_var}.indexOf(\"?\") >= 0);")
            generated.append(
                render_equality_assertion(line, indent, java_string_literal(expected_base), f'{actual_var}.substring(0, {actual_var}.indexOf("?"))')
            )
            actual_query = f'{actual_var}.substring({actual_var}.indexOf("?") + 1)'
        generated.extend(
            [
                f"{indent}final java.util.List<String> {expected_params_var} = new java.util.ArrayList<String>(java.util.Arrays.asList({java_string_literal(expected_query)}.split(\"&\", -1)));",
                f"{indent}final java.util.List<String> {actual_params_var} = new java.util.ArrayList<String>(java.util.Arrays.asList({actual_query}.split(\"&\", -1)));",
                f"{indent}java.util.Collections.sort({expected_params_var});",
                f"{indent}java.util.Collections.sort({actual_params_var});",
                render_equality_assertion(line, indent, expected_params_var, actual_params_var),
            ]
        )
        changed += 1
    if not changed:
        return [], {"ok": False, "error_class": "no_query_string_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT", "changed_assertions": changed}


def synthesize_query_expression_order_insensitive(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        start_line, _ = line_span_from_action(action)
        method_start, method_end = enclosing_method_bounds_for_line(original, start_line)
    span_start, span_end = line_span_from_action(action)
    if not span_start or not span_end:
        return "", {"ok": False, "error_class": "missing_query_assertion_span", "error": ""}

    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    block = "".join(lines_plain[span_start - 1 : span_end])
    if len(re.findall(r"\bassertEquals\s*\(", block)) != 1:
        return "", {"ok": False, "error_class": "ambiguous_query_assertion_span", "error": ""}
    parsed = split_assert_equals_arguments(block)
    if not parsed:
        return "", {"ok": False, "error_class": "unsupported_query_assertion_line", "error": block.strip()}
    indent, expected, actual = parsed
    method_text = "\n".join(lines_plain[int(method_start or span_start) - 1 : int(method_end or span_end)])
    if "?" not in method_text and "buildUrl" not in method_text and "suffix" not in method_text:
        return "", {"ok": False, "error_class": "query_context_not_visible", "error": ""}
    if any(token in expected + actual for token in ["delete", "remove", "clear"]):
        return "", {"ok": False, "error_class": "unsafe_query_expression", "error": ""}

    expected_var = f"stabilityOpsExpectedUrl{span_start}"
    actual_var = f"stabilityOpsActualUrl{span_start}"
    equality_base = render_equality_assertion(
        block,
        indent,
        f'{expected_var}.substring(0, {expected_var}.indexOf("?"))',
        f'{actual_var}.substring(0, {actual_var}.indexOf("?"))',
    )
    equality_params = render_equality_assertion(
        block,
        indent,
        f"stabilityOpsExpectedParams{span_start}",
        f"stabilityOpsActualParams{span_start}",
    )
    replacement = (
        f"{indent}final String {expected_var} = String.valueOf({expected});\n"
        f"{indent}final String {actual_var} = String.valueOf({actual});\n"
        f"{indent}{assertion_truth_call(block)}({expected_var}.indexOf(\"?\") >= 0 && {actual_var}.indexOf(\"?\") >= 0);\n"
        f"{equality_base}\n"
        f"{indent}final java.util.List<String> stabilityOpsExpectedParams{span_start} = new java.util.ArrayList<String>(java.util.Arrays.asList({expected_var}.substring({expected_var}.indexOf(\"?\") + 1).split(\"&\", -1)));\n"
        f"{indent}final java.util.List<String> stabilityOpsActualParams{span_start} = new java.util.ArrayList<String>(java.util.Arrays.asList({actual_var}.substring({actual_var}.indexOf(\"?\") + 1).split(\"&\", -1)));\n"
        f"{indent}java.util.Collections.sort(stabilityOpsExpectedParams{span_start});\n"
        f"{indent}java.util.Collections.sort(stabilityOpsActualParams{span_start});\n"
        f"{equality_params}\n"
    )
    revised = "".join(lines_keepends[: span_start - 1]) + replacement + "".join(lines_keepends[span_end:])
    patch, meta = unified_diff_for_revised(original, revised, rel_path)
    meta.update(
        {
            "mode": "transform_to_unified_diff",
            "transform": "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT",
            "target_file": rel_path,
            "guard": "query URL equality is split into base equality and unordered query-parameter equality",
        }
    )
    return patch, meta


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


def split_assert_call_two_args(block: str, method_name: str) -> tuple[str, str, str] | None:
    start = block.find(method_name)
    if start < 0:
        return None
    open_index = block.find("(", start)
    close_index = block.rfind(");")
    if open_index < 0 or close_index < open_index:
        return None
    split = split_top_level_two_args(block[open_index + 1 : close_index])
    if not split:
        return None
    indent_match = re.match(r"^(\s*)", block)
    return indent_match.group(1) if indent_match else "", split[0], split[1]


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


def split_assertj_is_equal_to_arguments(block: str) -> tuple[str, str, str] | None:
    match = re.match(
        r"^(?P<indent>\s*)(?:Assertions\.)?assertThat\s*\(\s*(?P<actual>.+?)\s*\)\s*"
        r"\.\s*isEqualTo\s*\(\s*(?P<expected>.+?)\s*\)\s*;\s*$",
        block.strip() and block or "",
        flags=re.S,
    )
    if not match:
        return None
    return match.group("indent"), match.group("expected").strip(), match.group("actual").strip()


def split_java_equals_json_arguments(block: str) -> tuple[str, str, str] | None:
    text = block.strip() and block or ""
    patterns = [
        re.compile(
            r"^(?P<indent>\s*)assert\s+(?P<left>.+?)\.equals\s*\(\s*(?P<right>.+?)\s*\)\s*;\s*$",
            flags=re.S,
        ),
        re.compile(
            r"^(?P<indent>\s*)(?:[A-Za-z_][\w.]*\.)?assertTrue\s*\(\s*(?:\"(?:\\.|[^\"\\])*\"\s*,\s*)?"
            r"(?P<left>.+?)\.equals\s*\(\s*(?P<right>.+?)\s*\)\s*\)\s*;\s*$",
            flags=re.S,
        ),
    ]
    for pattern in patterns:
        match = pattern.match(text)
        if not match:
            continue
        left = match.group("left").strip()
        right = match.group("right").strip()
        if ";" in left or ";" in right:
            return None
        left_literal = java_string_literal_value(left)
        right_literal = java_string_literal_value(right)
        if left_literal is not None and right_literal is None:
            return match.group("indent"), left, right
        return match.group("indent"), right, left
    return None


def assert_equals_prefix(block: str) -> str:
    match = re.match(r"^(?P<indent>\s*)(?P<prefix>(?:[A-Za-z_][\w.]*\.)?)assertEquals\s*\(", block.strip() and block or "")
    return match.group("prefix") if match else ""


def render_equality_assertion(block: str, indent: str, expected: str, actual: str) -> str:
    """Render equality with the assertion mechanism already used by the test."""
    if split_java_equals_json_arguments(block):
        return f"{indent}assert {expected}.equals({actual});"
    if split_groovy_assert_that_json_arguments(block):
        return f"{indent}assert {actual} == {expected}"
    if split_assertj_is_equal_to_arguments(block):
        prefix = "Assertions." if re.search(r"\bAssertions\.assertThat\s*\(", block) else ""
        return f"{indent}{prefix}assertThat({actual}).isEqualTo({expected});"
    if split_assert_that_json_arguments(block):
        assert_prefix_match = re.search(r"(?P<prefix>(?:[A-Za-z_][\w.]*\.)?)assertThat\s*\(", block)
        assert_prefix = assert_prefix_match.group("prefix") if assert_prefix_match else ""
        matcher = "is" if re.search(r"\bis\s*\(", block) else "equalTo"
        matcher_prefix_match = re.search(
            rf"(?P<prefix>(?:[A-Za-z_][\w.]*\.)?){matcher}\s*\(", block
        )
        matcher_prefix = matcher_prefix_match.group("prefix") if matcher_prefix_match else ""
        return f"{indent}{assert_prefix}assertThat({actual}, {matcher_prefix}{matcher}({expected}));"
    return f"{indent}{assert_equals_prefix(block)}assertEquals({expected}, {actual});"


def assertion_truth_call(block: str) -> str:
    """Return the assertTrue callee matching an existing assertion statement."""
    match = re.search(r"(?P<prefix>(?:[A-Za-z_][\w.]*\.)?)assert(?:Equals|True)\s*\(", block)
    return f"{match.group('prefix')}assertTrue" if match else "assertTrue"


def java_type_name(original: str, fqcn: str) -> str:
    simple = fqcn.rsplit(".", 1)[-1]
    if re.search(rf"^\s*import\s+{re.escape(fqcn)}\s*;", original, flags=re.M):
        return simple
    return fqcn


def reflection_comparator(original: str, member_kind: str) -> str:
    fqcn = {
        "METHOD": "java.lang.reflect.Method",
        "FIELD": "java.lang.reflect.Field",
        "CONSTRUCTOR": "java.lang.reflect.Constructor",
    }[member_kind]
    member_type = java_type_name(original, fqcn)
    if member_kind == "METHOD":
        left_key = "left.getName() + java.util.Arrays.toString(left.getParameterTypes())"
        right_key = "right.getName() + java.util.Arrays.toString(right.getParameterTypes())"
    elif member_kind == "CONSTRUCTOR":
        left_key = "java.util.Arrays.toString(left.getParameterTypes())"
        right_key = "java.util.Arrays.toString(right.getParameterTypes())"
    else:
        left_key = "left.getName()"
        right_key = "right.getName()"
    return (
        f"new java.util.Comparator<{member_type}>() {{ public int compare({member_type} left, "
        f"{member_type} right) {{ return ({left_key}).compareTo({right_key}); }} }}"
    )


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
        parsed = json.loads(literal)
    except json.JSONDecodeError:
        return False, "json_string_literal_not_strict_json"
    if not isinstance(parsed, (dict, list)):
        return False, "json_literal_not_object_or_array"
    return True, ""


def json_semantic_pair_allowed(expected: str, actual: str) -> tuple[bool, str]:
    expected_literal = java_string_literal_value(expected)
    actual_literal = java_string_literal_value(actual)
    literal_values = [value for value in (expected_literal, actual_literal) if value is not None]
    for value in literal_values:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False, "json_string_literal_not_strict_json"
        if not isinstance(parsed, (dict, list)):
            return False, "json_literal_not_object_or_array"
    if any(value is not None for value in (expected_literal, actual_literal)):
        return True, ""
    literal_fragments = []
    for expr in (expected, actual):
        for literal in re.findall(r'"(?:\\.|[^"\\])*"', expr, flags=re.S):
            value = java_string_literal_value(literal)
            if value is not None:
                literal_fragments.append(value.strip())
    has_json_fragment = any(fragment.startswith("{") or fragment.startswith("[") for fragment in literal_fragments)
    json_tokens = (
        "json",
        "Json",
        "JSON",
        "serialized",
        "Serialized",
        "payload",
        "Payload",
        "toJson",
        "toJSON",
        "JsonUtil",
        "JsonUtils",
        "JSONUtil",
        "GsonUtils",
        "GSON.",
        "Gson",
        "ObjectMapper",
        "writeValueAsString",
        "JSON.toJSONString",
        "JSONPath",
        "JSONObject",
        "JSONArray",
    )
    if any(token in expected for token in json_tokens) or any(token in actual for token in json_tokens):
        return True, ""
    if has_json_fragment and re.search(r"\b(?:toString|String|serialized|payload|body|content)\b", expected + " " + actual):
        return True, ""
    return False, "json_expression_not_visibly_json"


def split_statement_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    depth = 0
    in_string = False
    in_char = False
    escaped = False
    for line in lines:
        current.append(line)
        for ch in line:
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
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(depth - 1, 0)
        if ";" in line and depth == 0 and not in_string and not in_char:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def parse_json_assertion_block(block: str) -> tuple[str, str, str] | None:
    parsed = split_assert_equals_arguments(block)
    if not parsed:
        parsed = split_assert_that_json_arguments(block)
        if not parsed:
            parsed = split_groovy_assert_that_json_arguments(block)
            if not parsed:
                parsed = split_assertj_is_equal_to_arguments(block)
                if not parsed:
                    parsed = split_java_equals_json_arguments(block)
    return parsed


def sort_json_object_fragment(value: str) -> str | None:
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    fragment = value[start : end + 1]
    try:
        parsed = json.loads(fragment)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    sorted_fragment = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value[:start] + sorted_fragment + value[end + 1 :]


def json_api_parse_template(original: str, sample: dict[str, str]) -> tuple[str | None, str]:
    if "com.alibaba.fastjson2.JSON" in original:
        return "com.alibaba.fastjson2.JSON.parse({expr})", ""
    if "com.alibaba.fastjson.JSON" in original:
        return "com.alibaba.fastjson.JSON.parse({expr})", ""
    if re.search(r"import\s+com\.alibaba\.fastjson2\.JSON\s*;", original):
        return "com.alibaba.fastjson2.JSON.parse({expr})", ""
    if re.search(r"import\s+com\.alibaba\.fastjson\.JSON\s*;", original):
        return "com.alibaba.fastjson.JSON.parse({expr})", ""
    json_api_v2 = False
    json_api_v1 = False
    for build_file in target_module_build_files(sample):
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
                    parsed = split_assertj_is_equal_to_arguments(line)
                    if not parsed:
                        parsed = split_java_equals_json_arguments(line)
                        if not parsed:
                            generated.append(line)
                            continue
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not expected_ok or not actual_ok or not pair_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error or pair_error,
                "error": expected if not expected_ok else actual,
            }
        generated.append(
            render_equality_assertion(
                line,
                indent,
                wrapper_template.format(expr=expected),
                wrapper_template.format(expr=actual),
            )
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        if len(re.findall(r"\b(?:assertEquals|assertThat|assertTrue)\s*\(", block)) > 1:
            generated_blocks: list[str] = []
            for statement_lines in split_statement_blocks(lines):
                statement = "\n".join(statement_lines)
                parsed_statement = parse_json_assertion_block(statement)
                if not parsed_statement:
                    generated_blocks.extend(statement_lines)
                    continue
                indent, expected, actual = parsed_statement
                expected_ok, expected_error = json_readtree_expr_allowed(expected)
                actual_ok, actual_error = json_readtree_expr_allowed(actual)
                pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
                if expected_ok and actual_ok and pair_ok:
                    generated_blocks.append(
                        render_equality_assertion(
                            statement,
                            indent,
                            wrapper_template.format(expr=expected),
                            wrapper_template.format(expr=actual),
                        )
                    )
                    changed += 1
                else:
                    generated_blocks.extend(statement_lines)
            if changed > 0:
                return generated_blocks, {"ok": True, "transform": transform_name, "changed_assertions": changed}
            return [], {"ok": False, "error_class": "multiple_assertions_in_unparsed_json_span", "error": ""}
        parsed = parse_json_assertion_block(block)
        if not parsed:
            return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not expected_ok or not actual_ok or not pair_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error or pair_error,
                "error": expected if not expected_ok else actual,
            }
        generated = [
            render_equality_assertion(
                block,
                indent,
                wrapper_template.format(expr=expected),
                wrapper_template.format(expr=actual),
            )
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
                parsed = split_java_equals_json_arguments(line)
                if not parsed:
                    generated.append(line)
                    continue
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not expected_ok or not actual_ok or not pair_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error or pair_error,
                "error": expected if not expected_ok else actual,
            }
        equality = render_equality_assertion(
            line,
            indent + "    ",
            wrapper_template.format(expr=expected),
            wrapper_template.format(expr=actual),
        ).lstrip()
        generated.extend(
            [
                f"{indent}try {{",
                f"{indent}    {equality}",
                f"{indent}}} catch (Exception e) {{",
                f"{indent}    throw new AssertionError(e);",
                f"{indent}}}",
            ]
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        if len(re.findall(r"\b(?:assertEquals|assertThat|assertTrue)\s*\(", block)) > 1:
            generated_blocks: list[str] = []
            for statement_lines in split_statement_blocks(lines):
                statement = "\n".join(statement_lines)
                parsed_statement = parse_json_assertion_block(statement)
                if not parsed_statement:
                    generated_blocks.extend(statement_lines)
                    continue
                indent, expected, actual = parsed_statement
                expected_ok, expected_error = json_readtree_expr_allowed(expected)
                actual_ok, actual_error = json_readtree_expr_allowed(actual)
                pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
                if expected_ok and actual_ok and pair_ok:
                    equality = render_equality_assertion(
                        statement,
                        indent + "    ",
                        wrapper_template.format(expr=expected),
                        wrapper_template.format(expr=actual),
                    ).lstrip()
                    generated_blocks.extend(
                        [
                            f"{indent}try {{",
                            f"{indent}    {equality}",
                            f"{indent}}} catch (Exception e) {{",
                            f"{indent}    throw new AssertionError(e);",
                            f"{indent}}}",
                        ]
                    )
                    changed += 1
                else:
                    generated_blocks.extend(statement_lines)
            if changed > 0:
                return generated_blocks, {"ok": True, "transform": transform_name, "changed_assertions": changed}
            return [], {"ok": False, "error_class": "multiple_assertions_in_unparsed_json_span", "error": ""}
        parsed = parse_json_assertion_block(block)
        if not parsed:
            return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        expected_ok, expected_error = json_readtree_expr_allowed(expected)
        actual_ok, actual_error = json_readtree_expr_allowed(actual)
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not expected_ok or not actual_ok or not pair_ok:
            return [], {
                "ok": False,
                "error_class": expected_error or actual_error or pair_error,
                "error": expected if not expected_ok else actual,
            }
        equality = render_equality_assertion(
            block,
            indent + "    ",
            wrapper_template.format(expr=expected),
            wrapper_template.format(expr=actual),
        ).lstrip()
        generated = [
            f"{indent}try {{",
            f"{indent}    {equality}",
            f"{indent}}} catch (Exception e) {{",
            f"{indent}    throw new AssertionError(e);",
            f"{indent}}}",
        ]
        changed = 1
    return generated, {"ok": True, "transform": transform_name, "changed_assertions": changed}


def synthesize_spring_content_json_string_assert(lines: list[str]) -> tuple[list[str], dict[str, Any]] | None:
    return None


def synthesize_json_api_parse_assert(lines: list[str], wrapper_template: str) -> tuple[list[str], dict[str, Any]]:
    spring_content_json = synthesize_spring_content_json_string_assert(lines)
    if spring_content_json:
        return spring_content_json
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
                parsed = split_java_equals_json_arguments(line)
                if not parsed:
                    generated.append(line)
                    continue
        indent, expected, actual = parsed
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not pair_ok:
            return [], {"ok": False, "error_class": pair_error, "error": expected}
        generated.append(
            render_equality_assertion(
                line,
                indent,
                wrapper_template.format(expr=expected),
                wrapper_template.format(expr=actual),
            )
        )
        changed += 1
    if changed == 0:
        block = "\n".join(lines)
        if len(re.findall(r"\b(?:assertEquals|assertThat|assertTrue)\s*\(", block)) > 1:
            return [], {"ok": False, "error_class": "multiple_assertions_in_unparsed_json_span", "error": ""}
        parsed = split_assert_equals_arguments(block)
        if not parsed:
            parsed = split_assert_that_json_arguments(block)
            if not parsed:
                parsed = split_java_equals_json_arguments(block)
                if not parsed:
                    return [], {"ok": False, "error_class": "no_assert_equals_in_span", "error": ""}
        indent, expected, actual = parsed
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        if not pair_ok:
            return [], {"ok": False, "error_class": pair_error, "error": expected}
        generated = [
            render_equality_assertion(
                block,
                indent,
                wrapper_template.format(expr=expected),
                wrapper_template.format(expr=actual),
            )
        ]
        changed = 1
    return generated, {"ok": True, "transform": "ID_JSON_API_PARSE_ASSERT", "changed_assertions": changed}


def synthesize_org_json_object_assert(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    """Compare JSON objects with the project-visible org.json semantic API."""

    def rewrite(statement: str) -> list[str] | None:
        parsed = parse_json_assertion_block(statement)
        if not parsed:
            return None
        indent, expected, actual = parsed
        pair_ok, pair_error = json_semantic_pair_allowed(expected, actual)
        fragments = [
            value.strip()
            for literal in re.findall(r'"(?:\\.|[^"\\])*"', expected + " " + actual, flags=re.S)
            if (value := java_string_literal_value(literal)) is not None
        ]
        if not pair_ok or not any(value.startswith("{") for value in fragments):
            return None
        similar = (
            f"new org.json.JSONObject({expected}).similar("
            f"new org.json.JSONObject({actual}))"
        )
        equality = render_equality_assertion(statement, indent + "    ", "true", similar).lstrip()
        return [
            f"{indent}try {{",
            f"{indent}    {equality}",
            f"{indent}}} catch (Exception e) {{",
            f"{indent}    throw new AssertionError(e);",
            f"{indent}}}",
        ]

    block = "\n".join(lines)
    rewritten = rewrite(block)
    if rewritten:
        return rewritten, {"ok": True, "transform": "ID_JSON_API_PARSE_ASSERT", "changed_assertions": 1}

    generated: list[str] = []
    changed = 0
    for statement_lines in split_statement_blocks(lines):
        statement_rewrite = rewrite("\n".join(statement_lines))
        if statement_rewrite:
            generated.extend(statement_rewrite)
            changed += 1
        else:
            generated.extend(statement_lines)
    if not changed:
        return [], {"ok": False, "error_class": "no_org_json_object_assertion", "error": ""}
    return generated, {"ok": True, "transform": "ID_JSON_API_PARSE_ASSERT", "changed_assertions": changed}


def expression_looks_jsonish(expr: str) -> bool:
    text = str(expr or "").strip()
    literal = java_string_literal_value(text)
    if literal is not None:
        stripped = literal.strip()
        return stripped.startswith("{") or stripped.startswith("[")
    return bool(
        any(token in text for token in ["JSON.toJSONString", "JSONPath", "JSONObject", "JSONArray"])
        or re.search(r"\b[A-Za-z_]\w*(?:Json|JSON|json)[A-Za-z_]*\b", text)
    )


def synthesize_json_api_method_json_asserts(
    original: str,
    sample: dict[str, str],
    rel_path: str,
) -> tuple[str, dict[str, Any]] | None:
    wrapper_template, wrapper_error = json_api_parse_template(original, sample)
    use_jackson = not wrapper_template and project_mentions_jackson(sample, original)
    use_org_json = not wrapper_template and not use_jackson and source_mentions_org_json_object(original)
    if not wrapper_template and not use_jackson and not use_org_json:
        return None
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return None
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    changed = 0
    method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
    prefix_lines: list[str] = []
    suffix_lines: list[str] = []
    body_lines = list(method_lines)
    for index, line in enumerate(method_lines[:8]):
        if "{" in line:
            prefix_lines = method_lines[: index + 1]
            body_lines = method_lines[index + 1 :]
            break
    while body_lines and not body_lines[-1].strip():
        suffix_lines.insert(0, body_lines.pop())
    if body_lines and body_lines[-1].strip() == "}":
        suffix_lines.insert(0, body_lines.pop())
    revised_method: list[str] = []
    revised_method.extend(prefix_lines)
    for statement_lines in split_statement_blocks(body_lines):
        statement = "\n".join(statement_lines)
        parsed = parse_json_assertion_block(statement)
        if not parsed:
            revised_method.extend(statement_lines)
            continue
        indent, expected, actual = parsed
        pair_ok, _pair_error = json_semantic_pair_allowed(expected, actual)
        if not pair_ok or not (expression_looks_jsonish(expected) or expression_looks_jsonish(actual)):
            revised_method.extend(statement_lines)
            continue
        if use_org_json:
            generated, meta = synthesize_org_json_object_assert(statement_lines)
        elif use_jackson:
            generated, meta = synthesize_json_semantic_assert_try_catch(
                statement_lines,
                "new com.fasterxml.jackson.databind.ObjectMapper().readTree({expr})",
                "ID_JSON_API_METHOD_ASSERTS",
            )
        else:
            generated, meta = synthesize_json_api_parse_assert(statement_lines, wrapper_template or "")
        if meta.get("ok"):
            revised_method.extend(generated)
            changed += int(meta.get("changed_assertions") or 1)
        else:
            revised_method.extend(statement_lines)
    revised_method.extend(suffix_lines)
    if not changed:
        return None
    revised_method_text = "\n".join(revised_method)
    if int(method_end) < len(lines_plain) or original.endswith("\n"):
        revised_method_text += "\n"
    revised = (
        "".join(lines_keepends[: int(method_start) - 1])
        + revised_method_text
        + "".join(lines_keepends[int(method_end) :])
    )
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
    fallback_patch, fallback_meta = synthesize_reflection_assertion_order_insensitive(original, sample, action, rel_path)
    if fallback_patch.strip() and fallback_meta.get("ok"):
        return fallback_patch, fallback_meta
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

    member_kind = {
        "METHOD_NAME": "METHOD",
        "FIELD_NAME": "FIELD",
        "CONSTRUCTOR_NAME": "CONSTRUCTOR",
    }[sort_key]
    statement = f"java.util.Arrays.sort({array_variable}, {reflection_comparator(original, member_kind)});"
    action = dict(action)
    action["insert_after_line"] = insert_after
    patch, meta = synthesize_insert_statement_after_line(original, sample, action, rel_path, "ID_SORT_REFLECTION_RESULTS", statement)
    if meta.get("ok"):
        meta.update({"array_variable": array_variable, "sort_key": sort_key})
    return patch, meta


def split_named_assert_arguments(block: str, assert_name: str) -> tuple[str, str, str] | None:
    start = block.find(assert_name)
    if start < 0:
        return None
    open_index = block.find("(", start)
    close_index = block.rfind(");")
    if open_index < 0 or close_index < open_index:
        return None
    split = split_top_level_two_args(block[open_index + 1 : close_index])
    if not split:
        return None
    indent_match = re.match(r"^(\s*)", block)
    return indent_match.group(1) if indent_match else "", split[0].strip(), split[1].strip()


def synthesize_reflection_assertion_order_insensitive(
    original: str,
    sample: dict[str, str],
    action: dict[str, Any],
    rel_path: str,
) -> tuple[str, dict[str, Any]]:
    method_start, method_end = method_bounds_for_sample(original, sample)
    if not method_start or not method_end:
        return "", {"ok": False, "error_class": "missing_target_method_bounds", "error": ""}
    lines_keepends = original.splitlines(keepends=True)
    lines_plain = original.splitlines()
    method_original = "".join(lines_keepends[int(method_start) - 1 : int(method_end)])

    reflection_string_pattern = re.compile(
        r"(?P<indent>^[ \t]*)assertEquals\s*\(\s*\"(?P<expected>[^\"]*,[^\"]*)\"\s*,\s*"
        r"(?P<actual>new\s+ReflectionToStringBuilder[\s\S]+?\.build\s*\(\s*\))\s*\)\s*;",
        re.M,
    )
    reflection_string_match = reflection_string_pattern.search(method_original)
    if reflection_string_match:
        expected_body = reflection_string_match.group("expected").strip()
        if expected_body.startswith("[") and expected_body.endswith("]"):
            expected_body = expected_body[1:-1]
        expected_items = [item.strip() for item in expected_body.split(",") if item.strip()]
        if len(expected_items) >= 2:
            expected_array = "new String[] {" + ", ".join(java_string_literal(item) for item in expected_items) + "}"
            indent = reflection_string_match.group("indent")
            actual = " ".join(reflection_string_match.group("actual").split())
            replacement = render_equality_assertion(
                reflection_string_match.group(0),
                indent,
                f"new java.util.HashSet(java.util.Arrays.asList({expected_array}))",
                f"new java.util.HashSet(java.util.Arrays.asList({actual}.replaceAll(\"^\\\\[|\\\\]$\", \"\").split(\",\")))",
            )
            revised_method = (
                method_original[: reflection_string_match.start()]
                + replacement
                + method_original[reflection_string_match.end() :]
            )
            revised = (
                "".join(lines_keepends[: int(method_start) - 1])
                + revised_method
                + "".join(lines_keepends[int(method_end) :])
            )
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_SORT_REFLECTION_RESULTS",
                    "target_file": rel_path,
                    "guard": "reflection toString assertion compared as comma-delimited unordered entries",
                }
            )
            return patch, meta

    for index in range(int(method_start) - 1, int(method_end)):
        line = lines_plain[index]
        if "assertArrayEquals" in line and ("FieldUtils.getFieldsWithAnnotation" in line or "getDeclaredFields" in line):
            parsed = split_named_assert_arguments(line, "assertArrayEquals")
            if not parsed:
                continue
            indent, expected, actual = parsed
            if not re.match(r"^[A-Za-z_]\w*$", expected):
                continue
            actual_var = f"{expected}ActualSorted"
            replacement = (
                f"{indent}final java.lang.reflect.Field[] {actual_var} = {actual};\n"
                f"{indent}java.util.Arrays.sort({expected}, {reflection_comparator(original, 'FIELD')});\n"
                f"{indent}java.util.Arrays.sort({actual_var}, {reflection_comparator(original, 'FIELD')});\n"
                f"{indent}assertArrayEquals({expected}, {actual_var});\n"
            )
            revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[index + 1 :])
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_SORT_REFLECTION_RESULTS",
                    "target_file": rel_path,
                    "guard": "reflection field array assertion sorted by field name",
                }
            )
            return patch, meta

        if "assertEquals" in line and ("ReflectionToStringBuilder" in line or "ToStringBuilder" in line):
            parsed = split_assert_equals_arguments(line)
            if not parsed:
                continue
            indent, expected, actual = parsed
            expected_literal = java_string_literal_value(expected)
            if expected_literal is None or "," not in expected_literal:
                continue
            expected_body = expected_literal.strip()
            if expected_body.startswith("[") and expected_body.endswith("]"):
                expected_body = expected_body[1:-1]
            expected_items = [item.strip() for item in expected_body.split(",") if item.strip()]
            if len(expected_items) < 2:
                continue
            expected_array = "new String[] {" + ", ".join(java_string_literal(item) for item in expected_items) + "}"
            replacement = render_equality_assertion(
                line,
                indent,
                f"new java.util.HashSet(java.util.Arrays.asList({expected_array}))",
                f"new java.util.HashSet(java.util.Arrays.asList({actual}.replaceAll(\"^\\\\[|\\\\]$\", \"\").split(\",\")))",
            ) + "\n"
            revised = "".join(lines_keepends[:index]) + replacement + "".join(lines_keepends[index + 1 :])
            patch, meta = unified_diff_for_revised(original, revised, rel_path)
            meta.update(
                {
                    "mode": "transform_to_unified_diff",
                    "transform": "ID_SORT_REFLECTION_RESULTS",
                    "target_file": rel_path,
                    "guard": "reflection toString assertion compared as comma-delimited unordered entries",
                }
            )
            return patch, meta

    return "", {"ok": False, "error_class": "inapplicable_reflection_sort", "error": str(action.get("sort_key") or "")}


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
        "OD_VIC_DATABASE_TABLE_CLEANUP",
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
        "ID_STABLE_COLLECTION_CONSTRUCTION": lambda: synthesize_stable_collection_construction(
            original, sample, action, rel_path
        ),
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
            return "", {"ok": False, "error_class": "inapplicable_database_fixture_setup", "error": "database fixture helper not visible"}
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

    if transform == "OD_VIC_SUBTYPE_REGISTRY_RESTORE_BEFORE":
        subtype_class = str(action.get("subtype_class") or "TestChecker").strip()
        type_expr = str(action.get("type_expr") or f"{subtype_class}.TYPE").strip()
        factory_expr = str(action.get("factory_expr") or "HealthCheckerFactory").strip()
        if not all(re.match(r"^[A-Za-z_][\w.]*$", item) for item in [subtype_class, type_expr, factory_expr]):
            return "", {"ok": False, "error_class": "unsafe_register_subtype_parameters", "error": ""}
        if subtype_class not in original or factory_expr not in original:
            return "", {"ok": False, "error_class": "inapplicable_register_subtype", "error": ""}
        statement = f"{factory_expr}.registerSubType({subtype_class}.class, {type_expr});"
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

    if transform == "OD_VIC_DATABASE_TABLE_CLEANUP":
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
        stable_patch, stable_meta = synthesize_stable_collection_construction(
            original, sample, action, rel_path
        )
        if stable_patch.strip() and stable_meta.get("ok"):
            stable_meta["transform"] = "ID_LIST_ORDER_INSENSITIVE"
            stable_meta["materialized_as"] = "stable_collection_construction"
            return stable_patch, stable_meta
        new_lines, meta = synthesize_list_order_insensitive(selected_lines)
        if not meta.get("ok"):
            patch, full_meta = synthesize_indexed_sequence_multiset_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_expected_collection_multiset_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_contains_fragments_order_insensitive(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_iterator_next_membership_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_multiline_string_order_insensitive_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_reflection_to_string_order_insensitive_assertion(
                original, sample, action, rel_path
            )
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_map_to_string_order_insensitive_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_unordered_collection_equality(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_indexed_list_sort_before_assertions(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_and_clause_order_insensitive_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
            patch, full_meta = synthesize_stable_collection_construction(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                full_meta["transform"] = "ID_LIST_ORDER_INSENSITIVE"
                full_meta["materialized_as"] = "stable_collection_construction"
                return patch, full_meta
    elif transform == "ID_ASSERTJ_LIST_ORDER_INSENSITIVE":
        new_lines, meta = synthesize_assertj_order_insensitive(selected_lines, original)
        if not meta.get("ok"):
            patch, full_meta = synthesize_map_to_string_order_insensitive_assertion(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                full_meta["transform"] = transform
                full_meta["materialized_as"] = "unordered_bracket_entry_assertion"
                return patch, full_meta
    elif transform == "ID_QUERY_STRING_ORDER_INSENSITIVE_ASSERT":
        new_lines, meta = synthesize_query_string_order_insensitive(selected_lines)
        if not meta.get("ok"):
            patch, full_meta = synthesize_query_expression_order_insensitive(original, sample, action, rel_path)
            if patch.strip() and full_meta.get("ok"):
                return patch, full_meta
    elif transform == "ID_JSON_READTREE_ASSERT":
        method_lines = lines_plain[int(method_start) - 1 : int(method_end)]
        selected_text = "\n".join(selected_lines)
        if split_groovy_assert_that_json_arguments(selected_text):
            new_lines, meta = synthesize_json_semantic_assert(selected_lines, "", "ID_JSON_READTREE_ASSERT")
        else:
            wrapper_template, effective_transform = json_assert_wrapper_for_method(method_lines, selected_lines, sample, original)
            if not wrapper_template or not effective_transform:
                if source_mentions_org_json_object(original):
                    new_lines, meta = synthesize_org_json_object_assert(selected_lines)
                else:
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
        if source_mentions_org_json_object(original):
            new_lines, meta = synthesize_org_json_object_assert(selected_lines)
        else:
            wrapper_template, wrapper_error = json_api_parse_template(original, sample)
            if not wrapper_template:
                return "", {
                    "ok": False,
                    "error_class": wrapper_error or "json_api_parser_not_visible",
                    "error": "",
                }
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
