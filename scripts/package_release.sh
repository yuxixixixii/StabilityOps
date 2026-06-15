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
  stabilityops/__init__.py \
  stabilityops/runtime.py \
  prompts/stabilityops_typed_action.md \
  prompts/stabilityops_action_revision.md \
  configs/stabilityops_qwen3_public_full721.json \
  configs/stabilityops_qwen3_public_smoke.json \
  examples \
  docs/stabilityops_dataset_card.md \
  docs/generated/paper_results_summary.json \
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
  scripts/run_stabilityops_experiment.py \
  scripts/evaluate_results.py \
  scripts/unsafe_patch_scan.py \
  scripts/vllm_start_services.sh \
  scripts/vllm_wait_ready.sh \
  scripts/vllm_status.sh \
  scripts/vllm_stop_services.sh \
  scripts/vllm_smoke_prompt.py \
  scripts/prepare_idoft_samples.py \
  scripts/build_smoke_idoft_samples.py \
  scripts/summarize_build_smoke.py
do
  copy_path "$path"
done

find "$OUT_DIR" -name ".DS_Store" -delete
find "$OUT_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$OUT_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

python3 - "$OUT_DIR" <<'PY'
import os
import pathlib
import re
import sys
import csv
import json

root = pathlib.Path(sys.argv[1])
replacements = {}
for old, new in [
    (os.environ.get("STABILITYOPS_SANITIZE_ABS_PROJECT", ""), "."),
    (os.environ.get("STABILITYOPS_SANITIZE_REMOTE_PROJECT", ""), "$PROJECT_DIR"),
    (os.environ.get("STABILITYOPS_SANITIZE_REMOTE_ALIAS", ""), "<remote-host>"),
]:
    if old:
        replacements[old.rstrip("/") + "/"] = new.rstrip("/") + "/"
        replacements[old.rstrip("/")] = new
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
    text = re.sub(r"/home/[^\s\"']+/[^\s\"']*/tools/apache-maven", "$PROJECT_DIR/tools/apache-maven", text)
    if text != original:
        path.write_text(text, encoding="utf-8")

drop_metadata_fields = {
    "PR Link",
    "pr_number",
    "Notes",
    "Status",
    "patch_cache_path",
    "patch_cache_ok",
    "patch_touches_test",
    "patch_url",
    "repo_cache_action",
    "repo_cache_dir",
    "repo_cache_ok",
}

def sanitize_csv(path: pathlib.Path) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fieldnames = [name for name in (reader.fieldnames or []) if name not in drop_metadata_fields]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})

def sanitize_jsonl(path: pathlib.Path) -> None:
    sanitized = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            for key in drop_metadata_fields:
                row.pop(key, None)
            sanitized.append(row)
    with path.open("w", encoding="utf-8") as handle:
        for row in sanitized:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

for rel in [
    "data/metadata/idoft_verified_feasible.csv",
    "data/metadata/subsets/idoft_verified_feasible_balanced_10_each.csv",
]:
    path = root / rel
    if path.exists():
        sanitize_csv(path)

for rel in [
    "data/metadata/idoft_verified_feasible.jsonl",
    "data/metadata/subsets/idoft_verified_feasible_balanced_10_each.jsonl",
]:
    path = root / rel
    if path.exists():
        sanitize_jsonl(path)
PY

tar -czf "dist/$VERSION.tar.gz" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
echo "release_dir=$OUT_DIR"
echo "archive=dist/$VERSION.tar.gz"
