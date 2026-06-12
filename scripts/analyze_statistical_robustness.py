#!/usr/bin/env python3
"""Offline paired statistical robustness analysis for paper tables."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path


METHODS = {
    "stabilityops": {
        "label": "StabilityOps",
        "path": "runs_remote/experiments/stabilityops_generic_api_level_merged_v2/results.jsonl",
    },
    "direct": {
        "label": "Direct Free-form",
        "path": "runs_remote/experiments/direct_freeform_qwen3_full721_rerun10_v1/results.jsonl",
    },
    "flakyfix": {
        "label": "Category-guided Free-form",
        "path": "runs_remote/experiments/flakyfix_style_qwen3_full721_rerun10_v1/results.jsonl",
    },
}


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def success(row: dict) -> bool:
    return row.get("decision") == "repaired" and not row.get("unsafe_patch")


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return (center - margin, center + margin)


def exact_mcnemar_p(b: int, c: int) -> float:
    """Two-sided exact binomial p-value for discordant counts b and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    pmf = 2.0 ** (-n)
    cdf = pmf
    for i in range(0, k):
        pmf *= (n - i) / (i + 1)
        cdf += pmf
    return min(1.0, 2.0 * cdf)


def bootstrap_delta_ci(a: list[bool], b: list[bool], seed: int, draws: int) -> tuple[float, float]:
    rng = random.Random(seed)
    n = len(a)
    values = []
    for _ in range(draws):
        diff = 0
        for _ in range(n):
            i = rng.randrange(n)
            diff += int(a[i]) - int(b[i])
        values.append(diff / n)
    values.sort()
    return values[int(0.025 * draws)], values[int(0.975 * draws)]


def pct(value: float) -> str:
    return f"{100 * value:.2f}%"


def sci_p(value: float) -> str:
    if value == 0:
        return "<1e-300"
    if value < 0.001:
        return f"{value:.2e}"
    return f"{value:.4f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, default=Path("data/metadata/idoft_verified_feasible.csv"))
    parser.add_argument("--output-json", type=Path, default=Path("docs/generated/statistical_robustness.json"))
    parser.add_argument("--output-md", type=Path, default=Path("docs/generated/statistical_robustness.md"))
    parser.add_argument("--bootstrap-draws", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260608)
    args = parser.parse_args()

    metadata = {}
    with args.metadata.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("PrimaryCategory") == "NOD":
                continue
            metadata[row["sample_id"]] = row

    by_method = {}
    for key, spec in METHODS.items():
        rows = {row["sample_id"]: row for row in read_jsonl(Path(spec["path"]))}
        rows = {sid: rows[sid] for sid in metadata if sid in rows}
        by_method[key] = rows

    sample_ids = sorted(set(metadata).intersection(*[set(rows) for rows in by_method.values()]))
    method_success = {
        key: [success(by_method[key][sid]) for sid in sample_ids]
        for key in by_method
    }

    overall = {}
    for key, flags in method_success.items():
        n = len(flags)
        s = sum(flags)
        lo, hi = wilson(s, n)
        overall[key] = {
            "label": METHODS[key]["label"],
            "success": s,
            "total": n,
            "rate": s / n,
            "wilson95": [lo, hi],
        }

    paired = []
    for baseline in ["direct", "flakyfix"]:
        a = method_success["stabilityops"]
        b = method_success[baseline]
        both = sum(x and y for x, y in zip(a, b))
        stab_only = sum(x and not y for x, y in zip(a, b))
        base_only = sum((not x) and y for x, y in zip(a, b))
        neither = sum((not x) and (not y) for x, y in zip(a, b))
        lo, hi = bootstrap_delta_ci(a, b, args.seed, args.bootstrap_draws)
        paired.append(
            {
                "comparison": f"StabilityOps vs {METHODS[baseline]['label']}",
                "both": both,
                "stabilityops_only": stab_only,
                "baseline_only": base_only,
                "neither": neither,
                "delta": (sum(a) - sum(b)) / len(a),
                "bootstrap95": [lo, hi],
                "mcnemar_p": exact_mcnemar_p(stab_only, base_only),
            }
        )

    category_ci = defaultdict(dict)
    for category in sorted({metadata[sid]["PrimaryCategory"] for sid in sample_ids}):
        cids = [sid for sid in sample_ids if metadata[sid]["PrimaryCategory"] == category]
        for key in by_method:
            flags = [success(by_method[key][sid]) for sid in cids]
            s = sum(flags)
            lo, hi = wilson(s, len(flags))
            category_ci[category][key] = {
                "success": s,
                "total": len(flags),
                "rate": s / len(flags),
                "wilson95": [lo, hi],
            }

    result = {
        "sample_count": len(sample_ids),
        "overall": overall,
        "paired": paired,
        "category_ci": category_ci,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Statistical Robustness",
        "",
        "This analysis uses only existing result JSONL files. No LLM calls or new validations are run.",
        "",
        "## Overall Success with Wilson 95% CI",
        "",
        "| Method | Success | Rate | 95% CI |",
        "| --- | --- | --- | --- |",
    ]
    for key in ["stabilityops", "direct", "flakyfix"]:
        item = overall[key]
        lines.append(
            f"| {item['label']} | {item['success']}/{item['total']} | {pct(item['rate'])} | "
            f"[{pct(item['wilson95'][0])}, {pct(item['wilson95'][1])}] |"
        )

    lines += [
        "",
        "## Paired Comparisons",
        "",
        "| Comparison | Both repair | StabilityOps only | Baseline only | Neither | Delta | Bootstrap 95% CI | McNemar p |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for item in paired:
        lines.append(
            f"| {item['comparison']} | {item['both']} | {item['stabilityops_only']} | "
            f"{item['baseline_only']} | {item['neither']} | {pct(item['delta'])} | "
            f"[{pct(item['bootstrap95'][0])}, {pct(item['bootstrap95'][1])}] | {sci_p(item['mcnemar_p'])} |"
        )

    lines += [
        "",
        "## Category Success with Wilson 95% CI",
        "",
        "| Category | Method | Success | Rate | 95% CI |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for category in sorted(category_ci):
        for key in ["stabilityops", "direct", "flakyfix"]:
            item = category_ci[category][key]
            lines.append(
                f"| {category} | {METHODS[key]['label']} | {item['success']}/{item['total']} | "
                f"{pct(item['rate'])} | [{pct(item['wilson95'][0])}, {pct(item['wilson95'][1])}] |"
            )

    args.output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
