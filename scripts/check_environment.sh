#!/usr/bin/env bash
set -euo pipefail

REQUIRE_GPU="${REQUIRE_GPU:-1}"
REQUIRE_JAVA="${REQUIRE_JAVA:-1}"

ok() {
  echo "[OK] $*"
}

warn() {
  echo "[WARN] $*" >&2
}

fail() {
  echo "[ERR] $*" >&2
  exit 1
}

need_cmd() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    ok "$name: $(command -v "$name")"
  else
    fail "missing command: $name"
  fi
}

cd "$(dirname "$0")/.."

need_cmd bash
need_cmd git
need_cmd curl

if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY'
import sys
print(f"[OK] python3: {sys.executable} {sys.version.split()[0]}")
if sys.version_info < (3, 8):
    raise SystemExit("[ERR] Python >= 3.8 is required for StabilityOps orchestration")
PY
else
  fail "missing command: python3"
fi

if [[ "$REQUIRE_GPU" == "1" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "[OK] nvidia-smi detected"
    nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader || true
  else
    fail "nvidia-smi not found. vLLM runs require an NVIDIA GPU environment."
  fi
else
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader || warn "nvidia-smi not found; GPU check skipped"
fi

if [[ "$REQUIRE_JAVA" == "1" ]]; then
  if command -v java >/dev/null 2>&1; then
    java -version 2>&1 | head -3
    java -version 2>&1 | grep -E 'version "1\.8|version "8|version "11|version "17' >/dev/null \
      || warn "Java is present, but IDoFT experiments were screened with Java 8. Some Maven projects may fail under this version."
  else
    fail "java not found. Install Java 8 for full Maven validation, or set RUN_SKIP_VALIDATION=1."
  fi
else
  command -v java >/dev/null 2>&1 && java -version 2>&1 | head -3 || warn "java not found; validation check skipped"
fi

echo "[OK] environment check completed"
