#!/usr/bin/env bash
set -euo pipefail

GPU_ID="${1:-}"
RUN_ID="${2:-stabilityops_qwen3_full721}"
EXPERIMENT_CONFIG="${3:-configs/stabilityops_qwen3_public_full721.json}"

if [[ -z "$GPU_ID" ]]; then
  echo "usage: bash scripts/run_stabilityops_qwen3.sh <gpu_id> [run_id] [config]" >&2
  echo "example: bash scripts/run_stabilityops_qwen3.sh 0 stabilityops_qwen3_smoke configs/stabilityops_qwen3_public_smoke.json" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

MODEL_ID="${QWEN3_MODEL_ID:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-}"
MODEL_REVISION="${MODEL_REVISION:-}"
MODEL_MAX_WORKERS="${MODEL_MAX_WORKERS:-4}"
VLLM_VENV="${VLLM_VENV:-$PWD/.venv-vllm}"

REQUIRE_JAVA="$([[ "${RUN_SKIP_VALIDATION:-0}" == "1" ]] && echo 0 || echo 1)" \
REQUIRE_GPU=1 \
bash scripts/check_environment.sh

choose_python() {
  if [[ -n "${VLLM_PYTHON:-}" ]]; then
    echo "$VLLM_PYTHON"
  elif [[ -x "$VLLM_VENV/bin/python" ]]; then
    echo "$VLLM_VENV/bin/python"
  elif [[ -x "/opt/anaconda3/bin/python" ]]; then
    echo "/opt/anaconda3/bin/python"
  else
    command -v python3
  fi
}

VLLM_PYTHON="$(choose_python)"

if ! "$VLLM_PYTHON" - <<'PY' >/dev/null 2>&1
import vllm, openai, huggingface_hub  # noqa: F401
PY
then
  if [[ "${AUTO_INSTALL_VLLM:-1}" != "1" ]]; then
    echo "vLLM dependencies are missing for $VLLM_PYTHON and AUTO_INSTALL_VLLM=0" >&2
    exit 1
  fi
  echo "setting up vLLM environment at $VLLM_VENV"
  VLLM_VENV="$VLLM_VENV" PROJECT_DIR="$PWD" bash scripts/create_vllm_env.sh
  VLLM_PYTHON="$VLLM_VENV/bin/python"
fi

if [[ "${RUN_SKIP_VALIDATION:-0}" != "1" ]]; then
  bash scripts/setup_maven.sh
fi

if [[ "${AUTO_PREPARE_DATA:-1}" == "1" ]]; then
  read -r DATASET_PATH CONFIG_LIMIT < <(
    python3 - "$EXPERIMENT_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(config["dataset"], config.get("limit") or "")
PY
  )
  PREPARE_LIMIT="${RUN_AGENT_LIMIT:-$CONFIG_LIMIT}"
  prepare_args=(
    --metadata "$DATASET_PATH"
    --workdir data/worktrees/idoft
    --patch-dir data/patches/idoft
    --log-dir "runs/experiments/$RUN_ID/prepare_logs"
    --output-jsonl "runs/experiments/$RUN_ID/prepare_samples.jsonl"
  )
  if [[ -n "$PREPARE_LIMIT" ]]; then
    prepare_args+=(--limit "$PREPARE_LIMIT")
  fi
  echo "preparing repositories from $DATASET_PATH limit=${PREPARE_LIMIT:-all}"
  python3 -u scripts/prepare_idoft_samples.py "${prepare_args[@]}"
fi

if [[ -z "${QWEN3_MODEL_PATH:-}" ]]; then
  echo "checking/downloading model: $MODEL_ID"
  download_args=(--model "$MODEL_ID" --max-workers "$MODEL_MAX_WORKERS")
  if [[ -n "$MODEL_CACHE_DIR" ]]; then
    download_args+=(--cache-dir "$MODEL_CACHE_DIR")
  fi
  if [[ -n "$MODEL_REVISION" ]]; then
    download_args+=(--revision "$MODEL_REVISION")
  fi
  QWEN3_MODEL_PATH="$("$VLLM_PYTHON" scripts/download_hf_model.py "${download_args[@]}" | tail -n 1)"
  export QWEN3_MODEL_PATH
fi

echo "model_path=$QWEN3_MODEL_PATH"
export VLLM_PYTHON
export RUN_RESUME="${RUN_RESUME:-1}"
export QWEN3_MODEL_ID="$MODEL_ID"
export QWEN3_SERVED_MODEL_NAME="${QWEN3_SERVED_MODEL_NAME:-$MODEL_ID}"

bash scripts/run_qwen3_transform_gpu.sh "$GPU_ID" "$RUN_ID" "$EXPERIMENT_CONFIG"
