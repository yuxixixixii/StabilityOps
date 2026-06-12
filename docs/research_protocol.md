# Research Protocol

## Claim

StabilityOps DSL studies whether converting flaky-test repair from free-form patch generation into typed DSL action planning plus guarded execution improves LLM-based repair.

The intended claim is narrow:

> For known flaky tests, StabilityOps DSL can improve repair success and patch safety under a fixed rerun validation budget by constraining LLMs to typed, guarded repair actions.

## Datasets

### Primary Dataset: IDoFT / FlakyFix-Compatible Subset

Use the International Dataset of Flaky Tests (IDoFT) as the primary dataset, following the FlakyFix setting as closely as possible.

Default selection:

- Start from Java/Maven entries in IDoFT, especially `pr-data.csv`, because FlakyFix focuses on Java tests and their developer fixes.
- Prefer samples where the developer fix modifies the test code rather than production code.
- Use single-label categories for the primary experiment; reserve semicolon-separated composite labels for a separate multi-cause analysis.
- Reconstruct the FlakyFix-compatible subset: 562 test-code-fix samples when metadata and fix links are available.
- For LLM-repair evaluation, follow FlakyFix's leakage-aware subset when possible: tests fixed or added after September 2021.
- For execution-based evaluation, first reproduce the FlakyFix executable subset if its replication package identifies the 35 runnable tests from 5 projects.
- If full FlakyFix replication metadata is unavailable, create our own executable subset from IDoFT by retaining only samples that can be checked out, built, patched, and rerun on the server.

Developer patches are evaluation-only artifacts. They must not be included in repair prompts except when constructing ground-truth labels offline before the repair prompt is generated.

### Secondary Dataset: FlakyDoctor-Compatible OD/ID Set

Use a FlakyDoctor-compatible OD/ID set as the secondary dataset if its artifact or sample list can be obtained.

Default use:

- Focus only on OD and ID tests, because FlakyDoctor reports results for 332 OD and 541 ID confirmed flaky tests.
- Use this dataset to test whether StabilityOps DSL generalizes beyond FlakyFix's test-code-only setting.
- Compare against FlakyDoctor only if the artifact can be run under the same samples and validation protocol.

### Non-Primary Reference: FlakyGuard Industrial Dataset

FlakyGuard evaluates on real-world industrial repositories and reports repair success and developer acceptance, but the dataset is not currently a reliable public benchmark for direct reproduction. Treat FlakyGuard as a conceptual and quantitative reference, especially for the context-selection problem, not as the default experimental dataset.

## Methods Under Comparison

### Direct LLM Repair

Single prompt with raw test code, logs, and basic project context. No stability intent and no agent decomposition.

### Category-Guided Repair

Prompt includes a root-cause or fix-category label but no stability-intent explanation and no intent-guided context expansion.

### Intent-Only Repair

Prompt includes inferred stability intent but uses the same raw context as Direct LLM Repair.

### StabilityOps DSL

Uses category-aware context planning, StabilityOps DSL action planning, guarded operator execution, and rerun validation feedback.

### FlakyFix-Style Prompting

Use the FlakyFix prompt variants as direct baselines on the IDoFT/FlakyFix-compatible subset:

- No-label GPT repair.
- Fix-category-label GPT repair.
- Fix-category-label plus in-context examples where category support is sufficient.

## Research Questions

- RQ1: Does StabilityOps DSL repair more known flaky tests than Direct LLM Repair?
- RQ2: Does typed DSL action planning help beyond category-guided free-form repair?
- RQ3: Which flaky categories benefit most from guarded DSL operators?
- RQ4: Does guarded DSL execution reduce unsafe patches?
- RQ5: Are findings consistent between the FlakyFix-compatible subset and the FlakyDoctor-compatible OD/ID subset?
- RQ6: On composite-label flaky tests, does multi-intent repair improve rerun success over single-intent repair?

## Metrics

Primary metric:

- `repair_success_rate`: proportion of samples whose candidate patch compiles, passes the target test once, and whose post-fix reruns all pass under the fixed rerun budget.
- `post_fix_outcome_consistency_rate`: proportion of samples whose post-fix rerun outcomes are consistent. This is reported separately because consistent failure is not a repair.
- `post_fix_consistent_pass_rate`: proportion of samples whose post-fix rerun outcomes are consistent and all PASS. This is the operational repair-success criterion.

Secondary metrics:

- `plausible_patch_rate`: compile success and target single-run success.
- `failure_rate_reduction`: pre-fix failure rate minus post-fix failure rate.
- `unsafe_patch_rate`: generated patch violates safety rules.
- `patch_size`: changed files, added lines, deleted lines.
- `intent_accuracy`: predicted intent category matches dataset or manual label.
- `validation_cost`: test runs, wall time, LLM calls, input/output tokens.

FlakyFix-aligned metrics:

- `codebleu`: similarity to developer fix when execution is unavailable.
- `sentence_bleu` and `corpus_bleu`: optional replication metrics for direct comparison.
- `pass_estimate`: optional logistic/bootstrap estimate only if we reproduce FlakyFix's statistical setup; do not use it as the primary claim.

## Default Validation Budget

- FlakyFix-compatible executable subset: 10 post-fix reruns for comparability.
- Our expanded executable IDoFT subset: 30 post-fix reruns if cost permits.
- FlakyDoctor-compatible OD/ID subset: match the artifact's default validation if running the original tool; otherwise use 30 reruns.

Report all repair conclusions as budgeted observations: "all N post-fix reruns passed", not "flakiness is permanently eliminated". Do not count deterministic failure as repair even if the outcomes are consistent.
