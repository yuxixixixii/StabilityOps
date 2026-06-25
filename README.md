# StabilityOps

**StabilityOps: Safer Flaky-Test Repair via LLM-Instantiated Guarded Repair Operators**

StabilityOps is an experimental framework for repairing **known flaky tests** with a typed DSL and a guarded patch executor. It does not ask an LLM to directly write arbitrary patches. Instead, the LLM selects a guarded repair operator and binds typed parameters; a deterministic executor checks the action and materializes a patch only when all guards pass.

```text
Known flaky test + known category
  -> LLM proposes a typed DSL action
  -> Guarded executor checks schema, scope, and operator guards
  -> Executor deterministically materializes a patch, or refuses with diagnostics
  -> Patch Safety Filter
  -> target single run + fixed-budget rerun validation
```

The claim is intentionally narrow: StabilityOps studies safer, more diagnosable **known flaky-test repair** under a fixed rerun budget. It does not perform flaky-test detection and does not claim permanent elimination of all future flakiness.

## What Is Included

This repository is intended to be directly usable after cloning.

- `stabilityops/`: StabilityOps runtime, guarded executor, DSL operators, safety checks, and validation.
- `scripts/run_stabilityops_experiment.py`: StabilityOps-only experiment runner.
- `scripts/run_stabilityops_qwen3.sh`: one-command Qwen3/vLLM runner with automatic model download.
- `scripts/download_hf_model.py`: explicit HuggingFace snapshot downloader.
- `scripts/setup_maven.sh`: automatic Apache Maven 3.8.8 setup for Java/Maven validation.
- `prompts/stabilityops_typed_action.md`: the default LLM prompt for producing typed StabilityOps DSL actions.
- `prompts/stabilityops_action_revision.md`: optional bounded action-revision prompt, used only when enabled in the config.
- `configs/stabilityops_qwen3_public_full721.json`: full 721-sample configuration.
- `configs/stabilityops_qwen3_public_smoke.json`: small 5-sample smoke configuration.
- `data/metadata/idoft_verified_feasible.csv`: the 721-sample executable IDoFT-derived metadata subset.
- `data/metadata/subsets/idoft_verified_feasible_balanced_10_each.csv`: a smaller balanced subset.
- `ARTIFACT.md`: artifact-oriented reproduction guide.
- `docs/stabilityops_dataset_card.md`: scope and reproduction notes for the released metadata subset.

Large generated artifacts are intentionally not tracked: cloned repositories, Maven worktrees, HuggingFace model weights, local patch caches, and experiment outputs.

## Requirements

Recommended environment:

- Linux server with an NVIDIA GPU for vLLM.
- Python 3.10+.
- Java 8 for the IDoFT Maven projects used in our experiments.
- Internet access to download Qwen3, Maven, and project dependencies.

The one-command runner automatically handles project-level dependencies:

- creates `.venv-vllm` and installs `vllm`, `torch`, `transformers`, `openai`, and `huggingface_hub` when needed;
- downloads `Qwen/Qwen3-Coder-30B-A3B-Instruct` unless `QWEN3_MODEL_PATH` is set;
- downloads Apache Maven 3.8.8 into `tools/`;
- clones/checks out the GitHub repositories listed in the metadata.

System-level dependencies are checked but not silently installed:

- NVIDIA driver/CUDA runtime compatible with vLLM;
- Java, preferably Java 8 for highest compatibility with the screened IDoFT Maven projects;
- `git`, `curl`, and normal network access.

Check the environment without starting a run:

```bash
bash scripts/check_environment.sh
```

If you only want to test LLM action generation and skip Maven validation:

```bash
REQUIRE_JAVA=0 bash scripts/check_environment.sh
```

The default model is:

```text
Qwen/Qwen3-Coder-30B-A3B-Instruct
```

The runner downloads it automatically through `huggingface_hub` if `QWEN3_MODEL_PATH` is not already set.

If you are in China or have slow HuggingFace access, set:

```bash
export HF_ENDPOINT=https://hf-mirror.com
export HF_HUB_DISABLE_XET=1
```

## Quick Start

After cloning the repository, run a small Qwen3 smoke experiment on GPU 0:

```bash
cd stabilityops

export HF_HUB_DISABLE_XET=1
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_smoke \
  configs/stabilityops_qwen3_public_smoke.json
```

What the script does:

1. Checks whether a vLLM-capable Python environment exists.
2. Creates `.venv-vllm` and installs vLLM dependencies if needed.
3. Downloads Apache Maven 3.8.8 into `tools/` if validation is enabled.
4. Reads the dataset in the config and prepares the required GitHub worktrees under `data/worktrees/idoft/`.
5. Downloads `Qwen/Qwen3-Coder-30B-A3B-Instruct` if no local model path is provided.
6. Starts an OpenAI-compatible vLLM service on `127.0.0.1:8003`.
7. Runs StabilityOps and writes outputs under `runs/experiments/<run_id>/`.
8. Evaluates the resulting `results.jsonl`.

To reuse an already downloaded model:

