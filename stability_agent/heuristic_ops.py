"""Deterministic operator-selection baseline for StabilityOps ablations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from stability_agent.runtime import (
    find_test_file,
    line_span_from_action,
    method_bounds_for_sample,
    method_open_line,
    project_mentions_jackson,
    sample_test_method,
    target_test_relative_path,
)


def _no_safe(reason: str, evidence: list[str] | None = None) -> dict[str, Any]:
    return {
        "stability_spec": {
            "root_cause": "known_category",
            "required_invariant": "The target test should produce deterministic repeated outcomes.",
            "evidence_lines": [],
            "confidence": "low",
        },
        "transform_action": {"transform": "NO_SAFE_TRANSFORM", "reason": reason},
        "repair_rationale": reason,
        "safety_notes": evidence or [],
    }


def _target_file(sample: dict[str, str]) -> str:
    return target_test_relative_path(sample)


def _read_target(sample: dict[str, str]) -> tuple[Path | None, str, list[str], int | None, int | None]:
    path = find_test_file(sample)
    if not path or not path.exists():
        return None, "", [], None, None
    original = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    start, end = method_bounds_for_sample(original, sample)
    return path, original, original.splitlines(), start, end


def _method_text(lines: list[str], start: int | None, end: int | None) -> str:
    if not start or not end:
        return "\n".join(lines)
    return "\n".join(lines[start - 1 : end])


def _line_action(transform: str, sample: dict[str, str], line_no: int, **extra: Any) -> dict[str, Any]:
    return {
        "stability_spec": {
            "root_cause": sample.get("PrimaryCategory") or sample.get("Category") or "",
            "required_invariant": "Equivalent values should be compared under deterministic semantics.",
            "evidence_lines": [line_no],
            "operator_rationale": f"heuristic selected {transform} from visible target-method evidence",
            "confidence": "medium",
        },
        "transform_action": {
            "transform": transform,
            "target_file": _target_file(sample),
            "start_line": line_no,
            "end_line": line_no,
            **extra,
        },
        "repair_rationale": f"heuristic selected {transform}",
        "safety_notes": ["deterministic heuristic baseline; no LLM call"],
    }


def _first_line(lines: list[str], start: int | None, end: int | None, pattern: str, flags: int = 0) -> int | None:
    if not start or not end:
        return None
    regex = re.compile(pattern, flags)
    for line_no in range(start, end + 1):
        if regex.search(lines[line_no - 1]):
            return line_no
    return None


def _reflection_assignment(lines: list[str], start: int | None, end: int | None) -> tuple[int | None, str, str]:
    if not start or not end:
        return None, "", ""
    pattern = re.compile(
        r"\b(?P<var>[A-Za-z_]\w*)\s*=\s*[^;\n]*\.(?P<kind>getDeclaredMethods|getMethods|getMemberMethods|getDeclaredFields|getFields|getMemberFields|getDeclaredConstructors|getConstructors|getMemberConstructors)\s*\("
    )
    for line_no in range(start, end + 1):
        match = pattern.search(lines[line_no - 1])
        if match:
            kind = match.group("kind")
            if "Method" in kind:
                key = "METHOD_NAME"
            elif "Constructor" in kind:
                key = "CONSTRUCTOR_NAME"
            else:
                key = "FIELD_NAME"
            return line_no, match.group("var"), key
    return None, "", ""


def _static_reset_candidates(original: str, test_class: str) -> list[dict[str, str]]:
    resets: list[dict[str, str]] = []
    pattern = re.compile(
        r"\bstatic\b(?![^;\n]*\bfinal\b)[^;\n{}]*?\b(?P<type>[A-Za-z_][\w.<>?,\s\[\]]*)\s+(?P<field>[A-Za-z_]\w*)\s*(?:=|;)",
        re.M,
    )
    for match in pattern.finditer(original):
        field = match.group("field")
        if field in {"serialVersionUID"}:
            continue
        type_name = re.sub(r"\s+", "", match.group("type")).rsplit(".", 1)[-1]
        if type_name in {"int", "long", "short", "byte", "double", "float"}:
            op = "ASSIGN_ZERO"
        elif type_name == "boolean":
            op = "ASSIGN_FALSE"
        elif any(token in type_name for token in ["List", "Set", "Map", "Collection", "Queue", "Deque"]):
            op = "CLEAR_COLLECTION"
        else:
            continue
        resets.append({"receiver": test_class, "field": field, "operation": op})
    return resets[:4]


def _safe_test_class(sample: dict[str, str]) -> str:
    simple = str(sample.get("test_simple_class") or "")
    if simple and re.match(r"^[A-Za-z_]\w*$", simple):
        return simple
    class_name = str(sample.get("test_class") or "")
    simple = class_name.rsplit(".", 1)[-1]
    return simple if re.match(r"^[A-Za-z_]\w*$", simple) else ""


def _odvic_project_action(sample: dict[str, str], original: str) -> dict[str, Any] | None:
    if "TableUtils" in original or "createDao(" in original or "RuntimeExceptionDao" in original or "Dao<" in original:
        return {
            "stability_spec": {
                "root_cause": "OD-Vic",
                "required_invariant": "Database table state should be reset before the victim test.",
                "evidence_lines": [],
                "operator_rationale": "ORMLite DAO/table pattern visible in target file",
                "confidence": "high",
            },
            "transform_action": {
                "transform": "OD_VIC_ORMLITE_TABLE_CLEANUP",
                "target_file": _target_file(sample),
            },
            "repair_rationale": "heuristic selected ORMLite table cleanup",
            "safety_notes": ["api-level guarded operator"],
        }
    return None


def heuristic_repair_json(sample: dict[str, str], bundle: dict[str, Any] | None = None) -> dict[str, Any]:
    """Select a guarded operator using deterministic code-pattern evidence.

    This is intentionally conservative. It is an ablation baseline for asking
    whether a hand-written operator ranking can replace LLM action selection.
    """
    path, original, lines, start, end = _read_target(sample)
    if not path:
        return _no_safe("missing_target_test_file")
    category = str(sample.get("PrimaryCategory") or sample.get("Category") or "").strip()
    method_text = _method_text(lines, start, end)
    repo = str(sample.get("repo_slug") or "")

    if category == "NIO":
        test_class = _safe_test_class(sample)
        resets = _static_reset_candidates(original, test_class) if test_class else []
        if resets:
            return {
                "stability_spec": {
                    "root_cause": "NIO",
                    "required_invariant": "Static mutable test state should be reset at the start of each target run.",
                    "evidence_lines": [start] if start else [],
                    "operator_rationale": "visible non-final static mutable fields",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "NIO_STATIC_FIELD_RESET",
                    "target_file": _target_file(sample),
                    "resets": resets,
                },
                "repair_rationale": "heuristic selected static field reset",
                "safety_notes": ["typed reset operations only"],
            }
        return _no_safe("no_visible_resettable_static_fields")

    if category == "OD-Vic":
        action = _odvic_project_action(sample, original)
        if action:
            return action
        return _no_safe("no_supported_od_vic_project_pattern")

    if category == "OD":
        action = _odvic_project_action(sample, original)
        if action:
            return action
        if "getenv(" in method_text and ("setenv(" in method_text or "putenv(" in method_text):
            return {
                "stability_spec": {
                    "root_cause": "OD",
                    "required_invariant": "Environment mutations should be restored after the target check.",
                    "evidence_lines": [start] if start else [],
                    "operator_rationale": "visible environment save/mutation pattern",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "OD_RESTORE_ENV_AFTER_MUTATION",
                    "target_file": _target_file(sample),
                },
                "repair_rationale": "heuristic selected environment restore",
                "safety_notes": ["typed environment restore only"],
            }
        if repo in {"alibaba/fastjson", "alibaba/fastjson2"} and "extends TestCase" in original:
            return {
                "stability_spec": {
                    "root_cause": "OD",
                    "required_invariant": "Locale/timezone global defaults should be deterministic.",
                    "evidence_lines": [],
                    "operator_rationale": "FastJSON TestCase timezone/locale pattern",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "OD_FASTJSON_DEFAULT_TZ_LOCALE",
                    "target_file": _target_file(sample),
                    "timezone": "Asia/Shanghai",
                    "locale_expr": "java.util.Locale.CHINA",
                },
                "repair_rationale": "heuristic selected FastJSON default timezone/locale setup",
                "safety_notes": ["guarded setup operator"],
            }
        return _no_safe("no_supported_od_pattern")

    if category == "ID":
        if "getDeclaredMethods()" in original or "getDeclaredFields()" in original or "getDeclaredConstructors()" in original:
            return {
                "stability_spec": {
                    "root_cause": "ID",
                    "required_invariant": "Reflection-discovered members should be ordered deterministically.",
                    "evidence_lines": [],
                    "operator_rationale": "declared-member reflection use visible in target/repo source",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "ID_SORT_DECLARED_MEMBERS_BY_NAME",
                    "target_file": _target_file(sample),
                },
                "repair_rationale": "heuristic selected declared-member sort",
                "safety_notes": ["generic reflection-order operator"],
            }

        line_no, array_variable, sort_key = _reflection_assignment(lines, start, end)
        if line_no and array_variable:
            return {
                "stability_spec": {
                    "root_cause": "ID",
                    "required_invariant": "Reflection results should be sorted before order-sensitive assertions.",
                    "evidence_lines": [line_no],
                    "operator_rationale": "reflection array assignment visible in target method",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "ID_SORT_REFLECTION_RESULTS",
                    "target_file": _target_file(sample),
                    "insert_after_line": line_no,
                    "array_variable": array_variable,
                    "sort_key": sort_key,
                },
                "repair_rationale": "heuristic selected reflection result sort",
                "safety_notes": ["typed reflection sort parameters"],
            }

        if "assertEquals" in method_text and (
            "com.alibaba.fastjson" in original
            or "fastjson" in original.lower()
            or "JSON.toJSONString" in method_text
            or "JSONPath" in method_text
        ):
            return {
                "stability_spec": {
                    "root_cause": "ID",
                    "required_invariant": "JSON assertions should compare parsed JSON values.",
                    "evidence_lines": [start] if start else [],
                    "operator_rationale": "FastJSON API and JSON assertion evidence",
                    "confidence": "medium",
                },
                "transform_action": {
                    "transform": "ID_FASTJSON_METHOD_JSON_ASSERTS",
                    "target_file": _target_file(sample),
                },
                "repair_rationale": "heuristic selected FastJSON method JSON asserts",
                "safety_notes": ["method-level guarded FastJSON API operator"],
            }

        json_line = _first_line(lines, start, end, r"assert(?:Equals|That)\s*\(.*[\\{\\[]|assert(?:Equals|That)\s*\(.*json", re.I)
        if json_line and project_mentions_jackson(sample, original):
            return _line_action("ID_JSON_READTREE_ASSERT_TRY_CATCH", sample, json_line)

        assertj_line = _first_line(lines, start, end, r"\.containsExactly\s*\(|assertThat\s*\(.*\)\.isEqualTo\s*\(")
        if assertj_line:
            return _line_action("ID_ASSERTJ_LIST_ORDER_INSENSITIVE", sample, assertj_line)

        junit_line = _first_line(lines, start, end, r"assertEquals\s*\(.*(?:get\s*\(|\[[^\]]+\])")
        if junit_line:
            return _line_action("ID_LIST_ORDER_INSENSITIVE", sample, junit_line)

        return _no_safe("no_supported_id_pattern")

    return _no_safe(f"unsupported_category:{category}")
