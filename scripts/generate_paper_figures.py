#!/usr/bin/env python3
"""Generate paper figures from recorded experiment summaries."""

from __future__ import annotations

import json
from pathlib import Path
from collections import Counter

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Patch


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "docs" / "generated" / "paper_results_summary.json"
OPERATOR_ONLY_SUMMARY = ROOT / "docs" / "generated" / "operator_only_baseline_summary.json"
EXPERIMENTS = ROOT / "runs_remote" / "experiments"
FIG_DIR = ROOT / "paper_jss" / "figures"

plt.rcParams.update({
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "font.size": 9,
})


COLORS = {
    "repaired": "#2f6f4e",
    "refused": "#d7a72f",
    "unsafe": "#b64a3a",
    "other": "#8793a1",
    "pending": "#eef0f3",
    "funnel": "#356c9b",
    "gap": "#c7ccd4",
    "direct": "#7d87a8",
    "category": "#c68642",
    "flakyfix": "#8b6ab8",
}


RUNS = {
    "StabilityOps": EXPERIMENTS / "stabilityops_generic_api_level_merged_v2" / "results.jsonl",
    "Direct": EXPERIMENTS / "direct_freeform_qwen3_full721_rerun10_filterfix_v2" / "results.jsonl",
    "Category-guided Free-form": EXPERIMENTS / "flakyfix_style_qwen3_full721_rerun10_filterfix_v2" / "results.jsonl",
    "FlakyFix-style": EXPERIMENTS / "flakyfix_original_qwen3_full721_rerun10_v2" / "results.jsonl",
}


