#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-stabilityops-release}"
OUT_DIR="${OUT_DIR:-dist/$VERSION}"

cd "$(dirname "$0")/.."

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

copy_path() {
  local src="$1"
  if [[ -e "$src" ]]; then
    mkdir -p "$OUT_DIR/$(dirname "$src")"
    rsync -a "$src" "$OUT_DIR/$(dirname "$src")/"
  fi
}

for path in \
  README.md \
  ARTIFACT.md \
  .gitignore \
  LICENSE \
  CITATION.cff \
  requirements.txt \
  environment.yml \
  stability_agent \
  prompts \
  configs/stabilityops_qwen3_public_full721.json \
  configs/stabilityops_qwen3_public_smoke.json \
  examples \
  docs/research_protocol.md \
  docs/idoft_executable_subset_report.md \
  docs/generated/paper_results_summary.json \
  docs/generated/patch_safety_filter_examples.md \
  docs/generated/safety_audit_patches \
  data/metadata/idoft_verified_feasible.csv \
  data/metadata/idoft_verified_feasible.jsonl \
  data/metadata/idoft_verified_feasible_summary.json \
  data/metadata/subsets/idoft_verified_feasible_balanced_10_each.csv \
  data/metadata/subsets/idoft_verified_feasible_balanced_10_each.jsonl
do
  copy_path "$path"
done

for path in \
  scripts/create_vllm_env.sh \
  scripts/check_environment.sh \
  scripts/download_hf_model.py \
  scripts/setup_maven.sh \
  scripts/package_release.sh \
  scripts/run_stabilityops_qwen3.sh \
  scripts/run_qwen3_transform_gpu.sh \
  scripts/run_agent_experiment.py \
  scripts/evaluate_results.py \
  scripts/unsafe_patch_scan.py \
  scripts/vllm_start_services.sh \
  scripts/vllm_wait_ready.sh \
  scripts/vllm_status.sh \
  scripts/vllm_stop_services.sh \
  scripts/vllm_smoke_prompt.py \
  scripts/prepare_idoft_samples.py \
  scripts/build_smoke_idoft_samples.py \
  scripts/summarize_build_smoke.py \
  scripts/analyze_failure_modes.py \
  scripts/analyze_validation_funnel.py \
  scripts/analyze_developer_patch_similarity.py \
  scripts/analyze_statistical_robustness.py \
  scripts/generate_paper_figures.py
do
  copy_path "$path"
done

find "$OUT_DIR" -name ".DS_Store" -delete
find "$OUT_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$OUT_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

python3 - "$OUT_DIR" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
replacements = {
    "": "",
    ".": ".",
    "$PROJECT_DIR": "$PROJECT_DIR",
    "<remote-host>": "<remote-host>",
}
for path in root.rglob("*"):
    if not path.is_file():
        continue
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".gz", ".zip"}:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    original = text
    for old, new in replacements.items():
        text = text.replace(old, new)
    if text != original:
        path.write_text(text, encoding="utf-8")
PY

tar -czf "dist/$VERSION.tar.gz" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
echo "release_dir=$OUT_DIR"
echo "archive=dist/$VERSION.tar.gz"
