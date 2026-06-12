#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
VLLM_VENV="${VLLM_VENV:-$PROJECT_DIR/.venv-vllm}"
MINIFORGE_DIR="${MINIFORGE_DIR:-$PROJECT_DIR/tools/miniforge3}"
PYTHON_BIN="${PYTHON_BIN:-}"

python_ok() {
  "$1" - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

if [ -z "$PYTHON_BIN" ]; then
  for candidate in \
    "$MINIFORGE_DIR/bin/python" \
    python3.11 \
    python3.10 \
    python3
  do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
    if [ -x "$candidate" ] && python_ok "$candidate"; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [ -z "$PYTHON_BIN" ]; then
  mkdir -p "$PROJECT_DIR/tools"
  installer="$PROJECT_DIR/tools/miniforge.sh"
  url="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
  echo "No Python >=3.10 found; installing Miniforge to $MINIFORGE_DIR"
  if [ ! -x "$MINIFORGE_DIR/bin/python" ]; then
    curl -L "$url" -o "$installer"
    bash "$installer" -b -p "$MINIFORGE_DIR"
  fi
  PYTHON_BIN="$MINIFORGE_DIR/bin/python"
fi

if ! python_ok "$PYTHON_BIN"; then
  echo "Python >=3.10 is required for vLLM, got: $($PYTHON_BIN --version)" >&2
  exit 2
fi

echo "using python: $PYTHON_BIN ($($PYTHON_BIN --version))"

if [ ! -d "$VLLM_VENV" ]; then
  "$PYTHON_BIN" -m venv "$VLLM_VENV"
fi

"$VLLM_VENV/bin/python" -m pip install --upgrade pip setuptools wheel
"$VLLM_VENV/bin/python" -m pip install \
  "vllm>=0.6.4" \
  "openai>=1.50.0" \
  "transformers>=4.45.0" \
  "accelerate>=0.34.0" \
  "pydantic>=2.8.0" \
  "tenacity>=8.5.0"

"$VLLM_VENV/bin/python" - <<'PY'
import importlib.util
import sys
print("python", sys.executable)
for name in ["vllm", "torch", "transformers", "openai"]:
    spec = importlib.util.find_spec(name)
    print(name, "OK" if spec else "MISSING")
PY