def pct(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%"


def load_summary() -> dict:
    with SUMMARY.open(encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def repaired_sample_ids(path: Path) -> set[str]:
    repaired = set()
    for row in read_jsonl(path):
        if row.get("decision") == "repaired" or (
            row.get("post_fix_consistent_pass") and not row.get("unsafe_patch")
        ):
            repaired.add(str(row["sample_id"]))
    return repaired


def load_repair_sets() -> dict[str, set[str]]:
    sets = {
        "StabilityOps": repaired_sample_ids(RUNS["StabilityOps"]),
        "Direct": repaired_sample_ids(RUNS["Direct"]),
        "Category-guided Free-form": repaired_sample_ids(RUNS["Category-guided Free-form"]),
        "FlakyFix-style": repaired_sample_ids(RUNS["FlakyFix-style"]),
    }
    operator_summary = json.loads(OPERATOR_ONLY_SUMMARY.read_text(encoding="utf-8"))
    sets["Op-only"] = {
        str(row["sample_id"])
        for row in operator_summary["combined_rows"]
        if row.get("repaired")
    }
    return sets


def outcome_rows(summary: dict) -> list[dict]:
    by_key = {row["key"]: row for row in summary["main_table"]}
    operator_summary = json.loads(OPERATOR_ONLY_SUMMARY.read_text(encoding="utf-8"))["summary"]
    return [
        by_key["stabilityops"],
        {
            "key": "operator_only",
            "method": "Operator-only",
            "samples": int(operator_summary["denominator"]),
            "repaired": int(operator_summary["repair_success"]["count"]),
            "no_materialized_patches": (
                int(operator_summary["denominator"]) - int(operator_summary["materialized_patches"]["count"])
            ),
            "unsafe_materialized_patches": int(operator_summary["unsafe_materialized"]["count"]),
        },
        by_key["direct"],
        by_key["category_guided"],
        by_key["flakyfix_style"],
    ]


def draw_outcome_stack(summary: dict) -> None:
    methods = outcome_rows(summary)
    labels = [m["method"].replace(" ", "\n") for m in methods]
    total = methods[0]["samples"]
    y_real = np.arange(len(methods))

    repaired = [m["repaired"] for m in methods]
    refused = [m["no_materialized_patches"] for m in methods]
    unsafe = [m["unsafe_materialized_patches"] for m in methods]
    other = [m["samples"] - repaired[i] - refused[i] - unsafe[i] for i, m in enumerate(methods)]

    fig, ax = plt.subplots(figsize=(7.1, 3.0))
    left = [0] * len(methods)
    segments = [
        ("Repaired", repaired, COLORS["repaired"]),
        ("No materialized patch", refused, COLORS["refused"]),
        ("Unsafe materialized", unsafe, COLORS["unsafe"]),
        ("Validation/other failure", other, COLORS["other"]),
    ]

    for name, values, color in segments:
        ax.barh(y_real, values, left=left, label=name, color=color, edgecolor="white", linewidth=0.6)
        for i, value in enumerate(values):
            if value >= 45:
                ax.text(left[i] + value / 2, i, f"{value}\n{pct(value, total)}",
                        ha="center", va="center", color="white", fontsize=7)
        left = [left[i] + values[i] for i in range(len(values))]

    ax.set_xlim(0, total)
    ax.set_yticks(np.arange(len(labels)), labels)
    ax.set_xlabel("Number of samples")
    ax.legend(ncol=4, loc="lower center", bbox_to_anchor=(0.5, 1.02), frameon=False, fontsize=7.5)
    ax.grid(axis="x", color="#e6e8eb", linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "outcome_stack.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "outcome_stack.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def draw_outcome_compact(summary: dict) -> None:
    methods = outcome_rows(summary)
    labels = ["Stability\nOps", "Op-only", "Direct", "Cat.\nFree-form", "FlakyFix-\nstyle"]
    total = methods[0]["samples"]
    x_real = np.arange(len(methods))

    repaired = [m["repaired"] / total * 100 for m in methods]
    refused = [m["no_materialized_patches"] / total * 100 for m in methods]
    unsafe = [m["unsafe_materialized_patches"] / total * 100 for m in methods]
    other = [100 - repaired[i] - refused[i] - unsafe[i] for i in range(len(methods))]

    fig, ax = plt.subplots(figsize=(3.35, 2.45))
    bottom = [0.0] * len(methods)
    segments = [
        ("Repaired", repaired, COLORS["repaired"]),
        ("No patch", refused, COLORS["refused"]),
        ("Unsafe", unsafe, COLORS["unsafe"]),
        ("Other failure", other, COLORS["other"]),
    ]

    for name, values, color in segments:
        ax.bar(x_real, values, bottom=bottom, label=name, color=color, edgecolor="white", linewidth=0.5)
        for i, value in enumerate(values):
            if value >= 8:
                text_color = "#20252b" if name == "No patch" else "white"
                fontsize = 6.2 if value < 15 else 7
                ax.text(i, bottom[i] + value / 2, f"{value:.0f}%", ha="center", va="center",
                        color=text_color, fontsize=fontsize)
        bottom = [bottom[i] + values[i] for i in range(len(values))]

    ax.set_ylim(0, 100)
    ax.set_ylabel("Samples (%)")
    ax.set_xticks(np.arange(len(labels)), labels)
    ax.tick_params(axis="x", labelrotation=0, labelsize=7.2)
    ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.02), frameon=False,
              fontsize=6.2, columnspacing=0.55, handlelength=1.0)
    ax.grid(axis="y", color="#e6e8eb", linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "outcome_compact.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "outcome_compact.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def draw_success_overlap() -> None:
    repair_sets = load_repair_sets()
    methods = ["StabilityOps", "Op-only", "Direct", "Category-guided Free-form", "FlakyFix-style"]
    display_labels = {
        "StabilityOps": "StabilityOps",
        "Op-only": "Op-only",
        "Direct": "Direct",
        "Category-guided Free-form": "Cat. Free-form",
        "FlakyFix-style": "FlakyFix-style",
    }
    all_samples = set().union(*repair_sets.values())
    exact = Counter()
    for sample_id in all_samples:
        combo = tuple(method for method in methods if sample_id in repair_sets[method])
        exact[combo] += 1
    none_count = 721 - len(all_samples)
    combos = [combo for combo, _ in exact.most_common()]
    counts = [exact[combo] for combo in combos]

    fig = plt.figure(figsize=(3.35, 3.15))
    grid = fig.add_gridspec(2, 1, height_ratios=[1.65, 1.0], hspace=0.08)
    ax = fig.add_subplot(grid[0])
    matrix_ax = fig.add_subplot(grid[1], sharex=ax)

    x = np.arange(len(combos))
    ax.bar(x, counts, color=COLORS["repaired"], edgecolor="white", linewidth=0.5)
    ax.set_ylabel("Repairs")
    ax.set_xlim(-0.6, len(combos) - 0.4)
    ax.grid(axis="y", color="#e6e8eb", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    for i, count in enumerate(counts):
        if count >= 18 or i < 4:
            offset = 2.0 if count < 40 else 2.6
            ax.text(i, count + offset, str(count), ha="center", va="bottom", fontsize=5.3)
    ax.text(
        0.99,
        0.94,
        f"No method: {none_count}",
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=6.5,
        color="#555b63",
    )
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)

    matrix_ax.set_ylim(-0.6, len(methods) - 0.4)
    matrix_ax.set_yticks(range(len(methods)), [display_labels[method] for method in methods])
    matrix_ax.invert_yaxis()
    matrix_ax.set_xticks([])
    matrix_ax.tick_params(axis="y", labelsize=6.8)
    for i, combo in enumerate(combos):
        active_y = []
        for y, method in enumerate(methods):
            active = method in combo
            color = "#263238" if active else "#d4d8dd"
            size = 18 if active else 10
            matrix_ax.scatter(i, y, s=size, color=color, zorder=3)
            if active:
                active_y.append(y)
        if len(active_y) > 1:
            matrix_ax.plot([i, i], [min(active_y), max(active_y)], color="#263238", linewidth=0.65, zorder=2)
    matrix_ax.set_xlim(-0.6, len(combos) - 0.4)
    for spine in ["top", "right", "bottom"]:
        matrix_ax.spines[spine].set_visible(False)
    matrix_ax.grid(axis="x", color="#f0f1f3", linewidth=0.5)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "success_overlap_upset.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "success_overlap_upset.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def safety_rule_counts(path: Path) -> Counter:
    counts = Counter()
    for row in read_jsonl(path):
        if not row.get("unsafe_patch"):
            continue
        findings = row.get("unsafe_findings") or []
        if not findings:
            counts["unknown"] += 1
            continue
        for finding in findings:
            if isinstance(finding, dict):
                counts[str(finding.get("rule") or "unknown")] += 1
            elif finding:
                counts[str(finding)] += 1
    return counts


def draw_safety_filter_findings() -> None:
    direct = safety_rule_counts(RUNS["Direct"])
    category = safety_rule_counts(RUNS["Category-guided Free-form"])
    flakyfix = safety_rule_counts(RUNS["FlakyFix-style"])
    label_map = {
        "formatting_only_patch": "Formatting only",
        "adds_helper_method": "Helper method",
        "adds_import": "Import added",
        "placeholder_hunk_header": "Placeholder hunk",
        "fixed_sleep": "Fixed sleep",
        "empty_patch": "Empty patch",
        "adds_class_field": "Class field",
        "hunk_header_outside_target_method": "Out-of-scope hunk",
        "deleted_assertion": "Assertion removed",
        "trivial_assertion": "Trivial assertion",
        "changes_non_target_file": "Non-target file",
        "deleted_test_annotation": "Test disabled",
    }
    ordered_rules = [
        rule
        for rule, _ in (direct + category + flakyfix).most_common()
        if rule in label_map
    ][:8]
    labels = [label_map[rule] for rule in ordered_rules]
    direct_values = [direct[rule] for rule in ordered_rules]
    category_values = [category[rule] for rule in ordered_rules]
    flakyfix_values = [flakyfix[rule] for rule in ordered_rules]

    y = np.arange(len(ordered_rules))
    height = 0.58
    totals = [direct_values[i] + category_values[i] + flakyfix_values[i] for i in range(len(y))]
    max_total = max(totals)
    fig, ax = plt.subplots(figsize=(3.35, 2.65))

    ax.barh(y, direct_values, height, label="Direct", color=COLORS["direct"], edgecolor="white", linewidth=0.4)
    ax.barh(
        y,
        category_values,
        height,
        left=direct_values,
        label="Cat. Free-form",
        color=COLORS["category"],
        edgecolor="white",
        linewidth=0.4,
    )
    ax.barh(
        y,
        flakyfix_values,
        height,
        left=[direct_values[i] + category_values[i] for i in range(len(y))],
        label="FlakyFix-style",
        color=COLORS["flakyfix"],
        edgecolor="white",
        linewidth=0.4,
    )
    ax.set_yticks(y, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Filter findings", fontsize=7.4)
    ax.grid(axis="x", color="#e6e8eb", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_xlim(0, max_total * 1.15)

    for i, (direct_value, category_value, flakyfix_value, total_value) in enumerate(
        zip(direct_values, category_values, flakyfix_values, totals)
    ):
        starts = [0, direct_value, direct_value + category_value]
        values = [direct_value, category_value, flakyfix_value]
        for start, value in zip(starts, values):
            if value >= max_total * 0.12:
                ax.text(start + value / 2, i, str(value), ha="center", va="center", fontsize=6.1, color="white")
        ax.text(total_value + max_total * 0.018, i, str(total_value), va="center", fontsize=6.2, color="#33383f")

    ax.tick_params(axis="y", labelsize=6.8, length=0)
    ax.tick_params(axis="x", labelsize=6.8)
    ax.legend(ncol=3, loc="lower center", bbox_to_anchor=(0.5, 1.02), frameon=False,
              fontsize=6.2, columnspacing=0.7, handlelength=1.1)
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "safety_filter_findings.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "safety_filter_findings.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def draw_stabilityops_funnel(summary: dict) -> None:
    total = summary["main_table"][0]["samples"]
    coverage = summary["stabilityops_coverage"]
    steps = [
        ("Input samples", total),
        ("Operator selected", round(coverage["operator_coverage_rate"] * total)),
        ("Action applicable", round(coverage["operator_applicability_rate"] * total)),
        ("Patch materialized", round(coverage["operator_materialization_rate"] * total)),
        ("Rerun10 repaired", summary["main_table"][0]["repaired"]),
    ]
    gaps = summary["top_coverage_gaps"][:5]

    fig, (ax, gap_ax) = plt.subplots(
        2, 1, figsize=(7.1, 4.45), gridspec_kw={"height_ratios": [1.0, 1.0], "hspace": 0.55}
    )

    y = list(range(len(steps)))
    names = [s[0] for s in steps]
    counts = [s[1] for s in steps]
    ax.barh(y, counts, color=COLORS["funnel"], edgecolor="white", linewidth=0.6)
    ax.invert_yaxis()
    ax.set_yticks(y, names)
    ax.set_xlim(0, total)
    ax.set_xlabel("Samples")
    ax.set_title("(a) Guarded execution funnel", pad=6, fontsize=9)
    ax.grid(axis="x", color="#e6e8eb", linewidth=0.8)
    ax.set_axisbelow(True)
    for i, count in enumerate(counts):
        ax.text(count - 10, i, f"{count} ({pct(count, total)})", va="center", ha="right",
                color="white", fontsize=8)
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)

    gap_names = [
        name.replace("dsl_coverage_gap:", "").replace("validation_failure:", "validation:").replace("_", " ")
        for name, _ in gaps
    ]
    gap_counts = [count for _, count in gaps]
    gy = list(range(len(gaps)))
    gap_ax.barh(gy, gap_counts, color=COLORS["gap"], edgecolor="white", linewidth=0.6)
    gap_ax.invert_yaxis()
    gap_ax.set_yticks(gy, gap_names)
    gap_ax.set_xlim(0, max(gap_counts) * 1.15)
    gap_ax.set_xlabel("Samples")
    gap_ax.set_title("(b) Top uncovered or rejected cases", pad=6, fontsize=9)
    gap_ax.grid(axis="x", color="#e6e8eb", linewidth=0.8)
    gap_ax.set_axisbelow(True)
    for i, count in enumerate(gap_counts):
        gap_ax.text(count + 2, i, str(count), va="center", fontsize=8)
    for spine in ["top", "right", "left"]:
        gap_ax.spines[spine].set_visible(False)

    fig.tight_layout()
    fig.savefig(FIG_DIR / "stabilityops_funnel.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "stabilityops_funnel.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def draw_stabilityops_funnel_compact(summary: dict) -> None:
    total = summary["main_table"][0]["samples"]
    coverage = summary["stabilityops_coverage"]
    steps = [
        ("Input", total),
        ("Operator selected", round(coverage["operator_coverage_rate"] * total)),
        ("Guard applicable", round(coverage["operator_applicability_rate"] * total)),
        ("Patch materialized", round(coverage["operator_materialization_rate"] * total)),
        ("Accepted repair", summary["main_table"][0]["repaired"]),
    ]

    counts = [count for _, count in steps]
    drops = [counts[i] - counts[i + 1] for i in range(len(counts) - 1)]
    drop_labels = ["no operator", "guard refused", "no patch", "validation failed"]

    fig, ax = plt.subplots(figsize=(3.35, 3.05))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    x_center = 0.40
    y_positions = np.linspace(0.86, 0.16, len(steps))
    max_width = 0.62
    min_width = 0.40
    node_height = 0.112

    for i, ((name, count), y) in enumerate(zip(steps, y_positions)):
        width = min_width + (max_width - min_width) * (count / total)
        color = COLORS["repaired"] if i == len(steps) - 1 else COLORS["funnel"]
        x0 = x_center - width / 2
        box = FancyBboxPatch(
            (x0, y - node_height / 2),
            width,
            node_height,
            boxstyle="round,pad=0.012,rounding_size=0.018",
            linewidth=0.6,
            edgecolor="white",
            facecolor=color,
        )
        ax.add_patch(box)
        ax.text(
            x_center,
            y + 0.018,
            name,
            ha="center",
            va="center",
            color="white",
            fontsize=7.1,
            fontweight="bold",
        )
        ax.text(
            x_center,
            y - 0.026,
            f"{count}  ({count / total * 100:.1f}%)",
            ha="center",
            va="center",
            color="white",
            fontsize=6.6,
        )

        if i < len(steps) - 1:
            y_next = y_positions[i + 1]
            arrow = FancyArrowPatch(
                (x_center, y - node_height / 2 - 0.012),
                (x_center, y_next + node_height / 2 + 0.012),
                arrowstyle="-|>",
                mutation_scale=7.5,
                linewidth=0.7,
                color="#9aa3ad",
            )
            ax.add_patch(arrow)
            mid_y = (y + y_next) / 2
            ax.text(
                0.82,
                mid_y,
                f"-{drops[i]}",
                ha="center",
                va="center",
                fontsize=7.0,
                color="#353b42",
                fontweight="bold",
            )
            ax.text(
                0.82,
                mid_y - 0.038,
                drop_labels[i],
                ha="center",
                va="center",
                fontsize=5.9,
                color="#5f6872",
            )

    ax.text(0.82, 0.94, "Stopped between stages", ha="center", va="center", fontsize=6.3, color="#5f6872")

    fig.tight_layout()
    fig.savefig(FIG_DIR / "stabilityops_funnel_compact.pdf", bbox_inches="tight")
    fig.savefig(FIG_DIR / "stabilityops_funnel_compact.png", dpi=240, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    summary = load_summary()
    draw_outcome_stack(summary)
    draw_outcome_compact(summary)
    draw_success_overlap()
    draw_safety_filter_findings()
    draw_stabilityops_funnel(summary)
    draw_stabilityops_funnel_compact(summary)
    print(f"wrote {FIG_DIR / 'outcome_stack.pdf'}")
    print(f"wrote {FIG_DIR / 'outcome_compact.pdf'}")
    print(f"wrote {FIG_DIR / 'success_overlap_upset.pdf'}")
    print(f"wrote {FIG_DIR / 'safety_filter_findings.pdf'}")
    print(f"wrote {FIG_DIR / 'stabilityops_funnel.pdf'}")
    print(f"wrote {FIG_DIR / 'stabilityops_funnel_compact.pdf'}")


if __name__ == "__main__":
    main()
