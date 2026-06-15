# StabilityOps Dataset Card

This artifact includes metadata for 721 IDoFT-derived flaky-test repair candidates.

## Scope

- The metadata describes known flaky tests with known category labels.
- The artifact does not perform flaky-test detection or category prediction.
- Third-party repositories are not vendored. They are prepared on demand from the repository URL, detected SHA, module path, and test identifier recorded in the metadata.

## Included Files

- `data/metadata/idoft_verified_feasible.csv`: full 721-sample metadata.
- `data/metadata/idoft_verified_feasible.jsonl`: JSONL version of the same metadata.
- `data/metadata/subsets/idoft_verified_feasible_balanced_10_each.csv`: small balanced smoke subset.
- `data/metadata/idoft_verified_feasible_summary.json`: category and screening summary.

The clean release package removes PR/developer-patch/cache fields from these metadata files. The remaining fields are sufficient to rebuild pre-fix worktrees and run StabilityOps validation.

## Screening Meaning

`pipeline_status=verified_feasible` means that, under our Java/Maven screening environment, the target repository could be checked out, the target test file could be located, and the target test command could be executed successfully before repair experiments.

This does not mean the artifact proves that every test is currently reproducibly flaky. The repair setting follows known flaky-test repair: the input test and category are assumed to be known from the dataset.

## Reproduction

The one-command runner prepares the required repositories automatically:

```bash
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_smoke \
  configs/stabilityops_qwen3_public_smoke.json
```

For a full run, use:

```bash
bash scripts/run_stabilityops_qwen3.sh \
  0 \
  stabilityops_qwen3_full721 \
  configs/stabilityops_qwen3_public_full721.json
```

Prepared worktrees, patches, logs, model weights, and Maven caches are generated locally and are intentionally ignored by git.

Developer/PR patches are not downloaded by default and are not used by StabilityOps prompts or executor logic. They can be fetched only through an explicit evaluation-only flag in `scripts/prepare_idoft_samples.py`.