```bash
export QWEN3_MODEL_PATH=/path/to/Qwen3-Coder-30B-A3B-Instruct/snapshot
bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_smoke configs/stabilityops_qwen3_public_smoke.json
```

To skip Maven validation and only test LLM action generation plus executor/safety plumbing:

```bash
RUN_SKIP_VALIDATION=1 \
bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_no_validation configs/stabilityops_qwen3_public_smoke.json
```

To reuse already prepared repositories without cloning/checking out again:

```bash
AUTO_PREPARE_DATA=0 \
bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_smoke configs/stabilityops_qwen3_public_smoke.json
```

## Full 721-Sample Run

The full configuration is:

```text
configs/stabilityops_qwen3_public_full721.json
```

Run it on GPU 0:

```bash
export HF_HUB_DISABLE_XET=1
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_full721 \
  configs/stabilityops_qwen3_public_full721.json
```

Resume an interrupted run:

```bash
RUN_RESUME=1 \
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_full721 \
  configs/stabilityops_qwen3_public_full721.json
```

Limit the number of samples:

```bash
RUN_AGENT_LIMIT=20 \
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_limit20 \
  configs/stabilityops_qwen3_public_full721.json
```

## Running Modes

Most users should start with the smoke run before launching the full experiment.

| Mode | Command pattern | Purpose |
| --- | --- | --- |
| Environment check | `bash scripts/check_environment.sh` | Verify system dependencies without running an experiment. |
| Smoke run | `bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_smoke configs/stabilityops_qwen3_public_smoke.json` | Download model/repos as needed and run 5 samples. |
| Limited full config | `RUN_AGENT_LIMIT=20 bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_limit20 configs/stabilityops_qwen3_public_full721.json` | Test the full configuration on a small prefix. |
| Full run | `bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_full721 configs/stabilityops_qwen3_public_full721.json` | Run all 721 metadata samples. |

More detailed artifact instructions are in `ARTIFACT.md`.

## Prompts Used by Default

The public StabilityOps configuration uses one LLM prompt per repair attempt:

```text
prompts/stabilityops_typed_action.md
```

That prompt asks the LLM to output a typed StabilityOps DSL action, not a free-form patch. Context retrieval, schema checks, operator guards, patch materialization, Patch Safety Filter checks, and rerun validation are handled by deterministic code.

The full Patch Safety Filter used by the experiment is implemented in `stabilityops/runtime.py` as the combination of syntactic unsafe-edit checks and patch-applicability checks. The standalone `scripts/unsafe_patch_scan.py` script is only a lightweight diagnostic scanner for individual patches; it is not the full evaluation filter.

`prompts/stabilityops_action_revision.md` is included for optional bounded retry experiments. It is disabled in the default public configs (`transform_action_repair_attempts: 0`).

## Dataset Notes

The released metadata file is:

```text
data/metadata/idoft_verified_feasible.csv
```

It contains 721 IDoFT-derived repair candidates that were executable under our Java 8 + Maven 3.8.8 screening protocol. Each released row records repository URL, detected SHA, module path, fully-qualified test name, flaky category, and validation metadata needed to rebuild and execute the sample.

The public StabilityOps runner uses only pre-fix information: repository URL, detected SHA, module path, target test, known category, and local execution metadata. It does **not** load developer patches, PR patch diffs, or post-fix code into prompts or repair execution. `scripts/prepare_idoft_samples.py` also skips developer patch downloads by default; pass `--download-developer-patches` only for separate evaluation-only analyses.

Important boundary:

- The repository includes the **metadata subset**, not cloned third-party repositories.
- The metadata is enough to reproduce the sample list and rebuild worktrees.
- The one-command runner clones/checks out the listed GitHub repositories by default.
- Users only need to run `scripts/prepare_idoft_samples.py` manually if they want to rebuild worktrees separately.
- The subset is not a claim that every test's pre-fix flaky behavior is re-reproduced; it is an executable known-flaky repair-candidate subset.
- PR/developer patch fields are excluded from the clean public metadata distributed by `scripts/package_release.sh`.

The construction protocol is documented in:

```text
docs/stabilityops_dataset_card.md
```

## Outputs

Each run writes:

```text
runs/experiments/<run_id>/
  results.jsonl
  eval.json
  run.log
  eval.log
  patches/
  validation_logs/
  rendered_prompts/
```

Evaluate an existing run manually:

```bash
python3 scripts/evaluate_results.py \
  --results runs/experiments/<run_id>/results.jsonl \
  --output-json runs/experiments/<run_id>/eval.json
```

In `eval.json`, `repair_success_rate` counts patches that pass the target test once, complete all configured post-fix reruns without failures, and are not rejected by the Patch Safety Filter.

## Packaging a Clean Release

Create a clean archive that excludes local caches, worktrees, model weights, and private experiment outputs:

```bash
bash scripts/package_release.sh stabilityops-release
```

This writes:

```text
dist/stabilityops-release/
dist/stabilityops-release.tar.gz
```

## Citation

If you use this artifact, please cite the corresponding paper or this repository. A placeholder `CITATION.cff` is included and should be updated with final publication metadata.

## License

MIT License. See `LICENSE`.
