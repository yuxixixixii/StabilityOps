#!/usr/bin/env bash
set -eo pipefail

GPU_ID="${1:-}"
RUN_ID="${2:-stabilityops_qwen3_transform_smoke}"
EXPERIMENT_CONFIG="${3:-configs/stabilityops_typed_dsl_affected_smoke5_qwen3.json}"
RUN_RESUME="${RUN_RESUME:-0}"
RUNNER_SCRIPT="${RUNNER_SCRIPT:-scripts/run_stabilityops_experiment.py}"

if [[ -z "$GPU_ID" ]]; then
  echo "usage: bash scripts/run_qwen3_transform_gpu.sh <gpu_id> [run_id] [experiment_config]" >&2
  exit 2
fi

cd "$(dirname "$0")/.."

MODEL_ID="${QWEN3_MODEL_ID:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"
MODEL_PATH="${QWEN3_MODEL_PATH:-$MODEL_ID}"
SERVED_MODEL_NAME="${QWEN3_SERVED_MODEL_NAME:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"
MODEL_ALIAS="${QWEN3_MODEL_ALIAS:-qwen3_coder_30b}"
VLLM_PORT="${VLLM_PORT:-8003}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.85}"
VLLM_CONFIG="configs/vllm_models_local_qwen3_gpu${GPU_ID}.json"

if [[ ! -f "$EXPERIMENT_CONFIG" ]]; then
  echo "missing experiment config: $EXPERIMENT_CONFIG" >&2
  exit 1
fi

if [[ "$MODEL_PATH" == /* && ! -f "$MODEL_PATH/config.json" ]]; then
  echo "missing local model snapshot: $MODEL_PATH" >&2
  exit 1
fi

echo "== GPU status before start =="
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits || true
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits || true

cat > "$VLLM_CONFIG" << JSON
{
  "services": [
    {
      "alias": "$MODEL_ALIAS",
      "model": "$MODEL_PATH",
      "served_model_name": "$SERVED_MODEL_NAME",
      "cuda_visible_devices": "$GPU_ID",
      "host": "127.0.0.1",
      "port": $VLLM_PORT,
      "gpu_memory_utilization": $VLLM_GPU_MEMORY_UTILIZATION,
      "max_model_len": 16384,
      "extra_args": ["--enforce-eager"]
    }
  ]
}
JSON

export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
export LD_LIBRARY_PATH="/usr/lib/ollama/cuda_v13:/usr/lib/ollama/mlx_cuda_v13:${LD_LIBRARY_PATH:-}"

bash scripts/vllm_stop_services.sh "$VLLM_CONFIG" || true

VLLM_PYTHON="${VLLM_PYTHON:-/opt/anaconda3/bin/python}" \
bash scripts/vllm_start_services.sh "$VLLM_CONFIG"

cleanup() {
  bash scripts/vllm_stop_services.sh "$VLLM_CONFIG" || true
}
trap cleanup EXIT

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-1800}" POLL_SECONDS="${POLL_SECONDS:-10}" \
bash scripts/vllm_wait_ready.sh "$VLLM_CONFIG"

if [[ "$RUN_RESUME" == "1" ]]; then
  mkdir -p "runs/experiments/$RUN_ID"
  RUN_AGENT_EXTRA_ARGS=(--resume)
else
  rm -rf "runs/experiments/$RUN_ID"
  mkdir -p "runs/experiments/$RUN_ID"
  RUN_AGENT_EXTRA_ARGS=()
fi
if [[ -n "${RUN_AGENT_LIMIT:-}" ]]; then
  RUN_AGENT_EXTRA_ARGS+=(--limit "$RUN_AGENT_LIMIT")
fi
if [[ "${RUN_SKIP_VALIDATION:-0}" == "1" ]]; then
  RUN_AGENT_EXTRA_ARGS+=(--skip-validation)
fi
if [[ -n "${RUN_VALIDATION_WORKERS:-}" ]]; then
  RUN_AGENT_EXTRA_ARGS+=(--validation-workers "$RUN_VALIDATION_WORKERS")
fi

python3 -u "$RUNNER_SCRIPT" \
  --config "$EXPERIMENT_CONFIG" \
  --run-id "$RUN_ID" \
  "${RUN_AGENT_EXTRA_ARGS[@]}" \
  2>&1 | tee "runs/experiments/$RUN_ID/run.log"

python3 scripts/evaluate_results.py \
  --results "runs/experiments/$RUN_ID/results.jsonl" \
  --output-json "runs/experiments/$RUN_ID/eval.json" \
  2>&1 | tee "runs/experiments/$RUN_ID/eval.log"

echo "== finished =="
echo "run_dir=runs/experiments/$RUN_ID"
