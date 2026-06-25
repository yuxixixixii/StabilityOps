# StabilityOps Artifact Guide

This document explains how to run and inspect the StabilityOps artifact. It is written for researchers who want to reproduce or extend the experiments, not only read the code.

## 1. Artifact Scope

StabilityOps evaluates **known flaky-test repair**. The input is a known flaky test instance with a known category label from the released metadata. The artifact does not perform flaky-test detection or category prediction.

The released artifact includes:

- StabilityOps source code and the default typed-action prompt;
- an executable IDoFT-derived metadata subset with 721 repair candidates;
- scripts for preparing third-party repositories from metadata;
- a Qwen3/vLLM runner that can download the model automatically;
- Maven-based target-test and rerun validation scripts;
- result aggregation scripts.

The released artifact does not include:

- cloned third-party project worktrees;
- HuggingFace model weights;
- Maven dependency caches;
- generated experiment outputs.
- developer patch diffs as repair inputs.

Those artifacts are created under ignored local directories when the runner is executed.

The default preparation and repair path uses only pre-fix information. Developer/PR patches are not downloaded unless `scripts/prepare_idoft_samples.py` is invoked with `--download-developer-patches`, and that flag is intended only for offline evaluation analyses, not for repair prompts or patch generation.

The default public configuration uses `prompts/stabilityops_typed_action.md` to ask the LLM for a typed StabilityOps DSL action. Deterministic code performs context retrieval, guarded action execution, Patch Safety Filter checks, and validation. The optional `prompts/stabilityops_action_revision.md` prompt is included only for bounded retry experiments and is disabled by default.

The full Patch Safety Filter used in the experiment is implemented in `stabilityops/runtime.py` as the combination of syntactic unsafe-edit checks and patch-applicability checks. The standalone `scripts/unsafe_patch_scan.py` helper is a lightweight diagnostic scanner for individual patches, not the full evaluation filter.

## 2. Hardware and Software Requirements

Recommended full-run environment:

- Linux server with NVIDIA GPU support;
- A100-class GPU for the default Qwen3 Coder model;
- Python 3.10+;
- Java 8, recommended for the screened IDoFT projects;
- `git`, `curl`, and internet access;
- sufficient disk space for model weights, cloned repositories, and Maven caches.

The default model is:

```text
Qwen/Qwen3-Coder-30B-A3B-Instruct
```

The full run can require tens of GB for the model cache and many GB for cloned repositories and Maven dependencies. If this is too heavy, first use the smoke configuration or set `RUN_AGENT_LIMIT`.

Check the system environment:

```bash
bash scripts/check_environment.sh
```

If you only want to test model generation and skip Maven validation:

```bash
REQUIRE_JAVA=0 bash scripts/check_environment.sh
```

## 3. One-Command Smoke Run

Run a 5-sample smoke experiment on GPU 0:

```bash
export HF_HUB_DISABLE_XET=1
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_smoke \
  configs/stabilityops_qwen3_public_smoke.json
```

The script automatically:

1. checks system requirements;
2. installs/uses a local vLLM Python environment;
3. downloads Maven 3.8.8;
4. clones and checks out the required GitHub repositories;
5. downloads Qwen3 if `QWEN3_MODEL_PATH` is not set;
6. starts vLLM as an OpenAI-compatible local service;
7. runs StabilityOps;
8. evaluates the results.

Expected output directory:

```text
runs/experiments/stabilityops_qwen3_smoke/
```

Expected files:

```text
results.jsonl
eval.json
run.log
eval.log
patches/
validation_logs/
rendered_prompts/
prepare_samples.jsonl
```

## 4. Full 721-Sample Run

Run the full metadata subset:

```bash
export HF_HUB_DISABLE_XET=1
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_full721 \
  configs/stabilityops_qwen3_public_full721.json
```

Resume the same run after interruption:

```bash
RUN_RESUME=1 \
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_full721 \
  configs/stabilityops_qwen3_public_full721.json
```

Run only the first 20 samples:

```bash
RUN_AGENT_LIMIT=20 \
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_limit20 \
  configs/stabilityops_qwen3_public_full721.json
```

## 5. Reusing Existing Model or Worktrees

Use an already downloaded model:

```bash
export QWEN3_MODEL_PATH=/path/to/Qwen3-Coder-30B-A3B-Instruct/snapshot
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_smoke \
  configs/stabilityops_qwen3_public_smoke.json
```

Reuse already prepared repositories:

```bash
AUTO_PREPARE_DATA=0 \
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_smoke \
  configs/stabilityops_qwen3_public_smoke.json
```

Prepare repositories without running the LLM:

```bash
python3 -u scripts/prepare_idoft_samples.py \
  --metadata data/metadata/idoft_verified_feasible.csv \
  --workdir data/worktrees/idoft \
  --patch-dir data/patches/idoft \
  --output-jsonl runs/prepare_idoft_samples.jsonl \
  --limit 20
```

The command above does not download developer patches. If a researcher wants to reproduce separate developer-patch similarity analyses, they must explicitly add `--download-developer-patches`; those artifacts remain excluded from StabilityOps prompts and executor inputs.

## 6. Result Evaluation

Evaluate an existing run:

```bash
python3 scripts/evaluate_results.py \
  --results runs/experiments/<run_id>/results.jsonl \
  --output-json runs/experiments/<run_id>/eval.json
```

Important metrics:

- `repair_success_rate`: patch passes the target run, passes all rerun attempts, and is not rejected by the Patch Safety Filter;
- `unsafe_materialized_patch_rate`: materialized patches rejected by the safety filter;
- `safety_rejection_rate`: typed actions refused before acceptable patch materialization;
- `operator_coverage_rate`: samples for which a non-`NO_SAFE_TRANSFORM` operator was selected;
- `operator_materialization_rate`: samples for which a patch was produced after guard checks.

## 7. Common Failure Modes

`nvidia-smi not found`:

- vLLM needs an NVIDIA GPU runtime. Use a GPU server or run only non-vLLM analysis scripts.

Model download is slow:

- set `HF_ENDPOINT=https://hf-mirror.com` if appropriate for your network;
- set `QWEN3_MODEL_PATH` to reuse an existing local snapshot.

Java or Maven validation fails:

- Java 8 is recommended;
- historical Maven projects may depend on unavailable repositories or plugins;
- use `RUN_SKIP_VALIDATION=1` only for plumbing tests, not repair-success claims.

Repository clone fails:

- rerun with the same command; existing prepared worktrees are reused;
- inspect `runs/experiments/<run_id>/prepare_logs/`.

vLLM starts but the experiment cannot connect:

- inspect `runs/vllm/<model_alias>.log`;
- check that port `8003` is free or set `VLLM_PORT`.

## 8. Reproducibility Boundary

The released metadata fixes sample IDs, project URLs, commits, module paths, test identifiers, and known categories. However, GitHub availability, Maven dependencies, and external package repositories can change over time. Therefore, validation results may vary if dependencies disappear or project build systems change.

Developer patches are evaluation-only artifacts and are not included in prompts.
